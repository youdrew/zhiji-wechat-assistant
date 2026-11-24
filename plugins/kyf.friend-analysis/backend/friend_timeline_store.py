"""好友时间线 storage: organized events, the user's and the Agent's edits, custom tracks and undo.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Adapted from kyf.self-analysis ``self_timeline_store`` for one friend (plugins cannot import each
other): every table carries ``user``; the built-in track "我们" is not stored unless its colour or
visibility changed; edits are anchored to evidence message ids; undo keeps one stack per source
(user / agent) through ``kyf_plugin.timeline.UndoJournal``. Periods are built from the events on
every read, so an edit shows in them at once.
"""
from __future__ import annotations

import json

from kyf_plugin.timeline import (COLORS, EDITABLE_EVENT_FIELDS, EDITABLE_PERIOD_FIELDS, UndoJournal, apply_edits,
                                 build_periods)

from analysis_store import now_ms
from contracts import AdvisorError

TRACK = "我们"
RESERVED = (TRACK, "全部")
CATEGORIES = ("见面聚会", "一起出行", "一起做的事", "同学共事", "互相帮忙", "礼物纪念", "情感", "其他")
NAME_CHARS = 8
DESC_CHARS = 60
TRACK_LIMIT = 8
UNDO_LIMIT = 200
PERIOD_GROUP = ("track", "category")
EVENT_FIELDS = (*EDITABLE_EVENT_FIELDS, "category", "note")
PERIOD_FIELDS = (*EDITABLE_PERIOD_FIELDS, "category")

SCHEMA = """
CREATE TABLE IF NOT EXISTS friend_timeline_candidates (
    user TEXT NOT NULL,
    generation INTEGER NOT NULL,
    id TEXT NOT NULL,
    msg_id TEXT NOT NULL,
    time_ms INTEGER,
    hashes TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY(user, generation, id)
);
CREATE TABLE IF NOT EXISTS friend_timeline_events (
    user TEXT NOT NULL,
    id TEXT NOT NULL,
    start_ms INTEGER,
    payload TEXT NOT NULL,
    PRIMARY KEY(user, id)
);
CREATE TABLE IF NOT EXISTS friend_timeline_edits (
    user TEXT NOT NULL,
    id TEXT NOT NULL,
    target TEXT NOT NULL,
    anchors TEXT NOT NULL DEFAULT '[]',
    track TEXT,
    state TEXT NOT NULL,
    fields TEXT NOT NULL DEFAULT '{}',
    updated_ms INTEGER NOT NULL DEFAULT 0,
    last_seq INTEGER,
    PRIMARY KEY(user, id)
);
CREATE TABLE IF NOT EXISTS friend_timeline_deleted (
    user TEXT NOT NULL,
    event_id TEXT NOT NULL,
    anchors TEXT NOT NULL,
    track TEXT,
    updated_ms INTEGER NOT NULL,
    last_seq INTEGER,
    PRIMARY KEY(user, event_id)
);
CREATE TABLE IF NOT EXISTS friend_timeline_tracks (
    user TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    visible INTEGER NOT NULL DEFAULT 1,
    builtin INTEGER NOT NULL DEFAULT 0,
    position INTEGER NOT NULL DEFAULT 0,
    updated_ms INTEGER NOT NULL DEFAULT 0,
    last_seq INTEGER,
    PRIMARY KEY(user, name)
);
CREATE TABLE IF NOT EXISTS friend_timeline_undo (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    actor TEXT NOT NULL,
    agent TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL,
    changes TEXT NOT NULL,
    updated_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS friend_timeline_undo_user ON friend_timeline_undo(user, actor, seq);
CREATE TABLE IF NOT EXISTS friend_timeline_meta (
    user TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY(user, key)
);
"""
# User edits live only in these tables; the journal records their rows before each change.
TRACKED = {"friend_timeline_edits": "id", "friend_timeline_deleted": "event_id", "friend_timeline_tracks": "name"}
USER_TABLES = ("friend_timeline_events", *TRACKED, "friend_timeline_undo", "friend_timeline_meta")
EDIT_ACTIONS = ("confirm", "unconfirm", "edit", "delete", "restore", "add", "unlink")


def ensure_schema(connection):
    connection.executescript(SCHEMA)


def _error(code, message, detail=None):
    return AdvisorError(code, message)


class TimelineData:
    """One friend's timeline rows; the store interface ``kyf_plugin.timeline.apply_edit`` expects.

    Writes execute on the account connection without committing: callers wrap a whole edit in
    ``with data.db`` under ``data.lock``."""

    def __init__(self, database, user):
        self.database, self.user = database, user
        self.lock, self.db = database.lock, database._connect()

    def rows(self, sql, values=()):
        return self.database._rows(sql, values)

    # -- version ------------------------------------------------------------------------------------
    def meta(self, key, default=None):
        row = self.database._row("SELECT value FROM friend_timeline_meta WHERE user=? AND key=?", (self.user, key))
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO friend_timeline_meta(user,key,value) VALUES(?,?,?)",
                        (self.user, key, json.dumps(value, ensure_ascii=False)))

    def version(self):
        return self.meta("version", 0)

    def bump(self):
        self.set_meta("version", self.version() + 1)

    # -- organized events ---------------------------------------------------------------------------
    def events(self):
        return [json.loads(row["payload"]) for row in
                self.rows("SELECT payload FROM friend_timeline_events WHERE user=? ORDER BY start_ms IS NULL,"
                          "start_ms DESC,id DESC", (self.user,))]

    def event(self, identifier):
        row = self.database._row("SELECT payload FROM friend_timeline_events WHERE user=? AND id=?",
                                 (self.user, identifier))
        return json.loads(row["payload"]) if row else None

    def replace_events(self, connection, events):
        connection.execute("DELETE FROM friend_timeline_events WHERE user=?", (self.user,))
        connection.executemany("INSERT INTO friend_timeline_events(user,id,start_ms,payload) VALUES(?,?,?,?)",
                               [(self.user, event["id"], event.get("startMs"), json.dumps(event, ensure_ascii=False))
                                for event in events])

    def visible_events(self):
        """``[(event, edit)]``: the user's words win, deleted ones are hidden, unknown tracks fall back to 我们."""
        names = set(self.track_names())
        result = []
        for event, edit in apply_edits(self.events(), self.edits("event"), self.deleted_sets(), match_track=False,
                                       editable=EVENT_FIELDS):
            if event.get("track") not in names:
                event = {**event, "track": TRACK}
            if event.get("category") not in CATEGORIES:
                event = {**event, "category": "其他"}
            result.append((event, edit))
        return result

    def periods(self):
        """Periods built from the visible events per (track, category); no model call."""
        events = [event for event, _edit in self.visible_events() if event.get("status") != "cancelled"]
        return build_periods(events, group_fields=PERIOD_GROUP)

    # -- edits ---------------------------------------------------------------------------------------
    def edits(self, target=None):
        sql = "SELECT * FROM friend_timeline_edits WHERE user=?" + (" AND target=?" if target else "") + \
            " ORDER BY updated_ms,id"
        return [{**row, "anchors": json.loads(row["anchors"]), "fields": json.loads(row["fields"])}
                for row in self.rows(sql, (self.user, target) if target else (self.user,))]

    def _edit_row(self, identifier, target):
        rows = self.rows("SELECT * FROM friend_timeline_edits WHERE user=? AND id=? AND target=?",
                         (self.user, identifier, target))
        return rows[0] if rows else None

    def _put(self, identifier, target, anchors, track, state, fields):
        self.db.execute("INSERT OR REPLACE INTO friend_timeline_edits(user,id,target,anchors,track,state,fields,"
                        "updated_ms,last_seq) VALUES(?,?,?,?,?,?,?,?,(SELECT last_seq FROM friend_timeline_edits "
                        "WHERE user=? AND id=?))",
                        (self.user, identifier, target, json.dumps(sorted(set(anchors))), track, state,
                         json.dumps(fields or {}, ensure_ascii=False), now_ms(), self.user, identifier))

    def add_edit(self, target, action, identifier, anchors=(), fields=None, track=None):
        if target not in ("event", "period", "memory") or action not in EDIT_ACTIONS:
            raise AdvisorError("invalid-request", "时间线修改不正确")
        row = self._edit_row(identifier, target)
        if action in ("confirm", "unconfirm") and row is not None and row["state"] == "manual":
            return  # a manual item counts as the user's own already
        if action == "unconfirm":
            if row and json.loads(row["fields"]):
                self.db.execute("UPDATE friend_timeline_edits SET state='edited',updated_ms=? WHERE user=? AND id=?",
                                (now_ms(), self.user, identifier))
            else:
                self.db.execute("DELETE FROM friend_timeline_edits WHERE user=? AND id=? AND target=?",
                                (self.user, identifier, target))
        elif action == "confirm":
            if row:
                self.db.execute("UPDATE friend_timeline_edits SET state='confirmed',updated_ms=? WHERE user=? AND id=?",
                                (now_ms(), self.user, identifier))
            else:
                self._put(identifier, target, anchors, track, "confirmed", fields)
        elif action in ("restore", "delete") and target == "event":
            self.db.execute("DELETE FROM friend_timeline_edits WHERE user=? AND id=? AND target=?",
                            (self.user, identifier, target))
        elif action == "restore":
            self.db.execute("DELETE FROM friend_timeline_edits WHERE user=? AND id=? AND target=?",
                            (self.user, identifier, target))
        elif action == "delete":
            if row is not None and row["state"] == "manual":
                self.db.execute("DELETE FROM friend_timeline_edits WHERE user=? AND id=?", (self.user, identifier))
            else:
                self._put(identifier, target, anchors, track, "deleted", {})
        elif action == "unlink":
            self._put(identifier, target, anchors, track, "unlink", fields)
        else:
            self._put(identifier, target, anchors, track, "manual" if action == "add" else "edited", fields)

    def deleted_sets(self):
        return [{**row, "anchors": set(json.loads(row["anchors"]))} for row in
                self.rows("SELECT * FROM friend_timeline_deleted WHERE user=? ORDER BY updated_ms,event_id",
                          (self.user,))]

    def add_deleted(self, event_id, anchors, track=None):
        self.db.execute("INSERT OR REPLACE INTO friend_timeline_deleted(user,event_id,anchors,track,updated_ms,last_seq) "
                        "VALUES(?,?,?,?,?,(SELECT last_seq FROM friend_timeline_deleted WHERE user=? AND event_id=?))",
                        (self.user, event_id, json.dumps(sorted(set(anchors))), track, now_ms(), self.user, event_id))

    def drop_deleted(self, event_id):
        self.db.execute("DELETE FROM friend_timeline_deleted WHERE user=? AND event_id=?", (self.user, event_id))

    def unlinked(self):
        """``[(memory id, anchor set)]`` the user detached from automatic linking."""
        return [(row["fields"].get("memoryId"), set(row["anchors"])) for row in self.edits("memory")
                if row["state"] == "unlink"]

    # -- tracks ---------------------------------------------------------------------------------------
    def tracks(self):
        stored = {row["name"]: row for row in
                  self.rows("SELECT * FROM friend_timeline_tracks WHERE user=? ORDER BY position,name", (self.user,))}
        own = stored.pop(TRACK, None)
        result = [{"name": TRACK, "builtin": True, "color": (own or {}).get("color") or COLORS[0], "description": "",
                   "visible": bool(own["visible"]) if own else True}]
        result += [{"name": row["name"], "builtin": False, "color": row["color"], "description": row["description"],
                    "visible": bool(row["visible"])} for row in stored.values()]
        return result

    def track_names(self, custom_only=False):
        names = [row["name"] for row in self.tracks()]
        return names[1:] if custom_only else names

    def set_track(self, name, description=None, color=None, visible=None):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > NAME_CHARS:
            raise AdvisorError("invalid-request", "主线名称须为 1 到 %d 个字" % NAME_CHARS)
        name = name.strip()
        if color is not None and color not in COLORS:
            raise AdvisorError("invalid-request", "请选择主线颜色")
        if description is not None and len(description) > DESC_CHARS:
            raise AdvisorError("invalid-request", "主线说明最多 %d 字" % DESC_CHARS)
        rows = self.rows("SELECT * FROM friend_timeline_tracks WHERE user=? AND name=?", (self.user, name))
        stamp = now_ms()
        if name == TRACK:
            if description:
                raise AdvisorError("invalid-request", "“我们”不能改说明")
            current = self.tracks()[0]
            self.db.execute("INSERT OR REPLACE INTO friend_timeline_tracks(user,name,description,color,visible,builtin,"
                            "position,updated_ms,last_seq) VALUES(?,?,'',?,?,1,0,?,(SELECT last_seq FROM "
                            "friend_timeline_tracks WHERE user=? AND name=?))",
                            (self.user, TRACK, color or current["color"],
                             int(current["visible"] if visible is None else bool(visible)), stamp, self.user, TRACK))
            return
        if name in RESERVED:
            raise AdvisorError("invalid-request", "主线不能叫“%s”" % name)
        if not rows:
            if color is None:
                raise AdvisorError("invalid-request", "请选择主线颜色")
            if len(self.track_names(custom_only=True)) >= TRACK_LIMIT:
                raise AdvisorError("invalid-request", "每位好友最多 %d 条自定义主线" % TRACK_LIMIT)
            position = self.database._row("SELECT coalesce(max(position),0)+1 AS n FROM friend_timeline_tracks "
                                          "WHERE user=?", (self.user,))["n"]
            self.db.execute("INSERT INTO friend_timeline_tracks(user,name,description,color,visible,builtin,position,"
                            "updated_ms) VALUES(?,?,?,?,1,0,?,?)",
                            (self.user, name, description or "", color, position, stamp))
            return
        updates, values = ["updated_ms=?"], [stamp]
        for column, value in (("color", color), ("description", description)):
            if value is not None:
                updates.append(column + "=?")
                values.append(value)
        if visible is not None:
            updates.append("visible=?")
            values.append(int(bool(visible)))
        self.db.execute("UPDATE friend_timeline_tracks SET " + ",".join(updates) + " WHERE user=? AND name=?",
                        (*values, self.user, name))

    def delete_track(self, name):
        if not isinstance(name, str) or not name.strip():
            raise AdvisorError("invalid-request", "主线不正确")
        name = name.strip()
        if name == TRACK:
            raise AdvisorError("invalid-request", "“我们”不能删除")
        if not self.rows("SELECT 1 FROM friend_timeline_tracks WHERE user=? AND name=?", (self.user, name)):
            return
        self.db.execute("DELETE FROM friend_timeline_tracks WHERE user=? AND name=?", (self.user, name))
        # Whatever was on the track, by the model or moved by hand, goes back to 我们.
        for row in self.edits():
            if row["fields"].get("track") == name or row["track"] == name:
                fields = {**row["fields"], **({"track": TRACK} if row["fields"].get("track") == name else {})}
                self.db.execute("UPDATE friend_timeline_edits SET fields=?,track=?,updated_ms=? WHERE user=? AND id=?",
                                (json.dumps(fields, ensure_ascii=False), TRACK if row["track"] == name else row["track"],
                                 now_ms(), self.user, row["id"]))

    # -- undo -------------------------------------------------------------------------------------------
    def journal(self):
        return UndoJournal(self.db, self.lock, "friend_timeline_undo", TRACKED, scope={"user": self.user},
                           actors=True, seq_column="last_seq", on_change=self.bump)

    def agent_edits(self):
        """``{seq: updated_ms}`` of the Agent's undo entries, to mark rows it changed last."""
        return {row["seq"]: row["updated_ms"] for row in
                self.rows("SELECT seq,updated_ms FROM friend_timeline_undo WHERE user=? AND actor='agent'",
                          (self.user,))}
