"""Synthetic Core API fixtures for the advisor plugin.

Plugin-only fixtures for Core API responses and gateway engine calls.
No Core implementation is imported.

Nothing in this module reads a real account, a real ``.local`` directory, the
network or a provider. Sources, model stores and runtimes are injected.
"""
from __future__ import annotations

import threading
import time

import base64
import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from kyf_plugin import CoreError, Plugin, TaskContext
from accounts import Accounts
from catalog_store import AgentConfigStore
from contracts import AdvisorError
from model_context import ModelContext

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


class SyntheticMessages:
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



class FakeModelStore:
    """Model service descriptors already resolved by Core; no provider access."""
    def __init__(self, mode="api", key="synthetic-key", context_tokens=8192, source_id="source-synthetic"):
        self.mode, self.key = mode, key
        self.context_tokens, self.source_id = context_tokens, source_id
        self.protocol, self.model = "chat_completions", "synthetic-model"
        self.retained = False

    def services(self, refresh=False):
        if self.mode == "broken" or (self.mode != "api" and not self.retained):
            return []
        identity = [self.source_id, self.model, self.key, self.context_tokens]
        stamp = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        return [{"id": self.source_id, "protocol": self.protocol, "model": self.model,
                 "url": "http://127.0.0.1:9/synthetic-gateway", "contextTokens": self.context_tokens,
                 "fingerprint": stamp, "stamp": stamp, "concurrency": 4, "roles": ["main"]}]

    def service_for(self, role, refresh=False):
        services = [item for item in self.services(refresh=refresh) if role in (item.get("roles") or [])]
        return services[0] if services else (self.services(refresh=refresh) or [None])[0]


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


class FakeCore(SyntheticMessages):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.statuses = {}
        self.prepared = {}
        self.snapshot = {"data": {"memories": [], "observations": [], "goal": "", "profile": {"background": "", "style": ""}, "proofs": {}, "exclusions": []}, "stamp": "human-v1"}
        self.grants = {}
        self.tasks = {}
        self.history_hook = None
        self.friend_list = []
        self.saved_settings = {"revision": 0, "values": {"includeMemories": True, "includeObservations": True}}
        self.model_settings = {"revision": 0, "values": {"maxConcurrentTasks": 4,
                               "searchSource": "none", "searxngUrl": "http://127.0.0.1:8080", "searchOpenPages": True}}

    def get(self, route, **query):
        if route == "settings" and not query:
            return json.loads(json.dumps(self.saved_settings))
        if route == "models/settings" and not query:
            return json.loads(json.dumps(self.model_settings))
        raise AssertionError("Unexpected synthetic Core route: " + route)

    def scope(self):
        return {"account": self.identity()[0]}

    def friends(self, account):
        self._check(account)
        return [dict(item) for item in self.friend_list]

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.identity()).encode()).hexdigest()

    def _check(self, account):
        if account != self.identity()[0]:
            raise CoreError(409, "account-changed", "synthetic account changed")

    def history_status(self, account, user, fresh=False):
        self._check(account)
        current = self.statuses.get(user)
        if current is None:
            return {"state": "idle", "revision": 0, "sourceFingerprint": self.fingerprint(), "epoch": "unprepared"}
        return {**current, "currentSourceFingerprint": self.fingerprint()}

    def history_prepare(self, account, user):
        self._check(account)
        if self.history_hook:
            self.history_hook()
        rows = sorted(self.users.get(user, []), key=lambda item: tuple(item["_sort"]))
        signature = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        previous = self.statuses.get(user, {})
        fingerprint = self.fingerprint()
        changed = previous.get("signature") != signature or previous.get("sourceFingerprint") != fingerprint
        prefix = self.prepared.get(user, [])
        append = rows[:len(prefix)] == prefix and previous.get("sourceFingerprint") == fingerprint
        epoch = previous.get("epoch") if append and previous else uuid.uuid4().hex
        self.prepared[user] = json.loads(json.dumps(rows))
        status = {"state": "ready", "revision": previous.get("revision", 0) + int(changed),
                  "readCount": len(rows), "totalCount": len(rows), "sourceFingerprint": fingerprint,
                  "epoch": epoch, "signature": signature}
        self.statuses[user] = status
        return dict(status)

    def history_messages(self, account, user, after=0, limit=500, **kwargs):
        self._check(account)
        self.page_calls.append({"user": user, "after": after, "page_size": limit})
        if self.page_delay:
            time.sleep(self.page_delay)
        if self.page_hook:
            self.page_hook(self)
        self._check(account)
        rows = self.prepared.get(user, [])
        messages = []
        for seq, row in enumerate(rows, 1):
            if seq <= after:
                continue
            raw = {key: row[key] for key in ("_sort", "type", "media", "quote") if key in row}
            if "media" in raw:
                from media import media_metadata
                raw["media"] = media_metadata(raw["media"])
            messages.append({"id": row["id"], "seq": seq, "sortKey": json.dumps(row["_sort"], separators=(",", ":")),
                             "side": row["side"], "senderId": row.get("senderId"), "senderName": row.get("senderName"),
                             "kind": row["kind"], "text": row["text"], "time": row["time"],
                             "group": user.endswith("@chatroom"), "raw": json.dumps(raw, ensure_ascii=False)})
            if len(messages) >= limit:
                break
        next_seq = messages[-1]["seq"] if messages and messages[-1]["seq"] < len(rows) else None
        return {"messages": messages, "next": next_seq}

    def post(self, route, body):
        if route == "history/verify":
            self._check(body["account"])
            from accounts import _hash
            messages = self.history_messages(body["account"], body["user"], limit=100000)["messages"]
            digests = {item["id"]: _hash({"sort_key": item["sortKey"], "side": item["side"],
                        "sender_id": item["senderId"], "kind": item["kind"], "text": item["text"]}) for item in messages}
            return {"results": [{"state": "same" if digests.get(item["id"]) == item["hash"] else "changed"}
                                for item in body["items"]]}
        if route.startswith("tasks/"):
            return {}
        raise AssertionError("Unexpected synthetic Core route: " + route)

    def human_snapshot(self, account, user):
        self._check(account)
        return json.loads(json.dumps(self.snapshot))

    def media(self, account, user, message_id, **kwargs):
        self._check(account)
        payload = self.media_analysis(user, message_id) if hasattr(self, "media_analysis") else {"error": "unsupported"}
        return wire_media(payload)

    def issue_grant(self, path):
        grant = "fg_" + uuid.uuid4().hex
        self.grants[grant] = {"path": str(path), "kind": "directory", "label": Path(path).name}
        return {"grant": grant}

    def claim_file(self, grant):
        return self.grants.pop(grant)

    def for_execution(self, token):
        return self

    def stop_task(self, task_id):
        self.tasks[task_id].cancelled.set()
        return {"state": "stopped"}


def wire_media(payload):
    result = dict(payload)
    result["items"] = [{**{key: value for key, value in item.items() if key != "bytes"},
                         **({"data": base64.b64encode(item["bytes"]).decode()} if "bytes" in item else {})}
                        for item in payload.get("items", [])]
    return result


class TestStores(Accounts):
    __test__ = False

    def __init__(self, root, plugin=None):
        plugin = plugin or SimpleNamespace(core=FakeCore(), data_dir=Path(root))
        super().__init__(plugin, root)
        self.config = AgentConfigStore(Path(root))


class TestProjection:
    __test__ = False

    def __init__(self, stores, source):
        self.stores, self.source = stores, source
        self.stores.plugin.core = source
        self.model = ModelContext()

    def prepare(self, account, user, **kwargs):
        self.source.history_prepare(account, user)
        self.stores.account(account).sync(user)
        return self.snapshot(account, user)

    def snapshot(self, account, user):
        return self.source.history_status(account, user)

    def messages(self, account, user):
        return self.stores.account(account).context_messages(user, None)

    def build_model_context(self, account, user, upto_seq, *args, **kwargs):
        return self.model.build_model_context(self.stores.account(account), user, upto_seq, *args, **kwargs)

    def source_scope(self, account):
        return self.source.fingerprint()

    def close(self):
        self.model.close()


class FakePlugin:
    def __init__(self, source, models, root):
        self.core = source
        self.models = models
        self.data_dir = Path(root)
        self.tasks = []
        self.threads = []
        self.contexts = {}
        self.sent = []
        self.logs = []

    def start_task(self, account, user, *, kind, title, params=None, handler):
        task = {"id": "task_" + uuid.uuid4().hex, "account": account, "user": user,
                "kind": kind, "title": title, "params": params or {}}
        context = TaskContext(self, task, {"id": "execution_" + uuid.uuid4().hex, "generation": 1, "token": "synthetic_execution_" + uuid.uuid4().hex})
        self.core.tasks[task["id"]] = context
        self.tasks.append(context)
        worker = threading.Thread(target=Plugin.run_task,
                                  args=(self, task, context.execution, handler, context), daemon=True)
        self.threads.append(worker)
        worker.start()
        return task, context

    def send(self, message):
        self.sent.append(message)

    def log(self, message, level="info"):
        self.logs.append((level, message))


class EngineAdapter:
    """Keep legacy deterministic engines while validating the new gateway call envelope."""
    def __init__(self, factory):
        self.factory = factory
        self.calls = []

    def available(self):
        return callable(self.factory)

    def __getattr__(self, method):
        runtime = self.factory()
        original = getattr(runtime, method)
        if method in {"respond", "compact", "inspect_media", "compact_session", "summarize_discussion"}:
            def model(service, token, request, callback, cancel):
                assert token.startswith("synthetic_execution_"), "Model call must carry its Core execution token"
                assert "key" not in service and "encryptedKey" not in service and "apiKey" not in service
                self.calls.append((method, token, dict(request)))
                return original(service, request, callback, cancel)
            return model
        return original


def build_service(source, models, root, runtime_factory=None):
    from service import AdvisorService
    plugin = FakePlugin(source, models, root)
    accounts = TestStores(root, plugin)
    engine = EngineAdapter(runtime_factory) if runtime_factory is not None else None
    service = AdvisorService(plugin, accounts, accounts.config, engine)
    return service


# Raw records are synthetic inputs to the fake Core contract, never WeChat sources.
FakeSource = FakeCore


def continued_task(plugin, previous, *, generation=2, user=None):
    task = {**previous.task, "user": previous.user if user is None else user}
    context = TaskContext(plugin, task, {"id": "execution_" + uuid.uuid4().hex, "generation": generation,
                                        "token": "synthetic_execution_" + uuid.uuid4().hex})
    plugin.core.tasks[task["id"]] = context
    return context
