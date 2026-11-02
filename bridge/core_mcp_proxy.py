"""Core's MCP capability proxy: per-connection tokens, the single authorization
entry and the registered tool dispatch.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

A paired client exchanges its secret once per connection for a short-lived token
bound to its client id and authorization version. Every tool call passes the one
authorization entry (full mode, per-contact mode, fixed exclusions, aggregates);
nothing about a client's claimed name, a tool parameter or a cached path can
influence it. Only explicitly registered Core/plugin business methods are
reachable here -- never plugin tokens, desktop control tokens, the plain HTTP
routes or arbitrary plugin RPC.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path

from core_leases import pid_alive
from core_mcp_store import McpStore, McpStoreError, now_ms, verify_credential
from friend_store import TAGS

HTTP_PREFIX = "/api/mcp/"
ENDPOINTS = ("handshake", "rpc")
MAX_MCP_BODY = 256 * 1024
SESSION_TTL_MS = 15 * 60 * 1000
MAX_SESSIONS_PER_CLIENT = 8
ACCOUNT_ID_LIMIT = 128
SUBJECT_ID_LIMIT = 512
SEARCH_PAGE_MAX = 100
MEMORY_PAGE_MAX = 100
OBSERVATION_PAGE_MAX = 100
MEMORY_ID_LIMIT = 256
OBSERVATION_ID_LIMIT = 256
# 媒体契约上限：只返回有界内嵌静帧（≤2 MiB/帧、≤4 帧），不返回音视频原件或磁盘路径。
MEDIA_MAX_FRAMES = 4
MEDIA_ITEM_MAX_BASE64 = (2 * 1024 * 1024) * 4 // 3 + 4
EVIDENCE_ORDINAL_MAX = 999
# 外部聊天导入的授权命名空间：素材库不属于任何已保存微信账号，授权表用这个固定
# 伪账号承载 external_contact 主体；它永远不会进入已保存账号注册表。
EXTERNAL_ACCOUNT = "external-chat"
# 助手工具的契约上限（advisor 插件再做细粒度校验）。
ASSISTANT_ID_MAX = 128
ASSISTANT_MESSAGE_MAX = 16000
ASSISTANT_MENTIONS_MAX = 32
ASSISTANT_SKILL_MAX_CHARS = 64 * 1024
ASSISTANT_TEMPLATE_MAX_CHARS = 64 * 1024
IMPORT_BATCH_ID_MAX = 128
IMPORT_CANDIDATE_PAGE_MAX = 100
IMPORT_QUERY_MAX = 500
# 阶段 4 自主发送：正文契约上限与桌面发送一致；正文哈希进意图与去重记录，正文
# 本身不进调用日志或去重结果。
SEND_WAIT_SECONDS = 280.0

# Actions that may never enter the registry; the check below keeps a future
# registration mistake from silently turning them into MCP tools.
FIXED_EXCLUSIONS = frozenset((
    "model.read_key_plaintext",        # 已保存模型密钥明文
    "mcp.manage_authorization",        # MCP 自身授权与凭据
    "data.destructive_delete",         # 无本机确认的高影响删除
))

# The capability directory: the probe, the phase-2/3a offline reads and the phase-3b
# versioned human-data writes are real; the remaining first-batch contracts are
# registered skeletons whose handlers arrive with their capability families.
# ``write`` actions require a requestId, dedup before their side effect and a
# re-checked authorization at commit time; ``aggregate`` entries without
# ``recomputable`` stay out of per-contact mode (account-level data only).
REGISTRY = {
    "zhiji_ping": {"kind": "probe", "subjects": False, "handler": "_ping"},
    "zhiji_list_accounts": {"kind": "business", "subjects": False, "aggregate": True,
                            "recomputable": True, "handler": "_list_accounts"},
    "zhiji_find_contacts": {"kind": "business", "subjects": True},
    "zhiji_get_contact": {"kind": "business", "subjects": True},
    "zhiji_search_messages": {"kind": "business", "subjects": True, "handler": "_search_messages"},
    "zhiji_get_message_context": {"kind": "business", "subjects": True, "handler": "_get_message_context"},
    "zhiji_get_media": {"kind": "business", "subjects": True, "handler": "_get_media"},
    "zhiji_list_memories": {"kind": "business", "subjects": True, "handler": "_list_memories"},
    "zhiji_get_memory_evidence": {"kind": "business", "subjects": True, "handler": "_get_memory_evidence"},
    "zhiji_create_memory": {"kind": "business", "subjects": True, "write": True, "handler": "_create_memory"},
    "zhiji_update_memory": {"kind": "business", "subjects": True, "write": True, "handler": "_update_memory"},
    "zhiji_confirm_memory": {"kind": "business", "subjects": True, "write": True, "handler": "_confirm_memory"},
    "zhiji_delete_memory": {"kind": "business", "subjects": True, "write": True, "handler": "_delete_memory"},
    "zhiji_get_contact_goal": {"kind": "business", "subjects": True, "handler": "_get_contact_goal"},
    "zhiji_save_contact_goal": {"kind": "business", "subjects": True, "write": True,
                                "handler": "_save_contact_goal"},
    # 账号级自身资料：账号级权限控制，逐联系人模式不提供（无联系人可重算）。
    "zhiji_get_self_profile": {"kind": "business", "subjects": False, "aggregate": True,
                               "recomputable": False, "handler": "_get_self_profile"},
    "zhiji_save_self_profile": {"kind": "business", "subjects": False, "aggregate": True,
                                "recomputable": False, "write": True, "handler": "_save_self_profile"},
    "zhiji_list_observations": {"kind": "business", "subjects": True, "handler": "_list_observations"},
    "zhiji_save_observation": {"kind": "business", "subjects": True, "write": True,
                               "handler": "_save_observation"},
    "zhiji_delete_observation": {"kind": "business", "subjects": True, "write": True,
                                 "handler": "_delete_observation"},
    # 阶段 3b 第二批：分析任务。开始/停止是写操作（requestId 幂等）；任务 ID 只轮询
    # 本客户端自己发起的任务；自我剖析是账号级汇总，逐联系人模式一律不提供。
    "zhiji_start_friend_analysis": {"kind": "business", "subjects": True, "write": True,
                                    "handler": "_start_friend_analysis"},
    "zhiji_get_friend_analysis": {"kind": "business", "subjects": True,
                                  "handler": "_get_friend_analysis"},
    "zhiji_get_task": {"kind": "business", "subjects": False, "handler": "_get_task"},
    "zhiji_stop_task": {"kind": "business", "subjects": False, "write": True, "handler": "_stop_task"},
    "zhiji_start_message_insights": {"kind": "business", "subjects": True, "write": True,
                                     "handler": "_start_message_insights"},
    "zhiji_get_message_insights": {"kind": "business", "subjects": True,
                                   "handler": "_get_message_insights"},
    "zhiji_start_self_analysis": {"kind": "business", "subjects": False, "aggregate": True,
                                  "recomputable": False, "write": True,
                                  "handler": "_start_self_analysis"},
    "zhiji_get_self_analysis": {"kind": "business", "subjects": False, "aggregate": True,
                                "recomputable": False, "handler": "_get_self_analysis"},
    "zhiji_stop_self_analysis": {"kind": "business", "subjects": False, "aggregate": True,
                                 "recomputable": False, "write": True,
                                 "handler": "_stop_self_analysis"},
    # 时间线修改与界面共用一条版本和一份撤销日志，账号级能力，逐联系人模式不提供。
    "zhiji_edit_self_timeline": {"kind": "business", "subjects": False, "aggregate": True,
                                 "recomputable": False, "write": True,
                                 "handler": "_edit_self_timeline"},
    "zhiji_undo_self_timeline": {"kind": "business", "subjects": False, "aggregate": True,
                                 "recomputable": False, "write": True,
                                 "handler": "_undo_self_timeline"},
    # 阶段 3b 第三批：聊天助手。模板与技能是用户自己的助手配置（不含联系人资料），
    # 讨论读取、提问与停止一律按联系人主体核验；提问中 @ 的每位好友参与者由处理器
    # 逐个核验授权。模型调用留在 advisor 插件与 Core 网关内。
    "zhiji_get_assistant_catalog": {"kind": "business", "subjects": False,
                                    "handler": "_get_assistant_catalog"},
    "zhiji_save_assistant_template": {"kind": "business", "subjects": False, "write": True,
                                      "handler": "_save_assistant_template"},
    "zhiji_enable_assistant_template": {"kind": "business", "subjects": False, "write": True,
                                        "handler": "_enable_assistant_template"},
    "zhiji_delete_assistant_template": {"kind": "business", "subjects": False, "write": True,
                                        "handler": "_delete_assistant_template"},
    "zhiji_import_assistant_skill": {"kind": "business", "subjects": False, "write": True,
                                     "handler": "_import_assistant_skill"},
    "zhiji_delete_assistant_skill": {"kind": "business", "subjects": False, "write": True,
                                     "handler": "_delete_assistant_skill"},
    "zhiji_list_assistant_friends": {"kind": "business", "subjects": True,
                                     "handler": "_list_assistant_friends"},
    "zhiji_list_assistant_threads": {"kind": "business", "subjects": True,
                                     "handler": "_list_assistant_threads"},
    "zhiji_get_assistant_thread": {"kind": "business", "subjects": True,
                                   "handler": "_get_assistant_thread"},
    "zhiji_new_assistant_thread": {"kind": "business", "subjects": True, "write": True,
                                   "handler": "_new_assistant_thread"},
    "zhiji_submit_assistant_question": {"kind": "business", "subjects": True, "write": True,
                                        "handler": "_submit_assistant_question"},
    "zhiji_get_assistant_run": {"kind": "business", "subjects": True,
                                "handler": "_get_assistant_run"},
    "zhiji_stop_assistant_run": {"kind": "business", "subjects": True, "write": True,
                                 "handler": "_stop_assistant_run"},
    # 阶段 3b 第三批：外部聊天导入。外部联系人按 external_contact 主体独立授权
    # （固定伪账号 external-chat 的稳定好友 ID，绝不按显示名碰撞到微信联系人）；
    # 列出联系人是按授权重算的聚合。没有文件输入工具：上传只经 GUI 显式选择。
    "zhiji_list_import_contacts": {"kind": "business", "subjects": False, "aggregate": True,
                                   "recomputable": True, "handler": "_list_import_contacts"},
    "zhiji_list_import_batches": {"kind": "business", "subjects": True,
                                  "handler": "_list_import_batches"},
    "zhiji_get_import_batch": {"kind": "business", "subjects": True,
                               "handler": "_get_import_batch"},
    "zhiji_list_import_candidates": {"kind": "business", "subjects": True,
                                     "handler": "_list_import_candidates"},
    "zhiji_get_import_messages": {"kind": "business", "subjects": True,
                                  "handler": "_get_import_messages"},
    "zhiji_start_import_recognition": {"kind": "business", "subjects": True, "write": True,
                                       "handler": "_start_import_recognition"},
    "zhiji_stop_import_recognition": {"kind": "business", "subjects": True, "write": True,
                                      "handler": "_stop_import_recognition"},
    "zhiji_save_import_batch": {"kind": "business", "subjects": True, "write": True,
                                "handler": "_save_import_batch"},
    "zhiji_remove_import_material": {"kind": "business", "subjects": True, "write": True,
                                     "handler": "_remove_import_material"},
    # 阶段 5：应用管理（§5 应用管理行）。完整模式开放；逐联系人模式受“全局管理”
    # 开关控制（authorize 内判定），且不扩大联系人权限。模型设置经唯一管理桥，
    # 密钥只写不读；高影响删除先返回待确认意图，确认凭证只能来自本机确认界面。
    "zhiji_get_model_settings": {"kind": "app_management", "subjects": False,
                                 "handler": "_get_model_settings"},
    "zhiji_save_model_settings": {"kind": "app_management", "subjects": False, "write": True,
                                  "handler": "_save_model_settings"},
    "zhiji_get_plugins": {"kind": "app_management", "subjects": False, "handler": "_get_plugins"},
    "zhiji_set_plugin_enabled": {"kind": "app_management", "subjects": False, "write": True,
                                 "handler": "_set_plugin_enabled"},
    "zhiji_get_usage": {"kind": "app_management", "subjects": False, "aggregate": True,
                        "recomputable": True, "handler": "_get_usage"},
    "zhiji_delete_account": {"kind": "app_management", "subjects": False, "write": True,
                             "handler": "_delete_account"},
    "zhiji_clear_local_data": {"kind": "app_management", "subjects": False, "write": True,
                               "handler": "_clear_local_data"},
}


class McpError(Exception):
    """A tool-call or handshake failure the loopback route reports verbatim."""

    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def install_source_fingerprint(root, data_root_path=None):
    """Install + data-root identity that contact grants bind to.

    A grant made under one data root never silently matches another copy of the
    same-named account. ``data_root_path`` may be None (install-only identity).
    """
    canonical = json.dumps([str(Path(root).resolve()).casefold(), str(data_root_path or "").casefold()],
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _request_id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(char) < 32 for char in value):
        raise McpError(400, "invalid-request", "请求标识不正确")
    return value


def _params_hash(action, account, subjects, params):
    canonical = json.dumps([action, account, subjects, params], ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _subjects(value):
    """Explicit subjects of one request: ``(accountId, subjectType, subjectId)`` tuples.

    Names, nicknames or paths in tool parameters are data, never authorization input.
    """
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 32:
        raise McpError(400, "invalid-request", "请求主体列表不正确")
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"accountId", "subjectType", "subjectId"}:
            raise McpError(400, "invalid-request", "请求主体不正确")
        account_id, subject_type, subject_id = item["accountId"], item["subjectType"], item["subjectId"]
        if not isinstance(account_id, str) or not 1 <= len(account_id) <= ACCOUNT_ID_LIMIT:
            raise McpError(400, "invalid-request", "账号标识不正确")
        if subject_type not in ("wechat_contact", "external_contact"):
            raise McpError(400, "invalid-request", "主体类型不正确")
        if not isinstance(subject_id, str) or not 1 <= len(subject_id) <= SUBJECT_ID_LIMIT:
            raise McpError(400, "invalid-request", "主体标识不正确")
        result.append((account_id, subject_type, subject_id))
    return result


def _account(value):
    if value is None:
        return None
    if not isinstance(value, str) or not 1 <= len(value) <= ACCOUNT_ID_LIMIT:
        raise McpError(400, "invalid-request", "账号标识不正确")
    return value


def _pid(value):
    """The MCP adapter's process id, so a dead holder's connection lease is reclaimed."""
    if value is None:
        return None
    if type(value) is not int or not 1 <= value <= 2 ** 31 - 1:
        raise McpError(400, "invalid-request", "进程标识不正确")
    return value


def _single_subject(body, action_subjects):
    """Read tools address exactly one WeChat contact; external imports arrive with their family."""
    subjects = _subjects(action_subjects)
    if len(subjects) != 1:
        raise McpError(400, "invalid-request", "该工具一次只接受一个联系人")
    account_id, subject_type, subject_id = subjects[0]
    if subject_type != "wechat_contact":
        raise McpError(400, "invalid-request", "该工具只接受微信联系人主体")
    if body.get("account") not in (None, account_id):
        raise McpError(400, "invalid-request", "账号与请求主体不一致")
    return account_id, subject_id


def _external_subject(body):
    """Import tools address exactly one external contact of the fixed pseudo-account.

    授权键是素材库里的稳定好友 ID；一个显示名永远不是主体，也不能借此落到某个
    微信联系人上。
    """
    subjects = _subjects(body.get("subjects"))
    if len(subjects) != 1:
        raise McpError(400, "invalid-request", "该工具一次只接受一个外部联系人")
    account_id, subject_type, subject_id = subjects[0]
    if account_id != EXTERNAL_ACCOUNT or subject_type != "external_contact":
        raise McpError(400, "invalid-request", "该工具只接受外部联系人主体")
    return subject_id


class McpServices:
    """Owns the MCP store, connection tokens and the registered tool dispatch."""

    def __init__(self, local, *, source_fingerprint=None, monotonic=time.monotonic):
        self.store = McpStore(Path(local) / "core" / "mcp")
        self.lock = threading.Lock()
        self.monotonic = monotonic
        self._sessions = {}  # token -> {"clientId", "authVersion", "pid", "expires"}
        self._fingerprint = source_fingerprint if callable(source_fingerprint) else (lambda: None)
        # Wired by Core: one shared-lease service ("mcp" lease per connection), the
        # explicit saved-account scope behind the offline read and write tools, and
        # the analysis-task bridge behind the phase-3b task tools.
        self.leases = None
        self.offline = None
        self.analysis = None
        # 阶段 3b 第三批：聊天助手与外部聊天导入的业务桥。
        self.assistant = None
        self.imports = None
        # 阶段 5：应用管理桥（模型设置/插件状态/用量/高影响删除）与确认服务。
        self.manage = None
        self.confirm = None
        # MCP 发起的任务：task id -> {"clientId", "authVersion"}；只有发起客户端能
        # 轮询/停止，撤权（授权版本提升）时全部失效并停止。
        self._task_clients = {}
        # MCP 发起的助手提问：run id -> {"clientId", "authVersion", "account", "user"}。
        self._run_clients = {}
        # MCP 发起的导入批次：batch id -> {"clientId", "authVersion"}。
        self._batch_clients = {}
        self.store.add_version_listener(self._authorization_changed)
        # One write gate per client: identical request ids never race their dedup check.
        self._gate_lock = threading.Lock()
        self._write_gates = {}
        # Wired by Core (阶段 6): whether the kyf.mcp plugin is currently enabled and
        # installed. Disabling or uninstalling the plugin leaves no callable MCP entry:
        # new handshakes and every authenticated route refuse with a stable code.
        self.plugin_gate = None

    def close(self):
        self.store.close()

    def _plugin_open(self):
        """Fail closed: a missing gate (test seams) allows, a broken one refuses."""
        gate = self.plugin_gate
        if gate is None:
            return True
        try:
            return bool(gate())
        except Exception:
            return False

    # -- source identity -----------------------------------------------------
    def fingerprint(self):
        try:
            value = self._fingerprint()
        except Exception:
            value = None
        return value if isinstance(value, str) and value else None

    # -- connection tokens ---------------------------------------------------
    def handshake(self, body):
        """Exchange the once-shown pairing secret for a short-lived connection token.

        The adapter passes its process id so Core can reclaim the connection lease
        when the holder dies; each connection is one ``mcp`` lease.
        """
        if not isinstance(body, dict) or not {"clientId", "credential"} <= set(body) or \
                set(body) - {"clientId", "credential", "pid"}:
            raise McpError(400, "invalid-request", "配对请求格式不正确")
        if not self.store.master_enabled():
            raise McpError(403, "mcp-disabled", "MCP 总开关已关闭")
        if not self._plugin_open():
            raise McpError(403, "mcp-plugin-disabled", "MCP 插件已停用或已卸载")
        client_id, secret = body["clientId"], body["credential"]
        pid = _pid(body.get("pid"))
        if pid is not None and not pid_alive(pid):
            # A connection lease bound to a dead holder would never be reclaimable.
            raise McpError(400, "invalid-request", "进程标识不正确")
        profile = self.store.client(client_id) if isinstance(client_id, str) else None
        valid = False
        if profile is not None and profile["enabled"]:
            valid = verify_credential(profile["credential_salt"], profile["credential_hash"], secret)
        if not valid:
            # One message for unknown id, disabled profile and wrong secret.
            raise McpError(401, "mcp-unauthorized", "客户端凭据无效")
        token = "zmt_" + secrets.token_hex(32)
        now = self.monotonic()
        with self.lock:
            self._prune_sessions(now)
            mine = sorted((session["expires"], key) for key, session in self._sessions.items()
                          if session["clientId"] == client_id)
            for _expires, key in mine[:max(0, len(mine) - MAX_SESSIONS_PER_CLIENT + 1)]:
                self._drop_session(key)
            self._sessions[token] = {"clientId": client_id, "authVersion": profile["auth_version"],
                                     "pid": pid, "expires": now + SESSION_TTL_MS / 1000}
        if self.leases is not None:
            self.leases.acquire("mcp", token, pid=pid)
        self.store.touch(client_id)
        return {"token": token, "clientId": client_id, "authVersion": profile["auth_version"],
                "expiresAtMs": now_ms() + SESSION_TTL_MS}

    def authenticate(self, headers):
        """The only identity the MCP route accepts: its own short-lived token."""
        if not self.store.master_enabled():
            raise McpError(403, "mcp-disabled", "MCP 总开关已关闭")
        if not self._plugin_open():
            raise McpError(403, "mcp-plugin-disabled", "MCP 插件已停用或已卸载")
        header = headers.get("Authorization") or "" if hasattr(headers, "get") else ""
        token = header[7:].strip() if header[:7].lower() == "bearer " else ""
        with self.lock:
            self._prune_sessions(self.monotonic())
            session = self._sessions.get(token) if isinstance(token, str) else None
            if session is None:
                raise McpError(401, "mcp-unauthorized", "MCP 令牌无效或已过期")
            # Activity slides the token TTL as well as the lease's idle clock.
            session["expires"] = self.monotonic() + SESSION_TTL_MS / 1000
        if self.leases is not None:
            self.leases.refresh("mcp", token)
        return {"clientId": session["clientId"], "authVersion": session["authVersion"]}

    def _drop_session(self, token):
        self._sessions.pop(token, None)
        if self.leases is not None:
            self.leases.release("mcp", token)

    def _prune_sessions(self, now):
        for token, session in list(self._sessions.items()):
            if session["expires"] <= now:
                self._drop_session(token)
            elif session.get("pid") is not None and not pid_alive(session["pid"]):
                # The adapter died without closing its connection; reclaim at once.
                self._drop_session(token)

    def active_connections(self):
        with self.lock:
            self._prune_sessions(self.monotonic())
            return len(self._sessions)

    def disconnect_all(self):
        """Drop every connection token at once; in-flight calls fail on their next check."""
        with self.lock:
            count = len(self._sessions)
            for token in list(self._sessions):
                self._drop_session(token)
            return count

    # -- authorization (§4.3) ------------------------------------------------
    def authorize(self, principal, action, *, account=None, subjects=()):
        """The single Core-side decision; every tool call passes through here.

        Returns the decision scope, or raises ``McpError`` with a stable code.
        """
        if not isinstance(principal, dict) or "clientId" not in principal or "authVersion" not in principal:
            raise McpError(401, "mcp-unauthorized", "MCP 令牌无效或已过期")
        profile = self.store.client(principal["clientId"])
        if profile is None or not profile["enabled"]:
            raise McpError(403, "client-suspended", "MCP 客户端已停用或已撤销")
        if profile["auth_version"] != principal["authVersion"]:
            # Mode switches, contact revocations and credential regenerations all
            # bump the version; old tokens and in-flight operations stop here.
            raise McpError(403, "stale-authorization", "授权已变化，请重新配对后调用")
        if action in FIXED_EXCLUSIONS:
            raise McpError(403, "fixed-exclusion", "该动作不属于 MCP 能力")
        entry = REGISTRY.get(action)
        if entry is None:
            raise McpError(403, "action-not-registered", "动作未登记")
        mode = profile["mode"]
        if mode == "per_contact":
            if entry["kind"] == "app_management" and not profile["global_management"]:
                raise McpError(403, "management-not-enabled", "该客户端未开启全局管理")
            if entry.get("aggregate") and not entry.get("recomputable"):
                raise McpError(403, "aggregate-blocked", "逐联系人模式不提供账号级汇总")
            if entry.get("subjects"):
                if not subjects:
                    raise McpError(403, "contact-not-authorized", "请求未指明已授权联系人")
                fingerprint = self.fingerprint()
                for subject_account, subject_type, subject_id in subjects:
                    if not self.store.has_grant(principal["clientId"], fingerprint, subject_account,
                                                subject_type, subject_id):
                        raise McpError(403, "contact-not-authorized", "该联系人未获授权")
        return {"mode": mode, "scope": "authorized_contacts" if mode == "per_contact" else "installation"}

    # -- dispatch ------------------------------------------------------------
    def call(self, principal, body):
        """Validate, authorize, persist dedup, then dispatch one registered action."""
        if not isinstance(body, dict) or set(body) - {"action", "params", "account", "subjects", "requestId"}:
            raise McpError(400, "invalid-request", "请求格式不正确")
        action = body.get("action")
        if not isinstance(action, str) or not re.fullmatch(r"[a-z0-9_.]{1,64}", action or ""):
            raise McpError(400, "invalid-request", "动作名称不正确")
        params = body.get("params", {})
        if not isinstance(params, dict):
            raise McpError(400, "invalid-request", "参数不正确")
        account = _account(body.get("account"))
        subjects = _subjects(body.get("subjects"))
        request_id = _request_id(body["requestId"]) if "requestId" in body else None
        started = time.monotonic()
        client_id = principal.get("clientId") if isinstance(principal, dict) else None
        if not isinstance(client_id, str) or not client_id:
            client_id = "unknown"
        entry = REGISTRY.get(action)
        try:
            decision = self.authorize(principal, action, account=account, subjects=subjects)
            if entry.get("write"):
                result = self._write_dispatch(principal, entry, body, decision, action=action,
                                              client_id=client_id, request_id=request_id,
                                              account=account, subjects=subjects, params=params)
            else:
                handler = entry.get("handler")
                if handler is None:
                    raise McpError(501, "capability-not-open", "该能力尚未开放")
                if request_id is not None:
                    # Persisted before dispatch so a later external commit is always attributable.
                    try:
                        self.store.dedup_begin(client_id, request_id, action,
                                               _params_hash(action, account, subjects, params))
                    except McpStoreError as exc:
                        raise McpError(exc.status, exc.code, exc.message) from None
                result = getattr(self, handler)(principal, body, decision)
            self.store.log_call(client_id, action, account_id=account,
                                subjects=[list(subject) for subject in subjects],
                                result_code="ok",
                                duration_ms=int((time.monotonic() - started) * 1000))
            return result
        except McpError as exc:
            self.store.log_call(client_id, action, account_id=account,
                                subjects=[list(subject) for subject in subjects],
                                result_code=exc.code,
                                duration_ms=int((time.monotonic() - started) * 1000))
            raise

    def _write_gate(self, client_id):
        """Serialize one client's writes so identical request ids cannot race their dedup."""
        with self._gate_lock:
            gate = self._write_gates.get(client_id)
            if gate is None:
                gate = self._write_gates[client_id] = threading.Lock()
            return gate

    def _write_dispatch(self, principal, entry, body, decision, *, action, client_id,
                        request_id, account, subjects, params):
        """Versioned writes: requestId 必填，去重记录先于副作用，重试返回原结果。

        同 ID 同参数的已完成请求直接返回记录的结果，不再重复生效；同 ID 不同参数
        由去重表拒绝（request-id-conflict）；上次失败（未生效）的同一请求可安全
        重试；进程中途崩溃留下的 recorded 记录结果未知，绝不盲目重复执行。
        """
        if request_id is None:
            raise McpError(400, "invalid-request", "写操作必须携带请求标识")
        digest = _params_hash(action, account, subjects, params)
        with self._write_gate(client_id):
            try:
                state = self.store.dedup_begin(client_id, request_id, action, digest)
            except McpStoreError as exc:
                raise McpError(exc.status, exc.code, exc.message) from None
            if state == "replay":
                prior = self.store.dedup_result(client_id, request_id)
                if prior is None or prior["state"] == "recorded":
                    raise McpError(409, "request-state-unknown",
                                   "同一请求标识的先前结果未知，请更换新的请求标识重试")
                if prior["state"] == "done":
                    return prior["result"]
                # failed：上次请求未生效（未产生副作用），同一参数可安全重试。
            handler = entry.get("handler")
            try:
                if handler is None:
                    raise McpError(501, "capability-not-open", "该能力尚未开放")
                result = getattr(self, handler)(principal, body, decision)
            except BaseException:
                self.store.dedup_complete(client_id, request_id, ok=False)
                raise
            self.store.dedup_complete(client_id, request_id, result)
            return result

    def _ping(self, principal, body, decision):
        """Connectivity and own-permission summary; reads no personal data at all."""
        profile = self.store.client(principal["clientId"])
        return {"service": "zhiji-mcp", "protocol": 1,
                "profile": {"clientId": principal["clientId"], "mode": decision["mode"],
                            "scope": decision["scope"],
                            "globalManagement": bool(profile["global_management"]),
                            "authVersion": profile["auth_version"]},
                "server": {"openCapabilities": sorted(name for name, entry in REGISTRY.items()
                                                      if entry.get("handler"))}}

    # -- offline saved-account reads (§3.2) ----------------------------------
    def _offline_provider(self):
        provider = self.offline
        if not callable(provider):
            raise McpError(501, "capability-not-open", "离线资料服务尚未接入")
        try:
            scope = provider()
        except McpError:
            raise
        except Exception:
            raise McpError(503, "core-unavailable", "资料服务暂时不可用") from None
        if scope is None:
            raise McpError(501, "capability-not-open", "离线资料服务尚未接入")
        return scope

    def _cursor_encode(self, payload):
        """An opaque pagination cursor bound to client, authorization version and source identity."""
        body = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                                   separators=(",", ":")).encode("utf-8")).decode("ascii")
        digest = hmac.new(bytes.fromhex(self.store.cursor_key()), body.encode("ascii"), hashlib.sha256)
        return body + "." + digest.hexdigest()[:32]

    def _cursor_decode(self, cursor, *, principal, account_id, subject_id):
        """Reject cursors from another client, another authorization version or another data root."""
        if not isinstance(cursor, str) or cursor.count(".") != 1:
            raise McpError(400, "cursor-invalid", "游标无效或已失效")
        body, signature = cursor.split(".")
        if not re.fullmatch(r"[0-9a-f]{32}", signature):
            raise McpError(400, "cursor-invalid", "游标无效或已失效")
        expected = hmac.new(bytes.fromhex(self.store.cursor_key()), body.encode("ascii"),
                            hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(expected, signature):
            raise McpError(400, "cursor-invalid", "游标无效或已失效")
        try:
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode("utf-8"))
        except (ValueError, UnicodeError):
            raise McpError(400, "cursor-invalid", "游标无效或已失效") from None
        if not isinstance(payload, dict) or payload.get("c") != principal["clientId"] or \
                payload.get("v") != principal["authVersion"] or payload.get("f") != self.fingerprint() or \
                payload.get("a") != account_id or payload.get("u") != subject_id:
            raise McpError(400, "cursor-invalid", "游标无效或已失效")
        offset = payload.get("o")
        if type(offset) is not int or offset < 0:
            raise McpError(400, "cursor-invalid", "游标无效或已失效")
        return offset

    def _list_accounts(self, principal, body, decision):
        provider = self._offline_provider()
        allowed = None
        if decision["mode"] == "per_contact":
            # Per-contact mode lists only accounts where this client holds a grant
            # under the current install + data root, and every summary is recomputed
            # over exactly those granted contacts -- never over the whole account.
            allowed = {}
            for grant in self.store.grants(principal["clientId"], self.fingerprint()):
                if grant["subjectType"] == "wechat_contact":
                    allowed.setdefault(grant["accountId"], []).append(grant["subjectId"])
            allowed = {account: sorted(set(subjects)) for account, subjects in allowed.items()}
        return provider.list_accounts(allowed)

    def _paged_subject_read(self, principal, body, decision, *, allowed_params, default_limit,
                            max_limit, page_field, run):
        """Shared shape of the bounded per-contact reads: params check, page size, HMAC cursor."""
        account_id, subject_id = _single_subject(body, body.get("subjects"))
        params = body.get("params", {})
        if not isinstance(params, dict) or set(params) - allowed_params:
            raise McpError(400, "invalid-request", "参数不正确")
        limit = params.get("limit", default_limit)
        if type(limit) is not int or not 1 <= limit <= max_limit:
            raise McpError(400, "invalid-request", "分页参数不正确")
        offset = 0
        if params.get("cursor") is not None:
            offset = self._cursor_decode(params["cursor"], principal=principal,
                                         account_id=account_id, subject_id=subject_id)
        result = run(account_id, subject_id, params, limit, offset)
        next_cursor = None
        if len(result.get(page_field, [])) == limit:
            next_cursor = self._cursor_encode(self._cursor_payload(principal, account_id,
                                                                   subject_id, offset + limit))
        return {**result, "requestScope": decision["scope"], "cursor": next_cursor}

    def _cursor_payload(self, principal, account_id, subject_id, offset):
        return {"c": principal["clientId"], "v": principal["authVersion"],
                "f": self.fingerprint(), "a": account_id, "u": subject_id, "o": offset}

    def _search_messages(self, principal, body, decision):
        params = body.get("params", {})
        if not isinstance(params, dict) or not isinstance(params.get("query"), str) or \
                not params["query"].strip() or len(params["query"]) > 256:
            raise McpError(400, "invalid-request", "查询条件不正确")
        return self._paged_subject_read(principal, body, decision, allowed_params={"query", "limit", "cursor"},
                                        default_limit=20, max_limit=SEARCH_PAGE_MAX,
                                        page_field="messages",
                                        run=lambda a, u, params, limit, offset:
                                        self._offline_provider().search_messages(
                                            a, u, query=params["query"], limit=limit, offset=offset))

    def _get_message_context(self, principal, body, decision):
        account_id, subject_id = _single_subject(body, body.get("subjects"))
        params = body.get("params", {})
        if not isinstance(params, dict) or set(params) != {"messageId"}:
            raise McpError(400, "invalid-request", "参数不正确")
        result = self._offline_provider().message_context(account_id, subject_id,
                                                          params["messageId"])
        return {**result, "requestScope": decision["scope"]}

    def _list_memories(self, principal, body, decision):
        params = body.get("params", {})
        if not isinstance(params, dict) or not isinstance(params.get("status", "active"), str) or \
                params.get("status", "active") not in ("active", "pending", "confirmed", "rejected",
                                                       "user_observed"):
            raise McpError(400, "invalid-request", "状态筛选不正确")
        return self._paged_subject_read(principal, body, decision,
                                        allowed_params={"status", "limit", "cursor"},
                                        default_limit=50, max_limit=MEMORY_PAGE_MAX,
                                        page_field="items",
                                        run=lambda a, u, params, limit, offset:
                                        self._offline_provider().list_memories(
                                            a, u, status=params.get("status", "active"),
                                            limit=limit, offset=offset))

    def _get_memory_evidence(self, principal, body, decision):
        account_id, subject_id = _single_subject(body, body.get("subjects"))
        params = body.get("params", {})
        if not isinstance(params, dict) or set(params) - {"memoryId", "ordinal"} or \
                "memoryId" not in params or not isinstance(params["memoryId"], str) or \
                not 1 <= len(params["memoryId"]) <= 256:
            raise McpError(400, "invalid-request", "参数不正确")
        ordinal = params.get("ordinal")
        if ordinal is not None and (type(ordinal) is not int or not 0 <= ordinal <= EVIDENCE_ORDINAL_MAX):
            raise McpError(400, "invalid-request", "证据编号不正确")
        result = self._offline_provider().memory_evidence(account_id, subject_id,
                                                          params["memoryId"], ordinal=ordinal)
        return {**result, "requestScope": decision["scope"]}

    def _get_media(self, principal, body, decision):
        account_id, subject_id = _single_subject(body, body.get("subjects"))
        params = body.get("params", {})
        if not isinstance(params, dict) or set(params) - {"messageId", "frames"} or \
                not isinstance(params.get("messageId"), str) or not params["messageId"] or \
                len(params["messageId"]) > 200:
            raise McpError(400, "invalid-request", "参数不正确")
        frames = params.get("frames", 1)
        if type(frames) is not int or not 1 <= frames <= MEDIA_MAX_FRAMES:
            raise McpError(400, "invalid-request", "帧数参数不正确")
        result = self._offline_provider().media(account_id, subject_id, params["messageId"],
                                                frames=frames)
        items = result.get("items")
        if isinstance(items, list) and any(not isinstance(item, dict) or
                                           len(item.get("data") or "") > MEDIA_ITEM_MAX_BASE64
                                           for item in items):
            # 契约上限在任何提供方下都必须成立：超限内容一律不内嵌，只报告不可用。
            result.update(available=False, reason="unavailable", items=[])
        return {**result, "requestScope": decision["scope"]}

    # -- phase 3b: versioned human-data writes (§5) ---------------------------
    # 契约只声明可修改字段并拒绝额外字段；提交前在同一入口重新核对授权版本与
    # 范围（撤权或版本提升使在途未提交写请求失效），版本条件由账户数据库事务
    # 内复核。写响应只带新版本/状态，不回显正文。

    def _write_params(self, params, allowed, required):
        """Reject extra fields and require the contract's keys before anything is written."""
        if not isinstance(params, dict) or set(params) - allowed or \
                any(key not in params for key in required):
            raise McpError(400, "invalid-request", "写入参数不正确")
        return params

    def _contact_write_scope(self, principal, action, body, allowed, required=()):
        """One authorized WeChat contact + strict params + a fresh authorization check."""
        account_id, subject_id = _single_subject(body, body.get("subjects"))
        params = self._write_params(body.get("params", {}), allowed, required)
        # 提交前重新核对：不能只信入口判定。
        self.authorize(principal, action, account=account_id,
                       subjects=[(account_id, "wechat_contact", subject_id)])
        return account_id, subject_id, params

    def _account_write_scope(self, principal, action, body, allowed, required):
        """Account-level write scope (self profile); per-contact mode never reaches here."""
        account_id = _account(body.get("account"))
        if account_id is None:
            raise McpError(400, "invalid-request", "缺少账号标识")
        params = self._write_params(body.get("params", {}), allowed, required)
        self.authorize(principal, action, account=account_id, subjects=[])
        return account_id, params

    @staticmethod
    def _memory_id(params):
        value = params.get("memoryId")
        if not isinstance(value, str) or not 1 <= len(value) <= MEMORY_ID_LIMIT:
            raise McpError(400, "invalid-request", "记忆标识不正确")
        return value

    @staticmethod
    def _observation_id(params):
        value = params.get("observationId")
        if not isinstance(value, str) or not 1 <= len(value) <= OBSERVATION_ID_LIMIT:
            raise McpError(400, "invalid-request", "观察记录标识不正确")
        return value

    @staticmethod
    def _expected_version(params):
        value = params.get("expectedVersion")
        if type(value) is not int or value < 0:
            raise McpError(400, "invalid-request", "预期版本不正确")
        return value

    @staticmethod
    def _tags(value):
        """Hashable strings only; the store then checks membership in the fixed tag set."""
        if not isinstance(value, list) or not 1 <= len(value) <= len(TAGS) or \
                any(not isinstance(tag, str) or not 1 <= len(tag) <= 64 for tag in value):
            raise McpError(400, "invalid-request", "标签不正确")
        return value

    def _deletion_confirm_intent(self, principal, kind, account_id, subject_id, params):
        """阶段 5 挂钩位：普通记忆/观察删除不套用全量清除确认（§7），这里预留
        独立确认意图的接入点。届时返回绑定客户端、授权版本、账号主体与参数哈希
        的待确认意图，删除处理改为登记意图并返回而不执行；当前始终返回 None。"""
        return None

    def _create_memory(self, principal, body, decision):
        account_id, subject_id, params = self._contact_write_scope(
            principal, "zhiji_create_memory", body, {"text", "tags", "eventTime"},
            ("text", "tags"))
        tags = self._tags(params["tags"])
        result = self._offline_provider().memory_write(account_id, subject_id,
            {"action": "create", "text": params["text"], "tags": tags,
             "eventTime": params.get("eventTime")})
        return {**result, "requestScope": decision["scope"]}

    def _update_memory(self, principal, body, decision):
        account_id, subject_id, params = self._contact_write_scope(
            principal, "zhiji_update_memory", body,
            {"memoryId", "text", "tags", "eventTime", "expectedVersion"},
            ("memoryId", "text", "tags", "eventTime", "expectedVersion"))
        # eventTime 为整体替换契约的一部分：显式传 null 才清除，缺省即参数错误。
        self._memory_id(params)
        self._expected_version(params)
        tags = self._tags(params["tags"])
        result = self._offline_provider().memory_write(account_id, subject_id,
            {"action": "edit", "id": params["memoryId"], "text": params["text"],
             "tags": tags, "eventTime": params["eventTime"],
             "expectedVersion": params["expectedVersion"]})
        return {**result, "requestScope": decision["scope"]}

    def _confirm_memory(self, principal, body, decision):
        account_id, subject_id, params = self._contact_write_scope(
            principal, "zhiji_confirm_memory", body,
            {"memoryId", "expectedVersion", "choice"}, ("memoryId", "expectedVersion"))
        self._memory_id(params)
        self._expected_version(params)
        choice = params.get("choice")
        if choice is not None and choice not in ("keep", "adopt", "both"):
            raise McpError(400, "invalid-request", "确认选项不正确")
        result = self._offline_provider().memory_write(account_id, subject_id,
            {"action": "confirm", "id": params["memoryId"],
             "expectedVersion": params["expectedVersion"], "choice": choice})
        return {**result, "requestScope": decision["scope"]}

    def _delete_memory(self, principal, body, decision):
        account_id, subject_id, params = self._contact_write_scope(
            principal, "zhiji_delete_memory", body, {"memoryId", "expectedVersion"},
            ("memoryId", "expectedVersion"))
        memory_id = self._memory_id(params)
        self._expected_version(params)
        intent = self._deletion_confirm_intent(principal, "memory", account_id, subject_id, params)
        if intent is not None:  # pragma: no cover - 阶段 5 接入独立确认时生效
            return intent
        result = self._offline_provider().memory_write(account_id, subject_id,
            {"action": "delete", "id": memory_id, "expectedVersion": params["expectedVersion"]})
        return {**result, "requestScope": decision["scope"]}

    def _get_contact_goal(self, principal, body, decision):
        account_id, subject_id = _single_subject(body, body.get("subjects"))
        return {**self._offline_provider().contact_goal(account_id, subject_id),
                "requestScope": decision["scope"]}

    def _save_contact_goal(self, principal, body, decision):
        account_id, subject_id, params = self._contact_write_scope(
            principal, "zhiji_save_contact_goal", body, {"text", "expectedVersion"},
            ("text", "expectedVersion"))
        self._expected_version(params)
        result = self._offline_provider().save_contact_goal(account_id, subject_id,
                                                            text=params["text"],
                                                            expected_version=params["expectedVersion"])
        return {**result, "requestScope": decision["scope"]}

    def _get_self_profile(self, principal, body, decision):
        account_id = _account(body.get("account"))
        if account_id is None:
            raise McpError(400, "invalid-request", "缺少账号标识")
        return {**self._offline_provider().self_profile(account_id),
                "requestScope": decision["scope"]}

    def _save_self_profile(self, principal, body, decision):
        account_id, params = self._account_write_scope(
            principal, "zhiji_save_self_profile", body,
            {"background", "style", "expectedVersion"},
            ("background", "style", "expectedVersion"))
        self._expected_version(params)
        result = self._offline_provider().save_self_profile(account_id,
                                                            background=params["background"],
                                                            style=params["style"],
                                                            expected_version=params["expectedVersion"])
        return {**result, "requestScope": decision["scope"]}

    def _list_observations(self, principal, body, decision):
        return self._paged_subject_read(principal, body, decision,
                                        allowed_params={"limit", "cursor"},
                                        default_limit=50, max_limit=OBSERVATION_PAGE_MAX,
                                        page_field="items",
                                        run=lambda a, u, params, limit, offset:
                                        self._offline_provider().observations(
                                            a, u, limit=limit, offset=offset))

    def _save_observation(self, principal, body, decision):
        account_id, subject_id, params = self._contact_write_scope(
            principal, "zhiji_save_observation", body,
            {"observationId", "judgement", "note", "dimension", "expectedVersion"},
            ("judgement",))
        observation_id = params.get("observationId")
        request = {"action": "save", "id": observation_id, "judgement": params["judgement"],
                   "note": params.get("note", ""), "dimension": params.get("dimension")}
        if observation_id is not None:
            # 更新必须带预期版本；新增没有可过期的版本。
            self._observation_id(params)
            self._expected_version(params)
            request["expectedVersion"] = params["expectedVersion"]
        result = self._offline_provider().observation_write(account_id, subject_id, request)
        return {**result, "requestScope": decision["scope"]}

    def _delete_observation(self, principal, body, decision):
        account_id, subject_id, params = self._contact_write_scope(
            principal, "zhiji_delete_observation", body,
            {"observationId", "expectedVersion"}, ("observationId", "expectedVersion"))
        self._observation_id(params)
        self._expected_version(params)
        intent = self._deletion_confirm_intent(principal, "observation", account_id,
                                               subject_id, params)
        if intent is not None:  # pragma: no cover - 阶段 5 接入独立确认时生效
            return intent
        result = self._offline_provider().observation_write(account_id, subject_id,
            {"action": "delete", "id": params["observationId"],
             "expectedVersion": params["expectedVersion"]})
        return {**result, "requestScope": decision["scope"]}

    # -- phase 3b second batch: analysis tasks (§5) ----------------------------
    # 开始好友分析经共享 TaskManager（租约、代次与迟到结果规则同 GUI 任务）；逐句
    # 分析、回复建议与自我剖析经插件仅有的几个登记 RPC。所有任务只按显式账号与稳
    # 定联系人 ID 寻址；模型调用全部留在插件与 Core 网关内，客户端拿不到任何密钥。
    # 撤权（授权版本提升）通过 store 的版本侦听停止该客户端的全部任务绑定。

    TASK_CONFLICTS = ("queue", "stop-other")
    INSIGHT_IDS_MAX = 20
    MESSAGE_ID_LIMIT = 200
    ANALYSIS_RESULT_MAX_CHARS = 512 * 1024
    FRIEND_ANALYSIS_SECTIONS = {"status": "scheme.status", "overview": "page.overview",
                                "persona": "page.persona", "chat": "page.chat", "trend": "page.trend"}
    SELF_ANALYSIS_SECTIONS = {"status": "self.analysisStatus", "persona": "self.persona",
                              "values": "self.values", "tags": "self.tags",
                              "tagEvidence": "self.tagEvidence", "periods": "self.periods",
                              "periodDetail": "self.periodDetail", "timeline": "self.timeline",
                              "timelineEvidence": "self.timelineEvidence"}
    SELF_ANALYSIS_PARAMS = {"tagEvidence": {"name"}, "periodDetail": {"period", "key"},
                            "timeline": {"offset"}, "timelineEvidence": {"id"}}
    # 时间线单页留出时期、主线与信封的余量，续读用 nextOffset。
    SELF_TIMELINE_PAGE_BYTES = 384 * 1024
    SELF_TIMELINE_EDIT_PARAMS = frozenset(("target", "action", "id", "fields", "expectedVersion"))

    def _analysis_provider(self):
        provider = self.analysis
        if provider is None:
            raise McpError(501, "capability-not-open", "分析任务服务尚未接入")
        return provider

    def _assistant_provider(self):
        provider = self.assistant
        if provider is None:
            raise McpError(501, "capability-not-open", "聊天助手服务尚未接入")
        return provider

    def _import_provider(self):
        provider = self.imports
        if provider is None:
            raise McpError(501, "capability-not-open", "外部聊天导入服务尚未接入")
        return provider

    def _register_task(self, principal, task_id):
        """Bind a task id to its starting client, so only it can poll and stop."""
        profile = self.store.client(principal["clientId"])
        with self.lock:
            self._task_clients[task_id] = {"clientId": principal["clientId"],
                                           "authVersion": profile["auth_version"] if profile else None}

    def _register_run(self, principal, run_id, account, user):
        """Bind an assistant question run to its starting client and discussion scope."""
        profile = self.store.client(principal["clientId"])
        with self.lock:
            self._run_clients[run_id] = {"clientId": principal["clientId"],
                                         "authVersion": profile["auth_version"] if profile else None,
                                         "account": account, "user": user}

    def _register_batch(self, principal, batch_id):
        """Bind an import batch run to its starting client."""
        profile = self.store.client(principal["clientId"])
        with self.lock:
            self._batch_clients[batch_id] = {"clientId": principal["clientId"],
                                             "authVersion": profile["auth_version"] if profile else None}

    def _authorization_changed(self, client_id):
        """授权版本提升后，该客户端发起的任务全部失效：排队任务关闭、在途任务吊销。"""
        with self.lock:
            stale = [task_id for task_id, binding in self._task_clients.items()
                     if binding["clientId"] == client_id]
            for task_id in stale:
                self._task_clients.pop(task_id, None)
            stale_runs = [(run_id, binding) for run_id, binding in self._run_clients.items()
                          if binding["clientId"] == client_id]
            for run_id, _binding in stale_runs:
                self._run_clients.pop(run_id, None)
            stale_batches = [batch_id for batch_id, binding in self._batch_clients.items()
                             if binding["clientId"] == client_id]
            for batch_id in stale_batches:
                self._batch_clients.pop(batch_id, None)
        if not any((stale, stale_runs, stale_batches)):
            return
        try:
            provider = self._analysis_provider()
        except McpError:
            provider = None
        for task_id in stale:
            if provider is not None:
                try:
                    provider.stop_task(task_id)
                except Exception:
                    pass  # 任务已不存在或服务正关闭；绑定已被清除，不会再被轮询到。
        try:
            assistant = self._assistant_provider()
        except McpError:
            assistant = None
        for run_id, binding in stale_runs:
            if assistant is not None:
                try:
                    assistant.stop(binding["account"], binding["user"], run_id)
                except Exception:
                    pass  # 会话已关闭或服务正关闭；停止意图已尽力，不再重试。
        try:
            imports = self._import_provider()
        except McpError:
            imports = None
        for batch_id in stale_batches:
            if imports is not None:
                try:
                    imports.stop_client_batch(batch_id)
                except Exception:
                    pass  # 批次已结束或服务正关闭；绑定已被清除。

    def _own_task(self, principal, params):
        """One task id the calling client itself registered; anything else is missing."""
        if not isinstance(params, dict) or set(params) - {"taskId"} or \
                not isinstance(params.get("taskId"), str) or not params["taskId"] or \
                len(params["taskId"]) > 128:
            raise McpError(400, "invalid-request", "参数不正确")
        with self.lock:
            binding = self._task_clients.get(params["taskId"])
        if binding is None or binding["clientId"] != principal["clientId"]:
            raise McpError(404, "not-found", "任务不存在或不属于本客户端")
        return params["taskId"]

    @staticmethod
    def _task_summary(task):
        """The bounded task view; titles and params never carry chat text."""
        return {"taskId": task["id"], "plugin": task["plugin"], "kind": task["kind"],
                "account": task["account"], "user": task["user"], "scheme": task["scheme"],
                "state": task["state"], "progress": task.get("progress"), "error": task.get("error"),
                "createdAt": task.get("createdAt"), "startedAt": task.get("startedAt"),
                "finishedAt": task.get("finishedAt")}

    @staticmethod
    def _analysis_account(provider, account_id):
        """Resolve the explicit accountId; a foreign or mistyped id never reaches Core."""
        return provider.resolve_account(account_id)

    @staticmethod
    def _depth(value):
        if value is not None and value not in ("basic", "standard", "deep"):
            raise McpError(400, "invalid-request", "分析深度不正确")
        return value

    @staticmethod
    def _outputs(value):
        if value is not None and (not isinstance(value, list) or not value or
                                  any(item not in ("profile", "tags", "timeline", "persona", "periods",
                                                   "values", "library") for item in value)):
            raise McpError(400, "invalid-request", "分析项目不正确")
        return value

    @staticmethod
    def _bounded(result):
        """A final guard on plugin-built payloads; oversized answers are refused, never trimmed."""
        if len(json.dumps(result, ensure_ascii=False)) > McpServices.ANALYSIS_RESULT_MAX_CHARS:
            raise McpError(503, "result-too-large", "结果超出单次返回上限，请缩小范围后重试")
        return result

    def _start_friend_analysis(self, principal, body, decision):
        account_id, subject_id, params = self._contact_write_scope(
            principal, "zhiji_start_friend_analysis", body, {"scheme", "conflict"})
        if subject_id.endswith("@chatroom"):
            raise McpError(400, "invalid-request", "群聊不提供好友分析")
        scheme = params.get("scheme")
        if scheme is not None and (not isinstance(scheme, str) or not 1 <= len(scheme) <= 48):
            raise McpError(400, "invalid-request", "分析方案不正确")
        conflict = params.get("conflict")
        if conflict is not None and conflict not in self.TASK_CONFLICTS:
            raise McpError(400, "invalid-request", "冲突处理方式不正确")
        provider = self._analysis_provider()
        account = self._analysis_account(provider, account_id)
        task = provider.start_friend_analysis(account, subject_id, scheme=scheme,
                                              conflict=conflict)["task"]
        self._register_task(principal, task["id"])
        return {"taskId": task["id"], "state": task["state"], "scheme": task["scheme"],
                "queuedBehind": task.get("afterTask"), "requestScope": decision["scope"]}

    def _get_friend_analysis(self, principal, body, decision):
        account_id, subject_id = _single_subject(body, body.get("subjects"))
        params = body.get("params", {})
        if not isinstance(params, dict) or set(params) - {"section"}:
            raise McpError(400, "invalid-request", "参数不正确")
        section = params.get("section", "status")
        method = self.FRIEND_ANALYSIS_SECTIONS.get(section)
        if method is None:
            raise McpError(400, "invalid-request", "结果分节不正确")
        provider = self._analysis_provider()
        account = self._analysis_account(provider, account_id)
        result = provider.friend_analysis_page(account, subject_id, method)
        return {**self._bounded(result), "section": section, "requestScope": decision["scope"]}

    def _get_task(self, principal, body, decision):
        task_id = self._own_task(principal, body.get("params"))
        task = self._analysis_provider().task(task_id)
        if task is None:
            raise McpError(404, "not-found", "任务不存在")
        return {"task": self._task_summary(task), "requestScope": decision["scope"]}

    def _stop_task(self, principal, body, decision):
        task_id = self._own_task(principal, body.get("params"))
        task = self._analysis_provider().stop_task(task_id)
        if task is None:
            raise McpError(404, "not-found", "任务不存在")
        return {"task": self._task_summary(task), "requestScope": decision["scope"]}

    @staticmethod
    def _insight_item(item):
        """One saved insight: states, applicability and the stored results themselves."""
        if not isinstance(item, dict):
            return {}
        trimmed = {"messageId": item.get("messageId"), "state": item.get("state"),
                   "analysisState": item.get("analysisState"),
                   "suggestionsState": item.get("suggestionsState"),
                   "applicability": item.get("applicability"),
                   "sourceState": item.get("sourceState"),
                   "sourceMessage": item.get("sourceMessage")}
        for key in ("analysis", "replies", "error", "suggestionsError"):
            if item.get(key) is not None:
                trimmed[key] = item[key]
        return trimmed

    def _start_message_insights(self, principal, body, decision):
        account_id, subject_id, params = self._contact_write_scope(
            principal, "zhiji_start_message_insights", body, {"messageId"}, ("messageId",))
        message_id = params["messageId"]
        if not isinstance(message_id, str) or not 1 <= len(message_id) <= self.MESSAGE_ID_LIMIT:
            raise McpError(400, "invalid-request", "消息标识不正确")
        provider = self._analysis_provider()
        account = self._analysis_account(provider, account_id)
        result = provider.start_message_insights(account, subject_id, message_id)
        item = next((item for item in result.get("items", [])
                     if isinstance(item, dict) and item.get("messageId") == message_id), None)
        if item is None:
            raise McpError(404, "not-found", "该消息没有分析与建议结果")
        return {**self._insight_item(item), "requestScope": decision["scope"]}

    def _get_message_insights(self, principal, body, decision):
        account_id, subject_id = _single_subject(body, body.get("subjects"))
        params = body.get("params", {})
        if not isinstance(params, dict) or set(params) - {"messageIds"}:
            raise McpError(400, "invalid-request", "参数不正确")
        ids = params.get("messageIds")
        if not isinstance(ids, list) or not ids or len(ids) > self.INSIGHT_IDS_MAX or \
                any(not isinstance(item, str) or not 1 <= len(item) <= self.MESSAGE_ID_LIMIT
                    for item in ids):
            raise McpError(400, "invalid-request", "消息标识列表不正确")
        provider = self._analysis_provider()
        account = self._analysis_account(provider, account_id)
        result = provider.message_insights(account, subject_id, ids)
        items = [self._insight_item(item) for item in
                 (result.get("items") or [])[:self.INSIGHT_IDS_MAX] if isinstance(item, dict)]
        return {"accountId": account_id, "user": subject_id, "items": self._bounded(items),
                "enabled": result.get("enabled"), "pendingCount": result.get("pendingCount"),
                "error": result.get("error"), "requestScope": decision["scope"]}

    def _self_analysis_account_scope(self, principal, action, body, allowed):
        """Account-level self-analysis scope; per-contact mode never reaches the provider."""
        account_id = _account(body.get("account"))
        if account_id is None:
            raise McpError(400, "invalid-request", "缺少账号标识")
        params = self._write_params(body.get("params", {}), allowed, ())
        self.authorize(principal, action, account=account_id, subjects=[])
        return account_id, params

    def _start_self_analysis(self, principal, body, decision):
        account_id, params = self._self_analysis_account_scope(
            principal, "zhiji_start_self_analysis", body, {"depth", "outputs"})
        request = {}
        depth = self._depth(params.get("depth"))
        outputs = self._outputs(params.get("outputs"))
        if depth is not None:
            request["depth"] = depth
        if outputs is not None:
            request["outputs"] = outputs
        provider = self._analysis_provider()
        account = self._analysis_account(provider, account_id)
        view = provider.start_self_analysis(account, request)
        return {"state": view.get("state"), "stopping": view.get("stopping"),
                "requestScope": decision["scope"]}

    def _self_analysis_section(self, params):
        if not isinstance(params, dict):
            raise McpError(400, "invalid-request", "参数不正确")
        section = params.get("section", "status")
        method = self.SELF_ANALYSIS_SECTIONS.get(section)
        if method is None:
            raise McpError(400, "invalid-request", "结果分节不正确")
        request = {key: value for key, value in params.items() if key != "section"}
        if set(request) - self.SELF_ANALYSIS_PARAMS.get(section, set()):
            raise McpError(400, "invalid-request", "参数不正确")
        if section == "timeline":
            request["pageBytes"] = self.SELF_TIMELINE_PAGE_BYTES
        return section, method, request

    def _get_self_analysis(self, principal, body, decision):
        account_id = _account(body.get("account"))
        if account_id is None:
            raise McpError(400, "invalid-request", "缺少账号标识")
        section, method, request = self._self_analysis_section(body.get("params", {}))
        provider = self._analysis_provider()
        account = self._analysis_account(provider, account_id)
        result = provider.self_analysis_page(account, method, request) if section != "status" else \
            provider.self_analysis_status(account)
        return {**self._bounded(result), "section": section, "requestScope": decision["scope"]}

    def _stop_self_analysis(self, principal, body, decision):
        account_id, _params = self._self_analysis_account_scope(
            principal, "zhiji_stop_self_analysis", body, set())
        provider = self._analysis_provider()
        account = self._analysis_account(provider, account_id)
        view = provider.stop_self_analysis(account)
        return {"state": view.get("state"), "stopping": view.get("stopping"),
                "requestScope": decision["scope"]}

    def _edit_self_timeline(self, principal, body, decision):
        account_id, params = self._account_write_scope(
            principal, "zhiji_edit_self_timeline", body, self.SELF_TIMELINE_EDIT_PARAMS,
            ("target", "action", "expectedVersion"))
        self._expected_version(params)
        provider = self._analysis_provider()
        account = self._analysis_account(provider, account_id)
        view = provider.edit_self_timeline(account, params)
        return {"version": view.get("version"), "id": view.get("editedId"),
                "label": view.get("editLabel"), "requestScope": decision["scope"]}

    def _undo_self_timeline(self, principal, body, decision):
        account_id, params = self._account_write_scope(
            principal, "zhiji_undo_self_timeline", body, {"expectedVersion"}, ("expectedVersion",))
        self._expected_version(params)
        provider = self._analysis_provider()
        account = self._analysis_account(provider, account_id)
        view = provider.undo_self_timeline(account, params)
        return {"version": view.get("version"), "undone": view.get("undone"),
                "requestScope": decision["scope"]}

    # -- phase 3b third batch: chat assistant (§5) ------------------------------
    # 讨论读取与提问/停止经 advisor 插件仅有的几个登记 RPC，账号与好友来自代理
    # 侧的已保存账号解析与授权主体；模板与技能是用户自己的配置，不含联系人资料。
    # 提交提问前每位 @ 好友参与者逐个核验授权（逐联系人模式任一未授权即整体拒
    # 绝，稳定码指明主体）。MCP 不代写草稿与排队消息，也不伪造微信事实。

    def _assistant_scope(self, principal, action, body, allowed, required=()):
        """One authorized WeChat contact + strict params + a fresh authorization check."""
        account_id, subject_id = _single_subject(body, body.get("subjects"))
        params = self._write_params(body.get("params", {}), allowed, required)
        # 提交前重新核对：不能只信入口判定。
        self.authorize(principal, action, account=account_id,
                       subjects=[(account_id, "wechat_contact", subject_id)])
        return account_id, subject_id, params

    @staticmethod
    def _assistant_id(params, key, label):
        value = params.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= ASSISTANT_ID_MAX:
            raise McpError(400, "invalid-request", label + "不正确")
        return value

    ASSISTANT_TEMPLATE_FIELDS = frozenset(("id", "name", "description", "prompt", "skillIds",
                                           "welcome"))

    def _no_extra(self, body):
        if body.get("params") or body.get("account") is not None:
            raise McpError(400, "invalid-request", "参数不正确")

    def _get_assistant_catalog(self, principal, body, decision):
        self._no_extra(body)
        result = self._assistant_provider().catalog()
        return {**self._bounded(result), "requestScope": decision["scope"]}

    def _save_assistant_template(self, principal, body, decision):
        params = self._write_params(body.get("params", {}), {"agent"}, ("agent",))
        agent = params["agent"]
        if not isinstance(agent, dict) or set(agent) - self.ASSISTANT_TEMPLATE_FIELDS or \
                len(json.dumps(agent, ensure_ascii=False)) > ASSISTANT_TEMPLATE_MAX_CHARS:
            raise McpError(400, "invalid-request", "助手模板不正确")
        result = self._assistant_provider().save_template(agent)
        return {**self._bounded(result), "requestScope": decision["scope"]}

    def _enable_assistant_template(self, principal, body, decision):
        params = self._write_params(body.get("params", {}), {"id", "enabled"}, ("id", "enabled"))
        agent_id = self._assistant_id(params, "id", "助手标识")
        if not isinstance(params["enabled"], bool):
            raise McpError(400, "invalid-request", "启停状态不正确")
        result = self._assistant_provider().enable_template(agent_id, params["enabled"])
        return {**self._bounded(result), "requestScope": decision["scope"]}

    def _delete_assistant_template(self, principal, body, decision):
        params = self._write_params(body.get("params", {}), {"id"}, ("id",))
        result = self._assistant_provider().delete_template(
            self._assistant_id(params, "id", "助手标识"))
        return {**self._bounded(result), "requestScope": decision["scope"]}

    def _import_assistant_skill(self, principal, body, decision):
        params = self._write_params(body.get("params", {}),
                                    {"name", "description", "content"},
                                    ("name", "description", "content"))
        for key in ("name", "description", "content"):
            if not isinstance(params[key], str) or len(params[key]) > ASSISTANT_SKILL_MAX_CHARS:
                raise McpError(400, "invalid-request", "技能内容不正确")
        result = self._assistant_provider().import_skill(params["name"], params["description"],
                                                         params["content"])
        return {**self._bounded(result), "requestScope": decision["scope"]}

    def _delete_assistant_skill(self, principal, body, decision):
        params = self._write_params(body.get("params", {}), {"id"}, ("id",))
        result = self._assistant_provider().delete_skill(
            self._assistant_id(params, "id", "技能标识"))
        return {**self._bounded(result), "requestScope": decision["scope"]}

    def _list_assistant_friends(self, principal, body, decision):
        self._no_extra(body)
        account_id, subject_id = _single_subject(body, body.get("subjects"))
        provider = self._assistant_provider()
        account = provider.resolve_account(account_id)
        result = provider.friends(account, subject_id)
        friends = result.get("friends") if isinstance(result, dict) else None
        if not isinstance(friends, list):
            raise McpError(503, "result-invalid", "好友列表格式不正确")
        if decision["mode"] == "per_contact":
            # 只列 @ 可选且已授权的好友：未授权联系人的名字与标识一律不出现。
            fingerprint = self.fingerprint()
            friends = [item for item in friends
                       if isinstance(item, dict) and
                       self.store.has_grant(principal["clientId"], fingerprint, account_id,
                                            "wechat_contact", item.get("user"))]
        return {"friends": friends, "requestScope": decision["scope"]}

    def _list_assistant_threads(self, principal, body, decision):
        account_id, subject_id, params = self._assistant_scope(
            principal, "zhiji_list_assistant_threads", body, {"agentId"})
        provider = self._assistant_provider()
        account = provider.resolve_account(account_id)
        result = provider.threads(account, subject_id, params.get("agentId"))
        return {**self._bounded(result), "requestScope": decision["scope"]}

    def _get_assistant_thread(self, principal, body, decision):
        account_id, subject_id, params = self._assistant_scope(
            principal, "zhiji_get_assistant_thread", body,
            {"agentId", "threadId", "beforeSeq"}, ("agentId", "threadId"))
        before_seq = params.get("beforeSeq")
        if before_seq is not None and (type(before_seq) is not int or before_seq < 0):
            raise McpError(400, "invalid-request", "分页位置不正确")
        provider = self._assistant_provider()
        account = provider.resolve_account(account_id)
        result = provider.thread(account, subject_id, params["agentId"], params["threadId"],
                                 before_seq=before_seq)
        return {**self._bounded(result), "requestScope": decision["scope"]}

    def _new_assistant_thread(self, principal, body, decision):
        account_id, subject_id, params = self._assistant_scope(
            principal, "zhiji_new_assistant_thread", body, {"agentId"}, ("agentId",))
        provider = self._assistant_provider()
        account = provider.resolve_account(account_id)
        result = provider.new_thread(account, subject_id, params["agentId"])
        return {**self._bounded(result), "requestScope": decision["scope"]}

    def _submit_assistant_question(self, principal, body, decision):
        account_id, subject_id, params = self._assistant_scope(
            principal, "zhiji_submit_assistant_question", body,
            {"agentId", "threadId", "message", "thinking", "mentions"},
            ("agentId", "threadId", "message"))
        message = params["message"]
        if not isinstance(message, str) or not message.strip() or \
                len(message) > ASSISTANT_MESSAGE_MAX or "\x00" in message:
            raise McpError(400, "invalid-request", "提问内容不正确")
        thinking = params.get("thinking", True)
        if not isinstance(thinking, bool):
            raise McpError(400, "invalid-request", "思考模式参数不正确")
        mentions = params.get("mentions") or []
        if not isinstance(mentions, list) or len(mentions) > ASSISTANT_MENTIONS_MAX or \
                any(not isinstance(item, str) or not 1 <= len(item) <= SUBJECT_ID_LIMIT
                    for item in mentions):
            raise McpError(400, "invalid-request", "@ 好友列表不正确")
        if decision["mode"] == "per_contact" and mentions:
            # 每位 @ 好友参与者逐个核验：任一未授权即整体拒绝，并指明是哪个主体。
            fingerprint = self.fingerprint()
            unauthorized = [mention for mention in dict.fromkeys(mentions)
                            if not self.store.has_grant(principal["clientId"], fingerprint,
                                                        account_id, "wechat_contact", mention)]
            if unauthorized:
                raise McpError(403, "participant-not-authorized",
                               "以下 @ 好友未获授权：" + "、".join(unauthorized))
        provider = self._assistant_provider()
        account = provider.resolve_account(account_id)
        result = provider.start(account, subject_id, params["agentId"], params["threadId"],
                                message, thinking=thinking, mentions=mentions)
        run = result.get("run") if isinstance(result, dict) else None
        if isinstance(run, dict) and run.get("id"):
            self._register_run(principal, run["id"], account, subject_id)
        bounded = self._bounded({"run": run or {}, "threadId": result.get("threadId"),
                                 "participants": result.get("participants")})
        return {**bounded, "requestScope": decision["scope"]}

    def _get_assistant_run(self, principal, body, decision):
        account_id, subject_id, params = self._assistant_scope(
            principal, "zhiji_get_assistant_run", body, {"runId", "after"}, ("runId",))
        run_id = self._assistant_id(params, "runId", "运行标识")
        after = params.get("after", 0)
        if type(after) is not int or after < 0:
            raise McpError(400, "invalid-request", "事件位置不正确")
        provider = self._assistant_provider()
        account = provider.resolve_account(account_id)
        result = provider.events(account, subject_id, run_id, after)
        return {**self._bounded(result), "requestScope": decision["scope"]}

    def _stop_assistant_run(self, principal, body, decision):
        account_id, subject_id, params = self._assistant_scope(
            principal, "zhiji_stop_assistant_run", body, {"runId"}, ("runId",))
        run_id = self._assistant_id(params, "runId", "运行标识")
        provider = self._assistant_provider()
        account = provider.resolve_account(account_id)
        result = provider.stop(account, subject_id, run_id)
        return {**self._bounded(result), "requestScope": decision["scope"]}

    # -- phase 3b third batch: external chat import (§5) ------------------------
    # 导入素材库是 Core 自有资料：代理直接走 GUI 同款的受控服务方法，转录插件只
    # 在 Core 的后台任务里被调用。外部联系人按 external_contact 主体独立授权；本
    # 机文件输入没有 MCP 通路（不接受任何路径参数），只管理已有素材。

    def _import_scope(self, principal, action, body, allowed, required=()):
        """One authorized external contact + strict params + a fresh authorization check."""
        subject_id = _external_subject(body)
        params = self._write_params(body.get("params", {}), allowed, required)
        self.authorize(principal, action, account=EXTERNAL_ACCOUNT,
                       subjects=[(EXTERNAL_ACCOUNT, "external_contact", subject_id)])
        return subject_id, params

    @staticmethod
    def _import_id(params, key, label):
        value = params.get(key)
        if not isinstance(value, str) or not 1 <= len(value) <= IMPORT_BATCH_ID_MAX:
            raise McpError(400, "invalid-request", label + "不正确")
        return value

    def _list_import_contacts(self, principal, body, decision):
        self._no_extra(body)
        contacts = self._import_provider().friends()
        if decision["mode"] == "per_contact":
            # 未授权的外部联系人不出现；授权键是稳定好友 ID，与显示名无关。
            fingerprint = self.fingerprint()
            contacts = [item for item in contacts
                        if isinstance(item, dict) and
                        self.store.has_grant(principal["clientId"], fingerprint,
                                             EXTERNAL_ACCOUNT, "external_contact", item.get("id"))]
        return {"contacts": contacts, "requestScope": decision["scope"]}

    def _list_import_batches(self, principal, body, decision):
        self._no_extra(body)
        subject_id = _external_subject(body)
        batches = self._import_provider().batches(subject_id)
        return {"batches": self._bounded(batches), "requestScope": decision["scope"]}

    def _get_import_batch(self, principal, body, decision):
        subject_id, params = self._import_scope(
            principal, "zhiji_get_import_batch", body, {"batchId"}, ("batchId",))
        result = self._import_provider().batch(self._import_id(params, "batchId", "导入批次标识"),
                                               subject_id)
        return {**self._bounded(result), "requestScope": decision["scope"]}

    def _import_page(self, principal, subject_id, params):
        """Shared limit/cursor handling of the paged import reads."""
        limit = params.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= IMPORT_CANDIDATE_PAGE_MAX:
            raise McpError(400, "invalid-request", "分页参数不正确")
        offset = 0
        if params.get("cursor") is not None:
            offset = self._cursor_decode(params["cursor"], principal=principal,
                                         account_id=EXTERNAL_ACCOUNT, subject_id=subject_id)
        return limit, offset

    def _import_next_cursor(self, principal, subject_id, offset):
        return self._cursor_encode(self._cursor_payload(principal, EXTERNAL_ACCOUNT,
                                                        subject_id, offset))

    def _list_import_candidates(self, principal, body, decision):
        subject_id, params = self._import_scope(
            principal, "zhiji_list_import_candidates", body,
            {"batchId", "limit", "cursor"}, ("batchId",))
        batch_id = self._import_id(params, "batchId", "导入批次标识")
        limit, offset = self._import_page(principal, subject_id, params)
        result = self._import_provider().candidates(batch_id, subject_id, limit, offset)
        next_cursor = self._import_next_cursor(principal, subject_id, offset + limit) \
            if len(result.get("items", [])) == limit else None
        return {**self._bounded(result), "requestScope": decision["scope"], "cursor": next_cursor}

    def _get_import_messages(self, principal, body, decision):
        subject_id, params = self._import_scope(
            principal, "zhiji_get_import_messages", body,
            {"platform", "batchId", "query", "limit", "cursor"})
        platform = params.get("platform")
        if platform is not None and (not isinstance(platform, str) or not platform or
                                     len(platform) > 80):
            raise McpError(400, "invalid-request", "来源平台不正确")
        batch_id = params.get("batchId")
        if batch_id is not None:
            batch_id = self._import_id(params, "batchId", "导入批次标识")
        query = params.get("query", "")
        if not isinstance(query, str) or len(query) > IMPORT_QUERY_MAX:
            raise McpError(400, "invalid-request", "查询条件不正确")
        limit, offset = self._import_page(principal, subject_id, params)
        result = self._import_provider().messages(subject_id, platform=platform, batch_id=batch_id,
                                                  query=query, limit=limit, offset=offset)
        next_cursor = self._import_next_cursor(principal, subject_id, offset + limit) \
            if len(result.get("items", [])) == limit else None
        return {**self._bounded(result), "requestScope": decision["scope"], "cursor": next_cursor}

    def _start_import_recognition(self, principal, body, decision):
        subject_id, params = self._import_scope(
            principal, "zhiji_start_import_recognition", body,
            {"batchId", "reprocess"}, ("batchId",))
        reprocess = params.get("reprocess", False)
        if not isinstance(reprocess, bool):
            raise McpError(400, "invalid-request", "重识别参数不正确")
        result = self._import_provider().start(self._import_id(params, "batchId", "导入批次标识"),
                                               subject_id, reprocess)
        self._register_batch(principal, result["id"])
        return {**result, "requestScope": decision["scope"]}

    def _stop_import_recognition(self, principal, body, decision):
        subject_id, params = self._import_scope(
            principal, "zhiji_stop_import_recognition", body, {"batchId"}, ("batchId",))
        result = self._import_provider().stop(self._import_id(params, "batchId", "导入批次标识"),
                                              subject_id)
        return {**result, "requestScope": decision["scope"]}

    def _save_import_batch(self, principal, body, decision):
        subject_id, params = self._import_scope(
            principal, "zhiji_save_import_batch", body,
            {"batchId", "candidateIds"}, ("batchId",))
        candidate_ids = params.get("candidateIds")
        if candidate_ids is not None:
            if not isinstance(candidate_ids, list) or not candidate_ids or \
                    len(candidate_ids) > 2000 or \
                    any(not isinstance(item, str) or not 1 <= len(item) <= IMPORT_BATCH_ID_MAX
                        for item in candidate_ids):
                raise McpError(400, "invalid-request", "候选记录选择不正确")
        result = self._import_provider().save(self._import_id(params, "batchId", "导入批次标识"),
                                              subject_id, candidate_ids)
        return {**result, "requestScope": decision["scope"]}

    def _remove_import_material(self, principal, body, decision):
        subject_id, params = self._import_scope(
            principal, "zhiji_remove_import_material", body,
            {"batchId", "materialId"}, ("batchId", "materialId"))
        result = self._import_provider().remove_material(
            self._import_id(params, "batchId", "导入批次标识"), subject_id,
            self._import_id(params, "materialId", "素材标识"))
        return {**result, "requestScope": decision["scope"]}

    # -- phase 5: application management (§5 应用管理行) -------------------------
    # 管理动作只经唯一的应用管理桥；模型密钥只写不读（查询一律返回 hasKey 布尔
    # 值），MCP 自身授权是固定排除项。删除整个账号/清空本机数据是两段式：第一
    # 次调用（无 confirmationId）登记待确认意图并交给本机确认界面；第二次调用携
    # 带 confirmationId，经一次性、短时、绑定参数哈希/客户端/授权版本/数据根/目
    # 标账号的校验后按既有清理链执行。MCP 工具永远不能自行确认。

    def _manage_provider(self):
        provider = self.manage
        if provider is None:
            raise McpError(501, "capability-not-open", "应用管理服务尚未接入")
        return provider

    def _get_model_settings(self, principal, body, decision):
        self._no_extra(body)
        return {**self._manage_provider().model_settings(), "requestScope": decision["scope"]}

    def _save_model_settings(self, principal, body, decision):
        params = self._write_params(body.get("params", {}), {"scheduling", "connection"}, ())
        if "scheduling" not in params and "connection" not in params:
            raise McpError(400, "invalid-request", "模型设置参数不正确")
        result = self._manage_provider().save_model_settings(params)
        return {**result, "requestScope": decision["scope"]}

    def _get_plugins(self, principal, body, decision):
        self._no_extra(body)
        return {**self._manage_provider().plugins(), "requestScope": decision["scope"]}

    def _set_plugin_enabled(self, principal, body, decision):
        params = self._write_params(body.get("params", {}), {"pluginId", "enabled"},
                                    ("pluginId", "enabled"))
        if not isinstance(params["pluginId"], str) or not params["pluginId"]:
            raise McpError(400, "invalid-request", "插件标识不正确")
        if not isinstance(params["enabled"], bool):
            raise McpError(400, "invalid-request", "启停状态不正确")
        return {**self._manage_provider().set_plugin_enabled(params["pluginId"], params["enabled"]),
                "requestScope": decision["scope"]}

    def _get_usage(self, principal, body, decision):
        account_id = _account(body.get("account"))
        if account_id is None:
            raise McpError(400, "invalid-request", "缺少账号标识")
        params = self._write_params(body.get("params", {}),
                                    {"since", "until", "plugin", "model", "task", "includeContacts"}, ())
        authorized = None
        if decision["mode"] == "per_contact":
            # 逐联系人模式只返回能从已授权联系人重算的子集（§4.2），由管理桥标 scope。
            authorized = {grant["subjectId"] for grant in
                          self.store.grants(principal["clientId"], self.fingerprint())
                          if grant["accountId"] == account_id and grant["subjectType"] == "wechat_contact"}
        return {**self._manage_provider().usage(account_id, params, authorized=authorized),
                "requestScope": decision["scope"]}

    DELETION_KINDS = {"zhiji_delete_account": "delete_account",
                      "zhiji_clear_local_data": "clear_local_data"}

    def _deletion_params(self, body):
        """``(kind, account_id, confirmation_id)`` of one high-impact deletion call."""
        kind = self.DELETION_KINDS.get(body.get("action"))
        params = self._write_params(body.get("params", {}), {"confirmationId"}, ())
        confirmation_id = params.get("confirmationId")
        if confirmation_id is not None and (not isinstance(confirmation_id, str) or
                                            not 1 <= len(confirmation_id) <= 128):
            raise McpError(400, "invalid-request", "确认标识不正确")
        account_id = _account(body.get("account"))
        if kind == "delete_account" and account_id is None:
            raise McpError(400, "invalid-request", "缺少账号标识")
        return kind, account_id, confirmation_id

    @staticmethod
    def _deletion_hash(action, account_id):
        """The confirm binding covers the deletion parameters only — never the confirmation id."""
        return _params_hash(action, account_id, [], {})

    def _delete_account(self, principal, body, decision):
        return self._high_impact_deletion(principal, body, decision)

    def _clear_local_data(self, principal, body, decision):
        return self._high_impact_deletion(principal, body, decision)

    def _high_impact_deletion(self, principal, body, decision):
        action = body["action"]
        kind, account_id, confirmation_id = self._deletion_params(body)
        manage = self._manage_provider()
        deletion_hash = self._deletion_hash(action, account_id)
        if confirmation_id is None:
            profile = self.store.client(principal["clientId"])
            intent = manage.begin_delete(
                action=action, kind=kind, client_id=principal["clientId"],
                client_name=profile["name"] if profile else principal["clientId"],
                auth_version=principal["authVersion"], fingerprint=self.fingerprint(),
                account_id=account_id, params_hash=deletion_hash)
            return {"confirmationId": intent["confirmId"], "state": "pending_confirmation",
                    "kind": kind, "account": account_id,
                    "expiresAtMs": intent["expiresAtMs"], "requestScope": decision["scope"],
                    "message": "请在本机确认界面确认后再携带 confirmationId 重新调用；"
                               "确认凭证一次性且短时有效"}
        manage.use_confirmation(confirmation_id, action=action, kind=kind,
                                client_id=principal["clientId"],
                                auth_version=principal["authVersion"],
                                fingerprint=self.fingerprint(), account_id=account_id,
                                params_hash=deletion_hash)
        result = manage.execute_delete_account(account_id) if kind == "delete_account" \
            else manage.execute_clear_local_data()
        return {**result, "requestScope": decision["scope"]}

    # -- loopback HTTP -------------------------------------------------------
    def http_handle(self, method, path, headers, body):
        """The dedicated MCP auth routes; nothing here touches other HTTP surfaces."""
        if method != "POST":
            raise McpError(405, "method-not-allowed", "MCP 认证路由只接受 POST")
        route = path[len(HTTP_PREFIX):]
        if route not in ENDPOINTS or "/" in route:
            raise McpError(404, "not-found", "接口不存在")
        if not isinstance(body, dict):
            raise McpError(400, "invalid-request", "请求格式不正确")
        if route == "handshake":
            return self.handshake(body)
        principal = self.authenticate(headers)
        return {"ok": True, "result": self.call(principal, body)}
