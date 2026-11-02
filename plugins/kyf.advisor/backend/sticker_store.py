"""Sticker identities and labels in the plugin's per-account database.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

``sticker_messages`` maps a mirrored sticker message to its content MD5 (Core reads it from the WeChat
source; a message id never changes its content, so the mapping is kept across mirror resets).
``stickers`` holds one row per distinct sticker of the account, shared by every friend: the store pack,
the official caption and pack name when WeChat has them, and the short label a vision model wrote.
"""
from __future__ import annotations

import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sticker_messages (
    user TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    sticker_id TEXT,
    PRIMARY KEY(user, msg_id)
);
CREATE INDEX IF NOT EXISTS sticker_messages_sticker ON sticker_messages(user, sticker_id);
CREATE TABLE IF NOT EXISTS stickers (
    sticker_id TEXT PRIMARY KEY,
    pack TEXT,
    pack_name TEXT,
    caption TEXT,
    description TEXT,
    model_fp TEXT,
    failures INTEGER NOT NULL DEFAULT 0,
    failed_model_fp TEXT,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sticker_requests (
    request_id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    state TEXT NOT NULL,
    task_id TEXT,
    created_at_ms INTEGER NOT NULL
);
"""
# A mirrored message is a sticker when Core's projection says so (raw media metadata).
_IS_STICKER = "json_extract(c.raw, '$.media.kind') = 'sticker'"


def ensure_schema(connection):
    connection.executescript(SCHEMA)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(stickers)")}
    if "failure_reason" not in columns:
        connection.execute("ALTER TABLE stickers ADD COLUMN failure_reason TEXT")


def _bounded(value, maximum):
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:maximum] or None


class StickerStoreMixin:
    def sticker_request(self, request_id):
        return self._row("SELECT * FROM sticker_requests WHERE request_id=?", (request_id,))

    def claim_sticker_request(self, user, request_id):
        """Persist admission before Core creates a task; an uncertain claim is never replayed."""
        with self.lock:
            connection = self._connect()
            with connection:
                inserted = connection.execute(
                    "INSERT OR IGNORE INTO sticker_requests VALUES(?,?,'prepared',NULL,?)",
                    (request_id, user, int(time.time() * 1000))).rowcount
                row = connection.execute("SELECT * FROM sticker_requests WHERE request_id=?", (request_id,)).fetchone()
            return dict(row), bool(inserted)

    def finish_sticker_request(self, user, request_id, task_id):
        with self.lock:
            connection = self._connect()
            with connection:
                connection.execute("UPDATE sticker_requests SET state=?,task_id=? "
                                   "WHERE request_id=? AND user=? AND state='prepared' AND task_id IS NULL",
                                   ("submitted" if task_id else "no-work", task_id, request_id, user))

    def sticker_unresolved(self, user, upto_seq, limit):
        """Mirrored sticker messages (oldest first) whose identity has not been asked of Core yet."""
        return [row["msg_id"] for row in self._rows(
            "SELECT c.msg_id FROM context_messages c LEFT JOIN sticker_messages m ON m.user=c.user AND m.msg_id=c.msg_id "
            f"WHERE c.user=? AND c.seq<=? AND m.msg_id IS NULL AND {_IS_STICKER} ORDER BY c.seq ASC LIMIT ?",
            (user, upto_seq, limit))]

    def save_sticker_refs(self, user, asked, details):
        """Record Core's answer for ``asked`` message ids; an id without details has no readable identity."""
        now = int(time.time() * 1000)
        with self.lock:
            connection = self._connect()
            with connection:
                for message_id in asked:
                    item = details.get(message_id) if isinstance(details, dict) else None
                    identifier = item.get("id") if isinstance(item, dict) else None
                    if not (isinstance(identifier, str) and len(identifier) == 32 and
                            all(char in "0123456789abcdef" for char in identifier)):
                        identifier = None
                    connection.execute("INSERT OR REPLACE INTO sticker_messages(user,msg_id,sticker_id) VALUES(?,?,?)",
                                       (user, message_id, identifier))
                    if identifier is None:
                        continue
                    connection.execute("INSERT OR IGNORE INTO stickers(sticker_id,updated_at_ms) VALUES(?,?)",
                                       (identifier, now))
                    # WeChat's own texts only fill in; a later read without them keeps what is known.
                    connection.execute(
                        "UPDATE stickers SET pack=COALESCE(?,pack), pack_name=COALESCE(?,pack_name),"
                        " caption=COALESCE(?,caption) WHERE sticker_id=?",
                        (_bounded(item.get("pack"), 128), _bounded(item.get("packName"), 40),
                         _bounded(item.get("caption"), 40), identifier))

    def sticker_infos(self, user, upto_seq):
        """``{message id: {"id", "caption", "packName", "description"}}`` of the conversation's known stickers."""
        rows = self._rows(
            "SELECT m.msg_id, m.sticker_id, s.caption, s.pack_name, s.description FROM sticker_messages m "
            "JOIN context_messages c ON c.user=m.user AND c.msg_id=m.msg_id "
            "LEFT JOIN stickers s ON s.sticker_id=m.sticker_id WHERE m.user=? AND c.seq<=?", (user, upto_seq))
        return {row["msg_id"]: {"id": row["sticker_id"], "caption": row["caption"], "packName": row["pack_name"],
                                "description": row["description"]} for row in rows}

    def sticker_queue(self, user, limit, max_failures, model_fp):
        """Stickers of this conversation that need a label, most used first, each with up to three of its messages.

        A sticker with an official caption needs none. Failures count per model, so a sticker a model could not
        read is tried again once another model is configured.
        """
        rows = self._rows(
            "SELECT m.sticker_id, COUNT(*) AS uses, MAX(c.seq) AS last_seq FROM sticker_messages m "
            "JOIN context_messages c ON c.user=m.user AND c.msg_id=m.msg_id "
            "JOIN stickers s ON s.sticker_id=m.sticker_id "
            "WHERE m.user=? AND s.description IS NULL AND s.caption IS NULL "
            "AND (s.failures<? OR s.failed_model_fp IS NOT ?) "
            "GROUP BY m.sticker_id ORDER BY uses DESC, last_seq DESC LIMIT ?", (user, max_failures, model_fp, limit))
        queue = []
        for row in rows:
            messages = [item["msg_id"] for item in self._rows(
                "SELECT c.msg_id FROM sticker_messages m JOIN context_messages c ON c.user=m.user AND c.msg_id=m.msg_id "
                "WHERE m.user=? AND m.sticker_id=? ORDER BY c.seq DESC LIMIT 3", (user, row["sticker_id"]))]
            queue.append({"id": row["sticker_id"], "uses": row["uses"], "messages": messages})
        return queue

    def sticker_work_pending(self, user, max_failures, model_fp):
        """Whether the mirrored conversation has sticker messages to identify or stickers to label."""
        if self._row(
                "SELECT 1 FROM context_messages c LEFT JOIN sticker_messages m ON m.user=c.user AND m.msg_id=c.msg_id "
                f"WHERE c.user=? AND m.msg_id IS NULL AND {_IS_STICKER} LIMIT 1", (user,)):
            return True
        return self._row(
            "SELECT 1 FROM sticker_messages m JOIN stickers s ON s.sticker_id=m.sticker_id "
            "WHERE m.user=? AND s.description IS NULL AND s.caption IS NULL "
            "AND (s.failures<? OR s.failed_model_fp IS NOT ?) LIMIT 1", (user, max_failures, model_fp)) is not None

    def save_sticker_description(self, sticker_id, text, model_fp):
        with self.lock:
            connection = self._connect()
            with connection:
                connection.execute("UPDATE stickers SET description=?, model_fp=?, failure_reason=NULL, updated_at_ms=? WHERE sticker_id=?",
                                   (text, model_fp, int(time.time() * 1000), sticker_id))

    def sticker_failed(self, sticker_id, model_fp, reason="unknown"):
        if reason not in ("media-unavailable", "model-error", "empty-label", "unknown"):
            raise ValueError("invalid sticker failure reason")
        with self.lock:
            connection = self._connect()
            with connection:
                connection.execute(
                    "UPDATE stickers SET failures=CASE WHEN failed_model_fp IS ? THEN failures+1 ELSE 1 END,"
                    " failed_model_fp=?, failure_reason=?, updated_at_ms=? WHERE sticker_id=?",
                    (model_fp, model_fp, reason, int(time.time() * 1000), sticker_id))
