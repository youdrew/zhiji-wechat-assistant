"""Bounds and public failures for the local reply-style library.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import re
import time

PAGE_SIZE = 500
MAX_QUERY_CHARS = 4000
MAX_CONTEXT_CHARS = 12000
DEFAULT_CONTEXT_CHARS = 6000
MAX_SOURCES = 8
ELLIPSIS = re.compile(r"…+|\.{3,}")
QUOTE_RULE = "quote必须逐字复制对应消息的text；确需省略中间内容时用……标注，且省略前后各段仍必须逐字出现、顺序一致，不得摘自context或messageFrame"


class SelfError(Exception):
    def __init__(self, code, message, detail=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.detail = detail


def quote_verified(text, quote):
    """A quote is verbatim; an ellipsis may stand for one elided span, segments in order."""
    if quote in text:
        return True
    segments = [segment for segment in ELLIPSIS.split(quote) if segment]
    if len(segments) < 2:
        return False
    position = 0
    for segment in segments:
        index = text.find(segment, position)
        if index < 0:
            return False
        position = index + len(segment)
    return True


class Stopped(Exception):
    pass


def now_ms():
    return int(time.time() * 1000)


def integer(value, default, minimum, maximum):
    if value is None:
        return default
    if type(value) is not int or not minimum <= value <= maximum:
        raise SelfError("invalid-request", "参数超出允许范围")
    return value


def account_scope(params, context):
    account = context.get("account") or params.get("account")
    if not isinstance(account, str) or not 1 <= len(account) <= 256:
        raise SelfError("invalid-request", "请先选择微信账号")
    if context.get("account") and params.get("account") not in (None, context["account"]):
        raise SelfError("invalid-request", "账号与当前页面不一致")
    return account
