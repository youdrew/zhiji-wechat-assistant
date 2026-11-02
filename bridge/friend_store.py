"""Per-account human data of 知己分析 inside Core's account database (``advisor.sqlite3``).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The tables live next to ``context_messages`` in ``advisor.sqlite3`` so the
existing account clear removes memories, evidence, jobs, settings and drafts
with the rest of the account projection. Raw chat text is never copied here
beyond short evidence excerpts; evidence identifies the projected message by
source fingerprint, message id and content hash instead of a projection ``seq``.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
import uuid

from core_contracts import AdvisorError

# v9 adds unified analysis; v10 persists its update/rebuild mode for resuming.
SCHEMA_VERSION = 10
TAGS = ("基本信息", "经历", "性格", "爱好", "偏好与忌口", "边界", "共同事件")
STATUSES = ("pending", "confirmed", "rejected")
EVIDENCE_STATES = ("ok", "recalled", "changed", "missing", "unready", "source-changed")
MAX_MEMORY_CHARS = 300
MAX_NOTE_CHARS = 2000
MAX_SETTING_CHARS = 4000
MAX_DRAFT_BYTES = 32 * 1024

FRIEND_SCHEMA = """
CREATE TABLE IF NOT EXISTS friend_state (
    user TEXT PRIMARY KEY,
    memory_version INTEGER NOT NULL DEFAULT 0,
    clear_epoch INTEGER NOT NULL DEFAULT 0,
    profile_revision INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    text TEXT NOT NULL,
    norm TEXT NOT NULL,
    status TEXT NOT NULL,
    origin TEXT NOT NULL,
    user_corrected INTEGER NOT NULL DEFAULT 0,
    event_time TEXT,
    first_mention_ms INTEGER,
    proposal_key TEXT,
    relation TEXT,
    related_id TEXT,
    job_id TEXT,
    model_fp TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS memories_user ON memories(user, status, updated_at_ms DESC);
CREATE UNIQUE INDEX IF NOT EXISTS memories_proposal ON memories(user, proposal_key);
CREATE TABLE IF NOT EXISTS memory_versions (
    memory_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    text TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    event_time TEXT,
    reason TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY(memory_id, version)
);
CREATE TABLE IF NOT EXISTS memory_evidence (
    memory_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    source_fp TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    sort_key TEXT,
    cursor TEXT,
    role TEXT NOT NULL,
    time_ms INTEGER,
    excerpt TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ok',
    checked_at_ms INTEGER,
    PRIMARY KEY(memory_id, ordinal)
);
CREATE INDEX IF NOT EXISTS memory_evidence_msg ON memory_evidence(msg_id);
CREATE TABLE IF NOT EXISTS memory_exclusions (
    user TEXT NOT NULL,
    norm TEXT NOT NULL,
    text TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY(user, norm)
);
CREATE TABLE IF NOT EXISTS memory_dedup (
    user TEXT NOT NULL,
    proposal_key TEXT NOT NULL,
    candidate_text TEXT NOT NULL,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    job_id TEXT,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY(user, proposal_key)
);
CREATE TABLE IF NOT EXISTS friend_settings (
    user TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS account_profile (
    id INTEGER PRIMARY KEY CHECK(id = 1),
    background TEXT NOT NULL,
    style TEXT NOT NULL,
    version INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS friend_observations (
    id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    judgement TEXT NOT NULL,
    note TEXT NOT NULL,
    dimension TEXT,
    version INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS friend_observations_user ON friend_observations(user, updated_at_ms DESC);
CREATE TABLE IF NOT EXISTS friend_drafts (
    user TEXT NOT NULL,
    entry TEXT NOT NULL,
    generation INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    base_version INTEGER,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY(user, entry)
);
CREATE TABLE IF NOT EXISTS friend_schemes (
    user TEXT PRIMARY KEY,
    plugin TEXT NOT NULL,
    scheme TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS friend_draft_closed (
    user TEXT NOT NULL,
    entry TEXT NOT NULL,
    generation INTEGER NOT NULL,
    PRIMARY KEY(user, entry)
);
"""


def now_ms():
    return int(time.time() * 1000)


def normalize_text(value):
    """Compare memories by meaning-bearing characters, not punctuation or width."""
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)


def content_hash(row):
    facts = [row.get("sort_key"), row.get("side"), row.get("sender_id") or "", row.get("kind") or "",
             row.get("text") or ""]
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def chain_step(digest, row):
    return hashlib.sha256((digest + "\x00" + row["msg_id"] + "\x00" + content_hash(row)).encode("utf-8")).hexdigest()


def clean_text(value, field, maximum, *, allow_empty=False):
    if not isinstance(value, str):
        raise AdvisorError("invalid-request", field + " 格式不正确")
    value = value.replace("\r\n", "\n").strip()
    if (not value and not allow_empty) or len(value) > maximum or "\x00" in value:
        raise AdvisorError("invalid-request", field + " 长度不正确")
    return value


def clean_tags(value):
    if not isinstance(value, list) or not 1 <= len(value) <= len(TAGS):
        raise AdvisorError("invalid-request", "请选择至少一个标签")
    tags = [tag for tag in TAGS if tag in value]
    if len(tags) != len(set(value)) or any(tag not in TAGS for tag in value):
        raise AdvisorError("invalid-request", "标签不在固定集合中")
    return tags


def clean_event_time(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", value):
        raise AdvisorError("invalid-request", "事件时间格式应为 YYYY、YYYY-MM 或 YYYY-MM-DD")
    return value


def backup_before_migration(connection, database_path):
    """Keep one byte-consistent copy of an older account database before upgrading it."""
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if not 0 < current < SCHEMA_VERSION:
        return None
    backup = database_path.with_name("advisor.v%d-backup.sqlite3" % current)
    if not backup.exists():
        temporary = backup.with_name(backup.name + "." + uuid.uuid4().hex + ".tmp")
        target = sqlite3.connect(str(temporary))
        try:
            connection.backup(target)
        finally:
            target.close()
        temporary.replace(backup)
    return backup


def ensure_schema(connection):
    """Create the human-data friend tables."""
    connection.executescript(FRIEND_SCHEMA)
    connection.execute("PRAGMA user_version=%d" % max(SCHEMA_VERSION, connection.execute("PRAGMA user_version").fetchone()[0]))


def _tags(row):
    return json.loads(row["tags_json"])


class FriendStore:
    """Transactional memory, job and settings records for one account database."""

    def __init__(self, database):
        self.database = database

    # -- plumbing ------------------------------------------------------------
    def _connection(self):
        # AccountDatabase._connect creates the friend tables with its own schema.
        return self.database._connect()

    def _rows(self, sql, params=()):
        with self.database.lock:
            return [dict(row) for row in self._connection().execute(sql, params)]

    def _row(self, sql, params=()):
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def write(self, action):
        """Run ``action(connection)`` in one transaction under the account lock."""
        with self.database.lock:
            connection = self._connection()
            with connection:
                return action(connection)

    # -- versions ------------------------------------------------------------
    def state(self, user):
        row = self._row("SELECT * FROM friend_state WHERE user=?", (user,))
        return row or {"user": user, "memory_version": 0, "clear_epoch": 0, "profile_revision": 0,
                       "updated_at_ms": 0}

    @staticmethod
    def bump(connection, user, *, clear=False, profile=False):
        connection.execute(
            "INSERT INTO friend_state(user,memory_version,clear_epoch,profile_revision,updated_at_ms) "
            "VALUES(?,1,?,?,?) ON CONFLICT(user) DO UPDATE SET memory_version=memory_version+1,"
            "clear_epoch=clear_epoch+?,profile_revision=profile_revision+?,updated_at_ms=excluded.updated_at_ms",
            (user, int(clear), int(profile), now_ms(), int(clear), int(profile)))

    def account_revision(self):
        row = self._row("SELECT version FROM account_profile WHERE id=1")
        return row["version"] if row else 0

    # -- memory reads ----------------------------------------------------------
    def memory(self, user, memory_id):
        row = self._row("SELECT * FROM memories WHERE id=? AND user=?", (memory_id, user))
        if row is None:
            raise AdvisorError("not-found", "记忆不存在或已被删除")
        return row

    def memories(self, user, statuses=STATUSES):
        marks = ",".join("?" for _ in statuses)
        return self._rows("SELECT * FROM memories WHERE user=? AND status IN (" + marks + ") "
                          "ORDER BY created_at_ms, id", (user, *statuses))

    def evidence(self, memory_ids):
        result = {}
        memory_ids = list(memory_ids)
        for start in range(0, len(memory_ids), 400):
            chunk = memory_ids[start:start + 400]
            marks = ",".join("?" for _ in chunk)
            for row in self._rows("SELECT * FROM memory_evidence WHERE memory_id IN (" + marks + ") "
                                  "ORDER BY memory_id, ordinal", tuple(chunk)):
                result.setdefault(row["memory_id"], []).append(row)
        return result

    def evidence_for_user(self, user):
        return self._rows("SELECT memory_evidence.* FROM memory_evidence JOIN memories ON "
                          "memories.id=memory_evidence.memory_id WHERE memories.user=?", (user,))

    def set_evidence_status(self, user, changes):
        """Persist re-verification results; only confirmed records change the advisor revision."""
        if not changes:
            return 0

        def action(connection):
            confirmed = False
            for memory_id, ordinal, status in changes:
                row = connection.execute("SELECT memories.status FROM memory_evidence JOIN memories ON "
                                         "memories.id=memory_evidence.memory_id WHERE memory_id=? AND ordinal=? "
                                         "AND memories.user=?", (memory_id, ordinal, user)).fetchone()
                if row is None:
                    continue
                connection.execute("UPDATE memory_evidence SET status=?,checked_at_ms=? WHERE memory_id=? AND ordinal=?",
                                   (status, now_ms(), memory_id, ordinal))
                confirmed = confirmed or row["status"] == "confirmed"
            if confirmed:
                self.bump(connection, user, profile=True)
            return len(changes)
        return self.write(action)

    def exclusions(self, user):
        return self._rows("SELECT * FROM memory_exclusions WHERE user=? ORDER BY created_at_ms", (user,))

    def dedup_links(self, user, limit=200):
        return self._rows("SELECT * FROM memory_dedup WHERE user=? ORDER BY created_at_ms DESC LIMIT ?", (user, limit))

    def versions(self, memory_id):
        return self._rows("SELECT * FROM memory_versions WHERE memory_id=? ORDER BY version", (memory_id,))

    # -- memory mutations ------------------------------------------------------
    @staticmethod
    def _locked(connection, user, memory_id):
        row = connection.execute("SELECT * FROM memories WHERE id=? AND user=?", (memory_id, user)).fetchone()
        if row is None:
            raise AdvisorError("not-found", "记忆不存在或已被删除")
        return dict(row)

    @staticmethod
    def _check_version(row, expected):
        if expected is not None and (type(expected) is not int or row["version"] != expected):
            raise AdvisorError("request-conflict", "这条记忆已被其他操作修改，请查看最新内容后再处理")

    @staticmethod
    def _snapshot(connection, row, reason):
        connection.execute("INSERT OR IGNORE INTO memory_versions(memory_id,version,text,tags_json,event_time,"
                           "reason,created_at_ms) VALUES(?,?,?,?,?,?,?)",
                           (row["id"], row["version"], row["text"], row["tags_json"], row["event_time"],
                            reason, now_ms()))

    @staticmethod
    def _exclude(connection, row, reason):
        connection.execute("INSERT OR REPLACE INTO memory_exclusions(user,norm,text,tags_json,reason,created_at_ms) "
                           "VALUES(?,?,?,?,?,?)", (row["user"], row["norm"], row["text"], row["tags_json"],
                                                   reason, now_ms()))

    @staticmethod
    def insert_evidence(connection, memory_id, items, start=0):
        for offset, item in enumerate(items):
            connection.execute(
                "INSERT OR REPLACE INTO memory_evidence(memory_id,ordinal,source_fp,msg_id,content_hash,sort_key,cursor,"
                "role,time_ms,excerpt,status,checked_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (memory_id, start + offset, item["source_fp"], item["msg_id"], item["content_hash"],
                 item.get("sort_key"), item.get("cursor"), item["role"], item.get("time_ms"),
                 str(item.get("excerpt") or "")[:500], item.get("status", "ok"), now_ms()))

    def create_user_memory(self, user, text, tags, event_time=None):
        text = clean_text(text, "记忆内容", MAX_MEMORY_CHARS)
        tags, event_time = clean_tags(tags), clean_event_time(event_time)

        def action(connection):
            stamp = now_ms()
            memory_id = "mem_" + uuid.uuid4().hex
            connection.execute(
                "INSERT INTO memories(id,user,tags_json,text,norm,status,origin,event_time,proposal_key,"
                "created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,'confirmed','user',?,?,?,?)",
                (memory_id, user, json.dumps(tags, ensure_ascii=False), text, normalize_text(text), event_time,
                 "user:" + memory_id, stamp, stamp))
            self.bump(connection, user, profile=True)
            return memory_id
        return self.memory(user, self.write(action))

    def edit(self, user, memory_id, text, tags, event_time, expected_version):
        text = clean_text(text, "记忆内容", MAX_MEMORY_CHARS)
        tags, event_time = clean_tags(tags), clean_event_time(event_time)

        def action(connection):
            row = self._locked(connection, user, memory_id)
            if row["status"] == "rejected":
                raise AdvisorError("not-found", "记忆已被忽略")
            self._check_version(row, expected_version)
            self._snapshot(connection, row, "edit")
            corrected = 1 if row["origin"] == "ai" and normalize_text(text) != row["norm"] else row["user_corrected"]
            connection.execute("UPDATE memories SET text=?,norm=?,tags_json=?,event_time=?,user_corrected=?,"
                               "version=version+1,updated_at_ms=? WHERE id=?",
                               (text, normalize_text(text), json.dumps(tags, ensure_ascii=False), event_time,
                                corrected, now_ms(), memory_id))
            self.bump(connection, user, profile=row["status"] == "confirmed")
        self.write(action)
        return self.memory(user, memory_id)

    def delete(self, user, memory_id, expected_version):
        def action(connection):
            row = self._locked(connection, user, memory_id)
            self._check_version(row, expected_version)
            self._exclude(connection, row, "deleted")
            connection.execute("DELETE FROM memory_evidence WHERE memory_id=?", (memory_id,))
            connection.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            # Candidates hanging on the removed record become ordinary candidates.
            connection.execute("UPDATE memories SET relation=NULL,related_id=NULL WHERE user=? AND related_id=?",
                               (user, memory_id))
            self.bump(connection, user, profile=row["status"] == "confirmed")
        self.write(action)
        return {"deleted": memory_id}

    def clear(self, user):
        def action(connection):
            ids = [row[0] for row in connection.execute("SELECT id FROM memories WHERE user=?", (user,))]
            for start in range(0, len(ids), 400):
                chunk = ids[start:start + 400]
                connection.execute("DELETE FROM memory_evidence WHERE memory_id IN (" +
                                   ",".join("?" for _ in chunk) + ")", chunk)
            connection.execute("DELETE FROM memories WHERE user=?", (user,))
            connection.execute("DELETE FROM memory_dedup WHERE user=?", (user,))
            # The clear epoch tells analysis plugins to start their extraction over.
            self.bump(connection, user, clear=True, profile=True)
            return len(ids)
        return {"cleared": self.write(action)}

    # -- candidate decisions ---------------------------------------------------
    def _merge_into(self, connection, candidate, target_id, reason):
        target = self._locked(connection, candidate["user"], target_id)
        if target["status"] != "confirmed":
            raise AdvisorError("request-conflict", "原记录已不是已确认状态，请刷新后处理")
        self._snapshot(connection, target, reason)
        tags = [tag for tag in TAGS if tag in set(_tags(target)) | set(_tags(candidate))]
        connection.execute("UPDATE memories SET text=?,norm=?,tags_json=?,event_time=COALESCE(?,event_time),"
                           "first_mention_ms=COALESCE(first_mention_ms,?),version=version+1,updated_at_ms=? WHERE id=?",
                           (candidate["text"], candidate["norm"], json.dumps(tags, ensure_ascii=False),
                            candidate["event_time"], candidate["first_mention_ms"], now_ms(), target_id))
        start = connection.execute("SELECT COALESCE(MAX(ordinal),-1)+1 FROM memory_evidence WHERE memory_id=?",
                                   (target_id,)).fetchone()[0]
        moved = [dict(row) for row in connection.execute(
            "SELECT * FROM memory_evidence WHERE memory_id=? ORDER BY ordinal", (candidate["id"],))]
        known = {row[0] for row in connection.execute("SELECT msg_id FROM memory_evidence WHERE memory_id=?", (target_id,))}
        self.insert_evidence(connection, target_id, [row for row in moved if row["msg_id"] not in known], start)
        connection.execute("DELETE FROM memory_evidence WHERE memory_id=?", (candidate["id"],))
        connection.execute("DELETE FROM memories WHERE id=?", (candidate["id"],))

    def _decide(self, connection, user, memory_id, action, expected_version=None, choice=None):
        row = self._locked(connection, user, memory_id)
        if row["status"] != "pending":
            raise AdvisorError("request-conflict", "该候选已处理")
        self._check_version(row, expected_version)
        if action == "ignore" or choice == "keep":
            self._exclude(connection, row, "ignored")
            connection.execute("UPDATE memories SET status='rejected',version=version+1,updated_at_ms=? WHERE id=?",
                               (now_ms(), memory_id))
            return
        related = row["related_id"] if row["relation"] in ("supplement", "conflict") else None
        if related and (row["relation"] == "supplement" or choice == "adopt"):
            exists = connection.execute("SELECT status FROM memories WHERE id=? AND user=?", (related, user)).fetchone()
            if exists is not None and exists["status"] == "confirmed":
                self._merge_into(connection, row, related, "supplement" if row["relation"] == "supplement" else "conflict")
                return
        if row["relation"] == "conflict" and choice not in ("both", "adopt") and related:
            raise AdvisorError("invalid-request", "请选择保留原记录、采用新说法或分别注明时期")
        connection.execute("UPDATE memories SET status='confirmed',relation=NULL,related_id=NULL,version=version+1,"
                           "updated_at_ms=? WHERE id=?", (now_ms(), memory_id))

    def decide(self, user, memory_id, action, expected_version=None, choice=None):
        if action not in ("confirm", "ignore") or choice not in (None, "keep", "adopt", "both"):
            raise AdvisorError("invalid-request", "候选操作不正确")

        def run(connection):
            self._decide(connection, user, memory_id, action, expected_version, choice)
            self.bump(connection, user, profile=action == "confirm" and choice != "keep")
        self.write(run)
        return {"memoryId": memory_id}

    def decide_many(self, user, memory_ids, action):
        """Apply one decision to exactly the selected pending candidates."""
        if action not in ("confirm", "ignore") or not isinstance(memory_ids, list) or not 1 <= len(memory_ids) <= 5000:
            raise AdvisorError("invalid-request", "批量操作范围不正确")
        if len(set(memory_ids)) != len(memory_ids) or any(not isinstance(item, str) for item in memory_ids):
            raise AdvisorError("invalid-request", "批量操作范围不正确")

        def run(connection):
            done, skipped = 0, 0
            for memory_id in memory_ids:
                row = connection.execute("SELECT status,relation FROM memories WHERE id=? AND user=?",
                                         (memory_id, user)).fetchone()
                # Conflicts always need an explicit per-record choice.
                if row is None or row["status"] != "pending" or (action == "confirm" and row["relation"] == "conflict"):
                    skipped += 1
                    continue
                self._decide(connection, user, memory_id, action)
                done += 1
            if done:
                self.bump(connection, user, profile=action == "confirm")
            return {"done": done, "skipped": skipped}
        return self.write(run)

    # -- analysis scheme the user looks at for this friend --------------------------
    def scheme(self, user):
        return self._row("SELECT plugin, scheme FROM friend_schemes WHERE user=?", (user,))

    def set_scheme(self, user, plugin, scheme):
        self.write(lambda connection: connection.execute(
            "INSERT INTO friend_schemes(user,plugin,scheme,updated_at_ms) VALUES(?,?,?,?) ON CONFLICT(user) "
            "DO UPDATE SET plugin=excluded.plugin,scheme=excluded.scheme,updated_at_ms=excluded.updated_at_ms",
            (user, plugin, scheme, now_ms())))
        return self.scheme(user)

    # -- contact goal and the account's own profile ----------------------------
    def goal(self, user):
        return self._row("SELECT * FROM friend_settings WHERE user=?", (user,)) or {
            "user": user, "goal": "", "version": 0, "updated_at_ms": 0}

    def save_goal(self, user, goal, expected_version):
        goal = clean_text(goal, "联系目标", MAX_SETTING_CHARS, allow_empty=True)

        def action(connection):
            row = connection.execute("SELECT version FROM friend_settings WHERE user=?", (user,)).fetchone()
            current = row["version"] if row else 0
            if type(expected_version) is not int or expected_version != current:
                raise AdvisorError("request-conflict", "联系目标已在其他窗口修改，请查看最新内容后再保存")
            connection.execute("INSERT INTO friend_settings(user,goal,version,updated_at_ms) VALUES(?,?,?,?) "
                               "ON CONFLICT(user) DO UPDATE SET goal=excluded.goal,version=excluded.version,"
                               "updated_at_ms=excluded.updated_at_ms", (user, goal, current + 1, now_ms()))
            self.bump(connection, user, profile=True)
        self.write(action)
        return self.goal(user)

    def account_profile(self):
        return self._row("SELECT * FROM account_profile WHERE id=1") or {
            "background": "", "style": "", "version": 0, "updated_at_ms": 0}

    def save_account_profile(self, background, style, expected_version):
        background = clean_text(background, "自身背景", MAX_SETTING_CHARS, allow_empty=True)
        style = clean_text(style, "表达风格", MAX_SETTING_CHARS, allow_empty=True)

        def action(connection):
            row = connection.execute("SELECT version FROM account_profile WHERE id=1").fetchone()
            current = row["version"] if row else 0
            if type(expected_version) is not int or expected_version != current:
                raise AdvisorError("request-conflict", "自身资料已在其他窗口修改，请查看最新内容后再保存")
            connection.execute("INSERT INTO account_profile(id,background,style,version,updated_at_ms) VALUES(1,?,?,?,?) "
                               "ON CONFLICT(id) DO UPDATE SET background=excluded.background,style=excluded.style,"
                               "version=excluded.version,updated_at_ms=excluded.updated_at_ms",
                               (background, style, current + 1, now_ms()))
        self.write(action)
        return self.account_profile()

    # -- manual observations ---------------------------------------------------
    def observations(self, user):
        return self._rows("SELECT * FROM friend_observations WHERE user=? ORDER BY updated_at_ms DESC", (user,))

    def save_observation(self, user, observation_id, judgement, note, dimension, expected_version):
        judgement = clean_text(judgement, "一句话判断", 200)
        note = clean_text(note, "依据备注", MAX_NOTE_CHARS, allow_empty=True)
        if dimension is not None and (not isinstance(dimension, str) or len(dimension) > 40):
            raise AdvisorError("invalid-request", "关联维度不正确")

        def action(connection):
            stamp = now_ms()
            if observation_id is None:
                identifier = "obs_" + uuid.uuid4().hex
                connection.execute("INSERT INTO friend_observations(id,user,judgement,note,dimension,version,"
                                   "created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,1,?,?)",
                                   (identifier, user, judgement, note, dimension or None, stamp, stamp))
                return identifier
            row = connection.execute("SELECT version FROM friend_observations WHERE id=? AND user=?",
                                     (observation_id, user)).fetchone()
            if row is None:
                raise AdvisorError("not-found", "观察记录不存在")
            if expected_version != row["version"]:
                raise AdvisorError("request-conflict", "观察记录已被修改，请查看最新内容后再保存")
            connection.execute("UPDATE friend_observations SET judgement=?,note=?,dimension=?,version=version+1,"
                               "updated_at_ms=? WHERE id=?", (judgement, note, dimension or None, stamp, observation_id))
            return observation_id
        identifier = self.write(action)
        return self._row("SELECT * FROM friend_observations WHERE id=?", (identifier,))

    def delete_observation(self, user, observation_id, expected_version=None):
        """Delete one observation; ``expected_version`` (MCP writes) rejects stale deletes."""
        def action(connection):
            row = connection.execute("SELECT version FROM friend_observations WHERE id=? AND user=?",
                                     (observation_id, user)).fetchone()
            if row is None:
                raise AdvisorError("not-found", "观察记录不存在")
            if expected_version is not None and expected_version != row["version"]:
                raise AdvisorError("request-conflict", "观察记录已被修改，请查看最新内容后再删除")
            connection.execute("DELETE FROM friend_observations WHERE id=? AND user=?", (observation_id, user))
        self.write(action)
        return {"deleted": observation_id}

    # -- drafts ----------------------------------------------------------------
    def drafts(self, user):
        rows = self._rows("SELECT * FROM friend_drafts WHERE user=? ORDER BY updated_at_ms", (user,))
        closed = {row["entry"]: row["generation"] for row in
                  self._rows("SELECT entry,generation FROM friend_draft_closed WHERE user=?", (user,))}
        return {"drafts": [{"entry": row["entry"], "generation": row["generation"], "seq": row["seq"],
                            "payload": json.loads(row["payload"]), "baseVersion": row["base_version"],
                            "updatedAtMs": row["updated_at_ms"]} for row in rows],
                "closedGenerations": closed}

    def save_draft(self, user, entry, generation, seq, payload, base_version=None):
        """Store the newest draft; late writes and writes after save/discard are ignored."""
        if not isinstance(entry, str) or not re.fullmatch(r"[a-z]+(:[A-Za-z0-9_-]{1,80})?", entry):
            raise AdvisorError("invalid-request", "草稿条目不正确")
        if any(type(value) is not int or value < 0 for value in (generation, seq)):
            raise AdvisorError("invalid-request", "草稿序号不正确")
        if base_version is not None and type(base_version) is not int:
            raise AdvisorError("invalid-request", "草稿版本不正确")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if not isinstance(payload, dict) or len(encoded.encode("utf-8")) > MAX_DRAFT_BYTES:
            raise AdvisorError("invalid-request", "草稿内容过长")

        def action(connection):
            closed = connection.execute("SELECT generation FROM friend_draft_closed WHERE user=? AND entry=?",
                                        (user, entry)).fetchone()
            if closed is not None and generation <= closed["generation"]:
                return False
            current = connection.execute("SELECT generation,seq FROM friend_drafts WHERE user=? AND entry=?",
                                         (user, entry)).fetchone()
            if current is not None and (generation, seq) <= (current["generation"], current["seq"]):
                return False
            connection.execute("INSERT INTO friend_drafts(user,entry,generation,seq,payload,base_version,updated_at_ms) "
                               "VALUES(?,?,?,?,?,?,?) ON CONFLICT(user,entry) DO UPDATE SET generation=excluded.generation,"
                               "seq=excluded.seq,payload=excluded.payload,base_version=excluded.base_version,"
                               "updated_at_ms=excluded.updated_at_ms",
                               (user, entry, generation, seq, encoded, base_version, now_ms()))
            return True
        return {"saved": self.write(action)}

    def close_draft(self, user, entry, generation):
        """Formal save or explicit discard: drop the draft and block its generation."""
        if type(generation) is not int or generation < 0 or not isinstance(entry, str) or len(entry) > 90:
            raise AdvisorError("invalid-request", "草稿序号不正确")

        def action(connection):
            connection.execute("DELETE FROM friend_drafts WHERE user=? AND entry=? AND generation<=?",
                               (user, entry, generation))
            connection.execute("INSERT INTO friend_draft_closed(user,entry,generation) VALUES(?,?,?) "
                               "ON CONFLICT(user,entry) DO UPDATE SET generation=MAX(generation,excluded.generation)",
                               (user, entry, generation))
        self.write(action)
        return {"closed": True}
