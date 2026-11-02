"""Synthetic integration tests for persistent discussions and native operations."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from contracts import AdvisorError
from service import MODEL_UNAVAILABLE_TEXT
from support import build_service
from support import FakeModelStore, FakeRuntime, FakeSource, wait_until, continued_task

A, U, G = "synthetic-session-account", "synthetic-friend", "builtin:advisor"


class NativeError(Exception):
    def __init__(self, code):
        self.code = code


class SessionRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.natives = {}
        self.native_counter = 0
        self.operations = {}
        self.deleted = []
        self.delete_fail = False
        self.summary_fail = False
        self.summary_fail_on_call = None
        self.fail_response = False
        self.compact_entered = threading.Event()
        self.compact_gate = None
        self.respond_gate = None
        self.respond_entered = threading.Event()
        self.compact_unknown = False
        self.compact_pending = False
        self.inspect_fails = False
        self.inspect_calls = []
        self.stop_operation_calls = []
        self.native_aborts = []
        self.summaries = []
        self.compactions = []

    def respond(self, config, request, on_event, cancel_event):
        self.respond_calls.append((config, dict(request)))
        native = request.get("runtimeSessionId")
        if native and native not in self.natives:
            raise NativeError("native-session-missing")
        if not native:
            self.native_counter += 1
            native = "synthetic-native-" + str(self.native_counter)
            self.natives[native] = request["threadId"]
            on_event({"type": "status", "state": "native-session-created", "runtimeSessionId": native})
        self.respond_entered.set()
        if self.respond_gate and request["message"] == "hold":
            while not self.respond_gate.wait(.01) and not cancel_event.is_set():
                pass
        if self.fail_response:
            raise NativeError("provider-error")
        return {"text": "SYNTHETIC ANSWER " + request["message"], "runtimeSessionId": native,
                "compactions": [dict(event) for event in self.compactions],
                "usage": {"inputTokens": 20, "totalInputTokens": 31, "cacheReadTokens": 7,
                          "cacheWriteTokens": 4, "estimated": False}}

    def inspect_session(self, config, request, on_event, cancel_event):
        self.inspect_calls.append(dict(request))
        if self.inspect_fails:
            raise NativeError("engine-unavailable")
        native = request.get("runtimeSessionId")
        if native not in self.natives:
            return {"state": "missing"}
        if request.get("operationId") in self.operations:
            return self.operations[request["operationId"]]
        return {"state": "idle", "effectiveHistoryTokensEstimate": 100, "compactions": []}

    def compact_session(self, config, request, on_event, cancel_event):
        self.compact_calls.append(dict(request))
        self.compact_entered.set()
        if self.compact_gate:
            self.compact_gate.wait(3)
        if self.compact_pending:
            self.operations[request["operationId"]] = {"state": "running", "runtimeSessionId": request["runtimeSessionId"]}
            return {"state": "unknown", "runtimeSessionId": request["runtimeSessionId"]}
        result = {"state": "completed", "summary": "SYNTHETIC DISCUSSION SUMMARY", "runtimeSessionId": request["runtimeSessionId"]}
        self.operations[request["operationId"]] = result
        if self.compact_unknown:
            raise NativeError("timeout")
        return result

    def stop_session_operation(self, config, request, on_event, cancel_event):
        self.stop_operation_calls.append(dict(request))
        operation = self.operations.get(request["operationId"])
        if operation is None:
            return {"state": "missing"}
        if operation["state"] == "running":
            self.native_aborts.append(request["runtimeSessionId"])
            operation = {"state": "stopped", "runtimeSessionId": request["runtimeSessionId"]}
            self.operations[request["operationId"]] = operation
        return dict(operation)

    def delete_session(self, config, request, on_event, cancel_event):
        if self.delete_fail:
            raise NativeError("engine-unavailable")
        native = request["runtimeSessionId"]
        self.deleted.append(native)
        self.natives.pop(native, None)
        return {"state": "completed"}

    def summarize_discussion(self, config, request, on_event, cancel_event):
        self.summaries.append(dict(request))
        if self.summary_fail or len(self.summaries) == self.summary_fail_on_call:
            raise NativeError("provider-error")
        return {"text": "保留讨论目标、已确认决定和待核实项；仅为合成摘要。"}


class SessionServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = FakeSource(account=A)
        self.source.seed_pages(U, 4)
        self.model = FakeModelStore(context_tokens=32768)
        self.runtime = SessionRuntime()
        self.service = build_service(self.source, self.model, self.root, runtime_factory=lambda: self.runtime)
        self.addCleanup(lambda: self.service.shutdown())

    def selected(self):
        return self.service.thread(A, U, G)["thread"]

    def answer(self, text="question", thread=None, key=None, **kwargs):
        thread = thread or self.selected()["id"]
        result = self.service.start(A, U, G, thread, text, key or "request-" + text.replace(" ", "-"), **kwargs)
        terminal = wait_until(lambda: (row if row["run"]["state"] != "running" else None)
                              if (row := self.service.events(A, U, result["run"]["id"])) else None)
        self.assertIsNotNone(terminal)
        # The API publishes its terminal result before the worker's final cleanup.
        # These helpers promise a settled run before tests begin another operation.
        worker = self.service._runs[result["run"]["id"]].worker
        worker.join(2)
        self.assertFalse(worker.is_alive())
        return terminal

    def compact_done(self, thread, key="compact-one"):
        op = self.service.compact_thread(A, U, G, thread, key)["operation"]
        terminal = wait_until(lambda: (row if row["operation"]["state"] != "running" else None)
                              if (row := self.service.session_operation(A, U, thread, op["id"])) else None)
        self.assertIsNotNone(terminal)
        task = self.service._session_tasks.get(op["id"])
        if task is not None and task.worker is not None:
            task.worker.join(2)
            self.assertFalse(task.worker.is_alive())
        return terminal

    def test_new_does_not_cancel_same_contact_background_or_steal_selection(self):
        first = self.selected()["id"]
        self.runtime.respond_gate = threading.Event()
        self.addCleanup(self.runtime.respond_gate.set)
        started = self.service.start(A, U, G, first, "hold", "hold-one")
        self.assertTrue(self.runtime.respond_entered.wait(2))
        second = self.service.new_thread(A, U, G, "new-second")["thread"]["id"]
        self.assertEqual(self.answer("second", second)["run"]["state"], "done")
        self.assertEqual(self.service.events(A, U, started["run"]["id"])["run"]["state"], "running")
        self.runtime.respond_gate.set()
        self.assertTrue(wait_until(lambda: self.service._runs[started["run"]["id"]].state == "done"))
        self.assertEqual(self.selected()["id"], second)
        self.service.select_thread(A, U, G, first)
        self.assertEqual(self.selected()["id"], first)

    def test_draft_cas_and_legacy_migration_are_per_thread(self):
        first = self.selected()["id"]
        saved = self.service.save_draft(A, U, G, first, "draft A", 0, 31)["draft"]
        second = self.service.new_thread(A, U, G, "draft-new")["thread"]["id"]
        self.service.select_thread(A, U, G, first)
        self.service.save_draft(A, U, G, None, "legacy B", 0, migrate=True)
        self.assertEqual(self.selected()["draft"]["text"], "draft A")
        self.assertEqual(self.service.thread(A, U, G, second)["thread"]["draft"]["text"], "legacy B")
        with self.assertRaises(AdvisorError):
            self.service.save_draft(A, U, G, first, "stale overwrite", 0)
        self.assertEqual(saved["scrollTop"], 31)

    def test_inactive_deleted_history_readonly_and_no_model_work(self):
        thread = self.answer()["thread"]["id"]
        before = len(self.runtime.respond_calls)
        self.service.save_template({"action": "delete", "id": G})
        listed = self.service.threads(A, U)
        self.assertEqual(listed["threads"][0]["id"], thread)
        self.assertTrue(listed["threads"][0]["readonly"])
        projection = self.service.select_thread(A, U, G, thread)
        self.assertTrue(projection["thread"]["readonly"])
        self.service.rename_thread(A, U, G, thread, "old discussion", "rename-old")
        with self.assertRaises(AdvisorError):
            self.service.compact_thread(A, U, G, thread, "cannot-compact")
        self.assertEqual(len(self.runtime.respond_calls), before)

    def test_manual_compaction_keeps_transcript_idempotent_and_counts_cache_once(self):
        answered = self.answer()
        thread = answered["thread"]["id"]
        self.assertEqual(answered["thread"]["usage"]["inputTokens"], 31)
        self.assertEqual(answered["thread"]["contextTokens"], 32768)
        before = self.selected()["messages"]
        first = self.compact_done(thread)
        self.assertEqual(first["operation"]["state"], "completed")
        second = self.service.compact_thread(A, U, G, thread, "compact-one")
        self.assertEqual(second["operation"]["id"], first["operation"]["id"])
        self.assertEqual(len(self.runtime.compact_calls), 1)
        self.assertEqual(len(self.runtime.respond_calls), 1)
        self.assertEqual(self.selected()["messages"], before)
        self.assertEqual(self.selected()["compactions"][0]["summary"], "SYNTHETIC DISCUSSION SUMMARY")

    def test_compaction_without_a_model_fails_plainly_and_a_later_request_still_runs(self):
        thread = self.answer()["thread"]["id"]
        self.model.mode = "local"
        failed = self.compact_done(thread, "compact-no-model")
        self.assertEqual(failed["operation"]["state"], "failed")
        self.assertEqual(failed["operation"]["error"], MODEL_UNAVAILABLE_TEXT)
        self.assertEqual(self.runtime.compact_calls, [])
        self.assertEqual(self.runtime.inspect_calls, [])
        self.model.mode = "api"
        self.assertEqual(self.compact_done(thread, "compact-model-back")["operation"]["state"], "completed")
        self.assertEqual(len(self.runtime.compact_calls), 1)

    def test_metadata_model_window_matches_runtime_and_ui_and_invalidates_prior_usage(self):
        self.model.context_tokens = 24576
        first = self.answer("metadata window")
        self.assertEqual(first["run"]["state"], "done", first)
        self.assertEqual(self.runtime.respond_calls[-1][0]["contextTokens"], 24576)
        self.assertEqual(first["thread"]["contextTokens"], 24576)
        self.assertEqual(first["thread"]["usage"]["contextTokens"], 24576)
        self.model.context_tokens = 12288
        changed = self.selected()
        self.assertEqual(changed["contextTokens"], 12288)
        self.assertIsNone(changed["usage"])
        second = self.answer("new metadata window", first["thread"]["id"])
        self.assertEqual(second["run"]["state"], "done", second)
        self.assertEqual(self.runtime.respond_calls[-1][0]["contextTokens"], 12288)
        self.assertEqual(second["thread"]["contextTokens"], 12288)
        self.assertEqual(second["thread"]["usage"]["contextTokens"], 12288)

    def test_compacted_since_usage_clears_on_new_usage_and_model_changes(self):
        # Synthetic timestamps establish ordering without sleeps or relying on
        # two extremely fast worker completions having different milliseconds.
        with patch("sessions.time", SimpleNamespace(time=lambda: 1)):
            first = self.answer("meter before compact")
        thread = first["thread"]["id"]
        self.assertFalse(first["thread"]["compactedSinceUsage"])
        compacted = self.compact_done(thread, "meter-compact-one")
        self.assertTrue(compacted["thread"]["compactedSinceUsage"])
        self.assertIsNone(compacted["thread"]["usage"])
        finished_ms = compacted["operation"]["completedAtMs"]
        with patch("sessions.time", SimpleNamespace(time=lambda: (finished_ms + 1) / 1000)):
            measured = self.answer("meter after compact", thread)
        self.assertFalse(measured["thread"]["compactedSinceUsage"])
        self.assertIsNotNone(measured["thread"]["usage"])
        # Invalidate an existing compaction flag by changing only model config.
        with patch("sessions.time", SimpleNamespace(time=lambda: 1)):
            self.answer("meter before another compact", thread)
        self.assertTrue(self.compact_done(thread, "meter-compact-two")["thread"]["compactedSinceUsage"])
        self.model.context_tokens = 16000
        changed = self.selected()
        self.assertFalse(changed["compactedSinceUsage"])
        self.assertIsNone(changed["usage"])

    def test_source_change_hides_usage_and_compacted_flag_before_context_refresh(self):
        measured = self.answer("source meter without compaction")
        measured_thread = measured["thread"]["id"]
        self.assertIsNotNone(measured["thread"]["usage"])
        compacted_thread = self.service.new_thread(A, U, G, "new-source-meter")["thread"]["id"]
        with patch("sessions.time", SimpleNamespace(time=lambda: 1)):
            self.answer("source meter with compaction", compacted_thread)
        compacted = self.compact_done(compacted_thread, "source-meter-compact")
        self.assertTrue(compacted["thread"]["compactedSinceUsage"])
        database = self.service.stores.account(A)
        original_context = database.context(U)
        self.source.workdir = "C:/synthetic-new-source-without-projection-refresh"
        self.assertNotEqual(self.source.fingerprint(), original_context["source_id"])
        self.service._status_cache.clear()  # The display reuses a source check for at most a couple of seconds.
        for thread in (measured_thread, compacted_thread):
            current = self.service.thread(A, U, G, thread)["thread"]
            self.assertIsNone(current["usage"])
            self.assertFalse(current["compactedSinceUsage"])
        self.assertEqual(database.context(U)["epoch"], original_context["epoch"])
        self.assertEqual(database.context(U)["source_id"], original_context["source_id"])

    def test_repeated_native_auto_compaction_preserves_single_operation_and_event_dates(self):
        event = {"id": "synthetic-auto-compaction", "kind": "auto", "summary": "合成的早期讨论摘要",
                 "startedAt": 1700000000100, "finishedAt": 1700000000250}
        self.runtime.compactions = [event, event]
        first = self.answer("auto compact first")
        self.assertEqual(first["run"]["state"], "done", first)
        thread = first["thread"]["id"]
        database = self.service.stores.account(A)
        initial = [row for row in database.session_operations(thread) if row["kind"] == "compact"]
        self.assertEqual(len(initial), 1)
        original = initial[0]
        self.assertTrue(original["metadata"]["automatic"])
        for response in (first, self.answer("auto compact second", thread), self.answer("auto compact third", thread)):
            self.assertEqual(response["run"]["state"], "done", response)
            events = response["thread"]["compactions"]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["id"], original["id"])
            self.assertEqual(events[0]["createdAtMs"], event["startedAt"])
            self.assertEqual(events[0]["completedAtMs"], event["finishedAt"])
        final = [row for row in database.session_operations(thread) if row["kind"] == "compact"]
        self.assertEqual(len(final), 1)
        for key in ("id", "version", "created_at_ms", "updated_at_ms", "result"):
            self.assertEqual(final[0][key], original[key])
        self.assertEqual(self.runtime.compact_calls, [])

    def test_unknown_compaction_is_busy_only_for_its_contact(self):
        thread = self.selected()["id"]
        database = self.service.stores.account(A)
        self.assertFalse(database.session_has_active_operations(U))
        operation = database.session_operation_begin(U, G, thread, "compact", "unknown-contact-scope")
        database.session_operation_update(operation["id"], state="unknown")
        self.assertTrue(database.session_has_active_operations(U))
        self.assertTrue(database.session_has_active_operations())
        self.assertFalse(database.session_has_active_operations("synthetic-other-friend"))
        database.session_operation_update(operation["id"], state="completed")
        self.assertFalse(database.session_has_active_operations(U))
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertEqual(self.runtime.compact_calls, [])

    def test_deleted_memory_or_changed_goal_cannot_compact_retired_native_profile(self):
        database = self.service.stores.account(A)
        for change in ("delete-memory", "change-goal"):
            with self.subTest(change=change):
                self.source.snapshot["stamp"] = "synthetic-before-" + change
                first = self.answer("profile basis " + change)
                thread = first["thread"]["id"]
                old_native = first["thread"]["runtimeSessionId"]
                old_profile = database.thread(thread)["profile_stamp"]
                self.source.snapshot["stamp"] = "synthetic-after-" + change
                self.assertNotEqual(self.service.friend_stamp(A, U), old_profile)
                calls_before = len(self.runtime.compact_calls)
                operation = self.service.compact_thread(A, U, G, thread, "stale-profile-" + change)["operation"]
                self.assertEqual(operation["state"], "failed")
                self.assertIn("资料版本已变化", operation["error"])
                self.assertEqual(len(self.runtime.compact_calls), calls_before)
                self.assertEqual(database.thread(thread)["runtime_session_id"], old_native)
                self.assertFalse(self.selected()["compactedSinceUsage"])

    def test_same_source_epoch_reset_cannot_compact_old_native_history(self):
        first = self.answer("epoch basis")
        thread = first["thread"]["id"]
        database = self.service.stores.account(A)
        old_context = database.context(U)
        self.source.statuses[U]["epoch"] = "synthetic-new-epoch"
        database.sync(U)
        reset = database.context(U)
        self.assertEqual(reset["source_id"], old_context["source_id"])
        self.assertNotEqual(reset["epoch"], old_context["epoch"])
        operation = self.service.compact_thread(A, U, G, thread, "stale-epoch")["operation"]
        self.assertEqual(operation["state"], "failed")
        self.assertIn("资料版本已变化", operation["error"])
        self.assertEqual(self.runtime.compact_calls, [])
        self.assertEqual(database.thread(thread)["runtime_session_id"], first["thread"]["runtimeSessionId"])
        self.assertFalse(self.selected()["compactedSinceUsage"])

    def test_changed_source_before_context_refresh_cannot_compact_previous_source(self):
        first = self.answer("source basis")
        thread = first["thread"]["id"]
        database = self.service.stores.account(A)
        old_context = database.context(U)
        self.source.workdir = "C:/synthetic-replaced-wechat-source"
        self.assertNotEqual(self.source.fingerprint(), old_context["source_id"])
        self.assertEqual(database.context(U)["epoch"], old_context["epoch"])
        operation = self.service.compact_thread(A, U, G, thread, "stale-source")["operation"]
        self.assertEqual(operation["state"], "failed")
        self.assertEqual(self.runtime.compact_calls, [])
        self.assertEqual(database.thread(thread)["runtime_session_id"], first["thread"]["runtimeSessionId"])

    def test_profile_or_epoch_change_during_compaction_rejects_late_native_success(self):
        database = self.service.stores.account(A)
        for change in ("profile", "epoch"):
            with self.subTest(change=change):
                first = self.answer("running compact " + change)
                thread = first["thread"]["id"]
                self.runtime.compact_entered.clear()
                gate = threading.Event()
                self.runtime.compact_gate = gate
                self.addCleanup(gate.set)
                operation = self.service.compact_thread(A, U, G, thread, "change-during-" + change)["operation"]
                self.assertTrue(self.runtime.compact_entered.wait(2))
                if change == "profile":
                        self.source.snapshot["stamp"] = "synthetic-changed-during-compact"
                else:
                    context = database.context(U)
                    self.source.statuses[U]["epoch"] = "synthetic-new-during-compact"
                    database.sync(U)
                gate.set()
                task = self.service._session_tasks[operation["id"]]
                self.assertTrue(wait_until(lambda: not task.worker.is_alive()))
                self.assertEqual(self.runtime.operations[operation["id"]]["state"], "completed")
                reconciled = self.service.session_operation(A, U, thread, operation["id"])
                self.assertEqual(reconciled["operation"]["state"], "interrupted")
                self.assertIsNone(reconciled["operation"]["summary"])
                self.assertFalse(reconciled["thread"]["compactedSinceUsage"])
                self.assertIsNone(reconciled["thread"]["activeOperation"])

    def test_compact_stop_revokes_task_and_rejects_late_native_success(self):
        thread = self.answer()["thread"]["id"]
        self.runtime.compact_gate = threading.Event()
        self.addCleanup(self.runtime.compact_gate.set)
        op = self.service.compact_thread(A, U, G, thread, "held-compact")["operation"]
        self.assertTrue(self.runtime.compact_entered.wait(2))
        for action in (lambda: self.service.start(A, U, G, thread, "blocked", "blocked"),
                       lambda: self.service.delete_thread(A, U, G, thread, "blocked-delete")):
            with self.assertRaises(AdvisorError):
                action()
        self.service.stop_session_operation(A, U, thread, op["id"])
        self.runtime.compact_gate.set()
        done = wait_until(lambda: self.service.session_operation(A, U, thread, op["id"])["operation"]["state"] == "interrupted")
        self.assertTrue(done)
        self.assertTrue(self.service._session_tasks[op["id"]].task.cancelled.is_set())
        self.assertIsNone(self.service.session_operation(A, U, thread, op["id"])["operation"]["summary"])

    def test_unknown_compaction_reconciles_without_reposting(self):
        thread = self.answer()["thread"]["id"]
        self.runtime.compact_unknown = True
        self.runtime.inspect_fails = True
        result = self.compact_done(thread)
        self.assertEqual(result["operation"]["state"], "unknown")
        self.runtime.inspect_fails = False
        result = self.service.session_operation(A, U, thread, result["operation"]["id"])
        self.assertEqual(result["operation"]["state"], "completed")
        self.assertEqual(len(self.runtime.compact_calls), 1)

    def test_unknown_compaction_survives_service_restart_and_queries_original_operation(self):
        thread = self.answer()["thread"]["id"]
        self.runtime.compact_unknown = True
        self.runtime.inspect_fails = True
        pending = self.compact_done(thread)["operation"]
        self.assertEqual(pending["state"], "unknown")
        old_runtime = self.runtime
        self.service.shutdown()
        # A fresh bridge/runtime instance sees native state committed before the
        # old connection failed, while the account database still says unknown.
        self.runtime = SessionRuntime()
        self.runtime.natives = dict(old_runtime.natives)
        self.runtime.operations = {key: dict(value) for key, value in old_runtime.operations.items()}
        self.runtime.native_counter = old_runtime.native_counter
        self.service = build_service(self.source, self.model, self.root,
                                      runtime_factory=lambda: self.runtime)
        restored = self.service.thread(A, U, G, thread)["thread"]
        self.assertEqual(restored["activeOperation"]["id"], pending["id"])
        self.assertEqual(restored["activeOperation"]["state"], "unknown")
        self.assertEqual(self.runtime.inspect_calls, [])
        reconciled = self.service.session_operation(A, U, thread, pending["id"])
        self.assertEqual(reconciled["operation"]["state"], "completed")
        self.assertEqual(reconciled["operation"]["id"], pending["id"])
        self.assertEqual(reconciled["operation"]["summary"], "SYNTHETIC DISCUSSION SUMMARY")
        self.assertEqual(self.runtime.inspect_calls[0]["operationId"], pending["id"])
        self.assertEqual(len(old_runtime.compact_calls), 1)
        self.assertEqual(self.runtime.compact_calls, [])
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertIsNone(reconciled["thread"]["activeOperation"])

    def test_unknown_compaction_stop_aborts_native_after_local_worker_finished_once(self):
        thread = self.answer("stop pending native")["thread"]["id"]
        self.runtime.compact_pending = True
        pending = self.compact_done(thread, "pending-native-stop")["operation"]
        task = self.service._session_tasks[pending["id"]]
        self.assertTrue(wait_until(lambda: not task.worker.is_alive()))
        self.assertEqual(pending["state"], "unknown")
        self.assertEqual(self.runtime.operations[pending["id"]]["state"], "running")
        stopped = self.service.stop_session_operation(A, U, thread, pending["id"])
        self.assertEqual(stopped["operation"]["state"], "stopped")
        self.assertEqual(len(self.runtime.stop_operation_calls), 1)
        self.assertEqual(self.runtime.stop_operation_calls[0]["operationId"], pending["id"])
        self.assertEqual(len(self.runtime.native_aborts), 1)
        self.assertIsNone(stopped["thread"]["activeOperation"])
        repeated = self.service.stop_session_operation(A, U, thread, pending["id"])
        self.assertEqual(repeated["operation"]["state"], "stopped")
        self.assertEqual(len(self.runtime.stop_operation_calls), 1)
        self.assertEqual(len(self.runtime.native_aborts), 1)
        self.assertEqual(len(self.runtime.compact_calls), 1)

    def test_completed_compaction_stop_never_aborts_completed_native_operation(self):
        thread = self.answer("completed before stop")["thread"]["id"]
        completed = self.compact_done(thread, "already-completed")["operation"]
        result = self.service.stop_session_operation(A, U, thread, completed["id"])
        self.assertEqual(result["operation"]["state"], "completed")
        self.assertEqual(result["operation"]["summary"], "SYNTHETIC DISCUSSION SUMMARY")
        self.assertEqual(self.runtime.stop_operation_calls, [])
        self.assertEqual(self.runtime.native_aborts, [])

    def test_unknown_compaction_after_service_restart_can_stop_native_without_resubmitting(self):
        thread = self.answer("restart before stop")["thread"]["id"]
        self.runtime.compact_pending = True
        pending = self.compact_done(thread, "restart-native-stop")["operation"]
        old_runtime = self.runtime
        self.service.shutdown()
        self.runtime = SessionRuntime()
        self.runtime.natives = dict(old_runtime.natives)
        self.runtime.operations = {key: dict(value) for key, value in old_runtime.operations.items()}
        self.runtime.native_counter = old_runtime.native_counter
        self.service = build_service(self.source, self.model, self.root,
                                      runtime_factory=lambda: self.runtime)
        self.assertNotIn(pending["id"], self.service._session_tasks)
        stopped = self.service.stop_session_operation(A, U, thread, pending["id"])
        self.assertEqual(stopped["operation"]["state"], "stopped")
        self.assertEqual(stopped["operation"]["id"], pending["id"])
        self.assertEqual(len(self.runtime.stop_operation_calls), 1)
        self.assertEqual(len(self.runtime.native_aborts), 1)
        self.assertEqual(len(old_runtime.compact_calls), 1)
        self.assertEqual(self.runtime.compact_calls, [])
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertIsNone(stopped["thread"]["activeOperation"])

    def test_delete_failure_keeps_pending_and_cleans_superseded_native_on_retry(self):
        first = self.answer()
        thread = first["thread"]["id"]
        first_native = first["thread"]["runtimeSessionId"]
        self.runtime.natives.pop(first_native)
        second = self.answer("next", thread)
        self.assertEqual(second["run"]["state"], "done")
        self.runtime.delete_fail = True
        result = self.service.delete_thread(A, U, G, thread, "delete")
        self.assertFalse(result["deleted"])
        with self.assertRaises(AdvisorError):
            self.service.save_draft(A, U, G, thread, "late", 0)
        with self.assertRaises(AdvisorError):
            self.service.start(A, U, G, thread, "question", "request-question")
        self.runtime.delete_fail = False
        self.assertTrue(self.service.delete_thread(A, U, G, thread, "delete")["deleted"])
        self.assertEqual(set(self.runtime.deleted), {first_native, second["thread"]["runtimeSessionId"]})
        self.assertTrue(self.service.delete_thread(A, U, G, thread, "delete")["deleted"])
        self.assertEqual(self.service.stores.account(A).thread_messages(thread), [])

    def test_model_window_change_reuses_native_but_missing_native_restores_history(self):
        first = self.answer()
        thread, native = first["thread"]["id"], first["thread"]["runtimeSessionId"]
        self.model.context_tokens = 16000
        second = self.answer("second", thread)
        self.assertEqual(second["thread"]["runtimeSessionId"], native)
        self.runtime.natives.pop(native)
        third = self.answer("third", thread)
        self.assertEqual(third["run"]["state"], "done")
        self.assertNotEqual(third["thread"]["runtimeSessionId"], native)
        recovery = self.runtime.respond_calls[-1][1]["recoveryText"]
        self.assertIn("question", recovery)
        self.assertIn("SYNTHETIC ANSWER", recovery)

    def test_failed_rebuild_preserves_question_and_retry_does_not_duplicate(self):
        first = self.answer()
        thread = first["thread"]["id"]
        database = self.service.stores.account(A)
        for i in range(30):
            database.append_message(thread, "user", "讨论重要细节" * 150 + str(i))
        self.runtime.natives.clear()
        self.runtime.summary_fail = True
        failed = self.answer("retry question", thread)
        self.assertEqual(failed["run"]["state"], "error")
        self.assertEqual(len(self.runtime.respond_calls), 1)
        self.runtime.summary_fail = False
        done = self.answer("retry question", thread, "retry-attempt", retry_run_id=failed["run"]["id"])
        self.assertEqual(done["run"]["state"], "done", done)
        self.assertEqual(sum(row["text"] == "retry question" for row in database.thread_messages(thread)), 1)
        self.assertTrue(self.runtime.summaries)

    def test_missing_earlier_question_or_answer_stops_rebuild_without_model_work(self):
        for missing_seq in (1, 2):
            with self.subTest(missing_seq=missing_seq):
                thread = self.service.new_thread(A, U, G, "new-missing-" + str(missing_seq))["thread"]["id"]
                first = self.answer("original " + str(missing_seq), thread)
                database = self.service.stores.account(A)
                with database.lock, database._connect() as connection:
                    connection.execute("DELETE FROM messages WHERE thread_id=? AND seq=?", (thread, missing_seq))
                self.runtime.natives.pop(first["thread"]["runtimeSessionId"])
                before = len(self.runtime.respond_calls), len(self.runtime.summaries)
                question = "recover missing " + str(missing_seq)
                failed = self.answer(question, thread)
                self.assertEqual(failed["run"]["state"], "error")
                self.assertTrue(any(event["type"] == "error" and event.get("state") == "rebuild-required"
                                    for event in failed["events"]), failed)
                self.assertIn("讨论记录缺失", failed["run"]["error"])
                self.assertEqual((len(self.runtime.respond_calls), len(self.runtime.summaries)), before)
                self.assertEqual(sum(row["role"] == "user" and row["text"] == question
                                     for row in database.thread_messages(thread)), 1)
                self.assertEqual(database.thread(thread)["runtime_session_id"], first["thread"]["runtimeSessionId"])

    def test_second_rebuild_chunk_failure_reuses_first_checkpoint_and_original_question(self):
        first = self.answer("seed discussion")
        thread = first["thread"]["id"]
        database = self.service.stores.account(A)
        for index in range(30):
            database.append_message(thread, "user", "合成讨论的重要决定与未完成事项" * 100 + str(index))
        self.runtime.natives.clear()
        self.runtime.summary_fail_on_call = 2
        failed = self.answer("resume checkpoint", thread)
        self.assertEqual(failed["run"]["state"], "error")
        self.assertEqual(len(self.runtime.summaries), 2)
        first_chunk, failed_chunk = (item["text"] for item in self.runtime.summaries)
        operations = [row for row in database.session_operations(thread) if row["kind"] == "rebuild"]
        self.assertEqual(len(operations), 1)
        self.assertEqual(operations[0]["state"], "failed")
        self.assertEqual(len(operations[0]["checkpoint"]["chunks"]), 1)
        saved_question = database.run(failed["run"]["id"])["user_message_id"]
        self.assertEqual(operations[0]["metadata"]["user_message_id"], saved_question)
        self.runtime.summary_fail_on_call = None
        done = self.answer("resume checkpoint", thread, "resume-checkpoint-retry", retry_run_id=failed["run"]["id"])
        self.assertEqual(done["run"]["state"], "done", done)
        self.assertNotEqual(done["run"]["id"], failed["run"]["id"])
        self.assertEqual(database.run(done["run"]["id"])["user_message_id"], saved_question)
        self.assertEqual(sum(item["text"] == first_chunk for item in self.runtime.summaries), 1)
        self.assertEqual(sum(item["text"] == failed_chunk for item in self.runtime.summaries), 2)
        self.assertEqual(sum(row["role"] == "user" and row["text"] == "resume checkpoint"
                             for row in database.thread_messages(thread)), 1)
        operations = [row for row in database.session_operations(thread) if row["kind"] == "rebuild"]
        self.assertEqual(len(operations), 2)
        self.assertEqual({row["metadata"]["user_message_id"] for row in operations}, {saved_question})
        self.assertEqual({row["state"] for row in operations}, {"failed", "completed"})
        self.assertEqual(len(self.runtime.respond_calls), 2)

    def test_changed_profile_excludes_retired_answers_and_recovery(self):
        first = self.answer("original")
        thread = first["thread"]["id"]
        self.service.friend_stamp = lambda _db, _user: "changed-profile"
        self.service._friend_context = lambda *_args: "CURRENT AUTHORIZED PROFILE"
        done = self.answer("new goal", thread)
        self.assertEqual(done["run"]["state"], "done")
        request = self.runtime.respond_calls[-1][1]
        self.assertNotIn("SYNTHETIC ANSWER original", request["recoveryText"])
        self.assertIn("original", request["recoveryText"])
        self.assertIn("CURRENT AUTHORIZED PROFILE", request["contextFileText"])
        self.assertTrue(any(row["text"] == "SYNTHETIC ANSWER original" for row in done["thread"]["messages"]))

    def test_core_continue_retries_original_question_with_new_execution_token(self):
        self.runtime.fail_response = True
        first = self.answer("continue original")
        self.assertEqual(first["run"]["state"], "error")
        previous = self.service.plugin.tasks[-1]
        self.assertTrue(wait_until(lambda: not self.service._runs[first["run"]["id"]].worker.is_alive()))
        task = continued_task(self.service.plugin, previous)
        self.runtime.fail_response = False
        self.assertEqual(self.service.resume_task(task), "done")
        current = self.service.thread(A, U, G, first["thread"]["id"])
        self.assertEqual(current["run"]["state"], "done")
        self.assertNotEqual(current["run"]["id"], first["run"]["id"])
        self.assertEqual([row["text"] for row in current["thread"]["messages"] if row["role"] == "user"], ["continue original"])
        self.assertEqual(self.service.engine.calls[-1][1], task.token)
        self.assertNotEqual(task.token, previous.token)
        self.assertEqual(len(self.service.plugin.tasks), 1, "continue must use Core's resumed task")
        before = len(self.runtime.respond_calls)
        self.assertEqual(self.service.resume_task(task), "done")
        self.assertEqual(len(self.runtime.respond_calls), before, "same generation must remain idempotent")

    def test_core_continue_rejects_cross_contact_question(self):
        self.runtime.fail_response = True
        first = self.answer("owned question")
        self.assertTrue(wait_until(lambda: not self.service._runs[first["run"]["id"]].worker.is_alive()))
        previous = self.service.plugin.tasks[-1]
        task = continued_task(self.service.plugin, previous, user="other-friend")
        before = len(self.runtime.respond_calls)
        with self.assertRaises(AdvisorError) as caught:
            self.service.resume_task(task)
        self.assertEqual(caught.exception.code, "run-unknown")
        self.assertEqual(len(self.runtime.respond_calls), before)

    def test_core_continue_never_replays_unconfirmed_native_compaction(self):
        thread = self.answer("uncertain compact")["thread"]["id"]
        self.runtime.compact_pending = True
        result = self.compact_done(thread)
        operation = result["operation"]["id"]
        self.assertEqual(result["operation"]["state"], "unknown")
        self.assertTrue(wait_until(lambda: not self.service._session_tasks[operation].worker.is_alive()))
        task = continued_task(self.service.plugin, self.service.plugin.tasks[-1])
        calls = len(self.runtime.compact_calls)
        with self.assertRaises(AdvisorError) as caught:
            self.service.resume_task(task)
        self.assertEqual(caught.exception.code, "run-active")
        self.assertEqual(len(self.runtime.compact_calls), calls)
        self.assertEqual(self.service.stores.account(A).session_operation(operation)["state"], "unknown")

    def test_core_continue_reconciles_completed_compaction_without_paid_replay(self):
        thread = self.answer("completed compact")["thread"]["id"]
        self.runtime.compact_unknown = True
        self.runtime.inspect_fails = True
        result = self.compact_done(thread)
        operation = result["operation"]["id"]
        self.assertEqual(result["operation"]["state"], "unknown")
        self.assertTrue(wait_until(lambda: not self.service._session_tasks[operation].worker.is_alive()))
        self.runtime.inspect_fails = False
        task = continued_task(self.service.plugin, self.service.plugin.tasks[-1])
        calls = len(self.runtime.compact_calls)
        self.assertEqual(self.service.resume_task(task), "done")
        self.assertEqual(len(self.runtime.compact_calls), calls)
        self.assertEqual(self.service.stores.account(A).session_operation(operation)["state"], "completed")

    def test_cancelled_recovery_cannot_save_late_provider_checkpoint(self):
        first = self.answer("recovery cancellation seed")
        thread = first["thread"]["id"]
        database = self.service.stores.account(A)
        for index in range(30):
            database.append_message(thread, "user", "合成讨论必须完整保存" * 150 + str(index))
        self.runtime.natives.clear()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def summarize(config, request, on_event, cancel):
            entered.set()
            self.assertTrue(release.wait(3))
            return {"text": "LATE SYNTHETIC SUMMARY MUST NOT BE SAVED"}
        self.runtime.summarize_discussion = summarize
        started = self.service.start(A, U, G, thread, "stop recovery", "stop-recovery")
        self.assertTrue(entered.wait(2))
        run = self.service._runs[started["run"]["id"]]
        self.service.stop(A, U, run.id)
        saved = database.thread_messages(thread)
        release.set()
        run.worker.join(2)
        self.assertFalse(run.worker.is_alive())
        self.assertEqual(run.state, "stopped")
        self.assertEqual(database.thread_messages(thread), saved)
        self.assertIsNone(database.session_recovery_latest(thread))
        operations = [row for row in database.session_operations(thread) if row["kind"] == "rebuild"]
        self.assertEqual(len(operations), 1)
        self.assertFalse(operations[0].get("checkpoint"))
        self.assertEqual(len(self.runtime.respond_calls), 1)

    def test_account_switch_during_compaction_reconciles_only_after_returning(self):
        thread = self.answer("account switch compaction")["thread"]["id"]
        self.runtime.compact_gate = threading.Event()
        self.addCleanup(self.runtime.compact_gate.set)
        operation = self.service.compact_thread(A, U, G, thread, "switch-during-compact")["operation"]
        self.assertTrue(self.runtime.compact_entered.wait(2))
        task = self.service._session_tasks[operation["id"]]
        self.source.account = "other-synthetic-account"
        self.runtime.compact_gate.set()
        task.worker.join(2)
        self.assertFalse(task.worker.is_alive())
        self.assertNotEqual(task.state, "running")
        self.source.account = A
        current = self.service.session_operation(A, U, thread, operation["id"])
        self.assertEqual(current["operation"]["state"], "interrupted")
        self.assertIsNone(current["operation"]["summary"])
        self.assertIsNone(current["thread"]["activeOperation"])
        self.assertFalse(current["thread"]["compactedSinceUsage"])


if __name__ == "__main__":
    unittest.main()
