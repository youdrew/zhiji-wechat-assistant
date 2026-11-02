"""Persistence for the 知己分析 v2 unified analysis: runs, scans, results and usage.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The tables live in the account's ``advisor.sqlite3`` next to the memory tables, so
clearing an account removes every run, scan generation, result and usage record.
A scan (chat labels or persona evidence) is written into its own generation; a
rebuilt generation replaces the published one only when it is complete.
"""
from __future__ import annotations

import json
import uuid

from contracts import AdvisorError

RUN_STATES = ("queued", "running", "done", "partial", "stopped", "interrupted", "error", "stale")
OPEN_STATES = ("queued", "running")
SCAN_STEPS = ("chat", "persona")
RESULT_PAGES = ("summary", "next", "persona")

RUN_SCHEMA = """
CREATE TABLE IF NOT EXISTS friend_runs (
    id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    state TEXT NOT NULL,
    trigger TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'update',
    request_id TEXT,
    model_fp TEXT,
    steps_json TEXT NOT NULL,
    error TEXT,
    queued_at_ms INTEGER NOT NULL,
    started_at_ms INTEGER,
    updated_at_ms INTEGER NOT NULL,
    finished_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS friend_runs_user ON friend_runs(user, queued_at_ms DESC);
CREATE UNIQUE INDEX IF NOT EXISTS friend_runs_request ON friend_runs(user, request_id);
CREATE TABLE IF NOT EXISTS friend_scans (
    user TEXT NOT NULL,
    step TEXT NOT NULL,
    generation INTEGER NOT NULL,
    published INTEGER NOT NULL DEFAULT 0,
    source_fp TEXT NOT NULL,
    version TEXT NOT NULL,
    model_fp TEXT NOT NULL,
    covered_count INTEGER NOT NULL DEFAULT 0,
    chain_digest TEXT NOT NULL DEFAULT '',
    built INTEGER NOT NULL DEFAULT 0,
    from_ms INTEGER,
    to_ms INTEGER,
    processed INTEGER NOT NULL DEFAULT 0,
    excluded INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY(user, step, generation)
);
CREATE TABLE IF NOT EXISTS friend_chat_blocks (
    user TEXT NOT NULL,
    generation INTEGER NOT NULL,
    block_id TEXT NOT NULL,
    side TEXT NOT NULL,
    msg_ids TEXT NOT NULL,
    hashes TEXT NOT NULL,
    first_ms INTEGER,
    last_ms INTEGER,
    intent TEXT,
    emotion TEXT,
    reply_to TEXT,
    response TEXT,
    invite TEXT,
    PRIMARY KEY(user, generation, block_id)
);
CREATE TABLE IF NOT EXISTS friend_persona_evidence (
    user TEXT NOT NULL,
    generation INTEGER NOT NULL,
    id TEXT NOT NULL,
    dim TEXT NOT NULL,
    lean TEXT,
    value TEXT,
    topic TEXT,
    note TEXT NOT NULL,
    msg_ids TEXT NOT NULL,
    hashes TEXT NOT NULL,
    excerpt TEXT NOT NULL,
    first_ms INTEGER,
    last_ms INTEGER,
    PRIMARY KEY(user, generation, id)
);
CREATE INDEX IF NOT EXISTS friend_persona_evidence_dim ON friend_persona_evidence(user, generation, dim);
CREATE TABLE IF NOT EXISTS friend_results (
    user TEXT NOT NULL,
    page TEXT NOT NULL,
    payload TEXT NOT NULL,
    deps TEXT NOT NULL,
    model_fp TEXT,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY(user, page)
);
CREATE TABLE IF NOT EXISTS friend_step_measurements (
    model_fp TEXT NOT NULL,
    step TEXT NOT NULL,
    calls INTEGER NOT NULL,
    seconds REAL NOT NULL,
    input_chars INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY(model_fp, step)
);
"""


def ensure_run_schema(connection, now):
    connection.executescript(RUN_SCHEMA)
    if "mode" not in {row[1] for row in connection.execute("PRAGMA table_info(friend_runs)")}:
        connection.execute("ALTER TABLE friend_runs ADD COLUMN mode TEXT NOT NULL DEFAULT 'update'")
    # Runs never survive a restart; saved scans and results stay, "继续" restarts by hand.
    connection.execute("UPDATE friend_runs SET state='interrupted',updated_at_ms=?,finished_at_ms=? "
                       "WHERE state IN ('queued','running')", (now, now))


class RunStore:
    """Thin transactional helpers over one :class:`analysis_store.FriendStore`."""

    def __init__(self, store, clock):
        self.store = store
        self.now = clock

    # -- runs ------------------------------------------------------------------
    def run(self, run_id):
        return self.store._row("SELECT * FROM friend_runs WHERE id=?", (run_id,))

    def latest_run(self, user):
        return self.store._row("SELECT * FROM friend_runs WHERE user=? AND state<>'stale' "
                               "ORDER BY queued_at_ms DESC, rowid DESC LIMIT 1", (user,))

    def last_completed(self, user):
        return self.store._row("SELECT * FROM friend_runs WHERE user=? AND state='done' "
                               "ORDER BY queued_at_ms DESC, rowid DESC LIMIT 1", (user,))

    def run_for_request(self, user, request_id):
        return self.store._row("SELECT * FROM friend_runs WHERE user=? AND request_id=?", (user, request_id))

    def create_run(self, user, trigger, request_id, model_fp, steps, mode="update"):
        def action(connection):
            if request_id is not None:
                existing = connection.execute("SELECT id FROM friend_runs WHERE user=? AND request_id=?",
                                              (user, request_id)).fetchone()
                if existing is not None:
                    return existing["id"]
            active = connection.execute("SELECT id FROM friend_runs WHERE user=? AND state IN ('queued','running')",
                                        (user,)).fetchone()
            if active is not None:
                return active["id"]
            stamp = self.now()
            run_id = "frun_" + uuid.uuid4().hex
            # A new run supersedes an unfinished one; its saved checkpoints remain usable.
            connection.execute("UPDATE friend_runs SET state='stale',updated_at_ms=? WHERE user=? "
                               "AND state IN ('stopped','interrupted','error','partial')", (stamp, user))
            connection.execute(
                "INSERT INTO friend_runs(id,user,state,trigger,mode,request_id,model_fp,steps_json,queued_at_ms,"
                "updated_at_ms) VALUES(?,?,'queued',?,?,?,?,?,?,?)",
                (run_id, user, trigger, mode, request_id, model_fp, json.dumps(steps, ensure_ascii=False), stamp, stamp))
            return run_id
        return self.run(self.store.write(action))

    def restart_run(self, run_id, steps):
        def action(connection):
            cursor = connection.execute(
                "UPDATE friend_runs SET state='queued',error=NULL,finished_at_ms=NULL,steps_json=?,updated_at_ms=? "
                "WHERE id=? AND state IN ('stopped','interrupted','error','partial')",
                (json.dumps(steps, ensure_ascii=False), self.now(), run_id))
            return cursor.rowcount == 1
        return self.store.write(action)

    def set_run(self, run_id, *, state=None, steps=None, error=None, started=False, model_fp=None):
        fields, params = ["updated_at_ms=?"], [self.now()]
        if state is not None:
            if state not in RUN_STATES:
                raise ValueError("invalid run state")
            fields.append("state=?")
            params.append(state)
            if state not in OPEN_STATES:
                fields.append("finished_at_ms=?")
                params.append(self.now())
        if steps is not None:
            fields.append("steps_json=?")
            params.append(json.dumps(steps, ensure_ascii=False))
        if error is not None:
            fields.append("error=?")
            params.append(error)
        if started:
            fields.append("started_at_ms=?")
            params.append(self.now())
        if model_fp is not None:
            fields.append("model_fp=?")
            params.append(model_fp)

        def action(connection):
            # Only an open run can change; a finished run is history.
            cursor = connection.execute("UPDATE friend_runs SET " + ",".join(fields) +
                                        " WHERE id=? AND state IN ('queued','running')", (*params, run_id))
            return cursor.rowcount == 1
        return self.store.write(action)

    # -- scans -----------------------------------------------------------------
    def scans(self, user, step):
        return self.store._rows("SELECT * FROM friend_scans WHERE user=? AND step=? ORDER BY generation",
                                (user, step))

    def published_scan(self, user, step):
        return self.store._row("SELECT * FROM friend_scans WHERE user=? AND step=? AND published=1", (user, step))

    def completed_scan(self, user, step):
        return self.store._row("SELECT * FROM friend_scans WHERE user=? AND step=? AND built=1 "
                               "ORDER BY generation DESC LIMIT 1", (user, step))

    def _publish_scan(self, connection, user, step, generation):
        for row in connection.execute("SELECT generation FROM friend_scans WHERE user=? AND step=? "
                                      "AND generation<>?", (user, step, generation)).fetchall():
            self._drop_generation(connection, user, step, row["generation"])
        connection.execute("UPDATE friend_scans SET published=1 WHERE user=? AND step=? AND generation=?",
                           (user, step, generation))

    def begin_scan(self, user, step, fields):
        """Start a fresh unpublished generation, dropping any other unpublished one."""
        def action(connection):
            for row in connection.execute("SELECT generation FROM friend_scans WHERE user=? AND step=? AND published=0",
                                          (user, step)).fetchall():
                self._drop_generation(connection, user, step, row["generation"])
            top = connection.execute("SELECT COALESCE(MAX(generation),0) FROM friend_scans WHERE user=? AND step=?",
                                     (user, step)).fetchone()[0]
            generation = top + 1
            connection.execute(
                "INSERT INTO friend_scans(user,step,generation,published,source_fp,version,model_fp,updated_at_ms) "
                "VALUES(?,?,?,0,?,?,?,?)", (user, step, generation, fields["source_fp"], fields["version"],
                                            fields["model_fp"], self.now()))
            return generation
        return self.store.write(action)

    @staticmethod
    def _drop_generation(connection, user, step, generation):
        table = "friend_chat_blocks" if step == "chat" else "friend_persona_evidence"
        connection.execute("DELETE FROM " + table + " WHERE user=? AND generation=?", (user, generation))
        connection.execute("DELETE FROM friend_scans WHERE user=? AND step=? AND generation=?", (user, step, generation))

    def commit_scan(self, user, step, generation, rows, progress, guard):
        """Atomically save one batch of a scan and advance its checkpoint.

        ``guard(connection)`` re-validates the run inside the transaction; a stale or
        cancelled batch writes nothing.
        """
        def action(connection):
            scan = connection.execute("SELECT * FROM friend_scans WHERE user=? AND step=? AND generation=?",
                                      (user, step, generation)).fetchone()
            if scan is None or scan["covered_count"] != progress["start"]:
                raise AdvisorError("stopping", "分析检查点已变化")
            guard(connection)
            if step == "chat":
                for item in rows:
                    connection.execute(
                        "INSERT OR REPLACE INTO friend_chat_blocks(user,generation,block_id,side,msg_ids,hashes,first_ms,"
                        "last_ms,intent,emotion,reply_to,response,invite) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (user, generation, item["block_id"], item["side"], json.dumps(item["msg_ids"]),
                         json.dumps(item["hashes"]), item.get("first_ms"), item.get("last_ms"), item.get("intent"),
                         item.get("emotion"), item.get("reply_to"), item.get("response"), item.get("invite")))
            else:
                for item in rows:
                    connection.execute(
                        "INSERT OR REPLACE INTO friend_persona_evidence(user,generation,id,dim,lean,value,topic,note,"
                        "msg_ids,hashes,excerpt,first_ms,last_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (user, generation, item["id"], item["dim"], item.get("lean"), item.get("value"),
                         item.get("topic"), item["note"], json.dumps(item["msg_ids"]), json.dumps(item["hashes"]),
                         item["excerpt"], item.get("first_ms"), item.get("last_ms")))
            connection.execute(
                "UPDATE friend_scans SET covered_count=?,chain_digest=?,from_ms=COALESCE(from_ms,?),"
                "to_ms=COALESCE(?,to_ms),processed=processed+?,excluded=excluded+?,built=?,updated_at_ms=? "
                "WHERE user=? AND step=? AND generation=?",
                (progress["covered"], progress["digest"], progress.get("from_ms"), progress.get("to_ms"),
                 progress.get("processed", 0), progress.get("excluded", 0), int(progress.get("built", False)),
                 self.now(), user, step, generation))
            if progress.get("built") and not scan["published"] and step == "chat":
                # The rebuilt generation is complete: it replaces the old one in this transaction.
                self._publish_scan(connection, user, step, generation)
        self.store.write(action)

    def chat_blocks(self, user, generation):
        return self.store._rows("SELECT * FROM friend_chat_blocks WHERE user=? AND generation=? ORDER BY first_ms, "
                                "block_id", (user, generation))

    def persona_evidence(self, user, generation, ids=None):
        rows = self.store._rows("SELECT * FROM friend_persona_evidence WHERE user=? AND generation=? "
                                "ORDER BY first_ms, id", (user, generation))
        return rows if ids is None else [row for row in rows if row["id"] in ids]

    # -- results ----------------------------------------------------------------
    def result(self, user, page):
        row = self.store._row("SELECT * FROM friend_results WHERE user=? AND page=?", (user, page))
        if row is None:
            return None
        return {**row, "payload": json.loads(row["payload"]), "deps": json.loads(row["deps"])}

    def save_result(self, user, page, payload, deps, model_fp, guard):
        if page not in RESULT_PAGES:
            raise ValueError("invalid result page")

        def action(connection):
            guard(connection)
            if page == "persona":
                scan = connection.execute("SELECT built FROM friend_scans WHERE user=? AND step='persona' "
                                          "AND generation=?", (user, deps["gen"])).fetchone()
                if scan is None or not scan["built"]:
                    raise AdvisorError("stopping", "性格与三观证据尚未完成")
                # Evidence and its audited result become visible together. Until this
                # transaction succeeds, the prior result keeps all of its citations.
                self._publish_scan(connection, user, "persona", deps["gen"])
            connection.execute(
                "INSERT INTO friend_results(user,page,payload,deps,model_fp,created_at_ms) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(user,page) DO UPDATE SET payload=excluded.payload,deps=excluded.deps,"
                "model_fp=excluded.model_fp,created_at_ms=excluded.created_at_ms",
                (user, page, json.dumps(payload, ensure_ascii=False), json.dumps(deps, ensure_ascii=False),
                 model_fp, self.now()))
        self.store.write(action)

    # -- measurements -------------------------------------------------------------
    def record_measurement(self, model_fp, step, seconds, input_chars):
        if not model_fp or seconds <= 0 or input_chars <= 0:
            return

        def action(connection):
            connection.execute(
                "INSERT INTO friend_step_measurements(model_fp,step,calls,seconds,input_chars,updated_at_ms) "
                "VALUES(?,?,1,?,?,?) ON CONFLICT(model_fp,step) DO UPDATE SET "
                "calls=MIN(calls+1,400),seconds=CASE WHEN calls>=400 THEN seconds*399.0/400+excluded.seconds "
                "ELSE seconds+excluded.seconds END,input_chars=CASE WHEN calls>=400 THEN "
                "CAST(input_chars*399.0/400 AS INTEGER)+excluded.input_chars ELSE input_chars+excluded.input_chars END,"
                "updated_at_ms=excluded.updated_at_ms", (model_fp, step, seconds, input_chars, self.now()))
        self.store.write(action)

    def measurement(self, model_fp, step):
        return self.store._row("SELECT * FROM friend_step_measurements WHERE model_fp=? AND step=?", (model_fp, step))

    # -- account clearing of one contact ----------------------------------------
    def clear_user(self, connection, user):
        for table in ("friend_runs", "friend_scans", "friend_chat_blocks", "friend_persona_evidence", "friend_results"):
            connection.execute("DELETE FROM " + table + " WHERE user=?", (user,))
