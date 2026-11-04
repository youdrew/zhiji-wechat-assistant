"""Phase-2 MCP runtime on synthetic data: connection leases, the explicit saved-account
offline scope and cursors bound to client, authorization version and source identity."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_leases import LeaseService
from core_mcp_offline import SavedAccountScope
from core_mcp_proxy import MEDIA_ITEM_MAX_BASE64, McpError, McpServices
from core_account_db import AccountStores
from friend_service import FriendService
from friend_store import FriendStore, content_hash

FINGERPRINT = "synthetic-install-fingerprint"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_services():
    directory = tempfile.TemporaryDirectory()
    clock = Clock()
    leases = LeaseService(idle_seconds=300, monotonic=clock)
    services = McpServices(directory.name, source_fingerprint=lambda: FINGERPRINT, monotonic=clock)
    services.leases = leases
    services._directory = directory  # keep the temp directory alive
    services._clock = clock
    return services


def headers(token):
    return {"Authorization": "Bearer " + token}


def pair(mcp, name="合成客户端", mode="full", pid=None, **capabilities):
    from test_core_mcp import legacy_capabilities
    created = mcp.store.create_profile(name, mode, **legacy_capabilities(mode, **capabilities))
    result = mcp.handshake({"clientId": created["clientId"], "credential": created["credential"],
                            **({"pid": pid} if pid is not None else {})})
    return created, result


def live_pid():
    """A real live process id, since leases are reclaimed the moment the holder dies."""
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    holder.test_cleanup = True
    return holder


def terminate(holder):
    holder.terminate()
    holder.wait(timeout=10)


class ConnectionLeaseTests(unittest.TestCase):
    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.leases = self.mcp.leases

    def test_handshake_holds_one_lease_per_connection(self):
        holder = live_pid()
        self.addCleanup(terminate, holder)
        created, token = pair(self.mcp, pid=holder.pid)
        self.assertEqual(self.leases.count("mcp"), 1)
        self.assertEqual(self.leases.holders("mcp")[0]["pid"], holder.pid)
        self.assertEqual(self.mcp.active_connections(), 1)
        self.mcp.disconnect_all()
        self.assertEqual(self.leases.count("mcp"), 0)

    def test_dead_pid_is_refused_at_handshake(self):
        created = self.mcp.store.create_profile("合成客户端", "full")
        with self.assertRaises(McpError) as caught:
            self.mcp.handshake({"clientId": created["clientId"], "credential": created["credential"],
                                "pid": 4_000_000})
        self.assertEqual(caught.exception.code, "invalid-request")

    def test_session_expiry_releases_the_lease(self):
        holder = live_pid()
        self.addCleanup(terminate, holder)
        _created, _token = pair(self.mcp, pid=holder.pid)
        self.mcp._clock.advance(15 * 60 + 1)
        self.assertEqual(self.mcp.active_connections(), 0)
        self.assertEqual(self.leases.count("mcp"), 0)

    def test_dead_adapter_process_is_reclaimed(self):
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            _created, _token = pair(self.mcp, pid=holder.pid)
            self.assertEqual(self.mcp.active_connections(), 1)
            self.assertEqual(self.leases.count("mcp"), 1)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        deadline = time.monotonic() + 10
        while holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.mcp.active_connections(), 0)
        self.assertEqual(self.leases.count("mcp"), 0)

    def test_gui_and_mcp_leases_never_kill_each_other(self):
        self.leases.acquire("gui", "main", pid=100)
        holder = live_pid()
        self.addCleanup(terminate, holder)
        _created, _token = pair(self.mcp, pid=holder.pid)
        self.mcp.disconnect_all()
        self.assertEqual(self.leases.count("gui"), 1)
        self.assertEqual(self.leases.count("mcp"), 0)

    def test_activity_keeps_the_lease_alive(self):
        holder = live_pid()
        self.addCleanup(terminate, holder)
        _created, token = pair(self.mcp, pid=holder.pid)
        self.mcp._clock.advance(10 * 60)
        self.mcp.authenticate(headers(token["token"]))
        self.mcp._clock.advance(10 * 60)
        self.mcp.authenticate(headers(token["token"]))
        self.assertEqual(self.mcp.active_connections(), 1)
        self.assertEqual(self.leases.count("mcp"), 1)


class FakeOffline:
    """Synthetic saved-account scope; records calls so cross-account reads are detectable."""

    def __init__(self):
        self.calls = []
        self.allowed = "unset"
        self.messages = [{"id": f"m{i}", "text": f"合成消息{i}"} for i in range(5)]
        self.memories = [{"id": f"mem{i}", "text": f"合成记忆{i}", "version": 1} for i in range(7)]
        self.media_result = {"account": "id-A", "user": "好友1", "available": False,
                             "reason": "offline", "items": []}
        self.media_calls = []

    def list_accounts(self, allowed_subjects=None):
        self.allowed = allowed_subjects
        return {"accounts": [{"accountId": "id-A", "label": "甲", "saved": True,
                              "sourceMode": "cached", "dataTimeMs": 1,
                              "coverage": {"contacts": 1, "readContacts": 1, "messages": 3},
                              "sourceVersion": {"revision": 2},
                              "scope": "installation" if allowed_subjects is None else "authorized_contacts"}],
                "scope": "installation" if allowed_subjects is None else "authorized_contacts"}

    def _guard(self, account_id, subject_id):
        if account_id not in ("id-A",) or subject_id != "好友1":
            raise McpError(404, "account-unknown", "该账号不是本机已保存账号")

    def search_messages(self, account_id, subject_id, *, query, limit, offset):
        self.calls.append((account_id, subject_id, query, limit, offset))
        self._guard(account_id, subject_id)
        return {"account": account_id, "user": subject_id, "limit": limit, "offset": offset,
                "sourceMode": "cached", "dataTimeMs": 5,
                "coverage": {"state": "ready", "readCount": 3, "totalCount": 3},
                "sourceVersion": {"epoch": "e1", "revision": 2},
                "messages": self.messages[offset:offset + limit]}

    def message_context(self, account_id, subject_id, message_id):
        self.calls.append((account_id, subject_id, message_id))
        self._guard(account_id, subject_id)
        return {"account": account_id, "user": subject_id, "status": "ok",
                "sourceMode": "cached", "messages": [{"id": message_id, "text": "上下文"}]}

    def list_memories(self, account_id, subject_id, *, status, limit, offset):
        self.calls.append(("memories", account_id, subject_id, status, limit, offset))
        self._guard(account_id, subject_id)
        return {"account": account_id, "user": subject_id, "status": status, "total": len(self.memories),
                "offset": offset, "memoryVersion": 3, "items": self.memories[offset:offset + limit],
                "sourceMode": "cached", "dataTimeMs": 5,
                "coverage": {"state": "ready", "readCount": 3, "totalCount": 3},
                "sourceVersion": {"epoch": "e1", "revision": 2}}

    def memory_evidence(self, account_id, subject_id, memory_id, ordinal=None):
        self.calls.append(("evidence", account_id, subject_id, memory_id, ordinal))
        self._guard(account_id, subject_id)
        return {"account": account_id, "user": subject_id, "memoryId": memory_id,
                "memory": {"id": memory_id, "version": 2},
                "versions": [{"version": 1, "text": "旧文"}],
                "evidence": [{"ordinal": 0, "excerpt": "合成摘录", "status": "ok", "messages": []}],
                "sourceMode": "cached"}

    def media(self, account_id, subject_id, message_id, *, frames=1):
        self.calls.append(("media", account_id, subject_id, message_id, frames))
        self._guard(account_id, subject_id)
        self.media_calls.append((account_id, subject_id, message_id, frames))
        return {**self.media_result, "account": account_id, "user": subject_id,
                "messageId": message_id, "frames": frames}


class OfflineHandlerTests(unittest.TestCase):
    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.provider = FakeOffline()
        self.mcp.offline = lambda: self.provider

    def principal(self, name="合成客户端", mode="full"):
        created, token = pair(self.mcp, name=name, mode=mode)
        return self.mcp.authenticate(headers(token["token"])), created

    def call(self, principal, body):
        return self.mcp.call(principal, body)

    def test_ping_lists_open_capabilities(self):
        principal, _created = self.principal()
        result = self.call(principal, {"action": "zhiji_ping", "params": {}})
        self.assertEqual(result["server"]["openCapabilities"],
["zhiji_clear_local_data", "zhiji_confirm_memory", "zhiji_create_memory",
                          "zhiji_delete_account", "zhiji_delete_assistant_skill",
                          "zhiji_delete_assistant_template", "zhiji_delete_memory",
                          "zhiji_delete_observation", "zhiji_edit_self_timeline",
                          "zhiji_enable_assistant_template",
                          "zhiji_find_contacts", "zhiji_get_assistant_catalog", "zhiji_get_assistant_run",
                          "zhiji_get_assistant_thread", "zhiji_get_contact", "zhiji_get_contact_brief", "zhiji_get_contact_goal",
                          "zhiji_get_friend_analysis", "zhiji_get_import_batch",
                          "zhiji_get_import_messages", "zhiji_get_media",
                          "zhiji_get_memory_evidence", "zhiji_get_message_context",
                          "zhiji_get_message_insights", "zhiji_get_model_settings",
                          "zhiji_get_plugins", "zhiji_get_recent_messages", "zhiji_get_self_analysis", "zhiji_get_self_profile",
                          "zhiji_get_task", "zhiji_get_usage",
                          "zhiji_import_assistant_skill", "zhiji_list_accounts",
                          "zhiji_list_assistant_friends", "zhiji_list_assistant_threads",
                          "zhiji_list_import_batches", "zhiji_list_import_candidates",
                          "zhiji_list_import_contacts", "zhiji_list_memories",
                          "zhiji_list_observations", "zhiji_list_recent_conversations", "zhiji_new_assistant_thread", "zhiji_ping",
                          "zhiji_remove_import_material", "zhiji_save_assistant_template",
                          "zhiji_save_contact_goal", "zhiji_save_import_batch",
                          "zhiji_save_model_settings", "zhiji_save_observation",
                          "zhiji_save_self_profile", "zhiji_search_messages",
                          "zhiji_set_plugin_enabled",
                          "zhiji_start_friend_analysis", "zhiji_start_import_recognition",
                          "zhiji_start_message_insights", "zhiji_start_self_analysis",
                          "zhiji_stop_assistant_run", "zhiji_stop_import_recognition",
                          "zhiji_stop_self_analysis", "zhiji_stop_task",
                          "zhiji_submit_assistant_question", "zhiji_undo_self_timeline",
                          "zhiji_update_memory"])

    def test_list_accounts_restricts_per_contact_mode_to_granted_subjects(self):
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友2")
        result = self.call(principal, {"action": "zhiji_list_accounts", "params": {}})
        # 汇总按授权联系人重算：只传已授权主体，绝不按整账号覆盖。
        self.assertEqual(self.provider.allowed, {"id-A": ["好友1", "好友2"]})
        self.assertEqual(result["scope"], "authorized_contacts")
        self.assertEqual(result["accounts"][0]["scope"], "authorized_contacts")
        full, _created = self.principal("完整客户端", mode="full")
        result = self.call(full, {"action": "zhiji_list_accounts", "params": {}})
        self.assertIsNone(self.provider.allowed)
        self.assertEqual(result["scope"], "installation")

    def test_per_contact_mode_reads_only_authorized_subjects(self):
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友2")
        for action, params in (("zhiji_list_memories", {"status": "active"}),
                               ("zhiji_get_memory_evidence", {"memoryId": "mem-1"}),
                               ("zhiji_get_media", {"messageId": "mv1"}),
                               ("zhiji_search_messages", {"query": "合成"}),
                               ("zhiji_get_message_context", {"messageId": "m1"})):
            body = {"action": action, "params": params,
                    "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友1"}]}
            with self.assertRaises(McpError) as caught:
                self.call(principal, body)
            self.assertEqual(caught.exception.code, "contact-not-authorized", action)
        # 只有授权主体的调用到达了提供方。
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.provider.media_calls, [])

    def test_search_maps_one_subject_and_reports_metadata(self):
        principal, _created = self.principal()
        result = self.call(principal, {"action": "zhiji_search_messages",
                                       "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                     "subjectId": "好友1"}],
                                       "params": {"query": "合成", "limit": 2}})
        self.assertEqual(self.provider.calls, [("id-A", "好友1", "合成", 2, 0)])
        self.assertEqual(result["sourceMode"], "cached")
        self.assertEqual(result["coverage"]["state"], "ready")
        self.assertIsNotNone(result["cursor"])

    def test_cursor_roundtrip_and_binding(self):
        principal, _created = self.principal()
        first = self.call(principal, {"action": "zhiji_search_messages",
                                      "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                    "subjectId": "好友1"}],
                                      "params": {"query": "合成", "limit": 2}})
        second = self.call(principal, {"action": "zhiji_search_messages",
                                       "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                     "subjectId": "好友1"}],
                                       "params": {"query": "合成", "limit": 2, "cursor": first["cursor"]}})
        self.assertEqual(self.provider.calls[-1], ("id-A", "好友1", "合成", 2, 2))

    def test_cursor_is_rejected_across_clients_and_versions(self):
        first_principal, _created = self.principal("客户端一")
        result = self.call(first_principal, {"action": "zhiji_search_messages",
                                             "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                           "subjectId": "好友1"}],
                                             "params": {"query": "合成", "limit": 2}})
        # Another client cannot continue the cursor.
        second_principal, _second = self.principal("客户端二")
        with self.assertRaises(McpError) as caught:
            self.call(second_principal, {"action": "zhiji_search_messages",
                                         "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                       "subjectId": "好友1"}],
                                         "params": {"query": "合成", "limit": 2, "cursor": result["cursor"]}})
        self.assertEqual(caught.exception.code, "cursor-invalid")
        # An authorization version bump invalidates it too.
        self.mcp.store.set_mode(_created["clientId"], "per_contact")
        self.mcp.store.set_mode(_created["clientId"], "full")  # version now 3
        _created3, refreshed = pair(self.mcp, name="客户端一")
        principal = self.mcp.authenticate(headers(refreshed["token"]))
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_search_messages",
                                  "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                "subjectId": "好友1"}],
                                  "params": {"query": "合成", "limit": 2, "cursor": result["cursor"]}})
        self.assertEqual(caught.exception.code, "cursor-invalid")
        # Tampered signatures are rejected as well.
        tampered = result["cursor"][:-1] + ("0" if result["cursor"][-1] != "0" else "1")
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_search_messages",
                                  "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                "subjectId": "好友1"}],
                                  "params": {"query": "合成", "limit": 2, "cursor": tampered}})
        self.assertEqual(caught.exception.code, "cursor-invalid")

    def test_requests_never_reach_a_second_account_or_foreign_subjects(self):
        principal, _created = self.principal()
        for subjects in ([{"accountId": "id-B", "subjectType": "wechat_contact", "subjectId": "好友1"}],
                         [{"accountId": "id-A", "subjectType": "external_contact", "subjectId": "导入"}],
                         [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友1"},
                          {"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友2"}]):
            with self.assertRaises(McpError) as caught:
                self.call(principal, {"action": "zhiji_search_messages", "subjects": subjects,
                                      "params": {"query": "合成"}})
            self.assertIn(caught.exception.code, ("account-unknown", "invalid-request"))
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_search_messages",
                                  "account": "id-B",
                                  "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                "subjectId": "好友1"}],
                                  "params": {"query": "合成"}})
        self.assertEqual(caught.exception.code, "invalid-request")
        # Only the id-B probe reached the provider; every malformed shape was refused earlier.
        self.assertEqual(self.provider.calls, [("id-B", "好友1", "合成", 20, 0)])

    def test_memory_listing_pages_filters_and_cursors(self):
        principal, _created = self.principal()
        first = self.call(principal, {"action": "zhiji_list_memories",
                                      "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                    "subjectId": "好友1"}],
                                      "params": {"status": "confirmed", "limit": 3}})
        self.assertEqual(self.provider.calls[-1], ("memories", "id-A", "好友1", "confirmed", 3, 0))
        self.assertEqual(len(first["items"]), 3)
        self.assertEqual(first["memoryVersion"], 3)
        self.assertEqual(first["requestScope"], "installation")
        self.assertIsNotNone(first["cursor"])
        second = self.call(principal, {"action": "zhiji_list_memories",
                                       "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                     "subjectId": "好友1"}],
                                       "params": {"status": "confirmed", "limit": 3, "cursor": first["cursor"]}})
        self.assertEqual(self.provider.calls[-1], ("memories", "id-A", "好友1", "confirmed", 3, 3))
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_list_memories",
                                  "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                "subjectId": "好友1"}],
                                  "params": {"status": "全部给我"}})
        self.assertEqual(caught.exception.code, "invalid-request")

    def test_memory_evidence_passes_memory_and_ordinal(self):
        principal, _created = self.principal()
        result = self.call(principal, {"action": "zhiji_get_memory_evidence",
                                       "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                     "subjectId": "好友1"}],
                                       "params": {"memoryId": "mem-1", "ordinal": 0}})
        self.assertEqual(self.provider.calls[-1], ("evidence", "id-A", "好友1", "mem-1", 0))
        self.assertEqual(result["memory"]["id"], "mem-1")
        self.assertEqual(result["versions"][0]["text"], "旧文")
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_memory_evidence",
                                  "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                "subjectId": "好友1"}],
                                  "params": {"ordinal": 0}})
        self.assertEqual(caught.exception.code, "invalid-request")
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_memory_evidence",
                                  "subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                "subjectId": "好友1"}],
                                  "params": {"memoryId": "mem-1", "ordinal": -1}})
        self.assertEqual(caught.exception.code, "invalid-request")

    def test_memory_cursor_dies_when_the_contact_is_revoked_mid_read(self):
        created = self.mcp.store.create_profile("逐联系人客户端", "per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")

        def re_pair():
            token = self.mcp.handshake({"clientId": created["clientId"],
                                        "credential": created["credential"]})
            return self.mcp.authenticate(headers(token["token"]))

        principal = re_pair()
        subjects = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友1"}]
        first = self.call(principal, {"action": "zhiji_list_memories", "subjects": subjects,
                                      "params": {"limit": 3}})
        self.assertIsNotNone(first["cursor"])
        # 撤权立即阻止续读：旧令牌先因授权版本失效被拒。
        self.mcp.store.revoke_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_list_memories", "subjects": subjects,
                                  "params": {"limit": 3, "cursor": first["cursor"]}})
        self.assertEqual(caught.exception.code, "stale-authorization")
        # 重新配对后仍因联系人未授权而拒绝。
        revoked_principal = re_pair()
        with self.assertRaises(McpError) as caught:
            self.call(revoked_principal, {"action": "zhiji_list_memories", "subjects": subjects,
                                          "params": {"limit": 3, "cursor": first["cursor"]}})
        self.assertEqual(caught.exception.code, "contact-not-authorized")
        # 即使之后重新授权，撤权前的游标也不可续读（绑定旧授权版本）。
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        principal = re_pair()
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_list_memories", "subjects": subjects,
                                  "params": {"limit": 3, "cursor": first["cursor"]}})
        self.assertEqual(caught.exception.code, "cursor-invalid")

    def test_media_parameters_and_contract_caps(self):
        principal, _created = self.principal()
        subjects = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友1"}]
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_media", "subjects": subjects,
                                  "params": {"messageId": "mv1", "frames": 5}})
        self.assertEqual(caught.exception.code, "invalid-request")
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_media", "subjects": subjects, "params": {}})
        self.assertEqual(caught.exception.code, "invalid-request")
        self.call(principal, {"action": "zhiji_get_media", "subjects": subjects,
                              "params": {"messageId": "mv1", "frames": 2}})
        self.assertEqual(self.provider.media_calls, [("id-A", "好友1", "mv1", 2)])
        # 超过 2 MiB 内嵌上限的内容在任何提供方下都不会出现：只报告不可用。
        self.provider.media_result = {"available": True, "items": [{"mime": "image/jpeg",
                                                                    "data": "A" * (MEDIA_ITEM_MAX_BASE64 + 1)}]}
        result = self.call(principal, {"action": "zhiji_get_media", "subjects": subjects,
                                       "params": {"messageId": "mv1"}})
        self.assertEqual((result["available"], result["reason"], result["items"]),
                         (False, "unavailable", []))

    def test_missing_provider_reports_a_stable_not_open_error(self):
        self.mcp.offline = None
        principal, _created = self.principal()
        for action, body in (("zhiji_list_accounts", {"params": {}}),
                             ("zhiji_get_media", {"subjects": [{"accountId": "id-A", "subjectType": "wechat_contact",
                                                                "subjectId": "好友1"}], "params": {"messageId": "mv1"}})):
            with self.assertRaises(McpError) as caught:
                self.call(principal, {"action": action, **body})
            self.assertEqual((caught.exception.code, caught.exception.status),
                             ("capability-not-open", 501))

    def test_provider_failure_is_reported_not_crashing(self):
        self.mcp.offline = lambda: None
        principal, _created = self.principal()
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_list_accounts", "params": {}})
        self.assertEqual(caught.exception.code, "capability-not-open")
        self.mcp.offline = lambda: (_ for _ in ()).throw(RuntimeError("bridge is closing"))
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_list_accounts", "params": {}})
        self.assertEqual(caught.exception.code, "core-unavailable")


class SyntheticSavedData:
    """One synthetic account with a small projection under a temporary data root."""

    def __init__(self):
        self.root = tempfile.TemporaryDirectory()
        base = Path(self.root.name)
        self.stores = AccountStores(base / ".local" / "advisor-data")
        self.database = self.stores.account("账号甲")
        self.database.context_begin("好友1", "{}", source_id="synthetic-source")
        rows = [{"msg_id": f"m{i}", "sort_key": f"2026-01-01 00:00:{i:02d}", "group_chat": 0,
                 "side": "self" if i % 2 else "other", "sender_id": "好友1",
                 "sender_name": "好友1", "kind": "文本", "time_ms": 1700000000000 + i,
                 "raw": "{}", "text": f"合成消息{i}"} for i in range(6)]
        rows.append({"msg_id": "mv1", "sort_key": "2026-01-01 00:00:06", "group_chat": 0,
                     "side": "other", "sender_id": "好友1", "sender_name": "好友1",
                     "kind": "图片", "time_ms": 1700000000006,
                     "raw": json.dumps({"media": {"kind": "image", "width": 800, "height": 600}}),
                     "text": None})
        rows.append({"msg_id": "vs1", "sort_key": "2026-01-01 00:00:07", "group_chat": 0,
                     "side": "other", "sender_id": "好友1", "sender_name": "好友1",
                     "kind": "语音", "time_ms": 1700000000007,
                     "raw": json.dumps({"media": {"kind": "voice", "durationMs": 3000,
                                                  "transcript": "合成转写文本"}}),
                     "text": None})
        self.database.context_append_page("好友1", rows, "{}", "{}")
        self.database.context_finish("好友1", len(rows))
        # 第二个联系人的投影：逐联系人汇总绝不能把它计入。
        self.database.context_begin("好友2", "{}", source_id="synthetic-source")
        other = [{"msg_id": f"n{i}", "sort_key": f"2026-01-02 00:00:{i:02d}", "group_chat": 0,
                  "side": "self", "sender_id": "好友2", "sender_name": "好友2", "kind": "文本",
                  "time_ms": 1700010000000 + i, "raw": "{}", "text": f"其他联系人的消息{i}"}
                 for i in range(4)]
        self.database.context_append_page("好友2", other, "{}", "{}")
        self.database.context_finish("好友2", len(other))
        self._add_memories()
        registry = base / ".local" / "real-client-data"
        registry.mkdir(parents=True, exist_ok=True)
        (registry / "accounts.json").write_text(json.dumps(
            {"accounts": [{"accountId": "id-A", "account": "账号甲", "nickname": "甲", "wechatId": ""}]},
            ensure_ascii=False), encoding="utf-8")

    def _add_memories(self):
        store = FriendStore(self.database)
        self.confirmed_id = store.create_user_memory("好友1", "用户记录的合成记忆", ["爱好"])["id"]

        def insert_pending(connection):
            connection.execute(
                "INSERT INTO memories(id,user,tags_json,text,norm,status,origin,created_at_ms,updated_at_ms)"
                " VALUES('mem-p1','好友1','[\"经历\"]','候选合成记忆','候选合成记忆','pending','ai',"
                "1700000100000,1700000100000)")
            target = connection.execute(
                "SELECT * FROM context_messages WHERE user='好友1' AND msg_id='m1'").fetchone()
            connection.execute(
                "INSERT INTO memory_evidence(memory_id,ordinal,source_fp,msg_id,content_hash,role,time_ms,"
                "excerpt,status,checked_at_ms) VALUES('mem-p1',0,'synthetic-source','m1',?,"
                "'other',1700000000001,'合成摘录','ok',1700000100000)",
                (content_hash(dict(target)),))
        store.write(insert_pending)

    def close(self):
        self.stores.close()
        self.root.cleanup()


def make_friends(source_behavior="offline"):
    data = SyntheticSavedData()

    class StubSource:
        def advisor_identity(self, messages=True):
            if source_behavior == "offline":
                raise RuntimeError("微信未运行")
            if source_behavior == "other-account":
                return ("账号乙", None)
            return ("账号甲", None)

    advisor = types.SimpleNamespace(stores=data.stores, source=StubSource(), context=None)
    return data, FriendService(advisor)


class SavedScopeTests(unittest.TestCase):
    def setUp(self):
        self.data, self.friends = make_friends()
        self.addCleanup(self.data.close)
        self.scope = SavedAccountScope(self.friends, self.registry_path())

    def registry_path(self, root=None):
        return Path(root or self.data.root.name) / ".local" / "real-client-data" / "accounts.json"

    def test_saved_reads_are_marked_cached_with_metadata(self):
        result = self.scope.search_messages("id-A", "好友1", query="合成消息", limit=10, offset=0)
        self.assertEqual(result["sourceMode"], "cached")
        self.assertEqual(result["accountId"], "账号甲")
        self.assertEqual(len(result["messages"]), 6)
        self.assertEqual(result["coverage"]["state"], "ready")
        self.assertIsNotNone(result["dataTimeMs"])
        self.assertEqual(result["sourceVersion"]["revision"], 1)

    def test_unknown_account_and_foreign_subject_never_cross_accounts(self):
        with self.assertRaises(McpError) as caught:
            self.scope.search_messages("id-其他", "好友1", query="合成", limit=10, offset=0)
        self.assertEqual(caught.exception.code, "account-unknown")
        # A subject with no saved projection is an honest empty result scoped to
        # that one account; no other account's data is ever opened.
        result = self.scope.search_messages("id-A", "陌生人", query="合成", limit=10, offset=0)
        self.assertEqual(result["messages"], [])
        self.assertEqual(result["coverage"]["state"], "empty")
        self.assertEqual(self.data.stores._accounts.keys(), {"账号甲"})  # no cross-account db was opened

    def test_missing_saved_data_is_a_reported_gap_not_a_new_database(self):
        # A second registry account without any projection: never materialized on read.
        (Path(self.data.root.name) / ".local" / "real-client-data" / "accounts.json").write_text(json.dumps(
            {"accounts": [{"accountId": "id-A", "account": "账号甲", "nickname": "", "wechatId": ""},
                          {"accountId": "id-B", "account": "账号乙", "nickname": "", "wechatId": ""}]},
            ensure_ascii=False), encoding="utf-8")
        result = self.scope.list_accounts()
        by_id = {item["accountId"]: item for item in result["accounts"]}
        self.assertTrue(by_id["id-A"]["saved"])
        self.assertFalse(by_id["id-B"]["saved"])
        self.assertEqual(by_id["id-B"]["coverage"]["messages"], 0)
        with self.assertRaises(McpError) as caught:
            self.scope.search_messages("id-B", "好友1", query="合成", limit=10, offset=0)
        self.assertEqual(caught.exception.code, "cache-gap")
        self.assertNotIn("账号乙", self.data.stores._accounts)

    def test_message_context_and_live_mode_marking(self):
        result = self.scope.message_context("id-A", "好友1", "m3")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["messages"][3]["id"], "m3")
        self.assertEqual(result["sourceMode"], "cached")
        data, friends = make_friends(source_behavior="live")
        self.addCleanup(data.close)
        live = SavedAccountScope(friends, self.registry_path(data.root.name))
        marked = live.search_messages("id-A", "好友1", query="合成消息", limit=10, offset=0)
        self.assertEqual(marked["sourceMode"], "live")

    def test_accounts_listing_reports_coverage(self):
        result = self.scope.list_accounts()
        self.assertEqual(len(result["accounts"]), 1)
        account = result["accounts"][0]
        self.assertEqual((account["accountId"], account["label"]), ("id-A", "甲"))
        self.assertEqual(account["coverage"]["messages"], 12)  # 两个联系人的完整账号覆盖
        self.assertEqual(account["scope"], "installation")

    def test_memories_are_paged_filtered_and_versioned(self):
        page = self.scope.list_memories("id-A", "好友1", status="active", limit=1, offset=0)
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["memoryVersion"], 1)
        self.assertEqual(page["status"], "active")
        self.assertEqual(page["sourceMode"], "cached")
        confirmed = self.scope.list_memories("id-A", "好友1", status="confirmed", limit=50, offset=0)
        self.assertEqual([item["id"] for item in confirmed["items"]], [self.data.confirmed_id])
        self.assertEqual(confirmed["items"][0]["version"], 1)
        observed = self.scope.list_memories("id-A", "好友1", status="user_observed", limit=50, offset=0)
        self.assertEqual([item["id"] for item in observed["items"]], [self.data.confirmed_id])
        empty = self.scope.list_memories("id-A", "陌生人", status="active", limit=50, offset=0)
        self.assertEqual(empty["items"], [])  # 未授权联系人按其自身范围如实为空

    def test_memory_evidence_reports_status_and_window(self):
        result = self.scope.memory_evidence("id-A", "好友1", "mem-p1")
        self.assertEqual(result["memory"]["id"], "mem-p1")
        self.assertEqual(result["memory"]["status"], "pending")
        self.assertEqual(result["versions"], [])
        evidence = result["evidence"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["status"], "ok")  # 摘要与投影逐字一致
        self.assertEqual(evidence[0]["excerpt"], "合成摘录")
        self.assertEqual([message["id"] for message in evidence[0]["messages"]][:2], ["m0", "m1"])
        self.assertTrue(any(message["target"] for message in evidence[0]["messages"]))
        single = self.scope.memory_evidence("id-A", "好友1", "mem-p1", ordinal=0)
        self.assertEqual(single["evidence"][0]["ordinal"], 0)
        with self.assertRaises(McpError) as caught:
            self.scope.memory_evidence("id-A", "好友1", "mem-p1", ordinal=9)
        self.assertEqual(caught.exception.code, "not-found")
        with self.assertRaises(McpError) as caught:
            self.scope.memory_evidence("id-A", "好友1", "mem-缺失")
        self.assertEqual(caught.exception.code, "not-found")

    def test_memory_evidence_flags_changed_projection_honestly(self):
        # 改写被引用消息后，只读证据核对必须报告 changed，而不是继续声称 ok。
        connection = sqlite3.connect(self.data.database.path)
        try:
            connection.execute("UPDATE context_messages SET text=? WHERE msg_id='m1'", ("被改动的合成消息",))
            connection.commit()
        finally:
            connection.close()
        result = self.scope.memory_evidence("id-A", "好友1", "mem-p1")
        self.assertEqual(result["evidence"][0]["status"], "changed")

    def test_media_reports_offline_missing_honestly(self):
        result = self.scope.media("id-A", "好友1", "mv1")
        self.assertEqual((result["available"], result["reason"]), (False, "offline"))
        self.assertEqual(result["metadata"]["kind"], "image")
        self.assertEqual(result["sourceMode"], "cached")
        voice = self.scope.media("id-A", "好友1", "vs1")
        self.assertEqual(voice["metadata"]["kind"], "voice")
        self.assertEqual(voice["metadata"]["transcript"], "合成转写文本")
        self.assertEqual((voice["available"], voice["reason"]), (False, "kind-not-inline"))
        for message_id in ("m0", "不存在"):
            with self.assertRaises(McpError) as caught:
                self.scope.media("id-A", "好友1", message_id)
            self.assertEqual(caught.exception.code, "not-found")

    def test_live_media_only_via_the_controlled_reader(self):
        class FakeMediaReader:
            def __init__(self, behavior="ok"):
                self.behavior = behavior
                self.calls = []

            def media(self, account, user, message_id, frames=1):
                self.calls.append((account, user, message_id, frames))
                if self.behavior == "ok":
                    return {"kind": "image", "sampling": "first-frame",
                            "items": [{"mime": "image/jpeg", "data": "0011"}]}
                if self.behavior == "not-current":
                    return {"error": "not-current"}
                raise RuntimeError("reader exploded")

        # 离线：受控读取器绝不被触碰。
        offline_reader = FakeMediaReader()
        offline = SavedAccountScope(self.friends, self.registry_path(), live_reader=offline_reader)
        result = offline.media("id-A", "好友1", "mv1")
        self.assertEqual((result["available"], result["reason"]), (False, "offline"))
        self.assertEqual(offline_reader.calls, [])
        # 在线且账号一致：经有界受控读取器返回静帧。
        data, friends = make_friends(source_behavior="live")
        self.addCleanup(data.close)
        reader = FakeMediaReader()
        live = SavedAccountScope(friends, self.registry_path(data.root.name), live_reader=reader)
        result = live.media("id-A", "好友1", "mv1", frames=3)
        self.assertEqual(reader.calls, [("账号甲", "好友1", "mv1", 3)])
        self.assertEqual((result["available"], result["reason"]), (True, None))
        self.assertEqual(result["items"], [{"mime": "image/jpeg", "data": "0011"}])
        self.assertEqual(result["sampling"], "first-frame")
        self.assertEqual(result["sourceMode"], "live")
        # 读取失败或消息不在当前窗口：如实报告，绝不假装可用。
        failing = SavedAccountScope(friends, self.registry_path(data.root.name), live_reader=FakeMediaReader("boom"))
        self.assertEqual((failing.media("id-A", "好友1", "mv1")["available"],
                          failing.media("id-A", "好友1", "mv1")["reason"]), (False, "unavailable"))
        window = SavedAccountScope(friends, self.registry_path(data.root.name),
                                   live_reader=FakeMediaReader("not-current"))
        self.assertEqual((window.media("id-A", "好友1", "mv1")["available"],
                          window.media("id-A", "好友1", "mv1")["reason"]),
                         (False, "not-in-current-window"))

    def test_live_claims_require_the_current_wechat_account_to_match(self):
        # 微信在跑但登录的是另一个账号：读取的是甲的缓存，绝不冒充 live。
        data, friends = make_friends(source_behavior="other-account")
        self.addCleanup(data.close)
        scope = SavedAccountScope(friends, self.registry_path(data.root.name))
        search = scope.search_messages("id-A", "好友1", query="合成消息", limit=5, offset=0)
        context = scope.message_context("id-A", "好友1", "m3")
        memories = scope.list_memories("id-A", "好友1", status="active", limit=5, offset=0)
        media = scope.media("id-A", "好友1", "mv1")
        summary = scope.list_accounts()
        for result in (search, context, memories, media):
            self.assertEqual(result["sourceMode"], "cached")
        self.assertEqual((media["available"], media["reason"]), (False, "offline"))
        self.assertEqual(summary["accounts"][0]["sourceMode"], "cached")

    def test_per_contact_summary_excludes_unauthorized_contacts(self):
        result = self.scope.list_accounts({"id-A": ["好友1"]})
        account = result["accounts"][0]
        self.assertEqual(result["scope"], "authorized_contacts")
        self.assertEqual(account["scope"], "authorized_contacts")
        # 只按授权联系人重算：好友2 的 4 条消息与联系人计数都不出现。
        self.assertEqual(account["coverage"], {"contacts": 1, "readContacts": 1, "messages": 8})
        # 未授权联系人的名单本身不产生任何账号条目。
        self.assertEqual(self.scope.list_accounts({"id-A": ["好友2"]})["accounts"][0]["coverage"],
                         {"contacts": 1, "readContacts": 1, "messages": 4})
        self.assertEqual(self.scope.list_accounts({})["accounts"], [])


if __name__ == "__main__":
    unittest.main()
