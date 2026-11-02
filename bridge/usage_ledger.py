"""Always-on model usage ledger: one row per gateway attempt in ``.local/core/usage.sqlite3``.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Writes go through a bounded queue and a single writer thread. When the queue is
full a record is dropped and counted as a gap; model traffic and chat data
maintenance never wait for this ledger. Unknown provider usage stays NULL.

Attempts still waiting for capacity or a response are kept in memory only. They
appear on the first records page until their final row is committed, and never
enter totals, since their usage is not known yet.
"""
from __future__ import annotations

import queue
import sqlite3
import threading
import time
from pathlib import Path

from core_contracts import AdvisorError

QUEUE_LIMIT = 10_000
BATCH = 200
PAGE_LIMIT = 100
ACTIVE_LIMIT = 1_000
ACTIVE_PHASES = ("queued", "running")
TIMELINE_BUCKET_LIMIT = 10_000
MAX_SAFE_INTEGER = 2**53 - 1
ACCOUNTING_COLUMNS = {
    "tool_use_prompt_tokens": "INTEGER", "total_input_tokens": "INTEGER", "total_output_tokens": "INTEGER",
    "total_tokens": "INTEGER", "accounting_version": "INTEGER", "total_tokens_source": "TEXT",
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  at_ms INTEGER NOT NULL,
  plugin TEXT NOT NULL,
  plugin_version TEXT,
  task TEXT,
  execution TEXT,
  account TEXT,
  user TEXT,
  scope TEXT NOT NULL,
  service TEXT,
  protocol TEXT,
  model TEXT,
  step TEXT,
  status TEXT NOT NULL,
  http_status INTEGER,
  duration_ms INTEGER,
  first_byte_ms INTEGER,
  first_text_ms INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  reasoning_tokens INTEGER,
  cache_read_tokens INTEGER,
  cache_write_tokens INTEGER,
  tool_use_prompt_tokens INTEGER,
  total_input_tokens INTEGER,
  total_output_tokens INTEGER,
  total_tokens INTEGER,
  accounting_version INTEGER,
  total_tokens_source TEXT,
  trace_record_id TEXT,
  outcome TEXT
);
CREATE INDEX IF NOT EXISTS attempts_account_time ON attempts(account, at_ms DESC);
CREATE INDEX IF NOT EXISTS attempts_user_time ON attempts(account, user, at_ms DESC);
CREATE TABLE IF NOT EXISTS gaps (at_ms INTEGER NOT NULL, dropped INTEGER NOT NULL);
"""
COLUMNS = ("id", "at_ms", "plugin", "plugin_version", "task", "execution", "account", "user", "scope", "service",
           "protocol", "model", "step", "status", "http_status", "duration_ms", "first_byte_ms", "first_text_ms",
           "input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens", "cache_write_tokens",
           "tool_use_prompt_tokens", "total_input_tokens", "total_output_tokens", "total_tokens", "accounting_version",
           "total_tokens_source",
           "trace_record_id")
RECORD_KEYS = {"id": "attemptId", "at_ms": "at", "plugin_version": "pluginVersion", "http_status": "httpStatus",
               "duration_ms": "durationMs", "first_byte_ms": "firstByteMs", "first_text_ms": "firstTextMs",
               "input_tokens": "inputTokens", "output_tokens": "outputTokens", "reasoning_tokens": "reasoningTokens",
               "cache_read_tokens": "cacheReadTokens", "cache_write_tokens": "cacheWriteTokens",
               "tool_use_prompt_tokens": "toolUsePromptTokens", "total_input_tokens": "totalInputTokens",
               "total_output_tokens": "totalOutputTokens", "total_tokens": "totalTokens",
               "accounting_version": "accountingVersion", "total_tokens_source": "totalTokensSource",
               "trace_record_id": "traceRecordId"}
FAILED = ("http-error", "network-error", "timeout", "stream-error")


def usage_scope(plugin, user, scope=None, multi=False):
    """Self-analysis belongs to the account owner, without inventing a contact ID."""
    scope = scope or ("friend" if user else "multi" if multi else "none")
    return "self" if scope == "none" and plugin == "kyf.self-analysis" and not user else scope


def _scope_sql(prefix=""):
    # Interpret pre-self-scope records consistently without rewriting the ledger.
    return (f"CASE WHEN {prefix}scope='none' AND {prefix}plugin='kyf.self-analysis' "
            f"AND COALESCE({prefix}user,'')='' THEN 'self' ELSE {prefix}scope END")


def _count(value):
    return value if type(value) is int and 0 <= value <= MAX_SAFE_INTEGER else None


def _sum_known(*values):
    return _count(sum(values)) if all(value is not None for value in values) else None


def _legacy_accounting(row):
    """Only infer totals whose original provider semantics are unambiguous.

    Historical records did not retain provider totals or Gemini tool prompt
    usage. In particular, a missing cache/thought/tool count is not zero.
    Version 1 records already carry the gateway's normalized accounting.
    """
    if row["accounting_version"] == 1:
        return
    protocol = row["protocol"]
    total_input = total_output = None
    if protocol in ("chat_completions", "responses", "ollama"):
        total_input, total_output = row["input_tokens"], row["output_tokens"]
    elif protocol == "anthropic":
        total_input = _sum_known(row["input_tokens"], row["cache_read_tokens"], row["cache_write_tokens"])
        total_output = row["output_tokens"]
    elif protocol == "gemini":
        total_input = _sum_known(row["input_tokens"], row["tool_use_prompt_tokens"])
        total_output = _sum_known(row["output_tokens"], row["reasoning_tokens"])
    if row["total_input_tokens"] is None:
        row["total_input_tokens"] = total_input
    if row["total_output_tokens"] is None:
        row["total_output_tokens"] = total_output
    if row["total_tokens"] is None:
        row["total_tokens"] = _sum_known(row["total_input_tokens"], row["total_output_tokens"])
        row["total_tokens_source"] = "derived" if row["total_tokens"] is not None else None
    row["accounting_version"] = 0


TOKEN_FIELDS = {
    "inputTokens": "input_tokens", "outputTokens": "output_tokens", "totalInputTokens": "total_input_tokens",
    "totalOutputTokens": "total_output_tokens", "totalTokens": "total_tokens", "reasoningTokens": "reasoning_tokens",
    "cacheReadTokens": "cache_read_tokens", "cacheWriteTokens": "cache_write_tokens",
    "toolUsePromptTokens": "tool_use_prompt_tokens",
}
COVERAGE_FIELDS = {
    "knownInputRequests": "total_input_tokens", "knownOutputRequests": "total_output_tokens",
    "knownTotalRequests": "total_tokens", "knownReasoningRequests": "reasoning_tokens",
    "knownCacheReadRequests": "cache_read_tokens", "knownCacheWriteRequests": "cache_write_tokens",
    "knownToolUsePromptRequests": "tool_use_prompt_tokens",
}
AGGREGATE_KEYS = ("requests", *TOKEN_FIELDS, *COVERAGE_FIELDS, "missingInputUsage", "missingOutputUsage",
                  "missingTotalUsage", "missingUsage", "failures", "lastAt")


def _aggregate_sql(prefix=""):
    field = lambda column: prefix + column
    expressions = [f"COUNT({field('id')})"]
    expressions += [f"COALESCE(SUM({field(column)}),0)" for column in TOKEN_FIELDS.values()]
    expressions += [f"COUNT({field(column)})" for column in COVERAGE_FIELDS.values()]
    for column in ("total_input_tokens", "total_output_tokens", "total_tokens"):
        expressions.append(f"COUNT({field('id')}) - COUNT({field(column)})")
    expressions += [
        f"COALESCE(SUM(CASE WHEN {field('id')} IS NOT NULL AND "
        f"({field('input_tokens')} IS NULL OR {field('output_tokens')} IS NULL) THEN 1 ELSE 0 END),0)",
        f"COALESCE(SUM(CASE WHEN {field('status')} IN ({','.join(repr(status) for status in FAILED)}) "
        "THEN 1 ELSE 0 END),0)", f"MAX({field('at_ms')})",
    ]
    return ", ".join(expressions)


def _empty_aggregate():
    return {key: None if key == "lastAt" else 0 for key in AGGREGATE_KEYS}


def _add_aggregate(target, source):
    for key in AGGREGATE_KEYS:
        if key == "lastAt":
            known = [value for value in (target[key], source[key]) if value is not None]
            target[key] = max(known) if known else None
        else:
            target[key] += source[key]


class UsageLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.gap_lock = threading.Lock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        try:
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.executescript(SCHEMA)
            self._migrate_accounting()
        except BaseException:
            self.connection.close()
            raise
        self.queue = queue.Queue(QUEUE_LIMIT)
        self.active_lock = threading.Lock()
        self.active = {}  # attempt id -> {"owner", "row", "finishing"}
        self.dropped = 0
        self.closed = False
        self.writer = threading.Thread(target=self._write_loop, daemon=True, name="usage-ledger")
        self.writer.start()

    def _migrate_accounting(self):
        """Extend the existing ledger in place, preserving raw usage and outcomes."""
        def safe_sum(*parts):
            expression = " + ".join(parts)
            valid = " AND ".join(f"{part} BETWEEN 0 AND {MAX_SAFE_INTEGER}" for part in parts)
            return f"CASE WHEN {valid} AND ({expression}) <= {MAX_SAFE_INTEGER} THEN {expression} END"

        plain_input, plain_output = safe_sum("input_tokens"), safe_sum("output_tokens")
        cached_input = safe_sum("input_tokens", "cache_read_tokens", "cache_write_tokens")
        tool_input = safe_sum("input_tokens", "tool_use_prompt_tokens")
        reasoning_output = safe_sum("output_tokens", "reasoning_tokens")
        # DDL and all backfill stages belong to one transaction. A failed
        # startup must not leave an externally visible partial migration.
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in self.connection.execute("PRAGMA table_info(attempts)")}
            for column, kind in ACCOUNTING_COLUMNS.items():
                if column not in columns:
                    self.connection.execute(f"ALTER TABLE attempts ADD COLUMN {column} {kind}")
            if self.connection.execute("SELECT 1 FROM attempts WHERE accounting_version IS NULL LIMIT 1").fetchone() is None:
                return
            self.connection.execute(
                "UPDATE attempts SET total_input_tokens=CASE "
                f"WHEN protocol IN ('chat_completions','responses','ollama') THEN {plain_input} "
                f"WHEN protocol='anthropic' THEN {cached_input} WHEN protocol='gemini' THEN {tool_input} END, "
                "total_output_tokens=CASE "
                f"WHEN protocol IN ('chat_completions','responses','ollama','anthropic') THEN {plain_output} "
                f"WHEN protocol='gemini' THEN {reasoning_output} END WHERE accounting_version IS NULL")
            self.connection.execute(
                f"UPDATE attempts SET total_tokens={safe_sum('total_input_tokens', 'total_output_tokens')} "
                "WHERE accounting_version IS NULL")
            self.connection.execute(
                "UPDATE attempts SET accounting_version=0, "
                "total_tokens_source=CASE WHEN total_tokens IS NOT NULL THEN 'derived' END "
                "WHERE accounting_version IS NULL")

    # Writing ----------------------------------------------------------------------------------------------------------
    def record(self, record, scope=None):
        """Queue one gateway attempt; never blocks."""
        row = {column: record.get(RECORD_KEYS.get(column, column)) for column in COLUMNS}
        if not isinstance(row["id"], str) or not isinstance(row["plugin"], str):
            return False
        row["scope"] = usage_scope(row["plugin"], row["user"], scope or record.get("scope"))
        for column in TOKEN_FIELDS.values():
            row[column] = _count(row[column])
        row["accounting_version"] = 1 if type(row["accounting_version"]) is int and row["accounting_version"] == 1 else None
        if row["total_tokens_source"] not in ("provider", "derived"):
            row["total_tokens_source"] = None
        _legacy_accounting(row)
        row["at_ms"] = row["at_ms"] if type(row["at_ms"]) is int else int(time.time() * 1000)
        with self.gap_lock:
            accepted = False
            if not self.closed:
                try:
                    self.queue.put_nowait(row)
                    accepted = True
                except queue.Full:
                    self.dropped += 1
        with self.active_lock:
            # A queued final row replaces the in-flight entry once it is committed.
            if accepted and row["id"] in self.active:
                self.active[row["id"]]["finishing"] = True
            elif not accepted:
                self.active.pop(row["id"], None)
        return accepted

    def track(self, attempt, scope=None, owner=None):
        """Show one attempt that is queued or running; its final ``record`` supersedes it."""
        if not isinstance(attempt, dict) or not isinstance(attempt.get("attemptId"), str):
            return False
        attempt_id, phase = attempt["attemptId"], attempt.get("phase")
        with self.active_lock:
            entry = self.active.get(attempt_id)
            if phase == "ended":
                if entry is not None and not entry["finishing"]:
                    del self.active[attempt_id]
                return True
            if (phase not in ACTIVE_PHASES or not isinstance(attempt.get("plugin"), str) or
                    not isinstance(attempt.get("account"), str) or (entry and entry["finishing"]) or
                    (entry is None and len(self.active) >= ACTIVE_LIMIT)):
                return False
            row = {RECORD_KEYS.get(column, column): None for column in COLUMNS}
            row.update({key: attempt.get(key) for key in ("attemptId", "plugin", "pluginVersion", "task", "execution",
                                                          "account", "user", "service", "protocol", "model", "step",
                                                          "traceRecordId")})
            row.update(at=attempt["at"] if type(attempt.get("at")) is int else int(time.time() * 1000), status=phase,
                       scope=usage_scope(attempt["plugin"], attempt.get("user"), scope or attempt.get("scope")),
                       outcome=None)
            self.active[attempt_id] = {"owner": owner, "row": row, "finishing": False}
        return True

    def forget(self, owner):
        """Drop in-flight attempts of a gateway generation that can no longer finish them."""
        with self.active_lock:
            for attempt_id in [key for key, entry in self.active.items()
                               if entry["owner"] is owner and not entry["finishing"]]:
                del self.active[attempt_id]

    def _write_loop(self):
        try:
            self._drain()
        finally:
            with self.lock:
                self.connection.close()

    def _drain(self):
        while True:
            try:
                first = self.queue.get(timeout=0.5)
            except queue.Empty:
                with self.gap_lock:
                    drained = self.closed and self.queue.empty()
                if drained:
                    self._write([])
                    return
                self._write([])
                continue
            if isinstance(first, threading.Event):
                self._write([])
                first.set()
                self.queue.task_done()
                continue
            rows = [first]
            barrier = None
            while len(rows) < BATCH:
                try:
                    item = self.queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, threading.Event):
                    barrier = item
                    break
                rows.append(item)
            self._write(rows)
            for _ in rows:
                self.queue.task_done()
            if barrier is not None:
                barrier.set()
                self.queue.task_done()

    def _write(self, rows):
        with self.lock:
            with self.gap_lock:
                dropped, self.dropped = self.dropped, 0
            if not rows and not dropped:
                return
            try:
                self.connection.execute("BEGIN")
                self.connection.executemany(
                    f"INSERT INTO attempts({','.join(COLUMNS)}) VALUES({','.join('?' * len(COLUMNS))}) "
                    "ON CONFLICT(id) DO UPDATE SET " +
                    ",".join(f"{column}=excluded.{column}" for column in COLUMNS if column != "id"),
                    [[row[column] for column in COLUMNS] for row in rows])
                if dropped:
                    self.connection.execute("INSERT INTO gaps(at_ms, dropped) VALUES(?,?)",
                                            (int(time.time() * 1000), dropped))
                self.connection.execute("COMMIT")
            except sqlite3.Error:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                with self.gap_lock:
                    self.dropped += dropped + len(rows)
            finally:
                # Still under the read lock: a page never misses a just-finished attempt.
                with self.active_lock:
                    for row in rows:
                        self.active.pop(row["id"], None)

    def flush(self, timeout=2.0):
        # A barrier is handled by the writer after earlier in-flight and queued
        # batches, so even an empty queue cannot hide a blocked database write.
        barrier = threading.Event()
        with self.gap_lock:
            if self.closed:
                return False
            try:
                self.queue.put_nowait(barrier)
            except queue.Full:
                return False
        return barrier.wait(timeout)

    def set_outcome(self, attempt_id, plugin, outcome):
        with self.lock:
            changed = self.connection.execute("UPDATE attempts SET outcome=? WHERE id=? AND plugin=?",
                                              (outcome, attempt_id, plugin)).rowcount
            row = self.connection.execute("SELECT trace_record_id, task FROM attempts WHERE id=?",
                                          (attempt_id,)).fetchone()
        return bool(changed), (row[0], row[1]) if row else (None, None)

    def clear(self, account=None):
        if not self.flush():
            raise AdvisorError("usage-busy", "用量记录仍在写入，请稍后再清空")
        with self.lock:
            if account is None:
                self.connection.execute("DELETE FROM attempts")
                self.connection.execute("DELETE FROM gaps")
            else:
                self.connection.execute("DELETE FROM attempts WHERE account=?", (account,))

    def close(self):
        with self.gap_lock:
            self.closed = True
        # The sole writer drains accepted records and closes its connection,
        # including after this bounded join if storage is temporarily stalled.
        self.writer.join(5)

    # Reading ----------------------------------------------------------------------------------------------------------
    @staticmethod
    def _where(filters, prefix=""):
        clauses, values = [f"{prefix}account=?"], [filters["account"]]
        if filters.get("since") is not None:
            clauses.append(f"{prefix}at_ms>=?")
            values.append(int(filters["since"]))
        if filters.get("until") is not None:
            clauses.append(f"{prefix}at_ms<?")
            values.append(int(filters["until"]))
        friend = filters.get("friend")
        if friend in ("none", "multi", "self"):
            clauses.append(f"({_scope_sql(prefix)})=?")
            values.append(friend)
        elif friend:
            clauses.append(f"{prefix}user=?")
            values.append(friend)
        if filters.get("status") == "active":
            clauses.append("0")  # Only in-flight attempts are active; none is stored yet.
        for key in ("plugin", "model", "status", "step", "task"):
            if filters.get(key) and not (key == "status" and filters[key] == "active"):
                if key == "step" and filters[key] == "__none__":
                    clauses.append(f"({prefix}step IS NULL OR {prefix}step='')")
                    continue
                clauses.append(f"{prefix}{key}=?")
                values.append(filters[key])
        return " WHERE " + " AND ".join(clauses), values

    def summary(self, filters):
        where, values = self._where(filters)
        with self.lock:
            row = self.connection.execute(
                f"SELECT {_aggregate_sql()} FROM attempts{where}", values).fetchone()
            gaps = self.connection.execute("SELECT COALESCE(SUM(dropped), 0) FROM gaps").fetchone()[0]
            extent = self._extent(filters)
        return {**dict(zip(AGGREGATE_KEYS, row)), **extent, "droppedRecords": gaps}

    def _extent(self, filters):
        """Available history for the current dimensions, independent of the visible time window."""
        where, values = self._where({key: value for key, value in filters.items() if key not in ("since", "until")})
        first, last = self.connection.execute(f"SELECT MIN(at_ms), MAX(at_ms) FROM attempts{where}", values).fetchone()
        return {"firstAt": first, "lastAt": last, "cutoffMs": int(time.time() * 1000)}

    def models(self, filters):
        """All matching model choices, independent of record pagination and the current model choice."""
        where, values = self._where({key: value for key, value in filters.items() if key != "model"})
        with self.lock:
            rows = self.connection.execute(
                f"SELECT DISTINCT model FROM attempts{where} AND model IS NOT NULL AND model<>'' ORDER BY model",
                values).fetchall()
        return [row[0] for row in rows]

    def by_friend(self, filters, order="requests"):
        where, values = self._where(filters)
        ordering = {"requests": "COUNT(id) DESC", "input": "SUM(total_input_tokens) DESC",
                    "output": "SUM(total_output_tokens) DESC", "total": "SUM(total_tokens) DESC",
                    "recent": "MAX(at_ms) DESC", "failures": f"SUM(status IN ({','.join(repr(item) for item in FAILED)})) DESC"}
        with self.lock:
            rows = self.connection.execute(
                f"SELECT {_scope_sql()} AS display_scope, NULLIF(user,'') AS display_user, {_aggregate_sql()} FROM attempts{where} "
                f"GROUP BY display_scope, display_user ORDER BY {ordering.get(order, ordering['requests'])}, display_scope, display_user LIMIT 500",
                values).fetchall()
        return [{"scope": row[0], "user": row[1], **dict(zip(AGGREGATE_KEYS, row[2:]))} for row in rows]

    def by_plugin(self, filters):
        return [{key: value for key, value in module.items() if key != "steps"} for module in self.modules(filters)]

    def modules(self, filters):
        where, values = self._where(filters)
        with self.lock:
            rows = self.connection.execute(
                f"SELECT plugin, NULLIF(step,''), {_aggregate_sql()} FROM attempts{where} "
                "GROUP BY plugin, NULLIF(step,'')", values).fetchall()
        modules = {}
        for plugin, step, *metrics in rows:
            module = modules.setdefault(plugin, {"plugin": plugin, **_empty_aggregate(), "steps": []})
            aggregate = dict(zip(AGGREGATE_KEYS, metrics))
            _add_aggregate(module, aggregate)
            module["steps"].append({"step": step, "name": None, **aggregate})
        for module in modules.values():
            module["steps"].sort(key=lambda item: (-item["totalTokens"], -item["requests"], item["step"] or ""))
        return sorted(modules.values(), key=lambda item: (-item["totalTokens"], -item["requests"], item["plugin"]))

    def timeline(self, filters, bounds):
        if not isinstance(bounds, (list, tuple)) or not 2 <= len(bounds) <= TIMELINE_BUCKET_LIMIT + 1 or \
                any(type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER for value in bounds) or \
                any(left >= right for left, right in zip(bounds, bounds[1:])):
            raise ValueError("invalid timeline boundaries")
        where, values = self._where(filters, "a.")
        # Calendar boundaries come from the client so hour/day/month buckets
        # retain the same local-time and daylight-saving behavior as trends.
        # One indexed SQL aggregation covers every bucket and plugin, including
        # attempts outside the first page and empty intervals.
        intervals = ",".join(f"({index},?,?)" for index in range(len(bounds) - 1))
        parameters = [value for pair in zip(bounds, bounds[1:]) for value in pair]
        buckets = [{"since": left, "until": right, **_empty_aggregate(), "modules": []}
                   for left, right in zip(bounds, bounds[1:])]
        with self.lock:
            rows = self.connection.execute(
                f"WITH bins(position,since,until) AS (VALUES {intervals}) "
                f"SELECT bins.position, a.plugin, {_aggregate_sql('a.')} FROM bins LEFT JOIN attempts a "
                f"ON a.at_ms >= bins.since AND a.at_ms < bins.until AND {where.removeprefix(' WHERE ')} "
                "GROUP BY bins.position, a.plugin ORDER BY bins.position", (*parameters, *values)).fetchall()
            extent = self._extent(filters)
            gaps = self.connection.execute("SELECT COALESCE(SUM(dropped),0) FROM gaps").fetchone()[0]
        for index, plugin, *metrics in rows:
            aggregate = dict(zip(AGGREGATE_KEYS, metrics))
            _add_aggregate(buckets[index], aggregate)
            if plugin is not None:
                buckets[index]["modules"].append({"plugin": plugin, **aggregate})
        for bucket in buckets:
            bucket["modules"].sort(key=lambda item: (-item["totalTokens"], -item["requests"], item["plugin"]))
        return {"bounds": list(bounds), "buckets": buckets, **extent, "droppedRecords": gaps}

    @staticmethod
    def _active_match(filters, row):
        if row["account"] != filters["account"]:
            return False
        if filters.get("since") is not None and row["at"] < int(filters["since"]):
            return False
        if filters.get("until") is not None and row["at"] >= int(filters["until"]):
            return False
        friend = filters.get("friend")
        if friend and row["scope" if friend in ("none", "multi", "self") else "user"] != friend:
            return False
        if filters.get("status") and filters["status"] not in ("active", row["status"]):
            return False
        if filters.get("step") == "__none__":
            if row["step"]:
                return False
        elif filters.get("step") and row["step"] != filters["step"]:
            return False
        return all(not filters.get(key) or row[key] == filters[key] for key in ("plugin", "model", "task"))

    def records(self, filters, before=None, limit=50):
        where, values = self._where(filters)
        if before:
            where += " AND (at_ms < ? OR (at_ms = ? AND id < ?))"
            values += [before[0], before[0], before[1]]
        limit = max(1, min(int(limit), PAGE_LIMIT))
        with self.lock:
            rows = self.connection.execute(
                f"SELECT {','.join(COLUMNS)}, outcome FROM attempts{where} ORDER BY at_ms DESC, id DESC LIMIT ?",
                (*values, limit + 1)).fetchall()
            with self.active_lock:
                active = [] if before else [dict(entry["row"]) for entry in self.active.values()
                                            if self._active_match(filters, entry["row"])]
        items = [dict(zip((*COLUMNS, "outcome"), row)) for row in rows[:limit]]
        view = [{RECORD_KEYS.get(key, key): value for key, value in item.items()} for item in items]
        for row in view:
            row["scope"] = usage_scope(row["plugin"], row["user"], row["scope"])
        stored = {item["attemptId"] for item in view}
        active = sorted((row for row in active if row["attemptId"] not in stored),
                        key=lambda row: (row["at"], row["attemptId"]), reverse=True)
        cursor = [items[-1]["at_ms"], items[-1]["id"]] if len(rows) > limit and items else None
        return {"records": active + view, "next": cursor}
