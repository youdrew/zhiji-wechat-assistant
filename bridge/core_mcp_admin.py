"""Settings-channel management API for the MCP plugin.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Reached only through the plugin interface by the kyf.mcp settings page (the host
forwards the page's calls with that plugin's token). MCP tools can never call
these routes: they live behind the plugin interface, not the MCP auth routes.
A generated credential is returned once and never shown again.
"""
from __future__ import annotations

from core_mcp_proxy import EXTERNAL_ACCOUNT, McpError
from core_mcp_store import DEFAULT_MODE, MAX_CALL_RECORDS, MODES, SUBJECT_TYPES


class McpAdminError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _body_field(body, key, checker, message):
    value = body.get(key)
    if not checker(value):
        raise McpAdminError(400, "invalid-request", message)
    return value


def handle_admin(mcp, method, parts, query, body):
    """Route one settings request; ``mcp`` is Core's ``McpServices``."""
    if method == "GET":
        if parts == ["profiles"]:
            return {"profiles": mcp.store.list_profiles()}
        if len(parts) == 2 and parts[0] == "profiles":
            return _profile_detail(mcp, parts[1])
        if parts == ["directory"]:
            return _directory(mcp)
        if parts == ["calls"]:
            try:
                limit = int(query.get("limit", "50"))
            except ValueError:
                raise McpAdminError(400, "invalid-request", "调用记录条数不正确") from None
            return {"calls": mcp.store.recent_calls(limit)}
        if parts == ["status"]:
            return _status(mcp)
        if parts == ["pairing"]:
            return _pairings(mcp)
        raise McpAdminError(404, "not-found", "接口不存在")
    if method == "POST":
        if parts == ["profiles"]:
            return _create(mcp, body)
        if len(parts) == 3 and parts[0] == "profiles":
            return _profile_action(mcp, parts[1], parts[2], body)
        if parts == ["contacts"]:
            return _grant(mcp, body)
        if parts == ["contacts", "revoke"]:
            return _revoke_contact(mcp, body)
        if parts == ["accounts"]:
            return _grant_account(mcp, body)
        if parts == ["accounts", "revoke"]:
            return _revoke_account(mcp, body)
        if parts == ["master"]:
            return _master(mcp, body)
        if parts == ["confirmations"]:
            return _resolve_confirmation(mcp, body)
        if parts == ["pairing", "decide"]:
            return _decide_pairing(mcp, body)
        if parts == ["connections", "disconnect"]:
            if not isinstance(body, dict) or body:
                raise McpAdminError(400, "invalid-request", "断开请求不接受参数")
            return {"disconnected": mcp.disconnect_all()}
        raise McpAdminError(404, "not-found", "接口不存在")
    raise McpAdminError(405, "method-not-allowed", "不支持的请求方法")


def _require_client(mcp, client_id):
    profile = mcp.store.client(client_id) if isinstance(client_id, str) else None
    if profile is None:
        raise McpAdminError(404, "client-not-found", "客户端不存在")
    return profile


def _create(mcp, body):
    """A new profile reads the selected accounts only unless capabilities are requested (G10)."""
    if not isinstance(body, dict) or set(body) - {"name", "mode", "globalManagement", "allowWrite",
                                                  "allowSend"}:
        raise McpAdminError(400, "invalid-request", "配对请求参数不正确")
    mode = body.get("mode", DEFAULT_MODE)
    if mode not in MODES:
        raise McpAdminError(400, "invalid-request", "授权模式不正确")
    flags = {}
    for key, message in (("globalManagement", "全局管理开关不正确"), ("allowWrite", "修改资料开关不正确"),
                         ("allowSend", "发送开关不正确")):
        flags[key] = body.get(key, False)
        if not isinstance(flags[key], bool):
            raise McpAdminError(400, "invalid-request", message)
    created = mcp.store.create_profile(body.get("name"), mode, flags["globalManagement"],
                                       allow_write=flags["allowWrite"], allow_send=flags["allowSend"])
    profile = mcp.store.client(created["clientId"])
    return {"profile": _view(mcp, profile), "credential": created["credential"]}


def _view(mcp, profile):
    """Profile view with its account grants (all fingerprints; the page filters by the current one)."""
    return {**mcp.store.profile_view(profile), "accountGrants": mcp.store.account_grants(profile["client_id"])}


def _profile_detail(mcp, client_id):
    profile = _require_client(mcp, client_id)
    # The current install fingerprint: the page needs it to grant new contacts and
    # must never assemble one itself.
    return {"profile": _view(mcp, profile), "contacts": mcp.store.grants(client_id),
            "accountGrants": mcp.store.account_grants(client_id),
            "sourceFingerprint": mcp.fingerprint()}


def _directory(mcp):
    """Saved accounts and their conversations for the contact matrix.

    The provider is Core's view of the local registry; when it is unavailable the
    page shows an empty matrix instead of inventing subject ids.
    """
    provider = getattr(mcp, "directory", None)
    if not callable(provider):
        raise McpAdminError(503, "mcp-directory-unavailable", "账号目录暂不可用")
    try:
        result = provider()
    except Exception:
        raise McpAdminError(503, "mcp-directory-unavailable", "账号目录暂不可用") from None
    if not isinstance(result, dict) or not isinstance(result.get("accounts"), list):
        raise McpAdminError(503, "mcp-directory-unavailable", "账号目录暂不可用")
    # The external-import library is granted as one fixed pseudo-account.
    return {**result, "externalAccountId": EXTERNAL_ACCOUNT, "sourceFingerprint": mcp.fingerprint()}


def _master(mcp, body):
    enabled = _body_field(body, "enabled", lambda value: isinstance(value, bool), "总启停开关不正确")
    mcp.store.set_master_enabled(enabled)
    if not enabled:
        # Turning the switch off is immediate: Agent-opened windows close (their guard would
        # refuse the next request anyway) and existing connection tokens stop too.
        ui = getattr(mcp, "ui", None)
        if ui is not None and callable(getattr(ui, "close_all", None)):
            try:
                ui.close_all()
            except Exception:
                pass
    return {"enabled": enabled, "disconnected": 0 if enabled else mcp.disconnect_all()}


def _profile_action(mcp, client_id, action, body):
    _require_client(mcp, client_id)
    if action == "credential":
        if not isinstance(body, dict) or body:
            raise McpAdminError(400, "invalid-request", "重新生成凭据不接受参数")
        regenerated = mcp.store.regenerate_credential(client_id)
        profile = mcp.store.client(client_id)
        return {"profile": _view(mcp, profile), "credential": regenerated["credential"]}
    if action == "enabled":
        enabled = _body_field(body, "enabled", lambda value: isinstance(value, bool), "启停状态不正确")
        mcp.store.set_enabled(client_id, enabled)
    elif action == "mode":
        mode = _body_field(body, "mode", lambda value: value in MODES, "授权模式不正确")
        mcp.store.set_mode(client_id, mode)
    elif action == "global-management":
        # Kept for old pages: an alias of the ``manage`` capability.
        enabled = _body_field(body, "enabled", lambda value: isinstance(value, bool), "全局管理开关不正确")
        mcp.store.set_global_management(client_id, enabled)
    elif action == "capabilities":
        if not isinstance(body, dict) or not body or set(body) - {"write", "send", "manage"} or \
                any(not isinstance(value, bool) for value in body.values()):
            raise McpAdminError(400, "invalid-request", "操作能力开关不正确")
        mcp.store.set_capabilities(client_id, **body)
    elif action == "revoke":
        if not isinstance(body, dict) or body:
            raise McpAdminError(400, "invalid-request", "撤销请求不接受参数")
        return mcp.store.revoke_profile(client_id)
    else:
        raise McpAdminError(404, "not-found", "接口不存在")
    return {"profile": _view(mcp, mcp.store.client(client_id))}


def _account_values(mcp, body):
    if not isinstance(body, dict) or set(body) != {"clientId", "sourceFingerprint", "accountId"}:
        raise McpAdminError(400, "invalid-request", "账号授权参数不正确")
    _require_client(mcp, body["clientId"])
    if not isinstance(body["sourceFingerprint"], str) or not body["sourceFingerprint"]:
        raise McpAdminError(400, "invalid-request", "安装指纹不正确")
    if not isinstance(body["accountId"], str) or not body["accountId"]:
        raise McpAdminError(400, "invalid-request", "账号标识不正确")
    return body


def _grant_account(mcp, body):
    values = _account_values(mcp, body)
    mcp.store.grant_account(values["clientId"], values["sourceFingerprint"], values["accountId"])
    return {"granted": True, "accountGrants": mcp.store.account_grants(values["clientId"])}


def _revoke_account(mcp, body):
    values = _account_values(mcp, body)
    result = mcp.store.revoke_account(values["clientId"], values["sourceFingerprint"], values["accountId"])
    return {**result, "accountGrants": mcp.store.account_grants(values["clientId"])}


def _contact_values(mcp, body):
    if not isinstance(body, dict) or set(body) != {"clientId", "sourceFingerprint", "accountId",
                                                  "subjectType", "subjectId"}:
        raise McpAdminError(400, "invalid-request", "联系人授权参数不正确")
    _require_client(mcp, body["clientId"])
    if not isinstance(body["sourceFingerprint"], str) or not body["sourceFingerprint"]:
        raise McpAdminError(400, "invalid-request", "安装指纹不正确")
    if body["subjectType"] not in SUBJECT_TYPES:
        raise McpAdminError(400, "invalid-request", "主体类型不正确")
    # Field-level checks and uniqueness live in the store; its errors carry the
    # same shape as the ones raised here.
    mcp.store.grant_contact(body["clientId"], body["sourceFingerprint"], body["accountId"],
                            body["subjectType"], body["subjectId"])
    return body


def _grant(mcp, body):
    values = _contact_values(mcp, body)
    return {"granted": True, "contacts": mcp.store.grants(values["clientId"])}


def _revoke_contact(mcp, body):
    values = _contact_values(mcp, body)
    result = mcp.store.revoke_contact(values["clientId"], values["sourceFingerprint"], values["accountId"],
                                      values["subjectType"], values["subjectId"])
    return {**result, "contacts": mcp.store.grants(values["clientId"])}


def _resolve_confirmation(mcp, body):
    """The local confirmation window's one-time decision on a high-impact deletion.

    只在本设置通道可达（MCP 认证路由没有这个接口，MCP 工具因此永远不能自行确
    认）；确认窗凭创建时下发的一次性令牌回传决定，绑定关系由 core_mcp_confirm
    校验。
    """
    confirm = getattr(mcp, "confirm", None)
    if confirm is None:
        raise McpAdminError(503, "mcp-unavailable", "确认服务未就绪")
    if not isinstance(body, dict) or set(body) != {"confirmId", "token", "granted"}:
        raise McpAdminError(400, "invalid-request", "确认回传参数不正确")
    try:
        return confirm.resolve(body["confirmId"], body["token"], body["granted"])
    except McpError as exc:
        raise McpAdminError(exc.status, exc.code, exc.message) from None


def _pairing_service(mcp):
    service = getattr(mcp, "pairing", None)
    if service is None:
        raise McpAdminError(503, "pairing-unavailable", "配对服务未就绪")
    return service


def _pairings(mcp):
    """Pending command-line pairing requests (G12) and the accounts the user may share."""
    service = _pairing_service(mcp)
    return {"pairings": service.pending(), "accounts": service.accounts()}


def _decide_pairing(mcp, body):
    """The settings page's approval or refusal of a pending pairing (the no-desktop fallback).

    只在本设置通道可达：配对的回环路由与 MCP 工具都不能批准配对。
    """
    if not isinstance(body, dict) or not {"pairingId", "approve"} <= set(body) or             set(body) - {"pairingId", "approve", "accounts", "capabilities"}:
        raise McpAdminError(400, "invalid-request", "配对决定参数不正确")
    try:
        return {"pairing": _pairing_service(mcp).decide(body["pairingId"], body["approve"],
                                                         body.get("accounts"), body.get("capabilities"))}
    except McpError as exc:
        raise McpAdminError(exc.status, exc.code, exc.message) from None


def _status(mcp):
    profiles = mcp.store.list_profiles()
    leases = getattr(mcp, "leases", None)
    return {"coreReady": True, "enabled": mcp.store.master_enabled(),
            "profiles": len(profiles),
            "enabledProfiles": sum(1 for item in profiles if item["enabled"]),
            "activeConnections": mcp.active_connections(),
            # 与共享租约一致：GUI 持有一个 gui 租约即视为主窗口在运行。
            "guiAttached": bool(leases is not None and leases.count("gui"))}
