"""Synthetic context tests: multi-shard import, append, cancellation, compression."""
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from support import TestProjection
from support import TestStores
from support import FakeSource, wait_until
from contracts import AdvisorError, COMPRESSION_VERSION
from model_context import ModelContext, _summary_key


def _projection(root, source):
    stores = TestStores(root)
    return stores, TestProjection(stores, source)


def _wait_ready(projection, account, user):
    def ready():
        snapshot = projection.snapshot(account, user)
        return snapshot if snapshot["state"] == "ready" else None

    return wait_until(ready)




class CompressionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = FakeSource()
        self.stores, self.projection = _projection(self.root, self.source)
        self.addCleanup(self.stores.close)
        self.addCleanup(self.projection.close)
        self.source.seed_pages("user-a", 400, text_size=60)
        self.projection.prepare("synthetic-account-1", "user-a")
        wait_until(lambda: self.projection.snapshot("synthetic-account-1", "user-a")["state"]
                   == "ready")

    def test_over_budget_input_is_compacted_hierarchically_then_cached(self):
        calls = []

        def compactor(text, range_key, level, target):
            calls.append((range_key, level))
            return "概" * 700

        text = self.projection.build_model_context(
            "synthetic-account-1", "user-a", None, 4000, compactor, "fp-1")
        self.assertLessEqual(len(text), 4000)
        self.assertIn("[摘要]", text)
        levels = {level for _key, level in calls}
        self.assertIn(1, levels)
        self.assertTrue(any(level >= 2 for level in levels), "hierarchy must deepen when needed")
        self.assertGreater(len(calls), 1)
        self.assertEqual(len(calls), len({key for key, _level in calls}))
        before = len(calls)
        cached = self.projection.build_model_context(
            "synthetic-account-1", "user-a", None, 4000, compactor, "fp-1")
        self.assertEqual(len(calls), before, "cached summaries must be reused")
        self.assertEqual(cached, text)
        other_fp = self.projection.build_model_context(
            "synthetic-account-1", "user-a", None, 4000, compactor, "fp-2")
        self.assertGreater(len(calls), before, "a new model fingerprint re-summarizes")
        self.assertLessEqual(len(other_fp), 4000)

    def test_short_history_is_never_compacted(self):
        def compactor(text, range_key, level, target):  # pragma: no cover - must not run
            raise AssertionError("compaction must not run for a fitting history")

        text = self.projection.build_model_context(
            "synthetic-account-1", "user-a", 20, 100000, compactor, "fp-1")
        self.assertNotIn("[摘要]", text)
        self.assertIn("[最近聊天记录]", text)

    def test_concurrent_builders_share_single_flight_summaries(self):
        import time as _time
        calls = []
        lock = threading.Lock()

        def compactor(text, range_key, level, target):
            with lock:
                calls.append((range_key, level))
            _time.sleep(0.05)
            return "概" * 600

        outcomes = []

        def build():
            outcomes.append(self.projection.build_model_context(
                "synthetic-account-1", "user-a", None, 3000, compactor, "fp-9"))

        threads = [threading.Thread(target=build) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(len(calls), len({key for key, _level in calls}),
                         "a source range must be summarized once")
        self.assertEqual(len(outcomes), 2)
        self.assertEqual(outcomes[0], outcomes[1])


class AdaptiveSummaryTests(unittest.TestCase):
    """Only synthetic text: the complete model input is the meaningful limit."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.stores = TestStores(Path(temp.name))
        self.addCleanup(self.stores.close)
        self.database = self.stores.account("synthetic-account-1")
        self.model = ModelContext()
        self.addCleanup(self.model.close)

    @staticmethod
    def segment(text, key):
        return {"key": key, "text": text, "chars": len(text) + 1,
                "level": 0, "coverage": [key]}

    def fit(self, segments, budget, compactor, **kwargs):
        return self.model._fit(segments, budget, self.database, "user-a", compactor, "fp",
                               **kwargs)

    def test_english_summary_over_2000_and_advisory_target_can_still_fit(self):
        segments = [self.segment(letter * 5600, letter) for letter in "ABC"]
        segments.append(self.segment("recent" * 80, "recent"))
        summary = ("English fact. " * 180).strip()
        calls = []

        def compact(text, key, level, target):
            calls.append((len(text), target))
            return summary

        result = self.fit(segments, 10000, compact)
        self.assertGreater(len(summary), 2000)
        self.assertTrue(any(len(summary) > target for _size, target in calls))
        self.assertIn(summary, result)
        self.assertLessEqual(len(result), 10000)
        self.assertIn("C" * 5600, result, "recent original messages must stay intact")
        before = len(calls)
        self.assertEqual(self.fit(segments, 10000, compact), result)
        self.assertEqual(len(calls), before, "useful oversize summaries are cached")

    def test_oversized_chunk_is_retained_while_neighbours_shrink_enough(self):
        segments = [self.segment("A" * 5500, "a"), self.segment("B" * 5500, "b"),
                    self.segment("recent" * 160, "recent")]
        targets = []

        def compact(text, key, level, target):
            targets.append(target)
            return "E" * 6182 if text.startswith("A") else "Useful English summary."

        result = self.fit(segments, 7000, compact)
        self.assertLessEqual(len(result), 7000)
        self.assertGreater(max(targets), 2000, "guidance is allocated from remaining space")
        self.assertIn("A" * 5500, result)
        self.assertNotIn("[摘要] " + "A" * 5500, result)
        self.assertIn("[摘要] Useful English summary.", result)
        self.assertNotIn("E" * 6182, result)
        rows = self.database._rows("SELECT text, coverage_json FROM summaries")
        self.assertEqual(len(rows), 1, "unhelpful model output must not be cached")
        self.assertEqual(json.loads(rows[0]["coverage_json"]), ["b"])

    def test_no_progress_gets_one_retry_then_clear_failure_without_bad_cache(self):
        segments = [self.segment("A" * 1000, "a")]
        calls = []

        def compact(text, key, level, target):
            calls.append(target)
            return text + " More details."

        with self.assertRaises(AdvisorError) as caught:
            self.fit(segments, 700, compact)
        self.assertEqual(caught.exception.code, "compression-failed")
        self.assertIn("连续未能缩短", str(caught.exception))
        self.assertEqual(len(calls), 2)
        self.assertLess(calls[1], calls[0])
        self.assertEqual(segments[0]["text"], "A" * 1000)
        self.assertEqual(self.database._rows("SELECT * FROM summaries"), [])

    def test_second_attempt_can_recover_from_no_progress(self):
        segments = [self.segment("A" * 1000, "a")]
        calls = []

        def compact(text, key, level, target):
            calls.append(target)
            return text if len(calls) == 1 else "Useful neutral facts."

        result = self.fit(segments, 700, compact)
        self.assertLessEqual(len(result), 700)
        self.assertIn("Useful neutral facts.", result)
        self.assertEqual(len(calls), 2)

    def test_legacy_valid_summary_cache_is_reused(self):
        text = "A" * 1000
        segments = [self.segment(text, "a")]
        key = _summary_key("user-a", 1, ["a"], text, 175)
        self.database.save_summary("user-a", key, COMPRESSION_VERSION, "fp", 1,
                                   "Cached neutral facts.", ["a"])

        def no_request(*_args):
            self.fail("existing useful cache should not call the model again")

        result = self.fit(segments, 700, no_request)
        self.assertIn("Cached neutral facts.", result)

    def test_changed_available_budget_reuses_useful_summaries_of_same_source(self):
        segments = [self.segment("A" * 5500, "a"), self.segment("B" * 5500, "b"),
                    self.segment("recent" * 160, "recent")]
        calls = []

        def compact(text, key, level, target):
            calls.append(target)
            return "Useful neutral facts."

        first = self.fit(segments, 7000, compact)
        self.assertEqual(len(calls), 1, "stop once the first chunk makes all remaining originals fit")

        def no_request(*_args):
            self.fail("a changed advisory budget must not invalidate useful source summaries")

        second = self.fit(segments, 7200, no_request)
        self.assertEqual(second, first)
        self.assertLessEqual(len(second), 7200)

    def test_byte_budget_accepts_longer_english_when_it_uses_fewer_bytes(self):
        segments = [self.segment("中" * 1000, "a")]
        summary = "E" * 1500
        result = self.fit(segments, 500, lambda *_args: summary, budget_bytes=2000)
        self.assertIn(summary, result)
        self.assertGreater(len(summary), len(segments[0]["text"]))
        self.assertLessEqual(len(result.encode("utf-8")), 2000)

    def test_byte_budget_rejects_shorter_but_more_expensive_cjk_summary(self):
        segments = [self.segment("E" * 1200, "a")]
        with self.assertRaisesRegex(AdvisorError, "连续未能缩短"):
            self.fit(segments, 5000, lambda *_args: "中" * 500, budget_bytes=1000)
        self.assertEqual(self.database._rows("SELECT * FROM summaries"), [])

    def test_early_stop_keeps_untouched_ranges_in_exact_order_and_reuses_cache(self):
        import model_context
        segments = [self.segment("A" * 5500, "a"), self.segment("B" * 5500, "b"),
                    self.segment("🙂中文" * 90, "recent")]
        calls, coverage = [], []
        original_render = model_context._render

        def compact(text, key, level, target):
            calls.append(text)
            self.assertEqual(text, "A" * 5500, "later original chunk needs no model call")
            return "Neutral facts."

        def render(older, recent):
            coverage.append([part for item in older + recent for part in item["coverage"]])
            return original_render(older, recent)

        with patch("model_context._render", side_effect=render):
            result = self.fit(segments, 1750, compact, budget_bytes=7000)
        self.assertEqual(len(calls), 1)
        self.assertEqual(coverage[-1], ["a", "b", "recent"])
        self.assertIn("B" * 5500, result)
        self.assertIn("🙂中文" * 90, result)
        self.assertLessEqual(len(result.encode("utf-8")), 7000)

        def no_request(*_args):
            self.fail("cached first chunk should make the complete context fit immediately")

        self.assertEqual(self.fit(segments, 1750, no_request, budget_bytes=7000), result)

    def test_cached_early_stop_rechecks_account_and_cancellation(self):
        segments = [self.segment("A" * 5500, "a"), self.segment("B" * 5500, "b"),
                    self.segment("recent" * 160, "recent")]
        self.fit(segments, 7000, lambda *_args: "Neutral facts.")
        original_summary = self.database.summary
        cancelled = threading.Event()

        def cached_then_cancel(*args):
            result = original_summary(*args)
            if result:
                cancelled.set()
            return result

        with patch.object(self.database, "summary", side_effect=cached_then_cancel):
            with self.assertRaises(AdvisorError) as caught:
                self.fit(segments, 7000, lambda *_args: self.fail("cache expected"), cancel=cancelled)
        self.assertEqual(caught.exception.code, "stopping")

        changed = False

        def cached_then_change(*args):
            nonlocal changed
            result = original_summary(*args)
            if result:
                changed = True
            return result

        def account_check():
            if changed:
                raise AdvisorError("account-changed", "Account changed")

        with patch.object(self.database, "summary", side_effect=cached_then_change):
            with self.assertRaises(AdvisorError) as caught:
                self.fit(segments, 7000, lambda *_args: self.fail("cache expected"), check=account_check)
        self.assertEqual(caught.exception.code, "account-changed")

    def test_early_stop_counts_full_prefix_and_managed_line_overhead(self):
        import model_context
        segments = [self.segment("A" * 5500, "a"), self.segment("B" * 5500, "b"),
                    self.segment("🙂中文" * 90, "recent")]
        prefix = "合成资料边界\n"

        def managed_cost(text):
            return len(text.encode("utf-8")) + 16 * sum(
                max(1, (len(line) + 699) // 700) for line in text.split("\n"))

        partial = prefix + model_context._render(
            [self.segment("[摘要] Facts.", "a"), segments[1]], [segments[2]])
        budget = managed_cost(partial) - 1
        calls = []

        def compact(*_args):
            calls.append(True)
            return "Facts."

        result = self.fit(segments, 1750, compact, budget_bytes=budget,
                          budget_cost=managed_cost, prefix=prefix)
        self.assertEqual(len(calls), 2, "the first summary fits raw bytes but exceeds managed-read cost")
        self.assertLessEqual(managed_cost(prefix + result), budget)

    def test_small_budget_includes_summary_labels_and_newlines(self):
        segments = [self.segment("A" * 200, "a"), self.segment("recent" * 3, "recent")]
        result = self.fit(segments, 70, lambda *_args: "Facts.")
        self.assertLessEqual(len(result), 70)
        self.assertIn("Facts.", result)
        self.assertIn("recent" * 3, result)

    def test_cancelled_summary_never_writes_even_when_output_is_useful(self):
        cancel = threading.Event()
        segments = [self.segment("A" * 1000, "a")]

        def compact(*_args):
            cancel.set()
            return "Useful neutral facts."

        with self.assertRaises(AdvisorError) as caught:
            self.fit(segments, 700, compact, cancel=cancel)
        self.assertEqual(caught.exception.code, "stopping")
        self.assertEqual(self.database._rows("SELECT * FROM summaries"), [])


if __name__ == "__main__":
    unittest.main()
