"""Queue lifecycle tests use synthetic Core data and gated in-process engines only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from contracts import AdvisorError
from support import build_service, FakeModelStore, FakeSource, wait_until
from test_sessions import SessionRuntime, NativeError

A, U, G = "synthetic-queue-account", "synthetic-queue-friend", "builtin:advisor"


class QueueRuntime(SessionRuntime):
    def __init__(self):
        super().__init__()
        self.gates = {}
        self.fail_messages = set()

    def respond(self, config, request, on_event, cancel_event):
        gate = self.gates.get(request["message"])
        if gate:
            while not gate.wait(.01) and not cancel_event.is_set():
                pass
        if request["message"] in self.fail_messages:
            raise NativeError("provider-error")
        return super().respond(config, request, on_event, cancel_event)

    def release(self):
        for gate in self.gates.values():
            gate.set()


class QueueServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = FakeSource(account=A)
        self.source.seed_pages(U, 4)
        self.models = FakeModelStore(context_tokens=32768)
        self.runtime = QueueRuntime()
        self.service = build_service(self.source, self.models, self.root, runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)
        self.addCleanup(self.runtime.release)
        self.thread = self.service.thread(A, U, G)["thread"]["id"]

    def enqueue(self, message, request=None, **extra):
        return self.service.queue_enqueue(A, U, G, self.thread, message, request or "request-" + message, **extra)

    def queue(self):
        return self.service.queue_get(A, U, G, self.thread)["queue"]

    def done(self, count):
        def complete():
            database = self.service.stores.account(A)
            rows = database.thread_messages(self.thread)
            return rows if sum(row["role"] == "assistant" for row in rows) >= count else None
        result = wait_until(complete)
        self.assertIsNotNone(result)
        self.assertTrue(wait_until(lambda: not any(run.worker.is_alive() for run in self.service._runs.values())))
        return result

    def active(self):
        run = wait_until(lambda: self.service.stores.account(A).active_run(self.thread))
        self.assertIsNotNone(run)
        return run

    def test_fifo_background_dispatch_stores_pending_outside_history_and_reuses_context(self):
        self.runtime.gates["first"] = threading.Event()
        accepted = self.enqueue("first")
        self.active()
        second = self.enqueue("second", thinking=False)
        third = self.enqueue("third")
        rows = self.service.stores.account(A).thread_messages(self.thread)
        self.assertEqual([row["text"] for row in rows if row["role"] == "user"], ["first"])
        self.assertEqual([item["message"] for item in self.queue()["items"]], ["second", "third"])
        self.assertEqual(accepted["item"]["requestId"], "request-first")
        # No renderer polling is needed to dispatch the remainder.
        self.runtime.gates["first"].set()
        self.done(3)
        calls = [request for _, request in self.runtime.respond_calls]
        self.assertEqual([request["message"] for request in calls], ["first", "second", "third"])
        self.assertFalse(calls[1]["thinking"])
        self.assertEqual(calls[1]["runtimeSessionId"], calls[2]["runtimeSessionId"])
        self.assertEqual(self.queue()["items"], [])

    def test_pause_only_blocks_next_and_edit_remove_keep_draft_and_options(self):
        self.runtime.gates["first"] = threading.Event()
        self.enqueue("first")
        active = self.active()
        self.service.queue_pause(A, U, G, self.thread)
        second = self.enqueue("second", thinking=False)["item"]
        third = self.enqueue("third")["item"]
        self.service.save_draft(A, U, G, self.thread, "separate draft", 0)
        edited = self.service.queue_edit(A, U, G, self.thread, second["id"], second["version"], "edited")["item"]
        self.service.queue_remove(A, U, G, self.thread, third["id"], third["version"])
        self.assertFalse(edited["thinking"])
        with self.assertRaises(AdvisorError):
            self.service.queue_edit(A, U, G, self.thread, second["id"], second["version"], "stale")
        self.assertFalse(self.service._runs[active["id"]].cancel.is_set())
        self.runtime.gates["first"].set()
        self.done(1)
        self.assertEqual([item["message"] for item in self.queue()["items"]], ["edited"])
        self.service.queue_resume(A, U, G, self.thread)
        self.done(2)
        self.assertEqual([request["message"] for _, request in self.runtime.respond_calls], ["first", "edited"])
        draft = self.service.stores.account(A).session_draft(U, G, self.thread)
        self.assertEqual(draft["text"], "separate draft")

    def test_duplicate_ack_after_edit_remove_and_completion_never_resends(self):
        self.service.queue_pause(A, U, G, self.thread)
        item = self.enqueue("first")["item"]
        self.service.queue_edit(A, U, G, self.thread, item["id"], item["version"], "edited")
        duplicate = self.enqueue("first")["item"]
        self.assertEqual(duplicate["id"], item["id"])
        self.assertEqual(duplicate["message"], "edited")
        self.service.queue_remove(A, U, G, self.thread, duplicate["id"], duplicate["version"])
        self.assertEqual(self.enqueue("first")["item"]["state"], "removed")
        second = self.enqueue("second")["item"]
        self.service.queue_resume(A, U, G, self.thread)
        self.done(1)
        duplicate = self.enqueue("second")["item"]
        self.assertEqual(duplicate["id"], second["id"])
        self.assertEqual(duplicate["state"], "dispatched")
        self.assertEqual(len(self.runtime.respond_calls), 1)

    def test_lost_ack_after_agent_disabled_or_deleted_still_acknowledges_original_item(self):
        with self.service.condition:
            item = self.enqueue("first")["item"]
            self.service.save_template({"action": "enable", "id": G, "enabled": False})
            duplicate = self.enqueue("first")["item"]
            self.assertEqual(duplicate["id"], item["id"])
            with self.assertRaises(AdvisorError):
                self.enqueue("different-new-question")
            self.service.save_template({"action": "delete", "id": G})
            self.assertEqual(self.enqueue("first")["item"]["id"], item["id"])
        self.assertTrue(wait_until(lambda: self.queue()["paused"]))
        self.assertEqual(self.service.plugin.tasks, [])
        self.assertEqual(len(self.queue()["items"]), 1)
        with self.assertRaises(AdvisorError):
            self.service.queue_enqueue(A, "foreign-friend", G, self.thread, "first", "request-first")
        self.service.delete_thread(A, U, G, self.thread, "delete-accepted-thread")
        with self.assertRaises(AdvisorError):
            self.enqueue("first")

    def test_failed_answer_pauses_and_explicit_retry_preserves_followup_queue(self):
        self.runtime.gates["first"] = threading.Event()
        self.runtime.fail_messages.add("first")
        self.enqueue("first")
        failed = self.active()
        self.enqueue("second")
        self.runtime.gates["first"].set()
        self.assertTrue(wait_until(lambda: self.queue()["paused"]))
        self.assertEqual(self.queue()["items"][0]["message"], "second")
        self.assertTrue(wait_until(lambda: not self.service._runs[failed["id"]].worker.is_alive()))
        self.runtime.fail_messages.clear()
        retry = self.service.start(A, U, G, self.thread, "first", "retry-first", retry_run_id=failed["id"])
        self.done(1)
        self.assertTrue(self.queue()["paused"])
        self.service.queue_resume(A, U, G, self.thread)
        self.done(2)
        self.assertEqual([request["message"] for _, request in self.runtime.respond_calls], ["first", "second"])

    def test_explicit_stop_even_completed_run_pauses_queue(self):
        self.enqueue("first")
        self.done(1)
        run = self.service.stores.account(A).latest_run(self.thread)
        self.service.stop(A, U, run["id"])
        self.enqueue("second")
        self.assertTrue(self.queue()["paused"])
        self.assertEqual([item["message"] for item in self.queue()["items"]], ["second"])
        self.assertEqual(len(self.runtime.respond_calls), 1)

    def test_stop_live_answer_retains_followups_until_resume(self):
        self.runtime.gates["first"] = threading.Event()
        self.enqueue("first")
        run = self.active()
        self.enqueue("second")
        self.service.stop(A, U, run["id"])
        self.assertTrue(wait_until(lambda: not self.service._runs[run["id"]].worker.is_alive()))
        self.assertTrue(self.queue()["paused"])
        self.assertEqual(len(self.queue()["items"]), 1)
        self.service.queue_resume(A, U, G, self.thread)
        self.done(1)
        self.assertEqual(self.queue()["items"], [])

    def test_dispatch_waits_for_terminal_worker_cleanup(self):
        cleanup = threading.Event()
        self.addCleanup(cleanup.set)
        completed = threading.Event()
        original = self.service._run_worker
        def delay_cleanup(run, agent):
            original(run, agent)
            if run.message == "first":
                completed.set()
                cleanup.wait(5)
        with patch.object(self.service, "_run_worker", delay_cleanup):
            self.enqueue("first")
            self.assertTrue(completed.wait(3))
            self.enqueue("second")
            self.assertEqual([request["message"] for _, request in self.runtime.respond_calls], ["first"])
            cleanup.set()
            self.done(2)

    def test_resume_during_stop_cleanup_does_not_ack_a_resume_that_will_be_lost(self):
        self.runtime.gates["first"] = threading.Event()
        cleanup = threading.Event()
        self.addCleanup(cleanup.set)
        original = self.service._run_worker
        def delay_cleanup(run, agent):
            original(run, agent)
            if run.message == "first":
                cleanup.wait(5)
        with patch.object(self.service, "_run_worker", delay_cleanup):
            self.enqueue("first")
            run = self.active()
            self.enqueue("second")
            self.service.stop(A, U, run["id"])
            with self.assertRaises(AdvisorError) as caught:
                self.service.queue_resume(A, U, G, self.thread)
            self.assertEqual(caught.exception.code, "run-active")
            self.assertTrue(self.queue()["paused"])
            cleanup.set()
            self.assertTrue(wait_until(lambda: not self.service._runs[run["id"]].worker.is_alive()))
            self.service.queue_resume(A, U, G, self.thread)
            self.done(1)

    def test_restart_pauses_pending_and_reads_never_resume(self):
        self.service.queue_pause(A, U, G, self.thread)
        item = self.enqueue("first")["item"]
        self.service.shutdown()
        self.service = build_service(self.source, self.models, self.root, runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)
        queue = self.service.thread(A, U, G, self.thread)["thread"]["queue"]
        self.assertTrue(queue["paused"])
        self.assertEqual(queue["reason"], "restart")
        self.assertEqual(queue["items"][0]["id"], item["id"])
        self.service.queue_get(A, U, G, self.thread)
        self.assertEqual(self.service.plugin.tasks, [])
        self.service.queue_resume(A, U, G, self.thread)
        self.done(1)

    def test_independent_contacts_threads_and_new_discussion_do_not_move_queue(self):
        self.service.queue_pause(A, U, G, self.thread)
        self.enqueue("pending")
        new = self.service.new_thread(A, U, G, "new-discussion")["thread"]
        self.assertNotEqual(new["id"], self.thread)
        other = "synthetic-other"
        self.source.seed_pages(other, 2)
        destination = self.service.thread(A, other, G)["thread"]["id"]
        self.service.queue_enqueue(A, other, G, destination, "other question", "other-request")
        self.assertTrue(wait_until(lambda: len(self.runtime.respond_calls) == 1))
        self.assertEqual(self.runtime.respond_calls[0][1]["user"], other)
        self.assertEqual(self.queue()["items"][0]["message"], "pending")
        for user, agent, thread in [(other, G, self.thread), (U, "builtin:empathy", self.thread), (U, G, destination)]:
            with self.assertRaises(AdvisorError):
                self.service.queue_get(A, user, agent, thread)

    def test_dispatch_revalidates_mentions_model_and_fresh_material(self):
        self.source.friend_list = [{"user": "synthetic-other", "name": "Other", "isGroup": False}]
        self.service.queue_pause(A, U, G, self.thread)
        self.enqueue("first", mentions=["synthetic-other"])
        self.source.friend_list.clear()
        self.service.queue_resume(A, U, G, self.thread)
        self.assertTrue(wait_until(lambda: self.queue()["paused"]))
        self.assertEqual(self.queue()["items"][0]["message"], "first")
        self.assertEqual(self.queue()["reason"], "invalid-mentions")
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertEqual(self.service.stores.account(A).session_participants(self.thread), [])

    def test_disabled_agent_and_changed_account_never_dispatch(self):
        self.service.queue_pause(A, U, G, self.thread)
        self.enqueue("first")
        self.service.save_template({"action": "enable", "id": G, "enabled": False})
        with self.assertRaises(AdvisorError):
            self.service.queue_resume(A, U, G, self.thread)
        with self.assertRaises(AdvisorError):
            self.enqueue("new")
        self.source.account = "different-account"
        with self.assertRaises(AdvisorError):
            self.service.queue_resume(A, U, G, self.thread)
        self.assertEqual(self.runtime.respond_calls, [])

    def test_compaction_waits_then_resumes_queue_but_unknown_stays_paused(self):
        self.enqueue("first")
        self.done(1)
        self.runtime.compact_gate = threading.Event()
        self.addCleanup(self.runtime.compact_gate.set)
        self.service.compact_thread(A, U, G, self.thread, "compact-one")
        self.assertTrue(self.runtime.compact_entered.wait(3))
        self.enqueue("second")
        self.assertEqual(len(self.runtime.respond_calls), 1)
        self.runtime.compact_gate.set()
        self.done(2)
        self.runtime.compact_pending = True
        self.service.compact_thread(A, U, G, self.thread, "compact-unknown")
        self.assertTrue(wait_until(lambda: self.queue()["paused"]))
        self.enqueue("third")
        with self.assertRaises(AdvisorError):
            self.service.queue_resume(A, U, G, self.thread)
        self.assertEqual(self.queue()["items"][0]["message"], "third")

    def test_stopping_compaction_pauses_saved_followups(self):
        self.enqueue("first")
        self.done(1)
        self.runtime.compact_gate = threading.Event()
        self.addCleanup(self.runtime.compact_gate.set)
        operation = self.service.compact_thread(A, U, G, self.thread, "compact-stop")["operation"]
        self.assertTrue(self.runtime.compact_entered.wait(3))
        self.enqueue("second")
        self.service.stop_session_operation(A, U, self.thread, operation["id"])
        self.assertTrue(self.queue()["paused"])
        self.runtime.compact_gate.set()
        self.assertTrue(wait_until(lambda: not self.service._session_tasks[operation["id"]].worker.is_alive()))
        self.assertEqual([item["message"] for item in self.queue()["items"]], ["second"])

    def test_compaction_fast_fail_pauses_followups_before_dispatch(self):
        self.enqueue("first")
        self.done(1)
        # Keep the service coordinator during enqueue/compact to exercise the
        # gap before the queue's dispatcher gets to claim the next item.
        with self.service.condition:
            self.enqueue("second")
            with patch.object(self.service.plugin, "start_task", side_effect=RuntimeError("synthetic task unavailable")):
                operation = self.service.compact_thread(A, U, G, self.thread, "compact-launch-fail")["operation"]
            self.assertEqual(operation["state"], "failed")
        self.assertTrue(self.queue()["paused"])
        self.assertEqual([item["message"] for item in self.queue()["items"]], ["second"])
        self.assertEqual(len(self.runtime.respond_calls), 1)

    def test_missing_model_retains_pending_then_resume_reads_fresh_data(self):
        self.models.mode = "broken"
        self.enqueue("first")
        self.assertTrue(wait_until(lambda: self.queue()["paused"]))
        self.assertEqual(len(self.queue()["items"]), 1)
        self.assertEqual(self.queue()["reason"], "model-unavailable")
        self.assertEqual(self.service.stores.account(A).thread_messages(self.thread), [])
        self.models.mode = "api"
        self.source.seed_pages(U, 1, start_seq=5)
        self.source.users[U][-1]["text"] = "SYNTHETIC_FRESH_MATERIAL_AFTER_ENQUEUE"
        self.service.queue_resume(A, U, G, self.thread)
        self.done(1)
        self.assertIn("SYNTHETIC_FRESH_MATERIAL_AFTER_ENQUEUE", self.runtime.respond_calls[0][1]["contextFileText"])

    def test_account_change_during_wait_preserves_pending_as_paused(self):
        self.runtime.gates["first"] = threading.Event()
        self.enqueue("first")
        self.active()
        self.enqueue("second")
        database = self.service.stores.account(A)
        self.source.account = "different-account"
        self.assertTrue(wait_until(lambda: database.queue_get(U, G, self.thread)["paused"]))
        self.assertEqual(database.queue_get(U, G, self.thread)["items"][0]["message"], "second")
        self.runtime.gates["first"].set()
        self.assertTrue(wait_until(lambda: not any(run.worker.is_alive() for run in self.service._runs.values())))
        self.source.account = A
        self.assertTrue(self.queue()["paused"])

    def test_delete_discussion_removes_pending_without_sending(self):
        self.service.queue_pause(A, U, G, self.thread)
        self.enqueue("first")
        deleted = self.service.delete_thread(A, U, G, self.thread, "delete-queued-thread")
        self.assertTrue(deleted["deleted"])
        with self.assertRaises(AdvisorError):
            self.service.queue_resume(A, U, G, self.thread)
        self.assertEqual(self.runtime.respond_calls, [])

    def test_task_launch_failure_preserves_failed_question_without_duplicate_dispatch(self):
        with patch.object(self.service.plugin, "start_task", side_effect=RuntimeError("synthetic failure")):
            self.enqueue("first")
            self.assertTrue(wait_until(lambda: self.queue()["paused"]))
        run = self.service.stores.account(A).latest_run(self.thread)
        self.assertEqual(run["state"], "error")
        self.assertEqual(self.queue()["items"], [])
        self.enqueue("first")
        self.service.queue_resume(A, U, G, self.thread)
        self.assertEqual(self.runtime.respond_calls, [])

    def test_direct_run_cannot_overtake_queue_and_started_item_cannot_be_edited(self):
        self.runtime.gates["first"] = threading.Event()
        item = self.enqueue("first")["item"]
        self.active()
        with self.assertRaises(AdvisorError):
            self.service.queue_edit(A, U, G, self.thread, item["id"], item["version"], "too late")
        self.enqueue("second")
        with self.assertRaises(AdvisorError):
            self.service.start(A, U, G, self.thread, "overtake", "not-queued")
        self.runtime.gates["first"].set()
        self.done(2)


if __name__ == "__main__":
    unittest.main()
