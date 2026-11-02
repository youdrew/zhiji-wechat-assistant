"""Owned persistence for assistant sessions; no access to WeChat source databases.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
Added 2026-10-10: versioned drafts, operations, native registry and resumable deletion.
Modified 2026-10-17 (Apache-2.0): durable latest-turn replacements retain old versions and their native recovery boundary.
Moved from bridge/advisor_session_store.py into the kyf.advisor plugin, 2026-10-13: the tables live in the plugin's
own ``advisor.sqlite3`` (copied there once by Core's migration).
Modified 2026-10-16 (Apache-2.0): schema v9 backs up message queues and tombstones their request identities on deletion.
Modified 2026-10-21 (Apache-2.0): schema v11 keeps selected @ friend identities with versioned drafts.
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from pathlib import Path

import context_snapshot
from contracts import AdvisorError, valid_draft_mentions

SCHEMA_VERSION = 11
BUSY_STATES = ("running", "unknown")
METADATA_KEYS = frozenset({"source_id", "model_fp", "template_revision", "context_epoch",
                           "profile_stamp", "native_source_id", "context_revision"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_selections (
 user TEXT NOT NULL, agent_id TEXT NOT NULL, thread_id TEXT NOT NULL,
 version INTEGER NOT NULL DEFAULT 1, updated_at_ms INTEGER NOT NULL,
 PRIMARY KEY(user,agent_id)
);
CREATE TABLE IF NOT EXISTS session_create_requests (
 user TEXT NOT NULL, agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
 thread_id TEXT NOT NULL, PRIMARY KEY(user,agent_id,request_id)
);
CREATE TABLE IF NOT EXISTS session_drafts (
 thread_id TEXT PRIMARY KEY REFERENCES threads(id) ON DELETE CASCADE,
 text TEXT NOT NULL DEFAULT '', scroll_top REAL NOT NULL DEFAULT 0,
 mentions_json TEXT NOT NULL DEFAULT '[]',
 version INTEGER NOT NULL DEFAULT 0, updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS session_usage (
 id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
 run_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
 UNIQUE(thread_id,run_id)
);
CREATE TABLE IF NOT EXISTS session_operations (
 id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, kind TEXT NOT NULL,
 request_id TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 metadata_json TEXT NOT NULL DEFAULT '{}', result_json TEXT,
 checkpoint_json TEXT, error TEXT, run_id TEXT,
 created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL,
 UNIQUE(thread_id,kind,request_id)
);
CREATE INDEX IF NOT EXISTS session_operations_thread ON session_operations(thread_id,created_at_ms);
CREATE TABLE IF NOT EXISTS session_native_registry (
 native_session_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
 metadata_json TEXT NOT NULL DEFAULT '{}', deleted INTEGER NOT NULL DEFAULT 0,
 created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS session_native_thread ON session_native_registry(thread_id);
CREATE TABLE IF NOT EXISTS session_recovery (
 id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
 summary TEXT NOT NULL, through_seq INTEGER NOT NULL, metadata_json TEXT NOT NULL,
 operation_id TEXT, created_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS session_recovery_thread ON session_recovery(thread_id,created_at_ms);
CREATE TABLE IF NOT EXISTS session_request_tombstones (
 user TEXT NOT NULL, agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
 thread_id TEXT NOT NULL, PRIMARY KEY(user,agent_id,request_id)
);
CREATE TABLE IF NOT EXISTS thread_participants (
 thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
 user TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', created_at_ms INTEGER NOT NULL,
 PRIMARY KEY(thread_id,user)
);
"""


def now_ms():
    return int(time.time() * 1000)


def _json(value):
    if not isinstance(value, dict):
        raise AdvisorError("invalid-request", "会话元数据格式不正确")
    try:
        result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (ValueError, TypeError) as exc:
        raise AdvisorError("invalid-request", "会话元数据格式不正确") from exc
    if len(result) > 2_000_000:
        raise AdvisorError("invalid-request", "会话元数据过长")
    return result


def _text(value, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise AdvisorError("invalid-request", "会话参数格式不正确")
    return value


def backup_before_migration(connection, database_path):
    """A SQLite backup includes committed WAL pages, before any schema mutation."""
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if not 0 < current < SCHEMA_VERSION:
        return None
    backup = Path(database_path).with_name("advisor.v%d-backup.sqlite3" % current)
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
            if temporary.exists():
                temporary.unlink()
    return backup


def ensure_schema(connection):
    connection.executescript(SCHEMA)
    added_title = False
    for table, name, definition in (
        ("threads", "title", "TEXT NOT NULL DEFAULT ''"),
        ("threads", "agent_name", "TEXT NOT NULL DEFAULT ''"),
        ("threads", "session_version", "INTEGER NOT NULL DEFAULT 1"),
        ("threads", "delete_state", "TEXT NOT NULL DEFAULT 'active'"),
        ("threads", "pending_replacement", "TEXT"),
        ("runs", "session_metadata_json", "TEXT"),
        ("runs", "superseded_by", "TEXT"),
        ("messages", "run_id", "TEXT"),
        ("messages", "superseded_by", "TEXT"),
        ("session_drafts", "mentions_json", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        columns = {row[1] for row in connection.execute("PRAGMA table_info(" + table + ")")}
        if name not in columns:
            connection.execute("ALTER TABLE " + table + " ADD COLUMN " + name + " " + definition)
            added_title = added_title or (table == "threads" and name == "title")
    if added_title:
        for row in connection.execute("SELECT id FROM threads").fetchall():
            first = connection.execute("SELECT text FROM messages WHERE thread_id=? AND role='user' ORDER BY seq LIMIT 1", (row["id"],)).fetchone()
            if first:
                connection.execute("UPDATE threads SET title=? WHERE id=?", (" ".join(first["text"].split())[:80], row["id"]))
    # Preserve existing native sessions for later ownership checks and deletion.
    for row in connection.execute("SELECT * FROM threads WHERE runtime_session_id IS NOT NULL").fetchall():
        metadata = {key: row[key] for key in METADATA_KEYS if key in row.keys()}
        metadata["native_source_id"] = row["source_id"]
        connection.execute(
            "INSERT OR IGNORE INTO session_native_registry(native_session_id,thread_id,metadata_json,created_at_ms,updated_at_ms) "
            "VALUES(?,?,?,?,?)", (row["runtime_session_id"], row["id"], _json(metadata), row["created_at_ms"], now_ms()))
    connection.execute("PRAGMA user_version=%d" % max(SCHEMA_VERSION, connection.execute("PRAGMA user_version").fetchone()[0]))
    # A transport dying does not establish whether native compaction committed.
    connection.execute("UPDATE session_operations SET state='unknown',version=version+1,updated_at_ms=? "
                       "WHERE state='running' AND kind='compact'", (now_ms(),))
    # Rebuilds leave the previous native binding intact until successful commit;
    # after restart their question can be retried against that durable binding.
    connection.execute("UPDATE session_operations SET state='interrupted',version=version+1,updated_at_ms=? "
                       "WHERE state='running' AND kind='rebuild'", (now_ms(),))
    connection.execute("UPDATE session_operations SET state='failed',error='清理中断，可重试',"
                       "version=version+1,updated_at_ms=? WHERE state='running' AND kind='delete'", (now_ms(),))


class SessionStoreMixin:
    """Methods run under AccountDatabase's reentrant lock and SQLite transaction."""

    def _session_require(self, connection, thread_id, user=None, agent_id=None, *, deleting=False):
        row = connection.execute("SELECT * FROM threads WHERE id=?", (thread_id,)).fetchone()
        if row is None or (user is not None and row["user"] != user) or (agent_id is not None and row["agent_id"] != agent_id):
            raise AdvisorError("thread-unknown", "会话不存在或归属不匹配")
        if row["delete_state"] != "active" and not deleting:
            raise AdvisorError("thread-unknown", "会话已删除或正在删除")
        return dict(row)

    def _session_assert_idle(self, connection, thread_id, *, run_id=None, operation_id=None):
        active = connection.execute("SELECT id FROM runs WHERE thread_id=? AND state='running' "
                                    "AND (? IS NULL OR id<>?) LIMIT 1", (thread_id, run_id, run_id)).fetchone()
        operation = connection.execute("SELECT id FROM session_operations WHERE thread_id=? "
                                       "AND state IN ('running','unknown') AND (? IS NULL OR id<>?) LIMIT 1",
                                       (thread_id, operation_id, operation_id)).fetchone()
        if active or operation:
            raise AdvisorError("run-active", "该会话仍有任务或结果待核对")

    def session_thread(self, user, agent_id, thread_id):
        with self.lock:
            return self._session_require(self._connect(), thread_id, user, agent_id)

    def session_threads(self, user, agent_id=None):
        query = "SELECT t.*,(SELECT COUNT(*) FROM messages m WHERE m.thread_id=t.id) AS message_count," \
                "(SELECT substr(text,1,160) FROM messages m WHERE m.thread_id=t.id AND role IN ('user','assistant') AND superseded_by IS NULL ORDER BY seq DESC LIMIT 1) AS preview " \
                "FROM threads t WHERE t.user=? AND t.delete_state<>'deleted'"
        args = [user]
        if agent_id is not None:
            query += " AND t.agent_id=?"
            args.append(agent_id)
        return self._rows(query + " ORDER BY t.updated_at_ms DESC,t.id DESC", args)

    def session_selected(self, user, agent_id):
        with self.lock:
            connection = self._connect()
            row = connection.execute("SELECT t.* FROM session_selections s JOIN threads t ON t.id=s.thread_id "
                                     "WHERE s.user=? AND s.agent_id=? AND t.user=s.user AND t.agent_id=s.agent_id "
                                     "AND t.delete_state='active'", (user, agent_id)).fetchone()
            if row:
                return dict(row)
            row = self.latest_thread(user, agent_id)
            if row:
                self.session_select(user, agent_id, row["id"])
            return row

    def session_select(self, user, agent_id, thread_id):
        with self.lock:
            connection = self._connect()
            with connection:
                row = self._session_require(connection, thread_id, user, agent_id)
                connection.execute("INSERT INTO session_selections(user,agent_id,thread_id,updated_at_ms) VALUES(?,?,?,?) "
                                   "ON CONFLICT(user,agent_id) DO UPDATE SET thread_id=excluded.thread_id,"
                                   "version=session_selections.version+1,updated_at_ms=excluded.updated_at_ms",
                                   (user, agent_id, thread_id, now_ms()))
            return row

    def session_rename(self, user, agent_id, thread_id, title, expected_version):
        title = _text(title, 120).strip()
        with self.lock:
            connection = self._connect()
            with connection:
                row = self._session_require(connection, thread_id, user, agent_id)
                if type(expected_version) is not int or row["session_version"] != expected_version:
                    raise AdvisorError("request-conflict", "会话已被修改，请刷新后重试")
                connection.execute("UPDATE threads SET title=?,session_version=session_version+1,updated_at_ms=? WHERE id=?",
                                   (title, now_ms(), thread_id))
            return self.thread(thread_id)

    def session_draft(self, user, agent_id, thread_id):
        with self.lock:
            connection = self._connect()
            self._session_require(connection, thread_id, user, agent_id)
            row = connection.execute("SELECT * FROM session_drafts WHERE thread_id=?", (thread_id,)).fetchone()
            if row is None:
                return {"thread_id": thread_id, "text": "", "scroll_top": 0, "version": 0,
                        "updated_at_ms": 0, "mentions": []}
            draft = dict(row)
            draft["mentions"] = valid_draft_mentions(json.loads(draft.pop("mentions_json")))
            return draft

    def session_save_draft(self, user, agent_id, thread_id, text, scroll_top=0, expected_version=0, mentions=None):
        _text(text, 16000, empty=True)
        mentions = valid_draft_mentions(mentions)
        if type(scroll_top) not in (float, int) or not math.isfinite(scroll_top) or not 0 <= scroll_top <= 1e10:
            raise AdvisorError("invalid-request", "滚动位置无效")
        with self.lock:
            connection = self._connect()
            with connection:
                previous = self.session_draft(user, agent_id, thread_id)
                if type(expected_version) is not int or previous["version"] != expected_version:
                    raise AdvisorError("request-conflict", "草稿已更新，请保留当前内容后重试")
                connection.execute("INSERT INTO session_drafts(thread_id,text,scroll_top,version,updated_at_ms,mentions_json) VALUES(?,?,?,?,?,?) "
                                   "ON CONFLICT(thread_id) DO UPDATE SET text=excluded.text,scroll_top=excluded.scroll_top,"
                                   "version=excluded.version,updated_at_ms=excluded.updated_at_ms,mentions_json=excluded.mentions_json",
                                   (thread_id, text, scroll_top, previous["version"] + 1, now_ms(),
                                    json.dumps(mentions, ensure_ascii=False, separators=(",", ":"))))
            return self.session_draft(user, agent_id, thread_id)

    def session_messages(self, user, agent_id, thread_id, before_seq=None, limit=60):
        if type(limit) is not int or not 1 <= limit <= 200 or (before_seq is not None and (type(before_seq) is not int or before_seq < 1)):
            raise AdvisorError("invalid-request", "消息分页参数无效")
        with self.lock:
            connection = self._connect()
            self._session_require(connection, thread_id, user, agent_id)
            rows = [dict(row) for row in connection.execute("SELECT * FROM messages WHERE thread_id=? "
                    "AND (? IS NULL OR seq<?) ORDER BY seq DESC LIMIT ?", (thread_id, before_seq, before_seq, limit + 1))]
            more = len(rows) > limit
            rows = list(reversed(rows[:limit]))
            for row in rows:
                if row["role"] != "user":
                    continue
                attempt = connection.execute("SELECT state,session_metadata_json FROM runs WHERE thread_id=? AND user_message_id=? "
                                             "ORDER BY created_at_ms DESC,rowid DESC LIMIT 1", (thread_id, row["id"])).fetchone()
                metadata = json.loads(attempt["session_metadata_json"] or "{}") if attempt else {}
                row["context_snapshot"] = context_snapshot.for_run(metadata.get("context_snapshot"), attempt["state"] if attempt else None)
                if "selected_mentions" in metadata:
                    row["selected_mentions"] = metadata["selected_mentions"]
            return {"messages": rows, "hasMore": more, "beforeSeq": rows[0]["seq"] if rows and more else None}

    def session_usage_save(self, thread_id, run_id, payload):
        encoded = _json(payload)
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id)
                run = connection.execute("SELECT thread_id FROM runs WHERE id=?", (run_id,)).fetchone()
                op = connection.execute("SELECT thread_id FROM session_operations WHERE id=?", (run_id,)).fetchone()
                if not (run or op) or (run or op)["thread_id"] != thread_id:
                    raise AdvisorError("request-conflict", "用量不属于该会话任务")
                connection.execute("INSERT INTO session_usage(id,thread_id,run_id,payload_json,created_at_ms) VALUES(?,?,?,?,?) "
                                   "ON CONFLICT(thread_id,run_id) DO UPDATE SET payload_json=excluded.payload_json",
                                   ("usage_" + uuid.uuid4().hex, thread_id, run_id, encoded, now_ms()))
            return self.session_latest_usage(thread_id)

    def session_latest_usage(self, thread_id):
        row = self._row("SELECT * FROM session_usage WHERE thread_id=? ORDER BY created_at_ms DESC,rowid DESC LIMIT 1", (thread_id,))
        if row:
            row["payload"] = json.loads(row.pop("payload_json"))
        return row

    def _session_operation_row(self, row):
        if row is None:
            return None
        result = dict(row)
        owner = self._row("SELECT user,agent_id FROM threads WHERE id=?", (result["thread_id"],))
        if owner:
            result.update(owner)
        for field in ("metadata", "result", "checkpoint"):
            raw = result.pop(field + "_json")
            result[field] = json.loads(raw) if raw is not None else None
        return result

    def session_operation(self, operation_id):
        return self._session_operation_row(self._row("SELECT * FROM session_operations WHERE id=?", (operation_id,)))

    def session_operations(self, thread_id):
        return [self._session_operation_row(row) for row in self._rows(
            "SELECT * FROM session_operations WHERE thread_id=? ORDER BY created_at_ms,id", (thread_id,))]

    def session_active_operation(self, thread_id):
        return self._session_operation_row(self._row("SELECT * FROM session_operations WHERE thread_id=? "
                    "AND state IN ('running','unknown') ORDER BY created_at_ms DESC LIMIT 1", (thread_id,)))

    def session_has_active_operations(self, user=None):
        query = ("SELECT 1 FROM session_operations o JOIN threads t ON t.id=o.thread_id "
                 "WHERE o.kind IN ('compact','rebuild') AND o.state IN ('running','unknown') "
                 "AND t.delete_state='active'")
        parameters = () if user is None else (user,)
        if user is not None:
            query += " AND t.user=?"
        return self._row(query + " LIMIT 1", parameters) is not None

    def session_operation_begin(self, user, agent_id, thread_id, kind, request_id, metadata=None, run_id=None):
        if kind not in ("compact", "rebuild", "delete"):
            raise AdvisorError("invalid-request", "会话操作类型无效")
        _text(request_id, 128)
        encoded = _json(metadata or {})
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id, user, agent_id)
                existing = connection.execute("SELECT * FROM session_operations WHERE thread_id=? AND kind=? AND request_id=?",
                                              (thread_id, kind, request_id)).fetchone()
                if existing:
                    return self._session_operation_row(existing)
                if run_id is not None:
                    run = connection.execute("SELECT thread_id,state FROM runs WHERE id=?", (run_id,)).fetchone()
                    automatic = kind == "compact" and ((metadata or {}).get("automatic") is True or (metadata or {}).get("reason") == "auto")
                    if not (kind == "rebuild" or automatic) or not run or run["thread_id"] != thread_id or run["state"] != "running":
                        raise AdvisorError("request-conflict", "恢复操作不属于当前任务")
                self._session_assert_idle(connection, thread_id, run_id=run_id)
                op_id, stamp = "op_" + uuid.uuid4().hex, now_ms()
                connection.execute("INSERT INTO session_operations(id,thread_id,kind,request_id,state,metadata_json,run_id,created_at_ms,updated_at_ms) "
                                   "VALUES(?,?,?,?,'running',?,?,?,?)", (op_id, thread_id, kind, request_id, encoded, run_id, stamp, stamp))
            return self.session_operation(op_id)

    def session_operation_update(self, operation_id, state=None, result=None, checkpoint=None, error=None, expected_version=None):
        allowed = {"running", "completed", "noop", "failed", "stopped", "unknown", "interrupted"}
        if state is not None and state not in allowed:
            raise AdvisorError("invalid-request", "会话操作状态无效")
        with self.lock:
            connection = self._connect()
            with connection:
                row = self.session_operation(operation_id)
                if row is None:
                    raise AdvisorError("not-found", "会话操作不存在")
                self._session_require(connection, row["thread_id"], deleting=row["kind"] == "delete")
                if expected_version is not None and row["version"] != expected_version:
                    raise AdvisorError("request-conflict", "操作状态已更新")
                if row["state"] in ("completed", "noop", "stopped"):
                    return row
                if state == "running" and row["state"] != "running":
                    self._session_assert_idle(connection, row["thread_id"], run_id=row["run_id"], operation_id=operation_id)
                connection.execute("UPDATE session_operations SET state=?,result_json=?,checkpoint_json=?,error=?,"
                                   "version=version+1,updated_at_ms=? WHERE id=?",
                                   (state or row["state"], _json(result) if result is not None else _json(row["result"]) if row["result"] is not None else None,
                                    _json(checkpoint) if checkpoint is not None else _json(row["checkpoint"]) if row["checkpoint"] is not None else None,
                                    error, now_ms(), operation_id))
            return self.session_operation(operation_id)

    def session_native_register(self, thread_id, native_session_id, metadata=None):
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_native_register_locked(connection, thread_id, native_session_id, metadata)
            return next(row for row in self.session_native_sessions(thread_id) if row["native_session_id"] == native_session_id)

    def _session_native_register_locked(self, connection, thread_id, native_session_id, metadata=None):
        _text(native_session_id, 256)
        thread = self._session_require(connection, thread_id)
        previous = connection.execute("SELECT * FROM session_native_registry WHERE native_session_id=?", (native_session_id,)).fetchone()
        if previous and (previous["thread_id"] != thread_id or previous["deleted"]):
            raise AdvisorError("request-conflict", "原生会话归属不匹配或已删除")
        values = {key: thread.get(key) for key in METADATA_KEYS if key in thread}
        values.setdefault("native_source_id", thread["source_id"])
        if previous:
            values.update(json.loads(previous["metadata_json"]))
        values.update(metadata or {})
        connection.execute("INSERT INTO session_native_registry(native_session_id,thread_id,metadata_json,created_at_ms,updated_at_ms) "
                           "VALUES(?,?,?,?,?) ON CONFLICT(native_session_id) DO UPDATE SET metadata_json=excluded.metadata_json,updated_at_ms=excluded.updated_at_ms",
                           (native_session_id, thread_id, _json(values), now_ms(), now_ms()))

    def session_native_sessions(self, thread_id, include_deleted=False):
        rows = self._rows("SELECT * FROM session_native_registry WHERE thread_id=?" + ("" if include_deleted else " AND deleted=0") +
                          " ORDER BY created_at_ms,native_session_id", (thread_id,))
        for row in rows:
            row["metadata"] = json.loads(row.pop("metadata_json"))
        return rows

    def session_native_deleted(self, thread_id, native_session_id):
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id, deleting=True)
                row = connection.execute("SELECT thread_id FROM session_native_registry WHERE native_session_id=?", (native_session_id,)).fetchone()
                if row is None or row["thread_id"] != thread_id:
                    raise AdvisorError("request-conflict", "原生会话归属不匹配")
                connection.execute("UPDATE session_native_registry SET deleted=1,metadata_json='{}',updated_at_ms=? WHERE native_session_id=?",
                                   (now_ms(), native_session_id))
        return True

    def session_bind_commit(self, thread_id, native_session_id, metadata):
        _json(metadata)
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id)
                self._session_native_register_locked(connection, thread_id, native_session_id, metadata)
                fields = [key for key in ("source_id", "model_fp", "template_revision", "context_epoch", "profile_stamp") if key in metadata]
                assignments = [key + "=?" for key in fields] + ["runtime_session_id=?", "pending_replacement=NULL"]
                connection.execute("UPDATE threads SET " + ",".join(assignments) + " WHERE id=?",
                                   [metadata[key] for key in fields] + [native_session_id, thread_id])
            return self.thread(thread_id)

    def session_run_metadata(self, run_id, **metadata):
        with self.lock:
            connection = self._connect()
            row = connection.execute("SELECT thread_id,session_metadata_json FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise AdvisorError("run-unknown", "任务不存在")
            self._session_require(connection, row["thread_id"])
            previous = json.loads(row["session_metadata_json"] or "{}")
            if metadata:
                if "context_snapshot" in metadata:
                    next_snapshot = context_snapshot.public(metadata["context_snapshot"])
                    current_snapshot = context_snapshot.public(previous.get("context_snapshot"))
                    if (next_snapshot is None or (current_snapshot is not None and
                            (not context_snapshot.same_scope(current_snapshot, next_snapshot) or
                             (current_snapshot["state"] == "recorded" and current_snapshot != next_snapshot)))):
                        raise AdvisorError("request-conflict", "本轮上下文快照已固定")
                    metadata["context_snapshot"] = next_snapshot
                previous.update(metadata)
                with connection:
                    connection.execute("UPDATE runs SET session_metadata_json=? WHERE id=?", (_json(previous), run_id))
            return previous

    def session_recovery_save(self, thread_id, summary, through_seq, metadata, operation_id=None):
        _text(summary, 2_000_000)
        _json(metadata)
        if type(through_seq) is not int or through_seq < 1:
            raise AdvisorError("invalid-request", "摘要覆盖边界无效")
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id)
                boundary = connection.execute("SELECT id FROM messages WHERE thread_id=? AND seq=?", (thread_id, through_seq)).fetchone()
                if boundary is None:
                    raise AdvisorError("request-conflict", "摘要覆盖边界不存在")
                if operation_id is not None:
                    op = self.session_operation(operation_id)
                    if not op or op["thread_id"] != thread_id or op["state"] in ("failed", "stopped"):
                        raise AdvisorError("request-conflict", "摘要不属于有效操作")
                    existing = connection.execute("SELECT * FROM session_recovery WHERE operation_id=? AND thread_id=?", (operation_id, thread_id)).fetchone()
                    if existing:
                        if existing["summary"] != summary or existing["through_seq"] != through_seq:
                            raise AdvisorError("request-conflict", "同一操作的摘要已保存")
                        return self._session_recovery_row(existing)
                values = dict(metadata)
                values["boundary_message_id"] = boundary["id"]
                identifier = "recovery_" + uuid.uuid4().hex
                connection.execute("INSERT INTO session_recovery(id,thread_id,summary,through_seq,metadata_json,operation_id,created_at_ms) VALUES(?,?,?,?,?,?,?)",
                                   (identifier, thread_id, summary, through_seq, _json(values), operation_id, now_ms()))
            return self.session_recovery_latest(thread_id)

    @staticmethod
    def _session_recovery_row(row):
        if not row:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        return result

    def session_recovery_latest(self, thread_id):
        return self._session_recovery_row(self._row("SELECT * FROM session_recovery WHERE thread_id=? ORDER BY created_at_ms DESC,rowid DESC LIMIT 1", (thread_id,)))

    def session_delete_begin(self, user, agent_id, thread_id, request_id):
        with self.lock:
            connection = self._connect()
            with connection:
                thread = self._session_require(connection, thread_id, user, agent_id, deleting=True)
                previous = connection.execute("SELECT * FROM session_operations WHERE thread_id=? AND kind='delete' ORDER BY created_at_ms DESC LIMIT 1", (thread_id,)).fetchone()
                if previous:
                    op = self._session_operation_row(previous)
                else:
                    _text(request_id, 128)
                    self._session_assert_idle(connection, thread_id)
                    if connection.execute("SELECT 1 FROM session_queue_items WHERE thread_id=? AND state='starting'", (thread_id,)).fetchone():
                        raise AdvisorError("run-active", "该会话有消息正在开始处理")
                    identifier, stamp = "op_" + uuid.uuid4().hex, now_ms()
                    connection.execute("INSERT INTO session_operations(id,thread_id,kind,request_id,state,created_at_ms,updated_at_ms) "
                                       "VALUES(?,?,'delete',?,'running',?,?)", (identifier, thread_id, request_id, stamp, stamp))
                    connection.execute("UPDATE threads SET delete_state='pending',session_version=session_version+1 WHERE id=?", (thread_id,))
                    selected = connection.execute("SELECT thread_id FROM session_selections WHERE user=? AND agent_id=?", (user, agent_id)).fetchone()
                    if selected and selected["thread_id"] == thread_id:
                        next_row = connection.execute("SELECT id FROM threads WHERE user=? AND agent_id=? AND delete_state='active' "
                                                      "ORDER BY updated_at_ms DESC,rowid DESC LIMIT 1", (user, agent_id)).fetchone()
                        if next_row:
                            connection.execute("UPDATE session_selections SET thread_id=?,version=version+1,updated_at_ms=? WHERE user=? AND agent_id=?",
                                               (next_row["id"], now_ms(), user, agent_id))
                        else:
                            connection.execute("DELETE FROM session_selections WHERE user=? AND agent_id=?", (user, agent_id))
                    op = self.session_operation(identifier)
            return {**op, "nativeSessions": self.session_native_sessions(thread_id)}

    def session_delete_finish(self, operation_id, error=None):
        with self.lock:
            connection = self._connect()
            with connection:
                op = self.session_operation(operation_id)
                if not op or op["kind"] != "delete":
                    raise AdvisorError("not-found", "删除操作不存在")
                thread = self._session_require(connection, op["thread_id"], deleting=True)
                if op["state"] == "completed":
                    return op
                remaining = connection.execute("SELECT 1 FROM session_native_registry WHERE thread_id=? AND deleted=0 LIMIT 1", (thread["id"],)).fetchone()
                if remaining and not error:
                    error = "原生会话尚未清理完成"
                if error:
                    connection.execute("UPDATE threads SET delete_state='failed' WHERE id=?", (thread["id"],))
                    connection.execute("UPDATE session_operations SET state='failed',error=?,version=version+1,updated_at_ms=? WHERE id=?",
                                       (_text(error, 2000), now_ms(), operation_id))
                else:
                    self._queue_delete_locked(connection, thread)
                    connection.execute("INSERT OR IGNORE INTO session_request_tombstones(user,agent_id,request_id,thread_id) "
                                       "SELECT user,agent_id,request_id,? FROM requests WHERE run_id IN (SELECT id FROM runs WHERE thread_id=?)",
                                       (thread["id"], thread["id"]))
                    for table in ("messages", "runs", "session_drafts", "session_usage", "session_recovery", "session_native_registry",
                                  "thread_participants"):
                        connection.execute("DELETE FROM " + table + " WHERE thread_id=?", (thread["id"],))
                    connection.execute("DELETE FROM session_operations WHERE thread_id=? AND id<>?", (thread["id"], operation_id))
                    connection.execute("UPDATE threads SET delete_state='deleted',title='',agent_name='',runtime_session_id=NULL,pending_replacement=NULL,"
                                       "model_fp=NULL,context_epoch=NULL,profile_stamp=NULL,source_id='',template_revision=NULL WHERE id=?", (thread["id"],))
                    connection.execute("UPDATE session_operations SET state='completed',metadata_json='{}',result_json=NULL,checkpoint_json=NULL,"
                                       "error=NULL,run_id=NULL,version=version+1,updated_at_ms=? WHERE id=?", (now_ms(), operation_id))
            return self.session_operation(operation_id)

    def session_retry_run(self, run_id, request_id):
        """A new attempt reuses the saved user message, never the old running identity."""
        _text(request_id, 128)
        with self.lock:
            connection = self._connect()
            with connection:
                old = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                if old is None:
                    raise AdvisorError("run-unknown", "任务不存在")
                thread = self._session_require(connection, old["thread_id"])
                message = connection.execute("SELECT * FROM messages WHERE id=? AND thread_id=?", (old["user_message_id"], thread["id"])).fetchone()
                if not message:
                    raise AdvisorError("request-conflict", "原问题无法恢复")
                existing = self.scoped_request(thread["user"], thread["agent_id"], request_id, message["text"])
                if existing:
                    if existing["thread_id"] != thread["id"]:
                        raise AdvisorError("request-conflict", "重试请求归属不匹配")
                    return existing, None
                if old["superseded_by"] or message["superseded_by"]:
                    raise AdvisorError("request-conflict", "这一轮已经被替换，不能重试旧版本")
                if old["state"] not in ("error", "stopped", "cancelled"):
                    raise AdvisorError("request-conflict", "该任务不需要重试")
                answered = connection.execute("SELECT 1 FROM runs WHERE thread_id=? AND user_message_id=? AND state='done' AND superseded_by IS NULL LIMIT 1",
                                              (thread["id"], message["id"])).fetchone()
                if answered:
                    raise AdvisorError("request-conflict", "该问题已生成完成，不再重复重试")
                self._session_assert_idle(connection, thread["id"])
                new_id, stamp = "run_" + uuid.uuid4().hex, now_ms()
                metadata = json.loads(old["session_metadata_json"] or "{}")
                # Each attempt finalizes availability once. The service inherits
                # the original frozen identities, never an already finalized object.
                metadata.pop("context_snapshot", None)
                connection.execute("INSERT INTO runs(id,thread_id,request_id,user_message_id,state,error,trigger,template_revision,context_revision,source_id,created_at_ms,updated_at_ms,session_metadata_json) "
                                   "VALUES(?,?,?,?,'running',NULL,?,?,?,?,?,?,?)",
                                   (new_id, thread["id"], request_id, message["id"], old["trigger"], old["template_revision"], old["context_revision"], old["source_id"], stamp, stamp, _json(metadata)))
                connection.execute("INSERT INTO requests(user,agent_id,request_id,message,run_id) VALUES(?,?,?,?,?)",
                                   (thread["user"], thread["agent_id"], request_id, message["text"], new_id))
                self._touch_thread(connection, thread["id"], stamp)
            return self.run(new_id), dict(message)

    def session_replace_run(self, run_id, request_id, mode, message=None):
        """Regenerate the latest answer, or ask the latest question again in other words (added 2026-10-15).

        Only the discussion's latest turn can be replaced, while nothing runs. The replaced messages stay in the
        transcript marked ``superseded_by`` the new run, so nothing said is lost; context and recovery skip them.
        """
        if mode not in ("regenerate", "edit"):
            raise AdvisorError("invalid-request", "替换方式不正确")
        _text(request_id, 128)
        with self.lock:
            connection = self._connect()
            with connection:
                old = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
                if old is None:
                    raise AdvisorError("run-unknown", "任务不存在")
                thread = self._session_require(connection, old["thread_id"])
                question = connection.execute("SELECT * FROM messages WHERE id=? AND thread_id=?",
                                              (old["user_message_id"], thread["id"])).fetchone()
                if question is None or question["role"] != "user":
                    raise AdvisorError("request-conflict", "原问题无法恢复")
                text = question["text"] if mode == "regenerate" else _text(message, 16000)
                if mode == "regenerate" and message != text:
                    raise AdvisorError("request-conflict", "重新生成的问题与原记录不一致")
                existing = self.scoped_request(thread["user"], thread["agent_id"], request_id, text)
                if existing is not None:
                    if existing["thread_id"] != thread["id"]:
                        raise AdvisorError("request-conflict", "请求归属不匹配")
                    if json.loads(existing.get("session_metadata_json") or "{}").get("replacement") != {"mode": mode, "run_id": run_id}:
                        raise AdvisorError("request-conflict", "该请求编号已经用于另一种提问操作")
                    return existing, None
                if question["superseded_by"] or old["superseded_by"]:
                    raise AdvisorError("request-conflict", "这一轮已经被替换")
                latest = connection.execute("SELECT id FROM runs WHERE thread_id=? ORDER BY created_at_ms DESC,rowid DESC LIMIT 1",
                                            (thread["id"],)).fetchone()
                last_question = connection.execute("SELECT id FROM messages WHERE thread_id=? AND role='user' AND superseded_by IS NULL "
                                                   "ORDER BY seq DESC LIMIT 1", (thread["id"],)).fetchone()
                if latest["id"] != run_id or last_question is None or last_question["id"] != question["id"]:
                    raise AdvisorError("request-conflict", "只能重新生成或修改最近一轮")
                if old["state"] not in (("done",) if mode == "regenerate" else ("done", "error", "stopped")):
                    raise AdvisorError("request-conflict", "这一轮还不能重新生成")
                self._session_assert_idle(connection, thread["id"])
                if self._queue_snapshot(connection, thread["id"])["items"]:
                    raise AdvisorError("run-active", "回答和排队的问题完成后才能重新生成或修改")
                new_id, stamp = "run_" + uuid.uuid4().hex, now_ms()
                previous = json.loads(old["session_metadata_json"] or "{}")
                metadata = {"replacement": {"mode": mode, "run_id": run_id},
                            "pending_revert": previous.get("pending_revert") or {
                                "messageId": previous.get("native_message_id"),
                                "runtimeSessionId": previous.get("runtime_session_id")}}
                connection.execute("UPDATE messages SET superseded_by=? WHERE thread_id=? AND seq>=? AND superseded_by IS NULL",
                                   (new_id, thread["id"], question["seq"] + (1 if mode == "regenerate" else 0)))
                connection.execute("UPDATE runs SET superseded_by=? WHERE id=?", (new_id, run_id))
                connection.execute("UPDATE threads SET pending_replacement=? WHERE id=?", (new_id, thread["id"]))
                connection.execute("INSERT INTO runs(id,thread_id,request_id,user_message_id,state,error,trigger,template_revision,"
                                   "context_revision,source_id,created_at_ms,updated_at_ms,session_metadata_json) VALUES(?,?,?,?,'running',NULL,?,?,?,?,?,?,?)",
                                   (new_id, thread["id"], request_id, question["id"] if mode == "regenerate" else None, old["trigger"],
                                    old["template_revision"], old["context_revision"], old["source_id"], stamp, stamp, _json(metadata)))
                if mode == "regenerate":
                    asked = dict(question)
                else:
                    asked = self._append_message(thread["id"], "user", text, run_id=new_id)
                    connection.execute("UPDATE runs SET user_message_id=? WHERE id=?", (asked["id"], new_id))
                connection.execute("INSERT INTO requests(user,agent_id,request_id,message,run_id) VALUES(?,?,?,?,?)",
                                   (thread["user"], thread["agent_id"], request_id, text, new_id))
                self._touch_thread(connection, thread["id"], stamp)
            return self.run(new_id), asked

    # Friends @-mentioned in a discussion stay in it until removed; their names are those at the mention.
    def session_participants(self, thread_id):
        return [{"user": row["user"], "name": row["name"]} for row in self._rows(
            "SELECT user,name FROM thread_participants WHERE thread_id=? ORDER BY created_at_ms,rowid", (thread_id,))]

    def session_add_participants(self, thread_id, participants):
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id)
                stamp = now_ms()
                for item in participants:
                    connection.execute("INSERT INTO thread_participants(thread_id,user,name,created_at_ms) VALUES(?,?,?,?) "
                                       "ON CONFLICT(thread_id,user) DO UPDATE SET name=excluded.name",
                                       (thread_id, item["user"], _text(item["name"], 120), stamp))
            return self.session_participants(thread_id)

    def session_remove_participant(self, user, agent_id, thread_id, participant):
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id, user, agent_id)
                connection.execute("DELETE FROM thread_participants WHERE thread_id=? AND user=?", (thread_id, participant))
            return self.session_participants(thread_id)
