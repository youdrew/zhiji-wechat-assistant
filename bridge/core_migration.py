"""One-time copy of bundled plugins' data out of the old per-account database and the global catalog.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Before the plugin architecture these features kept their tables in each account's
``advisor.sqlite3``. On the first use of an account, Core keeps one backup
(``advisor.pre-plugins.sqlite3``) and copies the tables into each plugin's own
database. The original tables are not deleted, so an older version can still
read them. A failed copy is retried on the next use; chat browsing is unaffected.
Modified 2026-10-13: kyf.advisor tables and the global assistant catalog (``migrate_global``)
are copied the same way, never moved.
Modified 2026-10-14: the old opencode state folder (``runtime/``) is not copied. Its sessions
are bound to the old working directory; the plugin rebuilds them from its migrated tables.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from core_account_db import _reparse

# plugin id -> (database file inside the plugin's account folder, tables it owns; parents before children)
LEGACY_TABLES = {
    "kyf.message-insights": ("insights.sqlite3", ("message_analysis_settings", "message_analyses", "auto_reply")),
    "kyf.friend-analysis": ("analysis.sqlite3", ("friend_jobs", "friend_checkpoints", "friend_measurements",
                                                 "friend_runs", "friend_scans", "friend_chat_blocks",
                                                 "friend_persona_evidence", "friend_results",
                                                 "friend_step_measurements")),
    "kyf.advisor": ("advisor.sqlite3", ("threads", "messages", "runs", "requests", "summaries",
                                        "session_selections", "session_create_requests", "session_drafts",
                                        "session_usage", "session_operations", "session_native_registry",
                                        "session_recovery", "session_request_tombstones")),
}
# plugin id -> (folder under .local, files copied into .local/plugin-data/<plugin>/; the first one marks it)
LEGACY_GLOBAL = {"kyf.advisor": ("advisor", ("config.json", "config.v1.backup.json"))}
BACKUP_NAME = "advisor.pre-plugins.sqlite3"
# Every plugin call for the account asks for the copy; a failed one (a full database backup) is not redone on each.
RETRY_SECONDS = 10.0


def _key(account):
    return hashlib.sha256(account.encode("utf-8")).hexdigest()


def _tables(connection, schema="main"):
    return {row[0]: row[1] for row in connection.execute(
        f"SELECT name, sql FROM {schema}.sqlite_master WHERE type='table'")}


def _columns(connection, schema, table):
    return [row[1] for row in connection.execute(f"PRAGMA {schema}.table_info({table})")]


def migrate_global(local, plugin_data=None, legacy=None):
    """Copy plugin-wide files (the assistant catalog) once; returns the plugins whose copy failed.

    Never raises: a failure leaves the plugin's defaults in place and is retried on the next start.
    """
    local = Path(local)
    plugin_data = Path(plugin_data) if plugin_data is not None else local / "plugin-data"
    failed = set()
    for plugin, (folder, names) in (LEGACY_GLOBAL if legacy is None else legacy).items():
        target_dir = plugin_data / plugin
        marker = target_dir / (names[0] + ".migrated")
        source_dir = local / folder
        try:
            if marker.exists() or not (source_dir / names[0]).is_file():
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            for name in names:
                source, target = source_dir / name, target_dir / name
                if not source.is_file() or _reparse(source) or os.path.lexists(target):
                    continue
                temporary = target.with_name(name + "." + uuid.uuid4().hex + ".tmp")
                try:
                    shutil.copyfile(source, temporary)
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            marker.write_text("1\n", encoding="utf-8")
        except OSError:
            failed.add(plugin)
    return failed


class PluginMigrations:
    def __init__(self, stores, plugin_data, legacy=None, clock=time.monotonic):
        self.stores = stores
        self.plugin_data = Path(plugin_data)
        self.legacy = LEGACY_TABLES if legacy is None else legacy
        self.clock = clock
        self.lock = threading.Lock()
        self.done = set()
        self.failures = {}  # account -> {plugin: error name}
        self.retry_at = {}  # account -> earliest clock() of the next attempt after a failure

    def failed_plugins(self, account=None):
        with self.lock:
            if account is not None:
                return set(self.failures.get(account, {}))
            return {plugin for failed in self.failures.values() for plugin in failed}

    def ensure(self, account):
        """Copy legacy data for ``account`` once; never raises into the caller."""
        if account in self.done:
            return True
        with self.lock:
            if account in self.done:
                return True
            if account in self.failures and self.clock() < self.retry_at.get(account, 0):
                return False
            failed = self._migrate(account)
            if failed:
                self.failures[account] = failed
                self.retry_at[account] = self.clock() + RETRY_SECONDS
                return False
            self.done.add(account)
            self.failures.pop(account, None)
            self.retry_at.pop(account, None)
            return True

    def _folder(self, plugin, account):
        return self.plugin_data / plugin / "accounts" / _key(account)

    def _migrate(self, account):
        failed = {}
        database = self.stores.account(account)
        source_path = Path(database.path)
        pending = [(plugin, spec) for plugin, spec in self.legacy.items()
                   if not (self._folder(plugin, account) / (spec[0] + ".migrated")).exists()]
        if not pending or not source_path.is_file():
            return failed
        try:
            with database.lock:
                database._connect()
                connection = sqlite3.connect(str(source_path), timeout=30)
                try:
                    backup = source_path.with_name(BACKUP_NAME)
                    if not backup.exists():
                        temporary = backup.with_name(backup.name + "." + uuid.uuid4().hex + ".tmp")
                        try:
                            target = sqlite3.connect(str(temporary))
                            try:
                                connection.backup(target)
                            finally:
                                target.close()
                            temporary.replace(backup)
                        finally:
                            temporary.unlink(missing_ok=True)  # A failed backup leaves no partial copy behind.
                    available = _tables(connection)
                    for plugin, (filename, tables) in pending:
                        try:
                            self._copy_tables(connection, available, self._folder(plugin, account), filename, tables)
                        except (OSError, sqlite3.Error) as exc:
                            failed[plugin] = type(exc).__name__
                finally:
                    connection.close()
        except (OSError, sqlite3.Error) as exc:
            for plugin, _spec in pending:
                failed.setdefault(plugin, type(exc).__name__)
        return failed

    @staticmethod
    def _copy_tables(connection, available, folder, filename, tables):
        folder.mkdir(parents=True, exist_ok=True)
        connection.execute("ATTACH DATABASE ? AS plugin", (str(folder / filename),))
        try:
            with connection:
                existing = _tables(connection, "plugin")
                for table in tables:
                    if table not in available:
                        continue
                    if table not in existing:
                        connection.execute(available[table].replace(
                            f"CREATE TABLE {table}", f"CREATE TABLE plugin.{table}", 1)
                            .replace(f"CREATE TABLE IF NOT EXISTS {table}",
                                     f"CREATE TABLE IF NOT EXISTS plugin.{table}", 1))
                    shared = [column for column in _columns(connection, "main", table)
                              if column in set(_columns(connection, "plugin", table))]
                    names = ",".join(f'"{column}"' for column in shared)
                    connection.execute(f"INSERT OR IGNORE INTO plugin.{table}({names}) "
                                       f"SELECT {names} FROM main.{table}")
        finally:
            connection.execute("DETACH DATABASE plugin")
        (folder / (filename + ".migrated")).write_text("1\n", encoding="utf-8")
