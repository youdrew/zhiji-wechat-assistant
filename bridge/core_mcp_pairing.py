"""User-confirmed one-command pairing for local MCP clients (G12, agent-integration §4).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The install CLI (``scripts/zhiji-mcp-cli.py connect``) asks Core for a pairing; Core
shows a local pairing window where the user picks the accounts to share and the
operation capabilities (修改资料与发起分析 / 发送微信 / 全局管理, all off by default).
The CLI polls with the pairing id and a one-time poll secret; only after the user's
approval does Core create (or repair) the profile and return its credential, once,
to that poll secret.

Trust boundaries:

- The loopback routes below are reachable by any local process, so none of them can
  approve anything. Approval comes only from the Core-spawned pairing window's
  stdout (read by :class:`core_mcp_pairing_window.ElectronPairingPresenter`) or from
  the kyf.mcp settings channel (``core_mcp_admin`` ``pairing/decide``), which MCP
  tools never reach. An existing MCP credential or token grants nothing here: the
  CLI cannot self-issue credentials.
- The poll secret is kept only as a SHA-256 hash; the credential is generated at the
  moment of delivery and never stored (the store keeps its verification hash).
- Without an interactive desktop the window reports unavailable; the request stays
  pending so the user can still approve it in 设置 → 插件 → MCP.
- Everything is in memory: a Core restart drops every pending pairing (fail closed).

HTTP entry for the integrator (route ``POST /api/mcp/pairing/<route>`` here, before
``McpServices.http_handle``, with the same JSON body parsing and McpError mapping as
``/api/mcp/*``)::

    handle_pairing(mcp, method, parts, body) -> dict      # raises McpError

``mcp`` is Core's ``McpServices`` with ``mcp.pairing = McpPairingService(...)``;
``parts`` is the path below ``PAIRING_PREFIX`` split on ``/``. Routes:

- ``request`` ``{name, client, replacesClientId?}`` →
  ``{pairingId, pollSecret, state, ui, expiresAtMs, replaces}``
- ``poll`` ``{pairingId, pollSecret}`` → ``{state, ui, expiresAtMs}``; once approved
  ``{state: "approved", clientId, credential, name, mode, capabilities, accounts,
  models}`` exactly once, then ``{state: "delivered"}``
- ``cancel`` ``{pairingId, pollSecret}`` → ``{state}``
- ``revoke`` ``{clientId, credential}`` → ``{revoked: true}`` (a client removing its
  own profile; it can only reduce access)
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time

from core_mcp_proxy import EXTERNAL_ACCOUNT, McpError
from core_mcp_store import NAME_LIMIT, McpStoreError, verify_credential

PAIRING_PREFIX = "/api/mcp/pairing/"
PAIRING_ROUTES = ("request", "poll", "cancel", "revoke")
# The user must decide within this window; the CLI waits at most this long.
PAIRING_TTL_SECONDS = 600.0
# After approval the CLI must collect the credential within this window.
DELIVERY_TTL_SECONDS = 300.0
# Final states stay visible to a late poll for a while, then the record is dropped.
RESULT_TTL_SECONDS = 120.0
MAX_PAIRINGS = 8
CLIENT_KINDS = ("claude-code", "codex", "cursor", "deepseek-harness", "generic")
CLIENT_LABELS = {"claude-code": "Claude Code", "codex": "Codex", "cursor": "Cursor",
                 "deepseek-harness": "DeepSeek Harness", "generic": "其他 MCP 客户端"}
CAPABILITIES = ("write", "send", "manage")
EXTERNAL_LABEL = "外部聊天导入"
PAIRING_MODE = "accounts"

UI_OPENING, UI_SHOWN, UI_UNAVAILABLE = "opening", "shown", "unavailable"


class PairingUnavailable(RuntimeError):
    """The desktop cannot show an interactive pairing window right now."""


def _hash(secret):
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _string(body, key, limit):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise McpError(400, "invalid-request", "配对请求参数不正确")
    return value.strip()


class McpPairingService:
    """In-memory pairing requests; only the local window or settings page can approve one."""

    def __init__(self, mcp, presenter=None, *, monotonic=time.monotonic, wall=time.time, spawn=None):
        self.mcp = mcp
        self.presenter = presenter
        self.monotonic = monotonic
        self.wall = wall
        # Test seam: run the presenter synchronously; production uses a daemon thread.
        self.spawn = spawn or (lambda target: threading.Thread(
            target=target, name="zhiji-mcp-pairing", daemon=True).start())
        self.lock = threading.RLock()
        self._records = {}

    # -- gates -------------------------------------------------------------------
    def _gate(self):
        store = self.mcp.store
        if not store.master_enabled():
            raise McpError(403, "mcp-disabled", "MCP 总开关已关闭，请先在 设置 → 插件 → MCP 打开")
        plugin_open = getattr(self.mcp, "_plugin_open", None)
        if callable(plugin_open) and not plugin_open():
            raise McpError(403, "mcp-plugin-disabled", "MCP 插件已停用或已卸载")

    # -- directory ---------------------------------------------------------------
    def accounts(self):
        """Saved accounts the user may share: ``[{accountId, label}]`` (no contacts, no text)."""
        provider = getattr(self.mcp, "directory", None)
        result = []
        if callable(provider):
            try:
                directory = provider()
            except Exception:
                directory = None
            for item in (directory or {}).get("accounts") or []:
                if isinstance(item, dict) and isinstance(item.get("accountId"), str) and item["accountId"]:
                    label = item.get("label") if isinstance(item.get("label"), str) else ""
                    result.append({"accountId": item["accountId"], "label": label or item["accountId"][:12]})
        result.append({"accountId": EXTERNAL_ACCOUNT, "label": EXTERNAL_LABEL})
        return result

    # -- request (loopback, unauthenticated) -------------------------------------
    def request(self, body):
        if not isinstance(body, dict) or not {"name", "client"} <= set(body) or \
                set(body) - {"name", "client", "replacesClientId"}:
            raise McpError(400, "invalid-request", "配对请求参数不正确")
        name = _string(body, "name", NAME_LIMIT)
        client = body.get("client")
        if client not in CLIENT_KINDS:
            raise McpError(400, "invalid-request", "客户端类型不正确")
        replaces_id = body.get("replacesClientId")
        if replaces_id is not None and (not isinstance(replaces_id, str) or not 1 <= len(replaces_id) <= 128):
            raise McpError(400, "invalid-request", "配对请求参数不正确")
        self._gate()
        target = self._replacement(name, replaces_id)
        now = self.monotonic()
        pairing_id = "zpr_" + secrets.token_hex(16)
        poll_secret = "zps_" + secrets.token_hex(32)
        with self.lock:
            self._prune(now)
            # A newer request for the same client name supersedes an older pending one.
            for record in self._records.values():
                if record["name"] == name and record["state"] == "pending":
                    record["state"] = "cancelled"
                    record["final"] = now
            while len([key for key, item in self._records.items() if item["state"] in ("pending", "approved")]) \
                    >= MAX_PAIRINGS:
                oldest = min((key for key, item in self._records.items()
                              if item["state"] in ("pending", "approved")),
                             key=lambda key: self._records[key]["created"])
                self._records.pop(oldest)
            self._records[pairing_id] = {
                "pairingId": pairing_id, "pollHash": _hash(poll_secret), "name": name, "client": client,
                "replaces": target, "state": "pending",
                "ui": UI_OPENING if self.presenter is not None else UI_UNAVAILABLE,
                "created": now, "createdAtMs": int(self.wall() * 1000),
                "expires": now + PAIRING_TTL_SECONDS, "selection": None, "deliverBy": None, "final": None,
            }
        if self.presenter is not None:
            self.spawn(lambda: self._present(pairing_id))
        view = self._client_view(pairing_id)
        return {**view, "pollSecret": poll_secret, "replaces": target is not None}

    def _replacement(self, name, replaces_id):
        """The existing profile a re-pair repairs instead of duplicating, if any."""
        store = self.mcp.store
        if replaces_id is not None:
            row = store.client(replaces_id)
            if row is not None:
                return {"clientId": row["client_id"], "name": row["name"]}
        same = [item for item in store.list_profiles() if item["name"] == name]
        if not same:
            return None
        latest = max(same, key=lambda item: item["createdAt"])
        return {"clientId": latest["clientId"], "name": latest["name"]}

    # -- presentation ------------------------------------------------------------
    def _present(self, pairing_id):
        with self.lock:
            record = self._records.get(pairing_id)
            if record is None or record["state"] != "pending":
                return
            payload = {"pairingId": pairing_id, "clientName": record["name"], "clientKind": record["client"],
                       "clientLabel": CLIENT_LABELS[record["client"]],
                       "replaces": record["replaces"]["name"] if record["replaces"] else None,
                       "accounts": self.accounts(), "expiresAtMs": self._deadline_ms(record)}

        def shown():
            self._set_ui(pairing_id, UI_SHOWN)

        try:
            decision = self.presenter(payload, shown)
        except Exception:
            # PairingUnavailable or a broken presenter: the settings page can still approve.
            self._set_ui(pairing_id, UI_UNAVAILABLE)
            return
        try:
            if isinstance(decision, dict) and decision.get("approved") is True:
                self.decide(pairing_id, True, decision.get("accounts"), decision.get("capabilities"))
            else:
                # Closed, timed out or refused: the window gave no approval.
                self.decide(pairing_id, False)
        except McpError:
            pass  # already decided elsewhere, expired, or an invalid selection (stays pending).

    def _set_ui(self, pairing_id, ui):
        with self.lock:
            record = self._records.get(pairing_id)
            if record is not None and record["state"] == "pending":
                record["ui"] = ui

    # -- decision (window stdout or settings channel only) -----------------------
    def decide(self, pairing_id, approve, accounts=None, capabilities=None):
        if not isinstance(approve, bool):
            raise McpError(400, "invalid-request", "配对决定不正确")
        selection = None
        if approve:
            selection = self._selection(accounts, capabilities)
        with self.lock:
            now = self.monotonic()
            record = self._records.get(pairing_id) if isinstance(pairing_id, str) else None
            if record is None:
                raise McpError(404, "pairing-unknown", "没有这个配对请求")
            self._expire(record, now)
            if record["state"] == "expired":
                raise McpError(410, "pairing-expired", "配对请求已过期")
            if record["state"] != "pending":
                raise McpError(409, "pairing-already-resolved", "该配对请求已有结论")
            if approve:
                self._gate()
                record.update(state="approved", selection=selection, deliverBy=now + DELIVERY_TTL_SECONDS)
            else:
                record.update(state="denied", final=now)
            return self._settings_view(record)

    def _selection(self, accounts, capabilities):
        accounts = [] if accounts is None else accounts
        if not isinstance(accounts, list) or any(not isinstance(item, str) for item in accounts):
            raise McpError(400, "invalid-request", "所选账号不正确")
        known = {item["accountId"]: item["label"] for item in self.accounts()}
        if any(item not in known for item in accounts) or len(set(accounts)) != len(accounts):
            raise McpError(400, "invalid-request", "所选账号不存在")
        capabilities = {} if capabilities is None else capabilities
        if not isinstance(capabilities, dict) or set(capabilities) - set(CAPABILITIES) or \
                any(not isinstance(value, bool) for value in capabilities.values()):
            raise McpError(400, "invalid-request", "操作能力开关不正确")
        return {"accounts": [{"accountId": item, "label": known[item]} for item in accounts],
                "capabilities": {name: bool(capabilities.get(name, False)) for name in CAPABILITIES}}

    # -- poll / cancel (holder of the poll secret) --------------------------------
    def _owned(self, body):
        if not isinstance(body, dict) or set(body) != {"pairingId", "pollSecret"} or \
                not isinstance(body["pairingId"], str) or not isinstance(body["pollSecret"], str):
            raise McpError(400, "invalid-request", "配对查询参数不正确")
        record = self._records.get(body["pairingId"])
        if record is None:
            raise McpError(404, "pairing-unknown", "配对请求不存在或已失效")
        if not secrets.compare_digest(record["pollHash"], _hash(body["pollSecret"])):
            raise McpError(403, "pairing-poll-invalid", "配对查询凭证无效")
        return record

    def poll(self, body):
        with self.lock:
            now = self.monotonic()
            self._prune(now)
            record = self._owned(body)
            self._expire(record, now)
            if record["state"] in ("pending", "approved"):
                self._gate()
            if record["state"] != "approved":
                return self._client_view(record["pairingId"])
            # Delivery is atomic under the lock: a second poll can never see a credential.
            selection = record["selection"]
            record.update(state="delivering", selection=None)
            try:
                delivered = self._deliver(record, selection)
            except (McpStoreError, McpError) as exc:
                record.update(state="failed", final=now)
                raise McpError(getattr(exc, "status", 500), "pairing-failed",
                               f"创建客户端档案失败：{exc.message}") from None
            record.update(state="delivered", final=now, clientId=delivered["clientId"])
            return {"pairingId": record["pairingId"], "state": "approved", **delivered,
                    "models": self._model_readiness()}

    def cancel(self, body):
        with self.lock:
            record = self._owned(body)
            if record["state"] in ("pending", "approved"):
                record.update(state="cancelled", selection=None, final=self.monotonic())
            return {"pairingId": record["pairingId"], "state": record["state"]}

    def _deliver(self, record, selection):
        """Create, or repair in place, the one profile for this client; returns the credential once."""
        store = self.mcp.store
        fingerprint = self.mcp.fingerprint()
        if selection["accounts"] and not fingerprint:
            raise McpError(503, "pairing-failed", "安装指纹暂不可用，不能授权账号")
        caps = selection["capabilities"]
        target = record["replaces"]
        row = store.client(target["clientId"]) if target else None
        if row is None:
            created = store.create_profile(record["name"], PAIRING_MODE, caps["manage"],
                                           allow_write=caps["write"], allow_send=caps["send"])
            client_id, credential = created["clientId"], created["credential"]
            try:
                for item in selection["accounts"]:
                    store.grant_account(client_id, fingerprint, item["accountId"])
            except BaseException:
                store.revoke_profile(client_id)  # fail closed: no half-granted profile survives.
                raise
        else:
            client_id = row["client_id"]
            # The new selection replaces the old permissions entirely; nothing is inherited.
            regenerated = store.regenerate_credential(client_id)
            credential = regenerated["credential"]
            if row["mode"] != PAIRING_MODE:
                store.set_mode(client_id, PAIRING_MODE)
            store.set_capabilities(client_id, write=caps["write"], send=caps["send"], manage=caps["manage"])
            if not row["enabled"]:
                store.set_enabled(client_id, True)
            wanted = {item["accountId"] for item in selection["accounts"]}
            for grant in store.account_grants(client_id, fingerprint) if fingerprint else []:
                if grant["accountId"] not in wanted:
                    store.revoke_account(client_id, fingerprint, grant["accountId"])
            for account_id in sorted(wanted):
                store.grant_account(client_id, fingerprint, account_id)
        return {"clientId": client_id, "credential": credential, "name": record["name"],
                "mode": PAIRING_MODE, "capabilities": dict(caps), "accounts": selection["accounts"],
                "repaired": row is not None}

    def _model_readiness(self):
        """Whether the special-analysis model is configured; ``None`` when Core cannot tell."""
        manage = getattr(self.mcp, "manage", None)
        reader = getattr(manage, "model_settings", None)
        if not callable(reader):
            return {"ready": None}
        try:
            connection = (reader() or {}).get("connection")
        except Exception:
            return {"ready": None}
        if not isinstance(connection, dict):
            return {"ready": None}
        api = connection.get("api") if isinstance(connection.get("api"), dict) else None
        return {"ready": bool(connection.get("mode") == "api" and api and api.get("hasKey"))}

    # -- self revoke (holder of a valid credential) ---------------------------------
    def revoke(self, body):
        if not isinstance(body, dict) or set(body) != {"clientId", "credential"}:
            raise McpError(400, "invalid-request", "撤销请求参数不正确")
        store = self.mcp.store
        row = store.client(body["clientId"]) if isinstance(body["clientId"], str) else None
        if row is None or not verify_credential(row["credential_salt"], row["credential_hash"], body["credential"]):
            raise McpError(401, "mcp-unauthorized", "客户端凭据无效")
        store.revoke_profile(row["client_id"])
        return {"revoked": True}

    # -- views ---------------------------------------------------------------------
    def pending(self):
        """Pending requests for the settings page (no secrets)."""
        with self.lock:
            now = self.monotonic()
            self._prune(now)
            return [self._settings_view(record) for record in
                    sorted(self._records.values(), key=lambda item: item["created"])
                    if record["state"] == "pending"]

    def _settings_view(self, record):
        return {"pairingId": record["pairingId"], "clientName": record["name"], "clientKind": record["client"],
                "clientLabel": CLIENT_LABELS[record["client"]],
                "replaces": record["replaces"]["name"] if record["replaces"] else None,
                "state": record["state"], "ui": record["ui"], "createdAtMs": record["createdAtMs"],
                "expiresAtMs": self._deadline_ms(record)}

    def _client_view(self, pairing_id):
        record = self._records[pairing_id]
        state = record["state"]
        if state in ("delivering",):
            state = "approved"
        return {"pairingId": pairing_id, "state": state, "ui": record["ui"],
                "expiresAtMs": self._deadline_ms(record)}

    def _deadline_ms(self, record):
        deadline = record["deliverBy"] if record["state"] == "approved" else record["expires"]
        return int((self.wall() + max(0.0, deadline - self.monotonic())) * 1000)

    def _expire(self, record, now):
        if record["state"] == "pending" and now >= record["expires"]:
            record.update(state="expired", final=now)
        elif record["state"] == "approved" and now >= record["deliverBy"]:
            # Approved but never collected: nothing was created, nothing to undo.
            record.update(state="expired", selection=None, final=now)

    def _prune(self, now):
        for key in list(self._records):
            record = self._records[key]
            self._expire(record, now)
            if record["final"] is not None and now - record["final"] >= RESULT_TTL_SECONDS:
                self._records.pop(key, None)


def handle_pairing(mcp, method, parts, body):
    """Loopback ``POST /api/mcp/pairing/<route>``; nothing here can approve a pairing."""
    if method != "POST":
        raise McpError(405, "method-not-allowed", "配对路由只接受 POST")
    if not isinstance(parts, list) or len(parts) != 1 or parts[0] not in PAIRING_ROUTES:
        raise McpError(404, "not-found", "接口不存在")
    service = getattr(mcp, "pairing", None)
    if service is None:
        raise McpError(503, "pairing-unavailable", "配对服务未就绪")
    route = parts[0]
    if route == "request":
        return service.request(body)
    if route == "poll":
        return service.poll(body)
    if route == "cancel":
        return service.cancel(body)
    return service.revoke(body)


def handle_pairing_path(mcp, method, path, body):
    """Same as :func:`handle_pairing` for a full request path under ``PAIRING_PREFIX``."""
    if not isinstance(path, str) or not path.startswith(PAIRING_PREFIX):
        raise McpError(404, "not-found", "接口不存在")
    return handle_pairing(mcp, method, path[len(PAIRING_PREFIX):].split("/"), body)
