"""Synthetic per-question supplied-context snapshots, never model read provenance."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from contracts import AdvisorError
from kyf_plugin import CoreError
import context_snapshot
from support import FakeModelStore, FakeSource, build_service, wait_until
from test_replace import NumberedRuntime

A, U, B, C, G = "synthetic-snapshot-account", "friend-a", "friend-b", "friend-c", "builtin:advisor"


class SnapshotRuntime(NumberedRuntime):
    def __init__(self):
        super().__init__()
        self.materials = []

    def respond(self, config, request, on_event, cancel_event):
        self.materials.append({path: Path(path).read_text(encoding="utf-8") for path in request.get("participantFiles", [])})
        return super().respond(config, request, on_event, cancel_event)


class ContextSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = FakeSource(account=A)
        self.source.seed_pages(U, 3)
        self.source.seed_pages(B, 2)
        self.source.seed_pages(C, 1)
        self.source.friend_list = [{"user": U, "name": "当前好友", "isGroup": False},
                                   {"user": B, "name": "同名好友", "isGroup": False},
                                   {"user": C, "name": "同名好友", "isGroup": False}]
        self.source.snapshot["data"]["goal"] = "合成联系目标"
        self.runtime = SnapshotRuntime()
        self.service = build_service(self.source, FakeModelStore(context_tokens=32768), self.root,
                                     runtime_factory=lambda: self.runtime)
        self.service.engine.participant_root = lambda account: (self.service.stores.directory(account) /
                                                               "engine" / "engine" / "workspace" / "participants")
        self.addCleanup(self.service.shutdown)
        self.thread_id = self.service.thread(A, U, G)["thread"]["id"]

    def settle(self, started):
        result = wait_until(lambda: (value if value["run"]["state"] != "running" else None)
                            if (value := self.service.events(A, U, started["run"]["id"])) else None)
        self.assertIsNotNone(result)
        self.service._runs[started["run"]["id"]].worker.join(3)
        return self.service.events(A, U, started["run"]["id"])

    def ask(self, text="合成问题", request="snapshot-first", **kwargs):
        return self.settle(self.service.start(A, U, G, self.thread_id, text, request, **kwargs))

    def questions(self):
        return [item for item in self.service.thread(A, U, G, self.thread_id)["thread"]["messages"] if item["role"] == "user"]

    def test_pending_admission_then_actual_sources_are_persisted_and_immutable(self):
        entered, release = threading.Event(), threading.Event()
        original = self.service._prepare_run_context
        def prepare(run):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(run)
        self.addCleanup(release.set)
        with patch.object(self.service, "_prepare_run_context", prepare):
            started = self.service.start(A, U, G, self.thread_id, "看看 @同名好友", "snapshot-pending", mentions=[B])
            self.assertTrue(entered.wait(2))
            pending = started["run"]["contextSnapshot"]
            self.assertEqual(pending["state"], "pending")
            self.assertEqual([(item["user"], item["name"], item["status"]) for item in pending["contacts"]],
                             [(U, "当前好友", "pending"), (B, "同名好友", "pending")])
            self.assertEqual(self.questions()[0]["mentions"], [B])
            release.set()
            terminal = self.settle(started)
        snapshot = terminal["thread"]["messages"][0]["contextSnapshot"]
        self.assertEqual(snapshot["state"], "recorded")
        self.assertEqual([item["sources"] for item in snapshot["contacts"]], [["chat", "profile"], ["chat", "profile"]])
        self.assertEqual([item["access"] for item in snapshot["contacts"]], ["required", "available"])
        self.assertTrue(all(item["status"] == "ready" for item in snapshot["contacts"]))
        self.source.friend_list[1]["name"] = "后来改名"
        self.service.remove_participant(A, U, G, self.thread_id, B)
        self.assertEqual(self.questions()[0]["contextSnapshot"], snapshot)
        database = self.service.stores.account(A)
        changed = copy.deepcopy(snapshot)
        changed["contacts"][1]["name"] = "不应更新"
        with self.assertRaises(AdvisorError):
            database.session_run_metadata(started["run"]["id"], context_snapshot=changed)
        self.service.stores.release_account(A)
        self.assertEqual(self.questions()[0]["contextSnapshot"], snapshot)
        encoded = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn(str(self.root), encoded)
        self.assertNotIn("read", {key for contact in snapshot["contacts"] for key in contact})

    def test_source_status_uses_rendered_material_and_distinguishes_empty_and_unavailable(self):
        self.source.snapshot["data"]["goal"] = ""
        self.source.users[C] = []
        entered, release = threading.Event(), threading.Event()
        original = self.service._prepare_run_context
        def prepare(run):
            entered.set()
            self.assertTrue(release.wait(3))
            return original(run)
        self.addCleanup(release.set)
        with patch.object(self.service, "_prepare_run_context", prepare):
            started = self.service.start(A, U, G, self.thread_id, "三位资料", "snapshot-empty", mentions=[B, C])
            self.assertTrue(entered.wait(2))
            self.source.friend_list = [item for item in self.source.friend_list if item["user"] != B]
            release.set()
            ended = self.settle(started)
        self.assertEqual(ended["run"]["state"], "done")
        contacts = ended["run"]["contextSnapshot"]["contacts"]
        self.assertEqual([(item["user"], item["sources"], item["status"]) for item in contacts],
                         [(U, ["chat"], "ready"), (B, [], "unavailable"), (C, [], "empty")])

    def test_retry_preserves_original_scope_and_old_attempt_after_failed_preparation(self):
        with patch.object(self.service, "_friend_context", side_effect=AdvisorError("context-error", "合成资料失败")):
            first = self.ask(mentions=[B])
        self.assertEqual(first["run"]["state"], "error")
        old_snapshot = first["run"]["contextSnapshot"]
        self.assertTrue(all(item["status"] == "unavailable" and item["sources"] == [] for item in old_snapshot["contacts"]))
        self.service.remove_participant(A, U, G, self.thread_id, B)
        database = self.service.stores.account(A)
        database.session_add_participants(self.thread_id, [{"user": C, "name": "新加入好友"}])
        self.source.friend_list[1]["name"] = "后来改名"
        retry = self.ask(request="snapshot-retry", retry_run_id=first["run"]["id"], mentions=[C])
        self.assertEqual(retry["run"]["state"], "done", retry)
        self.assertEqual([(item["user"], item["name"]) for item in retry["run"]["contextSnapshot"]["contacts"]],
                         [(U, "当前好友"), (B, "同名好友")])
        self.assertEqual(self.questions()[0]["mentions"], [B])
        self.assertEqual(len(self.questions()), 1)
        stored_old = database.session_run_metadata(first["run"]["id"])["context_snapshot"]
        self.assertEqual(stored_old, old_snapshot)
        self.assertIn("同名好友（friend-b）", "\n".join(self.runtime.materials[0].values()))
        self.assertNotIn("friend-c", "\n".join(self.runtime.materials[0].values()))

    def test_regenerate_inherits_scope_edit_captures_new_scope_and_preserves_old_rows(self):
        first = self.ask(mentions=[B])
        self.service.remove_participant(A, U, G, self.thread_id, B)
        self.service.stores.account(A).session_add_participants(self.thread_id, [{"user": C, "name": "同名好友"}])
        regenerated = self.ask(request="snapshot-regenerate", regenerate_run_id=first["run"]["id"])
        self.assertEqual([item["user"] for item in regenerated["run"]["contextSnapshot"]["contacts"]], [U, B])
        original_question = self.questions()[0]
        edited = self.ask(text="改问 @同名好友", request="snapshot-edit", edit_run_id=regenerated["run"]["id"], mentions=[C])
        self.assertEqual(edited["run"]["state"], "done", edited)
        questions = self.questions()
        self.assertEqual(questions[0]["contextSnapshot"], original_question["contextSnapshot"])
        self.assertEqual(questions[0]["mentions"], [B])
        self.assertEqual([item["user"] for item in questions[1]["contextSnapshot"]["contacts"]], [U, C])
        self.assertEqual(questions[1]["mentions"], [C])
        self.assertIn("supersededBy", questions[0])

    def test_queue_records_scope_when_admitted_not_while_waiting(self):
        self.service.queue_pause(A, U, G, self.thread_id)
        queued = self.service.queue_enqueue(A, U, G, self.thread_id, "排队合成问题", "snapshot-queue", mentions=[B])
        self.assertNotIn("contextSnapshot", queued["item"])
        self.assertEqual(self.questions(), [])
        self.service.stores.account(A).session_add_participants(self.thread_id, [{"user": C, "name": "同名好友"}])
        self.service.queue_resume(A, U, G, self.thread_id)
        run = wait_until(lambda: self.service.stores.account(A).latest_run(self.thread_id))
        ended = self.settle({"run": run})
        self.assertEqual(ended["run"]["state"], "done", ended)
        self.assertEqual([item["user"] for item in self.questions()[0]["contextSnapshot"]["contacts"]], [U, C, B])
        self.assertEqual(self.questions()[0]["mentions"], [B])

    def test_legacy_question_and_its_retry_remain_unknown_and_paging_keeps_metadata(self):
        database = self.service.stores.account(A)
        old, _ = database.create_run(database.thread(self.thread_id), "旧版合成问题", "snapshot-legacy", 1, 0, "source")
        database.finish_run(old["id"], self.thread_id, "error", error="合成错误")
        database.session_add_participants(self.thread_id, [{"user": B, "name": "同名好友"}])
        self.assertIsNone(self.questions()[0]["contextSnapshot"])
        retry = self.ask(text="旧版合成问题", request="snapshot-legacy-retry", retry_run_id=old["id"])
        self.assertEqual(retry["run"]["state"], "done", retry)
        self.assertIsNone(self.questions()[0]["contextSnapshot"])
        self.assertNotIn("mentions", self.questions()[0])
        self.ask(text="新版问题", request="snapshot-page", mentions=[C])
        questions = self.questions()
        page = self.service.thread(A, U, G, self.thread_id, before_seq=questions[1]["seq"] + 1)["thread"]
        self.assertIsNone(next(item for item in page["messages"] if item["id"] == questions[0]["id"])["contextSnapshot"])
        self.assertEqual(next(item for item in page["messages"] if item["id"] == questions[1]["id"])["contextSnapshot"], questions[1]["contextSnapshot"])

    def test_lost_ack_returns_frozen_scope_after_contact_removed_from_list(self):
        first = self.ask(mentions=[B])
        self.source.friend_list = [item for item in self.source.friend_list if item["user"] != B]
        duplicate = self.service.start(A, U, G, self.thread_id, "合成问题", "snapshot-first", mentions=[B])
        self.assertEqual(duplicate["run"]["id"], first["run"]["id"])
        self.assertEqual(duplicate["run"]["contextSnapshot"], first["run"]["contextSnapshot"])

    def test_roster_failure_cannot_leave_an_admitted_running_question_without_a_worker(self):
        with patch.object(self.source, "friends", side_effect=CoreError(503, "unavailable", "synthetic roster unavailable")):
            with self.assertRaises(AdvisorError):
                self.service.start(A, U, G, self.thread_id, "不应落库的问题", "snapshot-no-roster")
        database = self.service.stores.account(A)
        self.assertIsNone(database.active_run(self.thread_id))
        self.assertIsNone(database.find_run_by_request(self.thread_id, "snapshot-no-roster"))
        self.assertEqual(self.questions(), [])
        self.assertEqual(self.runtime.respond_calls, [])

    def test_account_clear_during_preparation_still_cancels_and_drains(self):
        entered = threading.Event()
        def prepare(run):
            entered.set()
            self.assertTrue(run.cancel.wait(3))
            raise AdvisorError("stopping", "合成取消")
        with patch.object(self.service, "_prepare_run_context", prepare):
            started = self.service.start(A, U, G, self.thread_id, "准备中清理", "snapshot-clear")
            self.assertTrue(entered.wait(2))
            self.service.pause_for_account_clear(A)
            self.assertTrue(self.service._runs[started["run"]["id"]].cancel.is_set())
            self.service._runs[started["run"]["id"]].worker.join(3)
            self.assertFalse(self.service._runs[started["run"]["id"]].worker.is_alive())
        self.assertEqual(self.runtime.respond_calls, [])
        self.service.resume_after_failed_account_clear()
        resumed = self.service.events(A, U, started["run"]["id"])
        self.assertEqual(resumed["run"]["state"], "stopped")
        for snapshot in (resumed["run"]["contextSnapshot"], self.questions()[0]["contextSnapshot"]):
            self.assertEqual(snapshot["state"], "recorded")
            self.assertTrue(all(item["status"] == "unavailable" and item["sources"] == [] for item in snapshot["contacts"]))
        self.service.stores.release_account(A)
        self.assertEqual(self.questions()[0]["contextSnapshot"]["state"], "recorded")

    def test_restart_finalizes_pending_snapshot_without_inventing_sources(self):
        database = self.service.stores.account(A)
        run, _ = database.create_run(database.thread(self.thread_id), "重启中断问题", "snapshot-restart", 1, 0, "source")
        pending = context_snapshot.pending(U, "当前好友", [{"user": B, "name": "同名好友"}])
        database.session_run_metadata(run["id"], context_snapshot=pending, selected_mentions=[B])
        self.service.stores.release_account(A)
        stored = self.service.stores.account(A).run(run["id"])
        self.assertEqual(stored["state"], "error")
        snapshot = self.questions()[0]["contextSnapshot"]
        self.assertEqual(snapshot["state"], "recorded")
        self.assertEqual([item["user"] for item in snapshot["contacts"]], [U, B])
        self.assertTrue(all(item["status"] == "unavailable" and item["sources"] == [] for item in snapshot["contacts"]))


if __name__ == "__main__":
    unittest.main()
