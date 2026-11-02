"""Core business methods behind the MCP chat-assistant tools (阶段 3b 第三批).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The capability proxy reaches the chat assistant only through this bridge: the
advisor plugin answers only its explicitly named RPC methods (模板与技能、讨论、
草稿与排队消息、提问、停止与结果), with the account and friend supplied by the
host context, never by tool parameters. There is no arbitrary plugin RPC, no
plugin token and no model key here -- the advisor's model calls stay behind the
Core gateway. MCP 提交提问与读取结果，但从不代写用户的草稿或排队消息（那是
主动助手对话里用户正在输入的内容），也不把助手讨论当成微信事实。

``accountId`` resolves through the explicit saved-account registry, so a
mistyped or foreign account id never falls back to another account's data.
"""
from __future__ import annotations

from pathlib import Path

from core_mcp_offline import registry_records
from core_mcp_proxy import McpError
from core_plugin_process import PluginError, PluginUnavailable

ADVISOR_PLUGIN = "kyf.advisor"


def _from_plugin_error(exc):
    """Plugin business errors keep their stable code; only the status is mapped."""
    status = 404 if exc.code == "not-found" else 409
    return McpError(status, exc.code, exc.message)


class McpAssistantBridge:
    """The one chat-assistant surface the capability proxy may call."""

    def __init__(self, services, registry_path):
        self.services = services
        self.registry_path = Path(registry_path)

    # -- account --------------------------------------------------------------
    def resolve_account(self, account_id):
        """The saved-account record of one explicit accountId, as its Core account name."""
        record = registry_records(self.registry_path).get(account_id)
        if record is None:
            raise McpError(404, "account-unknown", "该账号不是本机已保存账号")
        return record["account"]

    # -- plugin rpc -----------------------------------------------------------
    def _rpc(self, method, params, *, account=None, user=None):
        try:
            self.services.require_plugin(ADVISOR_PLUGIN, account)
        except PluginUnavailable as exc:
            raise McpError(503, exc.code, exc.message) from None
        context = {key: value for key, value in (("account", account), ("user", user)) if value}
        try:
            return self.services.supervisor.rpc(ADVISOR_PLUGIN, method, params, context)
        except PluginUnavailable as exc:
            raise McpError(503, exc.code, exc.message) from None
        except PluginError as exc:
            raise _from_plugin_error(exc) from None

    # -- templates and skills -------------------------------------------------
    # 模板与技能是用户自己的助手配置，不含任何联系人资料；逐联系人模式同样可用。
    def catalog(self):
        return self._rpc("catalog.get", {})

    def save_template(self, agent):
        return self._rpc("catalog.saveAgent", {"agent": agent})

    def enable_template(self, agent_id, enabled):
        return self._rpc("catalog.enableAgent", {"id": agent_id, "enabled": enabled})

    def delete_template(self, agent_id):
        return self._rpc("catalog.deleteAgent", {"id": agent_id})

    def import_skill(self, name, description, content):
        return self._rpc("catalog.importSkill", {"name": name, "description": description,
                                                 "content": content})

    def delete_skill(self, skill_id):
        return self._rpc("catalog.deleteSkill", {"id": skill_id})

    # -- discussions, drafts and queued messages (read-only) --------------------
    def thread(self, account, user, agent_id, thread_id, before_seq=None):
        """One discussion with its draft and queued messages; an explicit threadId only."""
        params = {"agentId": agent_id, "threadId": thread_id}
        if before_seq is not None:
            params["beforeSeq"] = before_seq
        return self._rpc("thread.get", params, account=account, user=user)

    def threads(self, account, user, agent_id=None):
        params = {"agentId": agent_id} if agent_id else {}
        return self._rpc("threads.list", params, account=account, user=user)

    def friends(self, account, user):
        """Friends that may be @-mentioned; the proxy filters it to authorized contacts."""
        return self._rpc("friends.list", {}, account=account, user=user)

    def new_thread(self, account, user, agent_id):
        return self._rpc("thread.new", {"agentId": agent_id}, account=account, user=user)

    # -- questions, stops and results ------------------------------------------
    def start(self, account, user, agent_id, thread_id, message, *, thinking=True, mentions=None):
        params = {"agentId": agent_id, "threadId": thread_id, "message": message}
        if thinking is not None:
            params["thinking"] = thinking
        if mentions:
            params["mentions"] = mentions
        return self._rpc("run.start", params, account=account, user=user)

    def stop(self, account, user, run_id):
        return self._rpc("run.stop", {"runId": run_id}, account=account, user=user)

    def events(self, account, user, run_id, after=0):
        return self._rpc("run.events", {"runId": run_id, "after": after},
                         account=account, user=user)
