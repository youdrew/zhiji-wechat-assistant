"""Human-owned friend data as plugins see it, and the intake of plugin memory candidates.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Confirmed memories, the contact goal, the user's own profile and observations
belong to the user. Plugins read them and may propose candidates; a candidate
stays pending until the user confirms it in the host's 关系记忆 tab.
"""
from __future__ import annotations

import json
import uuid

from friend_context import snapshot as friend_snapshot
from friend_store import TAGS, FriendStore, normalize_text, now_ms

MAX_EVIDENCE = 8
RELATIONS = (None, "supplement", "update", "conflict", "uncertain")


class CandidateError(ValueError):
    pass


class CandidateConflict(RuntimeError):
    """The friend's memories were cleared after the plugin read them."""


def ensure_plugin_column(connection):
    columns = {row[1] for row in connection.execute("PRAGMA table_info(memories)")}
    if "plugin" not in columns:
        connection.execute("ALTER TABLE memories ADD COLUMN plugin TEXT")


def _memory_view(row, evidence):
    return {"id": row["id"], "text": row["text"], "tags": json.loads(row["tags_json"]), "status": row["status"],
            "origin": row["origin"], "plugin": row.get("plugin"), "eventTime": row.get("event_time"),
            "version": row["version"], "relation": row.get("relation"), "relatedId": row.get("related_id"),
            "createdAt": row["created_at_ms"], "updatedAt": row["updated_at_ms"],
            "evidence": [{"msgId": item["msg_id"], "hash": item["content_hash"], "role": item["role"],
                          "time": item.get("time_ms"), "excerpt": item["excerpt"], "status": item["status"]}
                         for item in evidence.get(row["id"], [])]}


def _clean_candidate(item):
    if not isinstance(item, dict):
        raise CandidateError("候选格式不正确")
    text = item.get("text")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 300 or "\x00" in text:
        raise CandidateError("候选内容长度不正确")
    tags = item.get("tags")
    if not isinstance(tags, list) or not tags or any(tag not in TAGS for tag in tags):
        raise CandidateError("候选标签不在固定集合中")
    key = item.get("proposalKey")
    if not isinstance(key, str) or not 1 <= len(key) <= 128:
        raise CandidateError("候选去重键不正确")
    if item.get("relation") not in RELATIONS:
        raise CandidateError("候选关系不正确")
    evidence = item.get("evidence") or []
    if not isinstance(evidence, list) or len(evidence) > MAX_EVIDENCE:
        raise CandidateError("候选证据不正确")
    cleaned = []
    for entry in evidence:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(field), str) and entry.get(field)
                                                  for field in ("sourceFp", "msgId", "hash", "role")):
            raise CandidateError("候选证据不正确")
        cleaned.append({"source_fp": entry["sourceFp"][:128], "msg_id": entry["msgId"][:256],
                        "content_hash": entry["hash"][:128], "sort_key": entry.get("sortKey"),
                        "cursor": entry.get("cursor"), "role": entry["role"][:32],
                        "time_ms": entry.get("time") if type(entry.get("time")) is int else None,
                        "excerpt": str(entry.get("excerpt") or "")[:500]})
    event_time = item.get("eventTime")
    return {"text": text.strip(), "tags": [tag for tag in TAGS if tag in tags], "proposal_key": key,
            "relation": item.get("relation"), "related_id": item.get("relatedId"),
            "event_time": event_time if isinstance(event_time, str) and len(event_time) <= 10 else None,
            "first_mention_ms": item.get("firstMentionMs") if type(item.get("firstMentionMs")) is int else None,
            "model_fp": str(item.get("modelFingerprint") or "")[:128] or None, "evidence": cleaned}


class HumanService:
    def __init__(self, stores, on_change=None, source=None):
        self.stores = stores
        self.on_change = on_change
        self.source = source

    def rows(self, account, user):
        """The stored human-data rows, read under one account lock, for plugins that mirror Core's reads."""
        store = self._store(account)
        with store.database.lock:
            return {"state": store.state(user), "memories": store.memories(user, ("pending", "confirmed")),
                    "exclusions": store.exclusions(user), "goal": store.goal(user),
                    "profile": store.account_profile(), "observations": store.observations(user)}

    def snapshot(self, account, user):
        """Human data with Core-verified evidence states and a stamp that changes with any input."""
        value = friend_snapshot(self._store(account), user, self.source)
        return {"stamp": value["stamp"], "data": value["data"]}

    def _store(self, account):
        return FriendStore(self.stores.account(account))

    def self_profile(self, account):
        """Account-owned self description; no arbitrary friend id is required."""
        profile = self._store(account).account_profile()
        return {key: profile[key] for key in ("background", "style", "version", "updated_at_ms")}

    def save_self_profile(self, account, background, style, expected_version):
        self._store(account).save_account_profile(background, style, expected_version)
        result = self.self_profile(account)
        if self.on_change is not None:
            try:
                self.on_change(account, None, result["version"])
            except Exception:
                pass  # A notification failure cannot undo the user's saved edit.
        return result

    def read(self, account, user):
        store = self._store(account)
        store.write(ensure_plugin_column)
        state = store.state(user)
        rows = store.memories(user, ("pending", "confirmed"))
        evidence = store.evidence(row["id"] for row in rows)
        goal, profile = store.goal(user), store.account_profile()
        return {
            "version": state["memory_version"], "clearEpoch": state["clear_epoch"],
            "profileRevision": state["profile_revision"],
            "memories": [_memory_view(row, evidence) for row in rows if row["status"] == "confirmed"],
            "candidates": [_memory_view(row, evidence) for row in rows if row["status"] == "pending"],
            "excluded": [row["text"] for row in store.exclusions(user)],
            "goal": {"text": goal["goal"], "version": goal["version"]},
            "self": {"background": profile["background"], "style": profile["style"], "version": profile["version"]},
            "observations": [{"id": row["id"], "judgement": row["judgement"], "note": row["note"],
                              "dimension": row.get("dimension"), "version": row["version"]}
                             for row in store.observations(user)],
        }

    def submit_candidates(self, account, user, plugin, binding, candidates, links=None, expected_clear_epoch=None):
        # No cap on how many: a plugin proposes as many as it finds, and the user reviews them.
        if not isinstance(candidates, list):
            raise CandidateError("候选列表格式不正确")
        cleaned = [_clean_candidate(item) for item in candidates]
        if links is None:
            links = []
        if not isinstance(links, list) or not all(
                isinstance(link, dict) and isinstance(link.get("proposalKey"), str) and
                isinstance(link.get("text"), str) and link.get("targetKind") in ("memory", "exclusion", "candidate") and
                isinstance(link.get("targetId"), str) and isinstance(link.get("relation"), str) for link in links):
            raise CandidateError("去重关系不正确")
        if expected_clear_epoch is not None and type(expected_clear_epoch) is not int:
            raise CandidateError("记忆版本不正确")
        store = self._store(account)

        def action(connection):
            ensure_plugin_column(connection)
            if expected_clear_epoch is not None:
                row = connection.execute("SELECT clear_epoch FROM friend_state WHERE user=?", (user,)).fetchone()
                if (row[0] if row else 0) != expected_clear_epoch:
                    raise CandidateConflict()
            blocked = {row[0] for row in connection.execute(
                "SELECT norm FROM memory_exclusions WHERE user=? UNION SELECT norm FROM memories "
                "WHERE user=? AND status IN ('pending','confirmed')", (user, user))}
            stamp, inserted, skipped = now_ms(), [], 0
            for item in cleaned:
                norm = normalize_text(item["text"])
                if not norm or norm in blocked:
                    skipped += 1
                    continue
                blocked.add(norm)
                memory_id = "mem_" + uuid.uuid4().hex
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO memories(id,user,tags_json,text,norm,status,origin,event_time,"
                    "first_mention_ms,proposal_key,relation,related_id,job_id,model_fp,plugin,created_at_ms,"
                    "updated_at_ms) VALUES(?,?,?,?,?,'pending','ai',?,?,?,?,?,?,?,?,?,?)",
                    (memory_id, user, json.dumps(item["tags"], ensure_ascii=False), item["text"], norm,
                     item["event_time"], item["first_mention_ms"], item["proposal_key"], item["relation"],
                     item["related_id"], binding["task"], item["model_fp"], plugin, stamp, stamp))
                if not cursor.rowcount:
                    skipped += 1
                    continue
                FriendStore.insert_evidence(connection, memory_id, item["evidence"])
                inserted.append(memory_id)
            for link in links:
                connection.execute("INSERT OR IGNORE INTO memory_dedup(user,proposal_key,candidate_text,target_kind,"
                                   "target_id,relation,job_id,created_at_ms) VALUES(?,?,?,?,?,?,?,?)",
                                   (user, link["proposalKey"][:128], link["text"][:300], link["targetKind"],
                                    link["targetId"][:128], link["relation"][:32], binding["task"], stamp))
            if inserted:
                FriendStore.bump(connection, user)
            return inserted, skipped
        inserted, skipped = store.write(action)
        if inserted and self.on_change is not None:
            try:
                self.on_change(account, user, store.state(user)["memory_version"])
            except Exception:
                pass
        return {"inserted": inserted, "skipped": skipped}
