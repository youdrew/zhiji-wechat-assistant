"""Settings-channel client for the kyf.mcp plugin.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Every RPC method the management page calls maps to one ``/plugin-api/v1/mcp/*``
route of Core. Core enforces that only this plugin's token reaches those routes
and that a generated credential is returned exactly once; this module only
forwards named requests and translates errors for the page.
"""
from __future__ import annotations

from kyf_plugin import CoreClient, CoreError, RpcError

CALLS_LIMIT = 50
# 数据范围：按账号（新建默认）、逐联系人、全部账号（旧档案兼容）。
_MODES = ("accounts", "per_contact", "full")
_DEFAULT_MODE = "accounts"
_CAPABILITIES = ("write", "send", "manage")


def _client_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise RpcError("invalid-request", "客户端标识不正确")
    return value


def _mode(value):
    if value not in _MODES:
        raise RpcError("invalid-request", "授权模式不正确")
    return value


def _limit(value):
    if value is None:
        return CALLS_LIMIT
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 500:
        raise RpcError("invalid-request", "调用记录条数不正确")
    return value


class McpAdmin:
    """Named forwards to Core's MCP management routes; no generic path pass-through."""

    def __init__(self, core: CoreClient):
        self.core = core

    def _get(self, path, **query):
        return self._call(lambda: self.core.get(path, **query))

    def _post(self, path, body=None):
        return self._call(lambda: self.core.post(path, body or {}))

    @staticmethod
    def _call(action):
        try:
            return action()
        except CoreError as exc:
            raise RpcError(exc.code, exc.message) from None

    # -- reads ----------------------------------------------------------------
    def status(self, _params):
        return self._get("mcp/status")

    def profiles(self, _params):
        return self._get("mcp/profiles")

    def profile(self, params):
        return self._get("mcp/profiles/" + _client_id((params or {}).get("clientId")))

    def directory(self, _params):
        return self._get("mcp/directory")

    def calls(self, params):
        return self._get("mcp/calls", limit=_limit((params or {}).get("limit")))

    # -- profiles and pairing --------------------------------------------------
    def create_profile(self, params):
        params = params or {}
        if not isinstance(params.get("name"), str):
            raise RpcError("invalid-request", "请输入客户端名称")
        body = {"name": params["name"], "mode": _mode(params.get("mode", _DEFAULT_MODE))}
        for key, message in (("globalManagement", "全局管理开关不正确"), ("allowWrite", "修改资料开关不正确"),
                             ("allowSend", "发送开关不正确")):
            if key in params:
                if not isinstance(params[key], bool):
                    raise RpcError("invalid-request", message)
                body[key] = params[key]
        return self._post("mcp/profiles", body)

    def credential(self, params):
        return self._post(f"mcp/profiles/{_client_id((params or {}).get('clientId'))}/credential")

    def set_enabled(self, params):
        params = params or {}
        if not isinstance(params.get("enabled"), bool):
            raise RpcError("invalid-request", "启停状态不正确")
        return self._post(f"mcp/profiles/{_client_id(params.get('clientId'))}/enabled",
                          {"enabled": params["enabled"]})

    def set_mode(self, params):
        params = params or {}
        return self._post(f"mcp/profiles/{_client_id(params.get('clientId'))}/mode",
                          {"mode": _mode(params.get("mode"))})

    def set_global_management(self, params):
        params = params or {}
        if not isinstance(params.get("enabled"), bool):
            raise RpcError("invalid-request", "全局管理开关不正确")
        return self._post(f"mcp/profiles/{_client_id(params.get('clientId'))}/global-management",
                          {"enabled": params["enabled"]})

    def set_capabilities(self, params):
        """Operation capabilities (修改资料与发起分析 / 发送微信 / 全局管理); any change re-pairs."""
        params = params or {}
        body = {key: params[key] for key in _CAPABILITIES if key in params}
        if not body or any(not isinstance(value, bool) for value in body.values()):
            raise RpcError("invalid-request", "操作能力开关不正确")
        return self._post(f"mcp/profiles/{_client_id(params.get('clientId'))}/capabilities", body)

    def revoke(self, params):
        return self._post(f"mcp/profiles/{_client_id((params or {}).get('clientId'))}/revoke")

    # -- contact grants ---------------------------------------------------------
    def _grant_values(self, params):
        params = params or {}
        if not isinstance(params.get("sourceFingerprint"), str) or not params["sourceFingerprint"]:
            raise RpcError("invalid-request", "安装指纹不正确")
        if params.get("subjectType") not in ("wechat_contact", "external_contact"):
            raise RpcError("invalid-request", "主体类型不正确")
        return {"clientId": _client_id(params.get("clientId")),
                "sourceFingerprint": params["sourceFingerprint"],
                "accountId": params.get("accountId"), "subjectType": params["subjectType"],
                "subjectId": params.get("subjectId")}

    def grant_contact(self, params):
        return self._post("mcp/contacts", self._grant_values(params))

    def revoke_contact(self, params):
        return self._post("mcp/contacts/revoke", self._grant_values(params))

    # -- account grants (按账号授权) ------------------------------------------------
    @staticmethod
    def _account_values(params):
        params = params or {}
        if not isinstance(params.get("sourceFingerprint"), str) or not params["sourceFingerprint"]:
            raise RpcError("invalid-request", "安装指纹不正确")
        if not isinstance(params.get("accountId"), str) or not params["accountId"]:
            raise RpcError("invalid-request", "账号标识不正确")
        return {"clientId": _client_id(params.get("clientId")),
                "sourceFingerprint": params["sourceFingerprint"], "accountId": params["accountId"]}

    def grant_account(self, params):
        return self._post("mcp/accounts", self._account_values(params))

    def revoke_account(self, params):
        return self._post("mcp/accounts/revoke", self._account_values(params))

    # -- switch and connections --------------------------------------------------
    def set_master(self, params):
        params = params or {}
        if not isinstance(params.get("enabled"), bool):
            raise RpcError("invalid-request", "总启停开关不正确")
        return self._post("mcp/master", {"enabled": params["enabled"]})

    def disconnect_all(self, _params):
        return self._post("mcp/connections/disconnect")

    # -- command-line pairing requests (G12) --------------------------------------
    def pairings(self, _params):
        """Pending ``zhiji-mcp-cli connect`` requests and the accounts that may be shared."""
        return self._get("mcp/pairing")

    def decide_pairing(self, params):
        """Approve (with the chosen accounts and capabilities) or refuse one pending pairing."""
        params = params or {}
        pairing_id = params.get("pairingId")
        if not isinstance(pairing_id, str) or not 1 <= len(pairing_id) <= 64:
            raise RpcError("invalid-request", "配对请求标识不正确")
        if not isinstance(params.get("approve"), bool):
            raise RpcError("invalid-request", "配对决定不正确")
        body = {"pairingId": pairing_id, "approve": params["approve"]}
        if params["approve"]:
            accounts = params.get("accounts", [])
            if not isinstance(accounts, list) or any(not isinstance(item, str) for item in accounts):
                raise RpcError("invalid-request", "所选账号不正确")
            capabilities = params.get("capabilities", {})
            if not isinstance(capabilities, dict) or set(capabilities) - set(_CAPABILITIES) or                     any(not isinstance(value, bool) for value in capabilities.values()):
                raise RpcError("invalid-request", "操作能力开关不正确")
            body.update(accounts=accounts, capabilities=capabilities)
        return self._post("mcp/pairing/decide", body)

    # -- this plugin's own settings (default pairing mode preselection) -----------
    def settings(self, _params):
        result = self._get("settings")
        values = result.get("values") if isinstance(result, dict) else None
        mode = values.get("defaultMode") if isinstance(values, dict) else None
        return {"defaultMode": mode if mode in _MODES else _DEFAULT_MODE}
