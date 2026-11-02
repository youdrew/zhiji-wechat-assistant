"""Conservative route-only model identity and persisted scan validation.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Core's cacheFingerprint covers protocol, original endpoint, alias and context;
actualModel/backendId still participate here. A routing generation is not a model
revision. Legacy opaque fingerprints are accepted only after exact reconstruction.
No stored fingerprint or source record is rewritten by these helpers.
"""
from functools import lru_cache
import hashlib
import json
import re

import friend_chat
import friend_persona
from contracts import AdvisorError

MAX_LEGACY_ROUTE = 10_000


def _fingerprint(base, actual, backend, revision):
    value = json.dumps([base, [actual, backend, revision]], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def model_identity(service):
    """Return a proof descriptor only when it agrees with Core's opaque identity."""
    base, actual, backend, revision = (service.get(key) for key in
                                      ("cacheFingerprint", "actualModel", "backendId", "routeRevision"))
    if (not isinstance(base, str) or re.fullmatch(r"[0-9a-f]{64}", base) is None or
            not isinstance(actual, str) or not actual or not isinstance(backend, str) or not backend or
            type(revision) is not int or revision < 0 or
            service.get("fingerprint") != _fingerprint(base, actual, backend, revision)):
        return None
    return (base, actual, backend)


@lru_cache(maxsize=4)
def _legacy_fingerprints(identity):
    # Bounded once per actual identity, never per source row. Unknown revisions
    # outside this legacy range fail closed; exact current matches still work.
    return frozenset(_fingerprint(*identity, revision) for revision in range(MAX_LEGACY_ROUTE + 1))


def route_equivalent(previous, config):
    """A positive result proves the model identity, not a cache's inputs/algorithm."""
    identity = config.get("cacheIdentity")
    return bool(identity and isinstance(previous, str) and previous in _legacy_fingerprints(tuple(identity)))


def persona_payload_valid(evidence, rows, covered):
    """Replay all persisted fields, reversing only documented pole normalization."""
    indices = {row["msg_id"]: index + 1 for index, row in enumerate(rows)}
    for cached in evidence:
        try:
            ids, hashes = json.loads(cached["msg_ids"]), json.loads(cached["hashes"])
            channel = next(channel.key for channel in friend_persona.CHANNELS
                           if cached["id"].startswith("pe_" + channel.key + "_"))
            item = {key: cached[key] for key in ("dim", "lean", "value", "note")}
            if item["dim"] in friend_persona.POLES:
                low, high = friend_persona.POLES[item["dim"]]
                item["lean"] = {"low": low, "high": high, "mixed": "两者"}[item["lean"]]
            item["msgs"] = [indices[identifier] for identifier in ids]
            expected = {key: value for key, value in cached.items() if key not in ("user", "generation")}
            expected.update(msg_ids=ids, hashes=hashes)
            if (friend_persona.validate_evidence([item], rows, channel=channel) != [expected] or
                    any(indices[identifier] > covered for identifier in ids)):
                return False
        except (KeyError, ValueError, TypeError, StopIteration):
            return False
    return True


def chat_payload_valid(cached, rows, covered):
    """Verify original blocks, hashes and label relationships with current rules."""
    if not chat_prefix_closed(rows, covered):
        return False
    blocks = friend_chat.blocks(rows[:covered])
    numbers = {block["id"]: index + 1 for index, block in enumerate(blocks)}
    if {row["block_id"] for row in cached} != set(numbers):
        return False
    try:
        raw = [{"b": numbers[row["block_id"]], **{key: row[key] for key in
                ("intent", "emotion", "response", "invite")}} for row in cached]
        replay = {row["block_id"]: row for row in friend_chat.validate(raw, blocks, numbers)}
        for row in cached:
            expected = {key: value for key, value in row.items() if key not in ("user", "generation")}
            expected.update(msg_ids=json.loads(row["msg_ids"]), hashes=json.loads(row["hashes"]))
            if replay.get(row["block_id"]) != expected:
                return False
    except (KeyError, ValueError, TypeError, AdvisorError):
        return False
    return True


def chat_prefix_closed(rows, covered):
    """A reusable prefix cannot end inside a block or between an ask and reply."""
    if type(covered) is not int or not 0 <= covered <= friend_chat.scan_end(rows):
        return False
    if covered == 0:
        return True
    blocks = friend_chat.blocks(rows)
    if any(block["start"] < covered < block["end"] for block in blocks):
        return False
    previous = next((block for block in reversed(blocks) if block["end"] <= covered), None)
    following = next((block for block in blocks if block["start"] >= covered), None)
    return previous is None or following is None or following["reply_to"] != previous["id"]


def chat_repair_answers(cached, rows, covered, *, legacy=False):
    """Reuse only source-verified fields; no stored row or checkpoint is changed.

    v1 invented an invite unknown for a missing answer. That value has ambiguous
    provenance, so only that field must be asked again when copying into v2.
    """
    if not chat_prefix_closed(rows, covered):
        return {}
    blocks = {block["id"]: block for block in friend_chat.blocks(rows[:covered])}
    enums = {"intent": friend_chat.INTENTS, "emotion": friend_chat.EMOTIONS,
             "response": friend_chat.RESPONSES, "invite": friend_chat.INVITES}
    answers = {}
    for row in cached:
        block = blocks.get(row.get("block_id"))
        if block is None or not block["text"]:
            continue
        try:
            if (json.loads(row["msg_ids"]) != [item["msg_id"] for item in block["rows"]] or
                    json.loads(row["hashes"]) != [friend_chat.content_hash(item) for item in block["rows"]] or
                    any(row.get(key) != block[key] for key in ("side", "first_ms", "last_ms", "reply_to"))):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        accepted = {key: row[key] for key, choices in enums.items() if row.get(key) in choices}
        if legacy and accepted.get("invite") == "无法判断":
            accepted.pop("invite")
        answers[block["id"]] = accepted
    return answers


def chat_coverage(cached, rows, covered, *, legacy=False):
    """Shared completion classifier; missing keys are private block IDs, not labels."""
    answers = chat_repair_answers(cached, rows, covered, legacy=legacy)
    return friend_chat.answer_coverage(answers, friend_chat.blocks(rows[:covered]))
