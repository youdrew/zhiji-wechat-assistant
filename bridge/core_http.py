"""Host endpoints for plugins and tasks used by the main window, settings and analysis window.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

from core_plugin_process import PluginError, PluginUnavailable
from core_contracts import AdvisorError, error_status
from core_plugins import DependencyError, ManifestError
from core_plugin_settings import SettingsError
from core_tasks import TaskError
from usage_ledger import MAX_SAFE_INTEGER, PAGE_LIMIT, TIMELINE_BUCKET_LIMIT

GET_PATHS = frozenset({"/api/plugins", "/api/tasks", "/api/task", "/api/usage/summary", "/api/usage/friends",
                       "/api/usage/records", "/api/usage/timeline", "/api/usage/modules",
                       "/api/schemes", "/api/plugins/settings", "/api/model-settings", "/api/attention",
                       "/api/account/self-profile"})
POST_PATHS = frozenset({"/api/plugins/enable", "/api/plugins/restart", "/api/plugins/refresh", "/api/plugins/rpc",
                        "/api/tasks", "/api/tasks/stop", "/api/tasks/continue", "/api/friend-opened",
                        "/api/usage/clear", "/api/schemes/select", "/api/plugins/settings", "/api/model-settings", "/api/attention/seen",
                        "/api/account/self-profile"})
CONFLICT_CHOICES = (None, "stop-other", "queue")


class HostError(Exception):
    def __init__(self, status, code, message, detail=None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


def _text(value, name, maximum=128):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise HostError(400, "invalid-request", f"{name} 不正确")
    return value


def _shape(request, required, optional=()):
    if not isinstance(request, dict) or not set(required) <= request.keys() or \
            not request.keys() <= set(required) | set(optional):
        raise HostError(400, "invalid-request", "请求参数不正确")


def _self_account(core, value):
    account = _text(value, "账号")
    scope = core.scope()
    if not scope.get("ready") or not scope.get("account"):
        raise HostError(409, "account-unavailable", "微信账号尚未就绪")
    if scope["account"] != account:
        raise HostError(409, "account-changed", "微信账号已变化")
    if core.tasks.account_blocked(account):
        raise HostError(409, "account-paused", "账号资料正在清理")
    core.ensure_account(account)
    return account


def _call(action):
    try:
        return action()
    except (TaskError, SettingsError) as exc:
        raise HostError(exc.status, exc.code, exc.message, exc.detail) from None
    except PluginUnavailable as exc:
        raise HostError(503, exc.code, exc.message) from None
    except PluginError as exc:
        raise HostError(422, exc.code, exc.message) from None
    except DependencyError as exc:
        raise HostError(409, exc.code, exc.message) from None
    except ManifestError as exc:
        raise HostError(409, "plugin-incompatible", str(exc)) from None
    except AdvisorError as exc:
        raise HostError(error_status(exc), exc.code, exc.message) from None
    except KeyError:
        raise HostError(404, "not-found", "插件不存在") from None


def _usage_ms(value, message="时间范围不正确"):
    if not isinstance(value, str) or not 1 <= len(value) <= 16 or not value.isascii() or not value.isdigit():
        raise HostError(400, "invalid-request", message)
    number = int(value)
    if number > MAX_SAFE_INTEGER:
        raise HostError(400, "invalid-request", message)
    return number


def _usage_filters(query, optional=()):
    _shape(query, ("account",), ("since", "until", "friend", "plugin", "model", "status", "step", "task", *optional))
    filters = {"account": _text(query["account"], "账号")}
    for key in ("since", "until"):
        if key in query:
            filters[key] = _usage_ms(query[key])
    if "since" in filters and "until" in filters and filters["since"] >= filters["until"]:
        raise HostError(400, "invalid-request", "时间范围不正确")
    for key in ("friend", "plugin", "model", "status", "step", "task"):
        if query.get(key):
            filters[key] = _text(query[key], key, 256)
    return filters


def _usage_boundaries(value):
    if not isinstance(value, str) or len(value) > (TIMELINE_BUCKET_LIMIT + 1) * 17:
        raise HostError(400, "invalid-request", "时间分桶边界不正确")
    parts = value.split(",")
    if not 2 <= len(parts) <= TIMELINE_BUCKET_LIMIT + 1:
        raise HostError(400, "invalid-request", "时间分桶数量不正确")
    bounds = [_usage_ms(part, "时间分桶边界不正确") for part in parts]
    if any(left >= right for left, right in zip(bounds, bounds[1:])):
        raise HostError(400, "invalid-request", "时间分桶边界必须递增")
    return bounds


def _usage_names(core, items):
    for item in items:
        manifest = core.registry.get(item["plugin"])
        item["name"] = manifest["name"] if manifest else None
    return items


def get(core, path, query):
    if path == "/api/account/self-profile":
        _shape(query, ("account",))
        account = _self_account(core, query["account"])
        result = _call(lambda: core.human.self_profile(account))
        _self_account(core, account)
        return result
    if path == "/api/model-settings":
        _shape(query, ())
        return _call(core.model_settings)
    if path.startswith("/api/usage/"):
        options = {"/api/usage/summary": (), "/api/usage/friends": ("order",),
                   "/api/usage/records": ("before", "limit"), "/api/usage/timeline": ("boundaries",),
                   "/api/usage/modules": ()}
        if path not in options:
            raise HostError(404, "not-found", "接口不存在")
        filters = _usage_filters(query, options[path])
        if path == "/api/usage/summary":
            plugins = _usage_names(core, core.ledger.by_plugin(filters))
            return {**core.ledger.summary(filters), "plugins": plugins, "models": core.ledger.models(filters)}
        if path == "/api/usage/modules":
            return {"modules": _usage_names(core, core.ledger.modules(filters))}
        if path == "/api/usage/timeline":
            return core.ledger.timeline(filters, _usage_boundaries(query.get("boundaries")))
        if path == "/api/usage/friends":
            order = query.get("order", "requests")
            if order not in ("requests", "input", "output", "total", "recent", "failures"):
                raise HostError(400, "invalid-request", "排序方式不正确")
            friends = core.ledger.by_friend(filters, order)
            names = core.usage_friend_names(filters["account"]) if any(item["user"] for item in friends) else {}
            for item in friends:
                item["name"] = names.get(item["user"]) if item["user"] else None
            return {"friends": friends}
        before = None
        if "before" in query:
            if not isinstance(query["before"], str):
                raise HostError(400, "invalid-request", "分页位置不正确")
            parts = query["before"].split(",", 1)
            if len(parts) != 2:
                raise HostError(400, "invalid-request", "分页位置不正确")
            before = (_usage_ms(parts[0], "分页位置不正确"), _text(parts[1], "分页位置", 256))
        limit = _usage_ms(query["limit"], "分页数量不正确") if "limit" in query else 50
        if not 1 <= limit <= PAGE_LIMIT:
            raise HostError(400, "invalid-request", "分页数量不正确")
        return core.ledger.records(filters, before, limit)

    if path == "/api/plugins":
        return core.listing()
    if path == "/api/plugins/settings":
        _shape(query, ("id",))
        return _call(lambda: core.registry.settings(_text(query["id"], "插件")))
    if path == "/api/tasks":
        _shape(query, ("account",), ("user", "plugin"))
        return {"tasks": core.tasks.tasks(account=query["account"], user=query.get("user"),
                                          plugin=query.get("plugin"), limit=50)}
    if path == "/api/schemes":
        _shape(query, ("account", "user"))
        account, user = _text(query["account"], "账号"), _text(query["user"], "好友")
        core.ensure_account(account)
        return core.schemes(account, user)
    if path == "/api/attention":
        # Friends whose plugin answer finished while its panel was not showing them (main window red dots).
        _shape(query, ("account",))
        return {"items": core.store.attention(_text(query["account"], "账号"))}
    if path == "/api/task":
        _shape(query, ("id",))
        task = core.tasks.task(_text(query["id"], "任务"))
        if task is None:
            raise HostError(404, "not-found", "任务不存在")
        return {"task": task}
    raise HostError(404, "not-found", "接口不存在")


def post(core, path, request):
    if path == "/api/account/self-profile":
        _shape(request, ("account", "background", "style", "expectedVersion"))
        account = _self_account(core, request["account"])
        result = _call(lambda: core.human.save_self_profile(account, request["background"], request["style"],
                                                           request["expectedVersion"]))
        _self_account(core, account)
        return result
    if path == "/api/model-settings":
        _shape(request, ("revision", "values"))
        return _call(lambda: core.save_model_settings(request["revision"], request["values"]))
    if path == "/api/plugins/settings":
        _shape(request, ("id", "revision", "values", "definition"))
        return _call(lambda: core.registry.save_settings(_text(request["id"], "插件"), request["revision"],
                                                        request["values"], request["definition"]))
    if path == "/api/plugins/enable":
        _shape(request, ("id", "enabled"))
        if type(request["enabled"]) is not bool:
            raise HostError(400, "invalid-request", "启停参数不正确")
        _call(lambda: core.set_enabled(_text(request["id"], "插件"), request["enabled"]))
        return core.listing()
    if path == "/api/plugins/restart":
        _shape(request, ("id",))
        _call(lambda: core.restart(_text(request["id"], "插件")))
        return core.listing()
    if path == "/api/plugins/refresh":
        _shape(request, ())
        return _call(core.refresh_plugins)
    if path == "/api/plugins/rpc":
        _shape(request, ("plugin", "method"), ("params", "account", "user"))
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise HostError(400, "invalid-request", "参数不正确")
        context = {key: request[key] for key in ("account", "user") if isinstance(request.get(key), str)}
        if _text(request["method"], "方法", 64).startswith("control."):
            # Manual sends and their SDK connection answer only the desktop main process's control routes.
            raise HostError(403, "forbidden", "该操作只能由桌面主进程发起")
        _call(lambda: core.require_plugin(_text(request["plugin"], "插件"), context.get("account")))
        result = _call(lambda: core.supervisor.rpc(_text(request["plugin"], "插件"), _text(request["method"], "方法", 64),
                                                   params, context))
        return {"result": result}
    if path == "/api/tasks":
        _shape(request, ("plugin", "account", "user", "scheme"), ("title", "params", "conflict"))
        if request.get("conflict") not in CONFLICT_CHOICES:
            raise HostError(400, "invalid-request", "冲突处理方式不正确")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise HostError(400, "invalid-request", "参数不正确")
        _call(lambda: core.require_plugin(_text(request["plugin"], "插件"), _text(request["account"], "账号")))
        return _call(lambda: core.tasks.create(
            _text(request["plugin"], "插件"), kind="scheme", account=_text(request["account"], "账号"),
            user=_text(request["user"], "好友"), scheme=_text(request["scheme"], "方案", 48),
            title=request.get("title") or "分析", params=params, conflict=request.get("conflict")))
    if path == "/api/tasks/stop":
        _shape(request, ("id",), ("account", "user"))
        return {"task": _call(lambda: core.tasks.stop(_text(request["id"], "任务")))}
    if path == "/api/tasks/continue":
        _shape(request, ("id",), ("conflict", "account", "user"))
        if request.get("conflict") not in CONFLICT_CHOICES:
            raise HostError(400, "invalid-request", "冲突处理方式不正确")
        task = core.tasks.task(_text(request["id"], "任务"))
        if task is not None and task.get("plugin") == "kyf.external-chat-import":
            return _call(lambda: core.external_import.continue_task(task["id"]))
        if task is not None:
            _call(lambda: core.require_plugin(task["plugin"], task["account"]))
        return _call(lambda: core.tasks.continue_task(_text(request["id"], "任务"), request.get("conflict")))
    if path == "/api/schemes/select":
        _shape(request, ("account", "user", "plugin", "scheme"))
        return _call(lambda: core.select_scheme(_text(request["account"], "账号"), _text(request["user"], "好友"),
                                                _text(request["plugin"], "插件"), _text(request["scheme"], "方案", 48)))
    if path == "/api/usage/clear":
        _shape(request, (), ("account",))
        core.ledger.clear(_text(request["account"], "账号") if request.get("account") else None)
        return {"ok": True}
    if path == "/api/attention/seen":
        _shape(request, ("plugin", "account", "user", "taskIds"))
        task_ids = request["taskIds"]
        if not isinstance(task_ids, list) or not 1 <= len(task_ids) <= 2000:
            raise HostError(400, "invalid-request", "已读任务列表不正确")
        task_ids = [_text(task_id, "任务", 256) for task_id in task_ids]
        stamp = core.store.attention_seen(_text(request["plugin"], "插件"), _text(request["account"], "账号"),
                                          _text(request["user"], "好友", 256), task_ids)
        return {"seenAt": stamp}
    if path == "/api/friend-opened":
        _shape(request, ("plugin", "account", "user"), ("scheme",))
        delivered = core.events.friend_opened(_text(request["plugin"], "插件"), _text(request["account"], "账号"),
                                              _text(request["user"], "好友"), request.get("scheme"))
        return {"delivered": delivered}
    raise HostError(404, "not-found", "接口不存在")
