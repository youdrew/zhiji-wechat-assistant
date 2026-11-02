"""Per-account plugin databases with a mirror of Core's chat projection.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

A turn reads the friend's whole history many times (model input, summaries,
media). Instead of asking Core for every query, the plugin keeps ``context`` and
``context_messages`` copies in its own ``advisor.sqlite3``, synchronised from
``/plugin-api/v1/history`` (new rows are appended; a changed source, epoch or row
resets the copy). The pattern is the one of kyf.friend-analysis (backend/account.py),
copied rather than imported: plugins do not import each other.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

from contracts import AdvisorError
from store import AdvisorDatabase, _checked_root

VERIFY_TAIL = 400
SYNC_ATTEMPTS = 3
SYNC_WAIT_SECONDS = 0.25


def _row(view, user):
    return (user, view["seq"], view["id"], view["sortKey"], 1 if view.get("group") else 0, view.get("side"),
            view.get("senderId") or "", view.get("senderName") or "", view.get("kind"), view.get("text") or "",
            view.get("time"), view.get("raw") or "{}")


def _hash(row):
    """Core's content hash of one projected message (evidence digest)."""
    facts = [row.get("sort_key"), row.get("side"), row.get("sender_id") or "", row.get("kind") or "",
             row.get("text") or ""]
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class AccountData(AdvisorDatabase):
    """One account's discussions plus the mirror of its conversations."""

    def __init__(self, plugin, data_root, account):
        super().__init__(data_root, account)
        self.plugin = plugin
        self._sync_locks = {}

    def _sync_lock(self, user):
        with self.lock:
            return self._sync_locks.setdefault(user, threading.Lock())

    def _write(self, sql, rows=None, many=False):
        with self.lock:
            connection = self._connect()
            with connection:
                (connection.executemany if many else connection.execute)(sql, rows if rows is not None else ())

    def sync(self, user, check=None):
        """Bring the copy of this conversation up to Core's projection; returns the mirrored context row.

        A copy whose source or epoch changed while it was read is read again, so the rows always
        belong to the source and epoch the row reports. Syncs of one conversation run one at a time:
        a second run's reset must not delete rows a first run has just synced and is reading. A run
        waiting for another run's copy keeps checking, so its stop never waits for that copy.
        """
        lock = self._sync_lock(user)
        while not lock.acquire(timeout=SYNC_WAIT_SECONDS):
            if check is not None:
                check()
        try:
            return self._sync(user, check)
        finally:
            lock.release()

    def _sync(self, user, check):
        core = self.plugin.core
        for _attempt in range(SYNC_ATTEMPTS):
            if check is not None:
                check()
            status = core.history_status(self.account, user)
            if check is not None:
                check()
            if status["state"] == "idle":
                return self.context(user)
            local = self.context(user)
            reset = (local is None or local["source_id"] != status.get("sourceFingerprint") or
                     local["epoch"] != status.get("epoch"))
            if reset:
                if check is not None:
                    check()
                with self.lock:
                    connection = self._connect()
                    with connection:
                        connection.execute("DELETE FROM context_messages WHERE user=?", (user,))
                        # Summaries of an older source or epoch never describe the new conversation. The first
                        # copy keeps the summaries migrated from Core's database: their keys address the content.
                        if local is not None:
                            connection.execute("DELETE FROM summaries WHERE user=?", (user,))
                        connection.execute("DELETE FROM context WHERE user=?", (user,))
            else:
                # The recent end is where Core's projection can change; re-copy from the first difference.
                tail = list(reversed(self._rows("SELECT * FROM context_messages WHERE user=? ORDER BY seq DESC LIMIT ?",
                                                 (user, VERIFY_TAIL))))
                if tail:
                    verdict = core.post("history/verify", {"account": self.account, "user": user,
                                                           "items": [{"id": row["msg_id"], "hash": _hash(row)}
                                                                     for row in tail]})
                    changed = [row["seq"] for row, item in zip(tail, verdict["results"]) if item["state"] != "same"]
                    if changed:
                        if check is not None:
                            check()
                        self._write("DELETE FROM context_messages WHERE user=? AND seq>=?", (user, min(changed)))
            last = self._row("SELECT seq FROM context_messages WHERE user=? ORDER BY seq DESC LIMIT 1", (user,))
            after = last["seq"] if last else 0
            while True:
                if check is not None:
                    check()
                page = core.history_messages(self.account, user, after=after, limit=500)
                if check is not None:
                    check()
                messages = page["messages"]
                if messages:
                    self._write("INSERT OR REPLACE INTO context_messages(user,seq,msg_id,sort_key,group_chat,side,"
                                "sender_id,sender_name,kind,text,time_ms,raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                [_row(view, user) for view in messages], many=True)
                    after = messages[-1]["seq"]
                if page.get("next") is None:
                    break
            final = core.history_status(self.account, user)
            if check is not None:
                check()
            if (final.get("sourceFingerprint"), final.get("epoch")) != (status.get("sourceFingerprint"),
                                                                       status.get("epoch")):
                continue
            self._write(
                "INSERT INTO context(user,state,revision,read_count,total_count,error,source_id,epoch,synced_at_ms) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user) DO UPDATE SET state=excluded.state,"
                "revision=excluded.revision,read_count=excluded.read_count,total_count=excluded.total_count,"
                "error=excluded.error,source_id=excluded.source_id,epoch=excluded.epoch,"
                "synced_at_ms=excluded.synced_at_ms",
                (user, final["state"], final.get("revision") or 0, after, final.get("totalCount"),
                 final.get("error"), final.get("sourceFingerprint"), final.get("epoch"), int(time.time() * 1000)))
            return self.context(user)
        raise AdvisorError("context-error", "聊天数据来源已经变化")


class Accounts:
    """Factory and lifecycle owner of the per-account databases (``<dataDir>/accounts/<sha256>``)."""

    def __init__(self, plugin, data_dir=None):
        self.plugin = plugin
        self.data_root = _checked_root(Path(data_dir if data_dir is not None else plugin.data_dir) / "accounts")
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
                database = self._accounts[account] = AccountData(self.plugin, self.data_root, account)
            return database

    def directory(self, account):
        """The account's plugin folder; the engine keeps its state in ``<folder>/engine``."""
        return self.data_root / hashlib.sha256(account.encode("utf-8")).hexdigest()

    def block_account(self, account):
        with self.lock:
            self._blocked.add(account)

    def unblock_account(self, account):
        with self.lock:
            if not self._closed:
                self._blocked.discard(account)

    def release_account(self, account):
        """Close one account's database so its files may be removed."""
        with self.lock:
            database = self._accounts.pop(account, None)
        if database is not None:
            database.close()

    def remove_account(self, account):
        with self.lock:
            database = self._accounts.pop(account, None)
        if database is not None:
            return database.remove()
        return AccountData(self.plugin, self.data_root, account).remove()

    def close(self):
        with self.lock:
            self._closed = True
            databases = list(self._accounts.values())
            self._accounts.clear()
        for database in databases:
            database.close()
