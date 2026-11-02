"""Durable, per-discussion outgoing messages. No provider or Core calls.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Acceptance identities outlive edits, removal and dispatch. Only a fingerprint of
the original request is retained after removal; deleting a discussion replaces
all its identities with the existing session request tombstones.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from contracts import AdvisorError, valid_identifier, valid_mentions, valid_request_id, valid_user_message

SCHEMA = """
CREATE TABLE IF NOT EXISTS session_queue_state (
 thread_id TEXT PRIMARY KEY REFERENCES threads(id) ON DELETE CASCADE,
 paused INTEGER NOT NULL DEFAULT 0, reason TEXT,
 version INTEGER NOT NULL DEFAULT 1, updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS session_queue_items (
 id TEXT PRIMARY KEY,
 thread_id TEXT NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
 user TEXT NOT NULL, agent_id TEXT NOT NULL, request_id TEXT NOT NULL,
 accepted_hash TEXT NOT NULL, dispatch_request_id TEXT NOT NULL UNIQUE,
 message TEXT NOT NULL, thinking INTEGER NOT NULL, mentions_json TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', version INTEGER NOT NULL DEFAULT 1,
 run_id TEXT, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL,
 UNIQUE(user,agent_id,request_id)
);
CREATE INDEX IF NOT EXISTS session_queue_thread ON session_queue_items(thread_id,state,created_at_ms);
"""


def _now():
    return int(time.time() * 1000)


def ensure_schema(connection):
    connection.executescript(SCHEMA)
    # Preserve an intentionally paused empty queue, but do not turn every idle
    # discussion into a paused one merely because the application was reopened.
    interrupted = [row[0] for row in connection.execute(
        "SELECT thread_id FROM session_queue_items WHERE state IN ('pending','starting') "
        "UNION SELECT thread_id FROM runs WHERE state='running'").fetchall()]
    # A claim is written before create_run, which in turn is written before any
    # model call. The dispatch request identity bridges either crash boundary.
    for item in connection.execute("SELECT * FROM session_queue_items WHERE state='starting'").fetchall():
        run = connection.execute("SELECT id FROM runs WHERE thread_id=? AND request_id=?",
                                 (item["thread_id"], item["dispatch_request_id"])).fetchone()
        connection.execute(
            "UPDATE session_queue_items SET state=?,run_id=?,message=CASE WHEN ? THEN '' ELSE message END,"
            "mentions_json=CASE WHEN ? THEN '[]' ELSE mentions_json END,version=version+1,updated_at_ms=? WHERE id=?",
            ("dispatched" if run else "pending", run["id"] if run else None, bool(run), bool(run), _now(), item["id"]))
    # Restart never silently sends a saved question or replays an interrupted run.
    for thread_id in interrupted:
        connection.execute("UPDATE session_queue_state SET reason='restart',paused=1,"
                           "version=version+1,updated_at_ms=? WHERE thread_id=?", (_now(), thread_id))


def _payload(message, thinking, mentions):
    message = valid_user_message(message)
    if not isinstance(thinking, bool):
        raise AdvisorError("invalid-request", "思考模式参数不正确")
    mentions = valid_mentions(mentions)
    payload = json.dumps([message, thinking, mentions], ensure_ascii=False, separators=(",", ":"))
    return message, mentions, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _version(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise AdvisorError("invalid-request", "队列版本格式不正确")


def _identifier(value):
    if not valid_identifier(value):
        raise AdvisorError("invalid-request", "待处理消息编号格式不正确")


def _item(row):
    if row is None:
        return None
    return {"id": row["id"], "requestId": row["request_id"],
            "dispatchRequestId": row["dispatch_request_id"], "message": row["message"],
            "thinking": bool(row["thinking"]), "mentions": json.loads(row["mentions_json"]),
            "state": row["state"], "version": row["version"], "runId": row["run_id"],
            "createdAtMs": row["created_at_ms"]}


class QueueStoreMixin:
    def _queue_snapshot(self, connection, thread_id):
        row = connection.execute("SELECT * FROM session_queue_state WHERE thread_id=?", (thread_id,)).fetchone()
        return {"paused": bool(row["paused"]) if row else False,
                "reason": row["reason"] if row else None, "version": row["version"] if row else 0,
                "items": [_item(item) for item in connection.execute(
                    "SELECT * FROM session_queue_items WHERE thread_id=? AND state IN ('pending','starting') "
                    "ORDER BY rowid", (thread_id,))]}

    def _queue_touch(self, connection, thread_id):
        connection.execute("UPDATE session_queue_state SET version=version+1,updated_at_ms=? WHERE thread_id=?", (_now(), thread_id))

    def _queue_ensure(self, connection, thread_id):
        connection.execute("INSERT OR IGNORE INTO session_queue_state(thread_id,updated_at_ms) VALUES(?,?)", (thread_id, _now()))

    def _queue_request(self, connection, user, agent_id, thread_id, request_id, fingerprint):
        if connection.execute("SELECT 1 FROM session_request_tombstones WHERE user=? AND agent_id=? AND request_id=?",
                              (user, agent_id, request_id)).fetchone():
            raise AdvisorError("thread-unknown", "该请求所属会话已删除")
        row = connection.execute("SELECT * FROM session_queue_items WHERE user=? AND agent_id=? AND request_id=?",
                                 (user, agent_id, request_id)).fetchone()
        if row and (row["thread_id"] != thread_id or row["accepted_hash"] != fingerprint):
            raise AdvisorError("request-conflict", "该请求编号已经用于另一条消息")
        return _item(row)

    def queue_find_request(self, user, agent_id, thread_id, request_id, message, *, thinking=True, mentions=None):
        request_id = valid_request_id(request_id)
        if request_id is None:
            raise AdvisorError("invalid-request", "消息排队需要请求编号")
        _message, _mentions, fingerprint = _payload(message, thinking, mentions)
        with self.lock:
            connection = self._connect()
            self._session_require(connection, thread_id, user, agent_id)
            return self._queue_request(connection, user, agent_id, thread_id, request_id, fingerprint)

    def queue_get(self, user, agent_id, thread_id):
        with self.lock:
            connection = self._connect()
            self._session_require(connection, thread_id, user, agent_id)
            return self._queue_snapshot(connection, thread_id)

    def queue_enqueue(self, user, agent_id, thread_id, message, request_id, *, thinking=True, mentions=None):
        request_id = valid_request_id(request_id)
        if request_id is None:
            raise AdvisorError("invalid-request", "消息排队需要请求编号")
        message, mentions, fingerprint = _payload(message, thinking, mentions)
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id, user, agent_id)
                self._queue_ensure(connection, thread_id)
                previous = self._queue_request(connection, user, agent_id, thread_id, request_id, fingerprint)
                if previous:
                    return {"item": previous, "queue": self._queue_snapshot(connection, thread_id), "duplicate": True}
                # Acceptance and direct-run identities share a namespace; neither
                # route can turn a retransmission into a second question.
                if connection.execute("SELECT 1 FROM requests WHERE user=? AND agent_id=? AND request_id=?",
                                      (user, agent_id, request_id)).fetchone():
                    raise AdvisorError("request-conflict", "该请求编号已经用于生成任务")
                identifier, dispatch_id, stamp = "queued_" + uuid.uuid4().hex, "qdispatch_" + uuid.uuid4().hex, _now()
                connection.execute(
                    "INSERT INTO session_queue_items(id,thread_id,user,agent_id,request_id,accepted_hash,dispatch_request_id,"
                    "message,thinking,mentions_json,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (identifier, thread_id, user, agent_id, request_id, fingerprint, dispatch_id, message, thinking,
                     json.dumps(mentions, ensure_ascii=False), stamp, stamp))
                self._queue_touch(connection, thread_id)
                row = connection.execute("SELECT * FROM session_queue_items WHERE id=?", (identifier,)).fetchone()
                return {"item": _item(row), "queue": self._queue_snapshot(connection, thread_id), "duplicate": False}

    def _queue_pending(self, connection, user, agent_id, thread_id, item_id, expected_version):
        _identifier(item_id)
        _version(expected_version)
        self._session_require(connection, thread_id, user, agent_id)
        row = connection.execute("SELECT * FROM session_queue_items WHERE id=? AND thread_id=?", (item_id, thread_id)).fetchone()
        if not row:
            raise AdvisorError("queue-item-unknown", "待处理消息不存在")
        if row["state"] != "pending":
            raise AdvisorError("queue-item-started", "该消息已开始处理或已移除")
        if row["version"] != expected_version:
            raise AdvisorError("queue-conflict", "待处理消息已更新，请刷新后重试")
        return row

    def queue_edit(self, user, agent_id, thread_id, item_id, message, expected_version):
        message = valid_user_message(message)
        with self.lock:
            connection = self._connect()
            with connection:
                self._queue_pending(connection, user, agent_id, thread_id, item_id, expected_version)
                changed = connection.execute("UPDATE session_queue_items SET message=?,version=version+1,updated_at_ms=? "
                                             "WHERE id=? AND state='pending' AND version=?",
                                             (message, _now(), item_id, expected_version)).rowcount
                if not changed:
                    raise AdvisorError("queue-conflict", "待处理消息已更新，请刷新后重试")
                self._queue_touch(connection, thread_id)
                return _item(connection.execute("SELECT * FROM session_queue_items WHERE id=?", (item_id,)).fetchone())

    def queue_remove(self, user, agent_id, thread_id, item_id, expected_version):
        with self.lock:
            connection = self._connect()
            with connection:
                self._queue_pending(connection, user, agent_id, thread_id, item_id, expected_version)
                changed = connection.execute("UPDATE session_queue_items SET state='removed',message='',mentions_json='[]',"
                                             "version=version+1,updated_at_ms=? WHERE id=? AND state='pending' AND version=?",
                                             (_now(), item_id, expected_version)).rowcount
                if not changed:
                    raise AdvisorError("queue-conflict", "待处理消息已更新，请刷新后重试")
                self._queue_touch(connection, thread_id)
                return _item(connection.execute("SELECT * FROM session_queue_items WHERE id=?", (item_id,)).fetchone())

    def queue_set_paused(self, user, agent_id, thread_id, paused, reason=None, expected_version=None):
        if not isinstance(paused, bool) or (reason is not None and (not isinstance(reason, str) or len(reason) > 200)):
            raise AdvisorError("invalid-request", "队列暂停参数不正确")
        if expected_version is not None:
            _version(expected_version)
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id, user, agent_id)
                current = self._queue_snapshot(connection, thread_id)
                if expected_version is not None and current["version"] != expected_version:
                    raise AdvisorError("queue-conflict", "队列已更新，请刷新后重试")
                self._queue_ensure(connection, thread_id)
                connection.execute("UPDATE session_queue_state SET paused=?,reason=?,version=version+1,updated_at_ms=? WHERE thread_id=?",
                                   (paused, reason if paused else None, _now(), thread_id))
                return self._queue_snapshot(connection, thread_id)

    def queue_claim(self, user, agent_id, thread_id):
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id, user, agent_id)
                # Take SQLite's write lock before inspecting readiness. Together
                # with the process lock this serializes claims against all edits.
                connection.execute("UPDATE session_queue_state SET version=version WHERE thread_id=?", (thread_id,))
                state = connection.execute("SELECT paused FROM session_queue_state WHERE thread_id=?", (thread_id,)).fetchone()
                if not state or state["paused"]:
                    return None
                if connection.execute("SELECT 1 FROM session_queue_items WHERE thread_id=? AND state='starting'", (thread_id,)).fetchone():
                    return None
                try:
                    self._session_assert_idle(connection, thread_id)
                except AdvisorError as exc:
                    if exc.code == "run-active":
                        return None
                    raise
                row = connection.execute("SELECT * FROM session_queue_items WHERE thread_id=? AND state='pending' ORDER BY rowid LIMIT 1", (thread_id,)).fetchone()
                if not row:
                    return None
                connection.execute("UPDATE session_queue_items SET state='starting',version=version+1,updated_at_ms=? WHERE id=?", (_now(), row["id"]))
                self._queue_touch(connection, thread_id)
                return _item(connection.execute("SELECT * FROM session_queue_items WHERE id=?", (row["id"],)).fetchone())

    def queue_dispatched(self, user, agent_id, thread_id, item_id, run_id):
        _identifier(item_id)
        _identifier(run_id)
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id, user, agent_id)
                row = connection.execute("SELECT * FROM session_queue_items WHERE id=? AND thread_id=?", (item_id, thread_id)).fetchone()
                run = connection.execute("SELECT * FROM runs WHERE id=? AND thread_id=?", (run_id, thread_id)).fetchone()
                if not row or not run or run["request_id"] != row["dispatch_request_id"]:
                    raise AdvisorError("queue-conflict", "排队消息与生成任务不匹配")
                if row["state"] == "dispatched" and row["run_id"] == run_id:
                    return _item(row)
                if row["state"] != "starting":
                    raise AdvisorError("queue-conflict", "排队消息未领取")
                connection.execute("UPDATE session_queue_items SET state='dispatched',run_id=?,message='',mentions_json='[]',"
                                   "version=version+1,updated_at_ms=? WHERE id=?", (run_id, _now(), item_id))
                self._queue_touch(connection, thread_id)
                return _item(connection.execute("SELECT * FROM session_queue_items WHERE id=?", (item_id,)).fetchone())

    def queue_release(self, user, agent_id, thread_id, item_id, reason):
        """Only release a pre-run claim; any persisted run makes replay unsafe."""
        _identifier(item_id)
        with self.lock:
            connection = self._connect()
            with connection:
                self._session_require(connection, thread_id, user, agent_id)
                row = connection.execute("SELECT * FROM session_queue_items WHERE id=? AND thread_id=?", (item_id, thread_id)).fetchone()
                if not row or row["state"] != "starting":
                    raise AdvisorError("queue-conflict", "排队消息未领取")
                run = connection.execute("SELECT id FROM runs WHERE thread_id=? AND request_id=?", (thread_id, row["dispatch_request_id"])).fetchone()
                if run:
                    connection.execute("UPDATE session_queue_items SET state='dispatched',run_id=?,message='',mentions_json='[]',"
                                       "version=version+1,updated_at_ms=? WHERE id=?", (run["id"], _now(), item_id))
                else:
                    connection.execute("UPDATE session_queue_items SET state='pending',version=version+1,updated_at_ms=? WHERE id=?", (_now(), item_id))
                connection.execute("UPDATE session_queue_state SET paused=1,reason=?,version=version+1,updated_at_ms=? WHERE thread_id=?",
                                   (str(reason)[:200], _now(), thread_id))
                return _item(connection.execute("SELECT * FROM session_queue_items WHERE id=?", (item_id,)).fetchone())

    def _queue_delete_locked(self, connection, thread):
        for column in ("request_id", "dispatch_request_id"):
            connection.execute("INSERT OR IGNORE INTO session_request_tombstones(user,agent_id,request_id,thread_id) "
                               "SELECT user,agent_id," + column + ",thread_id FROM session_queue_items WHERE thread_id=?", (thread["id"],))
        connection.execute("DELETE FROM session_queue_items WHERE thread_id=?", (thread["id"],))
        connection.execute("DELETE FROM session_queue_state WHERE thread_id=?", (thread["id"],))
