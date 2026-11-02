"""Per-account plugin database: a mirror of Core's chat projection plus the plugin's own tables.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The analysis reads full histories many times. Instead of asking Core for every
query, the plugin keeps ``context`` and ``context_messages`` copies in its own
``analysis.sqlite3``, synchronised from ``/plugin-api/v1/history`` (new rows are
appended; a changed source, epoch or row resets the copy). Writes to Core data go
through the plugin interface only.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

from kyf_plugin import account_dir

FILE = "analysis.sqlite3"
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
HUMAN_TTL = 2.0
VERIFY_TAIL = 400


def _row(view, user):
    return (user, view["seq"], view["id"], view["sortKey"], 1 if view.get("group") else 0, view.get("side"),
            view.get("senderId") or "", view.get("senderName") or "", view.get("kind"), view.get("text") or "",
            view.get("time"), view.get("raw") or "{}")


class AccountData:
    """Database object with the interface the analysis modules were written against."""

    def __init__(self, plugin, account):
        from analysis_store import ensure_schema
        self.plugin = plugin
        self.account = account
        self.lock = threading.RLock()
        self.path = account_dir(plugin.data_dir, account) / FILE
        self._connection = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._connection.row_factory = sqlite3.Row
        with self.lock, self._connection:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(MIRROR_SCHEMA)
            ensure_schema(self._connection)
        self._human = {}

    # Database interface --------------------------------------------------------------------------------------------
    def _connect(self):
        return self._connection

    def _rows(self, sql, params=()):
        with self.lock:
            return [dict(row) for row in self._connection.execute(sql, params)]

    def _row(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def context(self, user):
        return self._row("SELECT * FROM context WHERE user=?", (user,))

    def context_messages(self, user, upto_seq):
        if upto_seq is None:
            return self._rows("SELECT * FROM context_messages WHERE user=? ORDER BY seq ASC", (user,))
        return self._rows("SELECT * FROM context_messages WHERE user=? AND seq<=? ORDER BY seq ASC", (user, upto_seq))

    def close(self):
        with self.lock:
            self._connection.close()

    # Mirror -------------------------------------------------------------------------------------------------------
    def sync(self, user, check=None):
        """Bring the copy of this conversation up to Core's current projection; returns the context row."""
        core = self.plugin.core
        status = core.history_status(self.account, user)
        local = self.context(user)
        if status["state"] == "idle":
            return local
        reset = (local is None or local["source_id"] != status.get("sourceFingerprint") or
                 local["epoch"] != status.get("epoch"))
        if reset:
            with self.lock, self._connection:
                self._connection.execute("DELETE FROM context_messages WHERE user=?", (user,))
        else:
            # The recent end is where Core's projection can change; re-copy from the first difference.
            tail = list(reversed(self._rows("SELECT * FROM context_messages WHERE user=? ORDER BY seq DESC LIMIT ?",
                                             (user, VERIFY_TAIL))))
            if tail:
                verdict = core.post("history/verify", {"account": self.account, "user": user,
                                                       "items": [{"id": row["msg_id"], "hash": _hash(row)} for row in tail]})
                changed = [row["seq"] for row, item in zip(tail, verdict["results"]) if item["state"] != "same"]
                if changed:
                    with self.lock, self._connection:
                        self._connection.execute("DELETE FROM context_messages WHERE user=? AND seq>=?",
                                                 (user, min(changed)))
        last = self._row("SELECT * FROM context_messages WHERE user=? ORDER BY seq DESC LIMIT 1", (user,))
        after = last["seq"] if last else 0
        while True:
            if check is not None:
                check()
            page = core.history_messages(self.account, user, after=after, limit=500)
            messages = page["messages"]
            if messages:
                with self.lock, self._connection:
                    self._connection.executemany(
                        "INSERT OR REPLACE INTO context_messages(user,seq,msg_id,sort_key,group_chat,side,sender_id,"
                        "sender_name,kind,text,time_ms,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        [_row(view, user) for view in messages])
                after = messages[-1]["seq"]
            if page.get("next") is None:
                break
        with self.lock, self._connection:
            self._connection.execute(
                "INSERT INTO context(user,state,revision,read_count,total_count,error,source_id,epoch,synced_at_ms) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user) DO UPDATE SET state=excluded.state,"
                "revision=excluded.revision,read_count=excluded.read_count,total_count=excluded.total_count,"
                "error=excluded.error,source_id=excluded.source_id,epoch=excluded.epoch,"
                "synced_at_ms=excluded.synced_at_ms",
                (user, status["state"], status.get("revision") or 0, after, status.get("totalCount"),
                 status.get("error"), status.get("sourceFingerprint"), status.get("epoch"), int(time.time() * 1000)))
        return self.context(user)

    # Human data (read from Core, briefly cached) -------------------------------------------------------------------
    def human(self, user, refresh=False):
        cached = self._human.get(user)
        if refresh or cached is None or time.monotonic() - cached[0] > HUMAN_TTL:
            value = self.plugin.core.get(f"human/{user}", account=self.account, view="rows")
            cached = self._human[user] = (time.monotonic(), value)
        return cached[1]

    def forget_human(self, user):
        self._human.pop(user, None)


def _hash(row):
    """Core's content hash of one projected message (evidence digest)."""
    import hashlib
    facts = [row.get("sort_key"), row.get("side"), row.get("sender_id") or "", row.get("kind") or "",
             row.get("text") or ""]
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class Accounts:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = threading.Lock()
        self.items = {}

    def account(self, account):
        with self.lock:
            data = self.items.get(account)
            if data is None:
                data = self.items[account] = AccountData(self.plugin, account)
            return data

    def close(self):
        with self.lock:
            items, self.items = list(self.items.values()), {}
        for data in items:
            data.close()
