"""Saved generation outcomes survive source checks, retries and interrupted work."""
import json
import sys
import tempfile
import threading
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugin-sdk" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from insights import Insights
from prompts import InsightError, UNIFIED_VERSION, format_line, message_hash, row_from_view
from store import AnalysisStore, result_bindings

ACCOUNT, USER, TARGET = "synthetic-account", "synthetic-friend", "synthetic-target"


class SavedResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.account = ACCOUNT
        self.profile = "profile-a"
        self.model = "model-a"
        self.history = {"state": "ready", "sourceFingerprint": "source-a",
                        "currentSourceFingerprint": "source-a", "epoch": "epoch-a", "revision": 1}
        self.fail = set()
        self.status_reads = 0
        self.status_hook = None
        self.views = {key: {"id": key, "seq": index, "sortKey": json.dumps([index, "synthetic", index]),
                           "senderId": USER, "senderResolved": True, "side": "other", "kind": "text",
                           "text": "Synthetic chat " + key, "raw": "{}", "time": index * 1000}
                      for index, key in enumerate(("earlier", TARGET), 1)}
        self.latest = TARGET
        core = SimpleNamespace(scope=lambda: {"account": self.account}, get=self.get, post=self.post,
                               history_status=self.history_status)
        service = self.service = Insights.__new__(Insights)
        service.plugin = SimpleNamespace(core=core, data_dir=Path(self.temp.name))
        service.models = SimpleNamespace(services=self.models)
        service.clock = lambda: 100.0
        service.lock = threading.RLock()
        service.stores, service.entries, service.subscriptions = {}, {}, {}
        service.queue, service.active = OrderedDict(), {}
        service.wake = threading.Event()
        self.store = service.store(ACCOUNT)
        self.store.set(USER, True)
        self.addCleanup(lambda: [store.close() for store in service.stores.values()])
        self.analysis = {"summary": "Synthetic summary", "meaning": "Synthetic meaning",
                         "uncertainty": "Synthetic uncertainty", "responseDirection": "Synthetic direction",
                         "evidence": [{"kind": "chat", "messageId": TARGET,
                                       "text": format_line(row_from_view(self.views[TARGET]))}]}
        self.replies = [{"label": str(index), "text": "Synthetic reply " + str(index)} for index in range(3)]

    def models(self, *args, **kwargs):
        if "models" in self.fail:
            raise RuntimeError("synthetic model metadata unavailable")
        return [{"stamp": self.model}]

    def get(self, path, **query):
        if path in self.fail:
            raise RuntimeError("synthetic read unavailable")
        if path == "settings":
            return {"revision": 0, "values": {"replyStyle": "natural"}}
        if path == "history/recent":
            return {"messages": [self.views[self.latest]]}
        if path == "human/" + USER:
            return {"stamp": self.profile}
        raise AssertionError(path)

    def post(self, path, body):
        if path in self.fail:
            raise RuntimeError("synthetic read unavailable")
        if path == "history/rows":
            return {"messages": [self.views[key] for key in body["ids"] if key in self.views]}
        raise AssertionError(path)

    def history_status(self, *args, **kwargs):
        if "history/status" in self.fail:
            raise RuntimeError("synthetic history unavailable")
        self.status_reads += 1
        if self.status_hook:
            self.status_hook(self.status_reads)
        return dict(self.history)

    def seed(self, suggestions="ready"):
        generation = "synthetic-generation"
        self.store.begin(USER, [TARGET], generation, parts=("analysis", "suggestions"), unified=True)
        binding = ("source-a", self.model + ":" + UNIFIED_VERSION, self.profile, "epoch-a")
        hashes = {key: message_hash(row_from_view(view)) for key, view in self.views.items()}
        self.store.bind(USER, [TARGET], generation, binding, hashes, "context-a")
        self.store.finish(USER, [TARGET], generation, "ready", results={TARGET: self.analysis}, part="analysis")
        if suggestions != "running":
            self.store.finish(USER, [TARGET], generation, suggestions,
                              results={TARGET: self.replies} if suggestions == "ready" else None,
                              error="synthetic failure" if suggestions == "error" else None, part="suggestions")
        return generation

    def item(self):
        return self.service.status(ACCOUNT, USER, [TARGET])["items"][0]

    def test_ready_outputs_are_returned_and_copy_uses_saved_candidate(self):
        generation = self.seed()
        item = self.item()
        self.assertEqual((item["state"], item["sourceState"], item["applicability"]), ("ready", "verified", "current"))
        self.assertTrue(item["copyAllowed"])
        self.assertEqual(item["analysis"], self.analysis)
        self.assertEqual(item["replies"], self.replies)
        self.assertEqual(self.service.copy(ACCOUNT, USER, TARGET, generation, 1)["text"], self.replies[1]["text"])

    def test_reading_and_transient_core_failure_never_mutate_or_cancel(self):
        self.seed()
        before = self.store.rows(USER)
        cancelled = threading.Event()
        self.service.active[(ACCOUNT, USER, TARGET)] = SimpleNamespace(account=ACCOUNT, user=USER, ids=[TARGET], cancel=cancelled.set)
        for mode in ("reading", "history/status", "history/rows"):
            with self.subTest(mode=mode):
                self.history["state"] = "reading" if mode == "reading" else "ready"
                self.profile = "temporary-unready-proof"
                self.fail = set() if mode == "reading" else {mode}
                item = self.item()
                self.assertEqual(item["state"], "ready")
                self.assertEqual(item["applicability"], "unverified")
                self.assertTrue(item["copyAllowed"])
                self.assertEqual(item["replies"], self.replies)
                self.assertEqual(self.store.rows(USER), before)
                self.assertFalse(cancelled.is_set())
        self.history["state"], self.profile, self.fail = "ready", "profile-a", set()
        self.assertEqual(self.item()["sourceState"], "verified")
        self.assertEqual(self.store.rows(USER), before)

    def test_profile_or_model_update_preserves_ready_results_as_reference(self):
        generation = self.seed()
        before = self.store.rows(USER)
        self.profile, self.model = "profile-b", "model-b"
        item = self.item()
        self.assertEqual((item["state"], item["applicability"], item["sourceState"]), ("ready", "historical", "verified"))
        self.assertTrue(item["copyAllowed"])
        self.assertEqual(self.service.copy(ACCOUNT, USER, TARGET, generation, 0)["text"], self.replies[0]["text"])
        self.assertEqual(self.store.rows(USER), before)

    def test_copy_does_not_require_latest_profile_or_model_reads(self):
        generation = self.seed()
        self.fail = {"history/recent", "human/" + USER, "models"}
        self.assertTrue(self.item()["copyAllowed"])
        self.assertEqual(self.service.copy(ACCOUNT, USER, TARGET, generation, 2)["text"], self.replies[2]["text"])

    def test_new_message_and_rebuilt_identical_projection_keep_old_copy_valid(self):
        self.seed()
        self.views["newer"] = {**self.views[TARGET], "id": "newer", "seq": 3, "time": 3000}
        self.latest = "newer"
        self.history.update(epoch="rebuilt-epoch", revision=99)
        item = self.item()
        self.assertEqual((item["state"], item["applicability"]), ("ready", "historical"))
        self.assertTrue(item["copyAllowed"])

    def test_changed_uncited_context_or_source_keeps_saved_outputs_copyable(self):
        generation = self.seed()
        before = self.store.rows(USER)
        for kind in ("context", "source", "missing"):
            with self.subTest(kind=kind):
                original = self.views["earlier"].copy()
                if kind == "context":
                    self.views["earlier"]["text"] = "changed premise"
                elif kind == "source":
                    self.history["currentSourceFingerprint"] = "source-b"
                else:
                    self.views.pop("earlier")
                item = self.item()
                self.assertEqual((item["state"], item["sourceState"], item["applicability"]), ("ready", "changed", "needs-update"))
                self.assertTrue(item["copyAllowed"])
                self.assertEqual(item["analysis"], self.analysis)
                self.assertEqual(item["replies"], self.replies)
                # The legacy RPC keeps its validation; the UI copies displayed text directly.
                with self.assertRaises(InsightError) as error:
                    self.service.copy(ACCOUNT, USER, TARGET, generation, 0)
                self.assertEqual(error.exception.code, "source-changed")
                self.views["earlier"] = original
                self.history["currentSourceFingerprint"] = "source-a"
                self.assertEqual(self.store.rows(USER), before)

    def test_source_change_during_validation_is_temporary_not_persistent(self):
        self.seed()
        self.status_hook = lambda count: self.history.update(epoch="epoch-b") if count == 2 else None
        item = self.item()
        self.assertEqual(item["sourceState"], "checking")
        self.assertTrue(item["copyAllowed"])
        self.assertEqual(self.store.rows(USER)[0]["state"], "ready")
        self.status_hook = None
        self.assertTrue(self.item()["copyAllowed"])

    def test_copy_rechecks_fresh_ownership_after_a_temporary_failure(self):
        generation = self.seed()
        self.assertTrue(self.item()["copyAllowed"])
        before = self.store.rows(USER)
        calls = []

        def status(*args, **kwargs):
            calls.append(kwargs.get("fresh"))
            if len(calls) == 1:
                raise RuntimeError("synthetic ownership lookup temporarily unavailable")
            return dict(self.history)

        self.service.core.history_status = status
        with patch("insights.COPY_CHECK_DELAY", 0):
            result = self.service.copy(ACCOUNT, USER, TARGET, generation, 0)
        self.assertEqual(result["text"], self.replies[0]["text"])
        self.assertEqual(calls, [True, True, True])
        self.assertEqual(self.store.rows(USER), before)

    def test_copy_rechecks_when_import_finishes_after_a_temporary_empty_read(self):
        generation = self.seed()
        self.assertTrue(self.item()["copyAllowed"])
        self.status_reads = 0
        before = self.store.rows(USER)
        reads = []

        def status_hook(count):
            self.history.update(state="reading" if count == 1 else "ready", epoch="rebuilt-epoch")

        def post(path, body):
            reads.append(path)
            if len(reads) == 1:
                return {"messages": []}
            return self.post(path, body)

        self.status_hook = status_hook
        self.service.core.post = post
        with patch("insights.COPY_CHECK_DELAY", 0):
            result = self.service.copy(ACCOUNT, USER, TARGET, generation, 1)
        self.assertEqual(result["text"], self.replies[1]["text"])
        self.assertEqual(self.status_reads, 4)
        self.assertEqual(len(reads), 2)
        self.assertEqual(self.store.rows(USER), before)

    def test_copy_rechecks_a_refresh_that_starts_during_the_check(self):
        generation = self.seed()
        self.status_hook = lambda count: self.history.update(state="reading" if count == 2 else "ready")
        with patch("insights.COPY_CHECK_DELAY", 0):
            self.assertEqual(self.service.copy(ACCOUNT, USER, TARGET, generation, 2)["text"], self.replies[2]["text"])
        self.assertEqual(self.status_reads, 4)

    def test_copy_temporary_failures_stop_at_attempt_limit_without_mutation(self):
        generation = self.seed()
        before = self.store.rows(USER)
        self.history["state"] = "reading"
        with patch("insights.COPY_CHECK_DELAY", 0), self.assertRaises(InsightError) as error:
            self.service.copy(ACCOUNT, USER, TARGET, generation, 0)
        self.assertEqual(error.exception.code, "source-unavailable")
        self.assertEqual(self.status_reads, 6)
        self.assertEqual(self.store.rows(USER), before)

    def test_copy_does_not_begin_another_check_after_retry_window(self):
        generation = self.seed()
        self.history["state"] = "reading"
        with patch("insights.time.monotonic", side_effect=[0, 13]), self.assertRaises(InsightError) as error:
            self.service.copy(ACCOUNT, USER, TARGET, generation, 0)
        self.assertEqual(error.exception.code, "source-unavailable")
        self.assertEqual(self.status_reads, 2)

    def test_copy_detects_account_switch_before_retrying(self):
        generation = self.seed()

        def unavailable(count):
            self.account = "another-account"
            raise RuntimeError("synthetic transient lookup failure")

        self.status_hook = unavailable
        with patch("insights.COPY_CHECK_DELAY", 0), self.assertRaises(InsightError) as error:
            self.service.copy(ACCOUNT, USER, TARGET, generation, 0)
        self.assertEqual(error.exception.code, "account-changed")
        self.assertEqual(self.status_reads, 1)

    def test_copy_does_not_start_another_check_when_wait_reaches_deadline(self):
        generation = self.seed()
        self.history["state"] = "reading"
        with patch("insights.time.monotonic", side_effect=[0, 11.9, 11.95, 12.1]), \
                patch("insights.time.sleep") as sleep, self.assertRaises(InsightError) as error:
            self.service.copy(ACCOUNT, USER, TARGET, generation, 0)
        self.assertEqual(error.exception.code, "source-unavailable")
        sleep.assert_called_once()
        self.assertLessEqual(sleep.call_args.args[0], 0.051)
        self.assertEqual(self.status_reads, 2)

    def test_copy_detects_disabled_switch_or_new_version_before_retrying(self):
        for change in ("disabled", "new-version"):
            with self.subTest(change=change):
                self.store.set(USER, True)
                generation = self.seed()
                self.status_reads = 0

                def unavailable(count):
                    if change == "disabled":
                        self.store.set(USER, False)
                    else:
                        self.store.begin(USER, [TARGET], "new-generation", parts=("suggestions",), unified=True)
                    raise RuntimeError("synthetic transient lookup failure")

                self.status_hook = unavailable
                with patch("insights.COPY_CHECK_DELAY", 0), self.assertRaises(InsightError) as error:
                    self.service.copy(ACCOUNT, USER, TARGET, generation, 0)
                self.assertEqual(error.exception.code, "request-conflict")
                self.assertEqual(self.status_reads, 1)

    def test_real_source_change_after_temporary_failure_is_not_retried_or_allowed(self):
        generation = self.seed()

        def status_hook(count):
            if count == 1:
                raise RuntimeError("synthetic temporary lookup failure")
            self.history["currentSourceFingerprint"] = "different-source"

        self.status_hook = status_hook
        with patch("insights.COPY_CHECK_DELAY", 0), self.assertRaises(InsightError) as error:
            self.service.copy(ACCOUNT, USER, TARGET, generation, 0)
        self.assertEqual(error.exception.code, "source-changed")
        self.assertEqual(self.status_reads, 3)

    def test_partial_retry_only_regenerates_failed_part_with_its_own_binding(self):
        original = self.seed("error")
        old_binding = result_bindings(self.store.rows(USER)[0])["analysis"]
        self.profile = "profile-b"
        self.service.retry(ACCOUNT, USER, TARGET, "all", original)
        spec = self.service.queue[(ACCOUNT, USER)]["jobs"][TARGET]
        self.assertEqual(spec["parts"], ("suggestions",))
        self.assertIsNone(spec["preserved_context"])
        self.assertEqual(self.item()["analysis"], self.analysis)
        self.assertEqual(result_bindings(self.store.rows(USER)[0])["analysis"], old_binding)
        hashes = {key: message_hash(row_from_view(view)) for key, view in self.views.items()}
        binding = ("source-a", self.model + ":" + UNIFIED_VERSION, self.profile, "epoch-a")
        self.store.bind(USER, [TARGET], spec["generation"], binding, hashes, "context-b")
        self.store.finish(USER, [TARGET], spec["generation"], "ready", results={TARGET: self.replies}, part="suggestions")
        bindings = result_bindings(self.store.rows(USER)[0])
        self.assertEqual(bindings["analysis"], old_binding)
        self.assertEqual(bindings["suggestions"]["profile_stamp"], "profile-b")
        self.assertTrue(self.item()["copyAllowed"])
        before = self.store.rows(USER)
        self.store.finish(USER, [TARGET], original, "ready", results={TARGET: [{"label": "bad", "text": "late"}]}, part="suggestions")
        self.service.retry(ACCOUNT, USER, TARGET, "all", spec["generation"])
        self.assertEqual(self.store.rows(USER), before)

    def reopen(self):
        self.store.close()
        self.store = self.service.stores[ACCOUNT] = AnalysisStore(Path(self.temp.name), ACCOUNT)

    def test_restart_converts_only_unfinished_parts_to_retryable_error(self):
        generation = self.seed("running")
        self.reopen()
        item = self.item()
        self.assertEqual((item["state"], item["analysisState"], item["suggestionsState"]), ("partial", "ready", "error"))
        self.assertEqual(item["analysis"], self.analysis)
        self.assertIn("中断", item["suggestionsError"])
        self.assertFalse(item["copyAllowed"])
        before = self.store.rows(USER)
        self.store.finish(USER, [TARGET], generation, "ready", results={TARGET: self.replies}, part="suggestions")
        self.assertEqual(self.store.rows(USER), before)

    def test_new_suggestions_can_copy_while_old_analysis_has_changed_source(self):
        generation = self.seed("error")
        self.views["earlier"]["text"] = "corrected synthetic premise"
        self.service.retry(ACCOUNT, USER, TARGET, "suggestions", generation)
        spec = self.service.queue[(ACCOUNT, USER)]["jobs"][TARGET]
        hashes = {key: message_hash(row_from_view(view)) for key, view in self.views.items()}
        binding = ("source-a", self.model + ":" + UNIFIED_VERSION, self.profile, "epoch-a")
        self.store.bind(USER, [TARGET], spec["generation"], binding, hashes, "context-b")
        self.store.finish(USER, [TARGET], spec["generation"], "ready", results={TARGET: self.replies}, part="suggestions")
        item = self.item()
        self.assertEqual(item["analysisSourceState"], "changed")
        self.assertEqual(item["suggestionsSourceState"], "verified")
        self.assertTrue(item["copyAllowed"])
        self.assertEqual(self.service.copy(ACCOUNT, USER, TARGET, spec["generation"], 1)["text"], self.replies[1]["text"])

    def test_malformed_source_binding_is_unavailable_without_losing_result(self):
        self.seed()
        self.store.write(lambda c: c.execute("UPDATE message_analyses SET result_bindings_json=?",
                                            (json.dumps({"analysis": "bad", "suggestions": "bad"}),)))
        item = self.item()
        self.assertEqual(item["sourceState"], "unavailable")
        self.assertEqual(item["replies"], self.replies)
        self.assertTrue(item["copyAllowed"])

    def test_explicit_regeneration_keeps_saved_replies_until_a_successful_replacement(self):
        generation = self.seed()
        original_binding = result_bindings(self.store.rows(USER)[0])["suggestions"]
        self.service.retry(ACCOUNT, USER, TARGET, "suggestions", generation)
        spec = self.service.queue[(ACCOUNT, USER)]["jobs"][TARGET]
        self.assertNotEqual(spec["generation"], generation)
        self.assertEqual(spec["parts"], ("suggestions",))
        pending = self.item()
        self.assertEqual((pending["state"], pending["suggestionsState"]), ("pending", "pending"))
        self.assertEqual(pending["replies"], self.replies)
        self.assertTrue(pending["copyAllowed"])
        self.profile = "new-profile"
        binding = ("source-a", self.model + ":" + UNIFIED_VERSION, self.profile, "epoch-a")
        hashes = {key: message_hash(row_from_view(view)) for key, view in self.views.items()}
        self.store.bind(USER, [TARGET], spec["generation"], binding, hashes, "context-new")
        self.assertEqual(self.item()["replies"], self.replies)
        self.store.finish(USER, [TARGET], spec["generation"], "error", error="synthetic regeneration failure", part="suggestions")
        failed = self.item()
        self.assertEqual((failed["state"], failed["suggestionsState"]), ("partial", "error"))
        self.assertEqual(failed["replies"], self.replies)
        self.assertEqual(result_bindings(self.store.rows(USER)[0])["suggestions"], original_binding)
        self.assertEqual(self.service.copy(ACCOUNT, USER, TARGET, spec["generation"], 0)["text"], self.replies[0]["text"])
        self.service.retry(ACCOUNT, USER, TARGET, "suggestions", spec["generation"])
        replacement = self.service.queue[(ACCOUNT, USER)]["jobs"][TARGET]
        new_replies = [{"label": str(index), "text": "Replacement " + str(index)} for index in range(3)]
        self.store.bind(USER, [TARGET], replacement["generation"], binding, hashes, "context-new")
        self.store.finish(USER, [TARGET], replacement["generation"], "ready", results={TARGET: new_replies}, part="suggestions")
        self.assertEqual(self.item()["replies"], new_replies)
        self.assertEqual(self.item()["analysis"], self.analysis)
        self.assertEqual(result_bindings(self.store.rows(USER)[0])["suggestions"]["profile_stamp"], "new-profile")

    def test_cancelled_or_restarted_regeneration_retains_the_last_saved_result(self):
        generation = self.seed()
        self.service.retry(ACCOUNT, USER, TARGET, "suggestions", generation)
        self.service.cancel(ACCOUNT, USER)
        item = self.item()
        self.assertEqual(item["suggestionsState"], "error")
        self.assertEqual(item["replies"], self.replies)
        self.assertTrue(item["copyAllowed"])
        self.service.retry(ACCOUNT, USER, TARGET, "analysis", item["version"])
        self.reopen()
        item = self.item()
        self.assertEqual(item["analysisState"], "error")
        self.assertEqual(item["analysis"], self.analysis)
        self.assertEqual(item["replies"], self.replies)
        self.assertTrue(item["copyAllowed"])

    def test_legacy_stale_json_is_recovered_but_invalid_payload_is_not(self):
        self.seed()
        self.store.write(lambda c: c.execute("UPDATE message_analyses SET state='stale',analysis_state='stale',"
                                           "suggestions_state='stale',result_bindings_json=NULL"))
        self.reopen()
        self.assertEqual(self.item()["state"], "ready")
        self.assertTrue(self.item()["copyAllowed"])
        self.store.write(lambda c: c.execute("UPDATE message_analyses SET replies_json='[]'"))
        self.reopen()
        self.assertEqual(self.item()["state"], "partial")
        self.assertNotIn("replies", self.item())

    def test_cancel_preserves_success_and_rejects_late_bind_or_finish(self):
        generation = self.seed("running")
        cancelled = threading.Event()
        self.service.active[(ACCOUNT, USER, TARGET)] = SimpleNamespace(account=ACCOUNT, user=USER, ids=[TARGET],
            spec={"generation": generation}, cancel=cancelled.set)
        self.service.cancel(ACCOUNT, USER)
        self.assertTrue(cancelled.is_set())
        before = self.store.rows(USER)
        self.store.bind(USER, [TARGET], generation, ("bad", "bad", "bad", "bad"), {TARGET: "bad"}, "bad")
        self.store.finish(USER, [TARGET], generation, "ready", results={TARGET: self.replies}, part="suggestions")
        self.assertEqual(self.store.rows(USER), before)
        self.assertEqual(self.item()["analysis"], self.analysis)
        self.assertEqual(self.item()["suggestionsState"], "error")

    def test_copy_rejects_a_new_generation_or_disabled_switch_after_validation(self):
        generation = self.seed()
        self.status_hook = lambda count: self.store.set(USER, False) if count == 2 else None
        with self.assertRaises(InsightError) as error:
            self.service.copy(ACCOUNT, USER, TARGET, generation, 0)
        self.assertEqual(error.exception.code, "request-conflict")
        self.store.set(USER, True)
        self.status_reads = 0
        self.status_hook = lambda count: self.store.begin(USER, [TARGET], "new-generation", parts=("analysis",),
                                                        unified=True) if count == 2 else None
        with self.assertRaises(InsightError) as error:
            self.service.copy(ACCOUNT, USER, TARGET, generation, 0)
        self.assertEqual(error.exception.code, "request-conflict")

    def test_copy_validates_version_and_index(self):
        generation = self.seed()
        for version, index in (("old-generation", 0), (generation, True), (generation, "0"), (generation, 3)):
            with self.subTest(version=version, index=index), self.assertRaises(InsightError):
                self.service.copy(ACCOUNT, USER, TARGET, version, index)


if __name__ == "__main__":
    unittest.main()
