"""Core business methods behind the MCP application-management tools (阶段 5).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The capability proxy reaches management capabilities only through this bridge:
model settings go through Core's existing model-settings document and the model
source profile (``model_source_activate`` keeps the connection test and key
re-use rules), plugin state through the shared registry, usage through the
shared ledger, and high-impact deletions through the existing account-cleanup
chain. There is no generic HTTP, file or RPC tool here.

模型密钥只写不读：保存连接经 ``model_source_activate``（DPAPI 加密存储），查询
一律返回 ``hasKey`` 布尔值——任何响应都不含密钥明文。MCP 自身的授权与凭据永远
不可达（固定排除 + 本模块没有对应方法）。删除整个账号/清空本机数据先经
core_mcp_confirm 登记待确认意图，执行时走既有账号清理链（阻止新任务并排水），
执行后撤销相关授权主体并使未完成游标失效。
"""
from __future__ import annotations

from pathlib import Path

from backend_contracts import ModelSourceUnavailable
from core_mcp_confirm import McpDeletionConfirmService
from core_mcp_offline import registry_records
from core_mcp_proxy import EXTERNAL_ACCOUNT, McpError
from core_plugin_settings import SettingsError
from usage_ledger import AGGREGATE_KEYS

MCP_PLUGIN = "kyf.mcp"
ACCOUNTS_PATH = Path(".local") / "real-client-data" / "accounts.json"
USAGE_STRING_FILTERS = ("plugin", "model", "task")
USAGE_TIME_FILTERS = ("since", "until")
CONNECTION_FIELDS = frozenset(("protocol", "baseUrl", "model", "apiKey", "contextTokens", "timeoutMs"))


def _connection_view(view):
    """The bounded connection view; the key only ever appears as the ``hasKey`` flag."""
    if not isinstance(view, dict):
        return None
    api = view.get("api") if isinstance(view.get("api"), dict) else None
    return {"mode": view.get("mode"), "status": view.get("status"),
            "api": None if api is None else {key: api[key] for key in
                                             ("protocol", "baseUrl", "model", "contextTokens",
                                              "timeoutMs", "hasKey") if key in api}}


class McpManagementBridge:
    """The one application-management surface the capability proxy may call."""

    def __init__(self, services, accounts_path, *, deleter=None, presenter=None, confirm=None):
        self.services = services
        self.accounts_path = Path(accounts_path)
        self.confirm = confirm or McpDeletionConfirmService(presenter)
        self.deleter = deleter  # test seam; production wires the account API

    # -- model settings (§1 密钥行) ---------------------------------------------
    def model_settings(self):
        try:
            scheduling = self.services.model_settings()
        except SettingsError as exc:
            raise McpError(exc.status, exc.code, exc.message) from None
        except Exception:
            raise McpError(503, "management-unavailable", "模型设置暂时不可用") from None
        backend = getattr(self.services, "backend", None)
        model_source = getattr(backend, "model_source", None)
        connection = None
        if callable(model_source):
            try:
                connection = _connection_view(model_source())
            except Exception:
                connection = None  # 连接视图缺失不掩盖调度设置，也不捏造密钥状态。
        return {"scheduling": scheduling, "connection": connection}

    def save_model_settings(self, params):
        result = {}
        scheduling = params.get("scheduling")
        if scheduling is not None:
            if not isinstance(scheduling, dict) or set(scheduling) != {"revision", "values"} or \
                    type(scheduling["revision"]) is not int:
                raise McpError(400, "invalid-request", "模型调度设置不正确")
            try:
                result["scheduling"] = self.services.save_model_settings(
                    scheduling["revision"], scheduling["values"])
            except SettingsError as exc:
                raise McpError(exc.status, exc.code, exc.message) from None
            except Exception:
                raise McpError(503, "management-unavailable", "模型设置暂时不可用") from None
        connection = params.get("connection")
        if connection is not None:
            if not isinstance(connection, dict) or set(connection) - CONNECTION_FIELDS:
                raise McpError(400, "invalid-request", "模型连接参数不正确")
            backend = getattr(self.services, "backend", None)
            activate = getattr(backend, "model_source_activate", None)
            if not callable(activate):
                raise McpError(503, "management-unavailable", "模型设置暂时不可用")
            request = {"mode": "api", **connection}
            try:
                view = activate(request)
            except ModelSourceUnavailable as exc:
                raise McpError(503, "model-source-unavailable", str(exc)) from None
            except ValueError as exc:
                raise McpError(400, "invalid-request", str(exc)) from None
            except Exception:
                raise McpError(503, "management-unavailable", "模型连接保存失败") from None
            # 保存响应同样只给脱敏视图；密钥一旦写入就再也不返回。
            result["connection"] = _connection_view(view)
        return result

    # -- plugin state ------------------------------------------------------------
    def plugins(self):
        try:
            listing = self.services.listing()
        except Exception:
            raise McpError(503, "management-unavailable", "插件状态暂时不可用") from None
        plugins = []
        for item in listing.get("plugins", []) if isinstance(listing, dict) else []:
            if not isinstance(item, dict):
                continue
            # 有界视图：不带 contributes/manifest，避免把设置页资料倒给外部客户端。
            plugins.append({"id": item.get("id"), "name": item.get("name"),
                            "version": item.get("version"), "description": item.get("description"),
                            "enabled": bool(item.get("enabled")), "process": item.get("process"),
                            "migration": item.get("migration")})
        return {"plugins": plugins}

    def set_plugin_enabled(self, plugin_id, enabled):
        if plugin_id == MCP_PLUGIN:
            # MCP 自身授权与设置入口不能被 MCP 工具停用。
            raise McpError(403, "mcp-self-forbidden", "MCP 自身授权不能由 MCP 工具修改")
        if not isinstance(plugin_id, str) or not plugin_id or len(plugin_id) > 128:
            raise McpError(400, "invalid-request", "插件标识不正确")
        if not isinstance(enabled, bool):
            raise McpError(400, "invalid-request", "启停状态不正确")
        registry = getattr(self.services, "registry", None)
        if registry is None or registry.get(plugin_id) is None:
            raise McpError(404, "plugin-unknown", "插件不存在")
        try:
            result = self.services.set_enabled(plugin_id, enabled)
        except Exception:
            raise McpError(503, "management-unavailable", "插件启停暂时不可用") from None
        item = result if isinstance(result, dict) else {}
        return {"plugin": {"id": item.get("id", plugin_id), "enabled": bool(item.get("enabled", enabled))}}

    # -- usage (§4.2 聚合边界) -----------------------------------------------------
    def usage(self, account_id, params, *, authorized=None):
        """Model-gateway usage for one explicit account.

        逐联系人模式只在能从已授权联系人重算时返回（逐行聚合求和），并标
        ``scope=authorized_contacts``；无法归属到联系人的用量一律不返回。
        """
        record = self._account_record(account_id)
        filters = {"account": record["account"]}
        for key in USAGE_STRING_FILTERS:
            value = params.get(key)
            if value is not None:
                if not isinstance(value, str) or not value.strip() or len(value) > 200:
                    raise McpError(400, "invalid-request", "用量筛选条件不正确")
                filters[key] = value
        for key in USAGE_TIME_FILTERS:
            value = params.get(key)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise McpError(400, "invalid-request", "用量时间范围不正确")
                filters[key] = value
        ledger = getattr(self.services, "ledger", None)
        if ledger is None:
            raise McpError(503, "management-unavailable", "用量服务暂时不可用")
        include_contacts = params.get("includeContacts", False)
        if not isinstance(include_contacts, bool):
            raise McpError(400, "invalid-request", "逐联系人用量开关不正确")
        if authorized is None:
            usage = {"summary": ledger.summary(filters), "plugins": ledger.by_plugin(filters)}
            if include_contacts:
                usage["contacts"] = ledger.by_friend(filters)
            return {"usage": usage, "scope": "installation"}
        if not authorized:
            raise McpError(403, "contact-not-authorized", "该账号没有已授权联系人")
        rows = [row for row in ledger.by_friend(filters)
                if isinstance(row, dict) and row.get("scope") == "user" and row.get("user") in authorized]
        summary = {key: 0 for key in AGGREGATE_KEYS}
        for row in rows:
            for key in AGGREGATE_KEYS:
                if key == "lastAt":
                    summary[key] = max(summary[key], row.get(key) or 0)
                elif isinstance(row.get(key), (int, float)):
                    summary[key] += row[key]
        dropped = ledger.summary(filters).get("droppedRecords", 0)
        return {"usage": {"summary": summary, "contacts": rows, "excludedUnattributed": True,
                          "droppedRecords": dropped},
                "scope": "authorized_contacts"}

    # -- high-impact deletion (§7) -----------------------------------------------
    def begin_delete(self, *, action, kind, client_id, client_name, auth_version, fingerprint,
                     account_id, params_hash):
        account_label = "所有已保存账号"
        if kind == "delete_account":
            record = self._account_record(account_id)
            account_label = record.get("label") or account_id
        return self.confirm.begin(action=action, kind=kind, client_id=client_id,
                                  client_name=client_name, auth_version=auth_version,
                                  fingerprint=fingerprint, account_id=account_id,
                                  params_hash=params_hash, account_label=account_label)

    def use_confirmation(self, confirmation_id, *, action, kind, client_id, auth_version,
                         fingerprint, account_id, params_hash):
        self.confirm.check_use(confirmation_id, action=action, kind=kind, client_id=client_id,
                               auth_version=auth_version, fingerprint=fingerprint,
                               account_id=account_id, params_hash=params_hash)

    def execute_delete_account(self, account_id):
        """Run the existing cleanup chain for one account, then revoke its grants and cursors.

        确认后再核对账号仍在本机注册表中（数据根指纹已由确认校验绑定）；清理链
        自身会阻止新任务并排水。
        """
        record = self._account_record(account_id)
        deleter = self._deleter()
        deleter(account_id)
        self._after_deletion(account_id)
        return {"deleted": True, "accountId": account_id, "account": record["account"]}

    def execute_clear_local_data(self):
        """Delete every saved account through the same chain, then invalidate cursors."""
        records = registry_records(self.accounts_path)
        if not records:
            raise McpError(404, "account-unknown", "本机没有已保存账号")
        deleter = self._deleter()
        cleared, failed = [], []
        for account_id in sorted(records):
            try:
                deleter(account_id)
                self._after_deletion(account_id)
                cleared.append(account_id)
            except Exception as exc:
                # 单个账号清理失败不清空其余账号；结果如实报告已清与未清。
                failed.append({"accountId": account_id,
                               "error": str(exc) or type(exc).__name__})
        return {"cleared": cleared, "failed": failed, "account": "所有已保存账号"}

    # -- internals -------------------------------------------------------------
    def _deleter(self):
        """The complete existing cleanup chain; refuse rather than half-delete."""
        if callable(self.deleter):
            return self.deleter
        account_api = getattr(getattr(self.services, "backend", None), "account_api", None)
        delete = getattr(account_api, "delete", None)
        if callable(delete):
            return delete
        raise McpError(503, "account-cleaner-unavailable", "本机账号清理服务不可用，不能执行删除")

    def _after_deletion(self, account_id):
        """撤销相关授权主体并使未完成游标失效（§7 执行后）。"""
        mcp = getattr(self.services, "mcp", None)
        store = getattr(mcp, "store", None)
        if store is None:
            return
        try:
            store.revoke_account_grants(account_id)
            store.rotate_cursor_key()
        except Exception:
            pass  # 清理本身已完成；授权撤销失败不产生新的数据风险。

    def _account_record(self, account_id):
        if account_id == EXTERNAL_ACCOUNT:
            raise McpError(400, "invalid-request", "外部素材库不是已保存账号")
        record = registry_records(self.accounts_path).get(account_id)
        if record is None:
            raise McpError(404, "account-unknown", "该账号不是本机已保存账号")
        return record
