"""Core's per-account database: the chat projection and the user's human data.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Split out of advisor_store.py, 2026-10-13 (plugin architecture): the Core part of the old
Advisor account store. Each real account keeps one SQLite file at
``.local/advisor-data/<sha256(account)>/advisor.sqlite3`` (name kept so existing data and
rollbacks keep working). Core owns ``context`` / ``context_messages`` (the projection) and the
friend_store tables; tables that belonged to features now in plugins stay in the file
untouched (Core copies them once into the plugin's own database, see core_migration.py).
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import os
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path

import friend_store
from core_contracts import AdvisorError


class AccountPathError(RuntimeError):
    """An owned account-data path is not a plain directory under its root."""


def _reparse(path):
    try:
        details = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(details.st_mode):
        return True
    if getattr(details, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
        return True
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


def _long_path(path):
    """Windows extended-length form: engine caches nest past MAX_PATH, unreachable while LongPathsEnabled is 0."""
    path = os.path.abspath(path)
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    return "\\\\?\\UNC\\" + path[2:] if path.startswith("\\\\") else "\\\\?\\" + path


def _checked_root(root):
    root = Path(os.path.abspath(root))
    if _reparse(root):
        raise AccountPathError("account data root is not a plain directory")
    return root


def _ensure_directory(root, target, *, leaf_file=False):
    """Create ``target`` under ``root`` after rejecting reparse components."""
    root = _checked_root(root)
    target = Path(os.path.abspath(target))
    if target != root and not target.is_relative_to(root):
        raise AccountPathError("account data path escapes its root")
    for part in (target, *target.parents):
        if _reparse(part):
            raise AccountPathError("account data path contains a reparse point")
        if part == root:
            break
    directory = target.parent if leaf_file else target
    directory.mkdir(parents=True, exist_ok=True)
    if _reparse(directory) or not directory.is_dir():
        raise AccountPathError("account data path is not a plain directory")
    return target


def _write_json_atomic(path, document, commit_check=None):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if commit_check is not None:
            commit_check()
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS context (
    user TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL,
    read_count INTEGER NOT NULL,
    total_count INTEGER,
    error TEXT,
    source_id TEXT,
    epoch TEXT,
    highwater TEXT,
    cursor TEXT,
    last_sort TEXT,
    started_at_ms INTEGER,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS context_messages (
    user TEXT NOT NULL,
    seq INTEGER NOT NULL,
    msg_id TEXT NOT NULL,
    sort_key TEXT NOT NULL,
    group_chat INTEGER NOT NULL DEFAULT 0,
    side TEXT,
    sender_id TEXT,
    sender_name TEXT,
    kind TEXT,
    text TEXT,
    time_ms INTEGER,
    raw TEXT,
    PRIMARY KEY(user, seq)
);
"""


def _now_ms():
    return int(time.time() * 1000)


def _casefold(value):
    return value.casefold() if isinstance(value, str) else None


class _ReadSnapshot:
    """A read-only WAL snapshot; a slow consumer holds no Core writer lock."""
    def __init__(self, path):
        self.lock = threading.Lock()
        self.closed = False
        self.connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2,
                                          check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        # Unicode case folding for name matching in SQL (SQLite's LIKE folds ASCII only).
        self.connection.create_function("kyf_casefold", 1, _casefold, deterministic=True)
        self.connection.execute("PRAGMA query_only=ON")
        self.connection.execute("BEGIN")

    def read_rows(self, sql, params=()):
        with self.lock:
            if self.closed:
                raise AdvisorError("account-closed", "账号数据已关闭")
            deadline = time.monotonic() + 5
            self.connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 10000)
            try:
                return [dict(row) for row in self.connection.execute(sql, params)]
            finally:
                self.connection.set_progress_handler(None, 0)

    def read_row(self, sql, params=()):
        rows = self.read_rows(sql, params)
        return rows[0] if rows else None

    def close(self):
        with self.lock:
            if not self.closed:
                self.closed = True
                self.connection.close()


class AccountDatabase:
    """The append-only chat projection and human data of one real account."""

    def __init__(self, data_root, account):
        self.data_root = _checked_root(data_root)
        self.account = account
        self.digest = hashlib.sha256(account.encode("utf-8")).hexdigest()
        self.directory = self.data_root / self.digest
        self.path = self.directory / "advisor.sqlite3"
        self.lock = threading.RLock()
        self._connection = None
        self._closed = False
        self._readers_lock = threading.Lock()
        self._readers = set()

    # -- connection ----------------------------------------------------------
    def _connect(self):
        with self.lock:
            if self._connection is not None:
                return self._connection
            if self._closed:
                raise AdvisorError("account-closed", "账号数据已关闭")
            _ensure_directory(self.data_root, self.directory)
            for filename in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
                if _reparse(filename):
                    raise AccountPathError("account database is not a plain file")
            connection = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")
                friend_store.backup_before_migration(connection, self.path)
                connection.executescript(_SCHEMA)
                columns = {row[1] for row in connection.execute("PRAGMA table_info(context)")}
                if "epoch" not in columns:
                    connection.execute("ALTER TABLE context ADD COLUMN epoch TEXT")
                connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS context_message_identity ON context_messages(user,msg_id)")
                friend_store.ensure_schema(connection)
                connection.commit()
                self._connection = connection
            except Exception:
                self._connection = None
                connection.close()
                raise
            return connection

    def close(self):
        with self.lock:
            self._closed = True
            if self._connection is not None:
                try:
                    self._connection.close()
                finally:
                    self._connection = None
        with self._readers_lock:
            for reader in self._readers:
                reader.close()
            self._readers.clear()

    @contextmanager
    def read_snapshot(self):
        if self._connection is None:
            self._connect()
        with self._readers_lock:
            if self._closed:
                raise AdvisorError("account-closed", "账号数据已关闭")
            if len(self._readers) >= 8:
                raise AdvisorError("data-busy", "资料读取正在排队，请稍后重试")
            reader = _ReadSnapshot(self.path)
            self._readers.add(reader)
        try:
            yield reader
        finally:
            reader.close()
            with self._readers_lock:
                self._readers.discard(reader)

    def read_rows(self, sql, params=()):
        with self.read_snapshot() as reader:
            return reader.read_rows(sql, params)

    def read_row(self, sql, params=()):
        rows = self.read_rows(sql, params)
        return rows[0] if rows else None

    # -- low-level helpers ---------------------------------------------------
    def _rows(self, sql, params=()):
        connection = self._connect()
        with self.lock:
            return [dict(row) for row in connection.execute(sql, params)]

    def _row(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # -- context -------------------------------------------------------------
    def context(self, user):
        return self._row("SELECT * FROM context WHERE user=?", (user,))

    def context_begin(self, user, highwater_json, source_id=None, reset=False):
        now = _now_ms()
        with self.lock:
            connection = self._connect()
            with connection:
                if reset:
                    connection.execute("DELETE FROM context_messages WHERE user=?", (user,))
                    connection.execute("DELETE FROM context WHERE user=?", (user,))
                connection.execute(
                    "INSERT INTO context(user,state,revision,read_count,total_count,error,source_id,epoch,"
                    "highwater,cursor,last_sort,started_at_ms,updated_at_ms)"
                    " VALUES(?,?,?,?,?,NULL,?,?,?,NULL,NULL,?,?)"
                    " ON CONFLICT(user) DO UPDATE SET state='reading', error=NULL,"
                    " highwater=excluded.highwater, source_id=excluded.source_id,total_count=NULL,"
                    " started_at_ms=COALESCE(context.started_at_ms, excluded.started_at_ms),"
                    " updated_at_ms=excluded.updated_at_ms",
                    (user, "reading", 0, 0, None, source_id, uuid.uuid4().hex, highwater_json, now, now))
        return self.context(user)

    def context_append_page(self, user, rows_to_add, cursor_json, last_sort_json):
        """Append one imported page; a page that inserts nothing does not bump revision."""
        now = _now_ms()
        with self.lock:
            connection = self._connect()
            with connection:
                inserted = 0
                next_seq = connection.execute("SELECT COALESCE(MAX(seq),0) FROM context_messages WHERE user=?", (user,)).fetchone()[0]
                for row in rows_to_add:
                    prior = connection.execute("SELECT sort_key,text,raw,sender_id FROM context_messages WHERE user=? AND msg_id=?",
                                               (user, row["msg_id"])).fetchone()
                    if prior is not None:
                        if (prior["sort_key"], prior["text"], prior["raw"], prior["sender_id"]) != (
                                row["sort_key"], row["text"], row["raw"], row["sender_id"]):
                            raise AdvisorError("context-error", "历史消息内容发生变化，请重新读取")
                        continue
                    next_seq += 1
                    cursor = connection.execute(
                        "INSERT INTO context_messages(user,seq,msg_id,sort_key,group_chat,"
                        "side,sender_id,sender_name,kind,text,time_ms,raw)"
                        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (user, next_seq, row["msg_id"], row["sort_key"], row["group_chat"],
                         row["side"], row["sender_id"], row["sender_name"], row["kind"],
                         row["text"], row["time_ms"], row["raw"]))
                    inserted += cursor.rowcount
                # The forward cursor always advances; only fresh rows bump the
                # revision and the read count.
                if inserted:
                    connection.execute(
                        "UPDATE context SET state='reading', revision=revision+1,"
                        " read_count=read_count+?, cursor=?, last_sort=?, updated_at_ms=?"
                        " WHERE user=?",
                        (inserted, cursor_json, last_sort_json, now, user))
                else:
                    if cursor_json or last_sort_json:
                        connection.execute(
                            "UPDATE context SET cursor=?, last_sort=?, updated_at_ms=? WHERE user=?",
                            (cursor_json, last_sort_json, now, user))
        return inserted

    def context_finish(self, user, total_count):
        now = _now_ms()
        with self.lock:
            connection = self._connect()
            with connection:
                connection.execute(
                    "UPDATE context SET state='ready', error=NULL, total_count=?, updated_at_ms=?"
                    " WHERE user=?", (total_count, now, user))
        return self.context(user)

    def context_error(self, user, message):
        now = _now_ms()
        with self.lock:
            connection = self._connect()
            with connection:
                connection.execute(
                    "UPDATE context SET state='error', error=?, updated_at_ms=? WHERE user=?",
                    (message, now, user))
        return self.context(user)

    def context_messages(self, user, upto_seq):
        if upto_seq is None:
            return self._rows("SELECT * FROM context_messages WHERE user=? ORDER BY seq ASC", (user,))
        return self._rows(
            "SELECT * FROM context_messages WHERE user=? AND seq<=? ORDER BY seq ASC",
            (user, upto_seq))

    def context_window_matches(self, user, rows):
        """Validate overlap against immutable message facts, ignoring renamed contacts."""
        known = set()
        with self.lock:
            connection = self._connect()
            for row in rows:
                previous = connection.execute("SELECT * FROM context_messages WHERE user=? AND msg_id=?",
                                              (user, row["msg_id"])).fetchone()
                if previous is None:
                    continue
                facts = ("sort_key", "side", "sender_id", "kind", "text", "time_ms")
                # Newly available WeChat transcripts invalidate stale model context.
                old_raw, new_raw = json.loads(previous["raw"] or "{}"), json.loads(row["raw"] or "{}")
                old_quote = old_raw.get("inputMeta", {}).get("quote")
                new_quote = new_raw.get("inputMeta", {}).get("quote")
                if (any(previous[field] != row[field] for field in facts) or old_quote != new_quote or
                        old_raw.get("media") != new_raw.get("media")):
                    raise AdvisorError("context-error", "已读取消息发生变化，需要重新整理会话资料")
                known.add(row["msg_id"])
        return known

    # -- removal -------------------------------------------------------------
    def _assert_owned_tree(self):
        if not os.path.lexists(self.directory):
            return False
        if _reparse(self.directory) or not self.directory.is_dir():
            raise AccountPathError("account directory is not a plain directory")
        for base, directories, files in os.walk(_long_path(self.directory)):
            for name in list(directories) + list(files):
                path = Path(base) / name
                if _reparse(path):
                    raise AccountPathError("account directory contains a reparse point")
        return True

    def remove(self):
        """Close and delete this account's owned directory (projection, human data, legacy tables)."""
        with self.lock:
            self.close()
            if not self._assert_owned_tree():
                return {"deleted": False}
            shutil.rmtree(_long_path(self.directory))
        return {"deleted": True}


class AccountStores:
    """Factory and lifecycle owner of the per-account databases under ``.local/advisor-data``."""

    def __init__(self, root):
        self.root = _checked_root(root)
        self.data_root = self.root / ".local" / "advisor-data"
        self.lock = threading.RLock()
        self._accounts = {}
        self._blocked = set()
        self._closed = False

    def account(self, account):
        if not isinstance(account, str) or not account:
            raise AdvisorError("invalid-request", "invalid account")
        with self.lock:
            if self._closed or account in self._blocked:
                raise AdvisorError("account-closed", "账号数据已关闭")
            database = self._accounts.get(account)
            if database is None or database._closed:
                database = AccountDatabase(self.data_root, account)
                self._accounts[account] = database
            return database

    def saved_database(self, account):
        """显式「已保存账号」离线读取作用域：已有数据才打开，缺失时返回 None 绝不新建。

        Unlike :meth:`account` this never creates a database, so an offline MCP read
        of an account without local data reports a cache gap instead of silently
        materializing an empty projection.
        """
        if not isinstance(account, str) or not account:
            raise AdvisorError("invalid-request", "invalid account")
        with self.lock:
            if self._closed or account in self._blocked:
                raise AdvisorError("account-closed", "账号数据已关闭")
            database = self._accounts.get(account)
            if database is not None and not database._closed:
                return database
            digest_directory = self.data_root / hashlib.sha256(account.encode("utf-8")).hexdigest()
            if not (digest_directory / "advisor.sqlite3").is_file():
                return None
            database = AccountDatabase(self.data_root, account)
            self._accounts[account] = database
            return database

    def block_account(self, account):
        with self.lock:
            self._blocked.add(account)

    def unblock_account(self, account):
        with self.lock:
            if not self._closed:
                self._blocked.discard(account)

    def release_account(self, account):
        """Close one account's database so the parent may remove its files."""
        with self.lock:
            database = self._accounts.pop(account, None)
        if database is not None:
            database.close()

    def remove_account(self, account):
        with self.lock:
            database = self._accounts.pop(account, None)
        if database is not None:
            return database.remove()
        return AccountDatabase(self.data_root, account).remove()

    def close(self):
        with self.lock:
            self._closed = True
            databases = list(self._accounts.values())
            self._accounts.clear()
        for database in databases:
            database.close()
