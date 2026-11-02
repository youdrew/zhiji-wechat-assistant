"""Errors and budget arithmetic shared by the 知己分析 plugin modules.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Moved from Core (advisor_contracts) into kyf.friend-analysis, 2026-10-13: only what the
analysis uses. Counts are estimates, never exact token meters.
"""
from __future__ import annotations

DEFAULT_CONTEXT_TOKENS = 32768
OUTPUT_RESERVED_TOKENS = 4096
THINKING_RESERVED_TOKENS = 24576
CHAR_PER_TOKEN_NUM = 55
CHAR_PER_TOKEN_DEN = 100
SAFETY_MARGIN_PERCENT = 10


class AdvisorError(Exception):
    """A user-visible failure with a stable code (name kept from the original modules)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def effective_model_budget(context_tokens=None, model_metadata=None):
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


def safety_margin(chars):
    return max(512, chars * SAFETY_MARGIN_PERCENT // 100)


def friendly_error(exc):
    if isinstance(exc, AdvisorError):
        return exc.message
    code = getattr(exc, "code", None) or str(exc)
    return {"timeout": "模型响应超时，可以继续", "auth": "模型服务认证失败，请检查模型设置",
            "rate-limit": "模型服务请求受限，请稍后继续",
            "context-too-long": "模型拒绝了当前分段长度，请调大上下文设置后继续",
            "output-truncated": "模型输出过长被截断，可以继续", "provider-error": "模型服务响应失败，可以继续",
            "invalid-request": "模型请求格式不正确", "api-not-configured": "尚未配置生成模型，请先在设置中保存模型",
            "network": "无法连接模型服务，可以继续", "empty-response": "模型没有返回内容，可以继续",
            }.get(code, getattr(exc, "message", None) or "模型生成失败，请检查模型设置后继续")
