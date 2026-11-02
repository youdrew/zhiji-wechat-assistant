"""Core-side authorization storage for the MCP plugin: client profiles, contact grants,
request dedup and the call record.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Everything lives in ``.local/core/mcp/mcp.sqlite3``. Core owns the file; MCP clients
reach it only through the capability proxy and the settings page only through the
plugin interface. A pairing secret is verified, never stored: the profile keeps a
per-client salt and a SHA-256 verification hash of the secret that was shown once.
Call records keep ids, actions, result codes and durations -- never message text,
credentials or model keys. Retired client ids are never reused.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

MODES = ("full", "per_contact")
SUBJECT_TYPES = ("wechat_contact", "external_contact")
MAX_CALL_RECORDS = 500
CALL_TTL_MS = 7 * 24 * 60 * 60 * 1000
DEDUP_TTL_MS = 24 * 60 * 60 * 1000
# 写入结果只存紧凑的新版本/状态（契约不含正文），仍设上限兜底。
DEDUP_RESULT_MAX_CHARS = 8192
MAX_SUBJECTS_PER_CALL = 32
NAME_LIMIT = 64
CLIENT_ID_LIMIT = 128
SUBJECT_ID_LIMIT = 512
ACCOUNT_ID_LIMIT = 128

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
  client_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  mode TEXT NOT NULL,
  global_management INTEGER NOT NULL DEFAULT 0,
  auth_version INTEGER NOT NULL DEFAULT 1,
  credential_salt TEXT NOT NULL,
  credential_hash TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  last_used_at_ms INTEGER
);
CREATE TABLE IF NOT EXISTS retired_clients (
  client_id TEXT PRIMARY KEY,
  retired_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS contact_grants (
  client_id TEXT NOT NULL,
  source_fingerprint TEXT NOT NULL,
  account_id TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY (client_id, source_fingerprint, account_id, subject_type, subject_id)
);
CREATE TABLE IF NOT EXISTS request_dedup (
  client_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  action TEXT NOT NULL,
  params_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  expires_at_ms INTEGER NOT NULL,
  result_json TEXT,
  PRIMARY KEY (client_id, request_id)
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS call_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  time_ms INTEGER NOT NULL,
  client_id TEXT NOT NULL,
  action TEXT NOT NULL,
  account_id TEXT,
  subjects TEXT,
  result_code TEXT NOT NULL,
  duration_ms INTEGER
);
CREATE INDEX IF NOT EXISTS call_log_time ON call_log(time_ms);
"""


class McpStoreError(Exception):
    """A management request the store rejects; ``status``/``code`` go to the settings page."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def now_ms():
    return int(time.time() * 1000)


def credential_hash(salt_hex, secret):
    """Verification material of a pairing secret; the secret itself is never kept."""
    return hashlib.sha256((salt_hex + secret).encode("utf-8")).hexdigest()


def verify_credential(salt_hex, stored_hash, secret):
    if not isinstance(secret, str) or not 1 <= len(secret) <= 256:
        return False
    return hmac.compare_digest(credential_hash(salt_hex, secret), stored_hash)


def new_client_id():
    return "mcp_" + secrets.token_hex(8)


def new_secret():
    return "zmc_" + secrets.token_hex(32)


def _json(value):
    return None if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value, default=None):
    if value is None:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


class McpStore:
    """Transactional authorization records under ``.local/core/mcp``."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "mcp.sqlite3"
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        # A dedup record for an external side effect must survive a crash, so this
        # store pays for FULL synchronous despite being small.
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(SCHEMA)
        # Listeners run after any authorization version bump or profile removal, so
        # dependent state (task bindings, caches) can be invalidated at the source.
        self.version_listeners = []
        # Phase-1 databases predate the recorded write result; add the column in place.
        names = {row[1] for row in self.connection.execute("PRAGMA table_info(request_dedup)")}
        if "result_json" not in names:
            self.connection.execute("ALTER TABLE request_dedup ADD COLUMN result_json TEXT")

    def add_version_listener(self, listener):
        """``listener(clientId)`` after a version bump or profile removal; never raises."""
        with self.lock:
            self.version_listeners.append(listener)

    def _notify_version(self, client_id):
        for listener in list(self.version_listeners):
            try:
                listener(client_id)
            except Exception:
                pass  # 侦听器故障不回滚授权变更本身。

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

    # -- client profiles -----------------------------------------------------
    def client(self, client_id):
        if not isinstance(client_id, str) or not client_id:
            return None
        with self.lock:
            row = self.connection.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
        return dict(row) if row is not None else None

    def create_profile(self, name, mode, global_management=False):
        """``{clientId, credential, ...}``; the credential is shown once and never stored."""
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_LIMIT:
            raise McpStoreError(400, "invalid-request", "请输入 1-64 字的客户端名称")
        if mode not in MODES:
            raise McpStoreError(400, "invalid-request", "授权模式不正确")
        if not isinstance(global_management, bool):
            raise McpStoreError(400, "invalid-request", "全局管理开关不正确")
        name = name.strip()
        stamp = now_ms()
        salt = secrets.token_hex(16)
        secret = new_secret()
        client_id = new_client_id()

        def apply(db):
            if db.execute("SELECT 1 FROM retired_clients WHERE client_id=?", (client_id,)).fetchone():
                raise McpStoreError(409, "client-id-unavailable", "客户端标识不可复用，请重试")
            db.execute(
                "INSERT INTO clients(client_id,name,enabled,mode,global_management,auth_version,"
                "credential_salt,credential_hash,created_at_ms,last_used_at_ms)"
                " VALUES(?,?,1,?,?,1,?,?,?,NULL)",
                (client_id, name, mode, int(global_management), salt, credential_hash(salt, secret), stamp))
            return client_id

        self._write(apply)
        return {"clientId": client_id, "name": name, "mode": mode, "globalManagement": global_management,
                "authVersion": 1, "enabled": True, "createdAt": stamp, "credential": secret}

    def regenerate_credential(self, client_id):
        """A new secret invalidates the old one and every cursor bound to the old version."""
        salt = secrets.token_hex(16)
        secret = new_secret()
        stamp = now_ms()

        def apply(db):
            found = db.execute("SELECT 1 FROM clients WHERE client_id=?", (client_id,)).fetchone()
            if not found:
                raise McpStoreError(404, "client-not-found", "客户端不存在")
            db.execute(
                "UPDATE clients SET credential_salt=?, credential_hash=?, auth_version=auth_version+1,"
                " last_used_at_ms=? WHERE client_id=?",
                (salt, credential_hash(salt, secret), stamp, client_id))
            return db.execute("SELECT auth_version FROM clients WHERE client_id=?", (client_id,)).fetchone()[0]

        version = self._write(apply)
        self._notify_version(client_id)
        return {"clientId": client_id, "credential": secret, "authVersion": version}

    def set_enabled(self, client_id, enabled):
        if not isinstance(enabled, bool):
            raise McpStoreError(400, "invalid-request", "启停状态不正确")

        def apply(db):
            found = db.execute("SELECT enabled FROM clients WHERE client_id=?", (client_id,)).fetchone()
            if not found:
                raise McpStoreError(404, "client-not-found", "客户端不存在")
            if found[0] == int(enabled):
                return None
            # Disabling must also invalidate in-flight operations, so it bumps the version.
            db.execute("UPDATE clients SET enabled=?, auth_version=auth_version+1 WHERE client_id=?",
                       (int(enabled), client_id))
            return None

        self._write(apply)
        self._notify_version(client_id)
        return self.client(client_id)

    def set_mode(self, client_id, mode):
        if mode not in MODES:
            raise McpStoreError(400, "invalid-request", "授权模式不正确")

        def apply(db):
            found = db.execute("SELECT mode FROM clients WHERE client_id=?", (client_id,)).fetchone()
            if not found:
                raise McpStoreError(404, "client-not-found", "客户端不存在")
            db.execute("UPDATE clients SET mode=?, auth_version=auth_version+1 WHERE client_id=?", (mode, client_id))

        self._write(apply)
        self._notify_version(client_id)
        return self.client(client_id)

    def set_global_management(self, client_id, enabled):
        if not isinstance(enabled, bool):
            raise McpStoreError(400, "invalid-request", "全局管理开关不正确")

        def apply(db):
            found = db.execute("SELECT global_management FROM clients WHERE client_id=?", (client_id,)).fetchone()
            if not found:
                raise McpStoreError(404, "client-not-found", "客户端不存在")
            db.execute("UPDATE clients SET global_management=?, auth_version=auth_version+1 WHERE client_id=?",
                       (int(enabled), client_id))

        self._write(apply)
        self._notify_version(client_id)
        return self.client(client_id)

    def revoke_profile(self, client_id):
        """Delete the profile and keep its id from ever being handed out again."""
        stamp = now_ms()

        def apply(db):
            found = db.execute("SELECT 1 FROM clients WHERE client_id=?", (client_id,)).fetchone()
            if not found:
                raise McpStoreError(404, "client-not-found", "客户端不存在")
            db.execute("DELETE FROM clients WHERE client_id=?", (client_id,))
            db.execute("DELETE FROM contact_grants WHERE client_id=?", (client_id,))
            db.execute("DELETE FROM request_dedup WHERE client_id=?", (client_id,))
            db.execute("INSERT OR IGNORE INTO retired_clients(client_id, retired_at_ms) VALUES(?,?)",
                       (client_id, stamp))

        self._write(apply)
        self._notify_version(client_id)
        return {"revoked": True}

    @staticmethod
    def profile_view(row):
        return {"clientId": row["client_id"], "name": row["name"], "enabled": bool(row["enabled"]),
                "mode": row["mode"], "globalManagement": bool(row["global_management"]),
                "authVersion": row["auth_version"], "createdAt": row["created_at_ms"],
                "lastUsedAt": row["last_used_at_ms"]}

    def list_profiles(self):
        with self.lock:
            rows = self.connection.execute(
                "SELECT c.*, (SELECT COUNT(*) FROM contact_grants g WHERE g.client_id=c.client_id)"
                " AS contact_count FROM clients c ORDER BY c.created_at_ms").fetchall()
        views = []
        for row in rows:
            view = self.profile_view(row)
            view["contactCount"] = row["contact_count"]
            views.append(view)
        return views

    def touch(self, client_id):
        with self.lock:
            self.connection.execute("UPDATE clients SET last_used_at_ms=? WHERE client_id=?",
                                    (now_ms(), client_id))

    # -- contact grants ------------------------------------------------------
    @staticmethod
    def _grant_values(client_id, source_fingerprint, account_id, subject_type, subject_id):
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= CLIENT_ID_LIMIT:
            raise McpStoreError(400, "invalid-request", "客户端标识不正确")
        if not isinstance(source_fingerprint, str) or not source_fingerprint:
            raise McpStoreError(400, "invalid-request", "安装指纹不正确")
        if not isinstance(account_id, str) or not 1 <= len(account_id) <= ACCOUNT_ID_LIMIT:
            raise McpStoreError(400, "invalid-request", "账号标识不正确")
        if subject_type not in SUBJECT_TYPES:
            raise McpStoreError(400, "invalid-request", "主体类型不正确")
        if not isinstance(subject_id, str) or not 1 <= len(subject_id) <= SUBJECT_ID_LIMIT:
            raise McpStoreError(400, "invalid-request", "主体标识不正确")
        return client_id, source_fingerprint, account_id, subject_type, subject_id

    def grant_contact(self, client_id, source_fingerprint, account_id, subject_type, subject_id):
        values = self._grant_values(client_id, source_fingerprint, account_id, subject_type, subject_id)
        if self.client(client_id) is None:
            raise McpStoreError(404, "client-not-found", "客户端不存在")

        def apply(db):
            db.execute("INSERT OR IGNORE INTO contact_grants(client_id,source_fingerprint,account_id,"
                       "subject_type,subject_id,created_at_ms) VALUES(?,?,?,?,?,?)", (*values, now_ms()))

        self._write(apply)
        return {"granted": True}

    def revoke_contact(self, client_id, source_fingerprint, account_id, subject_type, subject_id):
        """Revoking raises the auth version so old cursors and in-flight writes stop working."""
        values = self._grant_values(client_id, source_fingerprint, account_id, subject_type, subject_id)

        def apply(db):
            removed = db.execute("DELETE FROM contact_grants WHERE client_id=? AND source_fingerprint=?"
                                 " AND account_id=? AND subject_type=? AND subject_id=?", values).rowcount
            db.execute("UPDATE clients SET auth_version=auth_version+1 WHERE client_id=?", (client_id,))
            return removed

        applied = self._write(apply)
        # 撤回联系人后使依赖状态失效（游标、任务等），无论是否确有删除。
        self._notify_version(client_id)
        return {"revoked": bool(applied)}

    def has_grant(self, client_id, source_fingerprint, account_id, subject_type, subject_id):
        with self.lock:
            return self.connection.execute(
                "SELECT 1 FROM contact_grants WHERE client_id=? AND source_fingerprint=? AND account_id=?"
                " AND subject_type=? AND subject_id=?",
                (client_id, source_fingerprint, account_id, subject_type, subject_id)).fetchone() is not None

    def grants(self, client_id, source_fingerprint=None):
        with self.lock:
            if source_fingerprint is None:
                rows = self.connection.execute(
                    "SELECT * FROM contact_grants WHERE client_id=? ORDER BY account_id, subject_type, subject_id",
                    (client_id,)).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT * FROM contact_grants WHERE client_id=? AND source_fingerprint=?"
                    " ORDER BY account_id, subject_type, subject_id", (client_id, source_fingerprint)).fetchall()
        return [{"accountId": row["account_id"], "subjectType": row["subject_type"], "subjectId": row["subject_id"],
                 "sourceFingerprint": row["source_fingerprint"], "createdAt": row["created_at_ms"]}
                for row in rows]

    def revoke_account_grants(self, account_id):
        """Remove every client's grants for one account and bump each affected auth version.

        阶段 5 高影响删除的执行后步骤（§7）：被删账号的全部授权主体失效，依赖授
        权版本的游标、任务绑定与缓存随版本侦听一并失效。
        """
        if not isinstance(account_id, str) or not account_id:
            raise McpStoreError(400, "invalid-request", "账号标识不正确")

        def apply(db):
            rows = db.execute("SELECT DISTINCT client_id FROM contact_grants WHERE account_id=?",
                              (account_id,)).fetchall()
            db.execute("DELETE FROM contact_grants WHERE account_id=?", (account_id,))
            for (client_id,) in rows:
                db.execute("UPDATE clients SET auth_version=auth_version+1 WHERE client_id=?", (client_id,))
            return [row[0] for row in rows]

        affected = self._write(apply)
        for client_id in affected:
            self._notify_version(client_id)
        return affected

    def rotate_cursor_key(self):
        """Invalidate every outstanding pagination cursor at once.

        删除账号或清空数据后调用：游标本身没有可逐一吊销的服务端状态，换签名密
        钥使所有未完成游标立即失效，客户端重新分页即可。
        """
        key = secrets.token_hex(32)
        self._write(lambda db: db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('cursor_key',?)",
                                          (key,)))
        return key

    # -- cursors -------------------------------------------------------------
    def cursor_key(self):
        """A persisted random signing key so a client cannot forge a pagination cursor.

        Generated once per authorization database; regenerating (by deleting it) only
        invalidates outstanding cursors, never authorization state.
        """
        with self.lock:
            row = self.connection.execute("SELECT value FROM settings WHERE key='cursor_key'").fetchone()
            if row is not None and re.fullmatch(r"[0-9a-f]{64}", row[0] or ""):
                return row[0]
            key = secrets.token_hex(32)
            self._write(lambda db: db.execute("INSERT OR REPLACE INTO settings(key,value)"
                                              " VALUES('cursor_key',?)", (key,)))
            return key

    # -- master switch -------------------------------------------------------
    # The settings page's total switch: off refuses every handshake and call, and
    # persists across restarts. It is authorization state, so it lives in this store.
    def master_enabled(self):
        with self.lock:
            row = self.connection.execute("SELECT value FROM settings WHERE key='master_enabled'").fetchone()
        return row is None or row[0] == "1"

    def set_master_enabled(self, enabled):
        if not isinstance(enabled, bool):
            raise McpStoreError(400, "invalid-request", "总启停开关不正确")

        def apply(db):
            db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('master_enabled',?)",
                       ("1" if enabled else "0",))

        self._write(apply)
        return enabled

    # -- request dedup -------------------------------------------------------
    def dedup_begin(self, client_id, request_id, action, params_hash):
        """``fresh`` on first sight, ``replay`` for an identical retry; conflicting reuse is rejected.

        A record is persisted before the caller dispatches, so an external side effect
        can always be attributed to a durable request id.
        """
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 128 or any(
                ord(char) < 32 for char in request_id):
            raise McpStoreError(400, "invalid-request", "请求标识不正确")
        if not isinstance(action, str) or not action:
            raise McpStoreError(400, "invalid-request", "动作名称不正确")
        if not isinstance(params_hash, str) or not params_hash:
            raise McpStoreError(400, "invalid-request", "参数摘要不正确")
        stamp = now_ms()

        def apply(db):
            db.execute("DELETE FROM request_dedup WHERE expires_at_ms < ?", (stamp,))
            row = db.execute("SELECT action, params_hash FROM request_dedup WHERE client_id=? AND request_id=?",
                             (client_id, request_id)).fetchone()
            if row is not None:
                if row[0] != action or row[1] != params_hash:
                    raise McpStoreError(409, "request-id-conflict", "同一请求标识不能携带不同参数")
                return "replay"
            db.execute("INSERT INTO request_dedup(client_id,request_id,action,params_hash,state,"
                       "created_at_ms,expires_at_ms) VALUES(?,?,?,?,?,?,?)",
                       (client_id, request_id, action, params_hash, "recorded", stamp, stamp + DEDUP_TTL_MS))
            return "fresh"

        return self._write(apply)

    def dedup_complete(self, client_id, request_id, result=None, ok=True):
        """Record the outcome of one write so an identical retry returns it verbatim.

        ``done`` rows keep the compact result (new version/status -- contracts carry
        no message text); ``failed`` rows mean nothing was applied, so the same
        request id with the same parameters may safely re-execute. A row that stays
        ``recorded`` belongs to a request whose process died mid-write: its outcome
        is unknown and the proxy refuses to guess.
        """
        encoded = None
        if ok and result is not None:
            try:
                encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                encoded = json.dumps({"unrecorded": True})
            if len(encoded) > DEDUP_RESULT_MAX_CHARS:
                encoded = json.dumps({"unrecorded": True})

        def apply(db):
            db.execute("UPDATE request_dedup SET state=?, result_json=? WHERE client_id=? AND request_id=?",
                       ("done" if ok else "failed", encoded, client_id, request_id))

        self._write(apply)

    def dedup_result(self, client_id, request_id):
        """``{"state", "result"}`` of a seen request id, or ``None`` when unknown."""
        if not isinstance(request_id, str) or not request_id:
            return None
        with self.lock:
            row = self.connection.execute(
                "SELECT state, result_json FROM request_dedup WHERE client_id=? AND request_id=?",
                (client_id, request_id)).fetchone()
        if row is None:
            return None
        return {"state": row[0], "result": _load(row[1]) if row[1] else None}

    def dedup_result_for(self, client_id, request_id, action):
        """The finished record of one request id, only for the action that began it.

        阶段 4 的结果查询按发起客户端归属；跨动作读取同一请求标识一律视为未知，
        避免把其他写工具的结果冒充发送结果。
        """
        if not isinstance(request_id, str) or not request_id or not isinstance(action, str):
            return None
        with self.lock:
            row = self.connection.execute(
                "SELECT action, state, result_json FROM request_dedup WHERE client_id=? AND request_id=?",
                (client_id, request_id)).fetchone()
        if row is None or row[0] != action:
            return None
        return {"state": row[1], "result": _load(row[2]) if row[2] else None}

    # -- call record ---------------------------------------------------------
    def log_call(self, client_id, action, *, account_id=None, subjects=None, result_code="ok", duration_ms=None):
        """Ids and result codes only; message text, keys and secrets never reach this table."""
        if isinstance(subjects, list) and len(subjects) > MAX_SUBJECTS_PER_CALL:
            subjects = subjects[:MAX_SUBJECTS_PER_CALL]
        stamp = now_ms()

        def apply(db):
            db.execute("INSERT INTO call_log(time_ms,client_id,action,account_id,subjects,result_code,duration_ms)"
                       " VALUES(?,?,?,?,?,?,?)",
                       (stamp, client_id, action, account_id, _json(subjects), result_code, duration_ms))
            db.execute("DELETE FROM call_log WHERE time_ms < ?", (stamp - CALL_TTL_MS,))
            keep = db.execute("SELECT id FROM call_log ORDER BY id DESC LIMIT ?", (MAX_CALL_RECORDS,)).fetchall()
            if len(keep) >= MAX_CALL_RECORDS:
                db.execute("DELETE FROM call_log WHERE id < ?", (keep[-1][0],))

        self._write(apply)

    def recent_calls(self, limit=50):
        if not isinstance(limit, int) or not 1 <= limit <= MAX_CALL_RECORDS:
            raise McpStoreError(400, "invalid-request", "调用记录条数不正确")
        with self.lock:
            rows = self.connection.execute(
                "SELECT * FROM call_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"time": row["time_ms"], "clientId": row["client_id"], "action": row["action"],
                 "accountId": row["account_id"], "subjects": _load(row["subjects"], []),
                 "resultCode": row["result_code"], "durationMs": row["duration_ms"]} for row in rows]
