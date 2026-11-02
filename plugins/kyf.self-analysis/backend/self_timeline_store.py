"""Account-local storage for 自我剖析·时间线: events, periods, tracks and the user's edits.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Timeline shares the account database but not its tables, like the tag store: it
reads every chat through the shared snapshot, and clearing the reply library
leaves it alone. Automatic events are rebuilt by each organize pass; the user's
edits are anchored to evidence message-id sets (doc 4.2), so a re-analysis that
finds the same evidence keeps the user's words.
"""
import json
import threading

from self_contracts import SelfError, now_ms

# 内置 10 类主线；提取提示词依赖这份目录，内置主线不能删除或改名，但可以隐藏。
BUILTIN_TRACKS = ("学业", "工作事业", "居住迁移", "感情婚恋", "家庭亲人", "健康", "旅行出行", "兴趣成就", "重大财务", "其他")
OVERVIEW_TRACKS = BUILTIN_TRACKS[:6]
COLORS = ("#07c160", "#576b95", "#c2841c", "#b05a4a", "#7f77dd", "#3d8f8a", "#9a6fb0", "#7b8a4e")
STATUSES = ("occurred", "planned", "cancelled", "uncertain")
KINDS = ("point", "start", "end", "state")
IMPORTANCE = ("important", "general")
PRECISIONS = ("day", "month", "season", "year", "about", "unknown")
NAME_CHARS = 8
DESC_CHARS = 60
VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS timeline_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS timeline_cache(fingerprint TEXT PRIMARY KEY,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS timeline_active(
 position INTEGER PRIMARY KEY,message_id TEXT NOT NULL,fingerprint TEXT NOT NULL,item TEXT NOT NULL,
 done INTEGER NOT NULL,reused INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS timeline_pending ON timeline_active(done,position);
CREATE INDEX IF NOT EXISTS timeline_message ON timeline_active(message_id,done);
CREATE TABLE IF NOT EXISTS timeline_staging(
 position INTEGER PRIMARY KEY,message_id TEXT NOT NULL,fingerprint TEXT NOT NULL,item TEXT NOT NULL,
 done INTEGER NOT NULL,reused INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS timeline_events(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,track TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'point',
 status TEXT NOT NULL DEFAULT 'occurred',importance TEXT NOT NULL DEFAULT 'general',
 start_ms INTEGER,end_ms INTEGER,precision TEXT NOT NULL DEFAULT 'unknown',phrase TEXT NOT NULL DEFAULT '',
 said_ms INTEGER,level TEXT NOT NULL DEFAULT 'possible',place TEXT NOT NULL DEFAULT '',
 people TEXT NOT NULL DEFAULT '',reason TEXT NOT NULL DEFAULT '',evidence TEXT NOT NULL DEFAULT '[]',
 conflicts TEXT NOT NULL DEFAULT '[]',details TEXT NOT NULL DEFAULT '[]',updated_ms INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS timeline_periods(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,track TEXT NOT NULL,start_ms INTEGER,end_ms INTEGER,
 precision TEXT NOT NULL DEFAULT 'unknown',ongoing INTEGER NOT NULL DEFAULT 0,last_ms INTEGER,
 evidence TEXT NOT NULL DEFAULT '[]',updated_ms INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS timeline_edits(
 id TEXT PRIMARY KEY,target TEXT NOT NULL,anchors TEXT NOT NULL DEFAULT '[]',track TEXT,
 state TEXT NOT NULL,fields TEXT NOT NULL DEFAULT '{}',updated_ms INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS timeline_tracks(
 name TEXT PRIMARY KEY,builtin INTEGER NOT NULL DEFAULT 0,position INTEGER NOT NULL DEFAULT 0,
 color TEXT NOT NULL DEFAULT '',description TEXT NOT NULL DEFAULT '',
 visible INTEGER NOT NULL DEFAULT 1,updated_ms INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS timeline_deleted(
 event_id TEXT PRIMARY KEY,anchors TEXT NOT NULL,track TEXT,updated_ms INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS timeline_undo(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,label TEXT NOT NULL,changes TEXT NOT NULL,updated_ms INTEGER NOT NULL);
"""

EDIT_ACTIONS = ("confirm", "unconfirm", "edit", "delete", "restore", "add", "merge", "detach", "visible")
# 用户修改只落在这三张表；撤销日志按主键记录它们被改动前的整行。
UNDO_TABLES = {"timeline_edits": "id", "timeline_deleted": "event_id", "timeline_tracks": "name"}
UNDO_LIMIT = 200


class TimelineStore:
    def __init__(self, store):
        self.store, self.lock, self.db = store, store.lock, store.db
        with self.lock, self.db:
            self.db.executescript(SCHEMA)
            if "details" not in {row["name"] for row in self.rows("PRAGMA table_info(timeline_events)")}:
                self.db.execute("ALTER TABLE timeline_events ADD COLUMN details TEXT NOT NULL DEFAULT '[]'")
            for index, name in enumerate(BUILTIN_TRACKS):
                self.db.execute("INSERT OR IGNORE INTO timeline_tracks(name,builtin,position,color,visible,updated_ms) "
                                "VALUES(?,1,?,?,1,?)", (name, index, COLORS[index % len(COLORS)], now_ms()))
        if self.run().get("state") == "running":
            self.update_run(state="paused", error="上次运行已中断，继续会从已保存的位置恢复")

    def rows(self, query, values=()):
        return self.store.rows(query, values)

    def meta(self, key, default=None):
        rows = self.rows("SELECT value FROM timeline_meta WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def _meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO timeline_meta VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def version(self):
        return self.meta("version", 0)

    def bump(self):
        with self.lock, self.db:
            self._meta("version", self.meta("version", 0) + 1)

    def run(self):
        return self.meta("run", {"state": "idle"})

    def update_run(self, **changes):
        with self.lock, self.db:
            run = {**self.run(), **changes, "updatedAt": now_ms()}
            self._meta("run", run)
            return run

    # Extraction cache and checkpoint -------------------------------------------------------------------------------
    def cache_get(self, fingerprint):
        rows = self.rows("SELECT payload FROM timeline_cache WHERE fingerprint=?", (fingerprint,))
        return json.loads(rows[0]["payload"]) if rows else None

    def cache_put(self, fingerprint, payload):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO timeline_cache VALUES(?,?)", (fingerprint, json.dumps(payload, ensure_ascii=False)))

    def stage(self, rows):
        """Queue one window per row, reusing the extraction cache like the shared evidence stage."""
        with self.lock, self.db:
            self.db.executemany("INSERT INTO timeline_staging VALUES(?,?,?,?,"
                                "EXISTS(SELECT 1 FROM timeline_cache WHERE fingerprint=?),"
                                "EXISTS(SELECT 1 FROM timeline_cache WHERE fingerprint=?))",
                                [(position, item["id"], fingerprint, json.dumps(item, ensure_ascii=False), fingerprint, fingerprint)
                                 for position, fingerprint, item in rows])

    def activate(self):
        with self.lock, self.db:
            self.db.execute("INSERT INTO timeline_active SELECT * FROM timeline_staging ORDER BY position")
            self.db.execute("DELETE FROM timeline_staging")
            # Publish the marker in the same transaction as the staged rows. A
            # crash must never leave an empty active snapshot marked prepared.
            self._meta("prepared", True)

    def reset_active(self):
        with self.lock, self.db:
            self.db.execute("DELETE FROM timeline_active")
            self.db.execute("DELETE FROM timeline_staging")

    def pending(self, limit=96, exclude=()):
        parts = ",".join("?" for _ in exclude)
        sql = ("SELECT position,fingerprint,item FROM timeline_active WHERE done=0"
               + (f" AND position NOT IN ({parts})" if parts else "") + " ORDER BY position LIMIT ?")
        return [{**row, "item": json.loads(row["item"])} for row in self.rows(sql, (*exclude, limit))]

    def save(self, rows, payloads):
        """Mark windows done and cache their parsed payloads."""
        with self.lock, self.db:
            for row, payload in zip(rows, payloads):
                self.db.execute("INSERT OR REPLACE INTO timeline_cache VALUES(?,?)",
                                (row["fingerprint"], json.dumps(payload, ensure_ascii=False)))
                self.db.execute("UPDATE timeline_active SET done=1 WHERE position=? AND fingerprint=? AND done=0",
                                (row["position"], row["fingerprint"]))

    def skip(self, rows):
        """Defer a failed window until the next pass; absence of cache records the failure."""
        with self.lock, self.db:
            for row in rows:
                self.db.execute("UPDATE timeline_active SET done=1 WHERE position=? AND fingerprint=? AND done=0",
                                (row["position"], row["fingerprint"]))

    def recover_failed(self):
        """Retry uncached failures without touching successful or verified empty results."""
        with self.lock, self.db:
            return self.db.execute("UPDATE timeline_active SET done=0,reused=0 WHERE done=1 AND NOT EXISTS "
                                   "(SELECT 1 FROM timeline_cache c WHERE c.fingerprint=timeline_active.fingerprint)").rowcount

    def records(self):
        """Every cached window payload, in snapshot order."""
        after = -1
        while rows := self.rows("SELECT a.position,a.fingerprint,a.item,c.payload FROM timeline_active a "
                                "JOIN timeline_cache c ON c.fingerprint=a.fingerprint WHERE a.done=1 AND a.position>? "
                                "ORDER BY a.position LIMIT 200", (after,)):
            for row in rows:
                yield {"fingerprint": row["fingerprint"], "item": json.loads(row["item"]), **json.loads(row["payload"])}
            after = rows[-1]["position"]

    def active_counts(self):
        return self.rows("SELECT count(*) total,coalesce(sum(done),0) done FROM timeline_active")[0]

    # Automatic results --------------------------------------------------------------------------------------------
    def replace_events(self, events):
        with self.lock, self.db:
            self.db.execute("DELETE FROM timeline_events")
            self.db.executemany("INSERT INTO timeline_events(id,title,track,kind,status,importance,start_ms,end_ms,precision,"
                                "phrase,said_ms,level,place,people,reason,evidence,conflicts,details,updated_ms) "
                                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                [(event["id"], event["title"], event["track"], event.get("kind", "point"),
                                  event.get("status", "occurred"), event.get("importance", "general"),
                                  event.get("startMs"), event.get("endMs"), event.get("precision", "unknown"),
                                  event.get("phrase", ""), event.get("saidMs"), event.get("level", "possible"),
                                  event.get("place", ""), event.get("people", ""), event.get("reason", ""),
                                  json.dumps(event.get("evidence", []), ensure_ascii=False),
                                  json.dumps(event.get("conflicts", []), ensure_ascii=False),
                                  json.dumps(event.get("details", []), ensure_ascii=False), now_ms())
                                 for event in events])

    def replace_periods(self, periods):
        with self.lock, self.db:
            self.db.execute("DELETE FROM timeline_periods")
            self.db.executemany("INSERT INTO timeline_periods(id,title,track,start_ms,end_ms,precision,ongoing,last_ms,"
                                "evidence,updated_ms) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                [(period["id"], period["title"], period["track"], period.get("startMs"), period.get("endMs"),
                                  period.get("precision", "unknown"), int(bool(period.get("ongoing"))), period.get("lastMs"),
                                  json.dumps(period.get("evidence", []), ensure_ascii=False), now_ms())
                                 for period in periods])

    def events(self):
        return [self._event(row) for row in
                self.rows("SELECT * FROM timeline_events ORDER BY start_ms IS NULL,start_ms DESC,id DESC")]

    def periods(self):
        return [self._period(row) for row in self.rows("SELECT * FROM timeline_periods ORDER BY start_ms IS NULL,id")]

    def event(self, identifier):
        rows = self.rows("SELECT * FROM timeline_events WHERE id=?", (identifier,))
        return self._event(rows[0]) if rows else None

    def _event(self, row):
        return {"id": row["id"], "title": row["title"], "track": row["track"], "kind": row["kind"],
                "status": row["status"], "importance": row["importance"], "startMs": row["start_ms"],
                "endMs": row["end_ms"], "precision": row["precision"], "phrase": row["phrase"], "saidMs": row["said_ms"],
                "level": row["level"], "place": row["place"], "people": row["people"], "reason": row["reason"],
                "evidence": json.loads(row["evidence"]), "conflicts": json.loads(row["conflicts"]),
                "details": json.loads(row["details"]) if "details" in row.keys() else []}

    def _period(self, row):
        return {"id": row["id"], "title": row["title"], "track": row["track"], "startMs": row["start_ms"],
                "endMs": row["end_ms"], "precision": row["precision"], "ongoing": bool(row["ongoing"]),
                "lastMs": row["last_ms"], "evidence": json.loads(row["evidence"])}

    # User edits, anchored to evidence message-id sets ----------------------------------------------------------------
    def edits(self, target=None):
        sql = "SELECT * FROM timeline_edits" + (" WHERE target=?" if target else "") + " ORDER BY updated_ms,id"
        values = (target,) if target else ()
        return [{**row, "anchors": json.loads(row["anchors"]), "fields": json.loads(row["fields"])} for row in self.rows(sql, values)]

    def add_edit(self, target, action, identifier, anchors=(), fields=None, track=None):
        if target not in ("event", "period", "track") or action not in EDIT_ACTIONS:
            raise SelfError("invalid-request", "时间线修改不正确")
        with self.lock, self.db:
            if action == "unconfirm":
                # 取消确认不删用户的字段：有字段就降级为 edited，没有才整行移除（doc 4.1/4.2）。
                row = self.rows("SELECT fields FROM timeline_edits WHERE id=? AND target=?", (identifier, target))
                if row and json.loads(row[0]["fields"]):
                    self.db.execute("UPDATE timeline_edits SET state='edited',updated_ms=? WHERE id=? AND target=?",
                                    (now_ms(), identifier, target))
                else:
                    self.db.execute("DELETE FROM timeline_edits WHERE id=? AND target=?", (identifier, target))
            elif action == "confirm":
                # 确认只提升状态，绝不覆盖已有 fields（确认与编辑是两个独立操作）。
                row = self.rows("SELECT fields FROM timeline_edits WHERE id=? AND target=?", (identifier, target))
                if row:
                    self.db.execute("UPDATE timeline_edits SET state='confirmed',updated_ms=? WHERE id=? AND target=?",
                                    (now_ms(), identifier, target))
                else:
                    self.db.execute("INSERT OR REPLACE INTO timeline_edits(id,target,anchors,track,state,fields,updated_ms) "
                                    "VALUES(?,?,?,?,?,?,?)",
                                    (identifier, target, json.dumps(sorted(set(anchors))), track, "confirmed",
                                     json.dumps(fields or {}, ensure_ascii=False), now_ms()))
            elif action == "restore":
                self.db.execute("DELETE FROM timeline_edits WHERE id=? AND target=?", (identifier, target))
                if target == "event":
                    # 恢复事件必须同时清掉删除锚点，否则重建后又被隐藏。
                    self.db.execute("DELETE FROM timeline_deleted WHERE event_id=?", (identifier,))
            elif action == "delete" and target == "period":
                # 时期每次整理都会重建，删除按依据集合锚定，在读取时隐藏。
                self.db.execute("INSERT OR REPLACE INTO timeline_edits(id,target,anchors,track,state,fields,updated_ms) "
                                "VALUES(?,?,?,?,?,?,?)",
                                (identifier, target, json.dumps(sorted(set(anchors))), track, "deleted", "{}", now_ms()))
            elif action == "delete":
                # 真实事件走 timeline_deleted（add_deleted）；这里顺带清掉手动事件的编辑行。
                self.db.execute("DELETE FROM timeline_edits WHERE id=? AND target=?", (identifier, target))
            elif action == "visible":
                self.db.execute("DELETE FROM timeline_edits WHERE id=? AND target='track' AND state='visible'", (identifier,))
            else:
                self.db.execute("INSERT OR REPLACE INTO timeline_edits(id,target,anchors,track,state,fields,updated_ms) "
                                "VALUES(?,?,?,?,?,?,?)",
                                (identifier, target, json.dumps(sorted(set(anchors))), track,
                                 "manual" if action == "add" else "edited" if action == "edit" else "confirmed",
                                 json.dumps(fields or {}, ensure_ascii=False), now_ms()))
            self.bump()

    def deleted_sets(self):
        return [{**row, "anchors": set(json.loads(row["anchors"]))} for row in
                self.rows("SELECT * FROM timeline_deleted ORDER BY updated_ms,event_id")]

    def add_deleted(self, event_id, anchors, track=None):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO timeline_deleted(event_id,anchors,track,updated_ms) VALUES(?,?,?,?)",
                            (event_id, json.dumps(sorted(set(anchors))), track, now_ms()))
            self.bump()

    def drop_deleted(self, event_id):
        with self.lock, self.db:
            self.db.execute("DELETE FROM timeline_deleted WHERE event_id=?", (event_id,))
            self.bump()

    # Undo journal: the page's Ctrl+Z and MCP share it, so it lives with the edits. ------------------------------------
    def user_state(self):
        """Every row a user edit can touch, keyed so ``push_undo`` can diff before and after."""
        return {table: {row[key]: dict(row) for row in self.rows(f"SELECT * FROM {table}")}
                for table, key in UNDO_TABLES.items()}

    def push_undo(self, label, before):
        """Record the before-image of whatever changed since ``before``; a no-op records nothing."""
        after = self.user_state()
        changes = {table: [[key, before[table].get(key)] for key in before[table].keys() | after[table].keys()
                           if before[table].get(key) != after[table].get(key)] for table in UNDO_TABLES}
        if not any(changes.values()):
            return
        with self.lock, self.db:
            self.db.execute("INSERT INTO timeline_undo(label,changes,updated_ms) VALUES(?,?,?)",
                            (label, json.dumps(changes, ensure_ascii=False), now_ms()))
            self.db.execute("DELETE FROM timeline_undo WHERE seq<=(SELECT max(seq) FROM timeline_undo)-?", (UNDO_LIMIT,))

    def undo(self):
        """Restore the newest journal entry and return its label, or None when there is nothing to undo."""
        with self.lock, self.db:
            rows = self.rows("SELECT * FROM timeline_undo ORDER BY seq DESC LIMIT 1")
            if not rows:
                return None
            for table, changes in json.loads(rows[0]["changes"]).items():
                for identifier, row in changes:
                    self.db.execute(f"DELETE FROM {table} WHERE {UNDO_TABLES[table]}=?", (identifier,))
                    if row is not None:
                        marks = ",".join("?" for _ in row)
                        self.db.execute(f"INSERT INTO {table}({','.join(row)}) VALUES({marks})", tuple(row.values()))
            self.db.execute("DELETE FROM timeline_undo WHERE seq=?", (rows[0]["seq"],))
            self.bump()
            return rows[0]["label"]

    def anchor_sets(self):
        """Edit anchors per event id, so re-analysis can re-anchor user decisions."""
        return {row["id"]: set(json.loads(row["anchors"])) for row in
                self.rows("SELECT id,anchors FROM timeline_edits WHERE target='event' AND state IN ('confirmed','edited')")}

    # Tracks ---------------------------------------------------------------------------------------------------------
    def tracks(self):
        return [{"name": row["name"], "builtin": bool(row["builtin"]), "color": row["color"],
                 "description": row["description"], "visible": bool(row["visible"])}
                for row in self.rows("SELECT * FROM timeline_tracks ORDER BY builtin DESC,position,name")]

    def track_names(self, custom_only=False):
        sql = "SELECT name FROM timeline_tracks" + (" WHERE builtin=0" if custom_only else "")
        return [row["name"] for row in self.rows(sql)]

    def set_track(self, name, description=None, color=None, visible=None):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_CHARS:
            raise SelfError("invalid-request", f"主线名称须为 1 到 {NAME_CHARS} 个字")
        name = name.strip()
        with self.lock, self.db:
            row = self.rows("SELECT * FROM timeline_tracks WHERE name=?", (name,))
            if not row:
                if color not in COLORS:
                    raise SelfError("invalid-request", "请选择主线颜色")
                if description is not None and len(description) > DESC_CHARS:
                    raise SelfError("invalid-request", f"主线说明最多 {DESC_CHARS} 字")
                self.db.execute("INSERT INTO timeline_tracks(name,builtin,position,color,description,visible,updated_ms) "
                                "VALUES(?,0,0,?,?,1,?)", (name, color, description or "", now_ms()))
            elif row[0]["builtin"]:
                # 内置主线只允许隐藏；改名与颜色由提取目录固定。
                if description is not None or color is not None:
                    raise SelfError("invalid-request", "内置主线不能改名或改颜色，可以从总览条上隐藏")
                if visible is not None:
                    self.db.execute("UPDATE timeline_tracks SET visible=?,updated_ms=? WHERE name=?",
                                    (int(bool(visible)), now_ms(), name))
            else:
                updates, values = ["updated_ms"], [now_ms()]
                if color is not None:
                    if color not in COLORS:
                        raise SelfError("invalid-request", "请选择主线颜色")
                    updates.append("color"), values.append(color)
                if description is not None:
                    if len(description) > DESC_CHARS:
                        raise SelfError("invalid-request", f"主线说明最多 {DESC_CHARS} 字")
                    updates.append("description"), values.append(description)
                if visible is not None:
                    updates.append("visible"), values.append(int(bool(visible)))
                self.db.execute(f"UPDATE timeline_tracks SET {','.join(u + '=?' for u in updates)} WHERE name=?",
                                (*values, name))
            self.bump()

    def delete_track(self, name):
        if not isinstance(name, str) or not name.strip():
            raise SelfError("invalid-request", "主线不正确")
        with self.lock, self.db:
            row = self.rows("SELECT * FROM timeline_tracks WHERE name=?", (name.strip(),))
            if not row:
                return
            if row[0]["builtin"]:
                raise SelfError("invalid-request", "内置主线不能删除，可以从总览条上隐藏")
            self.db.execute("DELETE FROM timeline_tracks WHERE name=?", (name.strip(),))
            self.bump()
