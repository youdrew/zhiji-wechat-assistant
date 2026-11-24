"""模拟对话·编码好友: the friend's reply-sample library, style statistics and the layered profile.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Deterministic, no model call. Samples invert kyf.self-analysis ``self_samples.extract``: my
consecutive messages are the input, the friend's run that follows within 30 minutes is the
reply. They are kept incrementally in this plugin's database and rebuilt when the mirror of
Core's projection resets. ``terms``, ``scene`` and the lexical score are copied from
kyf.self-analysis ``self_samples`` / ``self_retrieval``; ``focus`` and the prefix cut follow
``self_trial`` (plugins cannot import each other). The profile layers follow
docs/friend-simulation-requirements.md §3.3 plus 2a 标签 / 2b 我们的经历 from
docs/friend-tags-timeline-requirements.md §6; ``beforeMs`` truncates samples, statistics, labels,
tags and shared events for backtests and leaves layers 3–4 out (they cannot be cut back in time).
"""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import random
import re
import time
import unicodedata
from collections import Counter
from statistics import median

import friend_chat
import friend_persona
from analysis_store import content_hash, now_ms
from friend_run_store import RunStore

VERSION = "friend-sim-v1"
GAP_MS = 30 * 60 * 1000
MAX_INPUT = 2400
MAX_REPLY = 1600
MAX_TURN = 8
TAIL = 6
VERIFY_TAIL = 400
MAX_MESSAGES = 6
NO_SAMPLE_COUNTS = (1, 1, 2)
MIN_BUBBLES = 3
MIN_SHARE = 5
MIN_NEVER = 50
FEW_SAMPLES = 50
QUERY_TURNS = 6
FOCUS = 6
PROFILE_BUDGET = 3600
FACT_CHARS = 120
MAX_MEMORIES = 12
FIRM = ("较有把握", "有一定依据")
SCHEMA = """
CREATE TABLE IF NOT EXISTS friend_sim_samples (
    id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    seq INTEGER NOT NULL,
    msg_id TEXT NOT NULL,
    time_ms INTEGER NOT NULL,
    input TEXT NOT NULL,
    reply TEXT NOT NULL,
    scene TEXT NOT NULL,
    terms TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS friend_sim_samples_user ON friend_sim_samples(user, time_ms);
CREATE TABLE IF NOT EXISTS friend_sim_state (
    user TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    source_id TEXT,
    epoch TEXT,
    upto_seq INTEGER NOT NULL,
    digest TEXT NOT NULL,
    carry TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
"""
# Copied from kyf.self-analysis self_samples.SCENES.
SCENES = {
    "邀约": ("一起", "有空", "吃饭", "见面", "周末", "几点", "约吗", "去不去"),
    "安慰": ("难受", "伤心", "好累", "失眠", "压力", "崩溃", "失恋", "不开心"),
    "感谢": ("谢谢", "感谢", "辛苦", "多谢"),
    "拒绝": ("不方便", "没空", "算了", "不用了", "不去了", "拒绝"),
    "问候": ("早安", "晚安", "早上好", "最近怎么样", "在吗", "你好"),
    "解释": ("为什么", "怎么回事", "什么意思", "原因", "解释"),
}
EMOJI = re.compile(r"\[[一-鿿A-Za-z]{1,6}\]|[\U0001F000-\U0001FAFF☀-➿⭐❤]")
END_PUNCT = re.compile(r"[\s。！!？?～~…\.,，、]+$")
PARTICLES = "啊呀吧呢嘛哈啦哦噢喔呗咯嘿嗯哇耶诶欸么嘞捏"
ADDRESS = ("亲爱的", "宝贝", "小可爱", "兄弟", "哥们", "老哥", "老铁", "姐妹", "老师", "老板", "大佬", "同学",
           "哥哥", "姐姐", "宝", "哥", "姐", "亲")
ADDRESS_AT = re.compile(r"^(%s)(?=[\s，,。！!？?～~]|$)|(%s)[\s。！!？?～~]*$" % (
    "|".join(ADDRESS), "|".join(ADDRESS)))
COMMON = {"我们", "你们", "他们", "这个", "那个", "自己", "就是", "可以", "一下", "然后", "不是", "没有", "什么", "怎么",
          "真的", "还是", "还有", "知道", "现在", "时候", "今天", "明天", "已经", "因为", "所以", "但是", "如果", "这样",
          "一个", "不要", "觉得", "应该", "这么", "那么", "我的", "你的", "一起", "是不", "不会", "不能", "我也", "你也",
          "我就", "你就", "我在", "你在", "我要", "你要", "我是", "你是", "有点", "好像", "感觉"}
PUNCT_ENDS = (("。", "句号"), ("！!", "感叹号"), ("？?", "问号"), ("～~", "波浪号"), ("…", "省略号"), ("，,", "逗号"))


def ensure_schema(connection):
    connection.executescript(SCHEMA)


# -- retrieval helpers, copied from kyf.self-analysis self_samples ---------------------------------
def scene(text):
    return max(SCENES, key=lambda key: sum(word in text for word in SCENES[key])) if any(
        word in text for words in SCENES.values() for word in words) else "闲聊"


def terms(text):
    """Chinese characters + adjacent bigrams and whole Latin words; no downloads."""
    text = unicodedata.normalize("NFKC", str(text)).casefold()[:6000]
    found = set(re.findall(r"[a-z0-9_]{2,40}", text))
    for run in re.findall(r"[㐀-鿿]+", text):
        found.update(run)
        found.update(run[i:i + 2] for i in range(len(run) - 1))
    return sorted(found)[:1600]


# -- sample library ----------------------------------------------------------------------------------
def _plain(row, user):
    """A mirror row that can stand in a sample, or ``None`` (a barrier), as in ``self_samples._plain``."""
    text = row.get("text")
    if (row.get("group_chat") or row.get("kind") != "text" or row.get("side") not in ("self", "other") or
            not isinstance(text, str) or not text.strip() or len(text) > MAX_REPLY or
            type(row.get("time_ms")) is not int or row["time_ms"] <= 0):
        return None
    try:
        raw = json.loads(row.get("raw") or "{}")
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    meta = raw.get("inputMeta") or {}
    if not isinstance(meta, dict):
        return None
    source = meta.get("source") if isinstance(meta.get("source"), dict) else {}
    if source.get("kind") not in (None, "wechat") or meta.get("conversationId") not in (None, user):
        return None
    if (isinstance(raw.get("media"), dict) or raw.get("type") not in (None, "", 1, "1", "text", "文本") or
            any(raw.get(flag) for flag in ("forward", "forwarded", "isForward", "recalled", "system", "quote")) or
            any(meta.get(flag) for flag in ("quote", "forward", "forwarded", "isForward")) or
            re.search(r"<(?:appmsg|sysmsg|refermsg|recordinfo)\b", text, re.I)):
        return None
    return {"id": row["msg_id"], "seq": row["seq"], "side": row["side"], "time": row["time_ms"], "text": text.strip()}


def _key(user, row):
    return hashlib.sha256(json.dumps([VERSION, user, row["id"]], ensure_ascii=False).encode()).hexdigest()


def _sample(pending, user):
    mine, replies = pending["input"], pending["reply"]
    if not mine or not replies or pending.get("overflow") or sum(len(row["text"]) for row in replies) > MAX_REPLY:
        return None
    lines = [row["text"] for row in mine]
    while len(lines) > 1 and sum(map(len, lines)) > MAX_INPUT:
        lines = lines[1:]
    text = "\n".join(lines)[-MAX_INPUT:]
    return {"id": _key(user, replies[0]), "seq": replies[0]["seq"], "msg_id": replies[0]["id"],
            "time_ms": replies[-1]["time"], "input": lines, "reply": [row["text"] for row in replies],
            "scene": scene(text), "terms": terms(text)}


def extract(rows, carry, user):
    """``self_samples.extract`` with the sides swapped; returns upserts/deletes and the new carry.

    The last reply run is provisional: a later friend row in the next refresh updates the same id.
    """
    carry = json.loads(json.dumps(carry or {"tail": [], "pending": None}))
    tail, pending = carry.get("tail", []), carry.get("pending")
    found = {}

    def emit():
        item = _sample(pending, user) if pending else None
        if item:
            found[item["id"]] = item

    for raw in rows:
        row = _plain(raw, user)
        if row is None:
            emit()
            tail, pending = [], None
            continue
        if tail and not 0 <= row["time"] - tail[-1]["time"] <= GAP_MS:
            emit()
            tail, pending = [], None
        if row["side"] == "self":
            emit()
            pending = None
        elif pending is not None:
            if not pending.get("overflow"):
                pending["reply"].append(row)
                # An overlong monologue is no style exemplar; drop it until my next message.
                if len(pending["reply"]) > MAX_TURN or sum(len(x["text"]) for x in pending["reply"]) > MAX_REPLY:
                    identifier = _key(user, pending["reply"][0])
                    found[identifier] = {"id": identifier, "delete": True}
                    pending["overflow"], pending["reply"] = True, pending["reply"][:1]
        elif tail and tail[-1]["side"] == "self":
            start = len(tail)
            while start and tail[start - 1]["side"] == "self":
                start -= 1
            pending = {"input": tail[start:], "reply": [row]}
        tail = (tail + [row])[-TAIL:]
    if pending and not pending.get("overflow"):
        emit()
    return list(found.values()), {"tail": tail, "pending": pending}


def _digest(database, user, upto):
    digest = hashlib.sha256()
    for row in database._rows("SELECT * FROM context_messages WHERE user=? AND seq<=? ORDER BY seq DESC LIMIT ?",
                              (user, upto, VERIFY_TAIL)):
        digest.update((row["msg_id"] + "\x00" + content_hash(row) + "\x00").encode("utf-8"))
    return digest.hexdigest()


def refresh(database, user):
    """Bring the sample library up to the mirror; a reset source, epoch or changed tail rebuilds it."""
    with database.lock:
        connection = database._connect()
        ensure_schema(connection)
        context = database.context(user) or {}
        state = database._row("SELECT * FROM friend_sim_state WHERE user=?", (user,))
        top = database._row("SELECT MAX(seq) AS top FROM context_messages WHERE user=?", (user,))["top"] or 0
        rebuild = (state is None or state["version"] != VERSION or state["source_id"] != context.get("source_id") or
                   state["epoch"] != context.get("epoch") or top < state["upto_seq"] or
                   state["digest"] != _digest(database, user, state["upto_seq"]))
        upto, carry = (0, None) if rebuild else (state["upto_seq"], json.loads(state["carry"]))
        if not rebuild and top == upto:
            return
        rows = database._rows("SELECT * FROM context_messages WHERE user=? AND seq>? ORDER BY seq", (user, upto))
        found, carry = extract(rows, carry, user)
        with connection:
            if rebuild:
                connection.execute("DELETE FROM friend_sim_samples WHERE user=?", (user,))
            for item in found:
                if item.get("delete"):
                    connection.execute("DELETE FROM friend_sim_samples WHERE id=?", (item["id"],))
                    continue
                connection.execute(
                    "INSERT OR REPLACE INTO friend_sim_samples(id,user,seq,msg_id,time_ms,input,reply,scene,terms) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (item["id"], user, item["seq"], item["msg_id"], item["time_ms"],
                     json.dumps(item["input"], ensure_ascii=False), json.dumps(item["reply"], ensure_ascii=False),
                     item["scene"], " ".join(item["terms"])))
            connection.execute(
                "INSERT OR REPLACE INTO friend_sim_state(user,version,source_id,epoch,upto_seq,digest,carry,"
                "updated_at_ms) VALUES(?,?,?,?,?,?,?,?)",
                (user, VERSION, context.get("source_id"), context.get("epoch"), top, _digest(database, user, top),
                 json.dumps(carry, ensure_ascii=False), now_ms()))


def _library(database, user, before_ms):
    refresh(database, user)
    before = before_ms if isinstance(before_ms, int) else 2 ** 62
    return database._rows("SELECT * FROM friend_sim_samples WHERE user=? AND time_ms<? ORDER BY time_ms DESC, id",
                          (user, before))


def _search(library, text, limit, before):
    """``self_retrieval.search``'s lexical score: term overlap, scene match and a small recency bonus."""
    tokens = sorted(terms(text), key=lambda token: (-len(token), token))[:128]
    weights = {token: 0.22 if len(token) == 1 else 1 for token in tokens}
    total, target = max(1, sum(weights.values())), scene(text)
    ranked = []
    for row in library:
        own = set(row["terms"].split())
        overlap = sum(weight for token, weight in weights.items() if token in own) / total
        age = max(0, before - row["time_ms"]) / 86400000
        score = overlap + (0.1 if target != "闲聊" and target == row["scene"] else 0) + 0.04 / (1 + math.log1p(age))
        ranked.append((-score, -row["time_ms"], row["id"], row))
    return [item[3] for item in sorted(ranked, key=lambda item: item[:3])[:limit]]


def _query(turns):
    return "\n".join(text for turn in turns[-QUERY_TURNS:] for text in turn.get("messages") or [])[-MAX_INPUT:]


def focus(turns):
    """My trailing turns: what the friend answers (``self_trial.focus`` with the sides swapped)."""
    start = len(turns)
    while start > 0 and turns[start - 1].get("side") == "me":
        start -= 1
    return turns[start:]


def samples(database, user, turns, limit=8, before_ms=None):
    """Focus samples for my last messages first, then the whole exchange fills the rest; no duplicates."""
    library = _library(database, user, before_ms)
    if not library or limit <= 0:
        return []
    before = before_ms if isinstance(before_ms, int) else now_ms()
    asked = focus(turns)
    focused = _search(library, _query(asked or turns), limit, before)
    rest = _search(library, _query(turns), limit, before) if len(asked) < len(turns) else []
    picked, seen = [], set()
    for row in focused[:FOCUS] + rest + focused[FOCUS:]:
        if len(picked) < limit and row["id"] not in seen:
            seen.add(row["id"])
            picked.append({"input": json.loads(row["input"]), "reply": json.loads(row["reply"]),
                           "timeMs": row["time_ms"], "seq": row["seq"], "id": row["id"], "messageId": row["msg_id"]})
    return picked


# -- style statistics ----------------------------------------------------------------------------------
def _pct(part, whole):
    return "%d%%" % round(100 * part / whole) if whole else "0%"


def _length(text):
    return len(EMOJI.sub("e", re.sub(r"\s+", "", text)))


def _phrases(texts):
    """Frequent 2–4 character runs (message frequency), a longer run winning over its parts."""
    counts = Counter()
    for text in texts:
        grams = set()
        for run in re.findall(r"[一-鿿]+", EMOJI.sub(" ", text)):
            grams.update(run[i:i + n] for n in (2, 3, 4) for i in range(len(run) - n + 1))
        counts.update(gram for gram in grams if gram not in COMMON)
    floor = max(3, math.ceil(len(texts) * 0.02))
    kept = [gram for gram, count in counts.items() if count >= floor and not any(
        gram in other and other != gram and counts[other] >= 0.7 * count for other in counts if len(other) > len(gram))]
    chosen = []
    for gram in sorted(kept, key=lambda gram: (-counts[gram], -len(gram), gram)):
        if not any(gram in other or other in gram for other in chosen):
            chosen.append(gram)
    return [{"text": gram, "count": counts[gram]} for gram in chosen[:6]]


def style(texts):
    """Expression statistics over the friend's plain text messages, oldest first."""
    total = len(texts)
    result = {"messages": total, "length": None, "particles": [], "phrases": [], "repeats": [], "emoji": None,
              "endings": [], "marks": {}, "address": [], "never": []}
    if not total:
        return result
    lengths = sorted(_length(text) for text in texts)
    result["length"] = {"median": median(lengths), "p90": lengths[min(total - 1, int(total * 0.9))],
                        "max": lengths[-1]}
    finals = Counter()
    for text in texts:
        bare = END_PUNCT.sub("", EMOJI.sub("", text))
        if bare and bare[-1] in PARTICLES:
            finals[bare[-1]] += 1
    result["particles"] = [{"text": key, "share": count / total} for key, count in finals.most_common(5)
                           if count >= 3 and count / total >= 0.03]
    result["phrases"] = _phrases(texts)
    repeats = Counter(END_PUNCT.sub("", text) for text in texts if _length(text) <= 8)
    result["repeats"] = [{"text": key, "count": count} for key, count in repeats.most_common(5) if key and count >= 3]
    emoji = Counter(code for text in texts for code in EMOJI.findall(text))
    with_emoji = sum(1 for text in texts if EMOJI.search(text))
    result["emoji"] = {"share": with_emoji / total, "top": [key for key, _count in emoji.most_common(5)]}
    endings = Counter()
    for text in texts:
        bare = EMOJI.sub("", text).rstrip()
        name = "表情" if not bare else next((label for marks, label in PUNCT_ENDS if bare[-1] in marks), None)
        endings[name or ("省略号" if bare.endswith("...") else "不加标点")] += 1
    result["endings"] = [{"label": key, "share": count / total} for key, count in endings.most_common()
                         if count / total >= 0.05]
    marks = {"句号": "。", "感叹号": "！!", "问号": "？?", "波浪号": "～~", "省略号": "…"}
    result["marks"] = {label: sum(1 for text in texts if any(mark in text for mark in chars)) / total
                       for label, chars in marks.items()}
    result["marks"]["省略号"] = sum(1 for text in texts if "…" in text or "..." in text) / total
    result["marks"]["空格断句"] = sum(1 for text in texts if re.search(r"[一-鿿] +[一-鿿]", text)) / total
    address = Counter()
    for text in texts:
        match = ADDRESS_AT.search(text.strip())
        if match:
            address[match.group(1) or match.group(2)] += 1
    you = sum(text.count("您") for text in texts)
    result["address"] = [{"text": key, "count": count} for key, count in address.most_common(3) if count >= 3]
    if you >= 3:
        result["address"].append({"text": "您", "count": you})
    if total >= MIN_NEVER:
        never = []
        if result["marks"]["句号"] and not any(EMOJI.sub("", text).rstrip().endswith("。") for text in texts):
            never.append("句末从不加句号")
        for label in ("句号", "感叹号", "波浪号", "省略号", "问号"):
            if not result["marks"][label]:
                never.append("从不用" + label)
        if not with_emoji:
            never.append("从不用表情（包括微信表情）")
        if not you:
            never.append("从不用“您”")
        if not any("哈哈" in text for text in texts):
            never.append("从不用“哈哈”")
        if not any(re.search(r"[A-Za-z]", text) for text in texts):
            never.append("从不夹英文字母")
        if lengths[-1] <= 50:
            never.append("单条从不超过 %d 字" % lengths[-1])
        result["never"] = never
    return result


def bubbles(rows):
    """Per round: how many messages ta sent after I sent n (n and counts capped at ``MAX_MESSAGES``)."""
    found, groups = friend_chat.blocks(rows), {}
    for position, block in enumerate(found):
        if block["side"] == "other" and block["reply_to"] is not None:
            mine = min(MAX_MESSAGES, len(found[position - 1]["rows"]))
            groups.setdefault(mine, []).append(min(MAX_MESSAGES, len(block["rows"])))
    return dict(sorted(groups.items()))


def _shares(values, labels):
    counted = Counter(value for value in values if value in labels)
    total = sum(counted.values())
    return {"total": total, "items": [{"label": label, "count": counted[label], "share": counted[label] / total}
                                      for label in sorted(labels, key=lambda label: -counted[label])
                                      if counted[label]]}


def tendencies(labelled, before_ms=None):
    """聊天分析逐轮标注 → overall response mix, P(response | my intent) and invite outcomes; ≥5 samples each."""
    before = before_ms if isinstance(before_ms, int) else 2 ** 62
    labelled = [item for item in labelled if (item.get("last_ms") or 0) < before]
    by_id = {item["block_id"]: item for item in labelled}
    answers = [(by_id[item["reply_to"]], item) for item in labelled
               if item["side"] == "other" and item.get("reply_to") in by_id]
    labels = friend_chat.RESPONSES[:-1]
    overall = _shares([answer["response"] for _mine, answer in answers], labels)
    situational = []
    for intent in friend_chat.INTENTS:
        group = _shares([answer["response"] for mine, answer in answers if mine["intent"] == intent], labels)
        if group["total"] >= MIN_SHARE:
            situational.append({"intent": intent, **group})
    responded = {answer["reply_to"] for _mine, answer in answers}
    outcomes = [answer["invite"] for mine, answer in answers if mine["intent"] == "邀请"]
    outcomes += ["未明确答复" for item in labelled
                 if item["side"] == "self" and item["intent"] == "邀请" and item["block_id"] not in responded]
    invites = _shares(outcomes, friend_chat.INVITES[:-1])
    return {"overall": overall if overall["total"] >= MIN_SHARE else None, "situational": situational,
            "invites": invites if invites["total"] >= MIN_SHARE else None}


# -- profile -----------------------------------------------------------------------------------------
RULES = [
    "你就是这位好友本人，在微信私聊里回复“我”；你不是 AI 助手，也不是在演戏。",
    "只说 ta 现实里会说的话。不要突然变得更温柔、更包容、更体贴、更会说话。",
    "没有依据时不主动道歉、示好、表白或许诺。",
    "简短回应、敷衍、反问、转移话题、婉拒、不接茬都是真实的回应，不必每句都接住、都回应到位。",
    "保留 ta 的棱角：嘴硬、毒舌、不耐烦、自顾自说，照原样保留，不要磨平。",
]


def _clip(text, limit=FACT_CHARS):
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _mix(items):
    return "、".join("%s %s" % (item["label"], "%d%%" % round(100 * item["share"])) for item in items)


def _day(ms):
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000)) if isinstance(ms, int) and ms > 0 else ""


def _expression_lines(stats, rounds):
    lines = []
    for mine, counts in rounds.items():
        if len(counts) < MIN_SHARE:
            continue
        tally = Counter(min(count, 3) for count in counts)
        parts = ["%s %s" % ("%d 条" % key if key < 3 else "3 条及以上", _pct(tally[key], len(counts)))
                 for key in sorted(tally)]
        lines.append("我一次发 %s时，ta 回：%s（%d 轮）" % ("%d 条" % mine if mine < MAX_MESSAGES else "6 条及以上",
                                                     "、".join(parts), len(counts)))
    if stats["length"]:
        lines.append("单条长度：中位数 %g 字，九成不超过 %d 字" % (stats["length"]["median"], stats["length"]["p90"]))
    if stats["particles"]:
        lines.append("句末语气词：" + "、".join("%s %s" % (item["text"], "%d%%" % round(100 * item["share"]))
                                          for item in stats["particles"]))
    if stats["phrases"]:
        lines.append("常用说法：" + "、".join(item["text"] for item in stats["phrases"]))
    if stats["repeats"]:
        lines.append("常发的短句：" + "、".join("“%s”" % item["text"] for item in stats["repeats"]))
    if stats["emoji"] and stats["emoji"]["share"] is not None:
        line = "表情：%s 的消息带表情" % _pct(stats["emoji"]["share"] * 100, 100)
        lines.append(line + ("，常用 " + " ".join(stats["emoji"]["top"]) if stats["emoji"]["top"] else ""))
    elif stats["emoji"] and stats["emoji"]["top"]:
        lines.append("常用表情：" + " ".join(stats["emoji"]["top"]))
    if stats["endings"]:
        lines.append("句末：" + _mix(stats["endings"]))
    if stats["marks"].get("空格断句", 0) >= 0.1:
        lines.append("常用空格代替逗号断句")
    if stats["address"]:
        lines.append("称呼我：" + "、".join("“%s”" % item["text"] for item in stats["address"]))
    return lines


def _response_lines(reply, trend):
    lines = []
    if reply and reply.get("rate") is not None:
        line = "24 小时内回复率 %s（%d 次）" % (_pct(reply["rate"] * 100, 100), reply["samples"])
        if reply.get("medianSeconds") is not None:
            line += "，回复时间中位数 %s" % (("%d 分钟" % round(reply["medianSeconds"] / 60))
                                     if reply["medianSeconds"] >= 60 else "%d 秒" % reply["medianSeconds"])
        lines.append(line)
    if trend["overall"]:
        lines.append("整体回应方式：%s（%d 轮）" % (_mix(trend["overall"]["items"]), trend["overall"]["total"]))
    for group in trend["situational"]:
        lines.append("我%s时：%s（%d 轮）" % (group["intent"], _mix(group["items"]), group["total"]))
    if trend["invites"]:
        lines.append("我邀请 ta 的结果：%s（%d 次）" % (_mix(trend["invites"]["items"]), trend["invites"]["total"]))
    return lines


def _persona_lines(store, user, persona):
    lines, stamp = [], persona["created_at_ms"] if persona else None
    for row in store.observations(user):
        label = friend_persona.LABELS.get(row.get("dimension") or "")
        note = "：" + _clip(row["note"], 60) if row.get("note") else ""
        lines.append("我的观察（最可信）%s：%s%s" % ("·" + label if label else "", _clip(row["judgement"]), note))
        stamp = max(stamp or 0, row.get("updated_at_ms") or 0) or stamp
    if persona:
        payload = persona["payload"]
        for key, label in (("mbti", "MBTI"), ("attachment", "依恋类型"), ("politics", "政治立场")):
            report = (payload.get("selfReports") or {}).get(key)
            if report and report.get("value"):
                lines.append("%s（自述，仅作参考）：%s" % (label, _clip(report["value"], 40)))
        dims = [item for item in payload.get("dimensions") or [] if item.get("confidence") in FIRM and item.get("label")]
        for item in sorted(dims, key=lambda item: FIRM.index(item["confidence"])):
            detail = "：" + _clip(item["detail"], 60) if item.get("detail") else ""
            lines.append("%s %s（%s）%s" % (friend_persona.LABELS.get(item["id"], item["id"]), item["label"],
                                          item["confidence"], detail))
        for value in payload.get("values") or []:
            label = friend_persona.LABELS.get(value["id"], value["id"])
            if (value.get("selfReport") or {}).get("text"):
                lines.append("%s（自述）：%s" % (label, _clip(value["selfReport"]["text"])))
            inference = value.get("inference") or {}
            if inference.get("confidence") in FIRM and inference.get("label"):
                lines.append("%s：%s（%s）" % (label, _clip(inference["label"]), inference["confidence"]))
    return lines, stamp


def _memory_lines(store, user, summary, skip=()):
    """Layer 4; memories already written after a tag (2a) or a shared event (2b) are skipped."""
    rows = sorted((row for row in store.memories(user, ("confirmed",)) if row.get("id") not in skip),
                  key=lambda row: row.get("updated_at_ms") or 0, reverse=True)
    lines, stamp = [], None
    for row in rows[:MAX_MEMORIES]:
        tags = json.loads(row.get("tags_json") or "[]")
        lines.append("%s%s" % (tags[0] + "：" if tags else "", _clip(row["text"])))
        stamp = max(stamp or 0, row.get("updated_at_ms") or 0) or stamp
    if summary and summary["payload"].get("summary"):
        lines.append("最近概况（%s）：%s" % (_day(summary["created_at_ms"]), _clip(summary["payload"]["summary"], 400)))
        stamp = max(stamp or 0, summary["created_at_ms"])
    return lines, stamp


def _without_hidden(stats, rounds, hidden):
    """Statistics minus the speaking habits the user deleted on the tags page (keys of ``friend_tags``)."""
    if not hidden:
        return stats, rounds
    stats = dict(stats)
    # The tags page shows a short message that is also a phrase once, under the phrase key.
    hidden = hidden | {"repeat:" + key[len("phrase:"):] for key in hidden if key.startswith("phrase:")}
    for field, kind in (("phrases", "phrase"), ("repeats", "repeat"), ("particles", "particle"),
                        ("address", "address")):
        stats[field] = [item for item in stats[field] if "%s:%s" % (kind, item["text"]) not in hidden]
    stats["endings"] = [item for item in stats["endings"] if "ending:" + item["label"] not in hidden]
    stats["never"] = [text for text in stats["never"] if "never:" + text not in hidden]
    if stats["emoji"]:
        stats["emoji"] = {"share": None if "emoji" in hidden else stats["emoji"]["share"],
                          "top": [code for code in stats["emoji"]["top"] if "emoji:" + code not in hidden]}
    if "mark:空格断句" in hidden:
        stats["marks"] = {**stats["marks"], "空格断句": 0}
    if "length" in hidden:
        stats["length"] = None
    return stats, {mine: counts for mine, counts in rounds.items() if "bubbles:%d" % mine not in hidden}


def _rules(never):
    return RULES + ["ta 的“从不”清单，必须遵守：" + "；".join(never)] if never else list(RULES)


def encode(database, store, user, before_ms=None):
    """The layered friend profile (§3.3) plus the numbers the simulation draws from."""
    count = len(_library(database, user, before_ms))
    context = database.context(user)
    rows = database.context_messages(user, context["read_count"]) if context else []
    if isinstance(before_ms, int):
        rows = [row for row in rows if isinstance(row.get("time_ms"), int) and 0 < row["time_ms"] < before_ms]
    plain = [row for row in (_plain(raw, user) for raw in rows) if row]
    stats = style([row["text"] for row in plain if row["side"] == "other"])
    rounds = bubbles(rows)
    clock = before_ms if isinstance(before_ms, int) else int(time.time() * 1000)
    rate = friend_chat.reply_rate(rows, now_ms=clock, history_ready=bool(context and context["state"] == "ready"))
    reply = {"rate": rate["rate"], "medianSeconds": rate["medianSeconds"], "samples": rate["eligible"]} \
        if rate["rate"] is not None else None
    runs = RunStore(store, now_ms)
    scan = runs.published_scan(user, "chat")
    persona, summary = runs.result(user, "persona"), runs.result(user, "summary")
    analysed = bool(scan or persona or summary)
    trend = tendencies(runs.chat_blocks(user, scan["generation"]) if scan else [], before_ms)
    stats.update(samples=count, tendencies=trend)
    backtest = isinstance(before_ms, int)
    tags, timeline = importlib.import_module("friend_tags"), importlib.import_module("friend_timeline_view")
    # The user's own speaking habits rank first; a backtest uses none of the user's edits (their time is unknown).
    hidden, added = tags.habit_edits(store, user) if not backtest else (set(), [])
    shown, shown_rounds = _without_hidden(stats, rounds, hidden)
    mine_never = [text for text in added if text.startswith("从不")]
    expression = ["我的补充（最可信）：" + text for text in added if not text.startswith("从不")]
    layers = [{"key": "rules", "title": "硬规则（不可违背）", "atMs": None,
               "lines": _rules(mine_never + [text for text in shown["never"] if text not in mine_never])},
              {"key": "expression", "title": "表达方式", "atMs": clock,
               "lines": expression + _expression_lines(shown, shown_rounds)},
              {"key": "response", "title": "回应倾向", "atMs": clock, "lines": _response_lines(reply, trend)}]
    written = set()
    lines, used, stamp = tags.profile(database, store, user, before_ms)
    if lines:
        layers.append({"key": "tags", "title": "ta 是什么样的人（标签）", "atMs": stamp, "lines": lines})
        written |= used
    lines, used, stamp = timeline.profile(database, user, before_ms, now=clock)
    if lines:
        layers.append({"key": "shared", "title": "我们的经历", "atMs": stamp, "lines": lines})
        written |= used
    warnings = []
    # Backtests (before_ms): persona, values, memories and overviews are the latest results, not as of then,
    # so layers 3–4 would leak the answer; leave them out instead of truncating them. Tags and shared events
    # carry the time of every line, so they are cut instead (see friend_tags.profile / friend_timeline_view.profile).
    omitted = ["persona", "memory"] if backtest else []
    if analysed and not omitted:
        lines, stamp = _persona_lines(store, user, persona)
        if lines:
            layers.append({"key": "persona", "title": "性格与三观", "atMs": stamp, "lines": lines})
        lines, stamp = _memory_lines(store, user, summary, written)
        if lines:
            layers.append({"key": "memory", "title": "关系记忆与概况", "atMs": stamp, "lines": lines})
        fingerprint = (context or {}).get("source_id")
        scans = [item for item in (scan, runs.published_scan(user, "persona")) if item]
        if any(item["source_fp"] != fingerprint for item in scans) or (scan and scan["to_ms"] and sum(
                1 for row in rows if (row.get("time_ms") or 0) > scan["to_ms"]) >= FEW_SAMPLES):
            warnings.append("知己分析待更新，档案可能没有反映最近的聊天")
    elif not analysed:
        warnings.append("未做知己分析，模拟可能不准")
    if count < FEW_SAMPLES:
        warnings.append("ta 回复我的样例%s，模拟可能不准" % ("只有 %d 条" % count if count else "还没有"))
    times = [row["time"] for row in plain]
    return {"analysed": analysed, "warning": "；".join(warnings) or None, "layers": layers, "style": stats,
            "bubbles": rounds, "replyRate": reply, "omittedLayers": omitted,
            "range": {"fromMs": min(times) if times else None, "toMs": max(times) if times else None,
                      "messages": len(rows), "samples": count}}


def prefix(profile, budget=PROFILE_BUDGET):
    """Prompt text: the hard rules whole, then the other layers in priority order cut to a prefix of ``budget``."""
    out, used = [], 0
    for layer in profile["layers"]:
        lines = ["【%s】" % layer["title"]] + list(layer["lines"])
        if layer["key"] == "rules":
            out += lines
            continue
        for line in lines:
            if used + len(line) > budget:
                return "\n".join(out)
            out.append(line)
            used += len(line)
    return "\n".join(out)


def bubble_count(profile, my_count, rng=random):
    """Upper bound for this reply: drawn from what ta sent after I sent as many; pooled if that group is thin."""
    groups = profile.get("bubbles") or {}
    key = max(1, min(MAX_MESSAGES, int(my_count or 1)))
    pool = groups.get(key) or groups.get(str(key)) or []
    if len(pool) < MIN_BUBBLES:
        pool = [count for counts in groups.values() for count in counts]
    return min(MAX_MESSAGES, rng.choice(pool)) if pool else rng.choice(NO_SAMPLE_COUNTS)


def reply_probability(profile):
    reply = profile.get("replyRate")
    return reply["rate"] if reply and reply.get("rate") is not None and reply.get("samples", 0) >= MIN_SHARE else None


# -- 分析与建议 (kyf.message-insights) --------------------------------------------------------------------
# Added 2026-11-09. Reply suggestions write as the user, so the friend's profile goes to them as a
# description (no role-play rules; ta's habits marked as ta's), and the user's own way of talking to
# this friend is counted from the same mirror with the same ``style`` statistics.
MY_RECENT = 1500
ADVISOR_TITLES = {"expression": "ta 的表达方式（这是 ta 的说话习惯，不是我的）", "response": "ta 的回应倾向"}


def advisor_prefix(profile, budget=PROFILE_BUDGET):
    """The friend profile as a third-person description: rules left out, layers in priority order."""
    out, used = [], 0
    for layer in profile["layers"]:
        if layer["key"] == "rules":
            continue
        for line in ["【%s】" % ADVISOR_TITLES.get(layer["key"], layer["title"])] + list(layer["lines"]):
            if used + len(line) > budget:
                return "\n".join(out)
            out.append(line)
            used += len(line)
    return "\n".join(out)


def my_bubbles(rows):
    """How many messages I sent each time I answered ta within ``GAP_MS`` (capped at ``MAX_MESSAGES``)."""
    found = friend_chat.blocks(rows)
    return [min(MAX_MESSAGES, len(block["rows"])) for position, block in enumerate(found)
            if position and block["side"] == "self" and found[position - 1]["side"] == "other" and
            not block["after_barrier"] and 0 <= block["first_ms"] - found[position - 1]["last_ms"] <= GAP_MS]


def advisor_mine(rows, user, budget=1200):
    """我对 ta 说话的方式: statistics over my latest plain text messages to this friend."""
    plain = [row for row in (_plain(raw, user) for raw in rows) if row]
    texts = [row["text"] for row in plain if row["side"] == "self"][-MY_RECENT:]
    if len(texts) < 2 * MIN_SHARE:
        return ""
    stats, counts = style(texts), my_bubbles(rows[-4 * MY_RECENT:])
    lines = ["【我对 ta 说话的方式（我最近发给 ta 的 %d 条文字消息）】" % len(texts)]
    if len(counts) >= MIN_SHARE:
        tally = Counter(min(count, 3) for count in counts)
        lines.append("我回 ta 时一次发：%s（%d 轮）" % ("、".join(
            "%s %s" % ("%d 条" % key if key < 3 else "3 条及以上", _pct(tally[key], len(counts))) for key in sorted(tally)),
            len(counts)))
    if stats["length"]:
        lines.append("单条长度：中位数 %g 字，九成不超过 %d 字" % (stats["length"]["median"], stats["length"]["p90"]))
    if stats["endings"]:
        lines.append("句末：" + _mix(stats["endings"]))
    marks = [(label, share) for label, share in stats["marks"].items() if label != "空格断句" and share >= 0.05]
    if marks:
        lines.append("消息里用到：" + "、".join("%s %s" % (label, _pct(share * 100, 100)) for label, share in marks))
    if stats["marks"].get("空格断句", 0) >= 0.1:
        lines.append("常用空格代替逗号断句")
    if stats["particles"]:
        lines.append("句末语气词：" + "、".join("%s %s" % (item["text"], _pct(item["share"] * 100, 100))
                                          for item in stats["particles"]))
    if stats["phrases"]:
        lines.append("常用说法：" + "、".join(item["text"] for item in stats["phrases"]))
    if stats["repeats"]:
        lines.append("常发的短句：" + "、".join("“%s”" % item["text"] for item in stats["repeats"]))
    if stats["emoji"] and stats["emoji"]["top"]:
        lines.append("表情：%s 的消息带表情，常用 %s" % (_pct(stats["emoji"]["share"] * 100, 100), " ".join(stats["emoji"]["top"])))
    if stats["address"]:
        lines.append("称呼 ta：" + "、".join("“%s”" % item["text"] for item in stats["address"]))
    if stats["never"]:
        lines.append("我的“从不”：" + "；".join(stats["never"]))
    out, used = [], 0
    for line in lines:
        if used + len(line) > budget:
            break
        out.append(line)
        used += len(line)
    return "\n".join(out)
