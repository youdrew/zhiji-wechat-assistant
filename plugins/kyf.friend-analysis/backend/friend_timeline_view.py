"""好友时间线 pages: ``page.timeline`` / ``timelineEvidence`` / ``timelineEdit`` / ``timelineUndo(Agent)``.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The account and friend always come from the call context. The source of a write (user or agent)
is read from the context only (``kyf_plugin.actor``); each source keeps its own undo stack, so the
page's Ctrl+Z never undoes the Agent's edits and the Agent's undo never touches the user's. Rows
that come from 关系记忆 are read-only here; nothing is written to Core.
"""
from __future__ import annotations

import hashlib
import json
import time

import friend_chat
import friend_trends
from contracts import AdvisorError
from friend_extract import MAX_LINE_CHARS
from friend_timeline import (link_memories, memory_ids, memory_time, shared_memories)
from friend_timeline_store import (CATEGORIES, EVENT_FIELDS, PERIOD_FIELDS, PERIOD_GROUP, TRACK, TimelineData,
                                   _error)
from analysis_store import content_hash, now_ms
from friend_run_store import RunStore
from kyf_plugin import timeline as shared
from kyf_plugin.actor import AGENT, actor as call_actor, agent_name

EVIDENCE_LIMIT = 50
CONTEXT_BEFORE, CONTEXT_AFTER = 3, 2
GAP_MS = 30 * 60 * 1000
EDIT_PARAMS = ("target", "action", "id", "fields", "expectedVersion", "memoryId")
_errors = shared.reraise_as(_error)
_frequency = {}


def _check_version(data, params):
    expected = params.get("expectedVersion")
    if expected is not None and (type(expected) is not int or expected != data.version()):
        raise AdvisorError("conflict", "时间线已在别处修改，请读取最新后重试")


def _frequency_of(database, user):
    """``(months, earliestMs, latestMs)`` over the chat-trend message filter, cached by source and size."""
    context = database.context(user)
    key = (database.account, user, (context or {}).get("source_id"), (context or {}).get("read_count"),
           (context or {}).get("revision"))
    if _frequency.get((database.account, user), (None,))[0] != key:
        rows = database._rows("SELECT seq,side,time_ms,kind,text FROM context_messages WHERE user=? ORDER BY seq",
                              (user,))
        usable, _coverage = friend_trends._events(rows, int(time.time() * 1000))
        times = [item["time"] for item in usable]
        _frequency[(database.account, user)] = (key, (shared.monthly_counts(times), min(times, default=None),
                                                      max(times, default=None)))
    return _frequency[(database.account, user)][1]


def _run_view(store, user):
    runs = RunStore(store, now_ms)
    run = runs.latest_run(user)
    step = next((item for item in json.loads(run["steps_json"]) if item["key"] == "timeline"), None) if run else None
    analyzed = bool(runs.scans(user, "timeline"))
    if run is None or step is None:
        return {"state": "idle", "runState": run["state"] if run else None, "detail": "", "done": None,
                "total": None, "error": None, "analyzed": analyzed}
    return {"state": step["state"], "runState": run["state"], "detail": step.get("detail") or "",
            "done": step.get("done"), "total": step.get("total"), "error": step.get("error"), "analyzed": analyzed}


def _memory_brief(memory):
    return {"id": memory["id"], "text": memory.get("text") or "", "eventTime": memory.get("eventTime")}


def _links(data, memories):
    """``(memories by event id, memories shown as read-only rows)``."""
    by_id = {memory["id"]: memory for memory in memories}
    links = link_memories(data.events(), memories, data.unlinked())
    per_event = {}
    for memory_id, event in links.items():
        per_event.setdefault(event, []).append(by_id[memory_id])
    return per_event, [memory for memory in memories if memory["id"] not in links]


def _union(event, linked):
    seen = {row["messageId"] for row in event.get("evidence") or []}
    return len(seen | {msg for memory in linked for msg in memory_ids(memory)})


def memory_ref(memory):
    start, end, precision = memory_time(memory)
    return {"id": "memory:" + memory["id"], "memoryId": memory["id"], "title": str(memory.get("text") or "")[:40],
            "text": memory.get("text") or "", "track": TRACK, "category": "其他", "kind": "point",
            "status": "occurred", "importance": "general", "startMs": start, "endMs": end, "precision": precision,
            "whenText": shared.display_when(start, end, precision), "readOnly": True,
            "evidenceCount": len(memory_ids(memory))}


def _events(data, memories):
    per_event, unlinked = _links(data, memories)
    agent = data.agent_edits()
    rows = []
    for event, edit in data.visible_events():
        row = shared.event_row(event, edit, editable=EVENT_FIELDS, extra=("category", "note"))
        linked = per_event.get(event["id"], [])
        row.update(source="timeline", memory=_memory_brief(linked[0]) if linked else None,
                   memories=[_memory_brief(memory) for memory in linked],
                   agentEditedMs=agent.get((edit or {}).get("last_seq")), readOnly=False)
        if linked:
            row["level"] = "confirmed"  # a confirmed 关系记忆 counts as the user's confirmation
            row["evidenceCount"] = _union(event, linked)
            if row["startMs"] is None:
                start, end, precision = memory_time(linked[0])
                if start is not None:
                    row.update(startMs=start, endMs=end, precision=precision,
                               whenText=shared.display_when(start, end, precision))
        rows.append(row)
    rows.sort(key=lambda row: (row["startMs"] is None, -(row["startMs"] or 0), row["id"]))
    refs = [memory_ref(memory) for memory in unlinked if memory_time(memory)[0] is not None]
    return rows, refs


def view(service, account, user, params=None):
    """``page.timeline``: one page of events from ``offset`` within ``pageBytes`` (at most 2 MB)."""
    params = params or {}
    offset, page_bytes = params.get("offset", 0), params.get("pageBytes")
    if type(offset) is not int or offset < 0:
        raise AdvisorError("invalid-request", "时间线读取位置不正确")
    if page_bytes is not None and (type(page_bytes) is not int or page_bytes < 1):
        raise AdvisorError("invalid-request", "时间线读取范围不正确")
    account, user, _fingerprint, database, store = service._scope(account, user)
    data = TimelineData(database, user)
    events, refs = _events(data, shared_memories(database, user))
    names = set(data.track_names())
    periods = []
    for period, edit in shared.apply_period_edits(data.periods(), data.edits("period"), editable=PERIOD_FIELDS):
        row = shared.period_row(period, edit, editable=PERIOD_FIELDS, extra=("category",))
        if row["track"] not in names:
            row["track"] = TRACK
        periods.append(row)
    page, next_offset = shared.page_events(events, offset, page_bytes)
    frequency, earliest, latest = _frequency_of(database, user)
    journal = data.journal()
    user_count, user_items = journal.recent("user", 1)
    agent_count, agent_items = journal.recent(AGENT, 5)
    return {"account": account, "user": user, "version": data.version(), "events": page, "eventsTotal": len(events),
            "nextOffset": next_offset, "periods": periods, "tracks": data.tracks(), "categories": list(CATEGORIES),
            "coverage": {"earliestMs": earliest, "latestMs": latest}, "frequency": frequency, "memoryRefs": refs,
            "undo": {"user": {"count": user_count, "label": user_items[0]["label"] if user_items else None},
                     "agent": {"count": agent_count, "label": agent_items[0]["label"] if agent_items else None,
                               "items": agent_items}},
            "run": _run_view(store, user)}


# -- 模拟档案 -------------------------------------------------------------------------------------------
RECENT_MS = 183 * 86_400_000
PROFILE_EVENTS = 16
PROFILE_MEMORY = 60


def _month(ms, precision):
    day = time.localtime(ms / 1000)
    return "%d" % day.tm_year if precision in ("year", "about") else "%d.%02d" % (day.tm_year, day.tm_mon)


def _period_line(period):
    line = "%s 起%s" % (_month(period["startMs"], period.get("precision")), period["title"])
    last = period.get("lastMs")
    return line + ("，最近一次提到 %s" % time.strftime("%Y-%m", time.localtime(last / 1000)) if last else "")


def _event_line(event, linked):
    start, end, precision = event.get("startMs"), event.get("endMs"), event.get("precision", "unknown")
    if start is None and linked:
        start, end, precision = memory_time(linked[0])
    flags = [label for flag, label in ((event.get("status") == "planned", "计划"),
                                       (event.get("importance") == "important", "重要")) if flag]
    notes = "；".join(" ".join(str(memory.get("text") or "").split())[:PROFILE_MEMORY] for memory in linked)
    said = event.get("saidMs")
    when = shared.display_when(start, end, precision) if start or not said else         time.strftime("%Y-%m-%d 提到", time.localtime(said / 1000))
    return "%s %s%s%s" % (when, event["title"],
                          "（%s）" % "、".join(flags) if flags else "", "（关系记忆：%s）" % notes if notes else "")


def _within(event, linked, clock):
    """Recent shared events, every important one, plans still ahead and anything tied to a confirmed memory."""
    start = event.get("startMs")
    if linked or event.get("importance") == "important":
        return True
    if event.get("status") == "planned":
        return start is not None and (event.get("endMs") or start) >= clock
    # An undated shared event counts from when it was talked about.
    start = start if start is not None else event.get("saidMs")
    return start is not None and clock - RECENT_MS <= start <= clock


def _said_before(event, before_ms):
    rows = event.get("evidence") or []
    return bool(rows) and all(isinstance(row.get("time"), int) and 0 < row["time"] < before_ms for row in rows)


def profile(database, user, before_ms=None, now=None):
    """模拟档案 2b “我们的经历”: ``(lines, memory ids written here, stamp)``.

    Ongoing shared periods, then events (recent six months, important, plans not yet due, tied to a
    confirmed 共同事件 memory); the user's versions first, newest first, one section per track when
    custom tracks are used. Automatic events count unless 待判断 or cancelled. With ``before_ms``
    (backtests) only events whose every line was said before the cut are used, without edits, custom
    tracks or memory links (their time is unknown)."""
    data = TimelineData(database, user)
    backtest = isinstance(before_ms, int)
    clock = before_ms if backtest else now if isinstance(now, int) else now_ms()
    raw = data.events()
    if backtest:
        events = [({**event, "track": TRACK}, None) for event in raw
                  if event.get("status") not in ("uncertain", "cancelled") and _said_before(event, before_ms)]
        per_event, names = {}, [TRACK]
        periods = [(period, None) for period in shared.build_periods([event for event, _ in events],
                                                                     group_fields=PERIOD_GROUP)]
    else:
        if not raw and not data.edits():
            return [], set(), None  # nothing to show: no 关系记忆 read either
        per_event, _refs = _links(data, shared_memories(database, user)) if raw else ({}, [])
        events = [(event, edit) for event, edit in data.visible_events() if event.get("status") != "cancelled" and
                  (edit is not None or event["id"] in per_event or event.get("status") != "uncertain")]
        periods = shared.apply_period_edits(
            shared.build_periods([event for event, _ in events], group_fields=PERIOD_GROUP),
            data.edits("period"), editable=PERIOD_FIELDS)
        names = data.track_names()
    # (track, periods before events, the user's versions first, newest first, text)
    rows, used, stamp = [], set(), None
    for period, edit in periods:
        if period.get("ongoing") and period.get("startMs"):
            rows.append((period.get("track"), 0, edit is None, -period["startMs"], _period_line(period)))
    chosen = sorted(((edit is None and not per_event.get(event["id"]),
                      -(event.get("startMs") or event.get("saidMs") or 0), index)
                     for index, (event, edit) in enumerate(events)
                     if _within(event, per_event.get(event["id"], []), clock)))[:PROFILE_EVENTS]
    for automatic, newest, index in chosen:
        event, linked = events[index][0], per_event.get(events[index][0]["id"], [])
        rows.append((event.get("track"), 1, automatic, newest, _event_line(event, linked)))
        used.update(memory["id"] for memory in linked)
        said = [row.get("time") or 0 for row in event.get("evidence") or []] + [event.get("saidMs") or 0]
        stamp = max(stamp or 0, *said) or stamp
    sections = {}
    for track, *order, text in sorted(rows, key=lambda row: row[1:4]):
        sections.setdefault(track if track in names else TRACK, []).append(text)
    lines = []
    for name in [name for name in names if name in sections]:
        lines += (["主线「%s」：" % name] if len(sections) > 1 else []) + sections[name]
    return lines, used, stamp


# -- evidence ------------------------------------------------------------------------------------------
def _line(row):
    text = friend_chat.readable_text(row)
    if text is None or row.get("side") not in ("self", "other"):
        return None
    return {"side": row["side"], "speaker": "我" if row["side"] == "self" else "ta", "text": text[:240],
            "time": row.get("time_ms")}


def _around(database, user, row):
    stamp = row.get("time_ms") if isinstance(row.get("time_ms"), int) else None

    def near(other):
        return stamp is not None and isinstance(other.get("time_ms"), int) and abs(other["time_ms"] - stamp) <= GAP_MS
    before = database._rows("SELECT * FROM context_messages WHERE user=? AND seq<? ORDER BY seq DESC LIMIT ?",
                            (user, row["seq"], CONTEXT_BEFORE * 3))
    after = database._rows("SELECT * FROM context_messages WHERE user=? AND seq>? ORDER BY seq LIMIT ?",
                           (user, row["seq"], CONTEXT_AFTER * 3))
    earlier = [line for line in (_line(item) for item in before if near(item)) if line][:CONTEXT_BEFORE]
    later = [line for line in (_line(item) for item in after if near(item)) if line][:CONTEXT_AFTER]
    return list(reversed(earlier)), later


def _item(database, user, msg_id, source, phrase="", digest=None, fallback=None):
    row = database._row("SELECT * FROM context_messages WHERE user=? AND msg_id=?", (user, msg_id))
    if row is None:
        fallback = fallback or {}
        return {"messageId": msg_id, "time": fallback.get("time"), "text": fallback.get("text", ""),
                "side": fallback.get("side"), "speaker": None, "phrase": phrase, "truncated": False,
                "source": source, "status": "missing", "before": [], "after": []}
    text = friend_chat.readable_text(row) or ""
    before, after = _around(database, user, row)
    side = row.get("side") if row.get("side") in ("self", "other") else None
    return {"messageId": msg_id, "time": row.get("time_ms"), "text": text, "side": side,
            "speaker": {"self": "我", "other": "ta"}.get(side), "phrase": phrase,
            "truncated": len(text) > MAX_LINE_CHARS, "source": source,
            "status": "ok" if digest in (None, content_hash(row)) else "changed", "before": before, "after": after}


def evidence(service, account, user, params):
    """``page.timelineEvidence``: the latest 50 lines of one event, a linked memory's lines included."""
    identifier = params.get("id")
    if not isinstance(identifier, str) or not 0 < len(identifier) <= 200:
        raise AdvisorError("invalid-request", "事件不正确")
    account, user, _fingerprint, database, store = service._scope(account, user)
    data = TimelineData(database, user)
    memories = shared_memories(database, user)
    if identifier.startswith("memory:"):
        memory = next((row for row in memories if row["id"] == identifier[len("memory:"):]), None)
        linked, event = [memory] if memory else [], None
    else:
        event = data.event(identifier)
        per_event, _refs = _links(data, memories)
        linked = per_event.get(identifier, []) if event else []
    pairs, seen = [], set()
    for row in (event or {}).get("evidence") or []:
        if row["messageId"] not in seen:
            seen.add(row["messageId"])
            pairs.append((row.get("time") or 0, row["messageId"], "timeline", row.get("phrase", ""), row.get("hash"), row))
    for memory in linked:
        for row in memory.get("evidence") or []:
            if row.get("msgId") and row["msgId"] not in seen:
                seen.add(row["msgId"])
                pairs.append((row.get("time") or 0, row["msgId"], "memory", "", row.get("hash"),
                              {"time": row.get("time"), "text": row.get("excerpt") or ""}))
    pairs.sort(key=lambda pair: -pair[0])
    items = [_item(database, user, msg_id, source, phrase, digest, fallback)
             for _time, msg_id, source, phrase, digest, fallback in pairs[:EVIDENCE_LIMIT]]
    start, end, precision = ((event or {}).get("startMs"), (event or {}).get("endMs"),
                             (event or {}).get("precision", "unknown"))
    if start is None and linked:
        start, end, precision = memory_time(linked[0])
    return {"id": identifier, "total": len(pairs), "items": items, "phrase": (event or {}).get("phrase", ""),
            "resolvedWhen": shared.display_when(start, end, precision),
            "memory": _memory_brief(linked[0]) if linked else None,
            "memories": [_memory_brief(memory) for memory in linked]}


# -- edits and undo ------------------------------------------------------------------------------------
def _label(data, params):
    if params.get("action") == "unlink":
        return "取消与关系记忆的关联"

    def title_of(target, identifier):
        row = next((row for row in data.edits(target) if row["id"] == identifier), None)
        title = (row or {}).get("fields", {}).get("title")
        if not title and target == "event":
            title = (data.event(identifier) or {}).get("title")
        if not title and target == "period":
            title = next((period["title"] for period in data.periods() if period["id"] == identifier), None)
        return title
    return shared.edit_label(params, title_of)


def _unlink(data, params, source):
    if source == AGENT:
        raise AdvisorError("forbidden", "取消与关系记忆的关联只能在页面上操作")
    memory = params.get("memoryId") or (params.get("fields") or {}).get("memoryId")
    event = data.event(params.get("id")) if isinstance(params.get("id"), str) else None
    if params.get("target") != "event" or event is None:
        raise AdvisorError("not-found", "事件不存在")
    if not isinstance(memory, str) or not 0 < len(memory) <= 200:
        raise AdvisorError("invalid-request", "关系记忆不正确")
    identifier = "unlink-" + hashlib.sha256(json.dumps([event["id"], memory]).encode()).hexdigest()[:16]
    data.add_edit("memory", "unlink", identifier, anchors=[row["messageId"] for row in event["evidence"]],
                  fields={"memoryId": memory})
    return event["id"]


def edit(service, account, user, params, context=None):
    """``page.timelineEdit``: one version-checked edit, recorded on the caller's own undo stack."""
    if not isinstance(params, dict):
        raise AdvisorError("invalid-request", "时间线修改不正确")
    params = {key: params[key] for key in EDIT_PARAMS if key in params}  # never a source from params
    source, agent = call_actor(context), agent_name(context)
    if isinstance(params.get("id"), str) and params["id"].startswith("memory:"):
        raise AdvisorError("read-only", "这条来自关系记忆，请到关系记忆里修改")
    account, user, _fingerprint, database, _store = service._scope(account, user)
    data = TimelineData(database, user)
    with data.lock:
        _check_version(data, params)
        label = _label(data, params)
        journal = data.journal()
        before = journal.snapshot()
        with data.db:
            if params.get("action") == "unlink":
                identifier = _unlink(data, params, source)
            else:
                identifier = _errors(shared.apply_edit)(
                    data, {key: params.get(key) for key in ("target", "action", "id", "fields")},
                    default_track=TRACK, match_track=False, categories=CATEGORIES, event_fields=EVENT_FIELDS,
                    period_fields=PERIOD_FIELDS)
        if journal.push(label, before, actor=source, agent=agent, now_ms=now_ms()) is not None:
            with data.db:
                data.bump()
    return {**view(service, account, user), "editedId": identifier, "editLabel": label}


SKIPPED = {"user": "这处之后被 Agent 改过，没有撤销；需要的话直接修改",
           AGENT: "这处之后被用户改过，没有撤销"}


def undo(service, account, user, params, context=None, source="user"):
    """``page.timelineUndo`` (the user's own stack, page only) or ``page.timelineUndoAgent`` (the Agent's)."""
    if source == "user" and call_actor(context) == AGENT:
        raise AdvisorError("forbidden", "Agent 只能撤销自己的修改")
    account, user, _fingerprint, database, _store = service._scope(account, user)
    data = TimelineData(database, user)
    with data.lock:
        _check_version(data, params or {})
        result = data.journal().undo(actor=source)
    undone = result["label"] if result and result["undone"] else None
    skipped = {"label": result["label"], "reason": SKIPPED[source]} if result and not result["undone"] else None
    return {**view(service, account, user), "undone": undone, "skipped": skipped}
