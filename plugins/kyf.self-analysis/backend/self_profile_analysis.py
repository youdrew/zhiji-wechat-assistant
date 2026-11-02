"""Durable, bounded model work over every message in the profile corpus.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import hashlib
import json
import math

from self_contracts import SelfError

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_draft_parts(
 id INTEGER PRIMARY KEY,message_id TEXT NOT NULL,part INTEGER NOT NULL,parts INTEGER NOT NULL,
 payload TEXT NOT NULL,done INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS profile_part_pending ON profile_draft_parts(done,id);
CREATE TABLE IF NOT EXISTS profile_draft_notes(
 level INTEGER NOT NULL,position INTEGER NOT NULL,end_position INTEGER NOT NULL,payload TEXT NOT NULL,
 PRIMARY KEY(level,position));
CREATE TABLE IF NOT EXISTS profile_analysis_cache(
 key TEXT PRIMARY KEY,signature TEXT NOT NULL,notes TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS profile_cache_signature ON profile_analysis_cache(signature);
CREATE TABLE IF NOT EXISTS profile_analysis_cache_inputs(
 cache_key TEXT NOT NULL,fingerprint TEXT NOT NULL,
 PRIMARY KEY(cache_key,fingerprint));
CREATE INDEX IF NOT EXISTS profile_cache_input ON profile_analysis_cache_inputs(fingerprint);
CREATE TABLE IF NOT EXISTS profile_merge_cache(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profile_draft_note_order(position INTEGER PRIMARY KEY,first_part INTEGER NOT NULL);
"""


def encoded(value):
    return json.dumps(value, ensure_ascii=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def source_fingerprint(message):
    # Core may renumber its projection after an old row is removed. Sequence
    # offsets are not evidence identity; source IDs and timestamps already are.
    value = dict(message)
    if isinstance(value.get("context"), dict):
        context = dict(value["context"])
        for side in ("before", "after"):
            context[side] = [{key: item for key, item in row.items() if key != "seq"}
                             for row in context.get(side, [])]
        value["context"] = context
    return digest(value)


def _shorten(text, limit):
    if len(text) <= limit:
        return text
    marker = "…〔上下文已截断〕…"
    width = max(1, (limit - len(marker)) // 2)
    return text[:width] + marker + text[-width:]


def message_base(row, room):
    """Keep both sides of the exchange, with explicit loss markers when bounded."""
    info = row.get("context_info") or {}
    context = {"before": [{key: value for key, value in item.items() if key != "seq"} for item in row.get("context_before", [])],
               "after": [{key: value for key, value in item.items() if key != "seq"} for item in row.get("context_after", [])],
               "truncated": bool(info.get("truncated")), "timeUnknown": bool(info.get("timeUnknown")),
               "windowMinutes": info.get("windowMinutes", 60), "beforeLimit": info.get("beforeLimit", 12),
               "afterLimit": info.get("afterLimit", 6), "omittedBefore": 0, "omittedAfter": 0,
               "omittedUnknownTime": info.get("omittedUnknownTime", 0),
               "omittedInconsistentTime": info.get("omittedInconsistentTime", 0)}
    base = {"id": row["id"], "chat": row["name"][:120], "group": bool(row["is_group"]),
            "time": row["time_ms"] or None, "context": context, "part": 1, "parts": 1, "text": ""}
    reserve = min(max(64, len(row["text"])), max(64, room // 3))
    if len(row["text"]) > reserve:
        width = min(160, max(24, room // 12))
        base["messageFrame"] = {"head": row["text"][:width], "tail": row["text"][-width:]}
    while len(encoded(base)) + reserve + 32 > room:
        lines = context["before"] + context["after"]
        largest = max(lines, key=lambda line: len(line["text"]), default=None)
        if largest is not None and len(largest["text"]) > 100:
            largest["text"] = _shorten(largest["text"], max(80, len(largest["text"]) // 2))
            largest["truncated"] = True
        elif context["before"] or context["after"]:
            # The nearest replies survive longest. Remove whole rows so that
            # attribution cannot be lost by cutting through a speaker header.
            if len(context["before"]) > 2 * len(context["after"]):
                context["before"].pop(0)
                context["omittedBefore"] += 1
            elif context["after"]:
                context["after"].pop()
                context["omittedAfter"] += 1
            else:
                context["before"].pop(0)
                context["omittedBefore"] += 1
        else:
            break
        context["truncated"] = True
    if len(encoded(base)) + 32 >= room:
        raise SelfError("context-too-long", "模型上下文不足以容纳消息和归属信息，请增大上下文设置后重试")
    return base


class Analysis:
    def __init__(self, store):
        self.store, self.lock, self.db = store, store.lock, store.db
        self.signature = None
        with self.lock, self.db:
            self.db.executescript(SCHEMA)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(profile_draft_parts)")}
            if "fingerprint" not in columns:
                self.db.execute("ALTER TABLE profile_draft_parts ADD COLUMN fingerprint TEXT")
            self.db.execute("CREATE INDEX IF NOT EXISTS profile_part_fingerprint ON profile_draft_parts(fingerprint)")

    def reset(self):
        with self.lock, self.db:
            self.db.execute("DELETE FROM profile_draft_parts")
            self.db.execute("DELETE FROM profile_draft_notes")
            self.db.execute("DELETE FROM profile_draft_note_order")
            self.store._meta("profilePartsReady", False)

    def prepare(self, corpus, room, check, progress, signature=None, legacy_signature=None, legacy_limit=None):
        self.signature = signature
        identity = {"corpus": corpus.meta("revision", 0), "signature": signature, "room": room}
        if self.store.meta("profilePartsReady", False) and self.store.meta("profileAnalysisIdentity") == identity:
            return
        # Keep complete summaries independently of the current run's parts.
        # Older databases did not record batch membership: archive their entire
        # completed prefix as one bundle, never guess which note belongs to a row.
        old_signature = self.store.meta("profileAnalysisSignature", legacy_signature)
        if old_signature and not self.store.meta("profileAnalysisSignature"):
            notes = self.store.rows("SELECT payload FROM profile_draft_notes WHERE level=0 ORDER BY position")
            fits = all(isinstance(value, str) and (legacy_limit is None or len(value) <= legacy_limit)
                       for row in notes for value in json.loads(row["payload"]).values())
            if notes and fits:
                self._archive_legacy(notes, old_signature, check)
        # If interrupted here, regenerate parts. No model work starts before the
        # complete corpus has been partitioned, and no original text is cut off.
        with self.lock, self.db:
            self.db.execute("DELETE FROM profile_draft_parts")
            self.db.execute("DELETE FROM profile_draft_notes")
            self.db.execute("DELETE FROM profile_draft_note_order")
            self.store._meta("profilePartsReady", False)
        after = 0
        while rows := corpus.iter_messages(after_id=after, limit=200):
            check()
            values = []
            for row in rows:
                check()
                base = message_base(row, room)
                width = room - len(encoded(base)) - 32
                # Account for JSON escaping and changing part-number widths;
                # every source character must reach exactly one text fragment.
                while True:
                    count = max(1, math.ceil(len(row["text"]) / width))
                    parts = [encoded({**base, "part": i + 1, "parts": count,
                                      "text": row["text"][i * width:(i + 1) * width]}) for i in range(count)]
                    if all(len(payload) <= room for payload in parts):
                        break
                    width //= 2
                    if width < 1:
                        raise SelfError("context-too-long", "模型上下文不足以容纳消息")
                values.extend((row["id"], i + 1, count, payload, source_fingerprint(json.loads(payload))) for i, payload in enumerate(parts))
            check()
            with self.lock, self.db:
                self.db.executemany("INSERT INTO profile_draft_parts(message_id,part,parts,payload,fingerprint) VALUES(?,?,?,?,?)", values)
            after = rows[-1]["rowid"]
            progress()
        self._reuse(check)
        with self.lock, self.db:
            self.store._meta("profileAnalysisSignature", signature)
            self.store._meta("profileAnalysisIdentity", identity)
            self.store._meta("profilePartsReady", True)

    def _cache(self, messages, notes, signature):
        fingerprints = [source_fingerprint(item) for item in messages]
        key = digest([signature, fingerprints])
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO profile_analysis_cache VALUES(?,?,?)", (key, signature, encoded(notes)))
            self.db.executemany("INSERT OR IGNORE INTO profile_analysis_cache_inputs VALUES(?,?)",
                                [(key, value) for value in fingerprints])

    def _archive_legacy(self, notes, signature, check):
        # Stream source fingerprints: an existing full-history run may contain
        # hundreds of thousands of large context payloads.
        fingerprint_hash = hashlib.sha256(signature.encode("utf-8"))
        after, fingerprints = 0, []
        while rows := self.store.rows("SELECT id,payload FROM profile_draft_parts WHERE done=1 AND id>? ORDER BY id LIMIT 200", (after,)):
            check()
            for row in rows:
                value = source_fingerprint(json.loads(row["payload"]))
                fingerprints.append(value)
                fingerprint_hash.update(value.encode("ascii"))
            after = rows[-1]["id"]
        if fingerprints:
            key = fingerprint_hash.hexdigest()
            with self.lock, self.db:
                self.db.execute("INSERT OR REPLACE INTO profile_analysis_cache VALUES(?,?,?)",
                                (key, signature, encoded([json.loads(row["payload"]) for row in notes])))
                self.db.executemany("INSERT OR IGNORE INTO profile_analysis_cache_inputs VALUES(?,?)",
                                    [(key, value) for value in fingerprints])

    def _reuse(self, check):
        # A batch is usable only when every source fragment (including full
        # bounded context, attribution and timestamp) still matches. A changed
        # message invalidates its own batch, not the other historical batches.
        while rows := self.store.rows(
                "SELECT c.key,c.notes,(SELECT min(p.id) FROM profile_analysis_cache_inputs i "
                "JOIN profile_draft_parts p ON p.fingerprint=i.fingerprint WHERE i.cache_key=c.key) first_part "
                "FROM profile_analysis_cache c WHERE c.signature=? "
                "AND NOT EXISTS (SELECT 1 FROM profile_analysis_cache_inputs i WHERE i.cache_key=c.key "
                "AND NOT EXISTS (SELECT 1 FROM profile_draft_parts p WHERE p.fingerprint=i.fingerprint AND p.done=0)) "
                "ORDER BY first_part,c.key LIMIT 100", (self.signature,)):
            for row in rows:
                check()
                with self.lock, self.db:
                    # Another valid cached bundle may overlap this one. Recheck
                    # under the write lock so no fragment contributes twice.
                    missing = self.db.execute(
                        "SELECT 1 FROM profile_analysis_cache_inputs i WHERE i.cache_key=? AND NOT EXISTS "
                        "(SELECT 1 FROM profile_draft_parts p WHERE p.fingerprint=i.fingerprint AND p.done=0) LIMIT 1",
                        (row["key"],)).fetchone()
                    if missing:
                        continue
                    for note in json.loads(row["notes"]):
                        self._note(note, row["first_part"])
                    self.db.execute("UPDATE profile_draft_parts SET done=1 WHERE fingerprint IN "
                                    "(SELECT fingerprint FROM profile_analysis_cache_inputs WHERE cache_key=?)", (row["key"],))

    def _note(self, note, first_part):
        position = self.db.execute("SELECT coalesce(max(position),0)+1 FROM profile_draft_notes WHERE level=0").fetchone()[0]
        self.db.execute("INSERT INTO profile_draft_notes VALUES(0,?,?,?)", (position, position, encoded(note)))
        self.db.execute("INSERT INTO profile_draft_note_order VALUES(?,?)", (position, first_part))

    def order_notes(self):
        rows = self.store.rows("SELECT n.position,n.payload,o.first_part FROM profile_draft_notes n "
                               "JOIN profile_draft_note_order o ON o.position=n.position WHERE n.level=0 "
                               "ORDER BY o.first_part,n.position")
        if [row["position"] for row in rows] == list(range(1, len(rows) + 1)):
            return
        with self.lock, self.db:
            self.db.execute("DELETE FROM profile_draft_notes")
            self.db.execute("DELETE FROM profile_draft_note_order")
            for row in rows:
                self._note(json.loads(row["payload"]), row["first_part"])

    def counts(self):
        parts = self.store.rows("SELECT count(*) total,coalesce(sum(done),0) analyzed FROM profile_draft_parts")[0]
        messages = self.store.rows("SELECT count(*) n FROM (SELECT message_id FROM profile_draft_parts "
                                   "GROUP BY message_id HAVING min(done)=1)")[0]["n"]
        return {"totalParts": parts["total"], "analyzedParts": parts["analyzed"], "analyzedMessages": messages}

    def pending(self, room):
        rows = self.store.rows("SELECT id,payload FROM profile_draft_parts WHERE done=0 ORDER BY id LIMIT 200")
        batch, size = [], 2
        for row in rows:
            cost = len(row["payload"]) + 2
            if size + cost > room:
                if not batch:
                    raise SelfError("context-too-long", "模型上下文已改变，请恢复原上下文设置后继续")
                break
            batch.append((row["id"], json.loads(row["payload"])))
            size += cost
        return batch

    def save_batch(self, batch, note):
        with self.lock, self.db:
            self._cache([payload for _, payload in batch], [note], self.signature)
            self._note(note, batch[0][0])
            self.db.executemany("UPDATE profile_draft_parts SET done=1 WHERE id=?", [(key,) for key, _ in batch])

    def note_count(self, level):
        return self.store.rows("SELECT count(*) n FROM profile_draft_notes WHERE level=?", (level,))[0]["n"]

    def next_notes(self, level, room):
        end = self.store.rows("SELECT coalesce(max(end_position),0) n FROM profile_draft_notes WHERE level=?", (level + 1,))[0]["n"]
        rows = self.store.rows("SELECT position,payload FROM profile_draft_notes WHERE level=? AND position>? "
                               "ORDER BY position LIMIT 64", (level, end))
        notes, size = [], 2
        for row in rows:
            cost = len(row["payload"]) + 2
            if notes and size + cost > room:
                break
            if size + cost > room:
                raise SelfError("context-too-long", "归纳内容超过模型上下文，请检查模型设置")
            notes.append((row["position"], json.loads(row["payload"])))
            size += cost
        if len(rows) > 1 and len(notes) < 2:
            raise SelfError("context-too-long", "归纳内容超过模型上下文，请检查模型设置")
        return notes

    def save_merge(self, level, notes, result):
        with self.lock, self.db:
            position = self.db.execute("SELECT coalesce(max(position),0)+1 FROM profile_draft_notes WHERE level=?", (level + 1,)).fetchone()[0]
            self.db.execute("INSERT INTO profile_draft_notes VALUES(?,?,?,?)",
                            (level + 1, position, notes[-1][0], encoded(result)))

    def last_note(self, level):
        return json.loads(self.store.rows("SELECT payload FROM profile_draft_notes WHERE level=? ORDER BY position LIMIT 1", (level,))[0]["payload"])
