"""Synthetic regression coverage for attributed, restartable discussion recovery.

Copyright 2026 Know Your Friends contributors. Apache-2.0.
Modified 2026-10-16: cover language-independent advisory budgets and non-shrinking summaries.
"""
import copy
import threading
import unittest
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import patch

from contracts import AdvisorError
from recovery import ATTRIBUTION_RULE, attributed_chunks, byte_cost, recover_discussion, verify_run_history


VERSION = {"context_epoch": "synthetic-epoch", "profile_stamp": "synthetic-profile"}


class RecoveryDatabase:
    def __init__(self):
        self.messages = [
            {"id": "m1", "seq": 1, "role": "user", "text": "SYNTHETIC-USER-QUESTION " * 260, "run_id": "r1", "status": None},
            {"id": "m2", "seq": 2, "role": "assistant", "text": "合成建议尚未完成不能当事实。" * 250, "run_id": "r1", "status": "stopped"},
            {"id": "current-question", "seq": 3, "role": "user", "text": "继续讨论", "run_id": "current-run", "status": None},
        ]
        self.runs = [{"id": "r1", "state": "stopped", "user_message_id": "m1"},
                     {"id": "current-run", "state": "running", "user_message_id": "current-question"}]
        self.operations = []
        self.saved = None

    def thread_messages(self, _thread):
        return copy.deepcopy(self.messages)

    def _rows(self, _query, _params):
        return copy.deepcopy(self.runs)

    def session_run_metadata(self, _run):
        return dict(VERSION)

    def session_recovery_latest(self, _thread):
        return copy.deepcopy(self.saved)

    def session_operation_begin(self, _user, _agent, _thread, kind, request, metadata=None, run_id=None):
        previous = next((operation for operation in self.operations if operation["request_id"] == request), None)
        if previous:
            return previous
        operation = {"id": "op-" + str(len(self.operations)), "kind": kind, "request_id": request,
                     "metadata": metadata, "state": "running", "run_id": run_id}
        self.operations.append(operation)
        return operation

    def session_operations(self, _thread):
        return list(self.operations)

    def session_operation_update(self, identifier, **fields):
        operation = next(operation for operation in self.operations if operation["id"] == identifier)
        operation.update(copy.deepcopy(fields))
        return operation

    def session_recovery_save(self, _thread, summary, through_seq, metadata, operation_id):
        boundary = next(message for message in self.messages if message["seq"] == through_seq)
        self.saved = {"summary": summary, "through_seq": through_seq,
                      "metadata": {**metadata, "boundary_message_id": boundary["id"]}, "operation_id": operation_id}


class RecoveryRuntime:
    def __init__(self):
        self.calls = []
        self.fail_at = None

    def summarize_discussion(self, _service, _token, request, _on_event, _cancel):
        self.calls.append(copy.deepcopy(request))
        if len(self.calls) == self.fail_at:
            raise RuntimeError("synthetic-summary-failure")
        return {"text": "用户问题仍待讨论；助手建议未完成，未确认为朋友事实。"}


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.database = RecoveryDatabase()
        self.runtime = RecoveryRuntime()
        self.service = SimpleNamespace(
            _generation_service=lambda config: config["service"],
            _check_run=lambda _run, **_kwargs: None,
            _cache_commit_guard=lambda _run: nullcontext(),
            _recovery_failed=lambda _run, op: self.database.session_operation_update(op, state="failed"))
        self.run = SimpleNamespace(database=self.database, thread_id="synthetic-thread", message_id="current-question",
                                   id="current-run", account="synthetic-account", user="synthetic-user", agent_id="synthetic-agent",
                                   source_scope="synthetic-source", cancel=threading.Event(), **VERSION)
        self.config = {"inputBudgetTokens": 4096, "service": {"id": "synthetic"}, "token": "synthetic-token"}

    def recover(self, target=512):
        return recover_discussion(self.service, self.run, self.runtime, self.config, target)

    def one_source(self, text):
        self.database.messages = [self.database.messages[0], self.database.messages[-1]]
        self.database.messages[0]["text"] = text
        self.database.messages[-1]["seq"] = 2
        self.config["inputBudgetTokens"] = 20000

    def test_each_unicode_continuation_repeats_exact_role_status_and_sequence_without_loss(self):
        original = "🙂中文合成讨论" * 300
        block = {"text": original, "from_seq": 7, "through_seq": 7, "roles": ["assistant"],
                 "status": "stopped", "kind": "original", "incomplete": True}
        packets = list(attributed_chunks([block], 768))
        self.assertGreater(len(packets), 2)
        restored = []
        for index, packet in enumerate(packets):
            self.assertLessEqual(byte_cost(packet["text"]), 768)
            self.assertTrue(packet["text"].startswith(ATTRIBUTION_RULE))
            header, body = packet["text"][len(ATTRIBUTION_RULE):].split("\n", 1)
            self.assertIn("seq=7..7 role=assistant status=stopped", header)
            self.assertIn("part=" + str(index + 1), header)
            self.assertTrue(packet["incomplete"])
            restored.append(body[:-1])
        self.assertEqual("".join(restored), original)

    def test_bounded_multilevel_recovery_keeps_source_attribution_and_incomplete_status(self):
        self.run.thinking = True
        summary, coverage = self.recover()
        self.assertLessEqual(byte_cost(summary), 512)
        self.assertEqual(coverage["throughSeq"], 2)
        originals = [call for call in self.runtime.calls if "kind=original" in call["text"]]
        merges = [call for call in self.runtime.calls if "kind=summary" in call["text"]]
        self.assertGreater(len(originals), 2)
        self.assertTrue(merges)
        for request in self.runtime.calls:
            self.assertIs(request["thinking"], False)
            self.assertLessEqual(byte_cost(request["text"]), 2048)
            self.assertTrue(request["text"].startswith(ATTRIBUTION_RULE))
        self.assertTrue(any("seq=1..1 role=user status=complete" in call["text"] and "part=2" in call["text"] for call in originals))
        self.assertTrue(any("seq=2..2 role=assistant status=stopped" in call["text"] and "part=2" in call["text"] for call in originals))
        self.assertTrue(any("status=incomplete" in call["text"] for call in merges))
        self.assertIn("含未完成记录", summary)

    def test_retry_reuses_successful_attributed_chunk_checkpoint(self):
        self.runtime.fail_at = 2
        with self.assertRaisesRegex(RuntimeError, "synthetic-summary-failure"):
            self.recover()
        first_text, second_text = [call["text"] for call in self.runtime.calls]
        self.runtime.fail_at = None
        self.run.id = "retry-run"
        self.recover()
        self.assertEqual(sum(call["text"] == first_text for call in self.runtime.calls), 1)
        self.assertEqual(sum(call["text"] == second_text for call in self.runtime.calls), 2)

    def test_english_summary_above_advisory_and_old_char_limit_is_accepted_when_block_fits(self):
        self.one_source("合成资料" * 750)
        answer = "Historical user question remains open. " * 60
        original = copy.deepcopy(self.database.messages)
        with patch.object(self.runtime, "summarize_discussion", return_value={"text": answer}) as generate:
            summary, coverage = self.recover(4000)
        request = generate.call_args.args[2]
        self.assertNotIn("maxSummaryChars", request)
        self.assertGreater(len(answer), 1200)
        self.assertGreater(len(answer), request["targetSummaryChars"])
        self.assertIn(answer, summary)
        self.assertLessEqual(byte_cost(summary), 4000)
        self.assertEqual(coverage["throughSeq"], 1)
        self.assertEqual(self.database.messages, original)
        self.assertEqual(self.database.saved["summary"], summary)

    def test_advisory_uses_available_window_without_old_1200_char_ceiling(self):
        self.one_source("SYNTHETIC " * 1100)
        answer = "Preserved question. " * 300
        with patch.object(self.runtime, "summarize_discussion", return_value={"text": answer}) as generate:
            summary, _ = self.recover(8000)
        self.assertGreater(generate.call_args.args[2]["targetSummaryChars"], 1200)
        self.assertIn(answer, summary)
        self.assertLessEqual(byte_cost(summary), 8000)

    def test_useful_oversized_summary_is_reduced_hierarchically_without_truncation(self):
        self.one_source("合成资料" * 750)
        first = "Synthetic prior question. " * 120
        second = "Historical question remains unresolved; no friend facts inferred. " * 5
        with patch.object(self.runtime, "summarize_discussion",
                          side_effect=[{"text": first}, {"text": second}]) as generate:
            summary, _ = self.recover(1000)
        self.assertEqual(generate.call_count, 2)
        self.assertGreater(byte_cost(first), 1000)
        self.assertIn(first, generate.call_args_list[1].args[2]["text"])
        self.assertIn("kind=summary", generate.call_args_list[1].args[2]["text"])
        self.assertIn(second, summary)
        self.assertLessEqual(byte_cost(summary), 1000)

    def test_growing_summaries_are_not_cached_or_reused_as_source_and_retry_can_recover(self):
        self.one_source("合成资料" * 750)
        original = copy.deepcopy(self.database.messages)

        def expand(_service, _token, request, _events, _cancel):
            self.runtime.calls.append(copy.deepcopy(request))
            return {"text": request["text"] + "UNWANTED-EXPANSION"}

        with patch.object(self.runtime, "summarize_discussion", side_effect=expand):
            with self.assertRaisesRegex(AdvisorError, "连续整理未缩短"):
                self.recover()
        self.assertEqual(len(self.runtime.calls), 2)
        self.assertTrue(all("UNWANTED-EXPANSION" not in call["text"] for call in self.runtime.calls))
        self.assertFalse(self.database.operations[-1].get("checkpoint", {}).get("chunks"))
        self.assertIsNone(self.database.saved)
        self.assertEqual(self.database.messages, original)
        self.run.id = "retry-run"
        summary, _ = self.recover()
        self.assertLessEqual(byte_cost(summary), 512)

    def test_smallest_supported_window_still_merges_attributed_segments_within_budget(self):
        self.config["inputBudgetTokens"] = 4096 - 1024 - 256
        summary, coverage = self.recover()
        self.assertLessEqual(byte_cost(summary), 512)
        self.assertEqual(coverage["throughSeq"], 2)
        self.assertLessEqual(len(self.runtime.calls), 256)
        self.assertTrue(all(byte_cost(call["text"]) <= 768 for call in self.runtime.calls))

    def test_changed_chunk_strategy_does_not_reuse_incompatible_checkpoint(self):
        self.runtime.fail_at = 2
        with self.assertRaises(RuntimeError):
            self.recover()
        first_text = self.runtime.calls[0]["text"]
        old_fingerprint = self.database.operations[0]["checkpoint"]["fingerprint"]
        self.runtime.fail_at = None
        self.run.id = "retry-run"
        with patch("recovery.CHUNK_STRATEGY", "different-strategy-synthetic"):
            self.recover()
        self.assertEqual(sum(call["text"] == first_text for call in self.runtime.calls), 2)
        self.assertNotEqual(self.database.operations[1]["checkpoint"]["fingerprint"], old_fingerprint)

    def test_verified_prior_summary_is_not_replayed_with_covered_original_text(self):
        self.database.saved = {"summary": "SAVED-SYNTHETIC-SUMMARY", "through_seq": 1,
                               "metadata": {**VERSION, "boundary_message_id": "m1"}}
        self.recover()
        material = "\n".join(call["text"] for call in self.runtime.calls)
        self.assertIn("SAVED-SYNTHETIC-SUMMARY", material)
        self.assertIn("kind=prior-summary", material)
        self.assertNotIn("SYNTHETIC-USER-QUESTION", material)

    def test_missing_final_old_answer_is_detected_even_when_new_question_reuses_sequence(self):
        self.database.messages.pop(1)
        self.database.messages[-1]["seq"] = 2
        self.database.runs[0]["state"] = "done"
        with self.assertRaisesRegex(AdvisorError, "讨论记录缺失"):
            self.recover()
        self.assertEqual(self.runtime.calls, [])

    def test_legacy_relation_requires_one_matching_question_interval(self):
        self.database.messages[1].update(run_id=None, status=None)
        self.database.runs[0]["state"] = "done"
        verify_run_history(self.database, self.run.thread_id, self.database.messages, self.database.messages[-1])
        self.database.runs[0]["user_message_id"] = None
        with self.assertRaisesRegex(AdvisorError, "关联无法核验"):
            verify_run_history(self.database, self.run.thread_id, self.database.messages, self.database.messages[-1])


if __name__ == "__main__":
    unittest.main()
