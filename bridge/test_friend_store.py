"""Synthetic 知己分析 persistence tests: memories, decisions, candidates, drafts and migration.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Modified 2026-10-13 (Apache-2.0): extraction jobs, checkpoints and analysis runs belong to the
kyf.friend-analysis plugin (tested there); candidates arrive through Core's plugin intake.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from core_account_db import AccountStores
from core_contracts import AdvisorError
from core_human import CandidateConflict, CandidateError, HumanService
from friend_store import FriendStore, normalize_text

USER = "wxid_friend_a"


def evidence(msg_id="m-1"):
    return {"sourceFp": "fp", "msgId": msg_id, "hash": "h-" + msg_id, "sortKey": "[1,\"s\",1]",
            "cursor": None, "role": "friend", "time": 1_700_000_000_000, "excerpt": "原文"}


class StoreCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stores = AccountStores(self.root)
        self.addCleanup(self.stores.close)
        self.database = self.stores.account("synthetic-account")
        self.store = FriendStore(self.database)
        self.human = HumanService(self.stores)

    def commit(self, candidates, links=(), expected_clear_epoch=None):
        """One plugin batch through Core's candidate intake; returns how many were inserted."""
        result = self.human.submit_candidates("synthetic-account", USER, "kyf.friend-analysis", {"task": "task-1"},
                                              candidates, list(links), expected_clear_epoch)
        return len(result["inserted"])

    def candidate(self, text, key, tags=("爱好",), **extra):
        return {"text": text, "tags": list(tags), "proposalKey": key, "evidence": [evidence()], **extra}


class MemoryTests(StoreCase):
    def test_user_record_edit_delete_versions_and_exclusion(self):
        created = self.store.create_user_memory(USER, "对方喜欢爬山", ["爱好"], "2024-05")
        self.assertEqual((created["status"], created["origin"], created["version"]), ("confirmed", "user", 1))
        edited = self.store.edit(USER, created["id"], "对方喜欢周末爬山", ["爱好", "经历"], None, 1)
        self.assertEqual(edited["version"], 2)
        with self.assertRaises(AdvisorError) as conflict:
            self.store.edit(USER, created["id"], "过期编辑", ["爱好"], None, 1)
        self.assertEqual(conflict.exception.code, "request-conflict")
        self.assertEqual([item["version"] for item in self.store.versions(created["id"])], [1])
        self.store.delete(USER, created["id"], 2)
        self.assertEqual(self.store.memories(USER), [])
        self.assertEqual(self.store.exclusions(USER)[0]["norm"], normalize_text("对方喜欢周末爬山"))
        with self.assertRaises(AdvisorError):
            self.store.create_user_memory(USER, "x" * 301, ["爱好"])
        with self.assertRaises(AdvisorError):
            self.store.create_user_memory(USER, "自定义标签", ["自定义"])

    def test_candidates_confirm_ignore_batch_and_conflict_choice(self):
        self.assertEqual(self.commit([self.candidate("对方不吃香菜", "k1", ("偏好与忌口",)),
                                           self.candidate("对方养了一只猫", "k2", ("基本信息",)),
                                           self.candidate("对方喜欢跑步", "k3")]), 3)
        pending = {row["text"]: row for row in self.store.memories(USER, ("pending",))}
        before = self.store.state(USER)["profile_revision"]
        self.store.decide(USER, pending["对方不吃香菜"]["id"], "confirm")
        self.assertGreater(self.store.state(USER)["profile_revision"], before)
        self.store.decide(USER, pending["对方养了一只猫"]["id"], "ignore")
        self.assertIn(normalize_text("对方养了一只猫"), [row["norm"] for row in self.store.exclusions(USER)])
        result = self.store.decide_many(USER, [pending["对方喜欢跑步"]["id"], pending["对方养了一只猫"]["id"]], "confirm")
        self.assertEqual(result, {"done": 1, "skipped": 1})
        confirmed = self.store.memories(USER, ("confirmed",))
        target = next(row for row in confirmed if row["text"] == "对方喜欢跑步")
        self.commit([self.candidate("对方现在不跑步了", "k4", relation="conflict", relatedId=target["id"])])
        conflict = next(row for row in self.store.memories(USER, ("pending",)) if row["relation"] == "conflict")
        self.assertEqual(self.store.decide_many(USER, [conflict["id"]], "confirm"), {"done": 0, "skipped": 1})
        with self.assertRaises(AdvisorError):
            self.store.decide(USER, conflict["id"], "confirm")
        self.store.decide(USER, conflict["id"], "confirm", choice="adopt")
        merged = self.store.memory(USER, target["id"])
        self.assertEqual((merged["text"], merged["version"]), ("对方现在不跑步了", 3))

    def test_supplement_merges_into_confirmed_record_with_both_evidence(self):
        original = self.store.create_user_memory(USER, "对方在上海工作", ["基本信息"])
        self.commit([self.candidate("对方在上海做设计工作", "k1", ("基本信息",), relation="supplement",
                                    relatedId=original["id"])])
        supplement = self.store.memories(USER, ("pending",))[0]
        self.store.decide(USER, supplement["id"], "confirm")
        merged = self.store.memory(USER, original["id"])
        self.assertEqual((merged["text"], merged["version"]), ("对方在上海做设计工作", 2))
        self.assertEqual(len(self.store.evidence([original["id"]])[original["id"]]), 1)
        self.assertEqual(self.store.memories(USER, ("pending",)), [])

    def test_clear_rejects_batches_read_before_it_even_when_empty(self):
        epoch = self.store.state(USER)["clear_epoch"]
        self.store.clear(USER)
        for candidates in ([self.candidate("对方喜欢游泳", "k1")], []):
            with self.assertRaises(CandidateConflict):
                self.commit(candidates, expected_clear_epoch=epoch)
        self.assertEqual(self.store.memories(USER), [])
        self.assertEqual(self.commit([self.candidate("对方喜欢游泳", "k1")],
                                     expected_clear_epoch=self.store.state(USER)["clear_epoch"]), 1)

    def test_retried_batch_is_idempotent(self):
        self.assertEqual(self.commit([self.candidate("对方喜欢游泳", "same")]), 1)
        self.assertEqual(self.commit([self.candidate("对方喜欢游泳", "same")]), 0)
        self.assertEqual(len(self.store.memories(USER)), 1)

    def test_intake_keeps_uncertain_matches_and_dedup_links_and_rejects_bad_shapes(self):
        existing = self.store.create_user_memory(USER, "对方喜欢游泳", ["爱好"])
        links = [{"proposalKey": "k2", "text": "对方爱游泳", "targetKind": "memory", "targetId": existing["id"],
                  "relation": "duplicate"}]
        self.assertEqual(self.commit([self.candidate("对方也许会冬泳", "k1", relation="uncertain")], links), 1)
        pending = self.store.memories(USER, ("pending",))[0]
        self.assertEqual((pending["relation"], pending["origin"], pending["status"]), ("uncertain", "ai", "pending"))
        self.assertEqual([(row["proposal_key"], row["target_id"]) for row in self.store.dedup_links(USER)],
                         [("k2", existing["id"])])
        for bad in ({"relation": "replace"}, {"tags": ["自定义"]}, {"proposalKey": ""}, {"evidence": [{"msgId": "m"}]}):
            with self.subTest(bad=next(iter(bad))), self.assertRaises(CandidateError):
                self.commit([{**self.candidate("对方喜欢跑步", "k3"), **bad}])


class SettingsAndDraftTests(StoreCase):
    def test_goal_and_account_profile_use_version_checks(self):
        saved = self.store.save_goal(USER, "保持联系", 0)
        self.assertEqual(saved["version"], 1)
        with self.assertRaises(AdvisorError):
            self.store.save_goal(USER, "旧窗口的目标", 0)
        profile = self.store.save_account_profile("我是设计师", "简短直接", 0)
        self.assertEqual(profile["version"], 1)
        self.assertEqual(self.store.account_revision(), 1)

    def test_late_draft_writes_never_override_or_revive(self):
        self.assertTrue(self.store.save_draft(USER, "memory:new", 1, 2, {"text": "新"})["saved"])
        self.assertFalse(self.store.save_draft(USER, "memory:new", 1, 1, {"text": "旧"})["saved"])
        self.assertEqual(self.store.drafts(USER)["drafts"][0]["payload"], {"text": "新"})
        self.store.close_draft(USER, "memory:new", 1)
        self.assertFalse(self.store.save_draft(USER, "memory:new", 1, 3, {"text": "迟到"})["saved"])
        self.assertEqual(self.store.drafts(USER)["drafts"], [])
        self.assertTrue(self.store.save_draft(USER, "memory:new", 2, 1, {"text": "下一次编辑"})["saved"])
        self.assertEqual(self.store.drafts("other-user")["drafts"], [])

    def test_observations_are_separate_from_memories(self):
        saved = self.store.save_observation(USER, None, "聊工作时回应更积极", "最近两周的观察", None, None)
        self.store.save_observation(USER, saved["id"], "聊工作时回应更快", "", "互动", 1)
        self.assertEqual(self.store.observations(USER)[0]["version"], 2)
        self.assertEqual(self.store.memories(USER), [])


class MigrationTests(unittest.TestCase):
    def test_v6_database_is_backed_up_and_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stores = AccountStores(root)
            database = stores.account("synthetic-account")
            database._connect()
            path = database.path
            stores.close()
            # Simulate the previous release: no friend tables and user_version 6.
            connection = sqlite3.connect(str(path))
            for table in ("friend_state", "memories", "memory_versions", "memory_evidence", "memory_exclusions",
                          "memory_dedup", "friend_jobs", "friend_checkpoints", "friend_measurements",
                          "friend_settings", "account_profile", "friend_observations", "friend_drafts",
                          "friend_draft_closed"):
                connection.execute("DROP TABLE IF EXISTS " + table)
            connection.execute("INSERT INTO context(user,state,revision,read_count,updated_at_ms) "
                               "VALUES('legacy','ready',3,0,1)")
            connection.execute("PRAGMA user_version=6")
            connection.commit()
            connection.close()
            reopened = AccountStores(root)
            database = reopened.account("synthetic-account")
            self.assertEqual(database.context("legacy")["revision"], 3)
            FriendStore(database).create_user_memory("legacy", "对方喜欢猫", ["爱好"])
            backup = path.with_name("advisor.v6-backup.sqlite3")
            self.assertTrue(backup.is_file())
            copy = sqlite3.connect(str(backup))
            self.assertEqual(copy.execute("PRAGMA user_version").fetchone()[0], 6)
            self.assertEqual(copy.execute("SELECT revision FROM context WHERE user='legacy'").fetchone()[0], 3)
            self.assertIsNone(copy.execute("SELECT name FROM sqlite_master WHERE name='memories'").fetchone())
            copy.close()
            reopened.close()
            final = sqlite3.connect(str(path))
            self.assertEqual(final.execute("PRAGMA user_version").fetchone()[0], 10)
            final.close()

    def test_v8_database_is_backed_up_and_keeps_memories_without_core_analysis_tables(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stores = AccountStores(root)
            database = stores.account("synthetic-account")
            FriendStore(database).create_user_memory("legacy", "对方喜欢猫", ["爱好"])
            path = database.path
            stores.close()
            # Simulate the 知己分析 v1 release: memory tables, no unified analysis tables.
            connection = sqlite3.connect(str(path))
            for table in ("friend_runs", "friend_scans", "friend_chat_blocks", "friend_persona_evidence",
                          "friend_results", "friend_step_measurements"):
                connection.execute("DROP TABLE IF EXISTS " + table)
            connection.execute("PRAGMA user_version=8")
            connection.commit()
            connection.close()
            reopened = AccountStores(root)
            database = reopened.account("synthetic-account")
            self.assertEqual([row["text"] for row in FriendStore(database).memories("legacy")], ["对方喜欢猫"])
            # Analysis tables are the kyf.friend-analysis plugin's own database now.
            self.assertIsNone(database._row("SELECT name FROM sqlite_master WHERE name='friend_runs'"))
            backup = sqlite3.connect(str(path.with_name("advisor.v8-backup.sqlite3")))
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 8)
            self.assertIsNone(backup.execute("SELECT name FROM sqlite_master WHERE name='friend_runs'").fetchone())
            backup.close()
            reopened.close()

if __name__ == "__main__":
    unittest.main()
