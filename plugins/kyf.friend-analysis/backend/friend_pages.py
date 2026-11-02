"""Read-only page data for 知己分析 v2: 好友概述, 性格分析, 三观分析 and 聊天分析.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Everything here is assembled from stored results and the local projection. No model
is called; a page whose inputs changed since its result was made says "待更新".
Moved into the kyf.friend-analysis plugin, 2026-10-13 (Apache-2.0); ``service`` is the plugin's
page service over its mirror of Core's projection.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import friend_chat
import friend_cache
import friend_overview
import friend_persona
from contracts import AdvisorError
from kyf_plugin import CoreError
from kyf_plugin import persona as persona_catalog
from friend_analysis import RECENT_WINDOW
from friend_run_store import RunStore
from analysis_store import content_hash, now_ms

CONTEXT_ROWS = 5
FIRM = ("较有把握", "有一定依据")
OLD_TRAITS = {"socialEnergy": "表达活力", "humor": "幽默表达", "composure": "情绪平和", "initiative": "话题主动",
              "care": "关怀支持", "affection": "亲近表达",
              # The behaviour dimensions replaced by the Big Five on 2026-10-25.
              "communication": "表达方式", "social": "社交偏好", "planning": "安排习惯", "openness": "尝试偏好",
              "emotion": "情绪表达", "boundary": "边界表达", "conflict": "分歧沟通", "relating": "相处观察"}


def month_change(rows, now_ms):
    """Contact days in the last complete local calendar month and the month before it.

    ``None`` unless the readable history reaches back before the earlier month, so an
    uncovered month is never reported as a month without contact.
    """
    now = datetime.fromtimestamp(now_ms / 1000).astimezone()
    current = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last = (current - timedelta(days=1)).replace(day=1)
    previous = (last - timedelta(days=1)).replace(day=1)
    stamps = [row["time_ms"] for row in rows if row["side"] in ("self", "other") and
              isinstance(row["time_ms"], int) and 0 < row["time_ms"] <= now_ms]
    if not stamps or min(stamps) >= previous.timestamp() * 1000:
        return None
    days = {"last": set(), "previous": set()}
    for stamp in stamps:
        moment = datetime.fromtimestamp(stamp / 1000).astimezone()
        if last <= moment < current:
            days["last"].add(moment.date())
        elif previous <= moment < last:
            days["previous"].add(moment.date())
    return {"label": "%d 年 %d 月" % (last.year, last.month), "days": len(days["last"]),
            "previousDays": len(days["previous"])}


def _rows(database, user, context):
    return database.context_messages(user, context["read_count"]) if context else []


def _facts(database, store, user, context):
    times = database._row("SELECT MIN(time_ms) AS first, MAX(time_ms) AS last FROM context_messages "
                          "WHERE user=? AND time_ms>0", (user,)) or {}
    goal = store.goal(user)
    return {"firstMs": times.get("first"), "lastMs": times.get("last"),
            "messages": context["read_count"] if context else 0,
            "historyState": context["state"] if context else "empty",
            "goal": goal["goal"] or "自然交流", "goalIsDefault": not goal["goal"]}


def overview(service, account, user):
    account, user, fingerprint, database, store = service._scope(account, user)
    runs = RunStore(store, now_ms)
    context = database.context(user)
    summary, step = runs.result(user, "summary"), runs.result(user, "next")
    persona = runs.result(user, "persona")
    about = []
    if persona is not None:
        for item in persona["payload"].get("dimensions", []):
            if item["confidence"] in FIRM and item["label"] and item["id"] in friend_persona.GROUPS["personality"]:
                about.append({"kind": "persona", "id": item["id"], "label": friend_persona.LABELS.get(item["id"]),
                              "text": item["label"], "confidence": item["confidence"]})
        about.sort(key=lambda item: FIRM.index(item["confidence"]))
        about = about[:3]
    confirmed = store.memories(user, ("confirmed",))
    confirmed.sort(key=lambda row: row["updated_at_ms"], reverse=True)
    for row in confirmed:
        if len(about) >= 4:
            break
        tags = json.loads(row["tags_json"])
        if "性格" in tags or "爱好" in tags:
            about.append({"kind": "memory", "id": row["id"], "label": tags[0], "text": row["text"]})
    remember = [{"id": row["id"], "text": row["text"], "tags": json.loads(row["tags_json"])} for row in confirmed[:3]]
    change = None
    if context and context["state"] == "ready":
        rows = database._rows("SELECT side,time_ms FROM context_messages WHERE user=?", (user,))
        change = month_change(rows, int(time.time() * 1000))
    stale = False
    if summary is not None and context and context["state"] == "ready":
        latest = database._row("SELECT msg_id FROM context_messages WHERE user=? AND side IN ('self','other') "
                               "AND time_ms>0 ORDER BY seq DESC LIMIT 1", (user,))
        rows = database._rows("SELECT * FROM context_messages WHERE user=? ORDER BY seq DESC LIMIT ?",
                              (user, RECENT_WINDOW))
        # Compare the exact saved message identities and hashes. Checking only the
        # last id misses recalls, edits and backfills within the summary range.
        cited = summary["payload"].get("evidence", [])
        current_hashes = {row["msg_id"]: content_hash(row) for row in rows}
        stale = (context["source_id"] != fingerprint or
                 latest is not None and latest["msg_id"] != summary["payload"].get("lastMsgId") or
                 any(current_hashes.get(item["msgId"]) != item["hash"] for item in cited))
        try:
            config = service.analysis.config()
            _selected, _complete, deps = service.analysis.recent_deps(
                store, user, list(reversed(rows)), config,
                service.analysis._budget(SimpleNamespace(config=config), friend_overview.SUMMARY_SYSTEM),
                context["read_count"] > len(rows))
            stale = stale or summary["deps"] != deps
        except (AdvisorError, CoreError):
            stale = True
    return {"account": account, "user": user, "facts": _facts(database, store, user, context),
            "summary": dict(summary["payload"], createdAtMs=summary["created_at_ms"]) if summary else None,
            "next": dict(step["payload"], createdAtMs=step["created_at_ms"]) if step else None,
            "about": about, "remember": remember, "change": change, "stale": stale}


def persona(service, account, user):
    account, user, fingerprint, database, store = service._scope(account, user)
    runs = RunStore(store, now_ms)
    result = runs.result(user, "persona")
    scan = runs.published_scan(user, "persona")
    observations = [{"id": row["id"], "judgement": row["judgement"], "note": row["note"],
                     "dimension": row["dimension"], "legacyLabel": OLD_TRAITS.get(row["dimension"]),
                     "version": row["version"], "updatedAtMs": row["updated_at_ms"]} for row in store.observations(user)]
    stale = False
    if result is not None and scan is not None:
        evidence = runs.persona_evidence(user, scan["generation"])
        stale = result["deps"] != service.analysis.persona_deps(scan, evidence, result["deps"].get("model"))
        stale = stale or scan["source_fp"] != fingerprint or any(
            item["generation"] > scan["generation"] for item in runs.scans(user, "persona"))
        try:
            stale = stale or result["model_fp"] != service.analysis.config()["modelFp"]
        except (AdvisorError, CoreError):
            stale = True
    catalog = []
    for key, label, low, high, _cue in friend_persona.DIMENSIONS:
        catalog.append({"id": key, "label": label, "left": low, "right": high, "group": friend_persona.GROUP_OF[key]})
    for key, label, types, _cue in friend_persona.TYPE_DIMENSIONS:
        catalog.append({"id": key, "label": label, "types": list(types), "group": friend_persona.GROUP_OF[key]})
    if result is not None and "selfReports" not in result["payload"]:
        # A result from the earlier seven-dimension version cannot be drawn; the next analysis replaces it.
        result, stale = None, True
    return {"account": account, "user": user, "result": result["payload"] if result else None,
            "createdAtMs": result["created_at_ms"] if result else None, "stale": stale, "catalog": catalog,
            "observations": observations,
            "labels": {key: value for key, value in friend_persona.LABELS.items()},
            "families": friend_persona.FAMILY_LABELS, "sources": persona_catalog.SOURCES}


def persona_evidence(service, account, user, ids):
    account, user, _fingerprint, database, store = service._scope(account, user)
    if not isinstance(ids, list) or not 1 <= len(ids) <= 40 or any(not isinstance(item, str) for item in ids):
        raise AdvisorError("invalid-request", "证据编号不正确")
    runs = RunStore(store, now_ms)
    scan = runs.published_scan(user, "persona")
    if scan is None:
        return {"items": []}
    items = runs.persona_evidence(user, scan["generation"], set(ids))
    return {"items": [{"id": item["id"], "dim": item["dim"], "note": item["note"], "excerpt": item["excerpt"],
                       "timeMs": item["first_ms"], "messageId": json.loads(item["msg_ids"])[0],
                       "status": _status(database, user, json.loads(item["msg_ids"])[0], json.loads(item["hashes"])[0])}
                      for item in items]}


def _status(database, user, msg_id, value):
    row = database._row("SELECT * FROM context_messages WHERE user=? AND msg_id=?", (user, msg_id))
    if row is None:
        return "missing"
    return "ok" if content_hash(row) == value else "changed"


def message_context(service, account, user, msg_id):
    """The cited message with five rows on each side, from the managed projection only."""
    account, user, _fingerprint, database, _store = service._scope(account, user)
    if not isinstance(msg_id, str) or not msg_id or len(msg_id) > 200:
        raise AdvisorError("invalid-request", "消息编号不正确")
    target = database._row("SELECT seq FROM context_messages WHERE user=? AND msg_id=?", (user, msg_id))
    if target is None:
        context = database.context(user)
        return {"status": "missing" if context and context["state"] == "ready" else "unready", "messages": []}
    rows = database._rows("SELECT * FROM context_messages WHERE user=? AND seq BETWEEN ? AND ? ORDER BY seq",
                          (user, target["seq"] - CONTEXT_ROWS, target["seq"] + CONTEXT_ROWS))
    return {"status": "ok", "messages": [{
        "id": row["msg_id"], "target": row["msg_id"] == msg_id,
        "speaker": "我" if row["side"] == "self" else (row["sender_name"] or "对方"),
        "timeMs": row["time_ms"] if isinstance(row["time_ms"], int) and row["time_ms"] > 0 else None,
        "text": friend_chat.readable_text(row) or "[媒体或非文字消息]"} for row in rows]}


def chat(service, account, user):
    account, user, fingerprint, database, store = service._scope(account, user)
    runs = RunStore(store, now_ms)
    context = database.context(user)
    rows = _rows(database, user, context)
    ready = bool(context and context["state"] == "ready")
    reply = friend_chat.reply_rate(rows, now_ms=int(time.time() * 1000), history_ready=ready)
    scan = runs.published_scan(user, "chat")
    statistics = coverage = None
    if scan is not None:
        labelled = runs.chat_blocks(user, scan["generation"])
        statistics = friend_chat.statistics(labelled, rows[:scan["covered_count"]])
        completeness = friend_cache.chat_coverage(labelled, rows, scan["covered_count"],
                                                  legacy=scan["version"] == friend_chat.LEGACY_CHAT_VERSION)
        coverage = {"fromMs": scan["from_ms"], "toMs": scan["to_ms"], "blocks": len(labelled),
                    "labelled": sum(item["intent"] is not None for item in labelled),
                    "pending": max(0, friend_chat.scan_end(rows) - scan["covered_count"]),
                    "missingTextBlocks": len(completeness["missing"]), "missingFields": completeness["fields"],
                    "outdated": scan["version"] != friend_chat.CHAT_VERSION or scan["source_fp"] != fingerprint or
                                not friend_cache.chat_payload_valid(labelled, rows, scan["covered_count"])}
    try:
        service.analysis.config()
        configured = True
    except (AdvisorError, CoreError):
        configured = False
    return {"account": account, "user": user, "reply": reply, "statistics": statistics, "coverage": coverage,
            "historyReady": ready, "configured": configured}


def chat_round(service, account, user, block_id):
    account, user, _fingerprint, database, _store = service._scope(account, user)
    if not isinstance(block_id, str) or not block_id or len(block_id) > 200:
        raise AdvisorError("invalid-request", "互动编号不正确")
    context = database.context(user)
    return {"blocks": friend_chat.block_messages(_rows(database, user, context), [block_id])}
