"""Core's per-account database: projection writes, removal and owned paths. Synthetic data only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Cases moved from test_advisor_store.py (projection/removal/path parts), 2026-10-13.
"""
import json
import tempfile
import unittest
from pathlib import Path

from core_account_db import AccountPathError, AccountStores
from core_contracts import AdvisorError


def context_row(seq, text, **extra):
    return {"seq": seq, "msg_id": "m-%d" % seq, "sort_key": "[%d, \"s\", %d]" % (seq, seq),
            "group_chat": 0, "side": "other", "sender_id": "wxid", "sender_name": "朋友",
            "kind": "text", "text": text, "time_ms": 1_700_000_000_000 + seq,
            "raw": json.dumps({"_sort": [seq, "s", seq]}), **extra}


class AccountDatabaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stores = AccountStores(self.root)
        self.addCleanup(self.stores.close)

    def test_database_keeps_its_location_and_name(self):
        database = self.stores.account("acct-a")
        database._connect()
        self.assertEqual(database.path.parent.parent, self.root / ".local" / "advisor-data")
        self.assertEqual(database.path.name, "advisor.sqlite3")
        self.assertFalse(hasattr(database, "state_dir"))

    def test_context_append_is_transactional_and_ignores_duplicate_sequences(self):
        database = self.stores.account("acct-context")
        database.context_begin("user-a", "[5, \"message__message_1.db\", 1]")
        rows = [context_row(1, "第一条"), context_row(2, "第二条")]
        inserted = database.context_append_page("user-a", rows, "[2, \"message__message_1.db\", 2]",
                                                "[2, \"message__message_1.db\", 2]")
        self.assertEqual(inserted, 2)
        row = database.context("user-a")
        self.assertEqual((row["state"], row["revision"], row["read_count"]), ("reading", 1, 2))
        inserted = database.context_append_page("user-a", rows, None, None)
        self.assertEqual(inserted, 0)
        self.assertEqual(database.context("user-a")["revision"], 1)
        database.context_finish("user-a", 2)
        finished = database.context("user-a")
        self.assertEqual((finished["state"], finished["total_count"], finished["revision"]), ("ready", 2, 1))
        messages = database.context_messages("user-a", None)
        self.assertEqual([item["text"] for item in messages], ["第一条", "第二条"])
        self.assertEqual([item["text"] for item in database.context_messages("user-a", 1)], ["第一条"])

    def test_changed_message_facts_are_rejected_and_reset_starts_a_new_epoch(self):
        database = self.stores.account("acct-facts")
        database.context_begin("user-a", None, "source-1")
        database.context_append_page("user-a", [context_row(1, "原文")], None, None)
        with self.assertRaises(AdvisorError) as changed:
            database.context_append_page("user-a", [context_row(1, "已编辑")], None, None)
        self.assertEqual(changed.exception.code, "context-error")
        with self.assertRaises(AdvisorError):
            database.context_window_matches("user-a", [context_row(1, "已编辑")])
        self.assertEqual(database.context_window_matches("user-a", [context_row(1, "原文", sender_name="改名")]),
                         {"m-1"})
        epoch = database.context("user-a")["epoch"]
        database.context_begin("user-a", None, "source-2", reset=True)
        self.assertEqual(database.context_messages("user-a", None), [])
        self.assertNotEqual(database.context("user-a")["epoch"], epoch)
        database.context_error("user-a", "读取失败")
        self.assertEqual(database.context("user-a")["error"], "读取失败")

    def test_remove_account_deletes_only_its_owned_directory(self):
        database = self.stores.account("acct-remove")
        other = self.stores.account("acct-keep")
        database.context_begin("user", None)
        other.context_begin("user", None)
        self.assertTrue(database.directory.is_dir())
        result = self.stores.remove_account("acct-remove")
        self.assertTrue(result["deleted"])
        self.assertFalse(database.directory.exists())
        self.assertTrue(other.directory.is_dir())
        self.assertEqual(self.stores.remove_account("acct-never-opened"), {"deleted": False})

    def test_blocked_and_closed_accounts_refuse_access(self):
        self.stores.account("acct-block").context_begin("user", None)
        self.stores.block_account("acct-block")
        with self.assertRaises(AdvisorError):
            self.stores.account("acct-block")
        self.stores.unblock_account("acct-block")
        self.assertEqual(self.stores.account("acct-block").context("user")["state"], "reading")
        with self.assertRaises(AdvisorError):
            self.stores.account("")
        self.stores.close()
        with self.assertRaises(AdvisorError):
            self.stores.account("acct-block")

    def test_account_rejects_reparse_components(self):
        attacker = self.root / "outside"
        attacker.mkdir()
        linked = self.root / ".local" / "advisor-data"
        linked.parent.mkdir(parents=True)
        try:
            linked.symlink_to(attacker, target_is_directory=True)
        except (OSError, NotImplementedError):
            import subprocess
            created = subprocess.run(["cmd", "/c", "mklink", "/J", str(linked), str(attacker)],
                                     capture_output=True)
            if created.returncode != 0:
                self.skipTest("symlinks and junctions unavailable")
        with self.assertRaises(AccountPathError):
            self.stores.account("acct-evil").context_begin("user", None)


if __name__ == "__main__":
    unittest.main()
