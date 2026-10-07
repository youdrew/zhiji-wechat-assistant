"""Synthetic fakes shared by the Advisor focused tests.

Nothing in this module reads a real account, a real ``.local`` directory, the
network or a provider. Sources, model stores and runtimes are injected.
"""
from __future__ import annotations

import threading
import time

from backend_contracts import LOCAL_SOURCE_ID, ModelSourceUnavailable
from history_browser import encode_cursor


def make_message(seq, shard="message__message_1.db", local_id=1, side="other",
                 sender="wxid_friend", name="朋友", text=None, time_ms=None,
                 kind="text"):
    return {
        "id": "msg-%s-%s-%s" % (shard, seq, local_id),
        "side": side,
        "text": text if text is not None else "合成消息 %d" % seq,
        "kind": kind,
        "time": time_ms if time_ms is not None else 1_700_000_000_000 + seq * 60_000,
        "type": "文本",
        "senderId": sender,
        "senderName": name,
        "senderAvatar": "",
        "senderAvatarCandidates": [],
        "_sort": [seq, shard, local_id],
    }


class FakeSource:
    def __init__(self, account="synthetic-account-1", workdir="C:/synthetic-workdir",
                 page_size=256):
        self.account = str(account)
        self.workdir = str(workdir)
        self.page_size = page_size
        self.users = {}
        self.page_calls = []
        self.identity_calls = 0
        self.identity_hook = None
        self.page_hook = None
        self.page_delay = 0.0

    # -- fixtures ------------------------------------------------------------
    def add_messages(self, user, messages):
        self.users.setdefault(user, []).extend(messages)

    def seed_pages(self, user, count, *, start_seq=1, shards=None, text_size=16):
        """Seed ``count`` messages across interleaved shards (round-robin)."""
        shards = shards or ["message__message_1.db", "message__message_2.db",
                            "message__message_3.db"]
        for index in range(count):
            shard = shards[index % len(shards)]
            self.add_messages(user, [make_message(
                start_seq + index, shard=shard, local_id=index + 1,
                text=("消息内容 %d " % (start_seq + index)) + "x" * text_size)])

    # -- source contract -----------------------------------------------------
    def identity(self):
        self.identity_calls += 1
        if self.identity_hook is not None:
            return str(self.identity_hook()), self.workdir
        return self.account, self.workdir

    def history_highwater(self, user):
        messages = sorted(self.users.get(user, []), key=lambda item: tuple(item["_sort"]))
        return tuple(messages[-1]["_sort"]) if messages else None

    def history_page(self, user, highwater, after=None, page_size=256):
        self.page_calls.append({"user": user, "highwater": tuple(highwater) if highwater else None,
                                "after": tuple(after) if after else None,
                                "page_size": page_size})
        if self.page_delay:
            time.sleep(self.page_delay)
        if self.page_hook is not None:
            self.page_hook(self)
        messages = sorted(self.users.get(user, []), key=lambda item: tuple(item["_sort"]))
        page = []
        for message in messages:
            if highwater is not None and tuple(message["_sort"]) > tuple(highwater):
                break
            if after is not None and tuple(message["_sort"]) <= tuple(after):
                continue
            rendered = dict(message)
            rendered.setdefault("historyCursor", encode_cursor(self.account, user, tuple(message["_sort"])))
            page.append(rendered)
            if len(page) >= page_size:
                break
        next_after = tuple(page[-1]["_sort"]) if page else None
        return page, next_after


class FakeModelStore:
    def __init__(self, mode="api", key="synthetic-key", context_tokens=8192, source_id="source-synthetic"):
        self.mode = mode
        self.key = key
        self.context_tokens = context_tokens
        self.source_id = source_id

    def saved_selection(self):
        if self.mode == "broken":
            raise ModelSourceUnavailable("synthetic unavailable")
        if self.mode != "api":
            return {"selectedMode": "local", "api": None, "sourceId": LOCAL_SOURCE_ID}
        return {
            "selectedMode": "api",
            "sourceId": self.source_id,
            "api": {"protocol": "chat_completions", "baseUrl": "http://127.0.0.1:9",
                    "model": "synthetic-model", "contextTokens": self.context_tokens,
                    "encryptedKey": "cipher-text"},
        }

    def resolve_key(self, protocol, base_url, supplied_key):
        return self.key


class FakeRuntime:
    """Small deterministic runtime honoring the injected respond/compact contract."""

    def __init__(self, text="这是合成建议正文。", session="sess-1", compact_prefix="摘要"):
        self.text = text
        self.session = session
        self.compact_prefix = compact_prefix
        self.respond_calls = []
        self.compact_calls = []
        self.pause_after_first_chunk = False
        self.paused = threading.Event()
        self.finish_gate = None
        self.closed = 0
        self.account_shutdowns = []
        self.compact_gate = None

    def respond(self, config, request, on_event, cancel_event):
        self.respond_calls.append((config, request))
        on_event({"type": "reasoning", "text": "正在思考"})
        first = self.text[:3]
        if first:
            on_event({"type": "text", "text": first})
        if self.pause_after_first_chunk:
            self.paused.set()
            deadline = time.monotonic() + 10
            while not cancel_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.005)
            return {"text": first, "runtimeSessionId": self.session}
        remaining = self.text[3:]
        if remaining:
            on_event({"type": "text", "text": remaining})
        if self.finish_gate is not None:
            self.finish_gate.wait(10)
        return {"text": self.text, "runtimeSessionId": self.session}

    def compact(self, config, request, on_event, cancel_event):
        self.compact_calls.append(dict(request))
        if self.compact_gate is not None:
            self.compact_gate()
        base = self.compact_prefix + str(request.get("level", 1)) + "-"
        return {"text": base + request.get("text", "")[:32]}

    def close(self):
        self.closed += 1

    def shutdown_account(self, account):
        self.account_shutdowns.append(account)


def wait_until(predicate, timeout=10.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return predicate()
