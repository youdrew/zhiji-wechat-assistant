"""Shared Core errors, constants and message-window types.

This module must not import services, adapters, or database repositories.
Modified by Know Your Friends, 2026-10-13 (Apache-2.0): the retired label and
portrait contracts were removed with the legacy analysis pipeline.
"""
from __future__ import annotations

from pathlib import Path

# The id the retired local analysis used; kept so saved scopes and caches stay readable.
LOCAL_SOURCE_ID = "local:laya"


class ModelSourceUnavailable(RuntimeError):
    """A model profile operation could not complete."""


MODEL_CONNECTOR_ERRORS = frozenset({
    "invalid-url", "invalid-request", "context-too-long", "auth", "rate-limit", "timeout", "unsupported",
    "network", "provider-error", "invalid-output", "response-too-large", "empty-response",
    "output-truncated",
})


def model_source_failure(error, fallback):
    code = str(error)
    return ModelSourceUnavailable(code if code in MODEL_CONNECTOR_ERRORS else fallback)


ROOT = Path(__file__).resolve().parents[1]


SYSTEM_NAMES = {"filehelper": "文件传输助手", "newsapp": "腾讯新闻", "brandsessionholder": "公众号消息"}


MAX_ISSUED_IMAGES = 2048


MAX_IMAGE_BYTES = 8 * 1024 * 1024


PROFILE_METADATA_CACHE_LIMIT = 64


QUOTED_REPLY_TYPE = (57 << 32) | 49


SESSION_PREVIEWS = {3: "[图片]", 34: "[语音]", 43: "[视频]", 47: "[表情]",
                    48: "[位置]", 49: "[文件/链接/卡片]", 11000: "[表情]"}


class AccountPreparationPending(RuntimeError):
    """A selected account is waiting for its bounded background preparation."""

    def __init__(self):
        super().__init__("active account preparation pending")


class AccountUnavailableError(RuntimeError):
    """The unique live WeChat account or its cache-only database is not ready."""

    def __init__(self):
        super().__init__("当前微信账号未就绪")


class AccountChangedError(RuntimeError):
    """A request or queued task outlived the account that supplied its input."""

    def __init__(self):
        super().__init__("当前微信账号已变化")


class MessagesUnavailableError(RuntimeError):
    def __init__(self):
        super().__init__("当前微信消息尚未就绪")


class MessageWindow(list):
    def __init__(self, items, has_more_before):
        super().__init__(items)
        self.has_more_before = has_more_before


class MessageWindowBatch(dict):
    def __init__(self, windows, has_more_before):
        super().__init__(windows)
        self.has_more_before = has_more_before
