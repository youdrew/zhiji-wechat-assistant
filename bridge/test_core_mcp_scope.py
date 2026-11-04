"""G9/G10 on synthetic data: data scope (full / accounts / per-contact) is separate from
operation capabilities (read / write / send / manage), and the tool catalog lists only
what the calling profile can actually call with the plugins that are enabled."""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_mcp_store
from core_mcp_admin import McpAdminError, handle_admin
from core_mcp_offline import SavedAccountScope
from core_mcp_proxy import (CAPABILITIES, EXTERNAL_ACCOUNT, REGISTRY, McpError, McpServices,
                            Scope)
from core_mcp_store import MODES, McpStoreError
from test_core_mcp import (ACCOUNT_LEVEL_TOOLS, ALL_OPEN_TOOLS, MANAGE_TOOLS, SEND_TOOLS, FINGERPRINT,
                           headers, make_services)
from test_core_mcp_assistant import FakeAssistant, FakeImports

SUBJECT_A = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友1"}]
SUBJECT_B = [{"accountId": "id-B", "subjectType": "wechat_contact", "subjectId": "好友1"}]
KNOWN_PLUGINS = {"kyf.friend-analysis", "kyf.message-insights", "kyf.self-analysis",
                 "kyf.advisor", "kyf.external-chat-import"}
WRITE_TOOLS = {name for name, entry in REGISTRY.items() if entry.get("capability") == "write"}


class FakeOffline:
    def __init__(self):
        self.listed = None

    def list_accounts(self, allowed_subjects=None, *, accounts=None):
        self.listed = (allowed_subjects, accounts)
        names = sorted(accounts) if accounts is not None else ["id-A", "id-B"]
        scope = "installation" if accounts is None else "authorized_accounts"
        return {"accounts": [{"accountId": name, "scope": scope} for name in names], "scope": scope}

    def search_messages(self, account_id, subject_id, *, query, limit, offset):
        return {"account": account_id, "user": subject_id, "messages": []}


class Harness(unittest.TestCase):
    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.store = self.mcp.store
        self.offline = FakeOffline()
        self.mcp.offline = lambda: self.offline

    def pair(self, name="合成客户端", mode="accounts", **capabilities):
        created = self.store.create_profile(name, mode, **capabilities)
        return self.connect(created), created

    def connect(self, created):
        token = self.mcp.handshake({"clientId": created["clientId"], "credential": created["credential"]})
        return self.mcp.authenticate(headers(token["token"]))

    def denied(self, principal, action, **kwargs):
        with self.assertRaises(McpError) as caught:
            self.mcp.authorize(principal, action, **kwargs)
        return caught.exception.code

    def call_denied(self, principal, body):
        with self.assertRaises(McpError) as caught:
            self.mcp.call(principal, body)
        return caught.exception.code


class RegistryCapabilityTests(unittest.TestCase):
    def test_every_entry_declares_a_capability_consistent_with_write_and_kind(self):
        for name, entry in REGISTRY.items():
            capability = entry.get("capability")
            self.assertIn(capability, CAPABILITIES, name)
            if entry["kind"] == "app_management":
                expected = "manage"
            elif entry.get("write"):
                expected = "write"
            else:
                expected = "read"
            self.assertEqual(capability, expected, name)
            if "plugin" in entry:
                self.assertIn(entry["plugin"], KNOWN_PLUGINS, name)
        self.assertTrue(SEND_TOOLS.isdisjoint(REGISTRY))

    def test_bundled_plugin_ids_exist(self):
        root = Path(__file__).resolve().parent.parent / "plugins"
        for plugin_id in {entry["plugin"] for entry in REGISTRY.values() if "plugin" in entry}:
            manifest = json.loads((root / plugin_id / "plugin.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["id"], plugin_id)


class DefaultsTests(Harness):
    def test_new_profile_defaults_to_accounts_mode_and_read_only(self):
        self.assertEqual(MODES, ("full", "accounts", "per_contact"))
        created = self.store.create_profile("默认客户端")
        profile = self.store.client(created["clientId"])
        self.assertEqual(profile["mode"], "accounts")
        self.assertEqual(self.store.capabilities(profile), {"write": False, "send": False, "manage": False})
        result = handle_admin(self.mcp, "POST", ["profiles"], {}, {"name": "设置页客户端"})
        self.assertEqual(result["profile"]["mode"], "accounts")
        self.assertEqual(result["profile"]["capabilities"], {"write": False, "send": False, "manage": False})
        self.assertEqual(result["profile"]["accountGrants"], [])


class AccountScopeTests(Harness):
    def test_granted_account_cannot_read_another_account(self):
        principal, created = self.pair()
        self.store.grant_account(created["clientId"], FINGERPRINT, "id-A")
        # Any contact of the granted account, including ones added later, is readable.
        decision = self.mcp.authorize(principal, "zhiji_search_messages",
                                      subjects=[("id-A", "wechat_contact", "后来添加的好友")])
        self.assertEqual((decision["mode"], decision["scope"]), ("accounts", "authorized_accounts"))
        # Subject call in account B.
        self.assertEqual(self.denied(principal, "zhiji_search_messages",
                                     subjects=[("id-B", "wechat_contact", "好友1")]), "account-not-authorized")
        self.assertEqual(self.call_denied(principal, {"action": "zhiji_search_messages",
                                                      "params": {"query": "合成"}, "subjects": SUBJECT_B}),
                         "account-not-authorized")
        # Account-only call naming account B, and a subject tool with neither subject nor account.
        self.assertEqual(self.denied(principal, "zhiji_search_messages", account="id-B"),
                         "account-not-authorized")
        self.assertEqual(self.denied(principal, "zhiji_list_memories"), "account-not-authorized")
        manager, manager_created = self.pair("管理客户端", global_management=True)
        self.store.grant_account(manager_created["clientId"], FINGERPRINT, "id-A")
        self.assertEqual(self.denied(manager, "zhiji_get_usage", account="id-B"), "account-not-authorized")
        self.assertEqual(self.mcp.authorize(manager, "zhiji_get_usage", account="id-A")["scope"],
                         "authorized_accounts")
        # A mixed request is refused as a whole.
        self.assertEqual(self.denied(principal, "zhiji_search_messages",
                                     subjects=[("id-A", "wechat_contact", "好友1"),
                                               ("id-B", "wechat_contact", "好友1")]), "account-not-authorized")

    def test_new_accounts_and_other_roots_and_clients_are_not_granted(self):
        principal, created = self.pair()
        self.store.grant_account(created["clientId"], "another-data-root", "id-A")
        self.assertEqual(self.denied(principal, "zhiji_search_messages",
                                     subjects=[("id-A", "wechat_contact", "好友1")]), "account-not-authorized")
        other, other_created = self.pair("客户端二")
        self.store.grant_account(other_created["clientId"], FINGERPRINT, "id-A")
        self.assertEqual(self.denied(principal, "zhiji_search_messages",
                                     subjects=[("id-A", "wechat_contact", "好友1")]), "account-not-authorized")

    def test_non_recomputable_aggregates_are_blocked(self):
        principal, created = self.pair(allow_write=True)
        self.store.grant_account(created["clientId"], FINGERPRINT, "id-A")
        principal = self.connect(created)
        for action in ACCOUNT_LEVEL_TOOLS:
            self.assertEqual(self.denied(principal, action, account="id-A"), "aggregate-blocked", action)

    def test_list_accounts_shows_only_granted_accounts(self):
        principal, created = self.pair()
        self.store.grant_account(created["clientId"], FINGERPRINT, "id-A")
        result = self.mcp.call(principal, {"action": "zhiji_list_accounts", "params": {}})
        self.assertEqual(self.offline.listed, (None, frozenset({"id-A"})))
        self.assertEqual([item["accountId"] for item in result["accounts"]], ["id-A"])
        self.assertEqual(result["scope"], "authorized_accounts")
        # Nothing granted: an empty listing, never the whole installation.
        empty, _created = self.pair("空客户端")
        result = self.mcp.call(empty, {"action": "zhiji_list_accounts", "params": {}})
        self.assertEqual(result["accounts"], [])

    def test_saved_account_scope_never_reads_ungranted_accounts(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        registry = Path(directory.name) / "accounts.json"
        registry.write_text(json.dumps({"accounts": [
            {"accountId": "id-A", "account": "账号甲", "nickname": "甲"},
            {"accountId": "id-B", "account": "账号乙", "nickname": "乙"}]}), encoding="utf-8")
        read = []

        def summary(account):
            read.append(account)
            return {"saved": True, "sourceMode": "cached", "dataTimeMs": 1, "contacts": 2,
                    "readContacts": 2, "messages": 5, "sourceVersion": None}

        scope = SavedAccountScope(SimpleNamespace(saved_account_summary=summary), registry)
        result = scope.list_accounts(None, accounts=frozenset({"id-B"}))
        self.assertEqual(read, ["账号乙"])
        self.assertEqual([item["accountId"] for item in result["accounts"]], ["id-B"])
        self.assertEqual((result["scope"], result["accounts"][0]["scope"]),
                         ("authorized_accounts", "authorized_accounts"))
        self.assertEqual(scope.list_accounts()["scope"], "installation")

    def test_external_imports_are_granted_as_the_pseudo_account(self):
        principal, created = self.pair()
        imports = FakeImports([{"id": "friend-1", "name": "外部好友甲"}], [])
        self.mcp.imports = imports
        self.assertEqual(self.mcp.call(principal, {"action": "zhiji_list_import_contacts",
                                                   "params": {}})["contacts"], [])
        self.assertEqual(self.denied(principal, "zhiji_list_import_batches",
                                     subjects=[(EXTERNAL_ACCOUNT, "external_contact", "friend-1")]),
                         "account-not-authorized")
        self.store.grant_account(created["clientId"], FINGERPRINT, EXTERNAL_ACCOUNT)
        contacts = self.mcp.call(principal, {"action": "zhiji_list_import_contacts", "params": {}})["contacts"]
        self.assertEqual([item["id"] for item in contacts], ["friend-1"])
        self.mcp.authorize(principal, "zhiji_list_import_batches",
                           subjects=[(EXTERNAL_ACCOUNT, "external_contact", "friend-1")])
        # The pseudo-account never covers a WeChat contact, nor a WeChat account an external one.
        self.assertEqual(self.denied(principal, "zhiji_search_messages",
                                     subjects=[(EXTERNAL_ACCOUNT, "wechat_contact", "friend-1")]),
                         "account-not-authorized")
        self.store.grant_account(created["clientId"], FINGERPRINT, "id-A")
        self.assertEqual(self.denied(principal, "zhiji_list_import_batches",
                                     subjects=[("id-A", "external_contact", "friend-1")]),
                         "account-not-authorized")

    def test_assistant_friends_and_mentions_follow_the_granted_accounts(self):
        principal, created = self.pair(allow_write=True)
        self.mcp.assistant = FakeAssistant()
        self.store.grant_account(created["clientId"], FINGERPRINT, "id-A")
        principal = self.connect(created)
        result = self.mcp.call(principal, {"action": "zhiji_list_assistant_friends", "params": {},
                                           "subjects": SUBJECT_A})
        self.assertEqual([item["user"] for item in result["friends"]], ["好友1", "好友2", "好友3"])

    def test_revoke_account_bumps_the_version_and_stops_old_tokens(self):
        principal, created = self.pair()
        self.store.grant_account(created["clientId"], FINGERPRINT, "id-A")
        # Granting adds scope without invalidating the current token.
        self.mcp.authorize(principal, "zhiji_search_messages", subjects=[("id-A", "wechat_contact", "好友1")])
        notified = []
        self.store.add_version_listener(notified.append)
        before = self.store.client(created["clientId"])["auth_version"]
        self.assertEqual(self.store.revoke_account(created["clientId"], FINGERPRINT, "id-A"), {"revoked": True})
        self.assertEqual(self.store.client(created["clientId"])["auth_version"], before + 1)
        self.assertEqual(notified, [created["clientId"]])
        self.assertEqual(self.denied(principal, "zhiji_ping"), "stale-authorization")
        fresh = self.connect(created)
        self.assertEqual(self.denied(fresh, "zhiji_search_messages",
                                     subjects=[("id-A", "wechat_contact", "好友1")]), "account-not-authorized")

    def test_profile_revocation_and_account_deletion_clear_account_grants(self):
        _principal, created = self.pair()
        _other, other = self.pair("客户端二")
        self.store.grant_account(created["clientId"], FINGERPRINT, "id-A")
        self.store.grant_account(other["clientId"], FINGERPRINT, "id-A")
        self.store.grant_account(other["clientId"], FINGERPRINT, "id-B")
        self.assertCountEqual(self.store.revoke_account_grants("id-A"), [created["clientId"], other["clientId"]])
        self.assertEqual(self.store.account_grants(created["clientId"]), [])
        self.assertEqual([item["accountId"] for item in self.store.account_grants(other["clientId"])], ["id-B"])
        self.store.revoke_profile(other["clientId"])
        self.assertEqual(self.store.account_grants(other["clientId"]), [])
        with self.assertRaises(McpStoreError):
            self.store.grant_account(other["clientId"], FINGERPRINT, "id-A")

    def test_readable_scope_is_the_shared_source_of_truth(self):
        principal, created = self.pair()
        self.store.grant_account(created["clientId"], FINGERPRINT, "id-A")
        scope = self.mcp.readable_scope(principal)
        self.assertIsInstance(scope, Scope)
        self.assertEqual(scope.accounts(), frozenset({"id-A"}))
        self.assertTrue(scope.allows("id-A", "wechat_contact", "任意好友"))
        self.assertFalse(scope.allows("id-B", "wechat_contact", "好友1"))
        self.assertIsNone(scope.subjects("id-A"))
        self.assertEqual(scope.subjects("id-B"), frozenset())
        per_contact, per_created = self.pair("逐联系人客户端", "per_contact")
        self.store.grant_contact(per_created["clientId"], FINGERPRINT, "id-B", "wechat_contact", "好友2")
        scope = self.mcp.readable_scope(per_contact)
        self.assertEqual(scope.accounts(), frozenset({"id-B"}))
        self.assertEqual(scope.subjects("id-B"), frozenset({"好友2"}))
        self.assertFalse(scope.allows("id-B", "wechat_contact", "好友1"))
        full, _full = self.pair("完整客户端", "full")
        scope = self.mcp.readable_scope(full)
        self.assertIsNone(scope.accounts())
        self.assertTrue(scope.allows("任何账号", "wechat_contact", "任何人"))
        # Same entry checks as authorize: a stale token gets no scope.
        self.store.set_capabilities(created["clientId"], write=True)
        with self.assertRaises(McpError) as caught:
            self.mcp.readable_scope(principal)
        self.assertEqual(caught.exception.code, "stale-authorization")


class CapabilityTests(Harness):
    def test_read_only_profile_is_denied_write_send_and_manage_with_distinct_codes(self):
        for mode in ("full", "accounts", "per_contact"):
            principal, created = self.pair("只读客户端-" + mode, mode)
            self.store.grant_account(created["clientId"], FINGERPRINT, "id-A")
            self.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
            subjects = [("id-A", "wechat_contact", "好友1")]
            self.mcp.authorize(principal, "zhiji_search_messages", subjects=subjects)
            self.assertEqual(self.denied(principal, "zhiji_create_memory", subjects=subjects),
                             "write-not-enabled", mode)
            self.assertEqual(self.denied(principal, "zhiji_get_model_settings"), "management-not-enabled", mode)
            self.assertEqual(self.denied(principal, "zhiji_save_model_settings"), "management-not-enabled", mode)
        # Through the dispatcher the refusal comes before any dedup record.
        self.assertEqual(self.call_denied(principal, {"action": "zhiji_create_memory", "requestId": "req-1",
                                                      "params": {}, "subjects": SUBJECT_A}), "write-not-enabled")
        self.assertIsNone(self.store.dedup_result(created["clientId"], "req-1"))

    def test_each_capability_opens_only_its_own_family(self):
        principal, created = self.pair("写入客户端", "full", allow_write=True)
        subjects = [("id-A", "wechat_contact", "好友1")]
        self.mcp.authorize(principal, "zhiji_create_memory", subjects=subjects)
        self.mcp.authorize(principal, "zhiji_start_friend_analysis", subjects=subjects)
        self.assertEqual(self.denied(principal, "zhiji_get_model_settings"), "management-not-enabled")
        manager, _created = self.pair("管理客户端", "full", global_management=True)
        self.mcp.authorize(manager, "zhiji_get_model_settings")
        self.assertEqual(self.denied(manager, "zhiji_create_memory", subjects=subjects), "write-not-enabled")

    def test_capability_change_makes_the_old_token_stale(self):
        principal, created = self.pair()
        for change in ({"write": True}, {"send": True}, {"manage": True}, {"write": False}):
            before = self.store.client(created["clientId"])["auth_version"]
            self.store.set_capabilities(created["clientId"], **change)
            self.assertEqual(self.store.client(created["clientId"])["auth_version"], before + 1)
            self.assertEqual(self.denied(principal, "zhiji_ping"), "stale-authorization")
            principal = self.connect(created)
        self.assertEqual(self.store.capabilities(self.store.client(created["clientId"])),
                         {"write": False, "send": True, "manage": True})
        # The old switch is an alias of manage and bumps the version too.
        self.store.set_global_management(created["clientId"], False)
        self.assertEqual(self.denied(principal, "zhiji_ping"), "stale-authorization")
        with self.assertRaises(McpStoreError):
            self.store.set_capabilities(created["clientId"])
        with self.assertRaises(McpStoreError):
            self.store.set_capabilities(created["clientId"], write="yes")


class CatalogTests(Harness):
    def ping(self, principal):
        return self.mcp.call(principal, {"action": "zhiji_ping", "params": {}})

    def test_read_only_profile_sees_no_write_send_or_manage_tools(self):
        principal, _created = self.pair()
        result = self.ping(principal)
        self.assertEqual(result["profile"]["capabilities"], {"write": False, "send": False, "manage": False})
        open_tools = set(result["server"]["openCapabilities"])
        self.assertFalse(open_tools & (WRITE_TOOLS | SEND_TOOLS | MANAGE_TOOLS | ACCOUNT_LEVEL_TOOLS))
        self.assertEqual(open_tools, set(ALL_OPEN_TOOLS) - WRITE_TOOLS - SEND_TOOLS - MANAGE_TOOLS
                         - ACCOUNT_LEVEL_TOOLS)
        self.assertIn("zhiji_search_messages", open_tools)

    def test_legacy_send_grant_never_opens_send_tools_in_community(self):
        principal, _created = self.pair("旧客户端", "full", allow_write=True, allow_send=True,
                                       global_management=True)
        self.assertEqual(self.ping(principal)["server"]["openCapabilities"], ALL_OPEN_TOOLS)
        for action in SEND_TOOLS:
            code = self.call_denied(principal, {"action": action, "requestId": "req-send", "params": {}})
            self.assertEqual(code, "action-not-registered")
        self.assertIsNone(self.store.dedup_result(principal["clientId"], "req-send"))

    def test_disabled_business_plugins_hide_their_tools_and_a_broken_check_fails_closed(self):
        principal, _created = self.pair("完整客户端", "full", allow_write=True, allow_send=True,
                                       global_management=True)
        self.mcp.plugin_enabled = lambda plugin_id: plugin_id != "kyf.advisor"
        open_tools = set(self.ping(principal)["server"]["openCapabilities"])
        self.assertFalse(any("assistant" in name for name in open_tools))
        self.assertIn("zhiji_get_task", open_tools)
        self.assertEqual(self.call_denied(principal, {"action": "zhiji_get_assistant_catalog", "params": {}}),
                         "capability-not-open")

        def broken(_plugin_id):
            raise RuntimeError("synthetic registry failure")

        self.mcp.plugin_enabled = broken
        open_tools = set(self.ping(principal)["server"]["openCapabilities"])
        self.assertFalse(open_tools & {name for name, entry in REGISTRY.items() if "plugin" in entry})
        self.assertIn("zhiji_search_messages", open_tools)


class MigrationTests(unittest.TestCase):
    def legacy_database(self, rows):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        store_dir = Path(directory.name) / "core" / "mcp"
        store_dir.mkdir(parents=True)
        connection = sqlite3.connect(store_dir / "mcp.sqlite3")
        connection.executescript("""
        CREATE TABLE clients (
          client_id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          mode TEXT NOT NULL, global_management INTEGER NOT NULL DEFAULT 0,
          auth_version INTEGER NOT NULL DEFAULT 1, credential_salt TEXT NOT NULL,
          credential_hash TEXT NOT NULL, created_at_ms INTEGER NOT NULL, last_used_at_ms INTEGER);
        CREATE TABLE contact_grants (
          client_id TEXT NOT NULL, source_fingerprint TEXT NOT NULL, account_id TEXT NOT NULL,
          subject_type TEXT NOT NULL, subject_id TEXT NOT NULL, created_at_ms INTEGER NOT NULL,
          PRIMARY KEY (client_id, source_fingerprint, account_id, subject_type, subject_id));
        """)
        secrets = {}
        try:
            for client_id, mode, management, version in rows:
                secret = "zmc_" + client_id[-1] * 64
                secrets[client_id] = secret
                connection.execute("INSERT INTO clients VALUES(?,?,1,?,?,?,?,?,?,NULL)",
                                   (client_id, "旧" + mode, mode, management, version, "salt",
                                    core_mcp_store.credential_hash("salt", secret), core_mcp_store.now_ms()))
            connection.execute("INSERT INTO contact_grants VALUES(?,?,?,?,?,?)",
                               ("mcp_legacy_per_a", FINGERPRINT, "id-A", "wechat_contact", "好友1", 1))
            connection.commit()
        finally:
            connection.close()
        services = McpServices(directory.name, source_fingerprint=lambda: FINGERPRINT)
        self.addCleanup(services.close)
        return services, secrets

    def test_legacy_profiles_keep_their_effective_permissions(self):
        services, secrets = self.legacy_database([("mcp_legacy_full_1", "full", 0, 4),
                                                  ("mcp_legacy_per_a", "per_contact", 0, 2),
                                                  ("mcp_legacy_per_b", "per_contact", 1, 3)])
        store = services.store
        capabilities = {client_id: store.capabilities(store.client(client_id)) for client_id in secrets}
        self.assertEqual(capabilities, {
            "mcp_legacy_full_1": {"write": True, "send": False, "manage": True},
            "mcp_legacy_per_a": {"write": True, "send": False, "manage": False},
            "mcp_legacy_per_b": {"write": True, "send": False, "manage": True}})
        # No version bump: nothing an existing token could do has changed.
        self.assertEqual({client_id: store.client(client_id)["auth_version"] for client_id in secrets},
                         {"mcp_legacy_full_1": 4, "mcp_legacy_per_a": 2, "mcp_legacy_per_b": 3})

        def principal(client_id):
            token = services.handshake({"clientId": client_id, "credential": secrets[client_id]})
            return services.authenticate(headers(token["token"]))

        full = principal("mcp_legacy_full_1")
        for action in ("zhiji_create_memory", "zhiji_get_model_settings",
                       "zhiji_get_self_profile"):
            services.authorize(full, action, subjects=[("新账号", "wechat_contact", "新好友")])
        per_a = principal("mcp_legacy_per_a")
        subjects = [("id-A", "wechat_contact", "好友1")]
        services.authorize(per_a, "zhiji_create_memory", subjects=subjects)
        with self.assertRaises(McpError) as caught:
            services.authorize(per_a, "zhiji_get_model_settings")
        self.assertEqual(caught.exception.code, "management-not-enabled")
        services.authorize(principal("mcp_legacy_per_b"), "zhiji_get_model_settings")
        # Reopening never re-runs the migration over later changes.
        store.set_capabilities("mcp_legacy_full_1", write=False)
        services.close()
        reopened = McpServices(store.directory.parent.parent, source_fingerprint=lambda: FINGERPRINT)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.store.capabilities(reopened.store.client("mcp_legacy_full_1"))["write"])
        names = {row[1] for row in reopened.store.connection.execute("PRAGMA table_info(account_grants)")}
        self.assertEqual(names, {"client_id", "source_fingerprint", "account_id", "created_at_ms"})


class AdminTests(Harness):
    def admin(self, method, parts, body=None):
        return handle_admin(self.mcp, method, parts, {}, body if body is not None else {})

    def test_profile_capabilities_and_account_grants_through_the_settings_channel(self):
        created = self.admin("POST", ["profiles"], {"name": "设置页客户端", "mode": "accounts",
                                                    "allowWrite": True, "allowSend": False,
                                                    "globalManagement": False})
        client_id = created["profile"]["clientId"]
        self.assertEqual(created["profile"]["capabilities"], {"write": True, "send": False, "manage": False})
        grant = {"clientId": client_id, "sourceFingerprint": FINGERPRINT, "accountId": "id-A"}
        result = self.admin("POST", ["accounts"], grant)
        self.assertEqual([item["accountId"] for item in result["accountGrants"]], ["id-A"])
        detail = self.admin("GET", ["profiles", client_id])
        self.assertEqual(detail["accountGrants"][0]["sourceFingerprint"], FINGERPRINT)
        self.assertEqual(detail["profile"]["accountGrants"], detail["accountGrants"])
        self.assertEqual(detail["profile"]["mode"], "accounts")
        listing = self.admin("GET", ["profiles"])["profiles"][0]
        self.assertEqual((listing["mode"], listing["capabilities"]["write"], len(listing["accountGrants"])),
                         ("accounts", True, 1))
        version = detail["profile"]["authVersion"]
        view = self.admin("POST", ["profiles", client_id, "capabilities"], {"send": True, "manage": True})
        self.assertEqual(view["profile"]["capabilities"], {"write": True, "send": True, "manage": True})
        self.assertEqual(view["profile"]["authVersion"], version + 1)
        view = self.admin("POST", ["profiles", client_id, "global-management"], {"enabled": False})
        self.assertEqual(view["profile"]["capabilities"]["manage"], False)
        revoked = self.admin("POST", ["accounts", "revoke"], grant)
        self.assertEqual((revoked["revoked"], revoked["accountGrants"]), (True, []))
        for broken in ({"name": "x", "allowWrite": "yes"}, {"name": "x", "mode": "everything"},
                       {"name": "x", "extra": 1}):
            with self.assertRaises(McpAdminError):
                self.admin("POST", ["profiles"], broken)
        for broken in ({}, {"write": 1}, {"other": True}):
            with self.assertRaises(McpAdminError):
                self.admin("POST", ["profiles", client_id, "capabilities"], broken)
        for broken in ({**grant, "accountId": ""}, {**grant, "sourceFingerprint": ""}, {"clientId": client_id}):
            with self.assertRaises(McpAdminError):
                self.admin("POST", ["accounts"], broken)

    def test_directory_names_the_external_pseudo_account(self):
        self.mcp.directory = lambda: {"accounts": []}
        result = self.admin("GET", ["directory"])
        self.assertEqual((result["externalAccountId"], result["sourceFingerprint"]), (EXTERNAL_ACCOUNT, FINGERPRINT))


if __name__ == "__main__":
    unittest.main()
