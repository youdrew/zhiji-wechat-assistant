"""One-time copy of bundled plugins' old data: tables and the global catalog.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from core_account_db import AccountStores
from core_migration import BACKUP_NAME, LEGACY_TABLES, RETRY_SECONDS, PluginMigrations, migrate_global

ACCOUNT = "synthetic-account-1"
KEY = hashlib.sha256(ACCOUNT.encode("utf-8")).hexdigest()
ROOT = Path(__file__).resolve().parents[1]

# The shape the pre-plugin Advisor left in advisor.sqlite3 (columns trimmed to what the copy needs).
LEGACY_ADVISOR = """
CREATE TABLE threads (id TEXT PRIMARY KEY, user TEXT NOT NULL, agent_id TEXT NOT NULL, source_id TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL, title TEXT);
CREATE TABLE messages (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, status TEXT, created_at_ms INTEGER NOT NULL);
CREATE TABLE runs (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    request_id TEXT, state TEXT NOT NULL, error TEXT, template_revision INTEGER NOT NULL,
    context_revision INTEGER NOT NULL, source_id TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL);
CREATE TABLE summaries (user TEXT NOT NULL, range_key TEXT NOT NULL, version TEXT NOT NULL, model_fp TEXT NOT NULL,
    level INTEGER NOT NULL, text TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
    PRIMARY KEY(user, range_key, version, model_fp, level));
CREATE TABLE session_selections (user TEXT NOT NULL, agent_id TEXT NOT NULL, thread_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1, updated_at_ms INTEGER NOT NULL, PRIMARY KEY(user, agent_id));
INSERT INTO threads VALUES('thread:1','wxid_a','builtin:advisor','src',1,2,'周末');
INSERT INTO messages VALUES('msg:1','thread:1',1,'user','周末吃什么？',NULL,1);
INSERT INTO messages VALUES('msg:2','thread:1',2,'assistant','火锅',NULL,2);
INSERT INTO runs VALUES('run:1','thread:1','req-1','running',NULL,1,3,'src',1,2);
INSERT INTO summaries VALUES('wxid_a','range','advisor-compress-v3','fp',1,'摘要',1);
INSERT INTO session_selections VALUES('wxid_a','builtin:advisor','thread:1',1,2);
"""


class MigrationCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stores = AccountStores(self.root)
        self.addCleanup(self.stores.close)
        self.database = self.stores.account(ACCOUNT)
        connection = self.database._connect()
        with self.database.lock, connection:
            connection.executescript(LEGACY_ADVISOR)
        self.plugin_data = self.root / ".local" / "plugin-data"

    def folder(self, plugin):
        return self.plugin_data / plugin / "accounts" / KEY

    def plugin_rows(self, sql):
        connection = sqlite3.connect(str(self.folder("kyf.advisor") / "advisor.sqlite3"))
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()


class AccountMigrationTests(MigrationCase):
    def test_assistant_tables_are_copied_once_with_a_backup_and_kept_in_place(self):
        self.assertTrue(PluginMigrations(self.stores, self.plugin_data).ensure(ACCOUNT))
        self.assertEqual(self.plugin_rows("SELECT id, text FROM messages ORDER BY seq"),
                         [("msg:1", "周末吃什么？"), ("msg:2", "火锅")])
        self.assertEqual(self.plugin_rows("SELECT state FROM runs"), [("running",)])
        self.assertEqual(self.plugin_rows("SELECT text FROM summaries"), [("摘要",)])
        self.assertEqual(self.plugin_rows("SELECT thread_id FROM session_selections"), [("thread:1",)])
        self.assertIn("REFERENCES threads", self.plugin_rows(
            "SELECT sql FROM sqlite_master WHERE name='messages'")[0][0])
        self.assertTrue((self.folder("kyf.advisor") / "advisor.sqlite3.migrated").is_file())
        self.assertTrue(self.database.path.with_name(BACKUP_NAME).is_file())
        # The originals stay for older versions; a later start does not copy again.
        self.assertEqual(self.database._row("SELECT COUNT(*) AS n FROM messages")["n"], 2)
        connection = self.database._connect()
        with self.database.lock, connection:
            connection.execute("INSERT INTO messages VALUES('msg:3','thread:1',3,'user','再来',NULL,3)")
        self.assertTrue(PluginMigrations(self.stores, self.plugin_data).ensure(ACCOUNT))
        self.assertEqual(len(self.plugin_rows("SELECT id FROM messages")), 2)

    def test_core_no_longer_creates_plugin_tables_in_new_account_databases(self):
        fresh = self.stores.account("synthetic-account-2")
        names = {row["name"] for row in fresh._rows("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"context", "context_messages", "memories"} <= names)
        self.assertFalse(names & {"threads", "messages", "runs", "summaries", "auto_reply", "session_selections"})

    def test_old_engine_folder_is_not_copied_but_the_assistant_tables_are(self):
        # Native sessions in runtime/ are bound to the old working directory; the plugin rebuilds them from its tables.
        runtime = self.database.directory / "runtime"
        (runtime / "data" / "opencode").mkdir(parents=True)
        (runtime / "data" / "opencode" / "opencode.db").write_text("sessions", encoding="utf-8")
        self.assertTrue(PluginMigrations(self.stores, self.plugin_data).ensure(ACCOUNT))
        self.assertEqual(sorted(path.name for path in self.folder("kyf.advisor").iterdir()),
                         ["advisor.sqlite3", "advisor.sqlite3.migrated"])
        self.assertEqual(self.plugin_rows("SELECT id FROM threads"), [("thread:1",)])
        self.assertTrue((runtime / "data" / "opencode" / "opencode.db").is_file(), "the old tree is kept for rollback")

    def test_a_failed_copy_is_reported_for_its_plugin_and_retried(self):
        self.plugin_data.mkdir(parents=True)
        (self.plugin_data / "kyf.advisor").write_text("not a folder", encoding="utf-8")
        now = [100.0]
        migrations = PluginMigrations(self.stores, self.plugin_data, clock=lambda: now[0])
        attempts = []
        migrate = migrations._migrate
        migrations._migrate = lambda account: attempts.append(account) or migrate(account)
        self.assertFalse(migrations.ensure(ACCOUNT))
        self.assertEqual(migrations.failed_plugins(), {"kyf.advisor"})
        self.assertTrue((self.folder("kyf.message-insights") / "insights.sqlite3.migrated").is_file(),
                        "other plugins are not held back")
        (self.plugin_data / "kyf.advisor").unlink()
        # Every plugin call asks again; a failed copy is not redone on each of them.
        self.assertFalse(migrations.ensure(ACCOUNT))
        self.assertEqual(len(attempts), 1)
        now[0] += RETRY_SECONDS
        self.assertTrue(migrations.ensure(ACCOUNT))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(migrations.failed_plugins(), set())
        self.assertEqual(len(self.plugin_rows("SELECT id FROM threads")), 1)

    def test_a_failed_backup_leaves_no_partial_copy_and_is_retried(self):
        now = [100.0]
        migrations = PluginMigrations(self.stores, self.plugin_data, clock=lambda: now[0])
        with mock.patch.object(Path, "replace", side_effect=OSError(28, "No space left on device")):
            self.assertFalse(migrations.ensure(ACCOUNT))
        self.assertEqual(migrations.failed_plugins(ACCOUNT), set(LEGACY_TABLES))
        self.assertEqual([path.name for path in self.database.directory.iterdir() if path.name.endswith(".tmp")], [])
        self.assertFalse(self.database.path.with_name(BACKUP_NAME).exists())
        self.assertFalse((self.folder("kyf.advisor") / "advisor.sqlite3.migrated").exists(), "nothing copied yet")
        now[0] += RETRY_SECONDS
        self.assertTrue(migrations.ensure(ACCOUNT))
        self.assertTrue(self.database.path.with_name(BACKUP_NAME).is_file())
        self.assertEqual(len(self.plugin_rows("SELECT id FROM messages")), 2)

    def test_listing_shows_plugins_whose_data_is_not_migrated(self):
        from core_services import CoreServices
        local = self.root / ".local"
        (local / "advisor").mkdir(parents=True)
        (local / "advisor" / "config.json").write_text('{"version": 2}', encoding="utf-8")
        self.plugin_data.mkdir(parents=True)
        (self.plugin_data / "kyf.advisor").write_text("not a folder", encoding="utf-8")
        plugins = self.root / "plugins"
        for plugin in ("kyf.advisor", "kyf.friend-analysis"):
            (plugins / plugin).mkdir(parents=True)
            (plugins / plugin / "main.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            (plugins / plugin / "plugin.json").write_text(json.dumps({
                "schema": 1, "id": plugin, "name": "测试", "version": "1.0.0", "core": {"api": [1]},
                "runtime": {"type": "python", "entry": "main.py"}}), encoding="utf-8")
        core = CoreServices(SimpleNamespace(source=None), ROOT, "http://127.0.0.1:0", local=local,
                            bundled_dir=plugins, local_dir=self.root / "none")
        self.addCleanup(core.close)
        states = {item["id"]: item.get("migration") for item in core.listing()["plugins"]}
        self.assertEqual(states, {"kyf.advisor": "failed", "kyf.friend-analysis": None})
        core._global_migration_failures = set()
        core._migrations = PluginMigrations(self.stores, self.plugin_data)
        core._migrations.ensure(ACCOUNT)
        self.assertEqual(core.listing()["plugins"][0].get("migration"), "failed")

    def test_failed_migration_blocks_only_that_plugins_calls_and_can_be_retried(self):
        from core_plugin_process import PluginUnavailable
        from core_services import CoreServices
        local = self.root / ".local"
        core = CoreServices(SimpleNamespace(source=None), ROOT, "http://127.0.0.1:0", local=local,
                            bundled_dir=self.root / "no-plugins", local_dir=self.root / "no-local-plugins")
        self.addCleanup(core.close)
        core._history = SimpleNamespace(stores=self.stores)
        self.plugin_data.mkdir(parents=True, exist_ok=True)
        (self.plugin_data / "kyf.advisor").write_text("blocked by file", encoding="utf-8")
        with self.assertRaises(PluginUnavailable) as failed:
            core.require_plugin("kyf.advisor", ACCOUNT)
        self.assertEqual(failed.exception.code, "migration-failed")
        core.require_plugin("kyf.friend-analysis", ACCOUNT)
        self.assertEqual(self.database._row("SELECT COUNT(*) AS n FROM messages")["n"], 2)
        (self.plugin_data / "kyf.advisor").unlink()
        core._migrations.clock = lambda: time.monotonic() + RETRY_SECONDS
        core.require_plugin("kyf.advisor", ACCOUNT)
        self.assertEqual(self.plugin_rows("SELECT id FROM threads"), [("thread:1",)])

    def test_a_failed_catalog_copy_is_retried_on_use_without_a_restart(self):
        from core_plugin_process import PluginUnavailable
        from core_services import CoreServices
        local = self.root / ".local"
        (local / "advisor").mkdir(parents=True)
        (local / "advisor" / "config.json").write_text('{"version": 2, "agents": []}', encoding="utf-8")
        self.plugin_data.mkdir(parents=True)
        (self.plugin_data / "kyf.advisor").write_text("blocked by file", encoding="utf-8")
        core = CoreServices(SimpleNamespace(source=None), ROOT, "http://127.0.0.1:0", local=local,
                            bundled_dir=self.root / "no-plugins", local_dir=self.root / "no-local-plugins")
        self.addCleanup(core.close)
        with self.assertRaises(PluginUnavailable) as failed:
            core.require_plugin("kyf.advisor")
        self.assertEqual(failed.exception.code, "migration-failed")
        (self.plugin_data / "kyf.advisor").unlink()
        core.require_plugin("kyf.advisor")
        self.assertEqual(json.loads((self.plugin_data / "kyf.advisor" / "config.json").read_text(encoding="utf-8")),
                         {"version": 2, "agents": []})
        self.assertNotIn("kyf.advisor", core.migration_failures())


class GlobalMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.local = Path(temp.name) / ".local"
        self.target = self.local / "plugin-data" / "kyf.advisor"

    def test_catalog_and_its_v1_backup_are_copied_once(self):
        (self.local / "advisor").mkdir(parents=True)
        (self.local / "advisor" / "config.json").write_text('{"version": 2, "agents": []}', encoding="utf-8")
        (self.local / "advisor" / "config.v1.backup.json").write_text('{"version": 1}', encoding="utf-8")
        self.assertEqual(migrate_global(self.local), set())
        self.assertEqual(json.loads((self.target / "config.json").read_text(encoding="utf-8")),
                         {"version": 2, "agents": []})
        self.assertTrue((self.target / "config.v1.backup.json").is_file())
        self.assertTrue((self.target / "config.json.migrated").is_file())
        (self.local / "advisor" / "config.json").write_text('{"version": 2, "agents": [1]}', encoding="utf-8")
        (self.target / "config.json.migrated").unlink()
        self.assertEqual(migrate_global(self.local), set())
        self.assertEqual(json.loads((self.target / "config.json").read_text(encoding="utf-8"))["agents"], [],
                         "the plugin's own catalog is never overwritten")
        self.assertTrue((self.local / "advisor" / "config.json").is_file(), "the old catalog is kept")

    def test_nothing_to_copy_leaves_no_trace_and_failures_never_raise(self):
        self.assertEqual(migrate_global(self.local), set())
        self.assertFalse(self.target.exists())
        (self.local / "advisor").mkdir(parents=True)
        (self.local / "advisor" / "config.json").write_text("{}", encoding="utf-8")
        self.target.parent.mkdir(parents=True)
        self.target.write_text("not a folder", encoding="utf-8")
        self.assertEqual(migrate_global(self.local), {"kyf.advisor"})


if __name__ == "__main__":
    unittest.main()
