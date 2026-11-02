"""Wiring of Core's plugin host: registry, supervisor, tasks, events and the plugin interface.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

from core_events import EventService
from core_gateway import GatewayUnavailable, ModelGatewayHost
from core_history import HistoryService
from core_mcp_proxy import McpServices, install_source_fingerprint
from core_mcp_assistant import McpAssistantBridge
from core_mcp_import import McpImportBridge
from core_mcp_tasks import McpAnalysisBridge
from core_plugins import PluginRegistry
from core_model_settings import ModelSettingsStore
from core_plugin_settings import SettingsError
from core_plugin_process import PluginUnavailable
from core_store import CoreStore
from core_supervisor import PluginSupervisor
from core_tasks import TaskManager
from plugin_api import ApiError, PluginAPI
from model_capabilities import ModelCapabilitiesUnavailable, UnifiedModelCapabilities
from usage_ledger import UsageLedger

GRANT_TTL = 15 * 60
MAX_GRANTS = 16


class PluginFiles:
    """Files or folders the user picked in a host dialog, handed to one plugin through a one-time token."""

    def __init__(self, registry, clock=time.monotonic):
        self.registry = registry
        self.clock = clock
        self.lock = threading.Lock()
        self.grants = {}  # token -> {plugin, path, label, kind, expires}

    def _expire_locked(self):
        now = self.clock()
        for token in [token for token, grant in self.grants.items() if grant["expires"] <= now]:
            self.grants.pop(token, None)

    def grant(self, plugin, path):
        """``{token, label}`` for a picked path; the path itself never reaches the renderer."""
        if not isinstance(plugin, str) or self.registry.get(plugin) is None:
            raise KeyError(plugin)
        if not isinstance(path, str) or not path or len(path) > 4096 or "\x00" in path or not os.path.isabs(path):
            raise ValueError("invalid path")
        path = os.path.abspath(path)
        kind = "directory" if os.path.isdir(path) else "file" if os.path.isfile(path) else None
        if kind is None:
            raise ValueError("path not found")
        token = "fg_" + secrets.token_hex(16)
        label = os.path.basename(path.rstrip("\\/")) or path
        with self.lock:
            self._expire_locked()
            mine = sorted((grant["expires"], key) for key, grant in self.grants.items() if grant["plugin"] == plugin)
            for _expires, key in mine[:max(0, len(mine) - MAX_GRANTS + 1)]:
                self.grants.pop(key, None)
            self.grants[token] = {"plugin": plugin, "path": path, "label": label, "kind": kind,
                                  "expires": self.clock() + GRANT_TTL}
        return {"token": token, "label": label}

    def claim(self, plugin, token):
        """One-time: ``{path, label, kind}`` for the plugin the grant was made for; LookupError otherwise."""
        with self.lock:
            self._expire_locked()
            grant = self.grants.get(token) if isinstance(token, str) else None
            if grant is None or grant["plugin"] != plugin:
                raise LookupError(token)
            self.grants.pop(token, None)
        return {"path": grant["path"], "label": grant["label"], "kind": grant["kind"]}


def gateway_base(protocol, base_url):
    """For Ollama the gateway's service root is the host root (clients add ``/api`` or ``/v1``)."""
    if protocol != "ollama" or not isinstance(base_url, str):
        return base_url
    return re.sub(r"/+(?:v1|api)$", "", base_url.rstrip("/"))


class CoreServices:
    # The contact matrix stays a bounded response; more contacts arrive by searching in the page.
    DIRECTORY_CONTACTS = 500

    def __init__(self, backend, root, core_url, *, local=None, bundled_dir=None, local_dir=None):
        self.backend = backend
        self.root = Path(root)
        self.source = backend.source
        local = Path(local) if local is not None else self.root / ".local"
        self.local = local
        self.model_settings_store = ModelSettingsStore(local)
        try:
            # Import retired plugin fields before a business-settings save can
            # discard them. A damaged settings document must not stop the host.
            self.model_settings_store.read()
        except SettingsError:
            pass
        self.registry = PluginRegistry(self.root, bundled_dir=bundled_dir, local_dir=local_dir,
                                       state_path=local / "core" / "plugins.json", plugin_data_dir=local / "plugin-data")
        self.store = CoreStore(local / "core" / "core.sqlite3")
        self.supervisor = PluginSupervisor(self.registry, root=self.root, data_dir=local / "plugin-data",
                                           core_url=core_url)
        self.tasks = TaskManager(self.store, self.supervisor, self.registry)
        self.events = EventService(self)
        self.api = PluginAPI(self)
        self.ledger = UsageLedger(local / "core" / "usage.sqlite3")
        self._model_capabilities = UnifiedModelCapabilities()
        traces = getattr(backend, "model_traces", None)
        self.gateway = ModelGatewayHost(self.root, services=self._gateway_services, ledger=self.ledger, traces=traces)
        self.tasks.on_token(self.gateway.token)
        listen = getattr(backend, "add_model_listener", None)
        if callable(listen):
            listen(self.gateway.reconfigure)
        self._history = None
        self._human = None
        self._migrations = None
        self._external_import = None
        self._embeddings = None
        self._local_judge = None
        self.files = PluginFiles(self.registry)
        # Modified 2026-10-27: MCP clients reach Core through one capability proxy;
        # contact grants bind to the install + current data root, never to a name.
        self.mcp = McpServices(local, source_fingerprint=self._mcp_source_fingerprint)
        self.mcp.directory = self._mcp_directory
        self.mcp.offline = self._mcp_offline
        # Modified by Know Your Friends, 2026-10-27 (phase 6): disabling or uninstalling the
        # kyf.mcp plugin removes every callable MCP entry — the registry is the one source
        # of truth, so the proxy asks it per handshake and per authenticated call.
        self.mcp.plugin_gate = self._mcp_plugin_open
        # Modified 2026-10-27: MCP analysis-task tools reach the analysis plugins and the
        # shared TaskManager through one narrow business bridge, never arbitrary plugin RPC.
        self.mcp.analysis = McpAnalysisBridge(self, self.root / ".local" / "real-client-data"
                                              / "accounts.json")
        # Modified 2026-10-27: MCP assistant and import tools reach the advisor plugin's
        # named RPCs and Core's own import service through one narrow bridge each.
        self.mcp.assistant = McpAssistantBridge(self, self.root / ".local" / "real-client-data"
                                                / "accounts.json")
        self.mcp.imports = McpImportBridge(self)
        # Modified 2026-10-27: one shared Core is kept alive by the GUI, MCP connections
        # and running tasks together; the last lease ending starts the idle countdown.
        # Resolved before the on-demand confirm window, which holds its own lease while running.
        leases = getattr(backend, "leases", None)
        if leases is None:
            from core_leases import LeaseService
            leases = LeaseService()
        self.leases = leases
        self.mcp.leases = leases
        self.tasks.leases = leases
        # Modified 2026-10-27: 阶段 5 应用管理桥——模型设置/插件状态/用量与高影响
        # 删除；确认窗是按需后台组件（注入 presenter），删除走既有账号清理链。
        from core_mcp_manage import MCP_PLUGIN, McpManagementBridge
        from core_mcp_confirm_window import ElectronConfirmPresenter
        presenter = ElectronConfirmPresenter(
            self.root, core_url,
            admin_token=lambda: self.supervisor.token_for_plugin(MCP_PLUGIN),
            leases=leases)
        self.mcp.manage = McpManagementBridge(
            self, self.root / ".local" / "real-client-data" / "accounts.json", presenter=presenter)
        self.mcp.confirm = self.mcp.manage.confirm
        self.lock = threading.Lock()
        try:
            from core_migration import migrate_global
            self._global_migration_failures = migrate_global(local)
        except Exception:
            self._global_migration_failures = set()

    # Data views used by the plugin interface ----------------------------------------------------------------------------
    @property
    def embeddings(self):
        with self.lock:
            if self._embeddings is None:
                from core_embeddings import EmbeddingService
                self._embeddings = EmbeddingService(self.root,
                    self.local / "core" / "embeddings" / "bge-small-zh-v1.5", self.gateway)
            return self._embeddings

    @property
    def local_judge(self):
        with self.lock:
            if self._local_judge is None:
                from core_judge import JudgeService
                self._local_judge = JudgeService(self.root, self.local / "core" / "judge" / "julia-1",
                    self.gateway, self.backend.model_source_store, self.backend._model_changed)
            return self._local_judge

    @property
    def external_import(self):
        """Core-owned local import data is available even without a WeChat account."""
        with self.lock:
            if self._external_import is None:
                from core_external_import import ExternalImportService
                self._external_import = ExternalImportService(self, self.local / "core" / "external-chat")
            return self._external_import

    def scope(self):
        """The current account. Plugins ask on every call (draft saves while typing), so like the in-process
        assistant this reuses a verification younger than ~2 s; a replaced or released reader is never reused."""
        try:
            leased = getattr(self.source, "advisor_identity", None)
            if getattr(self.backend, "closing", False) or not callable(leased):
                account = self.backend._scoped_identity()[0]
            else:
                account = str(leased(messages=True)[0])
        except Exception:
            return {"account": None, "ready": False}
        return {"account": account, "ready": True}

    def friends(self, account):
        """Conversations the user added to this software, with names from the current session list."""
        selected = set(self.backend.selection_store.get(account).get("selectedSessions", []))
        cache = getattr(self.backend, "usage_names", None)
        ticket = cache.ticket() if cache is not None else None
        try:
            metadata = self.source.sessions()
            if cache is not None:
                cache.observe(metadata, ticket)
            sessions = metadata.get("sessions", [])
        except Exception:
            sessions = []
        names = {item.get("username"): item for item in sessions if isinstance(item, dict)}
        result = []
        for user in sorted(selected):
            item = names.get(user, {})
            result.append({"user": user, "name": item.get("name") or user,
                           "isGroup": bool(item.get("isGroup", user.endswith("@chatroom")))})
        return result

    def usage_friend_names(self, account):
        """Optional labels already observed for this account; never touches the chat source."""
        cache = getattr(self.backend, "usage_names", None)
        return cache.get(account) if cache is not None else {}

    @property
    def history(self):
        with self.lock:
            if self._history is None:
                accounts = self.backend.account_data()
                self._history = HistoryService(accounts.stores, accounts.context)
            return self._history

    @property
    def human(self):
        with self.lock:
            if self._human is None:
                from core_human import HumanService
                accounts = self.backend.account_data()
                self._human = HumanService(accounts.stores, on_change=self.events.human_changed, source=self.source)
            return self._human

    def _mcp_source_fingerprint(self):
        """Install + current data-root identity for MCP contact grants.

        Reads the configured path without directory discovery; a stub backend or a
        broken configuration degrades to the install-only fingerprint.
        """
        store = getattr(self.backend, "data_root_store", None)
        path = store.stored_path() if hasattr(store, "stored_path") else None
        return install_source_fingerprint(self.root, path)

    def _mcp_plugin_open(self):
        """Whether the MCP plugin itself is installed and enabled (阶段 6 禁用/卸载门)."""
        return self.registry.enabled("kyf.mcp")

    def _mcp_offline(self):
        """The explicit saved-account scope behind the MCP offline read tools.

        Built on demand from the account data service; a closing bridge surfaces
        as an unavailable provider instead of constructing a fresh one.
        """
        if self.backend.closing:
            return None
        from core_mcp_offline import SavedAccountScope
        return SavedAccountScope(self.backend.account_data().friends,
                                 self.root / ".local" / "real-client-data" / "accounts.json")

    def _mcp_directory(self):
        """Saved accounts and their added conversations, for the MCP settings page's contact matrix.

        Reads the local account registry and each account's selection; a WeChat
        source that is down only loses display names, never invents subject ids.
        No message content is read here.
        """
        accounts = []
        try:
            registry = json.loads((self.root / ".local" / "real-client-data" / "accounts.json")
                                  .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            registry = {}
        records = registry.get("accounts") if isinstance(registry, dict) else None
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            account, identifier = record.get("account"), record.get("accountId")
            if not isinstance(account, str) or not account or not isinstance(identifier, str) or not identifier:
                continue
            try:
                conversations = self.friends(account)
                labels = self.usage_friend_names(account)
            except Exception:
                conversations, labels = [], {}
            contacts = [{"subjectId": item["user"], "name": labels.get(item["user"]) or item["name"],
                         "isGroup": bool(item.get("isGroup"))} for item in conversations]
            contacts.sort(key=lambda item: (item["name"], item["subjectId"]))
            nickname = record.get("nickname") if isinstance(record.get("nickname"), str) else ""
            wechat_id = record.get("wechatId") if isinstance(record.get("wechatId"), str) else ""
            accounts.append({"accountId": identifier,
                             "label": nickname or wechat_id or identifier[:12],
                             "contacts": contacts[:self.DIRECTORY_CONTACTS],
                             "truncated": len(contacts) > self.DIRECTORY_CONTACTS})
        return {"accounts": accounts}

    def _gateway_services(self):
        """The configured model services with their credentials, for the gateway's private pipe only.

        The main model is always first; judge and image models follow when
        assigned to their own model. A failing judge or image model is silently
        omitted — its purpose falls back to the main model, and the delivered
        ``roles`` reflect the effective assignment, never the configured one.
        """
        store = getattr(self.backend, "model_source_store", None)
        try:
            selected = store.saved_selection() if store is not None else None
        except Exception:
            return []
        api = selected.get("api") if isinstance(selected, dict) else None
        if not isinstance(api, dict) or not selected.get("sourceId"):
            return []
        try:
            # Surfaced so a damaged settings document fails loudly, as before.
            self.model_settings()
        except SettingsError as exc:
            raise GatewayUnavailable(exc.code, exc.message) from None
        try:
            listing = store.models()
        except Exception:
            listing = {"models": [], "roles": {"judge": None, "image": None}}
        main_id = selected["sourceId"]
        main_roles = ["main"]
        extra = {}
        for role in ("judge", "image"):
            target = listing["roles"].get(role)
            if target is None:
                continue
            if target == main_id:
                # Same model under two purposes: one service, roles written in full.
                main_roles.append(role)
            else:
                extra.setdefault(target, []).append(role)
        try:
            key = store.resolve_key(api.get("protocol"), api.get("baseUrl"), None, main_id)
        except Exception:
            key = None
        if not key and api.get("protocol") != "ollama":
            return []
        main_entry = next((item for item in listing["models"] if item["id"] == main_id), None)
        main_service = self._configured_service(
            api, main_id, key, main_roles,
            main_entry["concurrency"] if main_entry else None, optional=False)
        services = [main_service]
        for target, roles in extra.items():
            entry = next((item for item in listing["models"] if item["id"] == target), None)
            if entry is None:
                continue
            try:
                if entry.get("protocol") == "systemone":
                    service = self.local_judge.service(entry, roles)
                    if service is not None:
                        services.append(service)
                    continue
                entry_key = store.resolve_key(entry["protocol"], entry["baseUrl"], None, target)
                if not entry_key and entry["protocol"] != "ollama":
                    continue  # 缺密钥：用途静默退回主模型
                entry_api = {"protocol": entry["protocol"], "baseUrl": entry["baseUrl"],
                             "model": entry["model"], "contextTokens": entry["contextTokens"],
                             "timeoutMs": entry["timeoutMs"]}
                private_profile = getattr(store, "api_profile", None)
                if callable(private_profile):
                    entry_api = private_profile(target) or entry_api
                service = self._configured_service(entry_api, target, entry_key, roles,
                                                   entry["concurrency"], optional=True)
            except Exception:
                continue  # 判断或图片模型出错不影响主模型
            if service is not None:
                services.append(service)
        return services

    def _configured_service(self, api, source_id, key, roles, concurrency, *, optional):
        """One service entry with its stamp and fingerprint; ``roles`` never enters either."""
        try:
            capabilities = self._model_capabilities.lookup(api, key)
        except ModelCapabilitiesUnavailable:
            if optional:
                return None
            raise GatewayUnavailable("upstream-unavailable", "统一模型网关暂时不可用或正在切换模型") from None
        # Only content-affecting settings belong in the stamp; a deadline edit must
        # not invalidate saved results or interrupt an already-running request.
        stamp = hashlib.sha256(json.dumps([source_id, api.get("protocol"), api.get("baseUrl"),
                                           api.get("model"), api.get("contextTokens"), api.get("modelMetadata"),
                                           api.get("encryptedKey")], ensure_ascii=False).encode("utf-8")).hexdigest()
        # Same identity as the pre-plugin model fingerprint, so incremental results stay usable.
        from core_contracts import effective_model_budget, model_fingerprint
        try:
            context_tokens = effective_model_budget(api.get("contextTokens"), api.get("modelMetadata"))["contextTokens"]
        except Exception:
            context_tokens = api.get("contextTokens")
        fingerprint = model_fingerprint(api["protocol"], api["baseUrl"], api["model"], context_tokens)
        cache_fingerprint = fingerprint
        metadata = {}
        if capabilities is not None:
            context_tokens = min(api.get("contextTokens") or 1000000, capabilities["context_length"], 1000000)
            # A routing generation fences live requests, but is not a model
            # content version. Keep the endpoint identity available to caches;
            # callers must also include actualModel/backendId in their keys.
            cache_fingerprint = model_fingerprint(api["protocol"], api["baseUrl"], api["model"], context_tokens)
            route = [capabilities["actual_model"], capabilities["backend_id"], capabilities["route_revision"]]
            fingerprint = hashlib.sha256(json.dumps(
                [cache_fingerprint, route],
                ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
            stamp = hashlib.sha256(json.dumps([stamp, capabilities], ensure_ascii=False,
                                               separators=(",", ":")).encode("utf-8")).hexdigest()
            metadata = {"actualModel": capabilities["actual_model"], "backendId": capabilities["backend_id"],
                        "routeRevision": capabilities["route_revision"],
                        "maxOutputTokens": capabilities["max_output_tokens"],
                        "supportsVision": capabilities["supports_vision"]}
        from model_source import DEFAULT_MODEL_TIMEOUT_MS, model_timeout_ms
        return {"id": source_id, "protocol": api["protocol"],
                "baseUrl": gateway_base(api["protocol"], api["baseUrl"]),
                "model": api["model"], "contextTokens": context_tokens, "stamp": stamp,
                "timeoutMs": model_timeout_ms(api.get("timeoutMs", DEFAULT_MODEL_TIMEOUT_MS)),
                "fingerprint": fingerprint, "cacheFingerprint": cache_fingerprint, "apiKey": key or "", **metadata,
                "concurrency": concurrency or 4, "roles": roles}

    def model_settings(self):
        return self.model_settings_store.read()

    def main_concurrency(self):
        """The main model's per-model concurrency, for local batch backpressure."""
        try:
            store = getattr(self.backend, "model_source_store", None)
            listing = store.models() if store is not None else None
            entry = next((item for item in (listing or {}).get("models", [])
                          if item["id"] == (listing or {}).get("roles", {}).get("main")), None)
            return entry["concurrency"] if entry else 4
        except Exception:
            return 4

    def save_model_settings(self, revision, values):
        saved = self.model_settings_store.save(revision, values)
        try:
            self.gateway.reconfigure()
        except GatewayUnavailable:
            # The durable save succeeded; an unavailable gateway reads it again
            # before accepting its next request.
            pass
        return saved

    def ensure_account(self, account):
        """Copy this account's legacy plugin tables before any plugin works with the account."""
        if not isinstance(account, str) or not account:
            return
        stores = self.history.stores
        with self.lock:
            if self._migrations is None:
                from core_migration import PluginMigrations
                self._migrations = PluginMigrations(stores, self.local / "plugin-data")
            migrations = self._migrations
        migrations.ensure(account)

    def model_services(self, plugin):
        try:
            return {"services": self.gateway.services()}
        except GatewayUnavailable as exc:
            raise ApiError(503, exc.code, exc.message) from None

    def require_plugin(self, plugin, account=None):
        """Do not let a plugin replace legacy data with defaults after an incomplete migration."""
        if account is not None:
            if self.tasks.account_blocked(account):
                raise PluginUnavailable("account-cleared", "账号资料正在清理或已清除，请重新打开应用")
            self.ensure_account(account)
        failed = self._retry_global_migration(plugin)
        if account is not None and self._migrations is not None:
            failed.update(self._migrations.failed_plugins(account))
        if plugin in failed:
            raise PluginUnavailable("migration-failed", "插件资料迁移未完成；原资料已保留，请修复后重试")

    def _retry_global_migration(self, plugin):
        """A failed copy of plugin-wide files (a few small files) is retried on use, not only at the next start."""
        from core_migration import LEGACY_GLOBAL, migrate_global
        with self.lock:
            if plugin in self._global_migration_failures and plugin in LEGACY_GLOBAL:
                if not migrate_global(self.local, legacy={plugin: LEGACY_GLOBAL[plugin]}):
                    self._global_migration_failures.discard(plugin)
            return set(self._global_migration_failures)

    def attempt_outcome(self, plugin, attempt_id, body):
        return self.gateway.attempt_outcome(plugin, attempt_id, body)

    # Analysis schemes ------------------------------------------------------------------------------------------------
    DEFAULT_SCHEME = ("kyf.friend-analysis", "default")

    def schemes(self, account, user):
        """Every available scheme, and the one this friend's analysis window shows."""
        from friend_store import FriendStore
        available = self.registry.schemes()
        keys = {(item["plugin"], item["id"]) for item in available}
        saved = FriendStore(self.history.stores.account(account)).scheme(user)
        selected = (saved["plugin"], saved["scheme"]) if saved else None
        if selected not in keys:
            selected = self.DEFAULT_SCHEME if self.DEFAULT_SCHEME in keys else (
                (available[0]["plugin"], available[0]["id"]) if available else None)
        return {"schemes": available, "selected": {"plugin": selected[0], "scheme": selected[1]} if selected else None}

    def friend_opened(self, account, user):
        """After an opened window refreshed the history: tell the friend's current scheme plugin."""
        selected = self.schemes(account, user)["selected"]
        if selected:
            self.events.friend_opened(selected["plugin"], account, user, selected["scheme"])

    def select_scheme(self, account, user, plugin, scheme):
        """Switching only changes what is shown; no task is started or stopped and no result is deleted."""
        from friend_store import FriendStore
        if not any(item["plugin"] == plugin and item["id"] == scheme for item in self.registry.schemes()):
            raise KeyError(scheme)
        FriendStore(self.history.stores.account(account)).set_scheme(user, plugin, scheme)
        return self.schemes(account, user)

    # Host actions ---------------------------------------------------------------------------------------------------
    def migration_failures(self):
        """Plugins whose old data could not be copied yet (retried on the next use)."""
        with self.lock:
            migrations = self._migrations
            failed = set(self._global_migration_failures)
        return failed | migrations.failed_plugins() if migrations is not None else failed

    def listing(self):
        listing = self.registry.listing()
        failed = self.migration_failures()
        for item in listing["plugins"]:
            item["process"] = self.supervisor.status(item["id"])
            if item["id"] in failed:
                item["migration"] = "failed"
        return listing

    def set_enabled(self, plugin_id, enabled):
        with self.events.lock:
            was_enabled = self.registry.enabled(plugin_id)
            result = self.registry.set_enabled(plugin_id, enabled)
            if enabled and not was_enabled:
                self.events.reenabled(plugin_id)
        if enabled:
            self.supervisor.reset(plugin_id)
        else:
            for task in self.store.tasks(plugin=plugin_id, states=("queued", "running"), limit=500):
                self.tasks.stop(task["id"])
            self.supervisor.stop(plugin_id, "插件已停用")
            self.supervisor.reconcile_dependencies()
        return result

    def refresh_plugins(self):
        self.registry.refresh()
        self.supervisor.reconcile_dependencies()
        return self.listing()

    def restart(self, plugin_id):
        self.supervisor.stop(plugin_id, "正在重新启动")
        self.supervisor.reset(plugin_id)
        self.supervisor.ensure(plugin_id)
        return self.supervisor.status(plugin_id)

    @staticmethod
    def account_key(account):
        """Folder name of an account inside each plugin's data directory."""
        return hashlib.sha256(account.encode("utf-8")).hexdigest()

    def clear_account(self, account):
        """Stop plugins and delete their tasks, subscriptions and data for one account."""
        cache = getattr(getattr(self, "backend", None), "usage_names", None)
        if cache is not None:
            cache.clear(account)
        self.tasks.quiesce_account(account)
        for plugin_id in list(self.registry.plugins):
            self.supervisor.stop(plugin_id, "正在清理账号数据")
        self.store.delete_account(account)
        self.ledger.clear(account)
        key = self.account_key(account)
        base = self.local / "plugin-data"
        from core_account_db import _long_path
        for directory in base.glob(f"*/accounts/{key}"):
            shutil.rmtree(_long_path(directory), ignore_errors=False)  # Engine caches nest past MAX_PATH.

    def close(self):
        if getattr(self, "mcp", None) is not None:
            self.mcp.close()
        if self._external_import is not None:
            self._external_import.close()
        cache = getattr(getattr(self, "backend", None), "usage_names", None)
        if cache is not None:
            cache.close()
        self.events.close()
        self.supervisor.close()
        if self._embeddings is not None:
            self._embeddings.close()
        if getattr(self, "_local_judge", None) is not None:
            self._local_judge.close()
        self.gateway.close()
        self.ledger.close()
        self.store.close()
