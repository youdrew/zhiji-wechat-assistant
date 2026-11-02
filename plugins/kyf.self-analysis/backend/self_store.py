"""Account-local SQLite index. Every page and its extraction checkpoint commit together.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import threading

from kyf_plugin import account_dir, connect
from self_contracts import SelfError, now_ms
from self_vectors import ensure_schema, input_digest

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(
 user TEXT PRIMARY KEY,name TEXT NOT NULL DEFAULT '',generation INTEGER NOT NULL DEFAULT 0,
 source TEXT,epoch TEXT,revision INTEGER,after_seq INTEGER NOT NULL DEFAULT 0,
 carry TEXT NOT NULL DEFAULT '{}',state TEXT NOT NULL DEFAULT 'pending',error TEXT,
 total_count INTEGER,skipped INTEGER NOT NULL DEFAULT 0,verified_seq INTEGER NOT NULL DEFAULT 0,
 stale INTEGER NOT NULL DEFAULT 0,needs_rebuild INTEGER NOT NULL DEFAULT 0,updated_ms INTEGER,
 latest TEXT,indexed TEXT);
CREATE TABLE IF NOT EXISTS proofs(
 user TEXT NOT NULL,seq INTEGER NOT NULL,msg_id TEXT NOT NULL,hash TEXT NOT NULL,
 PRIMARY KEY(user,seq));
CREATE TABLE IF NOT EXISTS samples(
 id TEXT PRIMARY KEY,user TEXT NOT NULL,source TEXT NOT NULL,epoch TEXT,
 from_seq INTEGER NOT NULL,to_seq INTEGER NOT NULL,time_ms INTEGER NOT NULL,
 input TEXT NOT NULL,reply TEXT NOT NULL,scene TEXT NOT NULL,norm TEXT NOT NULL,evidence TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sample_user ON samples(user,to_seq);
CREATE INDEX IF NOT EXISTS sample_time ON samples(time_ms);
CREATE TABLE IF NOT EXISTS terms(token TEXT NOT NULL,sample_id TEXT NOT NULL,
 PRIMARY KEY(token,sample_id));
CREATE INDEX IF NOT EXISTS term_sample ON terms(sample_id);
CREATE TABLE IF NOT EXISTS exclusions(sample_id TEXT PRIMARY KEY,updated_ms INTEGER NOT NULL);
"""


class Store:
    def __init__(self, data_dir, account):
        self.path = account_dir(data_dir, account) / "self-analysis.sqlite3"
        self.lock = threading.RLock()
        # Set whenever a sample may need a vector, so the semantic backfill waits
        # instead of rescanning every sample while nothing new arrives.
        self.samples_changed = threading.Event()
        self.db = connect(self.path)
        with self.lock, self.db:
            self.db.executescript(SCHEMA)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(conversations)")}
            # latest: the directory's newest source row; indexed: the source highwater of the
            # last complete read. Equal values let an incremental build skip the chat.
            for column in ("latest", "indexed"):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE conversations ADD COLUMN {column} TEXT")
            ensure_schema(self.db)
        if self.meta("build", {}).get("state") == "running":
            self.update_build(state="paused", error="上次运行已中断，继续会从已保存的位置恢复")

    def close(self):
        with self.lock:
            self.db.close()

    def rows(self, query, values=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(query, values)]

    def meta(self, key, default=None):
        rows = self.rows("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def _meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def bump(self, generation=False):
        self._meta("revision", self.meta("revision", 0) + 1)
        if generation:
            self._meta("generation", self.meta("generation", 0) + 1)

    def update_build(self, **changes):
        with self.lock, self.db:
            build = self.meta("build", {"state": "idle", "phase": "discovering"})
            build.update(changes, updatedAt=now_ms())
            self._meta("build", build)
            return build

    def begin(self, mode):
        with self.lock, self.db:
            old = self.meta("build", {})
            if old.get("state") in ("paused", "failed") and mode != "full":
                return self.update_build(state="running", error=None)
            if mode == "retry" and old.get("phase") == "done":
                self.db.execute("UPDATE conversations SET state='pending',error=NULL,verified_seq=0 WHERE state='failed'")
                return self.update_build(state="running", phase="indexing", error=None)
            # Directory is reconciled only when the complete snapshot is received.
            generation = self.meta("catalogGeneration", 0) + 1
            self._meta("catalogGeneration", generation)
            if mode == "full":
                self._clear_index()
                self._meta("source", None)
                self.bump(generation=True)
            self.db.execute("UPDATE conversations SET state='pending',error=NULL,verified_seq=0")
            return self.update_build(state="running", phase="discovering", cursor=None,
                                     catalogComplete=False, startedAt=now_ms(), error=None, mode=mode)

    def _clear_index(self, user=None):
        if user is None:
            for table in ("embeddings", "terms", "samples", "proofs", "conversations"):
                self.db.execute(f"DELETE FROM {table}")
        else:
            self.db.execute("DELETE FROM embeddings WHERE sample_id IN (SELECT id FROM samples WHERE user=?)", (user,))
            self.db.execute("DELETE FROM terms WHERE sample_id IN (SELECT id FROM samples WHERE user=?)", (user,))
            self.db.execute("DELETE FROM samples WHERE user=?", (user,))
            self.db.execute("DELETE FROM proofs WHERE user=?", (user,))

    def reset(self):
        with self.lock, self.db:
            self._clear_index()
            self.db.execute("DELETE FROM exclusions")
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='judge_shadow'").fetchone():
                self.db.execute("DELETE FROM judge_shadow")
            self.bump(generation=True)
            self._meta("source", None)
            self._meta("build", {"state": "idle", "phase": "discovering", "updatedAt": now_ms()})

    def catalog_page(self, page):
        with self.lock, self.db:
            source = page["sourceFingerprint"]
            if self.meta("source") not in (None, source):
                raise SelfError("source-changed", "聊天来源已变化，已有知识库已保留，请恢复原来源后继续")
            self._meta("source", source)
            self._meta("coverage", page.get("coverage") or {})
            generation = self.meta("catalogGeneration", 1)
            for row in page["conversations"]:
                if row.get("isGroup") or row["user"].endswith("@chatroom"):
                    continue
                latest = row.get("latest")
                latest = latest if isinstance(latest, str) and len(latest) <= 512 else None
                self.db.execute("INSERT INTO conversations(user,name,generation,latest) VALUES(?,?,?,?) ON CONFLICT(user) "
                                "DO UPDATE SET name=excluded.name,generation=excluded.generation,latest=excluded.latest",
                                (row["user"], row.get("name") or "", generation, latest))
            done = page.get("next") is None
            if done and (page.get("coverage") or {}).get("state", "complete") == "complete":
                removed = self.rows("SELECT user FROM conversations WHERE generation<>?", (generation,))
                for row in removed:
                    self._clear_index(row["user"])
                    self.db.execute("DELETE FROM conversations WHERE user=?", (row["user"],))
                if removed:
                    self.bump(generation=True)
            self.update_build(cursor=page.get("next"), catalogComplete=done,
                              phase="indexing" if done else "discovering")

    def restart_catalog(self):
        with self.lock, self.db:
            self._meta("catalogGeneration", self.meta("catalogGeneration", 0) + 1)
            self.update_build(cursor=None, catalogComplete=False, phase="discovering")

    def conversation(self, user):
        rows = self.rows("SELECT * FROM conversations WHERE user=?", (user,))
        return rows[0] if rows else None

    def pending(self, limit=1):
        return self.rows("SELECT * FROM conversations WHERE state IN ('pending','reading') "
                         "ORDER BY updated_ms IS NOT NULL,user LIMIT ?", (limit,))

    def skip_unchanged(self):
        """Finish chats whose newest source row, per this build's directory, was already read.

        Their samples stay as they are; each one is still checked against its original
        message whenever retrieval returns it.
        """
        with self.lock, self.db:
            return self.db.execute(
                "UPDATE conversations SET state='done',error=NULL WHERE state IN ('pending','reading') AND "
                "indexed IS NOT NULL AND indexed=latest AND generation=? AND source=? AND stale=0 AND needs_rebuild=0",
                (self.meta("catalogGeneration", 0), self.meta("source"))).rowcount

    def bind(self, user, status):
        with self.lock, self.db:
            local = self.conversation(user)
            changed = (local["source"] not in (None, status.get("sourceFingerprint")) or
                       local["epoch"] not in (None, status.get("epoch")) or
                       local["after_seq"] > (status.get("watermark") or {}).get("seq", status.get("readCount", 0)))
            if changed:
                self.invalidate(user)
            self.db.execute("UPDATE conversations SET source=?,epoch=?,revision=?,state='reading',error=NULL,needs_rebuild=0,"
                            "total_count=?,updated_ms=? WHERE user=?",
                            (status.get("sourceFingerprint"), status.get("epoch"), status.get("revision"),
                             status.get("totalCount"), now_ms(), user))
            return self.conversation(user)

    def invalidate(self, user):
        with self.lock, self.db:
            self._clear_index(user)
            self.db.execute("UPDATE conversations SET source=NULL,epoch=NULL,revision=NULL,after_seq=0,carry='{}',skipped=0,verified_seq=0,"
                            "state='pending',error=NULL,needs_rebuild=1,indexed=NULL WHERE user=?", (user,))
            self.bump(generation=True)

    def verified(self, user, seq):
        with self.lock, self.db:
            self.db.execute("UPDATE conversations SET verified_seq=? WHERE user=?", (seq, user))

    def page(self, user, messages, samples, carry, skipped, status, expected_after=None):
        with self.lock, self.db:
            local = self.conversation(user)
            if (local is None or local["state"] != "reading" or local["needs_rebuild"] or
                    local["source"] != status.get("sourceFingerprint") or local["epoch"] != status.get("epoch") or
                    (expected_after is not None and local["after_seq"] != expected_after) or
                    (messages and messages[0]["seq"] != local["after_seq"] + 1)):
                raise ValueError("projection changed during page")
            for row in messages:
                self.db.execute("INSERT OR REPLACE INTO proofs VALUES(?,?,?,?)",
                                (user, row["seq"], row["id"], row.get("hash") or ""))
            self.write_samples(samples)
            after = messages[-1]["seq"] if messages else local["after_seq"]
            self.db.execute("UPDATE conversations SET after_seq=?,carry=?,skipped=skipped+?,updated_ms=? WHERE user=?",
                            (after, json.dumps(carry, ensure_ascii=False), skipped, now_ms(), user))
            self.bump()
            self.update_build(currentUser=user)
        if samples:
            self.samples_changed.set()

    def write_samples(self, samples):
        """Apply prepared examples inside the caller's SQLite transaction."""
        for sample in samples:
            previous = self.rows("SELECT input,reply,evidence FROM samples WHERE id=?", (sample["id"],))
            self.db.execute("DELETE FROM terms WHERE sample_id=?", (sample["id"],))
            if sample.get("delete"):
                self.db.execute("DELETE FROM embeddings WHERE sample_id=?", (sample["id"],))
                self.db.execute("DELETE FROM samples WHERE id=?", (sample["id"],))
                self.bump(generation=True)
                continue
            evidence = json.dumps(sample["evidence"], ensure_ascii=False)
            if previous and (any(previous[0][key] != sample[key] for key in ("input", "reply")) or previous[0]["evidence"] != evidence):
                self.bump(generation=True)
            digest = input_digest(sample["input"])
            self.db.execute("DELETE FROM embeddings WHERE sample_id=? AND input_digest<>?", (sample["id"], digest))
            self.db.execute("INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            tuple(sample[key] for key in ("id", "user", "source", "epoch", "from_seq", "to_seq", "time_ms",
                                                          "input", "reply", "scene", "norm")) +
                            (evidence, digest))
            self.db.executemany("INSERT OR IGNORE INTO terms VALUES(?,?)", [(token, sample["id"]) for token in sample["terms"]])

    def finish_conversation(self, user, error=None, indexed=None):
        with self.lock, self.db:
            self.db.execute("UPDATE conversations SET state=?,error=?,stale=0,indexed=?,updated_ms=? WHERE user=?",
                            ("failed" if error else "done", error, None if error else indexed, now_ms(), user))

    def counts(self):
        with self.lock:
            count = self.rows("SELECT count(*) total,sum(state='done') done,sum(state='failed') failed,"
                              "sum(after_seq) messages,sum(skipped) skipped,sum(stale) stale FROM conversations")[0]
            count = {key: value or 0 for key, value in count.items()}
            count["storedSamples"] = self.rows("SELECT count(*) n FROM samples")[0]["n"]
            count["samples"] = self.rows("SELECT count(*) n FROM samples WHERE NOT EXISTS "
                                         "(SELECT 1 FROM exclusions WHERE sample_id=id)")[0]["n"]
            count["excludedSamples"] = count["storedSamples"] - count["samples"]
            return count

    def mark_stale(self, user):
        with self.lock, self.db:
            self.db.execute("UPDATE conversations SET stale=1 WHERE user=?", (user,))
            if self.rows("SELECT 1 FROM sqlite_master WHERE type='table' AND name='profile_corpus_conversations'"):
                # A chat finished earlier in an open sync round is read again for its new messages.
                self.db.execute("UPDATE profile_corpus_conversations SET state='pending',gap=NULL,ceiling=NULL,unread=0,"
                                "partial=0 WHERE user=? AND state='done'", (user,))

    def exclude(self, identifier, excluded):
        with self.lock, self.db:
            if excluded:
                if not self.rows("SELECT id FROM samples WHERE id=?", (identifier,)):
                    return False
                self.db.execute("INSERT OR REPLACE INTO exclusions VALUES(?,?)", (identifier, now_ms()))
            else:
                self.db.execute("DELETE FROM exclusions WHERE sample_id=?", (identifier,))
            self.bump(generation=True)
        self.samples_changed.set()
        return True
