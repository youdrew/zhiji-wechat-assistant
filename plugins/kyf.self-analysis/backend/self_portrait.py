"""本人画像: a short, read-only text of the 自我剖析 conclusions for another plugin's prompt.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Added 2026-11-09 for 分析与建议 (kyf.message-insights): the reply suggestions for a friend's
round of messages also see who the user is and what they are going through lately.
Modified 2026-11-09: compact lines only. Self-reports and the user's own notes stay as written,
an inference shrinks to its dimension and leaning, and 近况 keeps only periods that started
within a year and events of the last four months: the timeline marks many old periods as never
ended, and those are not what the user is going through now. Nothing here calls a model or writes.
"""
import re

from self_contracts import integer, now_ms
from self_persona import PersonaStore
from self_timeline import timeline_catalog
from self_trial import tag_lines
from self_values import ValuesStore

DEFAULT_CHARS = 2000
MIN_CHARS, MAX_CHARS = 200, 6000
RECENT_EVENTS = 8
DAY_MS = 86_400_000
PERIOD_DAYS, EVENT_DAYS = 365, 120
NOTE_CHARS, REPORT_CHARS, GIST_CHARS = 100, 60, 40
# How the user talks and relates; political and moral-foundation leanings do not shape a chat reply.
REPLY_DIMENSIONS = ("big5_", "att_", "val_")


def _clip(text, limit):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _leaning(inference):
    """偏高/中等/偏低 from a dimension's position, else the first clause of the conclusion."""
    position = inference.get("position")
    if isinstance(position, (int, float)):
        return "偏高" if position >= 0.65 else "偏低" if position <= 0.35 else "中等"
    return _clip(re.split(r"[。；;]", inference.get("text") or "")[0], GIST_CHARS)


def compact_facts(store):
    """Self-reports and the user's own notes first, then one short line per inferred dimension."""
    firm, inferred = [], []
    for noun, view in (("性格", PersonaStore(store).view()), ("三观", ValuesStore(store).view())):
        for key, label in PersonaStore.reports if noun == "性格" else ():
            report = (view.get("selfReports") or {}).get(key)
            if report and report.get("text"):
                firm.append(f"{label}（自述）：{_clip(report['text'], REPORT_CHARS)}")
        for row in view["dimensions"]:
            if row["manual"]:
                firm.append(f"{noun}·{row['label']}（本人补充）：{_clip(row['manual']['text'], NOTE_CHARS)}")
            if row["selfReport"] and row["selfReport"].get("text"):
                firm.append(f"{noun}·{row['label']}（自述）：{_clip(row['selfReport']['text'], REPORT_CHARS)}")
            elif (row["inference"] and row["inference"].get("text") and
                  (noun == "三观" or row["key"].startswith(REPLY_DIMENSIONS))):
                inferred.append(f"{noun}·{row['label']}：{_leaning(row['inference'])}（推测）")
    return firm + inferred


def recent_lines(catalog, before):
    """Ongoing periods that started within a year, then the newest events of the last four months."""
    lines = []
    for period in catalog["periods"]:
        start = period.get("startMs")
        if period.get("ongoing") and isinstance(start, int) and 0 <= before - start <= PERIOD_DAYS * DAY_MS:
            lines.append("近况·进行中：" + " ".join(part for part in (period.get("when"), period["title"]) if part))
    # Plans dated in the future are not what happened lately.
    events = [event for event in catalog["events"]
              if isinstance(event.get("startMs"), int) and 0 <= before - event["startMs"] <= EVENT_DAYS * DAY_MS]
    for event in events[:RECENT_EVENTS]:
        lines.append("近况·事件：" + " ".join(part for part in (event.get("when"), event["title"]) if part))
    return lines


def _prefix(lines, budget):
    result, used = [], 0
    for line in lines:
        if used + len(line) > budget:
            break
        result.append(line)
        used += len(line)
    return result


def portrait(store, tags, params):
    budget = integer(params.get("budgetChars"), DEFAULT_CHARS, MIN_CHARS, MAX_CHARS)
    before = params.get("beforeTimeMs")
    before = before if type(before) is int and before > 0 else None
    recent = recent_lines(timeline_catalog(store, before, with_start=True), before or now_ms())
    # Who the user is, then what they are going through now; tags fill what is left.
    lines = _prefix(compact_facts(store), budget * 4 // 10)
    lines += _prefix(recent, budget * 4 // 10)
    used = sum(len(line) for line in lines)
    lines += tag_lines(tags, 40, max(0, budget - used)) if tags is not None else []
    text = "\n".join(lines)
    return {"available": bool(text), "text": text,
            "stamp": [store.meta("revision", 0), store.meta("generation", 0)]}
