"""Per-account settings and per-message results of 分析与建议 in the plugin's own database.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Moved from Core (message_analysis_store) into kyf.message-insights, 2026-10-13. The tables
keep their names, so results copied from the old account database stay readable.
"""
import json
import threading
import time

from kyf_plugin import account_dir, connect
from prompts import InsightError, parse_reply_candidates


def saved_result(row, part):
    """Only expose complete saved outputs; an interrupted task is not a new result."""
    raw = row.get("analysis_json" if part == "analysis" else "replies_json")
    try:
        value = json.loads(raw) if raw else None
        if part == "suggestions":
            return parse_reply_candidates(json.dumps({"replies": value}, ensure_ascii=False))
        if (isinstance(value, dict) and all(isinstance(value.get(key), str) and value[key].strip()
                for key in ("summary", "meaning", "uncertainty", "responseDirection")) and
                isinstance(value.get("evidence"), list) and value["evidence"] and
                all(isinstance(item, dict) and isinstance(item.get("text"), str) for item in value["evidence"])):
            return value
    except (ValueError, TypeError, RecursionError, InsightError):
        pass
    return None


def source_binding(row):
    try:
        hashes = json.loads(row.get("context_hashes_json") or "{}")
    except (ValueError, TypeError):
        hashes = {}
    return {**{key: row.get(key) for key in ("scope", "config_stamp", "profile_stamp", "context_epoch",
                                             "message_hash", "context_version")}, "context_hashes": hashes}


def result_bindings(row):
    try:
        value = json.loads(row.get("result_bindings_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}

FILE = "insights.sqlite3"
SCHEMA = """
CREATE TABLE IF NOT EXISTS message_analysis_settings (
    user TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
    revision INTEGER NOT NULL DEFAULT 0, unified_enabled INTEGER
);
CREATE TABLE IF NOT EXISTS message_analyses (
    user TEXT NOT NULL, message_id TEXT NOT NULL, generation TEXT NOT NULL,
    state TEXT NOT NULL, scope TEXT, config_stamp TEXT, profile_stamp TEXT,
    context_epoch TEXT, message_hash TEXT, analysis_json TEXT, error TEXT,
    updated_at_ms INTEGER NOT NULL, analysis_state TEXT, suggestions_state TEXT NOT NULL DEFAULT 'none',
    replies_json TEXT, suggestions_error TEXT, context_hashes_json TEXT, context_version TEXT, result_bindings_json TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(user,message_id)
);
CREATE INDEX IF NOT EXISTS message_analyses_state ON message_analyses(user,state);
CREATE TABLE IF NOT EXISTS auto_reply (
    user TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, agent_id TEXT,
    revision INTEGER NOT NULL DEFAULT 0, updated_at_ms INTEGER NOT NULL,
    last_request_id TEXT, last_run_id TEXT
);
"""


def ensure_schema(connection):
    connection.executescript(SCHEMA)
    settings = {row[1] for row in connection.execute("PRAGMA table_info(message_analysis_settings)")}
    if "unified_enabled" not in settings:
        connection.execute("ALTER TABLE message_analysis_settings ADD COLUMN unified_enabled INTEGER")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(message_analyses)")}
    additions = {"analysis_state": "TEXT", "suggestions_state": "TEXT NOT NULL DEFAULT 'none'",
                 "replies_json": "TEXT", "suggestions_error": "TEXT", "context_hashes_json": "TEXT",
                 "context_version": "TEXT", "result_bindings_json": "TEXT",
                 "schema_version": "INTEGER NOT NULL DEFAULT 1"}
    for name, declaration in additions.items():
        if name not in columns:
            connection.execute("ALTER TABLE message_analyses ADD COLUMN " + name + " " + declaration)
    connection.execute("UPDATE message_analyses SET analysis_state=state WHERE analysis_state IS NULL")
    # Restore saved results hidden by the former freshness policy. Interrupted
    # work is a retryable failure, independent of whether its source changed.
    for stored in connection.execute("SELECT * FROM message_analyses").fetchall():
        row = dict(stored)
        bindings = result_bindings(row)
        for part in ("analysis", "suggestions"):
            state = row[part + "_state"]
            error_field = "error" if part == "analysis" else "suggestions_error"
            value = saved_result(row, part)
            if state in ("stale", "ready"):
                state = "ready" if value is not None else "error"
                error = None if value is not None else "上次任务未完成，请重试"
                connection.execute(f"UPDATE message_analyses SET {part}_state=?,{error_field}=? "
                                   "WHERE user=? AND message_id=?", (state, error, row["user"], row["message_id"]))
            elif state in ("pending", "running"):
                connection.execute(f"UPDATE message_analyses SET {part}_state='error',{error_field}=? "
                                   "WHERE user=? AND message_id=?",
                                   ("任务已中断，请重试", row["user"], row["message_id"]))
            if value is not None and part not in bindings:
                bindings[part] = source_binding(row)
        connection.execute("UPDATE message_analyses SET result_bindings_json=? WHERE user=? AND message_id=?",
                           (json.dumps(bindings), row["user"], row["message_id"]))
        AnalysisStore._aggregate(connection, row["user"], row["message_id"])


class AnalysisStore:
    def __init__(self, data_dir, account):
        self.lock = threading.RLock()
        self.connection = connect(account_dir(data_dir, account) / FILE)
        with self.lock, self.connection:
            ensure_schema(self.connection)

    def close(self):
        with self.lock:
            self.connection.close()

    def _rows(self, sql, params=()):
        with self.lock:
            return [dict(row) for row in self.connection.execute(sql, params)]

    def _row(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def write(self, action):
        with self.lock, self.connection:
            return action(self.connection)

    def settings(self, user):
        return self._row("SELECT * FROM message_analysis_settings WHERE user=?", (user,)) or {
            "user": user, "enabled": 0, "unified_enabled": None, "revision": 0}

    def legacy_auto_reply(self, user):
        row = self._row("SELECT enabled FROM auto_reply WHERE user=?", (user,))
        return bool(row and row["enabled"])

    def enabled_users(self):
        return [row["user"] for row in self._rows("SELECT user FROM message_analysis_settings WHERE unified_enabled=1")]

    def set(self, user, enabled):
        self.write(lambda c: c.execute(
            "INSERT INTO message_analysis_settings(user,unified_enabled,revision) VALUES(?,?,1) "
            "ON CONFLICT(user) DO UPDATE SET unified_enabled=excluded.unified_enabled,revision=revision+1",
            (user, int(enabled))))
        return self.settings(user)

    def pending_count(self, user):
        return self._row("SELECT COUNT(*) AS n FROM message_analyses WHERE user=? AND state IN ('pending','running')",
                         (user,))["n"]

    def rows(self, user, ids=None):
        if ids == []:
            return []
        if ids is None:
            return self._rows("SELECT * FROM message_analyses WHERE user=? ORDER BY updated_at_ms DESC LIMIT 240", (user,))
        return self._rows("SELECT * FROM message_analyses WHERE user=? AND message_id IN (" +
                          ",".join("?" for _ in ids) + ")", (user, *ids))

    def begin(self, user, ids, generation, *, parts=("analysis",), unified=False):
        def action(c):
            for message_id in ids:
                c.execute("INSERT INTO message_analyses(user,message_id,generation,state,updated_at_ms) "
                          "VALUES(?,?,?,'pending',?) ON CONFLICT(user,message_id) DO UPDATE SET "
                          "generation=excluded.generation,state='pending',updated_at_ms=excluded.updated_at_ms",
                          (user, message_id, generation, int(time.time() * 1000)))
                if "analysis" in parts:
                    c.execute("UPDATE message_analyses SET analysis_state='pending',error=NULL "
                              "WHERE user=? AND message_id=?", (user, message_id))
                if "suggestions" in parts:
                    c.execute("UPDATE message_analyses SET suggestions_state='pending',"
                              "suggestions_error=NULL WHERE user=? AND message_id=?", (user, message_id))
                c.execute("UPDATE message_analyses SET schema_version=? WHERE user=? AND message_id=?",
                          (2 if unified else 1, user, message_id))
        self.write(action)

    def bind(self, user, ids, generation, binding, hashes, context_version=None):
        def action(c):
            for message_id in ids:
                c.execute("UPDATE message_analyses SET state='running',scope=?,config_stamp=?,profile_stamp=?,"
                          "context_epoch=?,message_hash=? WHERE user=? AND message_id=? AND generation=? "
                          "AND state='pending'", (*binding, hashes[message_id], user, message_id, generation))
                c.execute("UPDATE message_analyses SET analysis_state=CASE WHEN analysis_state='pending' THEN "
                          "'running' ELSE analysis_state END,suggestions_state=CASE WHEN suggestions_state='pending' "
                          "THEN 'running' ELSE suggestions_state END,context_hashes_json=?,context_version=? "
                          "WHERE user=? AND message_id=? AND generation=? AND state='running'",
                          (json.dumps(hashes), context_version, user, message_id, generation))
        self.write(action)

    def finish(self, user, ids, generation, state, results=None, error=None, *, part=None):
        if state == "stale":
            state, error = "error", error or "任务已中断，请重试"
        def action(c):
            for message_id in ids:
                for component in ((part,) if part else ("analysis", "suggestions")):
                    field = "analysis_json" if component == "analysis" else "replies_json"
                    error_field = "error" if component == "analysis" else "suggestions_error"
                    value = results.get(message_id) if results is not None else None
                    updated = c.execute("UPDATE message_analyses SET " + component + "_state=?," + field + "=COALESCE(?," +
                              field + ")," + error_field + "=?,updated_at_ms=? WHERE user=? AND message_id=? AND "
                              "generation=? AND " + component + "_state IN ('pending','running')",
                              (state, json.dumps(value, ensure_ascii=False) if value else None, error,
                               int(time.time() * 1000), user, message_id, generation)).rowcount
                    if updated and state == "ready":
                        row = dict(c.execute("SELECT * FROM message_analyses WHERE user=? AND message_id=?",
                                             (user, message_id)).fetchone())
                        bindings = result_bindings(row)
                        bindings[component] = source_binding(row)
                        c.execute("UPDATE message_analyses SET result_bindings_json=? WHERE user=? AND message_id=?",
                                  (json.dumps(bindings), user, message_id))
                self._aggregate(c, user, message_id)
        self.write(action)

    def stale(self, user, ids):
        for row in self.rows(user, ids):
            self.finish(user, [row["message_id"]], row["generation"], "error", error="任务已中断，请重试")

    @staticmethod
    def _aggregate(c, user, message_id):
        c.execute("UPDATE message_analyses SET state=CASE "
                  "WHEN analysis_state='running' OR suggestions_state='running' THEN 'running' "
                  "WHEN analysis_state='pending' OR suggestions_state='pending' THEN 'pending' "
                  "WHEN analysis_state='ready' AND suggestions_state IN ('ready','none') THEN 'ready' "
                  "WHEN analysis_state='ready' OR suggestions_state='ready' THEN 'partial' "
                  "ELSE 'error' END "
                  "WHERE user=? AND message_id=?", (user, message_id))
