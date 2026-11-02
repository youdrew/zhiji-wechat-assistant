"""Synthetic-only durable outgoing-queue tests; no live accounts or models.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from contracts import AdvisorError
from store import AdvisorDatabase


class QueueStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user, self.agent = "synthetic-friend", "builtin:advisor"
        self.db = AdvisorDatabase(self.root, "synthetic-queue-account")
        self.addCleanup(lambda: self.db.close())
        self.thread = self.db.create_thread(self.user, self.agent, "synthetic-source", select=True)
        self.tid = self.thread["id"]

    def reopen(self):
        self.db.close()
        self.db = AdvisorDatabase(self.root, "synthetic-queue-account")
        self.db.thread(self.tid)

    def enqueue(self, request="synthetic-request-1", text="合成排队问题", **kwargs):
        return self.db.queue_enqueue(self.user, self.agent, self.tid, text, request, **kwargs)

    def get(self):
        return self.db.queue_get(self.user, self.agent, self.tid)

    def claim(self):
        return self.db.queue_claim(self.user, self.agent, self.tid)

    def assert_code(self, code, fn):
        with self.assertRaises(AdvisorError) as raised:
            fn()
        self.assertEqual(raised.exception.code, code)

    def run_claim(self, item):
        return self.db.create_run(self.thread, item["message"], item["dispatchRequestId"], 1, 1, "synthetic-source")[0]

    def test_uncreated_queue_is_empty_without_writing_state(self):
        self.assertEqual(self.get(), {"paused": False, "reason": None, "version": 0, "items": []})
        self.assertIsNone(self.claim())
        self.assertEqual(self.get()["version"], 0)

    def test_acceptance_is_durable_without_a_chat_message_or_run(self):
        result = self.enqueue(thinking=False, mentions=["synthetic-mentioned-friend"])
        self.assertFalse(result["duplicate"])
        item = result["item"]
        self.assertEqual(item["state"], "pending")
        self.assertFalse(item["thinking"])
        self.assertEqual(item["mentions"], ["synthetic-mentioned-friend"])
        self.assertNotEqual(item["requestId"], item["dispatchRequestId"])
        self.assertEqual(self.db.thread_messages(self.tid), [])
        self.assertIsNone(self.db.latest_run(self.tid))
        self.reopen()
        restored = self.get()
        self.assertTrue(restored["paused"])
        self.assertEqual(restored["reason"], "restart")
        self.assertEqual(restored["items"], [item])
        self.assertIsNone(self.claim())

    def test_enqueue_retry_keeps_original_acceptance_after_edit(self):
        item = self.enqueue(thinking=False, mentions=["synthetic-mentioned-friend"])["item"]
        edited = self.db.queue_edit(self.user, self.agent, self.tid, item["id"], "修改后的合成问题", item["version"])
        again = self.enqueue(thinking=False, mentions=["synthetic-mentioned-friend"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["item"], edited)
        self.assertEqual(len(self.get()["items"]), 1)
        self.assertFalse(edited["thinking"])
        self.assertEqual(edited["mentions"], item["mentions"])
        self.assert_code("request-conflict", lambda: self.enqueue(text="修改后的合成问题", thinking=False,
                                                                  mentions=["synthetic-mentioned-friend"]))
        self.assert_code("request-conflict", lambda: self.enqueue(thinking=True, mentions=["synthetic-mentioned-friend"]))
        self.assert_code("request-conflict", lambda: self.enqueue(thinking=False, mentions=[]))

    def test_remove_erases_payload_but_deduplicates_original_request_after_restart(self):
        item = self.enqueue(mentions=["synthetic-mentioned-friend"])["item"]
        removed = self.db.queue_remove(self.user, self.agent, self.tid, item["id"], item["version"])
        self.assertEqual(removed["message"], "")
        self.assertEqual(removed["mentions"], [])
        self.assertEqual(self.get()["items"], [])
        self.reopen()
        again = self.enqueue(mentions=["synthetic-mentioned-friend"])
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["item"]["state"], "removed")
        self.assertEqual(self.get()["items"], [])

    def test_edit_and_remove_require_pending_version(self):
        item = self.enqueue()["item"]
        edited = self.db.queue_edit(self.user, self.agent, self.tid, item["id"], "编辑文本", item["version"])
        self.assert_code("queue-conflict", lambda: self.db.queue_edit(self.user, self.agent, self.tid, item["id"], "旧文本", item["version"]))
        self.assert_code("queue-conflict", lambda: self.db.queue_remove(self.user, self.agent, self.tid, item["id"], item["version"]))
        self.claim()
        self.assert_code("queue-item-started", lambda: self.db.queue_edit(self.user, self.agent, self.tid, item["id"], "晚到文本", edited["version"]))
        self.assert_code("queue-item-started", lambda: self.db.queue_remove(self.user, self.agent, self.tid, item["id"], edited["version"]))

    def test_fifo_single_claim_and_active_run_block_next(self):
        first = self.enqueue()["item"]
        second = self.enqueue("synthetic-request-2", "第二条问题")["item"]
        claimed = self.claim()
        self.assertEqual(claimed["id"], first["id"])
        self.assertEqual(claimed["state"], "starting")
        self.assertIsNone(self.claim())
        run = self.run_claim(claimed)
        self.db.queue_dispatched(self.user, self.agent, self.tid, first["id"], run["id"])
        self.assertIsNone(self.claim())
        self.db.finish_run(run["id"], self.tid, "done", assistant_text="合成答案")
        self.assertEqual(self.claim()["id"], second["id"])

    def test_compaction_blocks_claim(self):
        self.enqueue()
        operation = self.db.session_operation_begin(self.user, self.agent, self.tid, "compact", "synthetic-compact")
        self.assertIsNone(self.claim())
        self.db.session_operation_update(operation["id"], "completed")
        self.assertIsNotNone(self.claim())

    def test_pause_resume_versions_and_new_items_stay_paused(self):
        item = self.enqueue()["item"]
        version = self.get()["version"]
        paused = self.db.queue_set_paused(self.user, self.agent, self.tid, True, "manual", version)
        self.assertTrue(paused["paused"])
        self.assertIsNone(self.claim())
        self.enqueue("synthetic-request-2", "第二条问题")
        self.assertTrue(self.get()["paused"])
        self.assert_code("queue-conflict", lambda: self.db.queue_set_paused(self.user, self.agent, self.tid, False, expected_version=version))
        resumed = self.db.queue_set_paused(self.user, self.agent, self.tid, False, expected_version=self.get()["version"])
        self.assertFalse(resumed["paused"])
        self.assertIsNone(resumed["reason"])
        self.assertEqual(self.claim()["id"], item["id"])

    def test_pause_empty_queue_is_explicit_and_persists(self):
        paused = self.db.queue_set_paused(self.user, self.agent, self.tid, True, "manual", 0)
        self.assertGreater(paused["version"], 0)
        self.enqueue()
        self.assertTrue(self.get()["paused"])
        self.assertIsNone(self.claim())

    def test_completed_empty_queue_does_not_become_paused_after_restart(self):
        self.enqueue()
        claimed = self.claim()
        run = self.run_claim(claimed)
        self.db.queue_dispatched(self.user, self.agent, self.tid, claimed["id"], run["id"])
        self.db.finish_run(run["id"], self.tid, "done", assistant_text="合成答案")
        self.reopen()
        self.assertFalse(self.get()["paused"])
        self.assertIsNone(self.get()["reason"])
        self.enqueue("synthetic-request-2", "下一条合成问题")
        self.assertIsNotNone(self.claim())

    def test_manually_paused_empty_queue_stays_paused_after_restart(self):
        self.db.queue_set_paused(self.user, self.agent, self.tid, True, "manual")
        self.reopen()
        self.assertTrue(self.get()["paused"])
        self.assertEqual(self.get()["reason"], "manual")

    def test_dispatch_idempotent_and_run_ownership_checked(self):
        self.enqueue()
        claimed = self.claim()
        self.assert_code("queue-conflict", lambda: self.db.queue_dispatched(self.user, self.agent, self.tid, claimed["id"], "run_missing"))
        run = self.run_claim(claimed)
        dispatched = self.db.queue_dispatched(self.user, self.agent, self.tid, claimed["id"], run["id"])
        self.assertEqual(dispatched["state"], "dispatched")
        self.assertEqual(dispatched["message"], "")
        self.assertEqual(self.db.queue_dispatched(self.user, self.agent, self.tid, claimed["id"], run["id"]), dispatched)
        self.assertEqual(self.enqueue()["item"], dispatched)
        self.assertEqual(self.get()["items"], [])

    def test_pre_run_claim_interrupted_by_restart_requires_explicit_resume(self):
        self.enqueue()
        claimed = self.claim()
        self.reopen()
        queue = self.get()
        self.assertTrue(queue["paused"])
        self.assertEqual(queue["items"][0]["state"], "pending")
        self.assertEqual(queue["items"][0]["dispatchRequestId"], claimed["dispatchRequestId"])
        self.assertIsNone(self.claim())
        self.db.queue_set_paused(self.user, self.agent, self.tid, False)
        self.assertEqual(self.claim()["id"], claimed["id"])

    def test_crash_after_create_run_never_replays_accepted_question(self):
        self.enqueue()
        claimed = self.claim()
        run = self.run_claim(claimed)
        self.reopen()
        self.assertEqual(self.get()["items"], [])
        self.assertTrue(self.get()["paused"])
        self.assertEqual(self.db.run(run["id"])["state"], "error")
        duplicate = self.enqueue()
        self.assertEqual(duplicate["item"]["runId"], run["id"])
        self.assertEqual(duplicate["item"]["state"], "dispatched")
        self.db.queue_set_paused(self.user, self.agent, self.tid, False)
        self.assertIsNone(self.claim())
        self.assertEqual(len([row for row in self.db.thread_messages(self.tid) if row["role"] == "user"]), 1)

    def test_failed_predispatch_release_pauses_retains_same_item(self):
        self.enqueue()
        claimed = self.claim()
        released = self.db.queue_release(self.user, self.agent, self.tid, claimed["id"], "api-not-configured")
        self.assertEqual(released["state"], "pending")
        self.assertEqual(released["dispatchRequestId"], claimed["dispatchRequestId"])
        self.assertEqual(self.get()["reason"], "api-not-configured")
        self.assertTrue(self.get()["paused"])
        self.assertIsNone(self.claim())

    def test_release_after_run_creation_never_returns_item_to_pending(self):
        self.enqueue()
        claimed = self.claim()
        run = self.run_claim(claimed)
        released = self.db.queue_release(self.user, self.agent, self.tid, claimed["id"], "dispatch-error")
        self.assertEqual(released["state"], "dispatched")
        self.assertEqual(released["runId"], run["id"])
        self.assertTrue(self.get()["paused"])
        self.assertEqual(self.get()["items"], [])

    def test_scope_account_user_agent_thread_isolation(self):
        item = self.enqueue()["item"]
        other = self.db.create_thread(self.user, self.agent, "synthetic-source")
        for user, agent in (("other-friend", self.agent), (self.user, "builtin:empathy")):
            for action in (
                lambda: self.db.queue_get(user, agent, self.tid),
                lambda: self.db.queue_enqueue(user, agent, self.tid, "文本", "different-request"),
                lambda: self.db.queue_edit(user, agent, self.tid, item["id"], "文本", item["version"]),
                lambda: self.db.queue_remove(user, agent, self.tid, item["id"], item["version"]),
                lambda: self.db.queue_claim(user, agent, self.tid),
                lambda: self.db.queue_set_paused(user, agent, self.tid, True),
                lambda: self.db.queue_find_request(user, agent, self.tid, "synthetic-request-1", "合成排队问题"),
            ):
                self.assert_code("thread-unknown", action)
        self.assert_code("queue-item-unknown", lambda: self.db.queue_edit(self.user, self.agent, other["id"], item["id"], "文本", item["version"]))
        self.assert_code("request-conflict", lambda: self.db.queue_enqueue(self.user, self.agent, other["id"], "合成排队问题", "synthetic-request-1"))
        another_account = AdvisorDatabase(self.root, "synthetic-second-account")
        self.addCleanup(another_account.close)
        self.assert_code("thread-unknown", lambda: another_account.queue_get(self.user, self.agent, self.tid))
        self.assertEqual(self.get()["items"], [item])

    def test_pending_queue_prevents_new_session_reusing_an_empty_thread(self):
        self.enqueue()
        created = self.db.create_thread(self.user, self.agent, "synthetic-source", select=True)
        self.assertNotEqual(created["id"], self.tid)

    def test_delete_thread_removes_payload_and_blocks_retransmission_into_new_thread(self):
        first = self.enqueue()["item"]
        removed = self.enqueue("synthetic-request-2", "已移除问题")["item"]
        self.db.queue_remove(self.user, self.agent, self.tid, removed["id"], removed["version"])
        op = self.db.session_delete_begin(self.user, self.agent, self.tid, "synthetic-delete")
        self.db.session_delete_finish(op["id"])
        connection = self.db._connect()
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM session_queue_items").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM session_queue_state").fetchone()[0], 0)
        new_thread = self.db.create_thread(self.user, self.agent, "synthetic-source")
        for item in (first, removed):
            for request in (item["requestId"], item["dispatchRequestId"]):
                self.assert_code("thread-unknown", lambda: self.db.queue_enqueue(self.user, self.agent, new_thread["id"], "文本", request))

    def test_claim_blocks_thread_delete_until_resolved(self):
        self.enqueue()
        claimed = self.claim()
        self.assert_code("run-active", lambda: self.db.session_delete_begin(self.user, self.agent, self.tid, "synthetic-delete"))
        self.db.queue_release(self.user, self.agent, self.tid, claimed["id"], "manual")
        self.db.session_delete_begin(self.user, self.agent, self.tid, "synthetic-delete")

    def test_two_concurrent_claims_cannot_dispatch_two_messages(self):
        first = self.enqueue()["item"]
        self.enqueue("synthetic-request-2", "第二条")
        barrier = threading.Barrier(3)
        results = []
        def claim():
            barrier.wait()
            results.append(self.claim())
        workers = [threading.Thread(target=claim) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        for worker in workers:
            worker.join(5)
            self.assertFalse(worker.is_alive())
        actual = [result for result in results if result]
        self.assertEqual(len(actual), 1)
        self.assertEqual(actual[0]["id"], first["id"])

    def test_fifo_uses_acceptance_order_when_clock_moves_backwards(self):
        with patch("queue_store._now", return_value=2000):
            first = self.enqueue()["item"]
        with patch("queue_store._now", return_value=1000):
            second = self.enqueue("synthetic-request-2", "第二条")["item"]
        self.assertEqual([item["id"] for item in self.get()["items"]], [first["id"], second["id"]])
        self.assertEqual(self.claim()["id"], first["id"])

    def test_edit_or_remove_racing_dispatch_never_loses_a_successful_edit(self):
        for remove in (False, True):
            for attempt in range(5):
                thread = self.db.create_thread(self.user, self.agent, "synthetic-source")
                tid = thread["id"]
                item = self.db.queue_enqueue(self.user, self.agent, tid, "原始合成消息", "race-%s-%s" % (remove, attempt))["item"]
                gate, results = threading.Barrier(3), {}
                def claim():
                    gate.wait()
                    results["claim"] = self.db.queue_claim(self.user, self.agent, tid)
                def change():
                    gate.wait()
                    try:
                        if remove:
                            results["change"] = self.db.queue_remove(self.user, self.agent, tid, item["id"], item["version"])
                        else:
                            results["change"] = self.db.queue_edit(self.user, self.agent, tid, item["id"], "编辑成功的合成消息", item["version"])
                    except AdvisorError as exc:
                        results["error"] = exc.code
                workers = [threading.Thread(target=claim), threading.Thread(target=change)]
                for worker in workers:
                    worker.start()
                gate.wait()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                if "error" in results:
                    self.assertEqual(results["error"], "queue-item-started")
                    self.assertEqual(results["claim"]["message"], "原始合成消息")
                elif remove:
                    self.assertIsNone(results["claim"])
                else:
                    self.assertEqual(results["claim"]["message"], "编辑成功的合成消息")

    def test_schema_v8_upgrade_backups_history_and_drafts_once(self):
        self.db.append_message(self.tid, "user", "原始合成问题")
        self.db.session_save_draft(self.user, self.agent, self.tid, "原始合成草稿")
        path = self.db.path
        self.db.close()
        connection = sqlite3.connect(path)
        connection.execute("DROP TABLE session_queue_items")
        connection.execute("DROP TABLE session_queue_state")
        connection.execute("PRAGMA user_version=8")
        connection.commit()
        connection.close()
        self.db = AdvisorDatabase(self.root, "synthetic-queue-account")
        self.assertEqual(self.db.thread_messages(self.tid)[0]["text"], "原始合成问题")
        self.assertEqual(self.db.session_draft(self.user, self.agent, self.tid)["text"], "原始合成草稿")
        self.assertEqual(self.db._connect().execute("PRAGMA user_version").fetchone()[0], 11)
        backup = path.with_name("advisor.v8-backup.sqlite3")
        self.assertTrue(backup.exists())
        saved = backup.read_bytes()
        copy = sqlite3.connect(backup)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("PRAGMA user_version").fetchone()[0], 8)
        self.assertIsNone(copy.execute("SELECT name FROM sqlite_master WHERE name='session_queue_items'").fetchone())
        copy.close()
        self.reopen()
        self.assertEqual(backup.read_bytes(), saved)

    def test_invalid_queue_payloads_do_not_create_state(self):
        for request, text, kwargs in ((None, "问题", {}), ("bad id", "问题", {}), ("ok", "", {}),
                                      ("ok", "问题", {"thinking": "yes"}), ("ok", "问题", {"mentions": [3]})):
            self.assert_code("invalid-request", lambda: self.enqueue(request, text, **kwargs))
        self.assertEqual(self.get()["version"], 0)

    def test_direct_run_request_cannot_be_reenqueued(self):
        self.db.create_run(self.thread, "原始直接问题", "synthetic-direct-request", 1, 1, "synthetic-source")
        self.assert_code("request-conflict", lambda: self.enqueue("synthetic-direct-request", "原始直接问题"))

    def test_removed_queue_acceptance_cannot_be_replayed_as_a_direct_run(self):
        item = self.enqueue()["item"]
        self.db.queue_remove(self.user, self.agent, self.tid, item["id"], item["version"])
        self.assert_code("request-conflict", lambda: self.db.create_run(self.thread, "合成排队问题", item["requestId"], 1, 1, "synthetic-source"))
        self.assertEqual(self.db.thread_messages(self.tid), [])

    def test_bad_item_identifiers_and_versions_return_domain_errors(self):
        item = self.enqueue()["item"]
        for identifier in ({}, [], None, ""):
            self.assert_code("invalid-request", lambda: self.db.queue_edit(self.user, self.agent, self.tid, identifier, "编辑", item["version"]))
            self.assert_code("invalid-request", lambda: self.db.queue_remove(self.user, self.agent, self.tid, identifier, item["version"]))
        for version in (True, -1, None, "1", 1.5):
            self.assert_code("invalid-request", lambda: self.db.queue_edit(self.user, self.agent, self.tid, item["id"], "编辑", version))


if __name__ == "__main__":
    unittest.main()
