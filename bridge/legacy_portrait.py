"""Read-only view of portraits saved by the retired WechatVibe analysis.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The local Laya and API label/portrait pipeline no longer runs. Its account
result database stays on disk; this module only reads the last saved aggregate
statistics for one private chat and derives the same display values the old
portrait showed. It never writes, recomputes from chat text, or calls a model.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from backend_contracts import ROOT

STYLE_LABELS = {
    "socialEnergy": "表达活力", "humor": "幽默表达", "composure": "情绪平和",
    "initiative": "话题主动", "care": "关怀支持", "affection": "亲近表达",
}


def result_database(account, root=ROOT):
    filename = hashlib.sha256(account.encode("utf-8")).hexdigest() + ".sqlite3"
    return Path(root) / ".local" / "real-client-data" / filename


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _valid_state(state):
    return (isinstance(state, dict) and isinstance(state.get("count"), int) and state["count"] > 0 and
            all(isinstance(state.get(field), dict) for field in ("emotion", "intent", "words")))


def _distribution(state, field):
    count = state["count"]
    items = [{"label": label, "probability": _number(mass) / count}
             for label, mass in state[field].items() if isinstance(label, str)]
    return sorted(items, key=lambda item: -item["probability"])


def _keywords(counts):
    pairs = [(word, count) for word, count in counts.items()
             if isinstance(word, str) and isinstance(count, int) and count >= 2]
    return [{"word": word, "count": count} for word, count in sorted(pairs, key=lambda item: (-item[1], item[0]))][:6]


def _traits(state):
    count = state.get("styleCount")
    style = state.get("style")
    if not isinstance(count, int) or count <= 0 or not isinstance(style, dict):
        return []
    return [{"key": key, "label": label, "val": int(100 * _number(style.get(key)) / count + .5)}
            for key, label in STYLE_LABELS.items()]


def _affinity(state):
    count = state.get("scoreCount")
    if not isinstance(count, int) or count <= 0:
        return None
    total, weighted = _number(state.get("scoreSum")), _number(state.get("scoreWeighted"))
    value = total if count == 1 else (.5 * total + .5 * weighted / (count - 1)) / (.75 * count)
    return int(50 + 50 * max(-1, min(1, value)) + .5)


def statistics_view(state, total=None):
    return {"kind": "statistics", "emotion": _distribution(state, "emotion"),
            "intent": _distribution(state, "intent"), "keywords": _keywords(state["words"]),
            "traits": _traits(state), "affinity": _affinity(state), "analyzed": state["count"],
            "total": total}


def _json(value):
    try:
        return json.loads(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _local(conn, account, user):
    best = None
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "batch_progress_v1" in tables:
        for (raw,) in conn.execute("SELECT state_json FROM batch_progress_v1 WHERE account=? AND session=? "
                                   "AND subject=?", (account, user, user)):
            state = _json(raw)
            if _valid_state(state) and (best is None or state["count"] > best["count"]):
                best = state
    if "profile_state_v1" in tables:
        for (raw,) in conn.execute("SELECT state FROM profile_state_v1 WHERE account=? AND session=? "
                                   "AND subject IN (?, '')", (account, user, user)):
            state = _json(raw)
            if _valid_state(state) and (best is None or state["count"] > best["count"]):
                best = state
    return best


def _api(conn, account, user):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "api_portrait_v1" not in tables:
        return []
    models = {}
    if "api_source_meta_v1" in tables:
        models = {source: model for source, model in conn.execute(
            "SELECT source_id, model FROM api_source_meta_v1 WHERE account=?", (account,))}
    items = []
    for source_id, available, processed, portrait_raw, resume_raw in conn.execute(
            "SELECT source_id, available_json, processed, portrait_json, resume_json FROM api_portrait_v1 "
            "WHERE account=? AND session=? AND subject=?", (account, user, user)):
        label = "API · " + str(models.get(source_id) or "模型")
        resume = _json(resume_raw) or {}
        statistics = resume.get("portraitStatistics") if isinstance(resume, dict) else None
        state = statistics.get("state") if isinstance(statistics, dict) else None
        totals = _json(available) or {}
        total = totals.get("targetTextCount") if isinstance(totals, dict) else None
        if _valid_state(state):
            items.append({"source": "api", "label": label, **statistics_view(state, total)})
            continue
        portrait = _json(portrait_raw)
        if isinstance(portrait, dict) and any(portrait.get(key) for key in ("summary", "communication", "topics")):
            items.append({"source": "api", "label": label, "kind": "portrait",
                          "portrait": {key: portrait.get(key) for key in (
                              "summary", "communication", "emotionExpression", "interactionPreferences",
                              "patterns", "boundaries", "uncertain", "topics", "traits", "affinity")},
                          "analyzed": processed if isinstance(processed, int) else 0, "total": total})
    return items


def read_legacy_portrait(account, user, root=ROOT):
    """Saved portraits for one private chat, newest data first; empty when none were saved."""
    if not isinstance(user, str) or not user or user.endswith("@chatroom"):
        return {"available": False, "items": []}
    path = result_database(account, root)
    if not path.is_file():
        return {"available": False, "items": []}
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return {"available": False, "items": [], "error": "旧版结果库无法读取"}
    try:
        items = []
        local = _local(conn, account, user)
        if local is not None:
            items.append({"source": "local", "label": "本地 Laya 统计", **statistics_view(local)})
        items.extend(_api(conn, account, user))
        return {"available": bool(items), "items": items}
    except sqlite3.Error:
        return {"available": False, "items": [], "error": "旧版结果库无法读取"}
    finally:
        conn.close()
