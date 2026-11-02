"""Private, bounded diagnostic storage. No business database is ever opened here.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

SIDE_LIMIT = 16 * 1024 * 1024
SPACE_LIMIT = 500 * 1024 * 1024
RECORD_LIMIT = 10000
BODY_KINDS = frozenset(("request", "response", "text", "events", "result"))


def protect_directory(path):
    """Keep inherited read access off the diagnostics directory."""
    if os.name != "nt":
        path.chmod(0o700)
        return
    import ctypes
    from ctypes import wintypes
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    descriptor = ctypes.c_void_p()
    convert = security.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD))
    convert.restype = wintypes.BOOL
    # The owner, SYSTEM and Administrators retain access; files inherit this ACL.
    if not convert("D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        apply = security.SetFileSecurityW
        apply.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
        apply.restype = wintypes.BOOL
        if not apply(str(path), 0x80000004, descriptor):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.LocalFree.argtypes = (ctypes.c_void_p,)
        kernel.LocalFree(descriptor)


def atomic_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class TraceStore:
    """One connection, owned by the service lock; DELETE journals and secure deletes.

    The durable deletion intent is committed before payload removal. A restart
    replays intents before exposing the database, including journal recovery.
    """

    def __init__(self, root, *, side_limit=SIDE_LIMIT, space_limit=SPACE_LIMIT, record_limit=RECORD_LIMIT):
        self.root = Path(root) / ".local" / "model-traces"
        self.root.mkdir(parents=True, exist_ok=True)
        protect_directory(self.root)
        self.side_limit, self.space_limit, self.record_limit = side_limit, space_limit, record_limit
        self.settings_path = self.root / "settings.json"
        self.fence_path = self.root / "deletion.json"
        self.runtime_path = self.root / "runtime.json"
        self.recovery_gap = None
        self.db = sqlite3.connect(self.root / "traces.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA secure_delete=ON")
        self.db.execute("PRAGMA auto_vacuum=FULL")
        self.db.execute("PRAGMA temp_store=MEMORY")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS records (
          id TEXT PRIMARY KEY, account TEXT NOT NULL, contact TEXT, session TEXT,
          task TEXT NOT NULL, started REAL NOT NULL, active INTEGER NOT NULL,
          metadata TEXT NOT NULL, request_bytes INTEGER NOT NULL DEFAULT 0,
          response_bytes INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS scope_time ON records(account, started DESC);
        CREATE INDEX IF NOT EXISTS task_scope ON records(account, task);
        CREATE TABLE IF NOT EXISTS chunks (
          record TEXT NOT NULL, seq INTEGER NOT NULL, content TEXT NOT NULL,
          chunk INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
          bytes INTEGER NOT NULL, final INTEGER NOT NULL,
          digest TEXT, byte_offset INTEGER NOT NULL DEFAULT -1, source_bytes INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(record, seq, content, chunk));
        CREATE INDEX IF NOT EXISTS body_chunks ON chunks(record, kind, seq, chunk);
        CREATE TABLE IF NOT EXISTS events (
          record TEXT NOT NULL, seq INTEGER NOT NULL, digest TEXT NOT NULL,
          PRIMARY KEY(record, seq));
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(chunks)")}
        for name, definition in (("digest", "TEXT"), ("byte_offset", "INTEGER NOT NULL DEFAULT -1"),
                                 ("source_bytes", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self.db.execute("ALTER TABLE chunks ADD COLUMN " + name + " " + definition)
        self.db.commit()
        self.settings = {"enabled": False, "retentionDays": 7}
        if self.settings_path.exists():
            saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(saved.get("enabled"), bool):
                self.settings["enabled"] = saved["enabled"]
            if saved.get("retentionDays") in (1, 7, 30):
                self.settings["retentionDays"] = saved["retentionDays"]
        self.deletion = {"version": 0, "pending": None}
        if self.fence_path.exists():
            self.deletion = json.loads(self.fence_path.read_text(encoding="utf-8"))
        if self.deletion.get("pending"):
            self._finish_delete(self.deletion["pending"])
        if self.runtime_path.exists():
            try:
                runtime = json.loads(self.runtime_path.read_text(encoding="utf-8"))
                if runtime.get("running") and self.settings["enabled"]:
                    self.recovery_gap = {"startedAt": runtime.get("startedAt"), "finishedAt": time.time(),
                        "reason": "上次进程未正常关闭，未落盘记录与丢失数量未知", "lostEvents": None}
            except (ValueError, OSError):
                pass
        atomic_json(self.runtime_path, {"running": True, "startedAt": time.time()})
        for row in self.db.execute("SELECT id, metadata FROM records WHERE active=1").fetchall():
            meta = json.loads(row["metadata"])
            meta.update(collectionStatus="interrupted", complete=False,
                        incompleteReason="进程中断，结果未知", transportStatus="unknown", status="unknown")
            self.update(row["id"], meta, active=False)
        self.db.commit()

    @property
    def delete_version(self):
        return self.deletion["version"]

    def save_settings(self, settings):
        atomic_json(self.settings_path, settings)
        self.settings = dict(settings)

    def usage(self):
        return sum(path.stat().st_size for path in self.root.iterdir() if path.is_file())

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM records WHERE COALESCE(json_extract(metadata,'$.kind'),'attempt')!='task-event'").fetchone()[0]

    def task_event_count(self):
        return self.db.execute("SELECT COUNT(*) FROM records WHERE json_extract(metadata,'$.kind')='task-event'").fetchone()[0]

    def insert(self, meta):
        self.db.execute("INSERT INTO records(id, account, contact, session, task, started, active, metadata) VALUES(?,?,?,?,?,?,?,?)",
                        (meta["recordId"], meta.get("accountId") or "", meta.get("contactId"),
                         meta.get("sessionId"), meta["taskId"], meta["startedAt"],
                         int(meta.get("collectionStatus") == "recording"), json.dumps(meta, ensure_ascii=False)))

    def metadata(self, record_id, account=None):
        row = self.db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not row or (account is not None and row["account"] != account):
            return None
        meta = json.loads(row["metadata"])
        meta["requestBytes"], meta["responseBytes"] = row["request_bytes"], row["response_bytes"]
        return meta

    def update(self, record_id, meta, active=None):
        self.db.execute("UPDATE records SET metadata=?" + (", active=?" if active is not None else "") + " WHERE id=?",
                        ((json.dumps(meta, ensure_ascii=False), int(active), record_id) if active is not None else
                         (json.dumps(meta, ensure_ascii=False), record_id)))

    def append(self, record_id, event):
        key = (record_id, event["eventSeq"], event.get("contentId", event["kind"]), event.get("chunkIndex", 0))
        existing = self.db.execute("SELECT text, kind, final, digest FROM chunks WHERE record=? AND seq=? AND content=? AND chunk=?", key).fetchone()
        content = event["text"]
        offset = event.get("byteOffset", -1)
        if not isinstance(offset, int) or offset < -1:
            return "conflict"
        digest = hashlib.sha256(json.dumps([content, event["kind"], bool(event.get("final")), offset], ensure_ascii=False).encode("utf-8")).hexdigest()
        if existing:
            if existing["digest"]:
                return "duplicate" if existing["digest"] == digest else "conflict"
            return "duplicate" if (existing["text"] == content and existing["kind"] == event["kind"] and bool(existing["final"]) == bool(event.get("final"))) else "conflict"
        side = "request_bytes" if event["kind"] == "request" else "response_bytes"
        used = self.db.execute("SELECT " + side + " FROM records WHERE id=?", (record_id,)).fetchone()[0]
        encoded = content.encode("utf-8")
        remaining = max(0, self.side_limit - used)
        limited = encoded[:remaining].decode("utf-8", errors="ignore")
        size = len(limited.encode("utf-8"))
        self.db.execute("INSERT INTO chunks(record,seq,content,chunk,kind,text,bytes,final,digest,byte_offset,source_bytes) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)", (*key, event["kind"], limited, size, int(bool(event.get("final"))), digest, offset, len(encoded)))
        self.db.execute("UPDATE records SET " + side + "=" + side + "+? WHERE id=?", (size, record_id))
        return "truncated" if size < len(encoded) else "saved"

    def missing_chunks(self, record_id):
        groups = self.db.execute("SELECT content,seq,COUNT(*) AS n,MAX(chunk) AS last,SUM(final) AS done,"
            "MAX(CASE WHEN final=1 THEN chunk ELSE -1 END) AS final_chunk FROM chunks WHERE record=? GROUP BY content,seq", (record_id,)).fetchall()
        if any(row["last"] + 1 != row["n"] or row["done"] != 1 or row["final_chunk"] != row["last"] for row in groups):
            return True
        for group in groups:
            offset = 0
            for chunk in self.db.execute("SELECT byte_offset,source_bytes FROM chunks WHERE record=? AND content=? AND seq=? ORDER BY chunk",
                                         (record_id, group["content"], group["seq"])):
                if chunk["byte_offset"] >= 0 and chunk["byte_offset"] != offset:
                    return True
                offset += chunk["source_bytes"]
        return False

    def body(self, record_id, kind, offset=0, limit=65536):
        total = self.db.execute("SELECT COALESCE(SUM(bytes),0) FROM chunks WHERE record=? AND kind=?", (record_id, kind)).fetchone()[0]
        parts, position, length = [], 0, 0
        for row in self.db.execute("SELECT text,bytes FROM chunks WHERE record=? AND kind=? ORDER BY seq, content, chunk", (record_id, kind)):
            if position + row["bytes"] <= offset:
                position += row["bytes"]
                continue
            raw = row["text"].encode("utf-8")
            begin = max(0, offset - position)
            raw = raw[begin:begin + limit - length]
            parts.append(raw)
            length += len(raw)
            position += row["bytes"]
            if length >= limit:
                break
        raw = b"".join(parts)
        # Keep pagination offsets at UTF-8 boundaries.
        text = raw.decode("utf-8", errors="ignore")
        end = offset + len(text.encode("utf-8"))
        return {"text": text, "offset": offset, "nextOffset": end if end < total else None, "totalBytes": total}

    def list(self, account, filters):
        clauses, values = ["account=?"], [account]
        if not filters.get("taskId"):
            clauses.append("(COALESCE(json_extract(metadata,'$.kind'),'attempt')!='task-event' OR NOT EXISTS "
                           "(SELECT 1 FROM records AS attempt WHERE attempt.account=records.account AND attempt.task=records.task "
                           "AND COALESCE(json_extract(attempt.metadata,'$.kind'),'attempt')!='task-event'))")
        for name, column in (("user", "contact"), ("taskId", "task")):
            if filters.get(name):
                clauses.append(column + "=?")
                values.append(filters[name])
        start, end = filters.get("from"), filters.get("to")
        if start is not None or end is not None:
            times, bounds = [], []
            if start is not None:
                times.append("started>=?")
                value = float(start)
                bounds.append(value / 1000 if value > 1e12 else value)
            if end is not None:
                times.append("started<?")
                value = float(end)
                bounds.append(value / 1000 if value > 1e12 else value)
            clauses.append("((" + " AND ".join(times) + ")" + (" OR active=1" if filters.get("includeActive") in (True, "true") else "") + ")")
            values.extend(bounds)
        for name in ("source", "model"):
            if filters.get(name):
                clauses.append("json_extract(metadata, '$." + name + "')=?")
                values.append(filters[name])
        status = filters.get("status")
        if status:
            clauses.append("json_extract(metadata, '$.status')=?")
            values.append(status)
        if filters.get("incomplete") in (True, "true", "1"):
            clauses.append("COALESCE(json_extract(metadata, '$.complete'),0)=0")
        if filters.get("search"):
            clauses.append("(id LIKE ? OR task LIKE ? OR json_extract(metadata,'$.model') LIKE ? OR json_extract(metadata,'$.errorCode') LIKE ?)")
            values.extend(["%" + filters["search"][:256] + "%"] * 4)
        where = " AND ".join(clauses)
        total = self.db.execute("SELECT COUNT(*) FROM records WHERE " + where, values).fetchone()[0]
        limit = min(50, max(1, int(filters.get("limit", 50))))
        offset = max(0, int(filters.get("offset", 0)))
        rows = self.db.execute("SELECT metadata FROM records WHERE " + where + " ORDER BY started DESC,id DESC LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
        return {"records": [json.loads(row[0]) for row in rows], "total": total, "offset": offset,
                "nextOffset": offset + len(rows) if offset + len(rows) < total else None}

    def delete(self, scope):
        if self.deletion.get("pending"):
            # A different cleanup request must not erase an earlier durable
            # intent after a disk/SQLite failure interrupted its execution.
            self._finish_delete(self.deletion["pending"])
        intent = {**scope, "id": uuid.uuid4().hex}
        self.deletion = {"version": self.delete_version + 1, "pending": intent}
        atomic_json(self.fence_path, self.deletion)
        self._finish_delete(intent)

    def _finish_delete(self, scope):
        clauses, values = [], []
        for key, column in (("accountId", "account"), ("contactId", "contact"), ("sessionId", "session")):
            if key in scope:
                clauses.append(column + "=?")
                values.append(scope[key] or "")
        if "recordIds" in scope:
            ids = scope["recordIds"]
            clauses.append("id IN (" + ",".join("?" for _ in ids) + ")")
            values.extend(ids)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        for table in ("chunks", "events"):
            self.db.execute("DELETE FROM " + table + " WHERE record IN (SELECT id FROM records" + where + ")", values)
        self.db.execute("DELETE FROM records" + where, values)
        self.db.commit()
        # Reclaim free pages and remove journal-managed copies before success.
        self.db.execute("VACUUM")
        self.deletion["pending"] = None
        atomic_json(self.fence_path, self.deletion)

    def expired(self, now=None):
        cutoff = (time.time() if now is None else now) - self.settings["retentionDays"] * 86400
        return [row[0] for row in self.db.execute("SELECT id FROM records WHERE started<?", (cutoff,))]

    def reclaim(self, reserve=0, reserve_bytes=65536):
        ids = self.expired()
        if ids:
            self.delete({"recordIds": ids})
        while (self.count() + reserve > self.record_limit or self.task_event_count() > self.record_limit or
               self.usage() >= self.space_limit - reserve_bytes):
            rows = self.db.execute("SELECT id FROM records WHERE active=0 ORDER BY started LIMIT 100").fetchall()
            if not rows:
                return False
            self.delete({"recordIds": [row[0] for row in rows]})
        return True

    def close(self):
        self.db.commit()
        self.db.close()
        atomic_json(self.runtime_path, {"running": False, "finishedAt": time.time()})
