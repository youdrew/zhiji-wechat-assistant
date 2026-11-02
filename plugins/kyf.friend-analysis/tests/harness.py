"""In-process harness: Core's real services and the plugin's analysis engine, with a scripted model.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Plugin calls to ``/plugin-api/v1`` go straight to Core's PluginAPI routes and task messages go
straight to the engine, so the tests check the plugin contract without HTTP or extra processes.
"""
from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "bridge", ROOT / "plugin-sdk" / "python", ROOT / "plugins" / "kyf.friend-analysis" / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backend_service import Backend  # noqa: E402
from conversation_selection import ConversationSelectionStore  # noqa: E402
from core_accounts import AccountDataService  # noqa: E402
from core_history import HistoryService  # noqa: E402
from core_human import HumanService  # noqa: E402
from core_plugin_process import PluginUnavailable  # noqa: E402
from core_services import CoreServices  # noqa: E402
from core_tasks import TaskError, token_hash  # noqa: E402
from plugin_api import ApiError  # noqa: E402
from kyf_plugin import Cancelled, CoreClient, CoreError, ExecutionRevoked, TaskContext  # noqa: E402

import friend_pages  # noqa: E402
import scheme  # noqa: E402
from account import Accounts  # noqa: E402
from analysis_store import FriendStore, now_ms  # noqa: E402
from friend_analysis import FriendAnalysis  # noqa: E402
from friend_run_store import RunStore  # noqa: E402

PLUGIN = "kyf.friend-analysis"


class LocalClient(CoreClient):
    """CoreClient whose requests run Core's plugin-interface routes in this process."""

    def __init__(self, api, execution_token=None):
        super().__init__("http://in-process", "in-process", execution_token)
        self.api = api

    def for_execution(self, execution_token):
        return LocalClient(self.api, execution_token)

    def _request(self, method, path, query=None, body=None, stream=False):
        query = {key: str(value) for key, value in (query or {}).items() if value is not None}
        parts = [unquote(part) for part in path.strip("/").split("/") if part]
        headers = {"X-KYF-Execution": self.execution_token} if self.execution_token else {}
        try:
            result = self.api._route(PLUGIN, method, parts, query, headers, json.loads(json.dumps(body or {})))
        except (ApiError, TaskError) as exc:
            raise (ExecutionRevoked if exc.code == "execution-revoked" else CoreError)(exc.status, exc.code,
                                                                                     exc.message) from None
        if not isinstance(result, (dict, list)) and result is not None:
            result = list(result)
        return json.loads(json.dumps(result, ensure_ascii=False))


class Models:
    """The plugin's model client over a scripted runtime; the service follows the saved context size."""

    def __init__(self, runtime, model_store):
        self.runtime, self.model_store = runtime, model_store
        self.bindings = {}
        self.lock = threading.Lock()

    def token(self, action, digest, binding):
        with self.lock:
            if action == "add":
                self.bindings[digest] = dict(binding)
            else:
                self.bindings.pop(digest, None)

    def services(self, refresh=False):
        tokens = self.model_store.context_tokens
        return [{"id": "svc-test", "protocol": "chat_completions", "model": "synthetic-model", "contextTokens": tokens,
                 "stamp": "%064x" % tokens, "fingerprint": "fp-%d" % tokens, "url": "http://127.0.0.1:9/s/svc-test"}]

    def service(self):
        return self.services()[0]

    def generate(self, task, request, step=None):
        task.check()
        with self.lock:
            binding = self.bindings.get(token_hash(task.token))
        if binding is None or binding["execution"] != task.execution["id"]:
            raise ExecutionRevoked(409, "execution-revoked", "Synthetic execution is no longer live")
        try:
            result = self.runtime.generate(None, {"system": request["system"], "message": request["prompt"]},
                                           None, task.cancelled)
        except Exception:
            if task.cancelled.is_set():
                raise Cancelled() from None
            raise
        return {"text": result["text"], "usage": result.get("usage")}


class FakePlugin:
    def __init__(self, api, data_dir, models):
        self.core = LocalClient(api)
        self.data_dir = data_dir
        self.models = models


class InProcessProcess:
    """The process identity and transport seen by Core's actual task manager."""

    def __init__(self, harness):
        self.harness = harness
        self.token = "synthetic-plugin-process"
        self.closed = False

    def send(self, message):
        if self.closed:
            raise PluginUnavailable("plugin-unavailable", "Synthetic plugin is closed")
        self.harness.deliver(PLUGIN, message)


class Harness:
    def __init__(self, source, model_store, runtime, base, account, user):
        self.source, self.model_store, self.runtime = source, model_store, runtime
        self.account, self.user = account, user
        selection = ConversationSelectionStore(base / "selection")
        selection.set_all_selected(account, [user])
        self.backend = Backend(source, model_source_store=model_store, selection_store=selection)
        self.service = AccountDataService(source, base)
        self.backend._accounts = self.service
        self.core = CoreServices(self.backend, ROOT, "http://127.0.0.1:0", local=base / ".local",
                                 bundled_dir=ROOT / "plugins", local_dir=base / "none")
        self.core._history = HistoryService(self.service.stores, self.service.context)
        self.core._human = HumanService(self.service.stores, source=source)
        self.backend._core = self.core
        self.contexts = {}
        self.threads = []
        # Task messages Core would send to the plugin process run here instead.
        self.process = InProcessProcess(self)
        self.core.supervisor.ensure = self.ensure_process
        self.core.supervisor.running = self.running_process
        self.core.supervisor.send = self.send
        models = Models(runtime, model_store)
        self.core.tasks.token_listeners[:] = [models.token]
        self.plugin = FakePlugin(self.core.api, base / ".local" / "plugin-data" / PLUGIN, models)
        self.plugin.data_dir.mkdir(parents=True, exist_ok=True)
        self.accounts = Accounts(self.plugin)
        self.analysis = FriendAnalysis(self.plugin, self.accounts)
        friends = self.service.friends
        friends.on_opened = lambda account, user: self.auto(account, user)
        friends.on_changed = lambda account, user, _version: self.auto(account, user)

    # Core -> plugin ------------------------------------------------------------------------------------------
    def running_process(self, plugin):
        return self.process if plugin == PLUGIN and not self.process.closed else None

    def ensure_process(self, plugin, wait=True):
        process = self.running_process(plugin)
        if process is None:
            raise PluginUnavailable("plugin-unavailable", "Synthetic plugin is not running")
        return process

    def send(self, plugin, message, start=True):
        process = self.ensure_process(plugin) if start else self.running_process(plugin)
        if process is None:
            raise PluginUnavailable("plugin-unavailable", "Synthetic plugin is not running")
        process.send(message)

    def run(self, task, execution):
        context = TaskContext(self.plugin, task, execution)
        self.contexts[execution["id"]] = context

        def work():
            try:
                scheme.run_scheme(self.analysis, self.accounts, context)
            finally:
                self.contexts.pop(execution["id"], None)
                self.core.tasks._ended(PLUGIN, {"execution": execution["id"]})
        thread = threading.Thread(target=work, daemon=True, name="test-scheme-task")
        self.threads.append(thread)
        thread.start()

    def deliver(self, plugin, message, start=True):
        if message["type"] == "task.start":
            self.run(message["task"], message["execution"])
        elif message["type"] == "task.cancel":
            context = self.contexts.get(message["execution"])
            if context is not None:
                context.cancelled.set()

    def auto(self, account, user):
        scheme.auto_update(self.plugin, self.analysis, self.accounts, account, user, self.run)

    def close(self):
        for context in list(self.contexts.values()):
            context.cancelled.set()
        for thread in self.threads:
            thread.join(20)
        self.analysis.close()
        self.accounts.close()
        self.process.closed = True
        self.core.close()
        self.service.shutdown()

    # The calls the window makes ------------------------------------------------------------------------------
    def store(self):
        return FriendStore(self.accounts.account(self.account))

    def runs(self):
        return RunStore(self.store(), now_ms)


class PageService:
    def __init__(self, harness):
        self.h = harness
        self.analysis = harness.analysis

    def _scope(self, account, user):
        database = self.h.accounts.account(account)
        context = database.sync(user)
        return account, user, (context or {}).get("source_id"), database, FriendStore(database)


class Friends:
    """The former FriendService surface, now served by Core (human data) and the plugin (analysis)."""

    def __init__(self, harness):
        self.h = harness
        self.pages = PageService(harness)
        self.analysis = harness.analysis

    def open(self, account, user):
        return self.h.service.friends.open(account, user)

    def overview(self, account, user):
        return self.h.service.friends.overview(account, user)

    def save_settings(self, account, user, request):
        return self.h.service.friends.save_settings(account, user, request)

    def save_observation(self, account, user, request):
        return self.h.service.friends.save_observation(account, user, request)

    def page(self, account, user, name, value=None):
        builder = {"overview": friend_pages.overview, "persona": friend_pages.persona, "chat": friend_pages.chat}
        if name in builder:
            return builder[name](self.pages, account, user)
        if name == "persona-evidence":
            return friend_pages.persona_evidence(self.pages, account, user, value)
        if name == "round":
            return friend_pages.chat_round(self.pages, account, user, value)
        return friend_pages.message_context(self.pages, account, user, value)

    def analysis_status(self, account, user):
        return {"account": account, "user": user, **self.analysis.status(account, user, self.h.store())}

    def analysis_estimate(self, account, user, mode="update"):
        return self.analysis.estimate(account, user, self.h.store(), mode)

    def analysis_start(self, account, user, _request_id, mode):
        result = self.h.core.tasks.create(PLUGIN, kind="scheme", account=account, user=user, scheme="default",
                                          title="知己分析", params={"mode": mode})
        task_id = result["task"]["id"]
        run = None
        for _ in range(200):
            run = self.h.runs().run_for_request(user, task_id)
            if run is not None:
                break
            threading.Event().wait(0.01)
        return {"run": self.analysis._public_run(run, account) if run else None, "task": result["task"]}

    def _task(self, user, run_id):
        run = self.h.runs().run(run_id)
        return run["request_id"]

    def analysis_cancel(self, account, user, run_id):
        return {"task": self.h.core.tasks.stop(self._task(user, run_id))}

    def analysis_resume(self, account, user, run_id):
        return self.h.core.tasks.continue_task(self._task(user, run_id))
