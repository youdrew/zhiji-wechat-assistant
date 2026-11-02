"""Local confirmation of high-impact deletions requested through MCP (阶段 5, §7).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

删除整个账号、清空本机数据等全量清除先在能力代理处登记一条待确认意图，本服务把
确认payload交给注入的确认界面（按需后台组件，随操作持有自己的租约，不要求知己
主窗口打开）。确认凭证一次性、短时，并绑定调用参数哈希、客户端、授权版本、安装
/数据根与目标账号；确认与执行是两条独立链路：

- 本机确认界面（Electron 确认窗）经 MCP 设置通道（core_mcp_admin 的
  ``confirmations`` 路由，永远不在 MCP 认证路由上）凭一次性令牌回传决定；
- MCP 客户端随后携带 ``confirmationId`` 重新调用删除工具，由
  :meth:`McpDeletionConfirmService.check_use` 校验绑定并按一次性消费。

无可交互桌面（presenter 未接入或抛出 :class:`ConfirmUnavailable`）、超时、拒绝、
参数变化、授权版本变化或数据根变化都不执行。意图保存在内存中：Core 重启即失效，
这是失败关闭的（宁可不执行，也不能凭残留状态删除数据）。
"""
from __future__ import annotations

import secrets
import threading
import time

from core_mcp_proxy import McpError

# 待确认意图的有效期：确认窗必须在此之内得到用户决定。
CONFIRM_TTL_SECONDS = 600.0
# 用户确认后凭证的使用窗口：之后的执行调用视为确认过期。
CONFIRM_USE_TTL_SECONDS = 300.0
MAX_INTENTS = 16

KIND_LABELS = {
    "delete_account": "删除整个账号",
    "clear_local_data": "清空本机数据",
}
IMPACT_TEXT = {
    "delete_account": "将删除该账号的全部本机资料：聊天投影、关系记忆、人工资料、分析结果、"
                      "插件账号数据与用量记录，不可恢复。",
    "clear_local_data": "将清空本机所有已保存账号的资料、分析结果、插件账号数据与用量记录，不可恢复。",
}
CONFIRM_ACTIONS = frozenset(("zhiji_delete_account", "zhiji_clear_local_data"))


class ConfirmUnavailable(RuntimeError):
    """The desktop cannot show an interactive confirmation right now."""


class McpDeletionConfirmService:
    """In-memory pending confirmations; only the local window can confirm one."""

    def __init__(self, presenter=None, *, monotonic=time.monotonic, wall=time.time):
        self.presenter = presenter
        self.monotonic = monotonic
        # 到期时间对外一律以墙钟毫秒表示（MCP 客户端与本机确认窗都按墙钟倒计时）；
        # 内部比较仍只在单调钟上进行，凭证语义不变。
        self.wall = wall
        self.lock = threading.Lock()
        self._intents = {}  # confirm_id -> record

    # -- creation (from the capability proxy) ---------------------------------
    def begin(self, *, action, kind, client_id, client_name, auth_version, fingerprint,
              account_id, params_hash, account_label):
        """Register one pending intent and hand its payload to the confirm window."""
        if action not in CONFIRM_ACTIONS or kind not in KIND_LABELS:
            raise McpError(400, "invalid-request", "确认操作不正确")
        if self.presenter is None:
            # 没有确认界面就永远不能确认：直接拒绝发起，而不是留下一个永远无法
            # 满足的待确认意图。
            raise McpError(503, "confirmation-unavailable", "本机确认界面未接入，不能发起高影响删除")
        now = self.monotonic()
        confirm_id = "zcf_" + secrets.token_hex(16)
        with self.lock:
            self._prune(now)
            while len(self._intents) >= MAX_INTENTS:
                oldest = min(self._intents, key=lambda key: self._intents[key]["created"])
                self._intents.pop(oldest)
            self._intents[confirm_id] = {
                "confirmId": confirm_id, "action": action, "kind": kind,
                "clientId": client_id, "clientName": client_name, "authVersion": auth_version,
                "fingerprint": fingerprint, "accountId": account_id, "paramsHash": params_hash,
                "token": secrets.token_hex(32), "state": "pending", "accountLabel": account_label,
                "created": now, "expires": now + CONFIRM_TTL_SECONDS, "useDeadline": None,
            }
        threading.Thread(target=self._present, args=(confirm_id,),
                         name="zhiji-mcp-confirm", daemon=True).start()
        return self.view(confirm_id)

    def _present(self, confirm_id):
        record = self._snapshot(confirm_id)
        if record is None:
            return
        payload = {"confirmId": confirm_id, "token": record["token"],
                   "action": record["action"], "kindLabel": KIND_LABELS[record["kind"]],
                   "clientName": record["clientName"],
                   "accountLabel": record["accountLabel"],
                   "impact": IMPACT_TEXT[record["kind"]], "expiresAtMs": self._deadline_ms(record)}
        try:
            granted = self.presenter(payload)
            if granted not in (True, False):
                raise ConfirmUnavailable("确认界面未返回明确决定")
        except ConfirmUnavailable:
            self._mark(confirm_id, "unavailable")
            return
        except Exception:
            self._mark(confirm_id, "unavailable")
            return
        try:
            self._apply(confirm_id, record["token"], granted)
        except McpError:
            pass  # 窗口迟到的决定不改变既有结论（已解决或已过期）。

    # -- local window resolution (via the MCP settings channel, never MCP tools)
    def resolve(self, confirm_id, token, granted):
        """The confirm window's one-time decision; ``token`` comes from its payload."""
        if not isinstance(token, str) or not token:
            raise McpError(403, "confirmation-token-invalid", "确认令牌无效")
        self._apply(confirm_id, token, bool(granted))
        return self.view(confirm_id)

    def _apply(self, confirm_id, token, granted):
        with self.lock:
            record = self._intents.get(confirm_id)
            now = self.monotonic()
            if record is None:
                raise McpError(404, "confirmation-unknown", "没有这个确认请求")
            if record["state"] != "pending":
                raise McpError(409, "confirmation-already-resolved", "该确认请求已有结论")
            if not secrets.compare_digest(record["token"], token):
                raise McpError(403, "confirmation-token-invalid", "确认令牌无效")
            if now >= record["expires"]:
                record["state"] = "expired"
                raise McpError(410, "confirmation-expired", "确认已过期，请重新发起删除请求")
            record["state"] = "confirmed" if granted else "denied"
            record["useDeadline"] = now + CONFIRM_USE_TTL_SECONDS

    def _mark(self, confirm_id, state):
        with self.lock:
            record = self._intents.get(confirm_id)
            if record is not None and record["state"] == "pending":
                record["state"] = state

    # -- consumption (from the executing MCP call) -----------------------------
    def check_use(self, confirm_id, *, action, kind, client_id, auth_version, fingerprint,
                  account_id, params_hash):
        """Validate one execution attempt and consume the confirmation (one-time).

        任何绑定字段变化——参数哈希、目标账号、客户端、授权版本、安装/数据根——
        以及未确认、被拒绝、已使用或过期的凭证都拒绝执行；等待中过期的意图按超
        时报告，而不是“尚未确认”。
        """
        with self.lock:
            record = self._intents.get(confirm_id)
            now = self.monotonic()
            if record is None:
                raise McpError(404, "confirmation-unknown", "确认请求不存在或已失效")
            if record["action"] != action or record["kind"] != kind or \
                    record["clientId"] != client_id or record["authVersion"] != auth_version or \
                    record["fingerprint"] != fingerprint or record["accountId"] != account_id or \
                    record["paramsHash"] != params_hash:
                raise McpError(403, "confirmation-mismatch", "确认与本次调用不一致，请重新发起删除请求")
            if record["state"] == "pending":
                if now >= record["expires"]:
                    record["state"] = "expired"
                    raise McpError(410, "confirmation-expired", "确认已超时，请重新发起删除请求")
                raise McpError(409, "confirmation-pending", "确认尚未完成，请先在本机确认界面确认")
            if record["state"] == "denied":
                raise McpError(403, "confirmation-denied", "该删除请求已被拒绝")
            if record["state"] == "unavailable":
                raise McpError(503, "confirmation-unavailable", "本机确认界面不可用，不能执行该删除")
            if record["state"] == "consumed":
                # 确认凭证一次性，第二次使用一律拒绝。
                raise McpError(403, "confirmation-consumed", "该确认已使用过，请重新发起删除请求")
            # state == confirmed
            if now >= record["expires"] or now >= record["useDeadline"]:
                record["state"] = "expired"
                raise McpError(410, "confirmation-expired", "确认已过期，请重新发起删除请求")
            record["state"] = "consumed"

    # -- views -----------------------------------------------------------------
    def view(self, confirm_id):
        record = self._snapshot(confirm_id)
        if record is None:
            raise McpError(404, "confirmation-unknown", "确认请求不存在或已失效")
        return {"confirmId": record["confirmId"], "action": record["action"], "kind": record["kind"],
                "state": record["state"], "accountId": record["accountId"],
                "expiresAtMs": self._deadline_ms(record)}

    def _deadline_ms(self, record):
        """Monotonic deadline as wall-clock epoch milliseconds for presenters and clients."""
        return int((self.wall() + max(0.0, record["expires"] - self.monotonic())) * 1000)

    def _snapshot(self, confirm_id):
        with self.lock:
            self._prune(self.monotonic())
            record = self._intents.get(confirm_id)
            return dict(record) if record else None

    def _prune(self, now):
        for key in list(self._intents):
            record = self._intents.get(key)
            if record is None:
                continue
            if record["state"] == "pending":
                if now >= record["expires"]:
                    record["state"] = "expired"
                    self._intents.pop(key, None)
                continue
            # 已确认的凭证在使用窗口内必须仍可校验；其余终态到过期即清理。
            deadline = record["useDeadline"] if record["state"] == "confirmed" else record["expires"]
            if now >= deadline:
                self._intents.pop(key, None)
