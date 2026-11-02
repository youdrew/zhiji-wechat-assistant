"""Versioned input-situation embeddings, stored in bounded SQLite batches.

The encoder belongs to Core. This module never downloads or loads a model.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import hashlib
import math
import struct

DIMENSIONS = 512
BATCH_SIZE = 16


def input_digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def model_info(value):
    if (not isinstance(value, dict) or value.get("ready") is not True or value.get("dimensions") != DIMENSIONS or
            not isinstance(value.get("modelId"), str) or not value["modelId"] or
            not isinstance(value.get("revision"), (str, int)) or isinstance(value["revision"], bool)):
        return None
    return {"modelId": value["modelId"], "revision": str(value["revision"]), "dimensions": DIMENSIONS}


def vectors(response, info, count):
    if (not isinstance(response, dict) or any(str(response.get(key)) != str(info[key]) for key in
                                           ("modelId", "revision", "dimensions")) or
            not isinstance(response.get("vectors"), list) or len(response["vectors"]) != count):
        raise ValueError("invalid embedding response")
    output = []
    for vector in response["vectors"]:
        if (not isinstance(vector, list) or len(vector) != DIMENSIONS or
                any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)):
            raise ValueError("invalid embedding vector")
        norm = math.sqrt(sum(value * value for value in vector))
        if not math.isfinite(norm) or norm < 1e-12:
            raise ValueError("empty embedding vector")
        output.append([float(value / norm) for value in vector])
    return output


def ensure_schema(db):
    """A metadata-only migration; legacy sample digests are filled with each vector batch."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(samples)")}
    if "input_digest" not in columns:
        db.execute("ALTER TABLE samples ADD COLUMN input_digest TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE TABLE IF NOT EXISTS embeddings(sample_id TEXT PRIMARY KEY,model_id TEXT NOT NULL,"
               "model_revision TEXT NOT NULL,dimensions INTEGER NOT NULL,input_digest TEXT NOT NULL,vector BLOB NOT NULL)")
    db.execute("PRAGMA user_version=2")


class VectorStore:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def _valid_join():
        return ("e.sample_id=s.id AND e.model_id=? AND e.model_revision=? AND e.dimensions=? "
                "AND s.input_digest<>'' AND e.input_digest=s.input_digest AND length(e.vector)=2048")

    def counts(self, info):
        source = self.store.meta("source")
        base = "s.source=? AND NOT EXISTS(SELECT 1 FROM exclusions WHERE sample_id=s.id)"
        total = self.store.rows("SELECT count(*) n FROM samples s WHERE " + base, (source,))[0]["n"]
        indexed = 0
        if info:
            values = (info["modelId"], info["revision"], info["dimensions"], source)
            indexed = self.store.rows("SELECT count(*) n FROM samples s JOIN embeddings e ON " + self._valid_join() +
                                      " WHERE " + base, values)[0]["n"]
        return {"indexedSamples": indexed, "pendingSamples": total - indexed, "totalSamples": total}

    def pending(self, info, limit=BATCH_SIZE):
        return self.store.rows("SELECT s.id,s.input,s.source FROM samples s LEFT JOIN embeddings e ON " + self._valid_join() +
                               " WHERE s.source=? AND e.sample_id IS NULL AND NOT EXISTS(SELECT 1 FROM exclusions "
                               "WHERE sample_id=s.id) ORDER BY s.id LIMIT ?",
                               (info["modelId"], info["revision"], info["dimensions"], self.store.meta("source"), limit))

    def save(self, rows, response, info, generation, source):
        encoded = vectors(response, info, len(rows))
        store = self.store
        with store.lock, store.db:
            if store.meta("generation", 0) != generation or store.meta("source") != source:
                return 0
            saved = 0
            for row, vector in zip(rows, encoded):
                current = store.rows("SELECT input,source FROM samples WHERE id=? AND NOT EXISTS "
                                     "(SELECT 1 FROM exclusions WHERE sample_id=id)", (row["id"],))
                if not current or current[0]["input"] != row["input"] or current[0]["source"] != source:
                    continue
                digest = input_digest(row["input"])
                store.db.execute("UPDATE samples SET input_digest=? WHERE id=?", (digest, row["id"]))
                store.db.execute("INSERT OR REPLACE INTO embeddings VALUES(?,?,?,?,?,?)",
                                 (row["id"], info["modelId"], info["revision"], info["dimensions"], digest,
                                  struct.pack("<" + "f" * DIMENSIONS, *vector)))
                saved += 1
            # Retrieval ranking can improve without invalidating content already
            # used by a suggestion. Its exact source digests remain unchanged.
            if saved:
                store.bump()
            return saved

    def scan_page(self, info, source, before, user=None, before_seq=None, after="", size=512):
        clauses, values = ["s.source=?", "s.time_ms<?", "s.id>?",
                           "NOT EXISTS(SELECT 1 FROM exclusions WHERE sample_id=s.id)"], [source, before, after]
        if user and before_seq:
            clauses.append("(s.user<>? OR s.to_seq<?)")
            values.extend((user, before_seq))
        return self.store.rows("SELECT s.id,e.vector FROM samples s JOIN embeddings e ON " + self._valid_join() +
                               " WHERE " + " AND ".join(clauses) + " ORDER BY s.id LIMIT ?",
                               (info["modelId"], info["revision"], info["dimensions"], *values, size))
