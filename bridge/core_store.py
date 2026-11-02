"""Core registry of plugin tasks, executions and event delivery state.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

``.local/core/core.sqlite3`` belongs to Core alone. Plugins never receive this
connection; they reach tasks through the plugin interface with tokens whose
hashes are kept here.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

TERMINAL = frozenset(("done", "partial", "failed", "stopped", "interrupted"))
ACTIVE = frozenset(("queued", "running", "stopping"))
STATES = TERMINAL | ACTIVE
KINDS = ("scheme", "background", "interactive")
SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  plugin TEXT NOT NULL,
  plugin_version TEXT NOT NULL,
  kind TEXT NOT NULL,
  account TEXT NOT NULL,
  user TEXT,
  scheme TEXT,
  title TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL,
  progress TEXT,
  error TEXT,
  after_task TEXT,
  created_at_ms INTEGER NOT NULL,
  started_at_ms INTEGER,
  finished_at_ms INTEGER,
  updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tasks_scope ON tasks(account, user, created_at_ms DESC);
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state);
CREATE TABLE IF NOT EXISTS executions (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  generation INTEGER NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL,
  started_at_ms INTEGER NOT NULL,
  ended_at_ms INTEGER,
  UNIQUE(task_id, generation)
);
CREATE TABLE IF NOT EXISTS subscriptions (
  plugin TEXT NOT NULL,
  account TEXT NOT NULL,
  user TEXT NOT NULL,
  event TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY(plugin, account, user, event)
);
CREATE TABLE IF NOT EXISTS event_positions (
  plugin TEXT NOT NULL,
  account TEXT NOT NULL,
  user TEXT NOT NULL,
  position TEXT NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  PRIMARY KEY(plugin, account, user)
);
CREATE TABLE IF NOT EXISTS task_attention_seen (
  task_id TEXT PRIMARY KEY,
  seen_at_ms INTEGER NOT NULL
);
"""
TASK_COLUMNS = ("id", "plugin", "plugin_version", "kind", "account", "user", "scheme", "title", "params", "state",
                "progress", "error", "after_task", "created_at_ms", "started_at_ms", "finished_at_ms", "updated_at_ms")


def now_ms():
    return int(time.time() * 1000)


def _json(value):
    return None if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def task_view(row):
    """The public projection of one task row."""
    if row is None:
        return None
    item = dict(zip(TASK_COLUMNS, row)) if not isinstance(row, dict) else dict(row)
    return {
        "id": item["id"], "plugin": item["plugin"], "pluginVersion": item["plugin_version"], "kind": item["kind"],
        "account": item["account"], "user": item["user"], "scheme": item["scheme"], "title": item["title"],
        "params": _load(item["params"], {}), "state": item["state"], "progress": _load(item["progress"]),
        "error": _load(item["error"]), "afterTask": item["after_task"], "createdAt": item["created_at_ms"],
        "startedAt": item["started_at_ms"], "finishedAt": item["finished_at_ms"], "updatedAt": item["updated_at_ms"],
    }


class CoreStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(SCHEMA)

    def close(self):
        with self.lock:
            self.connection.close()

    def _write(self, statements):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                result = statements(self.connection)
                self.connection.execute("COMMIT")
                return result
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise

    # Tasks -------------------------------------------------------------------------------------------------------
    def insert_task(self, task):
        stamp = now_ms()
        values = {"params": "{}", "progress": None, "error": None, "after_task": None, "started_at_ms": None,
                  "finished_at_ms": None, "created_at_ms": stamp, "updated_at_ms": stamp, **task}
        values["params"] = values["params"] if isinstance(values["params"], str) else _json(values["params"])
        self._write(lambda db: db.execute(
            f"INSERT INTO tasks({','.join(TASK_COLUMNS)}) VALUES({','.join('?' * len(TASK_COLUMNS))})",
            [values[column] for column in TASK_COLUMNS]))
        return self.task(values["id"])

    def task(self, task_id):
        with self.lock:
            row = self.connection.execute(f"SELECT {','.join(TASK_COLUMNS)} FROM tasks WHERE id=?", (task_id,)).fetchone()
        return task_view(row)

    def tasks(self, *, account=None, user=None, plugin=None, states=None, kind=None, limit=50):
        clauses, values = [], []
        for column, value in (("account", account), ("user", user), ("plugin", plugin), ("kind", kind)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        if states:
            clauses.append(f"state IN ({','.join('?' * len(states))})")
            values.extend(states)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.lock:
            rows = self.connection.execute(
                f"SELECT {','.join(TASK_COLUMNS)} FROM tasks{where} ORDER BY created_at_ms DESC, rowid DESC LIMIT ?",
                (*values, max(1, min(int(limit), 500)))).fetchall()
        return [task_view(row) for row in rows]

    def update_task(self, task_id, *, expected_states=None, **fields):
        """Update named fields; returns the new view, or None when the state guard failed."""
        mapping = {"state": "state", "progress": "progress", "error": "error", "after_task": "after_task",
                   "started_at_ms": "started_at_ms", "finished_at_ms": "finished_at_ms", "params": "params"}
        assignments, values = [], []
        for key, value in fields.items():
            column = mapping[key]
            if key in ("progress", "error", "params") and value is not None and not isinstance(value, str):
                value = _json(value)
            assignments.append(f"{column}=?")
            values.append(value)
        assignments.append("updated_at_ms=?")
        values.append(now_ms())
        guard, guard_values = "", []
        if expected_states:
            guard = f" AND state IN ({','.join('?' * len(expected_states))})"
            guard_values = list(expected_states)

        def apply(db):
            return db.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id=?{guard}",
                              (*values, task_id, *guard_values)).rowcount
        if not self._write(apply):
            return None
        return self.task(task_id)

    def interrupt_unfinished(self):
        """Application start: every unfinished task becomes interrupted, every token is void."""
        stamp = now_ms()
        error = _json({"code": "interrupted", "message": "应用重启时任务未结束，可继续"})

        def apply(db):
            changed = db.execute("UPDATE tasks SET state='interrupted', error=?, finished_at_ms=?, updated_at_ms=? "
                                 "WHERE state IN ('queued','running','stopping')", (error, stamp, stamp)).rowcount
            db.execute("UPDATE executions SET state='revoked', ended_at_ms=? WHERE state='active'", (stamp,))
            return changed
        return self._write(apply)

    def delete_account(self, account):
        def apply(db):
            ids = [row[0] for row in db.execute("SELECT id FROM tasks WHERE account=?", (account,))]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                db.execute(f"DELETE FROM executions WHERE task_id IN ({','.join('?' * len(chunk))})", chunk)
                db.execute(f"DELETE FROM task_attention_seen WHERE task_id IN ({','.join('?' * len(chunk))})", chunk)
            db.execute("DELETE FROM tasks WHERE account=?", (account,))
            db.execute("DELETE FROM subscriptions WHERE account=?", (account,))
            db.execute("DELETE FROM event_positions WHERE account=?", (account,))
        self._write(apply)

    # Attention (added 2026-10-17, Apache-2.0): acknowledge the exact finished tasks the host observed.
    # A task that finishes between polling and acknowledgement must keep its dot, even in the same millisecond.
    def attention(self, account):
        with self.lock:
            rows = self.connection.execute(
                "SELECT t.plugin, t.user, t.state, t.finished_at_ms, t.id, t.params FROM tasks t LEFT JOIN task_attention_seen a "
                "ON a.task_id=t.id "
                "WHERE t.account=? AND t.kind='interactive' AND t.user IS NOT NULL AND t.state IN ('done','failed') "
                "AND json_extract(t.params,'$.notify')=1 AND a.task_id IS NULL "
                "ORDER BY t.finished_at_ms DESC, t.rowid DESC LIMIT 2000", (account,)).fetchall()
        latest = {}
        for plugin, user, state, finished, task_id, raw_params in rows:
            item = latest.setdefault((plugin, user), {"plugin": plugin, "user": user, "state": state,
                                                     "finishedAt": finished, "taskIds": [], "tasks": []})
            item["taskIds"].append(task_id)
            params = _load(raw_params, {})
            item["tasks"].append({"id": task_id, "runId": params.get("runId"), "threadId": params.get("threadId")})
        return list(latest.values())

    def attention_seen(self, plugin, account, user, task_ids):
        stamp = now_ms()
        def apply(db):
            for task_id in set(task_ids):
                db.execute(
                    "INSERT OR IGNORE INTO task_attention_seen(task_id,seen_at_ms) "
                    "SELECT id, ? FROM tasks WHERE id=? AND plugin=? AND account=? AND user=? "
                    "AND kind='interactive' AND state IN ('done','failed') AND json_extract(params,'$.notify')=1",
                    (stamp, task_id, plugin, account, user))
        self._write(apply)
        return stamp

    # Executions --------------------------------------------------------------------------------------------------
    def insert_execution(self, execution_id, task_id, token_hash):
        stamp = now_ms()

        def apply(db):
            generation = db.execute("SELECT COALESCE(MAX(generation), 0) + 1 FROM executions WHERE task_id=?",
                                    (task_id,)).fetchone()[0]
            db.execute("UPDATE executions SET state='revoked', ended_at_ms=? WHERE task_id=? AND state='active'",
                       (stamp, task_id))
            db.execute("INSERT INTO executions(id, task_id, generation, token_hash, state, started_at_ms) "
                       "VALUES(?,?,?,?, 'active', ?)", (execution_id, task_id, generation, token_hash, stamp))
            return generation
        return self._write(apply)

    def execution(self, execution_id=None, *, token_hash=None):
        column, value = ("id", execution_id) if execution_id is not None else ("token_hash", token_hash)
        with self.lock:
            row = self.connection.execute(
                f"SELECT id, task_id, generation, state, started_at_ms, ended_at_ms FROM executions WHERE {column}=?",
                (value,)).fetchone()
        if row is None:
            return None
        return {"id": row[0], "task": row[1], "generation": row[2], "state": row[3], "startedAt": row[4],
                "endedAt": row[5]}

    def end_execution(self, execution_id, state="revoked"):
        return bool(self._write(lambda db: db.execute(
            "UPDATE executions SET state=?, ended_at_ms=? WHERE id=? AND state='active'",
            (state, now_ms(), execution_id)).rowcount))

    def active_executions(self):
        with self.lock:
            rows = self.connection.execute("SELECT id, task_id, generation FROM executions WHERE state='active'").fetchall()
        return [{"id": row[0], "task": row[1], "generation": row[2]} for row in rows]

    # Subscriptions and positions -------------------------------------------------------------------------------------
    def subscribe(self, plugin, account, user, event):
        self._write(lambda db: db.execute(
            "INSERT OR IGNORE INTO subscriptions(plugin, account, user, event, created_at_ms) VALUES(?,?,?,?,?)",
            (plugin, account, user, event, now_ms())))

    def unsubscribe(self, plugin, account, user, event):
        self._write(lambda db: db.execute(
            "DELETE FROM subscriptions WHERE plugin=? AND account=? AND user=? AND event=?",
            (plugin, account, user, event)))

    def subscriptions(self, *, account=None, event=None, plugin=None):
        clauses, values = [], []
        for column, value in (("account", account), ("event", event), ("plugin", plugin)):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.lock:
            rows = self.connection.execute(
                f"SELECT plugin, account, user, event FROM subscriptions{where} ORDER BY plugin, user", values).fetchall()
        return [{"plugin": row[0], "account": row[1], "user": row[2], "event": row[3]} for row in rows]

    def position(self, plugin, account, user):
        with self.lock:
            row = self.connection.execute("SELECT position FROM event_positions WHERE plugin=? AND account=? AND user=?",
                                          (plugin, account, user)).fetchone()
        return _load(row[0]) if row else None

    def set_position(self, plugin, account, user, position):
        self._write(lambda db: db.execute(
            "INSERT INTO event_positions(plugin, account, user, position, updated_at_ms) VALUES(?,?,?,?,?) "
            "ON CONFLICT(plugin, account, user) DO UPDATE SET position=excluded.position, "
            "updated_at_ms=excluded.updated_at_ms", (plugin, account, user, _json(position), now_ms())))
