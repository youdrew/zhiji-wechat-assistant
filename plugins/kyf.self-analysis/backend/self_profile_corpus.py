"""Durable, unsampled own-message corpus for a full personal-profile analysis.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

This corpus is independent of reply examples and tags. Core owns source access;
only its account-scoped history API and its saved external-chat records are read.
Private text stays in the local DB.
"""
import json
import math
import time
from collections import Counter

from kyf_plugin import Cancelled
from self_contracts import PAGE_SIZE, SelfError
from self_model import retry_unavailable

PREPARE_TIMEOUT = 180  # Maximum time without an advancing source read position.
CORPUS_VERSION = 4
COUNTS_VERSION = 1
CONTEXT_ROWS = 12
FOLLOWUP_ROWS = 6
CONTEXT_CHARS = 600
SPEAKER_PART_CHARS = 100
CONTEXT_WINDOW_MINUTES = 60
CONTEXT_WINDOW_MS = CONTEXT_WINDOW_MINUTES * 60 * 1000
SOURCE_ERRORS = {"account-changed", "account-unavailable", "source-changed", "core-unavailable"}
# A complete sync this recent is reused: a retry reads only the chats that new
# messages reopened instead of auditing every chat again (35 min on 3479 chats).
FRESH_SYNC_MS = 12 * 60 * 60 * 1000
# Core's history/status-many bound. Unchanged chats are confirmed this many per
# call instead of one prefix audit each, which was most of that 35 minutes.
STATUS_BATCH = 500
# Imported external chats join the corpus as one chat per source, keyed by this prefix.
EXTERNAL_PREFIX = "external:"
USER_TABLES = ("profile_corpus_messages", "profile_corpus_context", "profile_corpus_staging_messages",
               "profile_corpus_staging_context", "profile_corpus_proofs", "profile_corpus_staging_proofs",
               "profile_corpus_reply_rows", "profile_corpus_staging_reply_rows", "profile_corpus_conversations")
SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_corpus_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profile_corpus_conversations(
 user TEXT PRIMARY KEY,name TEXT NOT NULL,is_group INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending',source TEXT,epoch TEXT,revision,
 ceiling INTEGER,after_seq INTEGER NOT NULL DEFAULT 0,carry TEXT NOT NULL DEFAULT '[]',
 messages_read INTEGER NOT NULL DEFAULT 0,excluded INTEGER NOT NULL DEFAULT 0,
 unread INTEGER NOT NULL DEFAULT 0,partial INTEGER NOT NULL DEFAULT 0,gap TEXT,
 own_count INTEGER NOT NULL DEFAULT 0,context_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS profile_corpus_messages(
 rowid INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT NOT NULL UNIQUE,user TEXT NOT NULL,
 seq INTEGER NOT NULL,time_ms INTEGER NOT NULL,text TEXT NOT NULL,context TEXT NOT NULL,
 UNIQUE(user,seq));
CREATE INDEX IF NOT EXISTS profile_corpus_message_user ON profile_corpus_messages(user,seq);
CREATE TABLE IF NOT EXISTS profile_corpus_context(
 id TEXT NOT NULL UNIQUE,user TEXT NOT NULL,seq INTEGER NOT NULL,time_ms INTEGER NOT NULL,
 side TEXT NOT NULL,speaker TEXT NOT NULL,text TEXT NOT NULL,truncated INTEGER NOT NULL,
 PRIMARY KEY(user,seq));
CREATE INDEX IF NOT EXISTS profile_corpus_context_sides ON profile_corpus_context(user,seq,side);
CREATE TABLE IF NOT EXISTS profile_corpus_staging_messages(
 id TEXT PRIMARY KEY,user TEXT NOT NULL,seq INTEGER NOT NULL,time_ms INTEGER NOT NULL,
 text TEXT NOT NULL,context TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS profile_corpus_staging_message_user ON profile_corpus_staging_messages(user,seq);
CREATE TABLE IF NOT EXISTS profile_corpus_staging_context(
 id TEXT PRIMARY KEY,user TEXT NOT NULL,seq INTEGER NOT NULL,time_ms INTEGER NOT NULL,
 side TEXT NOT NULL,speaker TEXT NOT NULL,text TEXT NOT NULL,truncated INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS profile_corpus_staging_context_user ON profile_corpus_staging_context(user,seq);
CREATE TABLE IF NOT EXISTS profile_corpus_reply_rows(
 id TEXT PRIMARY KEY,user TEXT NOT NULL,seq INTEGER NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS profile_corpus_reply_order ON profile_corpus_reply_rows(user,seq);
CREATE TABLE IF NOT EXISTS profile_corpus_staging_reply_rows(
 id TEXT PRIMARY KEY,user TEXT NOT NULL,seq INTEGER NOT NULL,payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS profile_corpus_staging_reply_order ON profile_corpus_staging_reply_rows(user,seq);
CREATE TABLE IF NOT EXISTS profile_corpus_proofs(
 user TEXT NOT NULL,msg_id TEXT NOT NULL,hash TEXT NOT NULL,seq INTEGER NOT NULL,
 PRIMARY KEY(user,msg_id));
CREATE INDEX IF NOT EXISTS profile_corpus_proof_order ON profile_corpus_proofs(user,seq);
CREATE TABLE IF NOT EXISTS profile_corpus_staging_proofs(
 user TEXT NOT NULL,msg_id TEXT NOT NULL,hash TEXT NOT NULL,seq INTEGER NOT NULL,
 PRIMARY KEY(user,msg_id));
"""


def _number(value):
    return value if type(value) is int and value >= 0 else 0


def _count(value):
    return len(value) if isinstance(value, (list, dict)) else _number(value)


def _timestamp(value):
    return int(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else 0


def _plain(row, account, user):
    """Keep all attributable typed text, including punctuation and one-character replies."""
    text = row.get("text")
    if (row.get("kind") != "text" or row.get("side") not in ("self", "other") or
            not isinstance(text, str) or not text.strip()):
        return None
    raw = row.get("raw") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    meta = raw.get("inputMeta") or {}
    if not isinstance(meta, dict):
        return None
    if (any(raw.get(key) or meta.get(key) for key in ("forward", "forwarded", "isForward", "system", "recalled")) or
            meta.get("accountId") not in (None, account) or meta.get("conversationId") not in (None, user)):
        return None
    if (row.get("senderId") and meta.get("senderId") and row["senderId"] != meta["senderId"]):
        return None
    source = meta.get("source") or {}
    if not isinstance(source, dict) or source.get("kind") not in (None, "wechat"):
        return None
    # Core already unwraps quoted replies into the current sender's title. Its
    # quote metadata is context about someone else's message, never more prose.
    return text


def _excerpt(text):
    if len(text) <= CONTEXT_CHARS:
        return text, False
    marker = "\n…（中间省略）…\n"
    head = (CONTEXT_CHARS - len(marker)) // 2
    return text[:head] + marker + text[-(CONTEXT_CHARS - head - len(marker)):], True


def _speaker_part(text):
    if len(text) <= SPEAKER_PART_CHARS:
        return text
    head = (SPEAKER_PART_CHARS - 1) // 2
    return text[:head] + "…" + text[-(SPEAKER_PART_CHARS - head - 1):]


def _context_row(row, text, user, group):
    identifier = row.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise SelfError("invalid-page", "聊天消息缺少标识")
    speaker_truncated = False
    if row["side"] == "self":
        speaker = "本人"
    elif not group:
        speaker = "对方"
    else:
        name = row.get("senderName")
        sender = row.get("senderId")
        name = name.strip() if isinstance(name, str) else ""
        sender = sender.strip() if isinstance(sender, str) else ""
        if not name or not sender:
            raw = row.get("raw") or {}
            meta = (json.loads(raw) if isinstance(raw, str) else raw).get("inputMeta") or {}
            if not name and isinstance(meta.get("senderName"), str):
                name = meta["senderName"].strip()
            if not sender and isinstance(meta.get("senderId"), str):
                sender = meta["senderId"].strip()
        speaker_truncated = len(name) > SPEAKER_PART_CHARS or len(sender) > SPEAKER_PART_CHARS
        name, sender = _speaker_part(name), _speaker_part(sender)
        if name:
            speaker = "群友·" + name + (f"（{sender}）" if sender and sender != name else "")
        else:
            speaker = "群友·" + sender if sender else "群友（身份未知）"
    excerpt, truncated = _excerpt(text)
    return {"id": json.dumps([user, identifier], ensure_ascii=False), "seq": row["seq"],
            "time_ms": _timestamp(row.get("time")), "side": row["side"], "speaker": speaker,
            "text": excerpt, "truncated": truncated or speaker_truncated}


class ProfileCorpus:
    def __init__(self, store):
        self.store, self.db, self.lock = store, store.db, store.lock
        with self.lock, self.db:
            self.db.executescript(SCHEMA)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(profile_corpus_conversations)")}
            counts_added = not {"own_count", "context_count"} <= columns
            for column in ("catalog_generation", "rebuild", "refresh_required", "proofs_complete", "proofs_dirty",
                           "own_count", "context_count", "external"):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE profile_corpus_conversations ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
            # latest: the directory's newest source row, compared with prefix_ceiling (the
            # highwater of the last complete read) to skip unchanged chats.
            for column in ("source_revision", "prefix_signature", "prefix_ceiling", "latest"):
                if column not in columns:
                    self.db.execute(f"ALTER TABLE profile_corpus_conversations ADD COLUMN {column} TEXT")
            if "latest" not in columns:
                # An open round's directory predates this column; read it again.
                self.restart_catalog()
            for table in ("profile_corpus_proofs", "profile_corpus_staging_proofs"):
                if "relevant" not in {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN relevant INTEGER NOT NULL DEFAULT 0")
                    self.db.execute("UPDATE profile_corpus_conversations SET proofs_dirty=1")
                if "verified_round" not in {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN verified_round INTEGER NOT NULL DEFAULT 0")
            self.db.execute("CREATE INDEX IF NOT EXISTS profile_corpus_relevant_proof ON profile_corpus_proofs(user,relevant,seq)")
            if counts_added or self.meta("countsVersion") != COUNTS_VERSION:
                self._recount()
                self._meta("countsVersion", COUNTS_VERSION)
            if self.meta("version") != CORPUS_VERSION:
                # Older corpora may not have the full context needed by the current
                # analysis. Refresh each chat without deleting its saved messages.
                self.db.execute("UPDATE profile_corpus_conversations SET refresh_required=1,state='pending',ceiling=NULL")
                self._meta("version", CORPUS_VERSION)
                self.restart_catalog()

    def rows(self, query, values=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(query, values)]

    def meta(self, key, default=None):
        rows = self.rows("SELECT value FROM profile_corpus_meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def _meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO profile_corpus_meta VALUES(?,?)", (key, json.dumps(value)))

    def _changed(self):
        self._meta("revision", self.meta("revision", 0) + 1)

    def _recount(self, user=None):
        """One indexed recount on migration or after a whole chat is replaced."""
        query = ("UPDATE profile_corpus_conversations SET own_count=(SELECT count(*) FROM profile_corpus_messages m "
                 "WHERE m.user=profile_corpus_conversations.user),context_count=(SELECT count(*) FROM profile_corpus_context c "
                 "WHERE c.user=profile_corpus_conversations.user)")
        self.db.execute(query + (" WHERE user=?" if user is not None else ""), (user,) if user is not None else ())

    def _new_ids(self, table, items):
        identifiers = {item["id"] for item in items}
        if not identifiers:
            return 0
        marks = ",".join("?" for _ in identifiers)
        existing = self.rows(f"SELECT count(*) n FROM {table} WHERE id IN ({marks})", tuple(identifiers))[0]["n"]
        return len(identifiers) - existing

    def begin(self):
        """Start another sync while retaining the complete corpus and its identities."""
        with self.lock, self.db:
            self._meta("syncRound", self.meta("syncRound", 0) + 1)
            self._meta("roundOpen", True)
            self._meta("roundClosedAt", None)
            # A user may start a new run after interrupting a staged replacement.
            # Keep the committed chat; restart only its unfinished staging area.
            self.db.execute("DELETE FROM profile_corpus_staging_messages")
            self.db.execute("DELETE FROM profile_corpus_staging_context")
            self.db.execute("DELETE FROM profile_corpus_staging_reply_rows")
            self.db.execute("DELETE FROM profile_corpus_staging_proofs")
            self.db.execute("UPDATE profile_corpus_conversations SET state='pending',gap=NULL,ceiling=NULL,"
                            "after_seq=CASE WHEN rebuild THEN 0 ELSE after_seq END,"
                            "carry=CASE WHEN rebuild THEN '[]' ELSE carry END,"
                            "messages_read=CASE WHEN rebuild THEN 0 ELSE messages_read END,"
                            "excluded=CASE WHEN rebuild THEN 0 ELSE excluded END,unread=0,partial=0 WHERE external=0")
            self.restart_catalog()

    def restart_catalog(self):
        with self.lock, self.db:
            self._meta("catalogGeneration", self.meta("catalogGeneration", 0) + 1)
            for key, value in (("cursor", None), ("directoryRevision", None), ("catalogComplete", False),
                               ("directoryPartial", False), ("directoryGaps", {})):
                self._meta(key, value)

    def reset(self):
        with self.lock, self.db:
            revision = self.meta("revision", 0) + 1
            for table in ("profile_corpus_messages", "profile_corpus_context", "profile_corpus_staging_messages",
                          "profile_corpus_staging_context", "profile_corpus_reply_rows", "profile_corpus_staging_reply_rows",
                          "profile_corpus_conversations", "profile_corpus_meta"):
                self.db.execute(f"DELETE FROM {table}")
            self.db.execute("DELETE FROM profile_corpus_proofs")
            self.db.execute("DELETE FROM profile_corpus_staging_proofs")
            self._meta("version", CORPUS_VERSION)
            self._meta("revision", revision)
            self._meta("countsVersion", COUNTS_VERSION)

    def counts(self):
        with self.lock:
            counts = self.rows("SELECT count(*) conversations,sum(state='done') done,sum(state='failed') failed,"
                               "sum(messages_read) messages,sum(excluded) excluded,sum(unread) unread,"
                               "sum(partial) partial,coalesce(sum(own_count),0) own,"
                               "coalesce(sum(CASE WHEN is_group=0 AND external=0 THEN own_count ELSE 0 END),0) private,"
                               "coalesce(sum(CASE WHEN is_group=1 THEN own_count ELSE 0 END),0) groups,"
                               "coalesce(sum(CASE WHEN external=1 THEN own_count ELSE 0 END),0) external "
                               "FROM profile_corpus_conversations")[0]
            gaps = Counter(self.meta("directoryGaps", {}))
            for row in self.rows("SELECT gap,count(*) n FROM profile_corpus_conversations WHERE gap IS NOT NULL GROUP BY gap"):
                gaps[row["gap"]] += row["n"]
            if counts["unread"]:
                gaps["unread-messages"] = counts["unread"]
            if counts["partial"]:
                gaps["partial-conversations"] = counts["partial"]
            if self.meta("externalGap", 0):
                gaps["external-unreadable"] = 1
            return {"conversations": counts["conversations"], "readConversations": counts["done"] or 0,
                    "failedConversations": counts["failed"] or 0, "messagesRead": counts["messages"] or 0,
                    "ownMessages": counts["own"], "privateMessages": counts["private"],
                    "groupMessages": counts["groups"], "externalMessages": counts["external"],
                    "excludedMessages": counts["excluded"] or 0,
                    "unreadMessages": (counts["unread"] or 0) + self.meta("directoryGaps", {}).get("directory-unread-messages", 0),
                    "catalogComplete": self.meta("catalogComplete", False),
                    "directoryPartial": self.meta("directoryPartial", False),
                    "gaps": [{"kind": kind, "count": count} for kind, count in sorted(gaps.items()) if count > 0]}

    def iter_messages(self, after_id=0, limit=200):
        rows = self.rows("SELECT m.rowid,m.id,m.user,c.name,c.is_group,c.external,m.seq,m.time_ms,m.text,m.context "
                         "FROM profile_corpus_messages m JOIN profile_corpus_conversations c ON c.user=m.user "
                         "WHERE m.rowid>? ORDER BY m.rowid LIMIT ?", (after_id, max(1, min(limit, 1000))))
        return self._hydrate(rows)

    def messages_between(self, start_ms, end_ms):
        """Own messages of one time range without their context: a questionnaire searches
        a whole half-year but shows only a few lines, and those fetch theirs with ``around``."""
        return self.rows("SELECT m.id,m.user,c.name,m.time_ms,m.text FROM profile_corpus_messages m "
                         "JOIN profile_corpus_conversations c ON c.user=m.user "
                         "WHERE m.time_ms>=? AND m.time_ms<? ORDER BY m.rowid", (start_ms, end_ms))

    def iter_messages_like(self, patterns, limit=2000):
        """Own messages matching any SQL LIKE pattern, hydrated like ``iter_messages``.

        Deterministic sweeps (self-report recall) need the same context shape as
        model payloads without paging through the whole corpus; the cap bounds a
        pathological corpus and the sweep re-runs on every analysis pass anyway.
        """
        where = " OR ".join("m.text LIKE ?" for _ in patterns)
        rows = self.rows("SELECT m.rowid,m.id,m.user,c.name,c.is_group,c.external,m.seq,m.time_ms,m.text,m.context "
                         "FROM profile_corpus_messages m JOIN profile_corpus_conversations c ON c.user=m.user "
                         f"WHERE m.rowid>0 AND ({where}) ORDER BY m.rowid LIMIT ?",
                         (*patterns, max(1, min(limit, 5000))))
        return self._hydrate(rows)

    def _hydrate(self, rows):
        for row in rows:
            # Imported screenshots seldom show times, but their order is what was on screen:
            # an external chat's neighbors are its adjacent lines, without a time window.
            timed = not row.pop("external")
            before, before_unknown, before_inconsistent = self._neighbors(row, True, timed) if row["time_ms"] or not timed else ([], 0, 0)
            after, after_unknown, after_inconsistent = self._neighbors(row, False, timed) if row["time_ms"] or not timed else ([], 0, 0)
            unknown, inconsistent = before_unknown + after_unknown, before_inconsistent + after_inconsistent
            row.update(context_before=before, context_after=after,
                       context_info={"truncated": any(item["truncated"] for item in before + after) or bool(unknown + inconsistent),
                                     "timeUnknown": timed and (not bool(row["time_ms"]) or bool(unknown)), "windowMinutes": CONTEXT_WINDOW_MINUTES,
                                     "beforeLimit": CONTEXT_ROWS, "afterLimit": FOLLOWUP_ROWS,
                                     "omittedUnknownTime": unknown, "omittedInconsistentTime": inconsistent})
            row["context"] = "\n".join(item["speaker"] + "：" + item["text"] for item in before)
        return rows

    def around(self, message_id, before=3, after=2):
        """A short window of the chat around one own message, for showing evidence as a conversation."""
        rows = self.rows("SELECT m.user,m.seq,m.time_ms,c.external FROM profile_corpus_messages m "
                         "JOIN profile_corpus_conversations c ON c.user=m.user WHERE m.id=?", (message_id,))
        if not rows:
            return None
        message = rows[0]
        timed = not message["external"]
        if timed and not message["time_ms"]:
            return {"before": [], "after": []}
        keep = ("side", "speaker", "text", "time")
        earlier = self._neighbors(message, True, timed)[0][-before:] if before else []
        later = self._neighbors(message, False, timed)[0][:after] if after else []
        return {"before": [{key: row[key] for key in keep} for row in earlier],
                "after": [{key: row[key] for key in keep} for row in later]}

    def _neighbors(self, message, before, timed=True):
        if before:
            rows = self.rows("SELECT id,seq,time_ms,side,speaker,text,truncated FROM profile_corpus_context "
                             "WHERE user=? AND seq<? ORDER BY seq DESC LIMIT ?",
                             (message["user"], message["seq"], CONTEXT_ROWS))
            rows.reverse()
        else:
            rows = self.rows("SELECT id,seq,time_ms,side,speaker,text,truncated FROM profile_corpus_context "
                             "WHERE user=? AND seq>? ORDER BY seq LIMIT ?",
                             (message["user"], message["seq"], FOLLOWUP_ROWS))
        found, unknown, inconsistent = [], 0, 0
        for row in rows:
            if not timed:
                found.append({"id": row["id"], "seq": row["seq"], "time": row["time_ms"] or None, "side": row["side"],
                              "speaker": row["speaker"], "text": row["text"], "truncated": bool(row["truncated"])})
                continue
            if not row["time_ms"]:
                unknown += 1
                continue
            delta = message["time_ms"] - row["time_ms"] if before else row["time_ms"] - message["time_ms"]
            if delta < 0:
                inconsistent += 1
            elif delta <= CONTEXT_WINDOW_MS:
                found.append({"id": row["id"], "seq": row["seq"], "time": row["time_ms"], "side": row["side"],
                              "speaker": row["speaker"], "text": row["text"], "truncated": bool(row["truncated"])})
        return found, unknown, inconsistent

    def catalog(self, page):
        source = page.get("sourceFingerprint")
        if not isinstance(source, str) or not source:
            raise SelfError("source-unavailable", "无法确认聊天来源")
        with self.lock, self.db:
            if self.meta("source") not in (None, source):
                raise SelfError("source-changed", "聊天来源已变化，已保留原有资料库，请恢复原来源后继续")
            self._meta("source", source)
            revision = page.get("revision")
            if self.meta("directoryRevision") not in (None, revision):
                raise SelfError("directory-expired", "聊天目录已变化，请重新开始本人资料分析")
            self._meta("directoryRevision", revision)
            coverage = page.get("coverage") or {}
            gaps = {"failed-shards": _count(coverage.get("failedShards")),
                    "unresolved-tables": _count(coverage.get("unresolvedTables")),
                    "directory-unread-messages": _number(coverage.get("skippedMessages"))}
            previous_gaps = self.meta("directoryGaps", {})
            gaps = {key: max(value, previous_gaps.get(key, 0)) for key, value in gaps.items()}
            partial = self.meta("directoryPartial", False) or coverage.get("state", "complete") != "complete" or any(gaps.values())
            if partial:
                gaps["directory-partial"] = 1
            self._meta("directoryPartial", partial)
            self._meta("directoryGaps", gaps)
            generation = self.meta("catalogGeneration", 0)
            for row in page.get("conversations", []):
                user = row.get("user")
                if not isinstance(user, str) or not user:
                    raise SelfError("invalid-page", "聊天目录结构无效")
                name, group = row.get("name") or "", int(bool(row.get("isGroup")) or user.endswith("@chatroom"))
                old = self.rows("SELECT name,is_group FROM profile_corpus_conversations WHERE user=?", (user,))
                if old and (old[0]["name"], old[0]["is_group"]) != (name, group):
                    self._changed()
                    # Speaker attribution in group context also needs refreshing.
                    if old[0]["is_group"] != group:
                        self.db.execute("UPDATE profile_corpus_conversations SET refresh_required=1 WHERE user=?", (user,))
                latest = row.get("latest")
                latest = latest if isinstance(latest, str) and len(latest) <= 512 else None
                self.db.execute("INSERT INTO profile_corpus_conversations(user,name,is_group,catalog_generation,latest) "
                                "VALUES(?,?,?,?,?) ON CONFLICT(user) DO UPDATE SET name=excluded.name,is_group=excluded.is_group,"
                                "catalog_generation=excluded.catalog_generation,latest=excluded.latest",
                                (user, name, group, generation, latest))
            if page.get("next") is None and not partial:
                removed = self.rows("SELECT user FROM profile_corpus_conversations WHERE catalog_generation<>? AND external=0",
                                    (generation,))
                for row in removed:
                    for table in USER_TABLES:
                        self.db.execute(f"DELETE FROM {table} WHERE user=?", (row["user"],))
                if removed:
                    self._changed()
            self._meta("cursor", page.get("next"))
            self._meta("catalogComplete", page.get("next") is None)

    def pending(self):
        rows = self.rows("SELECT * FROM profile_corpus_conversations WHERE state='pending' AND external=0 ORDER BY user LIMIT 1")
        return rows[0] if rows else None

    def round_open(self):
        """Whether the last sync round still has unfinished or failed chats."""
        value = self.meta("roundOpen")
        if value is None:
            # Corpora from before this flag: an interrupted round leaves non-done chats.
            return bool(self.rows("SELECT 1 FROM profile_corpus_conversations WHERE state<>'done' LIMIT 1") and
                        self.meta("syncRound", 0))
        return bool(value)

    def close_round(self):
        """Close the round once every chat is done; failed chats keep it open for retry."""
        with self.lock, self.db:
            if not self.rows("SELECT 1 FROM profile_corpus_conversations WHERE state<>'done' LIMIT 1"):
                # Reopening a changed projection is incremental, not a new complete source audit.
                # Only begin() clears this stamp, so repeated updates cannot prolong its 12h window.
                if self.meta("roundOpen", True) and self.meta("roundClosedAt") is None:
                    self._meta("roundClosedAt", int(time.time() * 1000))
                self._meta("roundOpen", False)

    def recently_synced(self):
        """Whether the last full round closed within FRESH_SYNC_MS with a complete directory."""
        closed = self.meta("roundClosedAt")
        return (type(closed) is int and 0 <= time.time() * 1000 - closed <= FRESH_SYNC_MS and
                self.meta("catalogComplete", False) and not self.round_open())

    def retry(self):
        with self.lock, self.db:
            self.db.execute("UPDATE profile_corpus_conversations SET state='pending',gap=NULL WHERE state='failed'")

    def skip(self, user):
        """Finish an unchanged chat this round, keeping its saved corpus and proofs."""
        with self.lock, self.db:
            self.db.execute("UPDATE profile_corpus_conversations SET state='done',gap=NULL,ceiling=after_seq,unread=0,partial=0 "
                            "WHERE user=? AND state='pending'", (user,))

    def reopen(self, user):
        with self.lock, self.db:
            self.db.execute("UPDATE profile_corpus_conversations SET state='pending',gap=NULL,ceiling=NULL,unread=0,partial=0 "
                            "WHERE user=?", (user,))
            self._meta("roundOpen", True)

    def bind(self, user, status, force=False):
        with self.lock, self.db:
            row = self.rows("SELECT * FROM profile_corpus_conversations WHERE user=?", (user,))[0]
            source, epoch = status.get("sourceFingerprint"), status.get("epoch")
            if row["source"] not in (None, source):
                raise SelfError("source-changed", "聊天来源已变化，已保留原有资料库")
            ceiling = (status.get("watermark") or {}).get("seq", _number(status.get("readCount")))
            if type(ceiling) is not int or ceiling < 0:
                raise SelfError("invalid-page", "聊天读取位置无效")
            changed = row["source"] is not None and (
                row["epoch"] != epoch or ceiling < row["after_seq"] or
                (ceiling == row["after_seq"] and row["revision"] != status.get("revision")))
            if force or row["refresh_required"] or changed:
                for table in ("profile_corpus_staging_messages", "profile_corpus_staging_context", "profile_corpus_staging_proofs",
                              "profile_corpus_staging_reply_rows"):
                    self.db.execute(f"DELETE FROM {table} WHERE user=?", (user,))
                self.db.execute("UPDATE profile_corpus_conversations SET rebuild=1,refresh_required=0,after_seq=0,"
                                "ceiling=NULL,carry='[]',messages_read=0,excluded=0,proofs_complete=1 WHERE user=?", (user,))
                row = self.rows("SELECT * FROM profile_corpus_conversations WHERE user=?", (user,))[0]
            elif row["ceiling"] is not None:
                ceiling = row["ceiling"]
            total = _number(status.get("totalCount"))
            read = _number(status.get("readCount"))
            unread = max(total - read, _number(status.get("skippedMessages")), _number(status.get("skippedCount")))
            partial = status.get("state") in ("partial", "error", "unsupported") or status.get("partial") is True or bool(status.get("error"))
            self.db.execute("UPDATE profile_corpus_conversations SET source=?,epoch=?,revision=?,ceiling=?,unread=?,partial=?,"
                            "source_revision=?,proofs_complete=CASE WHEN source IS NULL THEN 1 ELSE proofs_complete END WHERE user=?",
                            (source, epoch, status.get("revision"), ceiling, unread, int(partial), status.get("sourceRevision"), user))
            return self.rows("SELECT * FROM profile_corpus_conversations WHERE user=?", (user,))[0]

    def page(self, user, rows, found, carry, excluded, expected_after, status, context_rows=(), reply_rows=()):
        with self.lock, self.db:
            local = self.rows("SELECT * FROM profile_corpus_conversations WHERE user=?", (user,))[0]
            if (local["after_seq"] != expected_after or local["source"] != status.get("sourceFingerprint") or
                    local["epoch"] != status.get("epoch")):
                raise SelfError("source-changed", "聊天读取进度或来源已变化")
            message_table = "profile_corpus_staging_messages" if local["rebuild"] else "profile_corpus_messages"
            context_table = "profile_corpus_staging_context" if local["rebuild"] else "profile_corpus_context"
            proof_table = "profile_corpus_staging_proofs" if local["rebuild"] else "profile_corpus_proofs"
            reply_table = "profile_corpus_staging_reply_rows" if local["rebuild"] else "profile_corpus_reply_rows"
            own_delta = self._new_ids(message_table, found) if not local["rebuild"] else 0
            context_delta = self._new_ids(context_table, context_rows) if not local["rebuild"] else 0
            self.db.executemany(f"INSERT INTO {reply_table}(id,user,seq,payload) VALUES(?,?,?,?) "
                                "ON CONFLICT(id) DO UPDATE SET seq=excluded.seq,payload=excluded.payload",
                                [(json.dumps([user, item["id"]], ensure_ascii=False), user, item["seq"],
                                  json.dumps(item, ensure_ascii=False, sort_keys=True)) for item in reply_rows])
            # Hashes contain no message text. Keep all source identities so a
            # former image/system row becoming typed text is not missed either.
            proofs = [(user, item.get("id"), item.get("hash"), item["seq"]) for item in rows
                      if isinstance(item.get("id"), str) and item.get("id") and
                      isinstance(item.get("hash"), str) and item.get("hash")]
            sync_round = self.meta("syncRound", 0)
            self.db.executemany(f"INSERT INTO {proof_table}(user,msg_id,hash,seq,verified_round) VALUES(?,?,?,?,?) "
                                "ON CONFLICT(user,msg_id) DO UPDATE SET hash=excluded.hash,seq=excluded.seq,"
                                "verified_round=excluded.verified_round",
                                [proof + (sync_round,) for proof in proofs])
            if len(proofs) != len(rows):
                self.db.execute("UPDATE profile_corpus_conversations SET proofs_complete=0 WHERE user=?", (user,))
            self.db.executemany(f"INSERT INTO {message_table}(id,user,seq,time_ms,text,context) VALUES(?,?,?,?,?,?) "
                                "ON CONFLICT(id) DO UPDATE SET seq=excluded.seq,time_ms=excluded.time_ms,"
                                "text=excluded.text,context=excluded.context",
                                [(item["id"], user, item["seq"], item["time_ms"], item["text"], item["context"]) for item in found])
            self.db.executemany(f"INSERT INTO {context_table}(id,user,seq,time_ms,side,speaker,text,truncated) "
                                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET seq=excluded.seq,time_ms=excluded.time_ms,"
                                "side=excluded.side,speaker=excluded.speaker,text=excluded.text,truncated=excluded.truncated",
                                [(item["id"], user, item["seq"], item["time_ms"], item["side"], item["speaker"],
                                  item["text"], int(item["truncated"])) for item in context_rows])
            self.db.execute("UPDATE profile_corpus_conversations SET after_seq=?,carry=?,messages_read=messages_read+?,excluded=excluded+?,"
                            "own_count=own_count+?,context_count=context_count+? WHERE user=?",
                            (rows[-1]["seq"], json.dumps(carry, ensure_ascii=False), len(rows), excluded, own_delta, context_delta, user))
            self.db.execute("UPDATE profile_corpus_conversations SET proofs_dirty=1 WHERE user=?", (user,))
            if not local["rebuild"] and (found or context_rows):
                # New context may change a previously analysed message's following
                # lines even when no new own message was added.
                self._changed()

    def _commit_rebuild(self, user):
        changed = False
        for table, staging, fields in (
                ("profile_corpus_messages", "profile_corpus_staging_messages", ("seq", "time_ms", "text", "context")),
                ("profile_corpus_context", "profile_corpus_staging_context",
                 ("seq", "time_ms", "side", "speaker", "text", "truncated")),
                ("profile_corpus_reply_rows", "profile_corpus_staging_reply_rows", ("seq", "payload"))):
            differences = " OR ".join(f"old.{key} IS NOT new.{key}" for key in fields)
            different = self.db.execute(f"SELECT 1 FROM {staging} new LEFT JOIN {table} old ON old.id=new.id "
                                        f"WHERE new.user=? AND (old.id IS NULL OR {differences}) LIMIT 1", (user,)).fetchone()
            removed = self.db.execute(f"DELETE FROM {table} WHERE user=? AND NOT EXISTS "
                                      f"(SELECT 1 FROM {staging} new WHERE new.id={table}.id)", (user,)).rowcount
            changed |= bool(different or removed)
            # Free the sequence keys before a rebuilt Core projection renumbers
            # messages. Upsert by identity preserves surviving own-message rowids.
            self.db.execute(f"UPDATE {table} SET seq=-rowid WHERE user=?", (user,))
            names = ",".join(("id", "user") + fields)
            updates = ",".join(f"{key}=excluded.{key}" for key in fields)
            self.db.execute(f"INSERT INTO {table}({names}) SELECT {names} FROM {staging} WHERE user=? "
                            f"ON CONFLICT(id) DO UPDATE SET {updates}", (user,))
            self.db.execute(f"DELETE FROM {staging} WHERE user=?", (user,))
        if changed:
            self._changed()
        self.db.execute("DELETE FROM profile_corpus_proofs WHERE user=?", (user,))
        self.db.execute("INSERT INTO profile_corpus_proofs SELECT * FROM profile_corpus_staging_proofs WHERE user=?", (user,))
        self.db.execute("DELETE FROM profile_corpus_staging_proofs WHERE user=?", (user,))
        self._recount(user)

    def _mark_relevant_proofs(self, user):
        """Verify only own words and the context candidates actually used around them.

        Keep unknown-time candidates too: a later timestamp correction may make
        them enter the model's context. Full hashes also cover omitted middles of
        context excerpts, without re-reading unrelated chats' other messages.
        """
        self.db.execute("UPDATE profile_corpus_proofs SET relevant=0 WHERE user=? AND relevant<>0", (user,))
        # Every private row can affect reply pairing, including a media barrier.
        # The shared sync must also invalidate knowledge-base examples after edits.
        if self.meta("sharedReplies", False) and self.rows(
                "SELECT 1 FROM profile_corpus_conversations WHERE user=? AND is_group=0", (user,)):
            self.db.execute("UPDATE profile_corpus_proofs SET relevant=1 WHERE user=?", (user,))
            self.db.execute("UPDATE profile_corpus_conversations SET proofs_dirty=0 WHERE user=?", (user,))
            return
        counts = self.rows("SELECT own_count,context_count FROM profile_corpus_conversations WHERE user=?", (user,))[0]
        if (counts["own_count"] >= 512 and
                counts["own_count"] * (CONTEXT_ROWS + FOLLOWUP_ROWS + 1) >= counts["context_count"]):
            # A dense chat needs one covering-index pass rather than two neighbor
            # queries and many proof writes for every own message. Reverse the
            # limits: a context row is wanted by own words before or after it.
            self.db.execute(f"WITH marked AS (SELECT seq,sum(side='self') OVER (ORDER BY seq "
                            f"ROWS BETWEEN {FOLLOWUP_ROWS} PRECEDING AND {CONTEXT_ROWS} FOLLOWING) wanted "
                            "FROM profile_corpus_context WHERE user=?) "
                            "UPDATE profile_corpus_proofs SET relevant=1 WHERE user=? "
                            "AND seq IN (SELECT seq FROM marked WHERE wanted>0)", (user, user))
            self.db.execute("UPDATE profile_corpus_conversations SET proofs_dirty=0 WHERE user=?", (user,))
            return
        after = 0
        while messages := self.rows("SELECT id,seq FROM profile_corpus_messages WHERE user=? AND seq>? "
                                   "ORDER BY seq LIMIT 200", (user, after)):
            wanted = set()
            for message in messages:
                wanted.add(message["id"])
                for operator, order, limit in (("<", "DESC", CONTEXT_ROWS), (">", "ASC", FOLLOWUP_ROWS)):
                    wanted.update(row["id"] for row in self.rows(
                        f"SELECT id FROM profile_corpus_context WHERE user=? AND seq{operator}? ORDER BY seq {order} LIMIT ?",
                        (user, message["seq"], limit)))
            identifiers = [(user, json.loads(identifier)[1]) for identifier in wanted]
            self.db.executemany("UPDATE profile_corpus_proofs SET relevant=1 WHERE user=? AND msg_id=?", identifiers)
            after = messages[-1]["seq"]
        self.db.execute("UPDATE profile_corpus_conversations SET proofs_dirty=0 WHERE user=?", (user,))

    def finish(self, user, failed=False, gap=None):
        with self.lock, self.db:
            row = self.rows("SELECT rebuild,proofs_dirty,partial,unread FROM profile_corpus_conversations WHERE user=?", (user,))[0]
            if (not failed and row["rebuild"] and (row["partial"] or row["unread"]) and
                    self.rows("SELECT 1 FROM profile_corpus_messages WHERE user=? LIMIT 1", (user,))):
                raise SelfError("stale-corpus", "聊天重读仍有缺口，原有资料库已保留，请恢复完整读取后继续")
            if not failed and row["rebuild"]:
                self._commit_rebuild(user)
                self.db.execute("UPDATE profile_corpus_conversations SET rebuild=0 WHERE user=?", (user,))
            if not failed and row["proofs_dirty"]:
                self._mark_relevant_proofs(user)
            self.db.execute("UPDATE profile_corpus_conversations SET state=?,gap=? WHERE user=?",
                            ("failed" if failed else "done", gap, user))
            if failed:
                self.db.execute("UPDATE profile_corpus_conversations SET unread=unread+max(0,coalesce(ceiling,0)-after_seq) WHERE user=?",
                                (user,))

    def replace_external(self, chats, unreadable=False):
        """Mirror the imported external chats, rebuilt from Core's saved records each sync.

        ``chats`` maps each corpus user (EXTERNAL_PREFIX + source id) to its name, own
        message rows, context lines and excluded count. A source missing from it was
        deleted or hidden in the import and leaves the corpus. When Core could not be
        read, the chats mirrored last time stay as they were and the gap is reported.
        """
        with self.lock, self.db:
            self._meta("externalGap", 1 if unreadable else 0)
            if unreadable:
                return
            for row in self.rows("SELECT user FROM profile_corpus_conversations WHERE external=1"):
                if row["user"] not in chats:
                    for table in USER_TABLES:
                        self.db.execute(f"DELETE FROM {table} WHERE user=?", (row["user"],))
                    self._changed()
            for user, chat in chats.items():
                old = self.rows("SELECT name FROM profile_corpus_conversations WHERE user=?", (user,))
                if old and old[0]["name"] != chat["name"]:
                    self._changed()
                read = len(chat["context"]) + chat["excluded"]
                self.db.execute("INSERT INTO profile_corpus_conversations(user,name,is_group,external,state,proofs_complete) "
                                "VALUES(?,?,0,1,'done',1) ON CONFLICT(user) DO UPDATE SET name=excluded.name,external=1,"
                                "state='done',gap=NULL", (user, chat["name"]))
                self.db.executemany("INSERT INTO profile_corpus_staging_messages(id,user,seq,time_ms,text,context) VALUES(?,?,?,?,?,?)",
                                    [(item["id"], user, item["seq"], item["time_ms"], item["text"], item["context"])
                                     for item in chat["messages"]])
                self.db.executemany("INSERT INTO profile_corpus_staging_context(id,user,seq,time_ms,side,speaker,text,truncated) "
                                    "VALUES(?,?,?,?,?,?,?,?)",
                                    [(item["id"], user, item["seq"], item["time_ms"], item["side"], item["speaker"],
                                      item["text"], int(item["truncated"])) for item in chat["context"]])
                self._commit_rebuild(user)
                self.db.execute("UPDATE profile_corpus_conversations SET messages_read=?,excluded=?,after_seq=?,ceiling=? WHERE user=?",
                                (read, chat["excluded"], read, read, user))


def external_chat(user, messages):
    """Corpus rows for one imported source: typed text by the owner or the other side, in order."""
    found, context, excluded, carry = [], [], 0, []
    for seq, item in enumerate(messages, 1):
        text = item.get("text")
        if (item.get("kind", "text") != "text" or item.get("sender") not in ("self", "other") or
                not isinstance(text, str) or not text.strip() or not isinstance(item.get("id"), str)):
            excluded += 1
            carry = []
            continue
        identifier = json.dumps([user, item["id"]], ensure_ascii=False)
        excerpt, truncated = _excerpt(text)
        speaker, time_ms = "本人" if item["sender"] == "self" else "对方", _timestamp(item.get("time"))
        context.append({"id": identifier, "seq": seq, "time_ms": time_ms, "side": item["sender"], "speaker": speaker,
                        "text": excerpt, "truncated": truncated})
        if item["sender"] == "self":
            found.append({"id": identifier, "seq": seq, "time_ms": time_ms, "text": text, "context": "\n".join(carry)})
        carry = (carry + [speaker + "：" + excerpt])[-CONTEXT_ROWS:]
    return {"messages": found, "context": context, "excluded": excluded}


class Reader:
    def __init__(self, core, account, corpus, context, progress, reuse_recent=False):
        self.core, self.account, self.corpus = core, account, corpus
        self.reuse_recent = reuse_recent
        self.context, self.progress = context, progress
        self.source_messages_read = 0
        self.source_messages_verified = 0
        self.current_chat_done, self.current_chat_total = 0, 0

    def _progress(self, phase="reading"):
        counts = self.corpus.counts()
        if self.progress is not None:
            self.progress(counts)
        update = getattr(self.context, "progress", None)
        if callable(update):
            update(done=counts["readConversations"] + counts["failedConversations"], total=counts["conversations"],
                   label={"preparing": "准备聊天记录", "verifying": "核对已有记录"}.get(phase, "读取全部聊天"),
                   unit="个聊天", phase=phase,
                   sourceMessagesRead=self.source_messages_read,
                   sourceMessagesVerified=self.source_messages_verified,
                   currentChatDone=self.current_chat_done, currentChatTotal=self.current_chat_total,
                   verifiedConversationsDone=counts["readConversations"], verifiedConversationsTotal=counts["conversations"])

    def run(self, resume=None):
        """Sync every chat. By default an unfinished round continues instead of restarting."""
        self.context.check()
        if resume is None:
            resume = self.corpus.round_open()
        reused = False
        if resume:
            self.corpus.retry()
        elif self.reuse_recent and self.corpus.recently_synced() and self._still_current():
            reused = True
        else:
            self.corpus.begin()
        self._discover()
        self._progress()
        # The age limit avoids another source audit, not a check of Core's local projection.
        # Events can be missed while the plugin is closed; batch-check done chats on reuse too.
        if resume or reused:
            self._recheck_done()
        batched = self._skip_unchanged()
        while (row := self.corpus.pending()) is not None:
            self.context.check()
            try:
                if not batched and self._unchanged(row):
                    self.context.check()
                    self.corpus.skip(row["user"])
                else:
                    self._read(row)
            except Cancelled:
                raise
            except Exception as exc:
                self.context.check()
                code = getattr(exc, "code", None)
                if code in SOURCE_ERRORS:
                    raise SelfError(code, "聊天来源不可用或已切换，请恢复连接或重新分析") from None
                if code == "verification-unavailable":
                    raise
                # Keep this chat's committed corpus and continue; the failed chat is
                # reported as a gap and retried when the round is resumed.
                self.corpus.finish(row["user"], failed=True,
                                   gap="noncontiguous-messages" if code == "invalid-page" else "unreadable-conversations")
            self._progress()
        self._external()
        self._progress()
        self.corpus.close_round()
        return self.corpus.counts()

    def _still_current(self):
        """A reused sync must pass the check every stage ends with. Core can rebuild a chat within
        the window, and reusing the sync then would stop each run until the window ran out."""
        rows = self.corpus.rows("SELECT user,epoch FROM profile_corpus_conversations WHERE state='done' AND external=0 "
                                "ORDER BY user LIMIT 1")
        if not rows:
            return True
        status, source = self.core.history_status(self.account, rows[0]["user"]), self.corpus.meta("source")
        return (status.get("sourceFingerprint") == source and status.get("currentSourceFingerprint", source) == source and
                status.get("epoch") == rows[0]["epoch"])

    def _recheck_done(self):
        """Cheaply re-audit chats finished earlier in this round instead of re-verifying proofs.

        ``sourceRevision`` covers every message shard and the source process, so it changes after a
        restart or any new message anywhere; it cannot tell whether this chat changed. Core's per-chat
        projection identity also catches updates whose ``message.new`` event was missed by the plugin.
        """
        source = self.corpus.meta("source")
        rows = self.corpus.rows("SELECT * FROM profile_corpus_conversations "
                                "WHERE state='done' AND external=0 ORDER BY user")
        statuses = self._statuses(rows)
        if statuses is not None:
            for row, status in statuses:
                if not self._current(row, status):
                    self.corpus.reopen(row["user"])
            self._progress()
            return
        for index, row in enumerate(rows):
            self.context.check()
            if index % 100 == 0:
                self.current_chat_done, self.current_chat_total = index, len(rows)
                self._progress(phase="verifying")
            status = retry_unavailable(lambda: self.core.history_status(self.account, row["user"]))
            self.context.check()
            if status.get("currentSourceFingerprint", status.get("sourceFingerprint")) != source:
                raise SelfError("source-changed", "聊天来源已变化，已保留原有资料库")
            if not self._current(row, status):
                self.corpus.reopen(row["user"])
        self._progress()

    def _discover(self):
        if self.corpus.meta("catalogComplete", False):
            return
        cursor, seen = self.corpus.meta("cursor"), set()
        restarted = False
        while True:
            self.context.check()
            try:
                page = self.core.history_conversations(self.account, after=cursor, limit=PAGE_SIZE)
            except Exception as exc:
                if cursor and not restarted and getattr(exc, "code", str(exc)) in ("directory-expired", "invalid-cursor"):
                    self.corpus.restart_catalog()
                    cursor, seen, restarted = None, set(), True
                    continue
                raise SelfError("source-changed" if getattr(exc, "code", None) == "source-changed" else "directory-unavailable",
                                "聊天目录读取未完成，可恢复连接后继续") from None
            self.context.check()
            try:
                self.corpus.catalog(page)
            except SelfError as exc:
                if exc.code == "directory-expired" and not restarted:
                    self.corpus.restart_catalog()
                    cursor, seen, restarted = None, set(), True
                    continue
                raise
            self._progress()
            cursor = page.get("next")
            if cursor is None:
                return
            if cursor in seen:
                raise SelfError("invalid-page", "聊天目录分页没有前进")
            seen.add(cursor)

    def _external(self):
        """Add the owner's saved messages from imported external chats.

        Friends bound to this WeChat account and external-only friends are included; each
        import source is one chat. Only records confirmed in the import are served by Core.
        """
        get = getattr(self.core, "get", None)
        if not callable(get):
            return
        chats = {}
        try:
            friends = get("external/friends").get("friends")
            if not isinstance(friends, list):
                raise SelfError("invalid-page", "外部聊天好友列表无效")
            for friend in friends:
                self.context.check()
                if not isinstance(friend, dict) or friend.get("wechatAccount") not in (None, "", self.account):
                    continue
                history = get("external/history", friendId=friend["id"])
                sources = {source["id"]: source for source in history.get("sources", []) if isinstance(source, dict)}
                grouped = {}
                for message in history.get("messages", []):
                    if isinstance(message, dict) and message.get("sourceId") in sources:
                        grouped.setdefault(message["sourceId"], []).append(message)
                for source_id, messages in grouped.items():
                    messages.sort(key=lambda item: str(item.get("order", "")))
                    user = EXTERNAL_PREFIX + source_id
                    name = f"{friend.get('name') or '外部好友'}（{sources[source_id].get('platform') or '外部来源'}·外部聊天）"
                    chats[user] = {"name": name[:200], **external_chat(user, messages)}
        except Cancelled:
            raise
        except Exception:
            self.context.check()
            self.corpus.replace_external({}, unreadable=True)
            return
        self.context.check()
        self.corpus.replace_external(chats)

    def _guard(self, status, epoch=None):
        source = self.corpus.meta("source")
        if (status.get("sourceFingerprint") != source or status.get("currentSourceFingerprint", source) != source or
                (epoch is not None and status.get("epoch") != epoch)):
            raise SelfError("source-changed", "聊天来源或版本已变化")

    def _audit(self, user, highwater=None):
        try:
            status = retry_unavailable(lambda: self.core.history_status(
                self.account, user, audit=True,
                **({"audit_highwater": highwater} if highwater is not None else {})))
        except TypeError:
            # Older adapters do not expose source metadata auditing. Their
            # epoch/revision remains usable, without claiming fresh source checks.
            status = retry_unavailable(lambda: self.core.history_status(self.account, user))
        current = status.get("currentSourceFingerprint", status.get("sourceFingerprint"))
        if current is not None and current != self.corpus.meta("source"):
            raise SelfError("source-changed", "聊天来源已变化，已保留原有资料库")
        return status

    def _statuses(self, rows):
        """Pair rows with Core's ordinary status, STATUS_BATCH chats per call; None if Core cannot batch."""
        many = getattr(self.core, "history_status_many", None)
        if not callable(many):
            return None
        source, paired = self.corpus.meta("source"), []
        for start in range(0, len(rows), STATUS_BATCH):
            self.context.check()
            batch = rows[start:start + STATUS_BATCH]
            try:
                response = retry_unavailable(lambda: many(self.account, [row["user"] for row in batch]))
            except Exception as exc:
                if not paired and getattr(exc, "code", None) == "not-found":
                    return None  # an older Core without the batch route
                if getattr(exc, "code", None) == "source-changed":
                    raise SelfError("source-changed", "聊天来源已变化，已保留原有资料库") from None
                raise
            self.context.check()
            results = response.get("results") if isinstance(response, dict) else None
            if (not isinstance(results, list) or len(results) != len(batch) or
                    any(not isinstance(status, dict) or status.get("user") != row["user"]
                        for row, status in zip(batch, results))):
                raise SelfError("invalid-page", "聊天状态结构无效")
            if response.get("currentSourceFingerprint", source) != source:
                raise SelfError("source-changed", "聊天来源已变化，已保留原有资料库")
            paired.extend(zip(batch, results))
        return paired

    def _skip_unchanged(self):
        """Finish, in batches, chats whose newest source row is still the end of their last read.

        Returns False when Core cannot batch, leaving each chat to the per-chat prefix audit.
        Batched skipping trusts the directory position and Core's projection identity; a message
        removed or inserted behind an unchanged newest row waits for that chat's next change.
        """
        rows = [row for row in self.corpus.rows("SELECT * FROM profile_corpus_conversations "
                                                "WHERE state='pending' AND external=0 ORDER BY user")
                if self._skippable(row)]
        statuses = self._statuses(rows)
        if statuses is None:
            return False
        for index, (row, status) in enumerate(statuses):
            if self._current(row, status):
                self.corpus.skip(row["user"])
            if index % STATUS_BATCH == STATUS_BATCH - 1:
                self._progress()
        self._progress()
        return True

    @staticmethod
    def _skippable(row):
        return not (row["latest"] is None or row["latest"] != row["prefix_ceiling"] or row["prefix_signature"] is None or
                    row["source"] is None or row["rebuild"] or row["refresh_required"] or row["proofs_dirty"] or
                    not row["proofs_complete"])

    @staticmethod
    def _current(row, status):
        """Whether Core's projection is still exactly the one this chat was last read from."""
        watermark = status.get("watermark") or {}
        return (status.get("state") == "ready" and status.get("importing") is not True and not status.get("error") and
                status.get("sourceFingerprint") == row["source"] and status.get("epoch") == row["epoch"] and
                status.get("revision") == row["revision"] and status.get("highwater") == row["prefix_ceiling"] and
                watermark.get("seq") == row["after_seq"] and
                _number(status.get("readCount")) >= _number(status.get("totalCount")))

    def _unchanged(self, row):
        """Whether a chat can be skipped without re-reading or re-verifying its saved text.

        The directory's newest row must equal the highwater of the last complete read, and the
        aggregate signature of that read prefix must be the same, so no message was added, removed
        or renumbered. An in-place text edit behind the prefix is not detected here; reply examples
        are still checked against their original messages whenever retrieval returns them.
        """
        if not self._skippable(row):
            return False
        status = self._audit(row["user"], row["prefix_ceiling"])
        return (self._current(row, status) and status.get("sourcePrefixCeiling") == row["prefix_ceiling"] and
                status.get("sourcePrefixSignature") == row["prefix_signature"])

    def _verify_previous(self, row, audit, pending_only=False):
        """Return whether this chat needs rebuilding, without treating failed checks as edits."""
        if row["source"] is None or "sourceRevision" not in audit:
            return False
        if row["refresh_required"]:
            return True
        if row["rebuild"]:
            return audit.get("sourceRevision") is None or row["source_revision"] != audit.get("sourceRevision")
        signature = audit.get("sourcePrefixSignature")
        prefix_changed = (row["prefix_signature"] is not None and signature is not None and
                          row["prefix_ceiling"] == audit.get("sourcePrefixCeiling") and
                          row["prefix_signature"] != signature)
        if prefix_changed and not pending_only:
            return True
        revision = audit.get("sourceRevision")
        if not pending_only and revision is not None and row["source_revision"] == revision:
            return False
        if not row["proofs_complete"]:
            # Legacy records have no complete source proof set. One local
            # refresh supplies it while preserving all unchanged row identities.
            return True
        self.current_chat_done, self.current_chat_total = 0, 0
        self._progress(phase="verifying")
        after = 0
        round_id = self.corpus.meta("syncRound", 0)
        condition = " AND verified_round<>?" if pending_only else ""
        while proofs := self.corpus.rows("SELECT seq,msg_id,hash FROM profile_corpus_proofs WHERE user=? AND relevant=1 AND seq>? "
                                        + condition + " ORDER BY seq LIMIT 500",
                                        (row["user"], after, round_id) if pending_only else (row["user"], after)):
            self.context.check()
            items = [{"id": item["msg_id"], "hash": item["hash"]} for item in proofs]
            many, verify, post = (getattr(self.core, name, None) for name in
                                  ("history_verify_many", "history_verify", "post"))
            try:
                if callable(many):
                    response = many(self.account, [{"user": row["user"], "items": items}])
                    groups = response.get("results") if isinstance(response, dict) else None
                    response = groups[0] if isinstance(groups, list) and len(groups) == 1 else None
                    if not isinstance(response, dict) or response.get("user") != row["user"]:
                        response = None
                elif callable(verify):
                    response = verify(self.account, row["user"], items, fresh=True)
                elif callable(post):
                    response = post("history/verify", {"account": self.account, "user": row["user"],
                                                        "items": items, "fresh": True})
                else:
                    response = None
            except Cancelled:
                raise
            except Exception as exc:
                if getattr(exc, "code", None) in SOURCE_ERRORS:
                    raise
                raise SelfError("verification-unavailable", "旧消息暂时无法核对，原有资料库已保留，可稍后继续") from None
            self.context.check()
            results = response.get("results") if isinstance(response, dict) else None
            if (not isinstance(results, list) or len(results) != len(items) or
                    any(not isinstance(item, dict) or item.get("id") != proof["id"] or
                        item.get("state") not in ("same", "changed", "missing")
                        for item, proof in zip(results, items))):
                raise SelfError("verification-unavailable", "旧消息暂时无法核对，原有资料库已保留，可稍后继续")
            if any(item["state"] != "same" for item in results):
                return True
            with self.corpus.lock, self.corpus.db:
                self.corpus.db.executemany("UPDATE profile_corpus_proofs SET verified_round=? WHERE user=? AND msg_id=?",
                                           [(round_id, row["user"], item["id"]) for item in items])
            self.source_messages_verified += len(items)
            self._progress(phase="verifying")
            after = proofs[-1]["seq"]
        return False

    def _ready(self, user, rebuild=False):
        status = retry_unavailable(lambda: self.core.history_prepare(
            self.account, user, refresh=True, **({"rebuild": True} if rebuild else {})))
        advanced_at = time.monotonic()
        best_read, best_seq, reported_at = 0, 0, None
        while status.get("state") not in ("ready", "partial") or status.get("importing") is True:
            self.context.check()
            current = status.get("currentSourceFingerprint")
            if current is not None and current != self.corpus.meta("source"):
                raise SelfError("source-changed", "聊天来源已变化")
            if status.get("sourceFingerprint") is not None:
                self._guard(status)
            now = time.monotonic()
            watermark = status.get("watermark") or {}
            read = _number(status.get("readCount"))
            seq = _number(watermark.get("seq")) if isinstance(watermark, dict) else 0
            if read > best_read or seq > best_seq:
                advanced_at = now
                best_read, best_seq = max(best_read, read), max(best_seq, seq)
            if status.get("state") in ("error", "unsupported") or now - advanced_at >= PREPARE_TIMEOUT:
                # Keep the known source shortfall even if preparation cannot finish.
                if status.get("sourceFingerprint") is not None:
                    self._guard(status)
                    self.corpus.bind(user, status)
                raise SelfError("history-error", "会话读取未完成")
            if reported_at is None or now - reported_at >= 1:
                self.current_chat_done = max(read, seq)
                self.current_chat_total = max(_number(status.get("totalCount")), self.current_chat_done)
                self._progress(phase="preparing")
                reported_at = now
            self.context.cancelled.wait(0.25)
            self.context.check()
            status = retry_unavailable(lambda: self.core.history_status(self.account, user))
        self.context.check()
        self._guard(status)
        return status

    def _read(self, row, force_rebuild=False):
        user = row["user"]
        audit = self._audit(user, row["prefix_ceiling"])
        rebuild = force_rebuild or self._verify_previous(row, audit)
        status = self._ready(user, rebuild=rebuild)
        local = self.corpus.bind(user, status, force=rebuild)
        baseline = audit
        highwater = status.get("highwater")
        if highwater is not None and (audit.get("sourcePrefixCeiling") != highwater or
                                      audit.get("sourcePrefixSignature") is None):
            # The old-prefix audit detects edits/additions behind its highwater.
            # Only advancing chats need a second aggregate to establish the new
            # checkpoint; this is never repeated for each message page.
            baseline = self._audit(user, highwater)
        after, carry, ceiling = local["after_seq"], json.loads(local["carry"]), local["ceiling"]
        self.current_chat_done, self.current_chat_total = after, ceiling
        self._progress()
        while after < ceiling:
            self.context.check()
            page = retry_unavailable(lambda: self.core.history_messages(self.account, user, after=after, limit=PAGE_SIZE))
            self.context.check()
            self._guard(page, status.get("epoch"))
            fresh = retry_unavailable(lambda: self.core.history_status(self.account, user))
            self.context.check()
            self._guard(fresh, status.get("epoch"))
            rows = page.get("messages")
            if not isinstance(rows, list):
                raise SelfError("invalid-page", "聊天消息分页结构无效")
            rows = [item for item in rows if type(item.get("seq")) is int and item["seq"] <= ceiling]
            if not rows or any(item["seq"] != after + index + 1 for index, item in enumerate(rows)):
                raise SelfError("invalid-page", "聊天消息分页不连续")
            found, context_rows, excluded = [], [], 0
            for item in rows:
                text = _plain(item, self.account, user)
                if text is None:
                    excluded += 1
                    carry = []
                    continue
                context_line = _context_row(item, text, user, bool(row["is_group"]))
                context_rows.append(context_line)
                if item["side"] == "self":
                    found.append({"id": context_line["id"], "seq": item["seq"],
                                  "time_ms": _timestamp(item.get("time")), "text": text, "context": "\n".join(carry)})
                carry = (carry + [context_line["speaker"] + "：" + context_line["text"]])[-CONTEXT_ROWS:]
            self.context.check()
            reply_rows = []
            if not row["is_group"]:
                from self_samples import _plain as reply_plain
                for item in rows:
                    # Only style-eligible original text is retained here. Everything
                    # else is an explicit pairing barrier, never media/raw payloads.
                    plain = reply_plain(item, user, self.account)
                    reply_rows.append({**plain, "kind": "text"} if plain else
                                      {"id": item["id"], "seq": item["seq"], "hash": item.get("hash") or "",
                                       "kind": "barrier"})
            self.corpus.page(user, rows, found, carry, excluded, after, status,
                             context_rows=context_rows, reply_rows=reply_rows)
            after = rows[-1]["seq"]
            self.source_messages_read += len(rows)
            self.current_chat_done = after
            self._progress()
        fresh = retry_unavailable(lambda: self.core.history_status(self.account, user))
        self.context.check()
        self._guard(fresh, status.get("epoch"))
        self.corpus.finish(user)
        if (row["source"] is not None and "sourceRevision" in audit and
                (audit.get("sourceRevision") is None or row["source_revision"] != audit.get("sourceRevision"))):
            # A new own message can make old, previously unused context relevant.
            # Check those newly selected dependencies before exposing an analysis.
            current = self.corpus.rows("SELECT * FROM profile_corpus_conversations WHERE user=?", (user,))[0]
            try:
                refresh = self._verify_previous(current, audit, pending_only=True)
            except Exception:
                with self.corpus.lock, self.corpus.db:
                    self.corpus.db.execute("UPDATE profile_corpus_conversations SET state='failed',refresh_required=1 WHERE user=?", (user,))
                raise
            if refresh:
                with self.corpus.lock, self.corpus.db:
                    self.corpus.db.execute("UPDATE profile_corpus_conversations SET state='failed',refresh_required=1 WHERE user=?", (user,))
                if force_rebuild:
                    raise SelfError("verification-unavailable", "消息上下文尚未完成核对，原有资料库已保留，请稍后继续")
                return self._read(current, force_rebuild=True)
        with self.corpus.lock, self.corpus.db:
            if (isinstance(baseline.get("sourcePrefixSignature"), str) and
                    baseline.get("sourcePrefixCeiling") is not None):
                self.corpus.db.execute("UPDATE profile_corpus_conversations SET prefix_signature=?,prefix_ceiling=? WHERE user=?",
                                       (baseline["sourcePrefixSignature"], baseline["sourcePrefixCeiling"], user))
