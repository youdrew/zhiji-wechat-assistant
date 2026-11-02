"""Synthetic diagnostics lifecycle/security tests; never opens user chat data."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from model_traces import ModelTraceService, TraceError, sanitize
from model_traces_store import TraceStore


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.account = "synthetic-account-a"
        self.service = ModelTraceService(Path(self.directory.name), lambda: self.account, ack_timeout=0.05)
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.service.close)

    def enable(self):
        self.service.update_settings({"enabled": True})
        return self.service.register_execution({"accountId": self.account, "contactId": "synthetic-friend", "source": "synthetic"})

    def emit(self, context, event_type, seq, record="attempt-1", **fields):
        return self.service.ingest(context["producerId"], {"type": event_type, "recordId": record, "eventSeq": seq,
            "collectionVersion": context["collectionVersion"], "deleteVersion": context["deleteVersion"], **fields})

    def viewer(self, scope="account"):
        return self.service.create_session("synthetic-window", scope)["token"]

    def complete(self, context, record="attempt-1"):
        self.emit(context, "start", 0, record, meta={"model": "synthetic", "protocol": "openai", "startedAt": time.time()})
        self.emit(context, "chunk", 1, record, kind="request", contentId="request-1", chunkIndex=0, text='{"model":"synthetic"}', final=True)
        self.emit(context, "chunk", 2, record, kind="response", contentId="response-2", chunkIndex=0, text="合成回复", final=True)
        self.emit(context, "end", 3, record, transportStatus="completed")
        self.service.outcome(context, "success", "adopted")
        self.assertTrue(self.service.flush())

    def test_default_off_and_separate_retention_preference(self):
        self.assertFalse(self.service.snapshot()["enabled"])
        ctx = self.service.register_execution({"accountId": self.account})
        self.assertFalse(self.service.capture_available(ctx))
        self.assertFalse(self.emit(ctx, "start", 0)["accepted"])
        self.service.update_settings({"retentionDays": 30})
        self.assertEqual(self.service.snapshot()["retentionDays"], 30)
        self.assertEqual(self.service.snapshot()["recordCount"], 0)
        with self.assertRaises(TraceError):
            self.service.update_settings({"retentionDays": 365})

    def test_complete_capture_and_history_after_disable(self):
        ctx = self.enable()
        self.complete(ctx)
        self.service.update_settings({"enabled": False})
        token = self.viewer()
        records = self.service.read(token, "records")["records"]
        self.assertEqual(records[0]["status"], "completed")
        self.assertTrue(records[0]["complete"])
        body = self.service.read(token, "body", {"recordId": "attempt-1", "kind": "response"})
        self.assertEqual(body["text"], "合成回复")
        self.assertFalse(self.service.snapshot()["enabled"])

    def test_viewer_cannot_supply_another_account_or_producer(self):
        ctx = self.enable()
        self.complete(ctx)
        token = self.viewer()
        with self.assertRaises(TraceError):
            self.service.read(token, "records", {"account": "synthetic-account-b"})
        with self.assertRaises(TraceError):
            self.service.ingest(token, {"type": "start"})
        self.account = "synthetic-account-b"
        with self.assertRaises(TraceError) as expired:
            self.service.read(token, "records")
        self.assertEqual(expired.exception.status, 409)
        self.assertEqual(self.service.read(self.viewer(), "records")["records"], [])
        self.account = "synthetic-account-a"
        self.assertEqual(self.service.read(self.viewer("application"), "records")["records"], [])

    def test_producer_cannot_forge_scope(self):
        ctx = self.enable()
        self.emit(ctx, "start", 0, meta={"accountId": "other", "taskId": "forged", "contactId": "other", "source": "summary"})
        self.service.flush()
        meta = self.service.read(self.viewer(), "detail", {"recordId": "attempt-1"})["record"]
        self.assertEqual(meta["accountId"], self.account)
        self.assertEqual(meta["contactId"], "synthetic-friend")
        self.assertEqual(meta["taskId"], ctx["taskId"])
        self.assertEqual(meta["source"], "summary")

    def test_stop_ends_capture_not_business_and_late_body_is_rejected(self):
        ctx = self.enable()
        self.assertTrue(self.service.capture_available(ctx))
        self.emit(ctx, "start", 0)
        self.service.flush()
        self.service.update_settings({"enabled": False})
        self.assertFalse(self.service.capture_available(ctx))
        self.assertFalse(self.emit(ctx, "chunk", 1, kind="response", text="late")["accepted"])
        meta = self.service.read(self.viewer(), "detail", {"recordId": "attempt-1"})["record"]
        self.assertEqual(meta["collectionStatus"], "stopped")
        self.assertEqual(meta["status"], "unknown")
        self.assertNotEqual(meta["transportStatus"], "failed")

    def test_clear_fences_queued_and_late_attempts(self):
        ctx = self.enable()
        self.complete(ctx)
        self.service.delete_scope(all_accounts=True)
        self.assertFalse(self.emit(ctx, "start", 4, "late")["accepted"])
        token = self.viewer()
        self.assertEqual(self.service.read(token, "records")["total"], 0)
        new = self.service.register_execution({"accountId": self.account})
        self.complete(new, "new-attempt")
        self.assertEqual(self.service.read(token, "records")["total"], 1)

    def test_scope_deletion_cleans_only_owned_rows(self):
        ctx = self.enable()
        self.complete(ctx)
        other = self.service.register_execution({"accountId": "synthetic-account-b"})
        self.complete(other, "other-attempt")
        self.service.delete_scope(self.account, contact_id="synthetic-friend")
        self.assertEqual(self.service.read(self.viewer(), "records")["total"], 0)
        self.account = "synthetic-account-b"
        self.assertEqual(self.service.read(self.viewer(), "records")["total"], 1)

    def test_out_of_order_chunks_and_duplicate_delivery(self):
        ctx = self.enable()
        self.emit(ctx, "start", 0)
        self.emit(ctx, "chunk", 1, kind="request", contentId="large", chunkIndex=1, text="第二", final=True)
        self.emit(ctx, "chunk", 1, kind="request", contentId="large", chunkIndex=0, text="第一", final=False)
        self.emit(ctx, "chunk", 1, kind="request", contentId="large", chunkIndex=0, text="第一", final=False)
        self.emit(ctx, "end", 2, transportStatus="completed")
        self.service.outcome(ctx, "success", "adopted")
        self.service.flush()
        result = self.service.read(self.viewer(), "body", {"recordId": "attempt-1"})
        self.assertEqual(result["text"], "第一第二")
        self.assertTrue(result["complete"])

    def test_missing_or_conflicting_chunks_do_not_claim_complete(self):
        ctx = self.enable()
        self.emit(ctx, "start", 0)
        self.emit(ctx, "chunk", 1, kind="response", contentId="body", chunkIndex=1, text="gap", final=True)
        self.emit(ctx, "end", 2, transportStatus="completed")
        self.service.outcome(ctx, "success", "adopted")
        self.service.flush()
        meta = self.service.read(self.viewer(), "detail", {"recordId": "attempt-1"})["record"]
        self.assertFalse(meta["complete"])
        self.assertEqual(meta["status"], "completed")  # completeness is independent
        self.emit(ctx, "chunk", 1, kind="response", contentId="body", chunkIndex=1, text="different", final=True)
        self.service.flush()
        meta = self.service.read(self.viewer(), "detail", {"recordId": "attempt-1"})["record"]
        self.assertIn("不同内容", meta["incompleteReason"])

    def test_processing_and_parse_failure_are_distinct_from_http_success(self):
        ctx = self.enable()
        self.emit(ctx, "start", 0)
        self.emit(ctx, "end", 1, transportStatus="completed", httpStatus=200)
        self.service.flush()
        token = self.viewer()
        self.assertEqual(self.service.read(token, "detail", {"recordId": "attempt-1"})["record"]["status"], "running")
        self.emit(ctx, "outcome", 2, applicationStatus="parse-failed", adoption="discarded")
        self.service.outcome(ctx, "success", "adopted")
        self.service.flush()
        meta = self.service.read(token, "detail", {"recordId": "attempt-1"})["record"]
        self.assertEqual(meta["transportStatus"], "completed")
        self.assertEqual(meta["status"], "error")

    def test_terminal_outcome_arriving_before_http_attempt_is_kept(self):
        ctx = self.enable()
        self.service.outcome(ctx, "success", "adopted")
        self.emit(ctx, "start", 0)
        self.emit(ctx, "end", 1, transportStatus="completed")
        self.service.flush()
        self.assertEqual(self.service.read(self.viewer(), "detail", {"recordId": "attempt-1"})["record"]["status"], "completed")

    def test_disable_save_failure_stays_disabled(self):
        self.enable()
        with patch.object(self.service._store, "save_settings", side_effect=OSError("synthetic")):
            state = self.service.update_settings({"enabled": False})
        self.assertFalse(state["collecting"])
        self.assertFalse(state["enabled"])
        self.assertIn("重启可能恢复", state["saveError"])

    def test_unacknowledged_live_producer_prevents_enable(self):
        ctx = self.service.register_execution({"accountId": self.account})
        self.service.producer_state(ctx["producerId"])
        state = self.service.update_settings({"enabled": True})
        self.assertFalse(state["enabled"])
        self.assertIn("未确认", state["saveError"])
        self.service.unregister(ctx["producerId"])
        self.assertTrue(self.service.update_settings({"enabled": True})["collecting"])

    def test_pause_preserves_preference_and_recovery_has_new_version(self):
        ctx = self.enable()
        self.emit(ctx, "start", 0)
        self.service.flush()
        with self.service.lock:
            self.service._pause("synthetic storage fault")
        state = self.service.snapshot()
        self.assertTrue(state["enabled"])
        self.assertFalse(state["collecting"])
        recovered = self.service.check_health()
        self.assertTrue(recovered["collecting"])
        self.assertGreater(recovered["collectionVersion"], ctx["collectionVersion"])
        self.assertFalse(self.emit(ctx, "end", 1, transportStatus="completed")["accepted"])

    def test_startup_storage_recovery_restores_saved_preferences(self):
        store = TraceStore(self.directory.name)
        store.save_settings({"enabled": True, "retentionDays": 30})
        store.close()
        ready = False
        def factory(root):
            if not ready:
                raise OSError("synthetic unavailable storage")
            return TraceStore(root)
        self.service._store_factory = factory
        initial = self.service.snapshot()
        self.assertEqual(initial["status"], "paused")
        ready = True
        recovered = self.service.check_health()
        self.assertTrue(recovered["enabled"])
        self.assertTrue(recovered["collecting"])
        self.assertEqual(recovered["retentionDays"], 30)
        self.assertGreater(recovered["collectionVersion"], initial["collectionVersion"])
        self.assertIsNotNone(recovered["pauseHistory"][-1]["finishedAt"])

    def test_disable_during_startup_failure_survives_storage_recovery(self):
        store = TraceStore(self.directory.name)
        store.save_settings({"enabled": True, "retentionDays": 30})
        store.close()
        ready = False
        def factory(root):
            if not ready:
                raise OSError("synthetic unavailable storage")
            return TraceStore(root)
        self.service._store_factory = factory
        stopped = self.service.update_settings({"enabled": False})
        self.assertFalse(stopped["collecting"])
        self.assertIn("本次已停止", stopped["saveError"])
        ready = True
        recovered = self.service.check_health()
        self.assertFalse(recovered["enabled"])
        self.assertFalse(recovered["collecting"])
        self.assertEqual(recovered["status"], "off")
        self.assertEqual(recovered["retentionDays"], 30)
        self.assertEqual(self.service._store.settings, {"enabled": False, "retentionDays": 30})
        self.assertIsNone(recovered["saveError"])

    def test_metadata_is_paged_without_body_and_body_is_bounded(self):
        ctx = self.enable()
        for index in range(55):
            self.complete(ctx, "attempt-" + str(index))
        token = self.viewer()
        page = self.service.read(token, "records", {"limit": "9999"})
        self.assertEqual(len(page["records"]), 50)
        self.assertEqual(page["nextOffset"], 50)
        self.assertNotIn("text", json.dumps(page))
        first = self.service.read(token, "body", {"recordId": "attempt-1", "kind": "response", "limit": 4})
        self.assertLessEqual(len(first["text"].encode()), 4)
        second = self.service.read(token, "body", {"recordId": "attempt-1", "kind": "response", "offset": first["nextOffset"]})
        self.assertEqual(first["text"] + second["text"], "合成回复")

    def test_header_allowlist_and_url_credentials(self):
        result = sanitize({"endpoint": "https://name:demo@synthetic.invalid/v1?key=test-secret&model=test",
                           "headers": {"Authorization": "secret", "Cookie": "secret", "Content-Type": "application/json"},
                           "nested": {"apiKey": "secret", "messages": "synthetic content"}})
        encoded = json.dumps(result)
        self.assertNotIn("test-secret", encoded)
        self.assertNotIn("demo@", encoded)
        self.assertEqual(result["headers"], {"content-type": "application/json"})
        self.assertEqual(result["nested"]["messages"], "synthetic content")

    def test_shared_process_budget_refuses_ninth_process_and_recovers(self):
        contexts = [self.enable()]
        contexts.extend(self.service.register_execution({"accountId": self.account}) for _ in range(8))
        for index, context in enumerate(contexts[:8]):
            self.assertTrue(self.service.bind_transport(context["producerId"], index + 100)["collecting"])
        self.assertFalse(self.service.bind_transport(contexts[8]["producerId"], 108)["collecting"])
        state = self.service.snapshot()
        self.assertEqual(state["producerBudgetBytes"], 48 * 1024 * 1024)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["status"], "paused")
        same = self.service.register_execution({"accountId": self.account})
        self.service.bind_transport(same["producerId"], 100)
        self.assertEqual(self.service.snapshot()["producerBudgetBytes"], 48 * 1024 * 1024)
        with self.assertRaises(TraceError):
            self.service.bind_transport(self.viewer(), 1)
        self.service.unregister(contexts[0]["producerId"])
        self.service.unregister(same["producerId"])
        with self.service.lock:
            self.service._producers[contexts[0]["producerId"]]["retireAt"] = time.monotonic() - 1
            self.service._producers[same["producerId"]]["retireAt"] = time.monotonic() - 1
            self.service._retire()
        self.assertTrue(self.service.check_health()["collecting"])
        self.assertEqual(self.service.snapshot()["producerBudgetBytes"], 48 * 1024 * 1024)

    def test_loss_reports_are_counted_once_per_process(self):
        ctx = self.enable()
        other = self.service.register_execution({"accountId": self.account})
        for item in (ctx, other):
            self.service.bind_transport(item["producerId"], 12345)
            self.service.ack(item["producerId"], self.service.snapshot()["collectionVersion"], 4)
        self.assertEqual(self.service.snapshot()["lostEvents"], 4)
        self.assertEqual(self.service.snapshot()["status"], "paused")
        self.assertTrue(self.service.snapshot()["enabled"])

    def test_account_epoch_invalidates_returned_account_session(self):
        self.service.current_account_version = lambda: self.epoch
        self.epoch = 1
        token = self.viewer()
        self.epoch = 3  # account A -> B -> A occurred without a diagnostic read.
        with self.assertRaises(TraceError):
            self.service.read(token, "records")

    def test_scoped_delete_revokes_background_producer(self):
        context = self.enable()
        self.complete(context)
        self.service.delete_scope(self.account)
        with self.assertRaises(TraceError):
            self.service.producer_state(context["producerId"])

    def test_preparation_event_is_zero_requests_and_collapses_after_attempt(self):
        context = self.enable()
        self.service.task_event(context, {"kind": "preparing"})
        self.service.flush()
        token = self.viewer()
        self.assertEqual(self.service.snapshot()["recordCount"], 0)
        self.assertEqual(len(self.service.read(token, "records")["records"]), 1)
        self.complete(context)
        self.assertEqual(self.service.snapshot()["recordCount"], 1)
        self.assertEqual(len(self.service.read(token, "records")["records"]), 1)
        chain = self.service.read(token, "records", {"taskId": context["taskId"]})
        self.assertEqual(len(chain["records"]), 2)

    def test_successful_zero_request_task_has_completed_status(self):
        context = self.enable()
        self.service.task_event(context, {"kind": "cache-hit"})
        self.service.outcome(context, "success", "adopted")
        self.service.flush()
        records = self.service.read(self.viewer(), "records")["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["requestCount"], 0)
        self.assertEqual(records[0]["transportStatus"], "not-sent")
        self.assertEqual(records[0]["status"], "completed")

    def test_resumed_job_gets_fresh_execution_and_persistent_previous_task(self):
        self.service.update_settings({"enabled": True})
        first = self.service.register_execution({"accountId": self.account, "businessJobId": "synthetic-job"})
        self.complete(first)
        second = self.service.register_execution({"accountId": self.account, "businessJobId": "synthetic-job"})
        self.assertNotEqual(first["taskId"], second["taskId"])
        self.assertNotEqual(first["executionId"], second["executionId"])
        self.assertEqual(second["resumeOfTaskId"], first["taskId"])

    def test_explicit_parser_outcome_survives_late_start_and_task_success(self):
        context = self.enable()
        self.service.record_outcome(context, "attempt-1", "parse-failed", "discarded", error_code="invalid-json")
        self.complete(context)
        token = self.viewer()
        first = self.service.read(token, "detail", {"recordId": "attempt-1"})["record"]
        self.assertEqual(first["applicationStatus"], "parse-failed")
        self.assertEqual(first["errorCode"], "invalid-json")
        self.assertEqual(first["status"], "error")
        foreign = self.service.register_execution({"accountId": self.account})
        with self.assertRaises(TraceError):
            self.service.record_outcome(foreign, "attempt-1", "success", "adopted")

    def test_parser_success_pending_adoption_resolves_from_final_business_result(self):
        context = self.enable()
        self.emit(context, "start", 0)
        self.emit(context, "end", 1, transportStatus="completed")
        self.service.record_outcome(context, "attempt-1", "success", "pending")
        self.service.flush()
        token = self.viewer()
        self.assertEqual(self.service.read(token, "detail", {"recordId": "attempt-1"})["record"]["status"], "running")
        self.service.outcome(context, "success", "discarded")
        self.service.flush()
        meta = self.service.read(token, "detail", {"recordId": "attempt-1"})["record"]
        self.assertEqual(meta["applicationStatus"], "success")
        self.assertEqual(meta["adoption"], "discarded")
        self.assertEqual(meta["status"], "not_adopted")

    def test_source_identity_never_waits_while_holding_diagnostic_lock(self):
        source_lock = threading.RLock()
        entered = threading.Event()
        def identity():
            entered.set()
            with source_lock:
                return self.account
        self.service.current_account = identity
        token = self.viewer()
        errors = []
        def read():
            try:
                self.service.read(token, "records")
            except Exception as error:
                errors.append(error)
        with source_lock:
            entered.clear()
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            self.assertTrue(entered.wait(1))
            acquired = self.service.lock.acquire(timeout=0.5)
            if acquired:
                try:
                    self.service.delete_scope("synthetic-other-account")
                finally:
                    self.service.lock.release()
        reader.join(2)
        self.assertTrue(acquired, "viewer held trace lock while waiting for source lock")
        self.assertFalse(reader.is_alive())
        self.assertEqual(errors, [])

    def test_retention_deletes_only_expired_attempt_and_fences_viewers(self):
        context = self.enable()
        now = time.time()
        self.emit(context, "start", 0, "expired", meta={"startedAt": now - 7 * 86400 + 3600})
        self.emit(context, "chunk", 1, "expired", kind="request", contentId="request", chunkIndex=0, text="old private synthetic", final=True)
        self.emit(context, "start", 0, "live", meta={"startedAt": now})
        self.emit(context, "chunk", 1, "live", kind="request", contentId="request", chunkIndex=0, text="live synthetic", final=True)
        self.service.flush()
        token = self.viewer()
        before = self.service.read(token, "records")
        producer_before = self.service.producer_state(context["producerId"])
        with patch("model_traces.time.time", return_value=now + 7200):
            listing = self.service.read(token, "records")
            self.assertEqual([row["recordId"] for row in listing["records"]], ["live"])
            self.assertGreater(listing["deleteVersion"], before["deleteVersion"])
            producer_after = self.service.producer_state(context["producerId"])
            self.assertEqual(producer_after["deleteVersion"], producer_before["deleteVersion"])
            self.assertEqual(producer_after["collectionVersion"], producer_before["collectionVersion"])
            self.assertTrue(producer_after["collecting"])
            self.assertGreater(producer_after["retentionCutoffAt"], producer_before["retentionCutoffAt"])
            with self.assertRaises(TraceError) as missing:
                self.service.read(token, "body", {"recordId": "expired"})
            self.assertEqual(missing.exception.status, 410)
            # Even a replay with a changed timestamp cannot resurrect a seen ID.
            self.emit(context, "start", 0, "expired", meta={"startedAt": now + 7200})
            self.emit(context, "chunk", 2, "expired", kind="response", text="must never return", final=True)
            self.service.record_outcome(context, "expired", "success", "adopted", result="must never return")
            self.emit(context, "chunk", 2, "live", kind="response", text="continued after expiration", final=True)
            self.emit(context, "end", 3, "live", transportStatus="completed")
            self.service.outcome(context, "success", "adopted")
            self.service.flush()
            live = self.service.read(token, "detail", {"recordId": "live"})["record"]
            self.assertEqual(live["status"], "completed")
            self.assertTrue(live["complete"])
            self.assertEqual(self.service.read(token, "body", {"recordId": "live", "kind": "response"})["text"], "continued after expiration")
            self.assertEqual(self.service.snapshot()["recordCount"], 1)
        self.service.close()
        restored = ModelTraceService(Path(self.directory.name), lambda: self.account)
        self.addCleanup(restored.close)
        restored_token = restored.create_session("after-restart")["token"]
        self.assertEqual([row["recordId"] for row in restored.read(restored_token, "records")["records"]], ["live"])
        with self.assertRaises(TraceError):
            restored.producer_state(context["producerId"])
        self.assertNotIn(b"old private synthetic", (restored._store.root / "traces.sqlite3").read_bytes())

    def test_account_delete_keeps_other_account_capture_and_queued_body(self):
        account_a = self.enable()
        account_b = self.service.register_execution({"accountId": "synthetic-account-b", "source": "synthetic"})
        self.emit(account_a, "start", 0, "a")
        self.emit(account_b, "start", 0, "b")
        self.service.flush()
        before = self.service.producer_state(account_b["producerId"])
        with self.service.lock:
            self.emit(account_b, "chunk", 1, "b", kind="response", text="kept in other account", final=True)
            self.service.delete_scope(self.account)
        self.service.flush()
        with self.assertRaises(TraceError):
            self.service.producer_state(account_a["producerId"])
        after = self.service.producer_state(account_b["producerId"])
        self.assertEqual((after["deleteVersion"], after["collectionVersion"]), (before["deleteVersion"], before["collectionVersion"]))
        self.assertTrue(after["collecting"])
        self.emit(account_b, "end", 2, "b", transportStatus="completed")
        self.service.outcome(account_b, "success", "adopted")
        self.service.flush()
        self.account = "synthetic-account-b"
        token = self.viewer()
        self.assertEqual(self.service.read(token, "body", {"recordId": "b", "kind": "response"})["text"], "kept in other account")
        self.assertEqual(self.service.read(token, "detail", {"recordId": "b"})["record"]["status"], "completed")

    def test_capacity_evicts_completed_attempt_without_stopping_live_attempt(self):
        completed = self.enable()
        self.complete(completed, "completed")
        live = self.service.register_execution({"accountId": self.account})
        self.emit(live, "start", 0, "live")
        self.service.flush()
        self.service._store.record_limit = 2
        token = self.viewer()
        before = self.service.read(token, "records")
        newer = self.service.register_execution({"accountId": self.account})
        self.emit(newer, "start", 0, "new")
        self.service.flush()
        self.emit(live, "chunk", 1, "live", kind="response", text="survived capacity cleanup", final=True)
        self.emit(live, "end", 2, "live", transportStatus="completed")
        self.service.outcome(live, "success", "adopted")
        self.service.flush()
        listing = self.service.read(token, "records")
        self.assertEqual({row["recordId"] for row in listing["records"]}, {"live", "new"})
        self.assertGreater(listing["deleteVersion"], before["deleteVersion"])
        self.assertEqual(self.service.read(token, "detail", {"recordId": "live"})["record"]["status"], "completed")
        self.assertEqual(self.service.producer_state(live["producerId"])["collectionVersion"], live["collectionVersion"])
        self.emit(completed, "start", 0, "completed")
        self.service.flush()
        self.assertIsNone(self.service._store.metadata("completed"))

    def test_record_tombstone_budget_is_global_and_released_on_retirement(self):
        context = self.enable()
        with patch("model_traces.TOMBSTONE_LIMIT", 2):
            self.emit(context, "start", 0, "first")
            self.emit(context, "start", 0, "second")
            self.emit(context, "start", 0, "over-budget")
            self.service.flush()
            state = self.service.snapshot()
            self.assertEqual(state["knownRecordIds"], 2)
            self.assertEqual(state["status"], "paused")
            self.assertTrue(state["enabled"])
            self.assertIsNone(self.service._store.metadata("over-budget"))
            self.service.unregister(context["producerId"])
            with self.service.lock:
                self.service._producers[context["producerId"]]["retireAt"] = time.monotonic() - 1
                self.service._retire()
            self.assertEqual(self.service.snapshot()["knownRecordIds"], 0)
            self.assertTrue(self.service.check_health()["collecting"])

    def test_failed_delete_is_replayed_before_a_different_scope_can_clear(self):
        first = self.enable()
        self.complete(first, "account-a-record")
        second = self.service.register_execution({"accountId": "synthetic-account-b"})
        self.complete(second, "account-b-record")
        token = self.viewer()
        store = self.service._store
        with patch.object(store, "_finish_delete", side_effect=OSError("synthetic unavailable disk")):
            with self.assertRaises(TraceError):
                self.service.delete_scope(all_accounts=True)
            original_intent = dict(store.deletion["pending"])
            with self.assertRaises(TraceError):
                self.service.delete_scope("synthetic-account-b")
            self.assertEqual(store.deletion["pending"], original_intent)
            self.assertEqual(json.loads(store.fence_path.read_text(encoding="utf-8"))["pending"], original_intent)
            with self.assertRaises(TraceError):
                self.service.read(token, "records")
        self.service.delete_scope("synthetic-account-b")
        self.assertEqual(self.service.read(token, "records")["records"], [])
        self.assertEqual(store.count(), 0)
        self.assertIsNone(store.deletion["pending"])
        self.assertIsNone(json.loads(store.fence_path.read_text(encoding="utf-8"))["pending"])

    def test_retirement_failure_preserves_pending_budget_until_successful_retry(self):
        context = self.enable()
        self.complete(context)
        self.service.record_outcome(context, "not-yet-started", "success", "pending", result="synthetic pending result")
        self.assertTrue(self.service.flush())
        with self.service.lock:
            producer = self.service._producers[context["producerId"]]
            producer["retireAt"] = time.monotonic() - 1
            pending_bytes = self.service._pending_bytes
            self.assertGreater(pending_bytes, 0)
            with patch.object(self.service._store, "metadata", side_effect=sqlite3.OperationalError("synthetic disk fault")):
                for _ in range(2):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.service._retire()
                    self.assertEqual(self.service._pending_bytes, pending_bytes)
                    self.assertIs(self.service._producers[context["producerId"]], producer)
            self.service._retire()
            self.assertNotIn(context["producerId"], self.service._producers)
            self.assertEqual(self.service._pending_bytes, 0)

    def test_retirement_fault_does_not_kill_storage_worker(self):
        context = self.enable()
        self.complete(context)
        fault_observed = threading.Event()
        retried = threading.Event()
        retire = self.service._retire
        def fail_once():
            if not fault_observed.is_set():
                fault_observed.set()
                raise sqlite3.OperationalError("synthetic retirement failure")
            retire()
            retried.set()
        with patch.object(self.service, "_retire", side_effect=fail_once):
            self.assertTrue(fault_observed.wait(3))
            self.service._thread.join(timeout=0.1)
            self.assertTrue(self.service._thread.is_alive())
            self.assertTrue(retried.wait(3))
        self.assertTrue(self.service.check_health()["collecting"])
        recovered = self.service.register_execution({"accountId": self.account})
        self.complete(recovered, "after-retirement-fault")
        self.assertEqual(self.service.read(self.viewer(), "detail", {"recordId": "after-retirement-fault"})["record"]["status"], "completed")

    def test_retirement_commit_failure_rolls_back_before_retry(self):
        context = self.enable()
        self.emit(context, "start", 0)
        self.service.record_outcome(context, "not-yet-started", "success", "pending", result="synthetic pending result")
        self.assertTrue(self.service.flush())
        store = self.service._store
        connection = store.db
        class FailFirstCommit:
            failed = False
            def __getattr__(self, name):
                return getattr(connection, name)
            def commit(self):
                if not self.failed:
                    self.failed = True
                    raise sqlite3.OperationalError("synthetic commit failure")
                return connection.commit()
        with self.service.lock, patch.object(store, "db", FailFirstCommit()):
            producer = self.service._producers[context["producerId"]]
            producer["retireAt"] = time.monotonic() - 1
            pending_bytes, revision = self.service._pending_bytes, self.service._revision
            with self.assertRaises(sqlite3.OperationalError):
                self.service._retire()
            self.assertEqual(store.metadata("attempt-1")["collectionStatus"], "recording")
            self.assertEqual(self.service._revision, revision)
            self.assertEqual(self.service._pending_bytes, pending_bytes)
            self.assertIs(self.service._producers[context["producerId"]], producer)
            self.service._retire()
            self.assertEqual(store.metadata("attempt-1")["collectionStatus"], "interrupted")
            self.assertEqual(self.service._revision, revision + 1)
            self.assertEqual(self.service._pending_bytes, 0)
            self.assertNotIn(context["producerId"], self.service._producers)


class TraceStoreTests(unittest.TestCase):
    def test_restart_marks_inflight_attempt_status_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(directory)
            store.insert({"recordId": "unfinished", "taskId": "t", "startedAt": time.time(),
                          "collectionStatus": "recording", "status": "running", "transportStatus": "running"})
            store.close()
            restored = TraceStore(directory)
            try:
                record = restored.metadata("unfinished")
                self.assertEqual(record["collectionStatus"], "interrupted")
                self.assertEqual(record["status"], "unknown")
                self.assertEqual(record["transportStatus"], "unknown")
                self.assertFalse(record["complete"])
            finally:
                restored.close()

    def test_durable_delete_intent_replays_before_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(directory)
            store.insert({"recordId": "a", "taskId": "t", "accountId": "account", "startedAt": time.time(), "collectionStatus": "recording"})
            store.append("a", {"eventSeq": 1, "kind": "request", "text": "synthetic-private-body", "final": True})
            store.db.commit()
            with patch.object(store, "_finish_delete", side_effect=OSError("synthetic crash")):
                with self.assertRaises(OSError):
                    store.delete({"accountId": "account"})
            store.close()
            restored = TraceStore(directory)
            self.assertEqual(restored.count(), 0)
            self.assertIsNone(restored.deletion["pending"])
            self.assertNotIn(b"synthetic-private-body", (restored.root / "traces.sqlite3").read_bytes())
            restored.close()

    def test_limits_reclaim_finished_and_expire_active(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TraceStore(directory, side_limit=7, record_limit=2)
            store.insert({"recordId": "a", "taskId": "t", "startedAt": time.time(), "collectionStatus": "complete"})
            status = store.append("a", {"eventSeq": 1, "kind": "request", "text": "中文合成", "final": True})
            self.assertEqual(status, "truncated")
            self.assertEqual(store.body("a", "request")["text"], "中文")
            store.insert({"recordId": "b", "taskId": "t", "startedAt": time.time() - 8 * 86400, "collectionStatus": "recording"})
            store.db.commit()
            self.assertTrue(store.reclaim())
            self.assertIsNone(store.metadata("b"))
            store.close()


if __name__ == "__main__":
    unittest.main()
