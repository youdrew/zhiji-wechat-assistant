"""知己分析 plugin store: memory-extraction jobs and checkpoints here, human data read from Core.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Split from Core (friend_store) into kyf.friend-analysis, 2026-10-13. Memories, evidence,
goals, the user's own profile and observations belong to Core; this store reads them
through the plugin interface and submits new memory candidates there.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
import uuid

from contracts import AdvisorError

TAGS = ("基本信息", "经历", "性格", "爱好", "偏好与忌口", "边界", "共同事件")
PLUGIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS friend_jobs (
    id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    kind TEXT NOT NULL,
    state TEXT NOT NULL,
    request_id TEXT,
    phase TEXT,
    error TEXT,
    source_fp TEXT,
    context_epoch TEXT,
    extract_version TEXT,
    model_fp TEXT,
    clear_epoch INTEGER NOT NULL DEFAULT 0,
    upto_seq INTEGER,
    start_count INTEGER NOT NULL DEFAULT 0,
    total_messages INTEGER,
    done_messages INTEGER NOT NULL DEFAULT 0,
    total_segments INTEGER,
    done_segments INTEGER NOT NULL DEFAULT 0,
    segment_range TEXT,
    covered_from_ms INTEGER,
    covered_to_ms INTEGER,
    candidates INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    started_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS friend_jobs_user ON friend_jobs(user, started_at_ms DESC);
CREATE UNIQUE INDEX IF NOT EXISTS friend_jobs_request ON friend_jobs(user, request_id);
CREATE TABLE IF NOT EXISTS friend_checkpoints (
    user TEXT PRIMARY KEY,
    source_fp TEXT NOT NULL,
    extract_version TEXT NOT NULL,
    covered_count INTEGER NOT NULL,
    chain_digest TEXT NOT NULL,
    last_msg_id TEXT,
    last_hash TEXT,
    covered_from_ms INTEGER,
    covered_to_ms INTEGER,
    built INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    clear_epoch INTEGER
);
CREATE TABLE IF NOT EXISTS friend_measurements (
    model_fp TEXT PRIMARY KEY,
    segments INTEGER NOT NULL,
    seconds REAL NOT NULL,
    updated_at_ms INTEGER NOT NULL
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


def ensure_schema(connection):
    from friend_run_store import ensure_run_schema
    connection.executescript(PLUGIN_SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(friend_checkpoints)")}
    if "clear_epoch" not in columns:
        connection.execute("ALTER TABLE friend_checkpoints ADD COLUMN clear_epoch INTEGER")
    ensure_run_schema(connection, now_ms())
    connection.execute("UPDATE friend_jobs SET state='interrupted',phase=NULL,updated_at_ms=? WHERE state='running'",
                       (now_ms(),))


def candidate_payload(item):
    """A validated extraction candidate in the plugin interface's candidate shape."""
    return {"text": item["text"], "tags": item["tags"], "proposalKey": item["proposal_key"],
            "relation": item.get("relation"), "relatedId": item.get("related_id"),
            "eventTime": item.get("event_time"), "firstMentionMs": item.get("first_mention_ms"),
            "evidence": [{"sourceFp": entry["source_fp"], "msgId": entry["msg_id"], "hash": entry["content_hash"],
                          "sortKey": entry.get("sort_key"), "cursor": entry.get("cursor"), "role": entry["role"],
                          "time": entry.get("time_ms"), "excerpt": entry.get("excerpt") or ""}
                         for entry in item.get("evidence") or []]}


class FriendStore:
    def __init__(self, database):
        self.database = database

    # -- plumbing ------------------------------------------------------------
    def _connection(self):
        return self.database._connect()

    def _rows(self, sql, params=()):
        return self.database._rows(sql, params)

    def _row(self, sql, params=()):
        return self.database._row(sql, params)

    def write(self, action):
        with self.database.lock:
            connection = self._connection()
            with connection:
                return action(connection)

    # -- human data (Core) -------------------------------------------------------
    def state(self, user):
        return self.database.human(user)["state"]

    def memories(self, user, statuses=("pending", "confirmed")):
        return [row for row in self.database.human(user)["memories"] if row["status"] in statuses]

    def exclusions(self, user):
        return self.database.human(user)["exclusions"]

    def goal(self, user):
        return self.database.human(user)["goal"]

    def account_profile(self, user):
        return self.database.human(user)["profile"]

    def observations(self, user):
        return self.database.human(user)["observations"]

    # -- jobs and checkpoints ------------------------------------------------------
    def job(self, job_id):
        return self._row("SELECT * FROM friend_jobs WHERE id=?", (job_id,))

    def latest_job(self, user):
        return self._row("SELECT * FROM friend_jobs WHERE user=? AND state<>'stale' "
                         "ORDER BY started_at_ms DESC, rowid DESC LIMIT 1", (user,))

    def job_for_request(self, user, request_id):
        return self._row("SELECT * FROM friend_jobs WHERE user=? AND request_id=?", (user, request_id))

    def create_job(self, user, kind, request_id, fields):
        def action(connection):
            if request_id is not None:
                existing = connection.execute("SELECT id FROM friend_jobs WHERE user=? AND request_id=?",
                                              (user, request_id)).fetchone()
                if existing is not None:
                    return existing["id"]
            stamp = now_ms()
            job_id = "fjob_" + uuid.uuid4().hex
            connection.execute("UPDATE friend_jobs SET state='stale',phase=NULL,updated_at_ms=? WHERE user=? "
                               "AND state IN ('running','stopped','error','interrupted')", (stamp, user))
            connection.execute(
                "INSERT INTO friend_jobs(id,user,kind,state,request_id,phase,source_fp,context_epoch,extract_version,"
                "model_fp,clear_epoch,upto_seq,start_count,total_messages,done_messages,total_segments,started_at_ms,"
                "updated_at_ms) VALUES(?,?,?,'running',?,'preparing',?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, user, kind, request_id, fields["source_fp"], fields["context_epoch"],
                 fields["extract_version"], fields["model_fp"], fields["clear_epoch"], fields["upto_seq"],
                 fields["start_count"], fields["total_messages"], fields["done_messages"], fields["total_segments"],
                 stamp, stamp))
            return job_id
        return self.job(self.write(action))

    def restart_job(self, job_id, fields):
        def action(connection):
            cursor = connection.execute(
                "UPDATE friend_jobs SET state='running',phase='preparing',error=NULL,finished_at_ms=NULL,"
                "source_fp=?,context_epoch=?,model_fp=?,upto_seq=?,total_messages=?,done_messages=?,"
                "attempts=attempts+1,clear_epoch=?,updated_at_ms=? WHERE id=? AND state IN ('stopped','error','interrupted')",
                (fields["source_fp"], fields["context_epoch"], fields["model_fp"], fields["upto_seq"],
                 fields["total_messages"], fields["done_messages"], fields["clear_epoch"], now_ms(), job_id))
            return cursor.rowcount == 1
        return self.write(action)

    def update_job(self, job_id, **fields):
        allowed = {"phase", "error", "attempts", "segment_range", "total_segments", "done_segments"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("invalid job update")
        names = sorted(fields)
        self.write(lambda connection: connection.execute(
            "UPDATE friend_jobs SET " + ",".join(name + "=?" for name in names) +
            ",updated_at_ms=? WHERE id=? AND state='running'", (*[fields[name] for name in names], now_ms(), job_id)))

    def finish_job(self, job_id, state, error=None):
        if state not in ("done", "stopped", "error", "stale", "interrupted"):
            raise ValueError("invalid job state")
        return bool(self.write(lambda connection: connection.execute(
            "UPDATE friend_jobs SET state=?,error=?,phase=NULL,updated_at_ms=?,finished_at_ms=? "
            "WHERE id=? AND state='running'", (state, error, now_ms(), now_ms(), job_id)).rowcount))

    def checkpoint(self, user):
        return self._row("SELECT * FROM friend_checkpoints WHERE user=?", (user,))

    def reset_memory_progress(self, user):
        """The user cleared this friend's memories: extraction starts again from the beginning."""
        def action(connection):
            connection.execute("DELETE FROM friend_checkpoints WHERE user=?", (user,))
            connection.execute("UPDATE friend_jobs SET state='stale',phase=NULL,updated_at_ms=? WHERE user=? "
                               "AND state IN ('running','stopped','error','interrupted')", (now_ms(), user))
        self.write(action)

    def commit_batch(self, job, candidates, links, checkpoint, segment, guard, core):
        """Submit one batch's candidates to Core, then save this plugin's progress and coverage.

        Core rejects the batch when the friend's memories were cleared after the job
        started; a repeated batch after a crash is ignored there by its proposal keys.
        """
        guard(None)
        inserted = 0
        if candidates or links:
            from kyf_plugin import CoreError
            try:
                result = core.post("human/candidates", {
                    "account": self.database.account, "user": job["user"],
                    "candidates": [{**candidate_payload(item), "modelFingerprint": job["model_fp"]} for item in candidates],
                    "links": [{"proposalKey": link["proposal_key"], "text": link["text"], "targetKind": link["target_kind"],
                               "targetId": link["target_id"], "relation": link["relation"]} for link in links],
                    "expectedClearEpoch": job["clear_epoch"]})
            except CoreError as exc:
                if exc.code == "memories-cleared":
                    raise AdvisorError("stopping", "这位联系人的记忆已被清空") from None
                # Core's own reason; its "invalid-request" code would otherwise read as a model error.
                raise AdvisorError("save-failed", "记忆保存失败：" + (exc.message or exc.code)) from None
            inserted = len(result.get("inserted") or [])
            self.database.forget_human(job["user"])

        def action(connection):
            current = connection.execute("SELECT state FROM friend_jobs WHERE id=?", (job["id"],)).fetchone()
            if current is None or current["state"] != "running":
                raise AdvisorError("stopping", "任务已过期")
            stamp = now_ms()
            connection.execute(
                "INSERT INTO friend_checkpoints(user,source_fp,extract_version,covered_count,chain_digest,last_msg_id,"
                "last_hash,covered_from_ms,covered_to_ms,built,updated_at_ms,clear_epoch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(user) DO UPDATE SET source_fp=excluded.source_fp,extract_version=excluded.extract_version,"
                "covered_count=excluded.covered_count,chain_digest=excluded.chain_digest,last_msg_id=excluded.last_msg_id,"
                "last_hash=excluded.last_hash,covered_from_ms=COALESCE(friend_checkpoints.covered_from_ms,"
                "excluded.covered_from_ms),covered_to_ms=excluded.covered_to_ms,"
                "built=MAX(friend_checkpoints.built,excluded.built),updated_at_ms=excluded.updated_at_ms,"
                "clear_epoch=excluded.clear_epoch",
                (job["user"], checkpoint["source_fp"], checkpoint["extract_version"], checkpoint["covered_count"],
                 checkpoint["chain_digest"], checkpoint["last_msg_id"], checkpoint["last_hash"],
                 checkpoint["covered_from_ms"], checkpoint["covered_to_ms"], int(checkpoint["built"]), stamp,
                 job["clear_epoch"]))
            connection.execute(
                "UPDATE friend_jobs SET done_messages=done_messages+?,done_segments=done_segments+1,candidates=candidates+?,"
                "covered_from_ms=COALESCE(covered_from_ms,?),covered_to_ms=COALESCE(?,covered_to_ms),"
                "segment_range=?,updated_at_ms=? WHERE id=?",
                (segment["messages"], inserted, segment.get("from_ms"), segment.get("to_ms"),
                 segment.get("label"), stamp, job["id"]))
            return inserted
        return self.write(action)

    def record_measurement(self, model_fp, segments, seconds):
        if not model_fp or segments <= 0 or seconds <= 0:
            return
        self.write(lambda connection: connection.execute(
            "INSERT INTO friend_measurements(model_fp,segments,seconds,updated_at_ms) VALUES(?,?,?,?) "
            "ON CONFLICT(model_fp) DO UPDATE SET segments=MIN(segments+excluded.segments,200),"
            "seconds=CASE WHEN segments+excluded.segments>200 THEN (seconds+excluded.seconds)*200.0/"
            "(segments+excluded.segments) ELSE seconds+excluded.seconds END,updated_at_ms=excluded.updated_at_ms",
            (model_fp, segments, seconds, now_ms())))

    def measurement(self, model_fp):
        return self._row("SELECT * FROM friend_measurements WHERE model_fp=?", (model_fp,))
