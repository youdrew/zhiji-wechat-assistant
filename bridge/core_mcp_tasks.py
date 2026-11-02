"""Core business methods behind the MCP analysis-task tools (阶段 3b 第二批).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The capability proxy reaches analysis capabilities only through this bridge:
scheme tasks go through the shared TaskManager (租约、代次与迟到结果规则与 GUI
任务完全相同), and the analysis plugins answer only their explicitly named
status/page RPC methods. There is no arbitrary plugin RPC, no plugin token and
no model key here -- model calls stay inside the plugins behind the Core gateway.

``accountId`` always resolves through the explicit saved-account registry, so a
mistyped or foreign account id never falls back to another account's data.
"""
from __future__ import annotations

from pathlib import Path

from core_mcp_offline import registry_records
from core_mcp_proxy import McpError
from core_plugin_process import PluginError, PluginUnavailable
from core_tasks import TaskError

FRIEND_ANALYSIS_PLUGIN = "kyf.friend-analysis"
INSIGHTS_PLUGIN = "kyf.message-insights"
SELF_ANALYSIS_PLUGIN = "kyf.self-analysis"
FRIEND_ANALYSIS_SCHEME = "default"


def _from_plugin_error(exc):
    """Plugin business errors keep their stable code; only the status is mapped."""
    status = 404 if exc.code == "not-found" else 409
    return McpError(status, exc.code, exc.message)


class McpAnalysisBridge:
    """The one analysis-task surface the capability proxy may call."""

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
    def _rpc(self, plugin, method, params, *, account=None, user=None):
        try:
            self.services.require_plugin(plugin, account)
        except PluginUnavailable as exc:
            raise McpError(503, exc.code, exc.message) from None
        context = {key: value for key, value in (("account", account), ("user", user)) if value}
        try:
            return self.services.supervisor.rpc(plugin, method, params, context)
        except PluginUnavailable as exc:
            raise McpError(503, exc.code, exc.message) from None
        except PluginError as exc:
            raise _from_plugin_error(exc) from None

    # -- friend analysis (scheme tasks) ----------------------------------------
    def start_friend_analysis(self, account, user, scheme=None, conflict=None):
        """A Core scheme task for one friend; queuing/cancellation follow the shared rules."""
        try:
            self.services.require_plugin(FRIEND_ANALYSIS_PLUGIN, account)
            return self.services.tasks.create(
                FRIEND_ANALYSIS_PLUGIN, kind="scheme", account=account, user=user,
                scheme=scheme or FRIEND_ANALYSIS_SCHEME, title="分析", conflict=conflict)
        except PluginUnavailable as exc:
            raise McpError(503, exc.code, exc.message) from None
        except TaskError as exc:
            raise McpError(exc.status, exc.code, exc.message) from None

    def task(self, task_id):
        return self.services.tasks.task(task_id)

    def stop_task(self, task_id):
        try:
            return self.services.tasks.stop(task_id)
        except TaskError as exc:
            raise McpError(exc.status, exc.code, exc.message) from None

    def friend_analysis_page(self, account, user, method, params=None):
        """One registered saved-result read of the friend-analysis plugin."""
        return self._rpc(FRIEND_ANALYSIS_PLUGIN, method, params or {}, account=account, user=user)

    # -- per-message insights (逐句分析与回复建议) -------------------------------
    def start_message_insights(self, account, user, message_id):
        return self._rpc(INSIGHTS_PLUGIN, "annotations.generate", {"messageId": message_id},
                         account=account, user=user)

    def message_insights(self, account, user, message_ids):
        return self._rpc(INSIGHTS_PLUGIN, "annotations.status", {"messageIds": message_ids},
                         account=account, user=user)

    # -- self analysis (自我剖析) -----------------------------------------------
    def start_self_analysis(self, account, params):
        return self._rpc(SELF_ANALYSIS_PLUGIN, "self.analyze", params, account=account)

    def self_analysis_status(self, account):
        return self._rpc(SELF_ANALYSIS_PLUGIN, "self.analysisStatus", {}, account=account)

    def stop_self_analysis(self, account):
        return self._rpc(SELF_ANALYSIS_PLUGIN, "self.analysisPause", {}, account=account)

    def self_analysis_page(self, account, method, params=None):
        """One registered saved-result read (persona, values, tags, periods, timeline) of the plugin."""
        return self._rpc(SELF_ANALYSIS_PLUGIN, method, params or {}, account=account)

    def _self_timeline_write(self, method, account, params):
        try:
            return self._rpc(SELF_ANALYSIS_PLUGIN, method, params, account=account)
        except McpError as exc:
            if exc.code == "conflict":  # MCP 写入的稳定冲突码，与其他带版本的写入一致
                raise McpError(409, "version-conflict", exc.message) from None
            raise

    def edit_self_timeline(self, account, params):
        return self._self_timeline_write("self.timelineEdit", account, params)

    def undo_self_timeline(self, account, params):
        return self._self_timeline_write("self.timelineUndo", account, params)
