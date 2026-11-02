"""Account-local store for 本人标签: own messages from every chat, tag mentions and the user's edits.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Tags share the reply library's database but not its tables: they read group chats
too, and clearing the reply library leaves them alone. Each mention points at the
original message it came from. The user's confirm/delete/add choices are keyed by
tag name, so re-analysis, merging and source changes never overwrite them.
"""
import json
import re
import time
from collections import Counter

from self_contracts import SelfError, now_ms
from self_samples import normalize

CATEGORIES = ("运动健身", "游戏棋牌", "影音娱乐", "阅读学习", "美食饮食", "旅行出行", "工作技能", "身份生活",
              "性格习惯", "观点态度", "其他")
NAME_CHARS = 20
EVIDENCE_LIMIT = 50
ANALYSIS_VERSION = 2
FOLLOWUP_ROWS = 2
CONTEXT_GAP_MS = 30 * 60 * 1000
RELATIONS = ("mention", "occasional", "interest", "trait", "uncertain")
RELATION_ORDER = {"uncertain": 0, "mention": 1, "occasional": 2, "trait": 3, "interest": 4}
NEGATIVE_NAME = re.compile(r"^(?:不|没|无|非|讨厌|拒绝|反对|厌恶|戒)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tag_conversations(
 user TEXT PRIMARY KEY,name TEXT NOT NULL DEFAULT '',is_group INTEGER NOT NULL DEFAULT 0,
 generation INTEGER NOT NULL DEFAULT 0,epoch TEXT,after_seq INTEGER NOT NULL DEFAULT 0,
 carry TEXT NOT NULL DEFAULT '[]',state TEXT NOT NULL DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS tag_messages(
 id TEXT PRIMARY KEY,user TEXT NOT NULL,seq INTEGER NOT NULL,time_ms INTEGER NOT NULL,
 text TEXT NOT NULL,context TEXT NOT NULL,followup TEXT NOT NULL DEFAULT '',truncated INTEGER NOT NULL DEFAULT 0,
 followup_count INTEGER NOT NULL DEFAULT 0,followup_seq INTEGER NOT NULL DEFAULT 0,
 context_data TEXT NOT NULL DEFAULT '{}',input_digest TEXT NOT NULL DEFAULT '',sync_generation INTEGER NOT NULL DEFAULT 0,
 state TEXT NOT NULL DEFAULT 'pending');
CREATE INDEX IF NOT EXISTS tag_message_state ON tag_messages(state,user,seq);
CREATE TABLE IF NOT EXISTS tag_mentions(
 message_id TEXT NOT NULL,key TEXT NOT NULL,name TEXT NOT NULL,category TEXT NOT NULL,explicit INTEGER NOT NULL,
 relation TEXT NOT NULL DEFAULT 'uncertain',quote TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(message_id,key));
CREATE INDEX IF NOT EXISTS tag_mention_key ON tag_mentions(key);
CREATE TABLE IF NOT EXISTS tag_aliases(key TEXT PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tag_edits(
 key TEXT PRIMARY KEY,name TEXT NOT NULL,category TEXT NOT NULL,state TEXT NOT NULL,updated_ms INTEGER NOT NULL);
"""
LEVELS = {"confirmed": 0, "strong": 1, "possible": 2}


def clean_name(value):
    if not isinstance(value, str):
        return ""
    name = " ".join(value.strip().strip("#＃\"'“”‘’「」《》").split())
    return name if 0 < len(name) <= NAME_CHARS and any(char.isalnum() for char in name) else ""


def day(ms):
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000))


class TagStore:
    def __init__(self, store):
        self.store, self.lock, self.db = store, store.lock, store.db
        with self.lock, self.db:
            self.db.executescript(SCHEMA)
            followup_metadata_added = False
            for table, additions in (
                    ("tag_messages", {"followup": "TEXT NOT NULL DEFAULT ''", "truncated": "INTEGER NOT NULL DEFAULT 0",
                                      "followup_count": "INTEGER NOT NULL DEFAULT 0", "followup_seq": "INTEGER NOT NULL DEFAULT 0",
                                      "context_data": "TEXT NOT NULL DEFAULT '{}'", "input_digest": "TEXT NOT NULL DEFAULT ''",
                                      "sync_generation": "INTEGER NOT NULL DEFAULT 0"}),
                    ("tag_mentions", {"relation": "TEXT NOT NULL DEFAULT 'uncertain'", "quote": "TEXT NOT NULL DEFAULT ''",
                                      "reason": "TEXT NOT NULL DEFAULT ''"})):
                columns = {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}
                for column, definition in additions.items():
                    if column not in columns:
                        self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                        followup_metadata_added |= table == "tag_messages" and column in ("followup_count", "followup_seq")
            self.db.execute("CREATE INDEX IF NOT EXISTS tag_message_context_tail ON tag_messages(user,time_ms,seq) "
                            "WHERE followup_count<2")
            if self.store.meta("tagAnalysisVersion") != ANALYSIS_VERSION or followup_metadata_added:
                existing = (self.rows("SELECT count(*) n FROM tag_messages")[0]["n"] > 0 or
                            self.rows("SELECT count(*) n FROM tag_conversations")[0]["n"] > 0)
                self.db.execute("UPDATE tag_mentions SET relation='uncertain',quote='',reason=''")
                self.store._meta("tagNeedsReview", existing)
                self.store._meta("tagAnalysisVersion", ANALYSIS_VERSION)
        if self.state().get("state") == "running":
            self.update(state="paused", error="上次分析已中断，继续会从已保存的位置恢复")

    def rows(self, query, values=()):
        return self.store.rows(query, values)

    def state(self):
        return {**self.store.meta("tagRun", {"state": "idle"}),
                "needsReview": self.store.meta("tagNeedsReview", False)}

    def reanalyze(self):
        """Re-read legacy evidence with richer context, keeping all manual decisions."""
        with self.lock, self.db:
            for table in ("tag_mentions", "tag_aliases", "tag_messages", "tag_conversations"):
                self.db.execute(f"DELETE FROM {table}")
            self.store._meta("tagRun", {"state": "idle", "catalogComplete": False, "merged": False})
            self.store._meta("tagNeedsReview", False)

    def update(self, **changes):
        with self.lock, self.db:
            run = {**self.state(), **changes, "updatedAt": now_ms()}
            self.store._meta("tagRun", run)
            return run

    # Reading ----------------------------------------------------------------------------------------------------------
    def begin(self):
        """Every run re-reads the directory; conversations keep their read position."""
        with self.lock, self.db:
            self.store._meta("tagGeneration", self.store.meta("tagGeneration", 0) + 1)
            self.db.execute("UPDATE tag_conversations SET state='pending'")
            return self.update(state="running", error=None, startedAt=now_ms())

    def resume(self):
        """Continue a stopped run; chats that failed to read are tried again."""
        with self.lock, self.db:
            self.db.execute("UPDATE tag_conversations SET state='pending' WHERE state='failed'")
            return self.update(state="running", error=None)

    def begin_corpus(self, source, settings):
        """Sync a completed shared snapshot without touching user choices or aliases."""
        with self.lock, self.db:
            maximum = self.rows("SELECT max(generation) n FROM tag_conversations")[0]["n"] or 0
            generation = max(maximum, self.store.meta("tagCorpusGeneration", 0)) + 1
            self.store._meta("tagCorpusGeneration", generation)
            self.store._meta("tagCorpusSettings", settings)
            self.store._meta("tagSource", source)
            self.store._meta("tagNeedsReview", False)
            self.update(state="running", error=None, phase="reading", done=0, total=0, catalogComplete=False)
            return generation

    def corpus_page(self, rows, generation):
        with self.lock, self.db:
            for row in rows:
                old = self.rows("SELECT input_digest FROM tag_messages WHERE id=?", (row["id"],))
                changed = not old or old[0]["input_digest"] != row["input_digest"]
                if changed:
                    self.db.execute("DELETE FROM tag_mentions WHERE message_id=?", (row["id"],))
                    self.db.execute("INSERT INTO tag_messages(id,user,seq,time_ms,text,context,followup,truncated,"
                                    "context_data,input_digest,sync_generation,state) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending') "
                                    "ON CONFLICT(id) DO UPDATE SET user=excluded.user,seq=excluded.seq,time_ms=excluded.time_ms,"
                                    "text=excluded.text,context=excluded.context,followup=excluded.followup,truncated=excluded.truncated,"
                                    "context_data=excluded.context_data,input_digest=excluded.input_digest,"
                                    "sync_generation=excluded.sync_generation,state='pending'",
                                    (row["id"], row["user"], row["seq"], row["time_ms"], row["text"], row["context"],
                                     row["followup"], 0, json.dumps(row["context_data"], ensure_ascii=False),
                                     row["input_digest"], generation))
                    self.update(merged=False)
                else:
                    self.db.execute("UPDATE tag_messages SET seq=?,context=?,followup=?,context_data=?,sync_generation=? WHERE id=?",
                                    (row["seq"], row["context"], row["followup"],
                                     json.dumps(row["context_data"], ensure_ascii=False), generation, row["id"]))
                self.db.execute("INSERT INTO tag_conversations(user,name,is_group,generation,state) VALUES(?,?,?,?,'done') "
                                "ON CONFLICT(user) DO UPDATE SET name=excluded.name,is_group=excluded.is_group,"
                                "generation=excluded.generation,state='done'",
                                (row["user"], row["name"], int(bool(row["is_group"])), generation))

    def finish_corpus(self, generation):
        with self.lock, self.db:
            stale = self.rows("SELECT id FROM tag_messages WHERE sync_generation<>?", (generation,))
            if stale:
                self.db.execute("DELETE FROM tag_mentions WHERE message_id IN "
                                "(SELECT id FROM tag_messages WHERE sync_generation<>?)", (generation,))
                self.db.execute("DELETE FROM tag_messages WHERE sync_generation<>?", (generation,))
                self.update(merged=False)
            self.db.execute("DELETE FROM tag_conversations WHERE generation<>?", (generation,))
            self.update(catalogComplete=True)

    def catalog(self, page):
        with self.lock, self.db:
            source = page["sourceFingerprint"]
            if self.store.meta("tagSource") not in (None, source):
                # Another WeChat source: its messages are different evidence. Edits stay.
                for table in ("tag_mentions", "tag_aliases", "tag_messages", "tag_conversations"):
                    self.db.execute(f"DELETE FROM {table}")
            self.store._meta("tagSource", source)
            generation = self.store.meta("tagGeneration", 1)
            for row in page["conversations"]:
                self.db.execute("INSERT INTO tag_conversations(user,name,is_group,generation) VALUES(?,?,?,?) "
                                "ON CONFLICT(user) DO UPDATE SET name=excluded.name,is_group=excluded.is_group,"
                                "generation=excluded.generation",
                                (row["user"], row.get("name") or "",
                                 int(bool(row.get("isGroup")) or row["user"].endswith("@chatroom")), generation))
            if page.get("next") is None and (page.get("coverage") or {}).get("state", "complete") == "complete":
                for row in self.rows("SELECT user FROM tag_conversations WHERE generation<>?", (generation,)):
                    self._clear(row["user"])
                    self.db.execute("DELETE FROM tag_conversations WHERE user=?", (row["user"],))

    def _clear(self, user):
        self.db.execute("DELETE FROM tag_mentions WHERE message_id IN (SELECT id FROM tag_messages WHERE user=?)", (user,))
        self.db.execute("DELETE FROM tag_messages WHERE user=?", (user,))

    def pending_conversation(self):
        rows = self.rows("SELECT * FROM tag_conversations WHERE state='pending' ORDER BY user LIMIT 1")
        return rows[0] if rows else None

    def bind(self, user, epoch):
        """A rebuilt conversation renumbers its messages, so its old candidates are read again."""
        with self.lock, self.db:
            row = self.rows("SELECT * FROM tag_conversations WHERE user=?", (user,))[0]
            if row["epoch"] != epoch:
                self._clear(user)
                self.db.execute("UPDATE tag_conversations SET epoch=?,after_seq=0,carry='[]' WHERE user=?", (epoch, user))
            return self.rows("SELECT * FROM tag_conversations WHERE user=?", (user,))[0]

    def page(self, user, found, carry, after, expected_after, context_lines=()):
        with self.lock, self.db:
            current = self.rows("SELECT after_seq FROM tag_conversations WHERE user=?", (user,))
            if not current or current[0]["after_seq"] != expected_after:
                raise SelfError("conflict", "读取进度已变化")
            self._extend_followups(user, context_lines)
            self.db.executemany("INSERT OR IGNORE INTO tag_messages(id,user,seq,time_ms,text,context,followup,truncated,"
                                "followup_count,followup_seq) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                [(row["id"], user, row["seq"], row["time_ms"], row["text"], row["context"],
                                  row.get("followup", ""), int(bool(row.get("truncated"))), row.get("followup_count", 0),
                                  row.get("followup_seq", row["seq"])) for row in found])
            self.db.execute("UPDATE tag_conversations SET after_seq=?,carry=? WHERE user=?",
                            (after, json.dumps(carry, ensure_ascii=False), user))

    def _extend_followups(self, user, lines):
        """New context invalidates earlier conclusions before they can be shown again."""
        if not lines:
            return
        earlier = self.rows("SELECT id,seq,time_ms,followup,followup_count,followup_seq FROM tag_messages "
                            "WHERE user=? AND followup_count<2 AND seq<? AND time_ms BETWEEN ? AND ?",
                            (user, lines[-1]["seq"], min(line["time"] for line in lines) - CONTEXT_GAP_MS,
                             max(line["time"] for line in lines)))
        for row in earlier:
            additions = []
            for line in lines:
                if line["seq"] <= max(row["seq"], row["followup_seq"]):
                    continue
                if not 0 <= line["time"] - row["time_ms"] <= CONTEXT_GAP_MS:
                    break
                additions.append(line)
                if len(additions) == FOLLOWUP_ROWS - row["followup_count"]:
                    break
            if not additions:
                continue
            followup = "\n".join(filter(None, [row["followup"], *(line["line"] for line in additions)]))
            self.db.execute("UPDATE tag_messages SET followup=?,followup_count=?,followup_seq=?,state='pending' WHERE id=?",
                            (followup, row["followup_count"] + len(additions), additions[-1]["seq"], row["id"]))
            self.db.execute("DELETE FROM tag_mentions WHERE message_id=?", (row["id"],))

    def finish_conversation(self, user, state="done"):
        with self.lock, self.db:
            self.db.execute("UPDATE tag_conversations SET state=? WHERE user=?", (state, user))

    # Extraction -------------------------------------------------------------------------------------------------------
    def pending_messages(self, limit):
        return self.rows("SELECT t.*,c.name,c.is_group FROM tag_messages t JOIN tag_conversations c ON c.user=t.user "
                         "WHERE t.state='pending' ORDER BY t.user,t.seq LIMIT ?", (limit,))

    def save_mentions(self, ids, mentions):
        with self.lock, self.db:
            self.db.executemany("INSERT OR REPLACE INTO tag_mentions(message_id,key,name,category,explicit,relation,quote,reason) "
                                "VALUES(?,?,?,?,?,?,?,?)",
                                [(message, normalize(tag["name"]), tag["name"], tag["category"],
                                  int(tag["relation"] in ("interest", "trait")), tag["relation"], tag["quote"], tag["reason"])
                                 for message, tag in mentions])
            self.db.executemany("UPDATE tag_messages SET state='done' WHERE id=?", [(identifier,) for identifier in ids])

    def _effective(self):
        """``{mention key: current tag name}`` after merges."""
        aliases = {row["key"]: row["name"] for row in self.rows("SELECT key,name FROM tag_aliases")}
        return {row["key"]: aliases.get(row["key"], row["name"])
                for row in self.rows("SELECT key,min(name) name FROM tag_mentions GROUP BY key")}

    def known_names(self, limit):
        """Most mentioned tags first, so the model can reuse their names."""
        effective = self._effective()
        counts = Counter()
        for row in self.rows("SELECT key,count(*) n FROM tag_mentions GROUP BY key"):
            counts[effective[row["key"]]] += row["n"]
        deleted = {row["key"] for row in self.rows("SELECT key FROM tag_edits WHERE state='deleted'")}
        return [name for name, _ in counts.most_common() if normalize(name) not in deleted][:limit]

    def merge_candidates(self):
        """Only compare synonyms with the same category and semantic relation."""
        edits = {row["key"]: row["state"] for row in self.rows("SELECT key,state FROM tag_edits")}
        groups = {}
        for tag in self._aggregate().values():
            if edits.get(tag["key"]) != "deleted":
                groups.setdefault((tag["category"], tag["relation"]), []).append({"name": tag["name"], "count": tag["mentions"],
                                                               "relation": tag["relation"],
                                                               "edited": tag["key"] in edits})
        return groups

    def merge(self, into, names):
        """Recheck meaning and manual choices when the model's answer is committed."""
        with self.lock, self.db:
            found = self._aggregate()
            edits = {row["key"]: row["state"] for row in self.rows("SELECT key,state FROM tag_edits")}
            target_key = normalize(into)
            target = found.get(target_key)
            if target is None or edits.get(target_key) == "deleted":
                return
            negative = bool(NEGATIVE_NAME.match(target["name"]))
            targets = {normalize(name) for name in names if normalize(name) != target_key}
            targets = {key for key in targets if key in found and key not in edits and
                       (found[key]["category"], found[key]["relation"]) == (target["category"], target["relation"]) and
                       bool(NEGATIVE_NAME.match(found[key]["name"])) == negative}
            self.db.executemany("INSERT OR REPLACE INTO tag_aliases(key,name) VALUES(?,?)",
                                [(key, target["name"]) for key, name in self._effective().items()
                                 if normalize(name) in targets and key not in edits])

    # Reading tags -----------------------------------------------------------------------------------------------------
    def _aggregate(self):
        effective, tags = self._effective(), {}
        for row in self.rows("SELECT m.key,m.category,m.relation,m.quote,m.reason,t.id,t.text,t.truncated,t.user,t.time_ms FROM tag_mentions m "
                             "JOIN tag_messages t ON t.id=m.message_id"):
            name = effective[row["key"]]
            tag = tags.setdefault(normalize(name), {"key": normalize(name), "name": name, "categories": Counter(),
                                                     "messages": set(), "users": set(), "days": set(), "relations": set(),
                                                     "firstMs": row["time_ms"], "lastMs": row["time_ms"]})
            tag["categories"][row["category"]] += 1
            tag["messages"].add(row["id"])
            tag["users"].add(row["user"])
            tag["days"].add(day(row["time_ms"]))
            anchored = bool(row["quote"] and row["quote"] in row["text"] and row["reason"] and not row["truncated"])
            tag["relations"].add(row["relation"] if anchored and row["relation"] in RELATIONS else "uncertain")
            tag["firstMs"], tag["lastMs"] = min(tag["firstMs"], row["time_ms"]), max(tag["lastMs"], row["time_ms"])
        for tag in tags.values():
            tag["category"] = tag.pop("categories").most_common(1)[0][0]
            tag["mentions"] = len(tag.pop("messages"))
            tag["relation"] = max(tag.pop("relations"), key=RELATION_ORDER.get)
        return tags

    def tags(self):
        found, result = self._aggregate(), []
        edits = {row["key"]: row for row in self.rows("SELECT * FROM tag_edits")}
        for key in found.keys() | edits.keys():
            tag, edit = found.get(key), edits.get(key)
            if edit is not None and edit["state"] == "deleted":
                continue
            if tag is None:
                tag = {"key": key, "name": edit["name"], "mentions": 0, "users": (), "days": (), "relation": "uncertain",
                       "firstMs": None, "lastMs": None}
            # Counts describe coverage, never interest. Only contextual, anchored
            # self-evidence that survived model review can raise an automatic tag.
            strong = tag["relation"] in ("interest", "trait")
            result.append({"key": key, "name": edit["name"] if edit else tag["name"],
                           "category": edit["category"] if edit else tag["category"],
                           "level": "confirmed" if edit else "strong" if strong else "possible",
                            "relation": tag["relation"],
                           "manual": tag["mentions"] == 0, "mentions": tag["mentions"], "chats": len(tag["users"]),
                           "days": len(tag["days"]), "firstMs": tag["firstMs"], "lastMs": tag["lastMs"]})
        order = {name: index for index, name in enumerate(CATEGORIES)}
        return sorted(result, key=lambda tag: (order.get(tag["category"], len(order)), LEVELS[tag["level"]],
                                               -tag["mentions"], tag["name"]))

    def evidence(self, name):
        key = normalize(name)
        keys = [mention for mention, current in self._effective().items() if normalize(current) == key]
        if not keys:
            return {"name": name, "items": [], "total": 0}
        marks = ",".join("?" for _ in keys)
        total = self.rows(f"SELECT count(DISTINCT message_id) n FROM tag_mentions WHERE key IN ({marks})", keys)[0]["n"]
        recent = self.rows("SELECT t.id FROM tag_mentions m JOIN tag_messages t ON t.id=m.message_id "
                           f"WHERE m.key IN ({marks}) GROUP BY t.id ORDER BY t.time_ms DESC,t.id LIMIT ?",
                           (*keys, EVIDENCE_LIMIT))
        identifiers = [row["id"] for row in recent]
        if not identifiers:
            return {"name": name, "items": [], "total": total}
        id_marks = ",".join("?" for _ in identifiers)
        rows = self.rows("SELECT t.id,t.time_ms,t.text,t.context,t.followup,t.truncated,c.name,c.is_group,"
                         "m.relation,m.quote,m.reason "
                         "FROM tag_mentions m JOIN tag_messages t ON t.id=m.message_id "
                         f"JOIN tag_conversations c ON c.user=t.user WHERE m.key IN ({marks}) AND t.id IN ({id_marks}) "
                         "ORDER BY t.time_ms DESC,t.id,m.key", (*keys, *identifiers))
        selected = {}
        for row in rows:
            previous = selected.get(row["id"])
            if previous is None or RELATION_ORDER.get(row["relation"], 0) > RELATION_ORDER.get(previous["relation"], 0):
                selected[row["id"]] = row
        return {"name": name, "total": total, "items": [
            {"id": row["id"], "chat": row["name"], "isGroup": bool(row["is_group"]), "timeMs": row["time_ms"],
             "context": row["context"], "followup": row["followup"], "truncated": bool(row["truncated"]),
             "text": row["text"], "relation": row["relation"], "quote": row["quote"], "reason": row["reason"],
             "explicit": row["relation"] in ("interest", "trait")} for row in selected.values()]}

    def edit(self, action, name, category=None):
        name = clean_name(name)
        if not name or action not in ("confirm", "delete", "reset", "add"):
            raise SelfError("invalid-request", f"标签须为 1 到 {NAME_CHARS} 个字")
        key = normalize(name)
        with self.lock, self.db:
            if action == "reset":
                self.db.execute("DELETE FROM tag_edits WHERE key=?", (key,))
                return
            current = self._aggregate().get(key)
            if action == "add":
                if category not in CATEGORIES:
                    raise SelfError("invalid-request", "请选择标签类别")
            else:
                category = (current or {}).get("category") or (self.rows("SELECT category FROM tag_edits WHERE key=?", (key,)) or
                                                               [{"category": "其他"}])[0]["category"]
            state = "deleted" if action == "delete" else "confirmed"
            self.db.execute("INSERT OR REPLACE INTO tag_edits(key,name,category,state,updated_ms) VALUES(?,?,?,?,?)",
                            (key, name if action == "add" or current is None else current["name"], category, state, now_ms()))

    def counts(self):
        conversations = self.rows("SELECT count(*) total,sum(state='done') done,sum(state='failed') failed,"
                                  "sum(is_group) groups FROM tag_conversations")[0]
        messages = self.rows("SELECT count(*) total,sum(state='pending') pending FROM tag_messages")[0]
        return {"conversations": conversations["total"], "readConversations": conversations["done"] or 0,
                "failedConversations": conversations["failed"] or 0, "groups": conversations["groups"] or 0,
                "messages": messages["total"], "pendingMessages": messages["pending"] or 0}
