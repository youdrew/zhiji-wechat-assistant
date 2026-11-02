"""Model input from the mirrored WeChat conversation, compacted only when it cannot fit.

Compaction is hierarchical: the oldest leaf segments are replaced by neutral
summaries which are cached per source range + compression version + model
fingerprint and produced single-flight, so concurrent assistants share them.
The full original history always stays in the projection; only the model input
is ever compacted.
"""
# Modified by Know Your Friends, 2026-10-09 (Apache-2.0): explicit regeneration
# refreshes source highwater after any older import and bypasses observed caches.
# Modified 2026-10-10: preserve media provenance and inject bounded visual evidence.
# Moved from bridge/advisor_context.py into the kyf.advisor plugin, 2026-10-13 (Apache-2.0): the projection import
# stays in Core (core_projection); the assembly works on the plugin's mirror and its own ``summaries`` table.
# Modified 2026-10-14 (Apache-2.0): sticker messages are written with their identity, caption and label.
# Modified 2026-10-16 (Apache-2.0): use adaptive summary guidance, retain source
# when a summary grows, and validate the complete context rather than a fixed cap.
# Modified 2026-10-16 (Apache-2.0): support byte budgets and stop summarizing as soon as all source ranges fit.
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from contextlib import nullcontext

from contracts import (
    COMPACT_CHUNK_CHARS, COMPRESSION_VERSION,
    RECENT_CONTEXT_PERCENT, MAX_COMPRESSION_LEVELS, AdvisorError, compress_key,
)
from media import MEDIA_BOUNDARY, row_media
from stickers import NOTE as STICKER_NOTE, label as sticker_label

_PLACEHOLDER = re.compile(r"\[[^\[\]\n]{0,12}\]")
_OLDER_HEADING = "[较早聊天记录（原文或摘要）]"


class ModelContext:
    """Builds one run's chat material and owns the single-flight summary locks."""

    def __init__(self):
        self.lock = threading.RLock()
        self._summary_locks = {}
        self._closed = False

    def close(self):
        with self.lock:
            self._closed = True
            self._summary_locks.clear()

    # -- model input assembly ------------------------------------------------
    def build_model_context(self, database, user, upto_seq, budget_chars, compactor, model_fp,
                            check=None, compact_budget=None, cancel=None, commit_guard=None,
                            media_observations=None, stickers=None, budget_bytes=None, budget_cost=None):
        """Assemble the model's WeChat context from ``database``'s mirror, compacting only when it cannot fit.

        ``stickers`` (message id -> known identity, caption and label) switches sticker messages from the
        unread-media placeholder to their sticker text; None keeps the placeholder.
        ``compactor(text, range_key, level, target_chars)`` receives a suggested
        summary size; any useful result is accepted if the full context can fit.
        ``budget_bytes`` overrides the legacy character ceiling for fit checks;
        source partitioning and cache identities remain character based.
        ``budget_cost`` may include managed-read formatting overhead; it is
        applied to the complete boundary and rendered context before any return.
        """
        segments = []
        boundary = MEDIA_BOUNDARY + "\n" if media_observations is not None else ""
        if stickers:
            boundary += STICKER_NOTE + "\n"
        budget_chars -= len(boundary)
        cost = budget_cost or _utf8_cost
        available = budget_chars if budget_bytes is None else budget_bytes - cost(boundary)
        self._check_active(check, cancel)
        if available <= (64 if budget_bytes is None else 0):
            if database.context_messages(user, upto_seq):
                raise AdvisorError("context-too-long", "模型窗口没有足够空间容纳聊天资料")
            return ""
        partition_budget = COMPACT_CHUNK_CHARS if budget_bytes is not None else budget_chars
        limit = max(64, min(COMPACT_CHUNK_CHARS, compact_budget or partition_budget)) - 1
        for row in database.context_messages(user, upto_seq):
            text = _format_line(row, stickers)
            if media_observations and row["msg_id"] in media_observations:
                text += "\n" + media_observations[row["msg_id"]]
            for offset in range(0, len(text), limit):
                piece = text[offset:offset + limit]
                digest = hashlib.sha256(piece.encode("utf-8")).hexdigest()
                key = "r%d:%d:%d:%s" % (row["seq"], offset, offset + len(piece), digest)
                segments.append({"key": key, "text": piece, "chars": len(piece) + 1,
                                 "level": 0, "coverage": [key]})
        if not segments:
            self._check_active(check, cancel)
            return ""
        return boundary + self._fit(segments, budget_chars, database, user, compactor, model_fp,
                                    check=check, compact_budget=limit, cancel=cancel, commit_guard=commit_guard,
                                    budget_bytes=budget_bytes, budget_cost=budget_cost,
                                    prefix=boundary if budget_bytes is not None else "")

    def _fit(self, segments, budget_chars, database, user, compactor, model_fp,
             check=None, compact_budget=None, cancel=None, commit_guard=None, budget_bytes=None,
             budget_cost=None, prefix=""):
        cost = (budget_cost or _utf8_cost) if budget_bytes is not None else len
        budget = budget_bytes if budget_bytes is not None else budget_chars

        def total(items):
            return sum(cost(item["text"]) + 1 for item in items)

        expected = [part for item in segments for part in item["coverage"]]

        def fitting(older, recent):
            self._check_active(check, cancel)
            if [part for item in older + recent for part in item["coverage"]] != expected:
                raise AdvisorError("compression-failed", "聊天摘要覆盖范围不完整")
            text = _render(older, recent)
            return text if cost(prefix + text) <= budget else None

        rendered = fitting([], segments)
        if rendered is not None:
            return rendered
        share = max(1, (budget - cost(prefix)) * RECENT_CONTEXT_PERCENT // 100)
        recent = []
        older = list(segments)
        consumed = 0
        while older:
            candidate = older[-1]
            candidate_size = total([candidate])
            if consumed + candidate_size > share:
                break
            older.pop()
            recent.insert(0, candidate)
            consumed += candidate_size
        stagnant_rounds = 0
        for _round in range(MAX_COMPRESSION_LEVELS):
            rendered = fitting(older, recent)
            if rendered is not None:
                return rendered
            if not older:
                break
            chunks = _chunk(older, compact_budget or COMPACT_CHUNK_CHARS)
            level = max(item["level"] for item in older) + 1
            # Reserve actual labels/newlines and recent material, then distribute
            # the remaining space by source size. This guides the model; it is
            # not a rejection threshold for an otherwise useful summary.
            labels = cost(prefix + _OLDER_HEADING) + len(chunks)
            if recent:
                labels += 1 + cost(_render([], recent))
            available = max(1, budget - labels - cost("[摘要] ") * len(chunks))
            source_size = total(older)
            summarized = []
            for index, chunk in enumerate(chunks):
                text = "\n".join(item["text"] for item in chunk)
                coverage = [part for item in chunk for part in item["coverage"]]
                allocation = available * total(chunk) // source_size
                target = max(1, allocation * len(text) // max(1, cost(text)) // (stagnant_rounds + 1))
                # A suggested length can change with the user's next question.
                # Cache useful summaries by source, not by that transient target.
                key = _summary_key(user, level, coverage, text)
                # Earlier versions included a fixed target in the cache key.
                # Reuse their useful results without preserving that limit.
                legacy_target = max(32, min(2000, budget_chars // 4))
                legacy_key = _summary_key(user, level, coverage, text, legacy_target)
                summary = self._summarize(database, user, key, level, text, compactor,
                                          model_fp, coverage, target, check, cancel, commit_guard,
                                          legacy_key=legacy_key, cost=cost)
                if summary is None:
                    # A model can elaborate instead of compressing. Keep every
                    # original range; helpful neighbouring chunks may still fit.
                    summarized.extend(chunk)
                else:
                    summarized.append({"key": "s:" + key, "text": "[摘要] " + summary,
                                       "chars": len(summary) + 6, "level": level, "coverage": coverage})
                # Preserve untouched source verbatim and avoid unnecessary model
                # calls once this one useful summary makes the full input fit.
                untouched = [item for rest in chunks[index + 1:] for item in rest]
                rendered = fitting(summarized + untouched, recent)
                if rendered is not None:
                    return rendered
            if total(summarized) >= total(older):
                stagnant_rounds += 1
                if stagnant_rounds >= 2:
                    raise AdvisorError("compression-failed", "模型连续未能缩短聊天资料，原始记录已保留，请重试整理")
            else:
                stagnant_rounds = 0
            older = summarized
        rendered = fitting(older, recent)
        if rendered is None:
            raise AdvisorError("compression-failed", "聊天资料多轮整理后仍未缩短到可用空间，原始记录已保留，请重试整理")
        return rendered

    def _summarize(self, database, user, key, level, text, compactor, model_fp,
                   coverage, target, check=None, cancel=None, commit_guard=None, legacy_key=None, cost=len):
        def useful(summary):
            return (isinstance(summary, str) and bool(summary.strip()) and
                    cost("[摘要] " + summary.strip()) < cost(text))

        def cached_summary():
            for cache_key in dict.fromkeys((key, legacy_key or key)):
                cached = database.summary(user, cache_key, COMPRESSION_VERSION, model_fp, level)
                if useful(cached):
                    return cached.strip()
            return None

        cached = cached_summary()
        if cached is not None:
            return cached
        lock = self._summary_lock(database.account, user, key, level, model_fp)
        while not lock.acquire(timeout=0.05):
            self._check_active(check, cancel)
        try:
            self._check_active(check, cancel)
            cached = cached_summary()
            if cached is not None:
                return cached
            summary = compactor(text, key, level, target)
            if not isinstance(summary, str) or not summary.strip():
                raise AdvisorError("compression-failed", "模型未返回摘要内容")
            summary = summary.strip()
            self._check_active(check, cancel)
            with commit_guard() if commit_guard is not None else nullcontext():
                with self.lock:
                    if self._closed or (cancel is not None and cancel.is_set()):
                        raise AdvisorError("stopping", "已停止")
                    if not useful(summary):
                        return None
                    database.save_summary(user, key, COMPRESSION_VERSION, model_fp, level, summary, coverage)
            return summary
        finally:
            lock.release()

    def _check_active(self, check, cancel):
        if check:
            check()
        with self.lock:
            if self._closed or (cancel is not None and cancel.is_set()):
                raise AdvisorError("stopping", "已停止")

    def _summary_lock(self, account, user, key, level, model_fp):
        with self.lock:
            identity = (account, user, key, level, model_fp)
            lock = self._summary_locks.get(identity)
            if lock is None:
                lock = threading.Lock()
                self._summary_locks[identity] = lock
            return lock


def _format_line(row, stickers=None):
    when = ""
    if isinstance(row.get("time_ms"), (int, float)) and row["time_ms"]:
        try:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["time_ms"] / 1000))
        except (OverflowError, OSError, ValueError):
            pass
    if row.get("side") == "self":
        speaker = "我"
    else:
        speaker = row.get("sender_name") or row.get("sender_id") or "对方"
        if row.get("group_chat") and row.get("sender_id"):
            speaker = speaker + " <" + row["sender_id"] + ">"
    text = (row.get("text") or "").replace("\r\n", "\n")
    media = row_media(row)
    if media and media["kind"] == "sticker" and stickers is not None:
        mark = sticker_label(stickers.get(row["msg_id"]))
        text = mark if not text.strip() or _PLACEHOLDER.fullmatch(text.strip()) else text + "\n" + mark
    elif media:
        text += "\n[媒体来源] " + json.dumps({"messageId": row["msg_id"], "kind": media["kind"]}, ensure_ascii=False)
        if media["kind"] in ("voice", "audio"):
            transcript = media.get("transcript")
            text += ("\n[微信已有语音转写，未听原音，可能有识别误差] " + json.dumps(transcript, ensure_ascii=False)
                     if transcript else "\n[语音内容未读取：微信暂无已有转写。]")
        else:
            text += "\n[图片/表情/视频占位；仅可依据随后明确提供的本轮视觉观察分析，若无观察则内容未读取。]"
    quote = json.loads(row.get("raw") or "{}").get("inputMeta", {}).get("quote")
    if quote is not None:
        text += "\n[引用] " + json.dumps(quote, ensure_ascii=False, separators=(",", ":"))
    return "[%s] %s: %s" % (when or "时间未知", speaker, text)


def _chunk(segments, limit):
    chunks = []
    current = []
    size = 0
    for segment in segments:
        if current and size + segment["chars"] > limit:
            chunks.append(current)
            current, size = [], 0
        current.append(segment)
        size += segment["chars"]
    if current:
        chunks.append(current)
    return chunks


def _summary_key(user, level, coverage, text, legacy_target=None):
    strategy = "adaptive-budget-v1" if legacy_target is None else legacy_target
    digest = hashlib.sha256(json.dumps([coverage, text, strategy], ensure_ascii=False).encode("utf-8")).hexdigest()
    return compress_key(user, level, digest, digest)


def _utf8_cost(text):
    return len(text.encode("utf-8"))


def _render(older, recent):
    parts = []
    if older:
        parts.append(_OLDER_HEADING)
        parts.extend(item["text"] for item in older)
    if recent:
        parts.append("[最近聊天记录]")
        parts.extend(item["text"] for item in recent)
    return "\n".join(parts)
