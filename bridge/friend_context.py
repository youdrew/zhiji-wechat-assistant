"""Verified snapshot of a friend's human data. No model inference or private file access.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Modified 2026-10-13 (plugin architecture): the prompt wording (HEADER, structured_sources,
render_section) moved to the plugins; Core keeps the snapshot and its stamp.
"""
from __future__ import annotations

import hashlib
import json
import time

from friend_store import content_hash


def message_role(row):
    """Who the message body speaks for: friend, self, forward or unknown."""
    raw = json.loads(row.get("raw") or "{}")
    kind_name = str(raw.get("type") or "")
    if "转发" in kind_name or "聊天记录" in kind_name:
        return "forward"
    if row.get("side") == "self":
        return "self"
    if row.get("side") == "other":
        return "friend"
    return "unknown"


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _raw(row):
    try:
        value = json.loads(row.get("raw") or "{}")
        return value if isinstance(value, dict) else None
    except (ValueError, TypeError):
        return None


def snapshot(store, user, source=None, cached=None):
    """One account-lock snapshot; batch original lookups and a short read cache.

    Revisions invalidate immediately. Recalled messages are checked on each read,
    including cache hits; query results never survive account/database replacement.
    The returned stamp covers effective inputs, not unrelated new chat messages.
    """
    database = store.database
    with database.lock:
        state, context = store.state(user), database.context(user)
        goal, profile, observations = store.goal(user), store.account_profile(), store.observations(user)
        evidence = store.evidence_for_user(user)
        recall = getattr(source, "message_recalled", None)
        recalled = {}
        if callable(recall):
            for message_id in {item["msg_id"] for item in evidence}:
                try:
                    recalled[message_id] = recall(user, message_id) is True
                except Exception:
                    recalled[message_id] = None  # Unverifiable evidence must not authorize a candidate.
        key = _digest([id(database), user, state, goal, profile, observations,
                       [(item["memory_id"], item["ordinal"], item["status"], item["role"],
                         item["source_fp"], item["msg_id"], item["content_hash"]) for item in evidence],
                       context, recalled])
        if cached and cached["key"] == key and time.monotonic() - cached["loaded"] < 5:
            return cached
        memories = store.memories(user, ("confirmed", "pending"))
        active = {row["id"] for row in memories}
        evidence = [item for item in evidence if item["memory_id"] in active]
        ids = sorted({item["msg_id"] for item in evidence})
        originals = {}
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            marks = ",".join("?" for _ in batch)
            for row in database._rows("SELECT * FROM context_messages WHERE user=? AND msg_id IN (" + marks + ")",
                                      (user, *batch)):
                originals[row["msg_id"]] = row
        proofs = {}
        for item in evidence:
            row = originals.get(item["msg_id"])
            status = item["status"]
            if context is None or context["state"] != "ready":
                status = "unready"
            elif context["source_id"] != item["source_fp"]:
                status = "source-changed"
            elif row is None:
                status = "missing"
            elif recalled.get(item["msg_id"], False) is not False:
                status = "recalled" if recalled[item["msg_id"]] else "unready"
            elif content_hash(row) != item["content_hash"]:
                status = "changed"
            parsed = _raw(row) if row else None
            raw = parsed or {}
            meta = raw.get("inputMeta")
            valid_meta = meta is None or isinstance(meta, dict)
            owned = bool(row and parsed is not None and valid_meta and
                         row.get("sender_id") == user and not row.get("group_chat") and
                         message_role(row) == "friend" and item["role"] == "friend")
            quote = bool(meta.get("quote")) if isinstance(meta, dict) else False
            media = raw.get("media")
            text = (row.get("text") or "") if row else ""
            if isinstance(media, dict):
                text = (media.get("transcript") or "") if media.get("kind") in ("voice", "audio") and media.get("transcriptSource") == "wechat" else ""
            if not isinstance(text, str):
                text = ""
            proof = {"status": status, "owned": owned, "quoted": quote, "messageId": item["msg_id"],
                     "excerpt": text[:240] if status == "ok" and owned and not quote else "",
                     "voice": isinstance(media, dict), "currentHash": content_hash(row) if row else None}
            proofs.setdefault(item["memory_id"], []).append(proof)
        data = {"memories": memories, "proofs": proofs, "goal": goal["goal"], "profile": profile,
                "observations": observations, "exclusions": [item["norm"] for item in store.exclusions(user)]}
        # Do not use only profile_revision: pending records and observations have
        # independent mutation paths, and evidence can change without that bump.
        stamp = "friend-v2-" + _digest([user, state["memory_version"], state["profile_revision"], data])
        return {"key": key, "loaded": time.monotonic(), "data": data, "stamp": stamp}
