"""Core plugin interface v1 (``/plugin-api/v1``) served on the loopback HTTP server.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Every request carries the plugin's process token; task-scoped writes also carry
an execution token that Core re-checks on each write. Account and friend checks
keep plugins from writing into the wrong conversation (correctness), they are
not a defence against a malicious plugin.
"""
from __future__ import annotations

import json
import re
import threading
from urllib.parse import unquote

from core_tasks import TaskError
from core_plugin_process import PluginError, PluginUnavailable
from core_plugin_settings import SettingsError
from core_embeddings import EmbeddingError
from core_gateway import GatewayUnavailable
from core_mcp_store import McpStoreError

PREFIX = "/plugin-api/v1/"
MAX_BODY = 4 * 1024 * 1024
USER = re.compile(r"[^\x00-\x1f/\\]{1,128}\Z")
EVENTS = ("message.new",)


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _user(value):
    if not isinstance(value, str) or not USER.match(value):
        raise ApiError(400, "invalid-request", "好友标识不正确")
    return value


def _int(value, default, minimum=0, maximum=2**53):
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid-request", "数字参数不正确") from None
    if not minimum <= number <= maximum:
        raise ApiError(400, "invalid-request", "数字参数超出范围")
    return number


class PluginAPI:
    """Routes one authenticated plugin request to Core services."""

    def __init__(self, core):
        self.core = core
        self._self_calls = threading.BoundedSemaphore(4)

    def authenticate(self, headers):
        header = headers.get("Authorization") or ""
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        plugin = self.core.supervisor.plugin_for_token(token)
        if plugin is None:
            raise ApiError(401, "unauthorized", "插件令牌无效")
        return plugin

    def _account(self, value):
        scope = self.core.scope()
        if not scope.get("account"):
            raise ApiError(409, "account-unavailable", "微信账号尚未就绪")
        if value != scope["account"]:
            raise ApiError(409, "account-changed", "微信账号已变化")
        self.core.ensure_account(value)
        return value

    def _execution(self, plugin, headers, task_id=None):
        try:
            return self.core.tasks.verify(headers.get("X-KYF-Execution"), plugin, task_id)
        except TaskError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from None

    def handle(self, method, path, query, headers, body):
        """Returns ``(status, payload)``; a payload may be an iterator of NDJSON lines."""
        plugin = self.authenticate(headers)
        route = path[len(PREFIX):].strip("/")
        parts = [unquote(part) for part in route.split("/")] if route else []
        if parts[:1] == ["mcp"]:
            return 200, self._mcp_manage(plugin, method, parts[1:], query, body or {})
        try:
            if method == "GET" and parts in (["settings"], ["models", "settings"]):
                # Settings do not depend on an account or legacy business-data migration.
                return 200, self._route(plugin, method, parts, query, headers, body or {})
            # Account validation precedes migration so an invalid scope creates no data.
            account = query.get("account") if method == "GET" else (body or {}).get("account")
            if account is not None:
                account = self._account(account)
            self.core.require_plugin(plugin, account)
            return 200, self._route(plugin, method, parts, query, headers, body or {})
        except (TaskError, SettingsError) as exc:
            raise ApiError(exc.status, exc.code, exc.message) from None
        except PluginUnavailable as exc:
            raise ApiError(503, exc.code, exc.message) from None
        except (EmbeddingError, GatewayUnavailable) as exc:
            raise ApiError(400 if exc.code == "embedding-invalid" else 503, exc.code, exc.message) from None

    def _mcp_manage(self, plugin, method, parts, query, body):
        """MCP 插件设置通道：只有 kyf.mcp 的设置页能读写 MCP 授权；MCP 工具永远到不了这里。"""
        from core_mcp_admin import McpAdminError, handle_admin
        if plugin != "kyf.mcp":
            raise ApiError(403, "mcp-admin-forbidden", "MCP 授权只能由本机设置界面管理")
        # Modified by Know Your Friends, 2026-10-27 (phase 6): a disabled or uninstalled
        # kyf.mcp plugin leaves no settings channel either; re-enabling happens in the
        # general plugin settings, never through this route.
        registry = getattr(self.core, "registry", None)
        if registry is not None and not registry.enabled("kyf.mcp"):
            raise ApiError(403, "mcp-admin-forbidden", "MCP 插件已停用或已卸载")
        mcp = getattr(self.core, "mcp", None)
        if mcp is None:
            raise ApiError(503, "mcp-unavailable", "MCP 服务未就绪")
        try:
            return handle_admin(mcp, method, parts, query, body)
        except McpAdminError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from None
        except McpStoreError as exc:
            raise ApiError(exc.status, exc.code, exc.message) from None

    def _route(self, plugin, method, parts, query, headers, body):
        core = self.core
        if method == "GET" and parts == ["models", "settings"]:
            if query:
                raise ApiError(400, "invalid-request", "模型设置不接受账号或插件标识")
            return core.model_settings()
        if method == "GET" and parts == ["settings"]:
            if query:
                raise ApiError(400, "invalid-request", "插件设置不接受账号或其他插件标识")
            return core.registry.settings(plugin)
        if method == "GET" and parts == ["scope"]:
            return core.scope()
        if method == "GET" and parts == ["account", "self-profile"]:
            account = self._account(query.get("account"))
            result = core.human.self_profile(account)
            self._account(account)
            return result
        if method == "POST" and parts == ["services", "self-context"]:
            return self._self_context(plugin, headers, body)
        if parts[:1] == ["embeddings"]:
            return self._embeddings(plugin, method, parts[1:], query, headers, body)
        if method == "GET" and parts == ["external", "friends"]:
            # External-only friends do not require a live WeChat account.
            return {"friends": core.external_import.store.list_friends()}
        if method == "GET" and parts == ["external", "history"]:
            friend_id = query.get("friendId")
            if not isinstance(friend_id, str) or not 1 <= len(friend_id) <= 128:
                raise ApiError(400, "invalid-request", "外部好友标识不正确")
            try:
                result = core.external_import.store.messages(friend_id, platform=query.get("platform") or None,
                    batch_id=query.get("batchId") or None, query=query.get("q", "")[:500])
            except KeyError:
                raise ApiError(404, "not-found", "外部好友不存在") from None
            return {**result, "confirmedRecordsOnly": True, "completeHistory": False,
                    "timeMayBeUnknown": True}
        if method == "GET" and parts == ["friends"]:
            return {"friends": core.friends(self._account(query.get("account")))}
        if method == "GET" and len(parts) == 2 and parts[0] == "friends":
            account, user = self._account(query.get("account")), _user(parts[1])
            match = [item for item in core.friends(account) if item["user"] == user]
            if not match:
                raise ApiError(404, "not-found", "好友不在已添加的会话中")
            return match[0]
        if parts[:1] == ["history"]:
            return self._history(method, parts[1:], query, body)
        if method == "GET" and len(parts) == 2 and parts[0] == "human":
            account, user = self._account(query.get("account")), _user(parts[1])
            if query.get("view") == "snapshot":
                return core.human.snapshot(account, user)
            if query.get("view") == "rows":
                return core.human.rows(account, user)
            return core.human.read(account, user)
        if method == "GET" and parts == ["media"]:
            account, user = self._account(query.get("account")), _user(query.get("user"))
            message_id = query.get("id")
            if not isinstance(message_id, str) or not 1 <= len(message_id) <= 512:
                raise ApiError(400, "invalid-request", "消息标识不正确")
            return core.history.media(account, user, message_id, frames=_int(query.get("frames"), 1, 1, 4))
        if method == "POST" and parts == ["human", "candidates"]:
            binding = self._execution(plugin, headers)
            account, user = self._account(body.get("account")), _user(body.get("user"))
            if binding["account"] != account or (binding["user"] not in (None, user)):
                raise ApiError(409, "scope-mismatch", "候选不属于该任务的好友")
            from core_human import CandidateConflict
            try:
                with core.tasks.submission(headers.get("X-KYF-Execution"), plugin):
                    return core.human.submit_candidates(account, user, plugin, binding, body.get("candidates"),
                                                        links=body.get("links"),
                                                        expected_clear_epoch=body.get("expectedClearEpoch"))
            except CandidateConflict:
                raise ApiError(409, "memories-cleared", "这位联系人的记忆已被清空，请重新建档") from None
            except ValueError as exc:
                raise ApiError(400, "invalid-request", str(exc)) from None
        if method == "POST" and parts == ["tasks"]:
            account = self._account(body.get("account"))
            user = _user(body["user"]) if body.get("user") is not None else None
            kind = body.get("kind", "background")
            scheme = body.get("scheme")
            if kind not in ("background", "interactive", "scheme"):
                raise ApiError(400, "invalid-request", "任务类型不正确")
            if kind == "scheme" and (user is None or not isinstance(scheme, str)):
                raise ApiError(400, "invalid-request", "方案任务须指定好友和方案")
            title = body.get("title")
            if not isinstance(title, str) or not 1 <= len(title) <= 80:
                raise ApiError(400, "invalid-request", "任务标题不正确")
            return core.tasks.create(plugin, kind=kind, account=account, user=user, title=title,
                                     scheme=scheme if kind == "scheme" else None,
                                     params=body.get("params") if isinstance(body.get("params"), dict) else {},
                                     from_plugin=True)
        if method == "POST" and len(parts) == 3 and parts[0] == "tasks" and parts[2] == "stop":
            # A plugin may stop only its own tasks (a user stop in its page, stale settings, account pause).
            return {"task": core.tasks.stop_owned(plugin, parts[1])}
        if method == "POST" and len(parts) == 3 and parts[0] == "files" and parts[2] == "claim":
            try:
                return core.files.claim(plugin, parts[1])
            except LookupError:
                raise ApiError(404, "not-found", "文件授权不存在或已过期") from None
        if method == "POST" and len(parts) == 3 and parts[0] == "tasks" and parts[2] in ("progress", "finish"):
            token = headers.get("X-KYF-Execution")
            if parts[2] == "progress":
                progress = {key: body[key] for key in ("done", "total", "label", "phase", "waitingModel")
                            if key in body}
                if any(key in progress and progress[key] is not None and type(progress[key]) is not int
                       for key in ("done", "total")):
                    raise ApiError(400, "invalid-request", "进度不正确")
                if isinstance(progress.get("label"), str):
                    progress["label"] = progress["label"][:80]
                return {"task": core.tasks.progress(token, plugin, parts[1], progress)}
            error = body.get("error")
            if error is not None and not isinstance(error, dict):
                raise ApiError(400, "invalid-request", "错误信息不正确")
            if error is not None:
                error = {"code": str(error.get("code") or "plugin-error")[:64],
                         "message": str(error.get("message") or "任务失败")[:300]}
            return {"task": core.tasks.finish(token, plugin, parts[1], body.get("state"), error)}
        if method == "GET" and parts == ["model", "services"]:
            return core.model_services(plugin)
        if method == "POST" and len(parts) == 4 and parts[:2] == ["model", "attempts"] and parts[3] == "outcome":
            self._execution(plugin, headers)
            return core.attempt_outcome(plugin, parts[2], body)
        if parts == ["subscriptions"] and method in ("POST", "DELETE"):
            account, user = self._account(body.get("account")), _user(body.get("user"))
            event = body.get("event", "message.new")
            if event not in EVENTS:
                raise ApiError(400, "invalid-request", "不支持订阅该事件")
            if method == "POST":
                replay_live = body.get("replayLive", False)
                if type(replay_live) is not bool:
                    raise ApiError(400, "invalid-request", "订阅回放选项不正确")
                core.events.subscribed(plugin, account, user, replay_live=replay_live)
                core.store.subscribe(plugin, account, user, event)
                core.events.wake.set()
            else:
                core.store.unsubscribe(plugin, account, user, event)
            return {"ok": True}
        raise ApiError(404, "not-found", "接口不存在")

    def _embeddings(self, plugin, method, parts, query, headers, body):
        source = query if method == "GET" else body
        account = self._account(source.get("account"))
        allowed = {"account", "texts", "kind"} if parts == ["embed"] else {"account"}
        if set(source) - allowed:
            raise ApiError(400, "invalid-request", "本机检索模型参数不正确")

        def check():
            self._account(account)
            if self.authenticate(headers) != plugin or not self.core.registry.enabled(plugin):
                raise ApiError(403, "plugin-disabled", "插件已停用")
            self.core.require_plugin(plugin, account)
            if headers.get("X-KYF-Execution"):
                binding = self._execution(plugin, headers)
                if binding.get("account") != account:
                    raise ApiError(409, "scope-mismatch", "检索模型请求不属于该任务账号")

        check()
        if method == "GET" and parts == ["status"]:
            result = self.core.embeddings.status()
        elif method == "POST" and parts == ["prepare"]:
            result = self.core.embeddings.prepare()
        elif method == "POST" and parts == ["embed"]:
            result = self.core.embeddings.embed(source.get("texts"), source.get("kind", "passage"),
                plugin=plugin, check=check)
        else:
            raise ApiError(404, "not-found", "接口不存在")
        check()
        return result

    def _history(self, method, parts, query, body):
        history = self.core.history
        source = query if method == "GET" else body
        account = self._account(source.get("account"))
        if method == "POST" and parts == ["verify-many"]:
            if body.get("fresh", True) is not True:
                raise ApiError(400, "invalid-request", "批量原文核验须使用当前来源")
            try:
                return history.verify_many(account, body.get("groups"))
            except ValueError:
                raise ApiError(400, "invalid-request", "核对项或聊天来源不正确") from None
        if method == "POST" and parts == ["status-many"]:
            users = body.get("users")
            if not isinstance(users, list):
                raise ApiError(400, "invalid-request", "聊天列表不正确")
            try:
                return history.status_many(account, users)
            except ValueError:
                raise ApiError(409, "source-changed", "聊天列表或聊天来源已变化，请重新读取") from None
        if method == "GET" and parts == ["conversations"]:
            after = query.get("after")
            if after is not None and (not isinstance(after, str) or len(after) > 128):
                raise ApiError(400, "invalid-request", "会话分页位置不正确")
            try:
                return history.conversations(account, after, _int(query.get("limit"), 500, 1, 500))
            except ValueError:
                raise ApiError(409, "directory-expired", "会话目录或账号来源已变化，请重新读取目录") from None
        user = _user(source.get("user"))
        if method == "GET" and parts == ["status"]:
            options = {"fresh": query.get("fresh") == "1"}
            if query.get("audit") == "1" or query.get("auditHighwater") is not None:
                options.update(audit=query.get("audit") == "1", audit_highwater=query.get("auditHighwater"))
            try:
                return history.status(account, user, **options)
            except ValueError:
                raise ApiError(409, "source-changed", "聊天来源或审计位置已变化，请重新读取") from None
        if method == "POST" and parts == ["prepare"]:
            if "rebuild" in body and type(body["rebuild"]) is not bool:
                raise ApiError(400, "invalid-request", "重新读取标记不正确")
            options = {"refresh": body.get("refresh") is True}
            if body.get("rebuild"):
                options["rebuild"] = True
            return history.prepare(account, user, **options)
        if method == "GET" and parts == ["messages"]:
            return history.messages(account, user, _int(query.get("after"), 0),
                                    _int(query.get("limit"), 500, 1, 500), _int(query.get("upto"), None))
        if method == "GET" and parts == ["export"]:
            return history.export(account, user)
        if method == "GET" and parts == ["context"]:
            message_id = query.get("id")
            if not isinstance(message_id, str) or not message_id:
                raise ApiError(400, "invalid-request", "消息标识不正确")
            return history.around(account, user, message_id, _int(query.get("before"), 10, 0, 50),
                                  _int(query.get("after"), 10, 0, 50))
        if method == "POST" and parts == ["rows"]:
            try:
                return history.rows(account, user, body.get("ids"), verify_sender=body.get("verifySender", False))
            except ValueError:
                raise ApiError(400, "invalid-request", "消息标识不正确") from None
        if method == "GET" and parts == ["recent"]:
            if query.get("full") == "1":
                return history.recent_rows(account, user, _int(query.get("limit"), 400, 1, 2000))
            return history.recent(account, user, _int(query.get("limit"), 200, 1, 2000))
        if method == "POST" and parts == ["stickers"]:
            try:
                return history.stickers(account, user, body.get("ids"))
            except ValueError:
                raise ApiError(400, "invalid-request", "表情消息标识不正确") from None
        if method == "POST" and parts == ["verify"]:
            try:
                return history.verify(account, user, body.get("items"), fresh=body.get("fresh", False))
            except ValueError:
                raise ApiError(400, "invalid-request", "核对项不正确") from None
        raise ApiError(404, "not-found", "接口不存在")

    def _self_context(self, plugin, headers, body):
        """One allowlisted read-only service, never a general cross-plugin RPC proxy."""
        if plugin != "kyf.message-insights":
            raise ApiError(403, "service-forbidden", "该插件不能调用本人表达服务")
        binding = self._execution(plugin, headers)
        account, user = self._account(body.get("account")), _user(body.get("user"))
        if binding["account"] != account or binding["user"] != user:
            raise ApiError(409, "scope-mismatch", "本人表达请求不属于该任务的好友")
        if set(body) - {"account", "user", "messageId", "query", "budgetChars", "limit",
                        "validateOnly", "generation", "sources", "profileVersion"}:
            raise ApiError(400, "invalid-request", "本人表达服务参数不正确")
        validation = {}
        if "validateOnly" in body and type(body["validateOnly"]) is not bool:
            raise ApiError(400, "invalid-request", "本人表达核验标记不正确")
        if body.get("validateOnly"):
            refs, generation, version = body.get("sources"), body.get("generation"), body.get("profileVersion")
            if (not isinstance(refs, list) or len(refs) > 10 or
                    any(not isinstance(ref, dict) or set(ref) != {"id", "hash"} or
                        any(not isinstance(ref[key], str) or not 1 <= len(ref[key]) <= 256
                            for key in ("id", "hash")) for ref in refs) or
                    type(generation) not in (str, int) or len(str(generation)) > 128 or
                    type(version) is not int or not 0 <= version <= 2**53):
                raise ApiError(400, "invalid-request", "本人表达核验引用不正确")
            validation = {"validateOnly": True, "generation": generation, "sources": refs, "profileVersion": version}
        elif any(key in body for key in ("generation", "sources", "profileVersion")):
            raise ApiError(400, "invalid-request", "核验引用只能用于核验请求")
        query, message_id = body.get("query", ""), body.get("messageId")
        if not isinstance(query, str) or len(query) > 12000 or "\x00" in query:
            raise ApiError(400, "invalid-request", "检索内容长度不正确")
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= 512:
            raise ApiError(400, "invalid-request", "消息标识不正确")
        budget = _int(body.get("budgetChars"), 6000, 100, 12000)
        limit = _int(body.get("limit"), 5, 1, 10)
        target = self.core.history.rows(account, user, [message_id])["messages"]
        if not target or target[0].get("side") != "other" or target[0].get("group"):
            raise ApiError(404, "not-found", "好友目标消息不存在")
        target = target[0]
        fingerprint = self.core.history.context.source_scope(account)
        if type(target.get("time")) is not int or target["time"] < 0:
            return {"available": False, "reason": "target-time-unavailable"}
        provider = "kyf.self-analysis"
        if self.core.registry.get(provider) is None or not self.core.registry.enabled(provider):
            return {"available": False, "reason": "plugin-disabled"}
        if not self._self_calls.acquire(blocking=False):
            return {"available": False, "reason": "service-busy"}
        try:
            self.core.require_plugin(provider, account)
            process = self.core.supervisor.ensure(provider, wait=False)
            if not process.ready.wait(0.25):
                return {"available": False, "reason": "service-warming"}
            result = process.call("self.context", {
                "query": query, "budgetChars": budget, "limit": limit, "messageId": message_id,
                "beforeTimeMs": target["time"], "beforeSeq": target["seq"],
                "sourceFingerprint": fingerprint, **validation}, {"account": account, "user": user}, timeout=6.0)
            # A stopped caller, replaced account or disabled provider cannot return late material.
            self._execution(plugin, headers)
            self._account(account)
            if self.core.history.context.source_scope(account) != fingerprint:
                raise ApiError(409, "scope-mismatch", "聊天资料来源已经变化")
            if not self.core.registry.enabled(provider):
                return {"available": False, "reason": "plugin-disabled"}
            if (not isinstance(result, dict) or type(result.get("available")) is not bool or
                    len(json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 96 * 1024):
                return {"available": False, "reason": "invalid-result"}
            if result.get("sourceFingerprint", fingerprint) != fingerprint:
                return {"available": False, "reason": "source-changed"}
            sources = result.get("sources", [])
            if (not isinstance(sources, list) or len(sources) > limit or
                    not isinstance(result.get("text", ""), str) or len(result.get("text", "")) > budget):
                return {"available": False, "reason": "invalid-result"}
            return result
        except (PluginUnavailable, PluginError):
            return {"available": False, "reason": "service-unavailable"}
        except (TypeError, ValueError, OverflowError, RecursionError):
            return {"available": False, "reason": "invalid-result"}
        finally:
            self._self_calls.release()
