"""Streaming conversation-to-reply pairs; carry state survives page boundaries.

Only attributable plain text is a style example. System/forward/media rows are
barriers, never treated as the account owner's prose. No inference is used.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import hashlib
import json
import re
import unicodedata

GAP_MS = 30 * 60 * 1000
MAX_INPUT = 2400
MAX_REPLY = 1600
MAX_TURN = 8
SCENES = {
    "邀约": ("一起", "有空", "吃饭", "见面", "周末", "几点", "约吗", "去不去"),
    "安慰": ("难受", "伤心", "好累", "失眠", "压力", "崩溃", "失恋", "不开心"),
    "感谢": ("谢谢", "感谢", "辛苦", "多谢"),
    "拒绝": ("不方便", "没空", "算了", "不用了", "不去了", "拒绝"),
    "问候": ("早安", "晚安", "早上好", "最近怎么样", "在吗", "你好"),
    "解释": ("为什么", "怎么回事", "什么意思", "原因", "解释"),
}


def scene(text):
    return max(SCENES, key=lambda key: sum(word in text for word in SCENES[key])) if any(
        word in text for words in SCENES.values() for word in words) else "闲聊"


def normalize(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())


def terms(text):
    """Chinese characters + adjacent bigrams and whole Latin words; no downloads."""
    text = unicodedata.normalize("NFKC", str(text)).casefold()[:6000]
    found = set(re.findall(r"[a-z0-9_]{2,40}", text))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        found.update(run)
        found.update(run[i:i + 2] for i in range(len(run) - 1))
    return sorted(found)[:1600]


def _plain(row, user, account=None):
    if (row.get("group") or row.get("kind") != "text" or row.get("side") not in ("self", "other") or
            not isinstance(row.get("text"), str) or not row["text"].strip() or len(row["text"]) > MAX_REPLY or
            type(row.get("time")) is not int or row["time"] <= 0):
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
    source_kind = (meta.get("source") or {}).get("kind")
    if source_kind not in (None, "wechat") or meta.get("conversationId") not in (None, user):
        return None
    sender = row.get("senderId") or meta.get("senderId")
    # Account is a database-directory identity, not necessarily the WeChat wxid.
    # Core's attributed side is authoritative; compare sender fields only to
    # each other, never guess a wxid from the directory/account identifier.
    if account and (meta.get("accountId") not in (None, account) or
                    (row.get("senderId") and meta.get("senderId") and row["senderId"] != meta["senderId"]) or
                    (row["side"] == "self" and not sender and not
                     (meta.get("accountId") == account and source_kind == "wechat"))):
        return None
    if (row.get("type") not in (None, "", 1, "1", "text", "文本") or
            any(raw.get(flag) for flag in ("forward", "forwarded", "isForward", "recalled", "system", "quote")) or
            any(meta.get(flag) for flag in ("quote", "forward", "forwarded", "isForward")) or
            raw.get("type") not in (None, "", 1, "1", "text", "文本") or
            re.search(r"<(?:appmsg|sysmsg|refermsg|recordinfo)\b", row["text"], re.I)):
        return None
    return {"id": row["id"], "seq": row["seq"], "side": row["side"], "time": row["time"],
            "text": row["text"].strip()[:MAX_REPLY], "hash": row.get("hash") or ""}


def _sample(pending, user, source, epoch):
    before, replies = pending["input"], pending["reply"]
    if not before or not replies or pending.get("overflow"):
        return None
    reply = "\n".join(row["text"] for row in replies)
    # A very long run is not truncated into a misleading style exemplar.
    if len(reply) > MAX_REPLY:
        return None
    prompt = "\n".join(("本人：" if row["side"] == "self" else "对方：") + row["text"] for row in before)
    prompt = prompt[-MAX_INPUT:]
    proof = before + replies
    key = [source, user, replies[0]["id"]]
    return {"id": hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest(), "user": user,
            "source": source, "epoch": epoch, "from_seq": min(row["seq"] for row in proof),
            "to_seq": replies[-1]["seq"], "time_ms": replies[-1]["time"], "input": prompt,
            "reply": reply, "scene": scene(prompt), "norm": normalize(reply),
            "evidence": [{"id": row["id"], "hash": row["hash"], "seq": row["seq"]} for row in proof],
            "terms": terms(prompt)}


def extract(rows, carry, user, source, epoch, account=None):
    """Return upsertable samples and a small durable carry; deterministic on retry.

    The last reply run is provisional: another self message on the next page
    updates the same sample ID instead of creating a duplicate.
    """
    carry = json.loads(json.dumps(carry or {"tail": [], "pending": None}))
    tail, pending = carry.get("tail", []), carry.get("pending")
    samples, skipped = {}, 0

    def emit():
        if pending:
            item = _sample(pending, user, source, epoch)
            if item:
                samples[item["id"]] = item

    for raw in rows:
        row = _plain(raw, user, account)
        if row is None:
            emit()
            tail, pending = [], None
            skipped += 1
            continue
        if tail and not 0 <= row["time"] - tail[-1]["time"] <= GAP_MS:
            emit()
            tail, pending = [], None
        if row["side"] == "other":
            emit()
            pending = None
        elif pending is not None:
            pending["reply"].append(row)
            # Bound checkpoint size for unusually long monologues; such runs are
            # deliberately excluded until a new incoming turn begins.
            if len(pending["reply"]) > MAX_TURN or sum(len(x["text"]) for x in pending["reply"]) > MAX_REPLY:
                identifier = hashlib.sha256(json.dumps(
                    [source, user, pending["reply"][0]["id"]], ensure_ascii=False).encode()).hexdigest()
                samples[identifier] = {"id": identifier, "delete": True}
                pending["overflow"] = True
                pending["reply"] = pending["reply"][:1]
        elif tail and tail[-1]["side"] == "other":
            pending = {"input": tail[-6:], "reply": [row]}
        tail = (tail + [row])[-6:]
        if pending and pending.get("overflow"):
            pending["reply"] = pending["reply"][:1]
    if pending and not pending.get("overflow"):
        emit()
    return list(samples.values()), {"tail": tail, "pending": pending}, skipped
