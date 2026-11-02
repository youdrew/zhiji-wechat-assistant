"""Deterministic relationship-trend facts from the managed chat projection.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Direction, time validation and conversation-start rules follow the behaviour of
SuperDatingApp ``relationship_metrics.calculate_relationship_metrics`` (the maintainer's
local reference project). Replies differ on purpose: a reply waits from the end of the
other side's first burst, and nothing after 8 hours of silence counts as a reply, because
that opens a new conversation. This module only decides the per-message
facts (who, when, how many characters, turn and conversation starts, reply delays);
the 知己分析 window buckets them into minute / hour / day / week / month / year
periods. The numbers describe observed message activity only; they are never a score
of the other person's feelings, and no model is called.
"""
from __future__ import annotations

import re
from datetime import datetime

SESSION_GAP_MS = 8 * 3600 * 1000          # silence that starts a new conversation (and is never a reply)
FOLLOW_UP_MS = 5 * 60 * 1000               # a pause this long inside one side's turn makes the rest follow-ups
FUTURE_TOLERANCE_MS = 5 * 60 * 1000
KINDS = ("text", "image", "voice", "video", "sticker", "other")
TYPE_KINDS = {"图片": "image", "语音": "voice", "视频": "video", "动画表情": "sticker"}
SIDES = ("self", "other")
TURN, START = 1, 2          # message flag bits: begins a turn / begins a conversation
_EMOJI_CODE = re.compile(r"\[[^\[\]\s]{1,8}\]")   # WeChat's inline [微笑] codes count as one character
_SPACE = re.compile(r"\s+")


def _local(ms):
    return datetime.fromtimestamp(ms / 1000).astimezone()


def _kind(row):
    if row.get("kind") == "text":
        return "text"
    return TYPE_KINDS.get(row.get("type"), "image" if row.get("kind") == "image" else "other")


def _chars(row, kind):
    text = row.get("text")
    if kind != "text" or not isinstance(text, str):
        return 0
    return len(_SPACE.sub("", _EMOJI_CODE.sub("e", text)))


def _events(rows, now_ms):
    """Usable messages with their flags and reply delays, in source order."""
    coverage = {"read": len(rows), "usable": 0, "unknownDirection": 0, "untimed": 0, "future": 0,
                "ambiguousSameTime": 0}
    usable = []
    # The running single-author turn: {"side", "since", "last", "settled"}; None after a barrier.
    # ``since`` is when its author started waiting for an answer: the end of the turn's first burst,
    # so splitting one thought into several messages adds nothing and a later follow-up ("?") does
    # not hide how long the first message waited.
    previous = None
    last_time = None
    last_side = None         # author of the running turn, None after a barrier or an ambiguous second
    interrupted = False
    index = 0
    while index < len(rows):
        row = rows[index]
        stamp = row.get("time_ms")
        side = row.get("side")
        problem = ("unknownDirection" if side not in SIDES else
                   "untimed" if not isinstance(stamp, int) or stamp <= 0 else
                   "future" if stamp > now_ms + FUTURE_TOLERANCE_MS else None)
        if problem:
            coverage[problem] += 1
            previous, last_side, interrupted, index = None, None, True, index + 1
            continue
        # Messages in the same second form one group; mixed authors cannot be ordered.
        group = [row]
        while (index + len(group) < len(rows) and rows[index + len(group)].get("time_ms") == stamp and
               rows[index + len(group)].get("side") in SIDES):
            group.append(rows[index + len(group)])
        index += len(group)
        sides = {item["side"] for item in group}
        gap = None if last_time is None else stamp - last_time
        new_session = gap is None or gap >= SESSION_GAP_MS
        start = gap is not None and gap >= SESSION_GAP_MS and not interrupted and len(sides) == 1
        reply = None
        if len(sides) == 1:
            current = next(iter(sides))
            if previous is not None and previous["side"] != current and 0 < stamp - previous["last"] < SESSION_GAP_MS:
                seconds = (stamp - previous["since"]) / 1000
                reply = int(seconds) if seconds.is_integer() else round(seconds, 1)
            if previous is not None and previous["side"] == current and not new_session:
                if not previous["settled"]:
                    if stamp - previous["last"] >= FOLLOW_UP_MS:
                        previous["settled"] = True
                    else:
                        previous["since"] = stamp
                previous["last"] = stamp
            else:
                previous = {"side": current, "since": stamp, "last": stamp, "settled": False}
        else:
            coverage["ambiguousSameTime"] += 1
            previous = None
        seen = set()
        for position, item in enumerate(group):
            kind = _kind(item)
            flags = 0
            if len(sides) == 1:
                if position == 0 and (new_session or interrupted or last_side != item["side"]):
                    flags |= TURN
            elif item["side"] not in seen:
                flags |= TURN
            seen.add(item["side"])
            if position == 0 and start:
                flags |= START
            usable.append({"time": stamp, "side": item["side"], "chars": _chars(item, kind), "kind": kind,
                           "flags": flags, "reply": reply if position == 0 else None})
        last_side = next(iter(sides)) if len(sides) == 1 else None
        last_time = stamp
        interrupted = False
    coverage["usable"] = len(usable)
    return usable, coverage


def relationship_trends(rows, *, context_state, now_ms):
    """Return every usable message of the readable history as compact columns.

    ``messages.time`` is delta-encoded milliseconds in time order; ``side`` is 0 for me and
    1 for the other person; ``kind`` indexes ``kinds``; ``flags`` carries TURN and START.
    ``replies`` pairs a message index with the reply delay in seconds. Long histories are
    never cut to a recent window."""
    ordered = sorted(rows, key=lambda row: row["seq"])
    usable, coverage = _events(ordered, now_ms)
    usable.sort(key=lambda item: item["time"])          # stable: same-time messages keep source order
    columns = {"time": [], "side": [], "chars": [], "kind": [], "flags": []}
    replies = []
    previous_time = 0
    for position, item in enumerate(usable):
        columns["time"].append(item["time"] - previous_time)
        previous_time = item["time"]
        columns["side"].append(SIDES.index(item["side"]))
        columns["chars"].append(item["chars"])
        columns["kind"].append(KINDS.index(item["kind"]))
        columns["flags"].append(item["flags"])
        if item["reply"] is not None:
            replies.append([position, item["reply"]])
    complete = context_state.get("state") == "ready"
    first_ms = usable[0]["time"] if usable else None
    last_ms = usable[-1]["time"] if usable else None
    reasons = []
    if not complete:
        reasons.append("聊天历史仍在读取或读取失败，统计可能不完整")
    if first_ms is None:
        reasons.append("可读历史中暂无可统计的消息")
    now = _local(now_ms)
    return {
        "timezone": now.tzname() or now.strftime("%z"),
        "utcOffsetMinutes": int(now.utcoffset().total_seconds() // 60),
        "cutoffMs": now_ms,
        "kinds": list(KINDS),
        "messages": columns,
        "replies": replies,
        "history": {"state": context_state.get("state"), "complete": complete, "firstMessageMs": first_ms,
                    "lastMessageMs": last_ms, "readCount": context_state.get("readCount", len(rows))},
        "coverage": coverage,
        "insufficient": reasons,
    }
