"""Core's small shared vocabulary: scoped errors, scope values, projection snapshots, model budgets.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Split out of advisor_contracts.py and advisor_http.py, 2026-10-13: only what Core itself uses
(account data, 关系记忆, the projection and the model gateway). Standard library only.
"""
from __future__ import annotations

import hashlib
import json

MAX_SCOPE_VALUE = 256

# Characters are only ever a conservative proxy for provider tokens, never an exact count.
DEFAULT_CONTEXT_TOKENS = 32768
OUTPUT_RESERVED_TOKENS = 4096
THINKING_RESERVED_TOKENS = 24576
CHAR_PER_TOKEN_NUM = 55
CHAR_PER_TOKEN_DEN = 100

CONTEXT_STATES = ("idle", "reading", "ready", "error")

# Codes kept stable for the host routes (/api/friend/*) and the plugin interface.
ERROR_CODES = frozenset({
    "invalid-request", "agent-unknown", "agent-disabled", "builtin-agent",
    "skill-unknown", "skill-name-taken", "config-invalid", "config-full",
    "api-not-configured", "engine-unavailable", "account-changed",
    "account-paused", "account-closed", "context-error", "context-too-long",
    "compression-failed", "thread-unknown", "thread-agent-mismatch",
    "run-active", "run-unknown", "request-conflict", "stopping", "invalid-suggestions",
    "not-found", "rebuild-required", "invalid-output", "data-busy", "usage-busy",
})


class AdvisorError(RuntimeError):
    """A scoped failure with a stable machine code (name kept from the pre-plugin Advisor)."""

    def __init__(self, code, message=None):
        if code not in ERROR_CODES:
            raise ValueError("unknown error code: " + str(code))
        super().__init__(message or code)
        self.code = code
        self.message = message or code


def error_status(error):
    """HTTP status of a scoped error."""
    if error.code in {"agent-unknown", "thread-unknown", "run-unknown", "not-found"}:
        return 404
    if error.code in {"run-active", "account-paused", "account-closed", "agent-disabled", "request-conflict"}:
        return 409
    if error.code in {"api-not-configured", "engine-unavailable", "data-busy", "usage-busy"}:
        return 503
    return 400


def valid_scope_value(value, field):
    if (not isinstance(value, str) or not value or len(value) > MAX_SCOPE_VALUE or
            any(ord(char) < 32 or ord(char) == 127 for char in value)):
        raise AdvisorError("invalid-request", "invalid " + field)
    return value


def public_context(row):
    if row is None:
        return {"state": "idle", "revision": 0, "readCount": 0}
    snapshot = {"state": row["state"], "revision": row["revision"], "readCount": row["read_count"]}
    if row.get("total_count") is not None:
        snapshot["totalCount"] = row["total_count"]
    if row.get("error"):
        snapshot["error"] = row["error"]
    return snapshot


def effective_model_budget(context_tokens=None, model_metadata=None):
    """Resolve the one effective window. Counts below are budget estimates only.

    Provider metadata is optional; no model request is made to obtain a meter.
    """
    if context_tokens is None and isinstance(model_metadata, dict):
        context_tokens = model_metadata.get("contextTokens", model_metadata.get("context_length"))
    if context_tokens is None:
        context_tokens = DEFAULT_CONTEXT_TOKENS
    if type(context_tokens) is not int or not 4096 <= context_tokens <= 1000000:
        raise AdvisorError("invalid-request", "模型上下文窗口须在 4096 到 1000000 之间")
    output = min(OUTPUT_RESERVED_TOKENS + THINKING_RESERVED_TOKENS, max(512, context_tokens // 4))
    return {"contextTokens": context_tokens, "outputReservedTokens": output,
            "inputBudgetTokens": context_tokens - output - 256,
            "inputBudgetChars": max(0, (context_tokens - output - 256) * CHAR_PER_TOKEN_NUM // CHAR_PER_TOKEN_DEN)}


def model_fingerprint(protocol, base_url, model, context_tokens):
    """Model identity plugins bind cached results to (same value as before the plugin split)."""
    identity = json.dumps([protocol, base_url, model, context_tokens],
                          ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()
