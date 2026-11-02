"""Synthetic-only regression tests for persistent assistant session ownership."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from contracts import AdvisorError
from session_store import SCHEMA_VERSION
from store import AdvisorDatabase, _long_path
from support import TestStores


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stores = TestStores(self.root)
        self.addCleanup(self.stores.close)
        self.db = self.stores.account("synthetic-session-account")
        self.user, self.agent = "synthetic-person", "builtin:advisor"
        self.thread = self.db.create_thread(self.user, self.agent, "source", agent_name="合成助手", select=True)
        self.tid = self.thread["id"]

    def reopen(self):
        self.stores.release_account("synthetic-session-account")
        self.db = self.stores.account("synthetic-session-account")
        return self.db

    def make_run(self, request="req-synthetic", message="合成问题", thread=None):
        return self.db.create_run(thread or self.db.thread(self.tid), message, request, 1, 1, "source")

    def complete(self, request="req-synthetic", native=None):
        run, message = self.make_run(request)
        self.db.finish_run(run["id"], self.tid, "done", assistant_text="合成答案", runtime_session_id=native)
        return run, message

    def test_v7_upgrade_backed_up_before_schema_changes_and_not_repeated(self):
        self.complete()
        self.db._connect().execute("CREATE TABLE unrelated_legacy_data (value TEXT)")
        self.db._connect().execute("INSERT INTO unrelated_legacy_data VALUES (?)", ("合成偏好",))
        self.db._connect().commit()
        path = self.db.path
        self.stores.release_account("synthetic-session-account")
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version=7")
        connection.execute("DROP TABLE session_usage")
        connection.execute("ALTER TABLE threads DROP COLUMN title")
        connection.commit()
        connection.close()
        self.db = self.stores.account("synthetic-session-account")
        self.assertEqual(len(self.db.thread_messages(self.tid)), 2)
        self.assertEqual(self.db.thread(self.tid)["title"], "合成问题")
        self.assertEqual(self.db._connect().execute("SELECT value FROM unrelated_legacy_data").fetchone()[0], "合成偏好")
        backup = path.with_name("advisor.v7-backup.sqlite3")
        self.assertTrue(backup.exists())
        copy = sqlite3.connect(backup)
        try:
            self.assertEqual(copy.execute("PRAGMA user_version").fetchone()[0], 7)
            self.assertEqual(copy.execute("SELECT count(*) FROM messages").fetchone()[0], 2)
            self.assertIsNone(copy.execute("SELECT name FROM sqlite_master WHERE name='session_usage'").fetchone())
        finally:
            copy.close()
        before = backup.read_bytes()
        self.reopen().thread(self.tid)
        self.assertEqual(backup.read_bytes(), before)
        self.assertEqual(self.db._connect().execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_v9_upgrade_backs_up_before_adding_replacement_columns(self):
        self.complete()
        self.db.session_save_draft(self.user, self.agent, self.tid, "合成草稿")
        self.db.queue_enqueue(self.user, self.agent, self.tid, "合成排队", "v9-queue")
        path = self.db.path
        self.stores.release_account("synthetic-session-account")
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE threads DROP COLUMN pending_replacement")
            connection.execute("ALTER TABLE runs DROP COLUMN superseded_by")
            connection.execute("ALTER TABLE messages DROP COLUMN superseded_by")
            connection.execute("PRAGMA user_version=9")
        connection.close()
        self.db = self.stores.account("synthetic-session-account")
        messages = self.db.thread_messages(self.tid)
        self.assertEqual([item["text"] for item in messages], ["合成问题", "合成答案"])
        self.assertTrue(all(item["superseded_by"] is None for item in messages))
        self.assertIsNone(self.db.thread(self.tid)["pending_replacement"])
        self.assertEqual(self.db.session_draft(self.user, self.agent, self.tid)["text"], "合成草稿")
        self.assertEqual(self.db.queue_get(self.user, self.agent, self.tid)["items"][0]["message"], "合成排队")
        backup = path.with_name("advisor.v9-backup.sqlite3")
        self.assertTrue(backup.exists())
        with sqlite3.connect(backup) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 9)
            self.assertNotIn("superseded_by", {row[1] for row in connection.execute("PRAGMA table_info(messages)")})
        connection.close()
        self.assertEqual(self.db._connect().execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_v10_upgrade_preserves_draft_and_adds_empty_identity_metadata_after_backup(self):
        self.db.session_save_draft(self.user, self.agent, self.tid, "@同名好友 原有草稿", 42)
        path = self.db.path
        self.stores.release_account("synthetic-session-account")
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE session_drafts DROP COLUMN mentions_json")
            connection.execute("PRAGMA user_version=10")
        connection.close()
        self.db = self.stores.account("synthetic-session-account")
        draft = self.db.session_draft(self.user, self.agent, self.tid)
        self.assertEqual((draft["text"], draft["scroll_top"], draft["version"], draft["mentions"]),
                         ("@同名好友 原有草稿", 42, 1, []))
        backup = path.with_name("advisor.v10-backup.sqlite3")
        self.assertTrue(backup.exists())
        saved = backup.read_bytes()
        with sqlite3.connect(backup) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 10)
            self.assertNotIn("mentions_json", {row[1] for row in connection.execute("PRAGMA table_info(session_drafts)")})
        connection.close()
        self.reopen().session_draft(self.user, self.agent, self.tid)
        self.assertEqual(backup.read_bytes(), saved)

    def test_create_is_idempotent_and_reuses_only_empty_selected_session(self):
        empty = self.db.create_thread(self.user, self.agent, "source", request_id="new-1", select=True)
        self.assertEqual(empty["id"], self.tid)
        self.db.session_save_draft(self.user, self.agent, self.tid, "保存的草稿")
        created = self.db.create_thread(self.user, self.agent, "source", request_id="new-2", select=True)
        self.assertNotEqual(created["id"], self.tid)
        again = self.db.create_thread(self.user, self.agent, "source", request_id="new-2", select=True)
        self.assertEqual(again["id"], created["id"])
        self.assertEqual(self.db.session_draft(self.user, self.agent, self.tid)["text"], "保存的草稿")

    def test_selection_survives_background_updates_and_reopen(self):
        other = self.db.create_thread(self.user, self.agent, "source")
        self.db.session_select(self.user, self.agent, self.tid)
        self.db.append_message(other["id"], "assistant", "后台结果")
        self.assertEqual(self.reopen().session_selected(self.user, self.agent)["id"], self.tid)

    def test_cross_scope_access_is_rejected_for_reads_writes_and_operations(self):
        for action in (
            lambda: self.db.session_thread("other-user", self.agent, self.tid),
            lambda: self.db.session_select(self.user, "other-agent", self.tid),
            lambda: self.db.session_draft("other-user", self.agent, self.tid),
            lambda: self.db.session_save_draft(self.user, "other-agent", self.tid, "x"),
            lambda: self.db.session_messages("other-user", self.agent, self.tid),
            lambda: self.db.session_delete_begin("other-user", self.agent, self.tid, "del"),
            lambda: self.db.session_operation_begin(self.user, "other-agent", self.tid, "compact", "cmp"),
        ):
            with self.assertRaises(AdvisorError):
                action()
        self.assertEqual(self.db.session_threads("other-user"), [])

    def test_draft_compare_and_swap_keeps_newer_edit_and_scroll(self):
        first = self.db.session_save_draft(self.user, self.agent, self.tid, "第一版", 120, 0)
        second = self.db.session_save_draft(self.user, self.agent, self.tid, "第二版", 240, first["version"])
        with self.assertRaises(AdvisorError):
            self.db.session_save_draft(self.user, self.agent, self.tid, "迟到第一版", 0, first["version"])
        actual = self.reopen().session_draft(self.user, self.agent, self.tid)
        self.assertEqual((actual["text"], actual["scroll_top"], actual["version"]), ("第二版", 240, second["version"]))

    def test_draft_concurrent_cas_has_one_winner(self):
        outcomes = []
        barrier = threading.Barrier(2)
        def write(value):
            barrier.wait()
            try:
                self.db.session_save_draft(self.user, self.agent, self.tid, value, expected_version=0)
                outcomes.append("ok")
            except AdvisorError as exc:
                outcomes.append(exc.code)
        workers = [threading.Thread(target=write, args=(value,)) for value in ("A", "B")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(3)
        self.assertCountEqual(outcomes, ["ok", "request-conflict"])

    def test_draft_mentions_survive_reopen_and_follow_the_same_cas_as_text(self):
        selected = [{"user": "synthetic-person-2", "name": "同名好友"},
                    {"user": "synthetic-person-3", "name": "同名好友"}]
        saved = self.db.session_save_draft(self.user, self.agent, self.tid, "@同名好友 问题", mentions=selected)
        actual = self.reopen().session_draft(self.user, self.agent, self.tid)
        self.assertEqual(actual["mentions"], selected)
        with self.assertRaises(AdvisorError):
            self.db.session_save_draft(self.user, self.agent, self.tid, "迟到草稿", mentions=[])
        self.assertEqual(self.db.session_draft(self.user, self.agent, self.tid)["mentions"], selected)
        cleared = self.db.session_save_draft(self.user, self.agent, self.tid, "", expected_version=saved["version"], mentions=[])
        self.assertEqual((cleared["text"], cleared["mentions"]), ("", []))

    def test_messages_page_by_stable_sequence_despite_new_append(self):
        for number in range(5):
            self.db.append_message(self.tid, "user", "合成%d" % number)
        page = self.db.session_messages(self.user, self.agent, self.tid, limit=2)
        self.assertEqual([row["seq"] for row in page["messages"]], [4, 5])
        self.db.append_message(self.tid, "assistant", "新到内容")
        older = self.db.session_messages(self.user, self.agent, self.tid, before_seq=page["beforeSeq"], limit=2)
        self.assertEqual([row["seq"] for row in older["messages"]], [2, 3])
        last = self.db.session_messages(self.user, self.agent, self.tid, before_seq=older["beforeSeq"], limit=2)
        self.assertEqual([row["seq"] for row in last["messages"]], [1])
        self.assertFalse(last["hasMore"])

    def test_first_question_title_and_historical_assistant_name(self):
        run, _ = self.make_run(message="合成标题\n第二行")
        row = self.db.thread(self.tid)
        self.assertEqual(row["title"], "合成标题 第二行")
        self.assertEqual(row["agent_name"], "合成助手")
        renamed = self.db.session_rename(self.user, self.agent, self.tid, "自己改名", row["session_version"])
        with self.assertRaises(AdvisorError):
            self.db.session_rename(self.user, self.agent, self.tid, "迟到改名", row["session_version"])
        self.db.finish_run(run["id"], self.tid, "done", assistant_text="结果")
        self.assertEqual(self.db.thread(self.tid)["title"], renamed["title"])

    def test_usage_is_per_run_and_not_cumulative(self):
        run, _ = self.complete()
        self.db.session_usage_save(self.tid, run["id"], {"inputTokens": 100, "modelFp": "m1"})
        self.db.session_usage_save(self.tid, run["id"], {"inputTokens": 110, "modelFp": "m1"})
        self.assertEqual(self.db.session_latest_usage(self.tid)["payload"]["inputTokens"], 110)
        other = self.db.create_thread("someone-else", self.agent, "source")
        with self.assertRaises(AdvisorError):
            self.db.session_usage_save(other["id"], run["id"], {"inputTokens": 123})
        self.assertEqual(len(self.db._rows("SELECT * FROM session_usage")), 1)

    def test_operation_is_deduplicated_excludes_answers_and_is_versioned(self):
        operation = self.db.session_operation_begin(self.user, self.agent, self.tid, "compact", "compact-1")
        self.assertEqual(operation["user"], self.user)
        again = self.db.session_operation_begin(self.user, self.agent, self.tid, "compact", "compact-1")
        self.assertEqual(again["id"], operation["id"])
        with self.assertRaises(AdvisorError):
            self.make_run()
        changed = self.db.session_operation_update(operation["id"], checkpoint={"stage": 1}, expected_version=operation["version"])
        with self.assertRaises(AdvisorError):
            self.db.session_operation_update(operation["id"], state="completed", expected_version=operation["version"])
        final = self.db.session_operation_update(operation["id"], state="completed", result={"summary": "合成摘要"}, expected_version=changed["version"])
        self.assertEqual(final["state"], "completed")
        self.assertIsNone(self.db.session_active_operation(self.tid))

    def test_automatic_compact_can_belong_to_current_run_but_not_other_run(self):
        run, _ = self.make_run()
        with self.assertRaises(AdvisorError):
            self.db.session_operation_begin(self.user, self.agent, self.tid, "compact", "manual")
        operation = self.db.session_operation_begin(self.user, self.agent, self.tid, "compact", "auto", metadata={"automatic": True}, run_id=run["id"])
        self.assertEqual(operation["run_id"], run["id"])

    def test_restart_preserves_uncertain_compaction_and_blocks_new_work(self):
        operation = self.db.session_operation_begin(self.user, self.agent, self.tid, "compact", "compact-1")
        self.db.session_operation_update(operation["id"], checkpoint={"native": "synthetic-native"})
        self.reopen()
        current = self.db.session_operation(operation["id"])
        self.assertEqual(current["state"], "unknown")
        self.assertEqual(current["checkpoint"], {"native": "synthetic-native"})
        with self.assertRaises(AdvisorError):
            self.make_run()
        with self.assertRaises(AdvisorError):
            self.db.session_delete_begin(self.user, self.agent, self.tid, "delete")

    def test_native_registry_rejects_cross_thread_reuse_and_keeps_all_generations(self):
        metadata = {"source_id": "source", "model_fp": "one", "context_epoch": "epoch", "profile_stamp": "p1-a1", "native_source_id": "scope-one"}
        self.db.session_bind_commit(self.tid, "native-one", metadata)
        self.db.session_bind_commit(self.tid, "native-two", {**metadata, "profile_stamp": "p2-a1"})
        other = self.db.create_thread("else", self.agent, "source")
        with self.assertRaises(AdvisorError):
            self.db.session_native_register(other["id"], "native-one")
        self.assertEqual(len(self.reopen().session_native_sessions(self.tid)), 2)
        self.assertEqual(self.db.session_native_sessions(self.tid)[0]["metadata"]["native_source_id"], "scope-one")

    def test_restart_interrupts_rebuild_without_losing_question_or_previous_binding(self):
        self.db.session_bind_commit(self.tid, "native-before-rebuild", {"profile_stamp": "p1-a0"})
        run, message = self.make_run()
        operation = self.db.session_operation_begin(self.user, self.agent, self.tid, "rebuild", "rebuild-started", run_id=run["id"])
        self.reopen()
        self.assertEqual(self.db.session_operation(operation["id"])["state"], "interrupted")
        self.assertIsNone(self.db.session_active_operation(self.tid))
        self.assertEqual(self.db.thread(self.tid)["runtime_session_id"], "native-before-rebuild")
        retried, reused = self.db.session_retry_run(run["id"], "retry-interrupted-rebuild")
        self.assertEqual(reused["id"], message["id"])
        self.assertEqual(retried["state"], "running")

    def test_model_change_plans_reuse_profile_change_plans_rebuild_without_destroying_old_binding(self):
        initial = {"source_id": "source", "model_fp": "one", "template_revision": 1, "context_epoch": "epoch", "profile_stamp": "p1-a1"}
        self.db.session_bind_commit(self.tid, "native-one", initial)
        planned = self.db.bind_runtime(self.tid, "new-model-source", "two", 1, "epoch", "p1-a1")
        self.assertFalse(planned["requiresRebuild"])
        self.assertEqual(planned["runtime_session_id"], "native-one")
        changed = self.db.bind_runtime(self.tid, "new-model-source", "two", 1, "epoch", "p2-a1")
        self.assertTrue(changed["requiresRebuild"])
        self.assertIsNone(changed["runtime_session_id"])
        persisted = self.db.thread(self.tid)
        self.assertEqual((persisted["runtime_session_id"], persisted["profile_stamp"]), ("native-one", "p1-a1"))

    def test_recovery_requires_real_boundary_and_preserves_provenance(self):
        run, message = self.complete()
        metadata = {"source_id": "source", "context_epoch": "e1", "profile_stamp": "p1-a1"}
        op = self.db.session_operation_begin(self.user, self.agent, self.tid, "rebuild", "rebuild-1")
        with self.assertRaises(AdvisorError):
            self.db.session_recovery_save(self.tid, "摘要", 500, metadata, op["id"])
        saved = self.db.session_recovery_save(self.tid, "摘要", message["seq"], metadata, op["id"])
        self.assertEqual(saved["metadata"]["boundary_message_id"], message["id"])
        self.assertEqual(self.db.session_recovery_save(self.tid, "摘要", message["seq"], metadata, op["id"])["id"], saved["id"])
        with self.assertRaises(AdvisorError):
            self.db.session_recovery_save(self.tid, "不同摘要", message["seq"], metadata, op["id"])

    def test_run_metadata_and_retry_do_not_duplicate_question_or_reuse_old_run_identity(self):
        run, message = self.make_run()
        metadata = self.db.session_run_metadata(run["id"], profile_stamp="p1-a0", context_epoch="e1")
        self.assertEqual(metadata["profile_stamp"], "p1-a0")
        self.db.finish_run(run["id"], self.tid, "error", error="合成失败")
        retried, reused_message = self.db.session_retry_run(run["id"], "req-retry")
        self.assertNotEqual(retried["id"], run["id"])
        self.assertEqual(reused_message["id"], message["id"])
        self.assertEqual(self.db.session_run_metadata(retried["id"]), metadata)
        duplicate, extra = self.db.session_retry_run(run["id"], "req-retry")
        self.assertEqual(duplicate["id"], retried["id"])
        self.assertIsNone(extra)
        self.assertEqual(len([row for row in self.db.thread_messages(self.tid) if row["role"] == "user"]), 1)
        self.assertEqual(self.db.finish_run(run["id"], self.tid, "done", assistant_text="迟到旧结果"), [])

    def test_latest_run_uses_insertion_order_when_timestamps_match(self):
        old, _ = self.make_run()
        self.db.finish_run(old["id"], self.tid, "error")
        retried, _ = self.db.session_retry_run(old["id"], "retry-same-timestamp")
        with self.db.lock, self.db._connect() as connection:
            connection.execute("UPDATE runs SET created_at_ms=100 WHERE thread_id=?", (self.tid,))
        self.assertEqual(self.db.latest_run(self.tid)["id"], retried["id"])

    def test_retry_keeps_each_reply_version_and_cannot_repeat_completed_answer(self):
        first, question = self.make_run()
        self.db.session_run_metadata(first["id"], profile_stamp="p1-a0")
        self.db.finish_run(first["id"], self.tid, "error", assistant_text="旧版本部分答案", assistant_status="error", status_text="失败")
        retry, _ = self.db.session_retry_run(first["id"], "retry-new-profile")
        self.db.session_run_metadata(retry["id"], profile_stamp="p2-a0")
        self.db.finish_run(retry["id"], self.tid, "done", assistant_text="新版本完成答案")
        replies = [row for row in self.db.thread_messages(self.tid) if row["role"] == "assistant"]
        self.assertEqual([row["run_id"] for row in replies], [first["id"], retry["id"]])
        self.assertEqual([self.db.session_run_metadata(row["run_id"])["profile_stamp"] for row in replies], ["p1-a0", "p2-a0"])
        with self.assertRaises(AdvisorError):
            self.db.session_retry_run(first["id"], "repeat-already-answered")
        duplicate, message = self.db.session_retry_run(first["id"], "retry-new-profile")
        self.assertEqual(duplicate["id"], retry["id"])
        self.assertIsNone(message)
        self.assertEqual(self.db.thread_messages(self.tid)[0]["run_id"], first["id"])

    def test_delete_busy_rejected_and_retry_cleanup_cannot_revive_records(self):
        run, _ = self.make_run()
        with self.assertRaises(AdvisorError):
            self.db.session_delete_begin(self.user, self.agent, self.tid, "delete-1")
        self.db.finish_run(run["id"], self.tid, "done", assistant_text="结果", runtime_session_id="native-one")
        self.db.session_save_draft(self.user, self.agent, self.tid, "草稿")
        self.db.session_usage_save(self.tid, run["id"], {"inputTokens": 7})
        op = self.db.session_delete_begin(self.user, self.agent, self.tid, "delete-1")
        self.assertEqual(len(op["nativeSessions"]), 1)
        failed = self.db.session_delete_finish(op["id"])
        self.assertEqual(failed["state"], "failed")
        for action in (
            lambda: self.make_run("another"),
            lambda: self.db.append_message(self.tid, "assistant", "迟到答案"),
            lambda: self.db.session_save_draft(self.user, self.agent, self.tid, "迟到草稿", expected_version=1),
            lambda: self.db.session_native_register(self.tid, "rogue-native"),
        ):
            with self.assertRaises(AdvisorError):
                action()
        self.reopen()
        retry = self.db.session_delete_begin(self.user, self.agent, self.tid, "delete-2")
        self.assertEqual(retry["id"], op["id"])
        self.db.session_native_deleted(self.tid, "native-one")
        self.assertEqual(self.db.session_delete_finish(op["id"])["state"], "completed")
        self.assertEqual(self.db.session_delete_finish(op["id"])["state"], "completed")
        self.assertEqual(self.db.thread(self.tid)["delete_state"], "deleted")
        self.assertEqual(self.db.thread(self.tid)["title"], "")
        self.assertEqual(self.db.session_threads(self.user), [])
        self.assertEqual(self.db.thread_messages(self.tid), [])
        self.assertIsNone(self.db.session_latest_usage(self.tid))
        with self.assertRaises(AdvisorError):
            self.db.scoped_request(self.user, self.agent, run["request_id"], "合成问题")
        self.assertIsNone(self.db.latest_thread(self.user, self.agent))

    def test_delete_switches_to_most_recent_session_and_preserves_unrelated_migrated_tables(self):
        second = self.db.create_thread(self.user, self.agent, "source")
        third = self.db.create_thread(self.user, self.agent, "source")
        self.db.append_message(second["id"], "assistant", "最近活动")
        with self.db.lock, self.db._connect() as connection:
            connection.execute("UPDATE threads SET updated_at_ms=9999999999999 WHERE id=?", (second["id"],))
        self.db._connect().execute("CREATE TABLE unrelated_legacy_data (value TEXT)")
        self.db._connect().execute("INSERT INTO unrelated_legacy_data VALUES (?)", ("喜爱合成例子",))
        self.db._connect().commit()
        self.db.session_select(self.user, self.agent, self.tid)
        op = self.db.session_delete_begin(self.user, self.agent, self.tid, "delete")
        self.assertEqual(self.db.session_selected(self.user, self.agent)["id"], second["id"])
        self.db.session_delete_finish(op["id"])
        self.assertEqual(self.db._connect().execute("SELECT value FROM unrelated_legacy_data").fetchone()[0], "喜爱合成例子")
        self.assertEqual(len(self.db.session_threads(self.user)), 2)

    def test_deleted_create_nonce_does_not_create_replacement(self):
        self.db.create_thread(self.user, self.agent, "source", request_id="create-once", select=True)
        operation = self.db.session_delete_begin(self.user, self.agent, self.tid, "delete")
        self.db.session_delete_finish(operation["id"])
        with self.assertRaises(AdvisorError):
            self.db.create_thread(self.user, self.agent, "source", request_id="create-once", select=True)


# Removes an account folder in a child process behaving like a default Windows install (LongPathsEnabled=0, as in
# bridge/test_core_account_cleanup.py): it clears its own PEB.IsLongPathAwareProcess flag. The engine's npm cache
# nests past MAX_PATH, so plain paths fail there as they do for most users.
SHORT_PATH_REMOVE = r'''
import ctypes, json, sys
sys.path.insert(0, sys.argv[1])
ntdll = ctypes.WinDLL("ntdll")
class Basic(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("peb", ctypes.c_void_p), ("rest", ctypes.c_void_p * 4)]
basic = Basic()
ntdll.NtQueryInformationProcess(ctypes.c_void_p(-1), 0, ctypes.byref(basic), ctypes.sizeof(basic), None)
flags = ctypes.c_ubyte.from_address(basic.peb + 3)
flags.value &= ~0x80
ntdll.RtlAreLongPathsEnabled.restype = ctypes.c_ubyte
from store import AdvisorDatabase
result = {"shortPaths": not ntdll.RtlAreLongPathsEnabled()}
try:
    result["removed"] = AdvisorDatabase(sys.argv[2], sys.argv[3]).remove()["deleted"]
except OSError as exc:
    result["removed"] = type(exc).__name__
print(json.dumps(result))
'''


@unittest.skipUnless(os.name == "nt", "Windows MAX_PATH behaviour")
class LongPathRemovalTests(unittest.TestCase):
    def test_account_folder_nesting_past_max_path_is_removed_without_long_path_support(self):
        account = "synthetic-long-path-account"
        with tempfile.TemporaryDirectory() as temporary:
            database = AdvisorDatabase(temporary, account)
            database._connect()
            database.close()
            deep = Path(database.directory) / "engine" / "home" / "AppData" / "Local" / "npm-cache" / ("x" * 120) / ("y" * 120)
            os.makedirs(_long_path(deep))
            with open(_long_path(deep / "content.bin"), "wb") as handle:
                handle.write(b"synthetic")
            self.assertGreater(len(str(deep / "content.bin")), 260)
            backend = Path(__file__).resolve().parents[1] / "backend"
            result = subprocess.run([sys.executable, "-c", SHORT_PATH_REMOVE, str(backend), temporary, account],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr[-400:])
            outcome = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertTrue(outcome["shortPaths"], "the child must run without long-path support")
            self.assertIs(outcome["removed"], True)
            self.assertFalse(os.path.lexists(_long_path(database.directory)))


if __name__ == "__main__":
    unittest.main()
