"""Chat assistant and assistant repository entry point (plugin contract v1).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Modified 2026-10-16: strict scoped message queue RPCs.
"""
from __future__ import annotations

import threading

from accounts import Accounts
from catalog_store import AgentConfigStore
from contracts import AdvisorError, valid_identifier, valid_scope_value
from engine_client import EnginePool
from kyf_plugin import CoreError, Plugin, RpcError
from service import AdvisorService

plugin = Plugin()
state = {}
service_lock = threading.Lock()


def advisor():
    with service_lock:
        if "service" not in state:
            accounts = Accounts(plugin)
            engine = EnginePool(lambda account: accounts.directory(account) / "engine")
            service = AdvisorService(plugin, accounts, AgentConfigStore(plugin.data_dir), engine)
            state["service"] = service
            plugin.on_shutdown.append(service.shutdown)
        return state["service"]


def scoped(context):
    return (valid_scope_value(context.get("account"), "account"),
            valid_scope_value(context.get("user"), "user"))


def dispatch(method, params, context):
    """Scope is supplied by the host, never by parameters in the plugin page."""
    if not isinstance(params, dict) or not isinstance(context, dict):
        raise RpcError("invalid-request", "请求参数不正确")
    if method not in METHOD_FIELDS:
        raise RpcError("unknown-method", "插件不支持该操作")
    if set(params) - METHOD_FIELDS[method]:
        raise RpcError("invalid-request", "请求包含不支持的参数")
    if not REQUIRED_FIELDS.get(method, set()) <= set(params):
        raise RpcError("invalid-request", "请求参数不正确")
    try:
        service = advisor()
        if method == "catalog.get":
            return service.catalog()
        catalog_actions = {"catalog.saveAgent": "save", "catalog.enableAgent": "enable",
                           "catalog.deleteAgent": "delete"}
        if method in catalog_actions:
            return service.save_template({**params, "action": catalog_actions[method]})
        if method in ("catalog.importSkill", "catalog.deleteSkill"):
            return service.import_skill({**params, "action": "import" if method == "catalog.importSkill" else "delete"})
        if method == "import.preview":
            return service.preview_assistant_import(params.get("source"))
        if method == "import.cancel":
            return service.cancel_assistant_import(params.get("previewId"))
        if method == "import.commit":
            return service.commit_assistant_import(params.get("previewId"), params.get("requestId"),
                params.get("agent"), params.get("acceptPartial", False))
        account, user = scoped(context)
        if method == "stickers.start":
            return service.stickers.start_requested(account, user, params.get("completionRequestId"))
        agent_id, thread_id = params.get("agentId"), params.get("threadId")
        request_id = params.get("requestId")
        if method == "thread.get":
            return service.thread(account, user, agent_id, thread_id, params.get("beforeSeq"))
        if method == "threads.list":
            return service.threads(account, user, agent_id)
        if method == "thread.new":
            return service.new_thread(account, user, agent_id, request_id)
        if method == "thread.select":
            return service.select_thread(account, user, agent_id, thread_id)
        if method == "thread.rename":
            return service.rename_thread(account, user, agent_id, thread_id, params.get("title"), request_id)
        if method == "thread.delete":
            return service.delete_thread(account, user, agent_id, thread_id, request_id)
        if method == "thread.compact":
            return service.compact_thread(account, user, agent_id, thread_id, request_id)
        if method == "thread.removeParticipant":
            return service.remove_participant(account, user, agent_id, thread_id, params.get("participant"))
        if method == "friends.list":
            return service.friends(account, user)
        if method == "queue.get":
            return service.queue_get(account, user, agent_id, thread_id)
        if method == "queue.enqueue":
            return service.queue_enqueue(account, user, agent_id, thread_id, params.get("message"), request_id,
                thinking=params.get("thinking", True), mentions=params.get("mentions"))
        if method == "queue.edit":
            return service.queue_edit(account, user, agent_id, thread_id, params.get("itemId"),
                params.get("version"), params.get("message"))
        if method == "queue.remove":
            return service.queue_remove(account, user, agent_id, thread_id, params.get("itemId"), params.get("version"))
        if method == "queue.pause":
            return service.queue_pause(account, user, agent_id, thread_id)
        if method == "queue.resume":
            return service.queue_resume(account, user, agent_id, thread_id)
        if method == "draft.save":
            return service.save_draft(account, user, agent_id, thread_id, params.get("text"),
                params.get("version"), params.get("scrollTop", 0), params.get("migrate", False), params.get("mentions"))
        if method == "operation.get":
            if ("operationId" in params) == ("requestId" in params):
                raise RpcError("invalid-request", "须指定一个操作标识")
            return service.session_operation(account, user, thread_id, params.get("operationId"), request_id)
        if method == "operation.stop":
            return service.stop_session_operation(account, user, thread_id, params.get("operationId"))
        if method == "run.start":
            if not valid_identifier(thread_id):
                raise RpcError("invalid-request", "请先选择助手会话")
            return service.start(account, user, agent_id, thread_id, params.get("message"), request_id,
                retry_run_id=params.get("retryRunId"), thinking=params.get("thinking", True),
                mentions=params.get("mentions"), regenerate_run_id=params.get("regenerateRunId"),
                edit_run_id=params.get("editRunId"))
        if method == "run.events":
            return service.events(account, user, params.get("runId"), params.get("after", 0))
        if method == "run.stop":
            return service.stop(account, user, params.get("runId"))
        if method == "context.prepare":
            return service.prepare_context(account, user)
        raise RpcError("unknown-method", "插件不支持该操作")
    except (AdvisorError, CoreError) as exc:
        raise RpcError(exc.code, exc.message) from None


METHOD_FIELDS = {
    "stickers.start": {"completionRequestId"},
    "catalog.get": set(), "catalog.saveAgent": {"agent"}, "catalog.enableAgent": {"id", "enabled"},
    "catalog.deleteAgent": {"id"}, "catalog.importSkill": {"name", "description", "content"},
    "catalog.deleteSkill": {"id"}, "import.preview": {"source"}, "import.cancel": {"previewId"},
    "import.commit": {"previewId", "requestId", "agent", "acceptPartial"},
    "thread.get": {"agentId", "threadId", "beforeSeq"}, "threads.list": {"agentId"},
    "thread.new": {"agentId", "requestId"}, "thread.select": {"agentId", "threadId"},
    "thread.rename": {"agentId", "threadId", "title", "requestId"},
    "thread.delete": {"agentId", "threadId", "requestId"},
    "thread.compact": {"agentId", "threadId", "requestId"},
    "thread.removeParticipant": {"agentId", "threadId", "participant"}, "friends.list": set(),
    "queue.get": {"agentId", "threadId"},
    "queue.enqueue": {"agentId", "threadId", "message", "requestId", "thinking", "mentions"},
    "queue.edit": {"agentId", "threadId", "itemId", "version", "message"},
    "queue.remove": {"agentId", "threadId", "itemId", "version"},
    "queue.pause": {"agentId", "threadId"}, "queue.resume": {"agentId", "threadId"},
    "draft.save": {"agentId", "threadId", "text", "version", "scrollTop", "migrate", "mentions"},
    "operation.get": {"threadId", "operationId", "requestId"},
    "operation.stop": {"threadId", "operationId"},
    "run.start": {"agentId", "threadId", "message", "requestId", "retryRunId", "thinking", "mentions",
                  "regenerateRunId", "editRunId"},
    "run.events": {"runId", "after"}, "run.stop": {"runId"}, "context.prepare": set(),
}
# The fields the former /api/advisor routes required (account and user now come from the host's context).
REQUIRED_FIELDS = {
    "stickers.start": {"completionRequestId"},
    "catalog.saveAgent": {"agent"}, "catalog.enableAgent": {"id", "enabled"}, "catalog.deleteAgent": {"id"},
    "catalog.importSkill": {"name", "description", "content"}, "catalog.deleteSkill": {"id"},
    "import.preview": {"source"}, "import.cancel": {"previewId"}, "import.commit": {"previewId", "requestId", "agent"},
    "thread.get": {"agentId"}, "thread.new": {"agentId", "requestId"}, "thread.select": {"agentId", "threadId"},
    "thread.rename": {"agentId", "threadId", "title", "requestId"}, "thread.delete": {"agentId", "threadId", "requestId"},
    "thread.compact": {"agentId", "threadId", "requestId"}, "draft.save": {"agentId", "text", "version"},
    "thread.removeParticipant": {"agentId", "threadId", "participant"},
    "queue.get": {"agentId", "threadId"},
    "queue.enqueue": {"agentId", "threadId", "message", "requestId"},
    "queue.edit": {"agentId", "threadId", "itemId", "version", "message"},
    "queue.remove": {"agentId", "threadId", "itemId", "version"},
    "queue.pause": {"agentId", "threadId"}, "queue.resume": {"agentId", "threadId"},
    "operation.get": {"threadId"}, "operation.stop": {"threadId", "operationId"},
    "run.start": {"agentId", "threadId", "message", "requestId"}, "run.events": {"runId"}, "run.stop": {"runId"},
}
METHODS = tuple(METHOD_FIELDS)

for name in METHODS:
    plugin.method(name)(lambda params, context, method=name: dispatch(method, params, context))


@plugin.task("interactive")
def resume_task(task):
    return advisor().resume_task(task)


@plugin.task("background")
def background_task(task):
    """Core's "continue" of an interrupted 识别表情 (sticker labels) task."""
    from stickers import TASK_PURPOSE
    if (task.task.get("params") or {}).get("purpose") != TASK_PURPOSE:
        return "failed"
    return advisor().stickers.run(task)


@plugin.event("human.changed")
def human_changed(event):
    if "service" in state:
        state["service"].human_changed(event.get("account"), event.get("user"))


if __name__ == "__main__":
    plugin.run()
