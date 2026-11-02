"""The plugin's per-account database: discussions, runs, summaries and a mirror of Core's chat projection.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

One SQLite file per real account at ``<dataDir>/accounts/<sha256(account)>/advisor.sqlite3``. The saved
thread transcript is an immutable UI projection of engine output: messages are append-only within a live
thread; explicit session or account deletion removes it. ``context`` and ``context_messages`` are a copy of
Core's projection kept by ``accounts.AccountData.sync``; Core owns the original. The engine's own state lives
in ``<account dir>/engine`` so Core's account clear removes it with the rest.
"""
# Modified by Know Your Friends, 2026-10-09: persist per-contact automatic suggestions.
# Modified by Know Your Friends, 2026-10-10: schema v7 adds 知己分析 records with a pre-migration backup.
# Modified by Know Your Friends, 2026-10-10 (Apache-2.0): schema v8 adds durable
# session selection, operation ownership, drafts and recoverable cleanup.
# Moved from bridge/advisor_store.py into the kyf.advisor plugin, 2026-10-13 (Apache-2.0): the assistant tables
# (copied once by Core's migration), the mirror tables and the restart rule for running runs; the catalog moved
# to catalog_store.py, the projection import and the human-data tables stay in Core.
# Modified 2026-10-16 (Apache-2.0): persist outgoing queues and pause them after restart.
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from pathlib import Path

import session_store
import sticker_store
import queue_store
import context_snapshot
from contracts import AdvisorError, new_identifier

FILE = "advisor.sqlite3"


class AdvisorPathError(RuntimeError):
    """An owned Advisor path is not a plain directory under its root."""


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
        raise AdvisorPathError("advisor root is not a plain directory")
    return root


def _ensure_directory(root, target, *, leaf_file=False):
    """Create ``target`` under ``root`` after rejecting reparse components."""
    root = _checked_root(root)
    target = Path(os.path.abspath(target))
    if target != root and not target.is_relative_to(root):
        raise AdvisorPathError("advisor path escapes its root")
    for part in (target, *target.parents):
        if _reparse(part):
            raise AdvisorPathError("advisor path contains a reparse point")
        if part == root:
            break
    directory = target.parent if leaf_file else target
    directory.mkdir(parents=True, exist_ok=True)
    if _reparse(directory) or not directory.is_dir():
        raise AdvisorPathError("advisor path is not a plain directory")
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


def _write_bytes_atomic(path, content):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    model_fp TEXT,
    template_revision INTEGER,
    context_epoch TEXT,
    source_id TEXT NOT NULL,
    runtime_session_id TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS threads_scope ON threads(user, agent_id, updated_at_ms DESC);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT,
    created_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS messages_thread_seq ON messages(thread_id, seq);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    request_id TEXT,
    user_message_id TEXT,
    state TEXT NOT NULL,
    error TEXT,
    trigger TEXT NOT NULL DEFAULT 'manual',
    template_revision INTEGER NOT NULL,
    context_revision INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS runs_thread_request ON runs(thread_id, request_id);
CREATE TABLE IF NOT EXISTS requests (
    user TEXT NOT NULL, agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
    message TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    PRIMARY KEY(user,agent_id,request_id)
);
CREATE INDEX IF NOT EXISTS runs_thread ON runs(thread_id, created_at_ms DESC);
CREATE TABLE IF NOT EXISTS summaries (
    user TEXT NOT NULL,
    range_key TEXT NOT NULL,
    version TEXT NOT NULL,
    model_fp TEXT NOT NULL,
    level INTEGER NOT NULL,
    text TEXT NOT NULL,
    coverage_json TEXT,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY(user, range_key, version, model_fp, level)
);
"""
# The copy of Core's projection (Core's columns; ``synced_at_ms`` instead of import cursors).
MIRROR_SCHEMA = """
CREATE TABLE IF NOT EXISTS context (
    user TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 0,
    read_count INTEGER NOT NULL DEFAULT 0,
    total_count INTEGER,
    error TEXT,
    source_id TEXT,
    epoch TEXT,
    synced_at_ms INTEGER NOT NULL
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
CREATE INDEX IF NOT EXISTS context_message_identity ON context_messages(user, msg_id);
"""


def _now_ms():
    return int(time.time() * 1000)


class AdvisorDatabase(session_store.SessionStoreMixin, sticker_store.StickerStoreMixin, queue_store.QueueStoreMixin):
    """Private append-only discussion store for one real account."""

    def __init__(self, data_root, account):
        self.data_root = _checked_root(data_root)
        self.account = account
        self.digest = hashlib.sha256(account.encode("utf-8")).hexdigest()
        self.directory = self.data_root / self.digest
        self.path = self.directory / FILE
        self.lock = threading.RLock()
        self._connection = None
        self._closed = False

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
                    raise AdvisorPathError("advisor database is not a plain file")
            connection = sqlite3.connect(str(self.path), timeout=30.0, check_same_thread=False)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=30000")
                session_store.backup_before_migration(connection, self.path)
                connection.executescript(_SCHEMA)
                connection.executescript(MIRROR_SCHEMA)
                # Tables copied from an older account database may predate these columns.
                for table, field, sql_type in (("threads", "model_fp", "TEXT"),
                                                ("threads", "template_revision", "INTEGER"),
                                                ("threads", "context_epoch", "TEXT"),
                                                ("runs", "user_message_id", "TEXT"),
                                                ("runs", "trigger", "TEXT NOT NULL DEFAULT 'manual'"),
                                                ("threads", "profile_stamp", "TEXT"),
                                                ("summaries", "coverage_json", "TEXT")):
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}
                    if field not in columns:
                        connection.execute("ALTER TABLE " + table + " ADD COLUMN " + field + " " + sql_type)
                session_store.ensure_schema(connection)
                sticker_store.ensure_schema(connection)
                queue_store.ensure_schema(connection)
                connection.commit()
                self._connection = connection
                self._mark_interrupted()
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

    def _mark_interrupted(self):
        """Runs cannot survive a plugin restart; their transcript stays intact."""
        now = _now_ms()
        rows = self._connection.execute(
            "SELECT id, thread_id, session_metadata_json FROM runs WHERE state='running'").fetchall()
        for row in rows:
            metadata = json.loads(row["session_metadata_json"] or "{}")
            snapshot = context_snapshot.public(metadata.get("context_snapshot"))
            if snapshot is not None and snapshot["state"] == "pending":
                metadata["context_snapshot"] = context_snapshot.recorded(snapshot, {}, supplied=False)
            self._connection.execute(
                "UPDATE runs SET state='error', error=?, updated_at_ms=?,session_metadata_json=? WHERE id=?",
                ("服务重启，运行已中断", now, json.dumps(metadata, ensure_ascii=False, separators=(",", ":")), row["id"]))
            self._append_message(row["thread_id"], "status", "上次生成已中断",
                                 status="error", created_at_ms=now, run_id=row["id"])
        self._connection.commit()

    # -- low-level helpers ---------------------------------------------------
    def _rows(self, sql, params=()):
        connection = self._connect()
        with self.lock:
            return [dict(row) for row in connection.execute(sql, params)]

    def _row(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # -- threads -------------------------------------------------------------
    def create_thread(self, user, agent_id, source_id, *, request_id=None, agent_name="", select=False):
        thread_id = new_identifier("thread", uuid.uuid4().hex)
        now = _now_ms()
        with self.lock:
            connection = self._connect()
            with connection:
                if request_id is not None:
                    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128:
                        raise AdvisorError("invalid-request", "会话创建请求编号无效")
                    previous = connection.execute("SELECT thread_id FROM session_create_requests WHERE user=? AND agent_id=? AND request_id=?",
                                                  (user, agent_id, request_id)).fetchone()
                    if previous:
                        return self._session_require(connection, previous["thread_id"], user, agent_id)
                if not isinstance(agent_name, str) or len(agent_name) > 120:
                    raise AdvisorError("invalid-request", "助手历史名称无效")
                if select:
                    current = connection.execute("SELECT t.* FROM session_selections s JOIN threads t ON t.id=s.thread_id "
                                                 "WHERE s.user=? AND s.agent_id=? AND t.delete_state='active'",
                                                 (user, agent_id)).fetchone()
                    if current is not None:
                        has_messages = connection.execute("SELECT 1 FROM messages WHERE thread_id=? LIMIT 1", (current["id"],)).fetchone()
                        has_draft = connection.execute("SELECT 1 FROM session_drafts WHERE thread_id=? AND text<>''", (current["id"],)).fetchone()
                        has_run = connection.execute("SELECT 1 FROM runs WHERE thread_id=? AND state='running' LIMIT 1", (current["id"],)).fetchone()
                        has_operation = connection.execute("SELECT 1 FROM session_operations WHERE thread_id=? AND state IN ('running','unknown') LIMIT 1", (current["id"],)).fetchone()
                        has_queue = connection.execute("SELECT 1 FROM session_queue_items WHERE thread_id=? AND state IN ('pending','starting') LIMIT 1", (current["id"],)).fetchone()
                        if not any((has_messages, has_draft, has_run, has_operation, has_queue)):
                            if request_id is not None:
                                connection.execute("INSERT INTO session_create_requests(user,agent_id,request_id,thread_id) VALUES(?,?,?,?)",
                                                   (user, agent_id, request_id, current["id"]))
                            return dict(current)
                connection.execute(
                    "INSERT INTO threads(id,user,agent_id,source_id,created_at_ms,updated_at_ms,agent_name)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (thread_id, user, agent_id, source_id, now, now, agent_name))
                if request_id is not None:
                    connection.execute("INSERT INTO session_create_requests(user,agent_id,request_id,thread_id) VALUES(?,?,?,?)",
                                       (user, agent_id, request_id, thread_id))
                if select:
                    connection.execute("INSERT INTO session_selections(user,agent_id,thread_id,updated_at_ms) VALUES(?,?,?,?) "
                                       "ON CONFLICT(user,agent_id) DO UPDATE SET thread_id=excluded.thread_id,version=session_selections.version+1,updated_at_ms=excluded.updated_at_ms",
                                       (user, agent_id, thread_id, now))
        return self.thread(thread_id)

    def thread(self, thread_id):
        return self._row("SELECT * FROM threads WHERE id=?", (thread_id,))

    def latest_thread(self, user, agent_id):
        return self._row(
            "SELECT * FROM threads WHERE user=? AND agent_id=? AND delete_state='active'"
            " ORDER BY created_at_ms DESC, rowid DESC LIMIT 1", (user, agent_id))

    def thread_messages(self, thread_id):
        return self._rows("SELECT * FROM messages WHERE thread_id=? ORDER BY seq ASC", (thread_id,))

    def set_runtime_session(self, thread_id, runtime_session_id):
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id)
                if runtime_session_id is not None:
                    self._session_native_register_locked(connection, thread_id, runtime_session_id)
                connection.execute("UPDATE threads SET runtime_session_id=? WHERE id=?",
                                   (runtime_session_id, thread_id))

    def recent_user_texts(self, thread_id):
        return [row["text"] for row in reversed(self._rows(
            "SELECT text FROM messages WHERE thread_id=? AND role='user' AND superseded_by IS NULL ORDER BY seq DESC LIMIT 2", (thread_id,)))]

    def bind_runtime(self, thread_id, source_id, model_fp, template_revision, context_epoch=None, profile_stamp=None):
        """Plan a binding without discarding the last known usable native session.

        The service commits a successful replacement with session_bind_commit.
        Model/window-only changes can reuse a session; provenance/profile changes
        require reconstruction. Existing callers see a null proposed native ID.
        """
        with self.lock:
            connection = self._connect()
            row = self._session_require(connection, thread_id)
            reasons = [key for key, before, after in (
                ("context", row["context_epoch"], context_epoch),
                ("profile", row.get("profile_stamp") or "p0-a0", profile_stamp or "p0-a0"),
            ) if before != after]
            desired = {"source_id": source_id, "model_fp": model_fp, "template_revision": template_revision,
                       "context_epoch": context_epoch, "profile_stamp": profile_stamp}
            planned = {**row, **desired, "requiresRebuild": bool(row["runtime_session_id"] and reasons),
                       "rebuildReason": ",".join(reasons), "previousRuntimeSessionId": row["runtime_session_id"],
                       "bindingMetadata": desired}
            if planned["requiresRebuild"]:
                planned["runtime_session_id"] = None
            if not row["runtime_session_id"]:
                with connection:
                    connection.execute("UPDATE threads SET source_id=?,model_fp=?,template_revision=?,context_epoch=?,profile_stamp=? WHERE id=?",
                                       (source_id, model_fp, template_revision, context_epoch, profile_stamp, thread_id))
            return planned

    def _touch_thread(self, connection, thread_id, now):
        connection.execute("UPDATE threads SET updated_at_ms=? WHERE id=?", (now, thread_id))

    def _append_message(self, thread_id, role, text, status=None, created_at_ms=None, run_id=None):
        connection = self._connect()
        self._session_require(connection, thread_id)
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM messages WHERE thread_id=?", (thread_id,)).fetchone()[0]
        message = {"id": new_identifier("msg", uuid.uuid4().hex), "thread_id": thread_id,
                   "seq": seq, "role": role, "text": text, "status": status,
                   "run_id": run_id,
                   "created_at_ms": created_at_ms if created_at_ms is not None else _now_ms()}
        connection.execute(
            "INSERT INTO messages(id,thread_id,seq,role,text,status,created_at_ms,run_id)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (message["id"], thread_id, seq, role, text, status, message["created_at_ms"], run_id))
        return message

    def append_message(self, thread_id, role, text, status=None):
        with self.lock:
            connection = self._connect()
            with connection:
                message = self._append_message(thread_id, role, text, status=status)
                self._touch_thread(connection, thread_id, message["created_at_ms"])
        return message

    # -- runs ----------------------------------------------------------------
    def find_run_by_request(self, thread_id, request_id):
        if request_id is None:
            return None
        return self._row("SELECT * FROM runs WHERE thread_id=? AND request_id=?",
                         (thread_id, request_id))

    def scoped_request(self, user, agent_id, request_id, message):
        if request_id is None:
            return None
        if self._row("SELECT thread_id FROM session_request_tombstones WHERE user=? AND agent_id=? AND request_id=?",
                     (user, agent_id, request_id)):
            raise AdvisorError("thread-unknown", "该请求所属会话已删除")
        if self._row("SELECT id FROM session_queue_items WHERE user=? AND agent_id=? AND request_id=?",
                     (user, agent_id, request_id)):
            raise AdvisorError("request-conflict", "该请求编号已用于排队消息")
        row = self._row("SELECT requests.message AS request_message,runs.* FROM requests "
                        "JOIN runs ON runs.id=requests.run_id WHERE requests.user=? AND "
                        "requests.agent_id=? AND requests.request_id=?", (user, agent_id, request_id))
        if row is not None and row["request_message"] != message:
            raise AdvisorError("request-conflict", "该请求编号已经用于另一条消息")
        return row

    def run(self, run_id):
        return self._row("SELECT * FROM runs WHERE id=?", (run_id,))

    def run_with_user(self, run_id):
        return self._row(
            "SELECT runs.*, threads.user AS scope_user FROM runs"
            " JOIN threads ON threads.id = runs.thread_id WHERE runs.id=?", (run_id,))

    def latest_run(self, thread_id):
        return self._row("SELECT * FROM runs WHERE thread_id=? ORDER BY created_at_ms DESC, rowid DESC LIMIT 1",
                         (thread_id,))

    def active_run(self, thread_id):
        return self._row("SELECT * FROM runs WHERE thread_id=? AND state='running' ORDER BY created_at_ms DESC LIMIT 1",
                         (thread_id,))

    def active_runs_for_user(self, user):
        return self._rows(
            "SELECT runs.* FROM runs JOIN threads ON threads.id = runs.thread_id"
            " WHERE threads.user=? AND runs.state='running'", (user,))

    def create_run(self, thread, user_message, request_id, template_revision,
                   context_revision, source_id, trigger="manual"):
        """Atomically create the run row and its user message projection."""
        if trigger not in {"manual", "auto-reply"}:
            raise AdvisorError("invalid-request", "invalid run trigger")
        run_id = new_identifier("run", uuid.uuid4().hex)
        now = _now_ms()
        with self.lock:
            connection = self._connect()
            with connection:
                actual = self._session_require(connection, thread["id"], thread["user"], thread["agent_id"])
                if request_id is not None:
                    scoped = self.scoped_request(thread["user"], thread["agent_id"], request_id, user_message)
                    if scoped is not None and scoped["thread_id"] != thread["id"]:
                        raise AdvisorError("request-conflict", "请求编号不属于该会话")
                existing = None
                if request_id is not None:
                    existing = connection.execute(
                        "SELECT * FROM runs WHERE thread_id=? AND request_id=?",
                        (thread["id"], request_id)).fetchone()
                if existing is not None:
                    return dict(existing), None
                self._session_assert_idle(connection, thread["id"])
                connection.execute(
                    "INSERT INTO runs(id,thread_id,request_id,state,error,template_revision,"
                    "context_revision,source_id,created_at_ms,updated_at_ms,trigger)"
                    " VALUES(?,?,?,'running',NULL,?,?,?,?,?,?)",
                    (run_id, thread["id"], request_id, template_revision, context_revision,
                     source_id, now, now, trigger))
                message = self._append_message(thread["id"], "user", user_message, run_id=run_id)
                if not actual["title"]:
                    connection.execute("UPDATE threads SET title=?,session_version=session_version+1 WHERE id=?",
                                       (" ".join(user_message.split())[:80], thread["id"]))
                connection.execute("UPDATE runs SET user_message_id=? WHERE id=?", (message["id"], run_id))
                if request_id is not None:
                    connection.execute("INSERT INTO requests(user,agent_id,request_id,message,run_id) VALUES(?,?,?,?,?)",
                                       (thread["user"], thread["agent_id"], request_id, user_message, run_id))
                self._touch_thread(connection, thread["id"], now)
        return self.run(run_id), message

    def finish_run(self, run_id, thread_id, state, error=None, assistant_text=None,
                   assistant_status=None, status_text=None, runtime_session_id=None):
        """One transaction: optional transcript rows + run state + session + touch."""
        now = _now_ms()
        appended = []
        with self.lock:
            connection = self._connect()
            with connection:
                current = connection.execute("SELECT state,thread_id FROM runs WHERE id=?", (run_id,)).fetchone()
                if current is None or current["thread_id"] != thread_id or current["state"] != "running":
                    return []
                if connection.execute("SELECT 1 FROM threads WHERE id=? AND delete_state='active'", (thread_id,)).fetchone() is None:
                    return []
                if status_text is not None:
                    appended.append(self._append_message(thread_id, "status", status_text,
                                                         status=state, run_id=run_id))
                if assistant_text is not None:
                    appended.append(self._append_message(thread_id, "assistant", assistant_text,
                                                         status=assistant_status, run_id=run_id))
                connection.execute(
                    "UPDATE runs SET state=?, error=?, updated_at_ms=? WHERE id=?",
                    (state, error, now, run_id))
                if runtime_session_id is not None:
                    self._session_native_register_locked(connection, thread_id, runtime_session_id)
                    connection.execute("UPDATE threads SET runtime_session_id=? WHERE id=?",
                                       (runtime_session_id, thread_id))
                self._touch_thread(connection, thread_id, now)
        return appended

    def freeze_run_context(self, run_id, revision):
        with self.lock:
            connection = self._connect()
            with connection:
                connection.execute("UPDATE runs SET context_revision=? WHERE id=? AND state='running'",
                                   (revision, run_id))

    # -- context -------------------------------------------------------------
    def context(self, user):
        return self._row("SELECT * FROM context WHERE user=?", (user,))

    def context_messages(self, user, upto_seq):
        if upto_seq is None:
            return self._rows("SELECT * FROM context_messages WHERE user=? ORDER BY seq ASC", (user,))
        return self._rows(
            "SELECT * FROM context_messages WHERE user=? AND seq<=? ORDER BY seq ASC",
            (user, upto_seq))

    def summary(self, user, range_key, version, model_fp, level):
        row = self._row(
            "SELECT text FROM summaries WHERE user=? AND range_key=? AND version=?"
            " AND model_fp=? AND level=?", (user, range_key, version, model_fp, level))
        return row["text"] if row else None

    def save_summary(self, user, range_key, version, model_fp, level, text, coverage=None):
        with self.lock:
            connection = self._connect()
            with connection:
                connection.execute(
                    "INSERT OR REPLACE INTO summaries(user,range_key,version,model_fp,level,text,"
                    "created_at_ms,coverage_json) VALUES(?,?,?,?,?,?,?,?)",
                    (user, range_key, version, model_fp, level, text, _now_ms(),
                     json.dumps(coverage, separators=(",", ":")) if coverage is not None else None))

    # -- removal -------------------------------------------------------------
    def _assert_owned_tree(self):
        if not os.path.lexists(self.directory):
            return False
        if _reparse(self.directory) or not self.directory.is_dir():
            raise AdvisorPathError("account directory is not a plain directory")
        # Modified 2026-10-14: extended-length paths, the engine folder nests past MAX_PATH.
        for base, directories, files in os.walk(_long_path(self.directory)):
            for name in list(directories) + list(files):
                path = Path(base) / name
                if _reparse(path):
                    raise AdvisorPathError("account directory contains a reparse point")
        return True

    def remove(self):
        """Close and delete this account's owned projection directory."""
        with self.lock:
            self.close()
            if not self._assert_owned_tree():
                return {"deleted": False}
            shutil.rmtree(_long_path(self.directory))
        return {"deleted": True}
