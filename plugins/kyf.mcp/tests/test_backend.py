"""The kyf.mcp backend forwards named page requests to Core's settings channel.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Synthetic only: the Core client is a recorder, so nothing here touches a real
Core, WeChat or the authorization database.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from kyf_plugin import CoreError, RpcError
from mcp_admin import McpAdmin

FINGERPRINT = "synthetic-install-fingerprint"


def admin(routes=None):
    """An admin over a scripted Core client; ``routes`` maps ``(method, path)`` to a reply."""
    replies = routes or {}
    core = SimpleNamespace(
        get=Mock(side_effect=lambda path, **query: replies[("GET", path)](query) if callable(
            replies.get(("GET", path))) else replies.get(("GET", path), {})),
        post=Mock(side_effect=lambda path, body=None: replies[("POST", path)](body) if callable(
            replies.get(("POST", path))) else replies.get(("POST", path), {})))
    return McpAdmin(core), core


class ForwardingTests(unittest.TestCase):
    def test_reads_forward_to_their_named_routes(self):
        service, core = admin({("GET", "mcp/status"): {"coreReady": True},
                               ("GET", "mcp/profiles"): {"profiles": []},
                               ("GET", "mcp/directory"): {"accounts": []},
                               ("GET", "mcp/calls"): {"calls": [{"action": "zhiji_ping"}]}})
        self.assertEqual(service.status({})["coreReady"], True)
        self.assertEqual(service.profiles({}), {"profiles": []})
        self.assertEqual(service.directory({}), {"accounts": []})
        self.assertEqual(service.calls({})["calls"][0]["action"], "zhiji_ping")
        self.assertEqual(service.calls({"limit": 10})["calls"][0]["action"], "zhiji_ping")
        core.get.assert_any_call("mcp/calls", limit=50)
        core.get.assert_any_call("mcp/calls", limit=10)

    def test_profile_detail_and_writes_carry_the_client_id(self):
        service, core = admin({("GET", f"mcp/profiles/{'mcp_x' * 2}"): {"sourceFingerprint": FINGERPRINT}})
        service.profile({"clientId": "mcp_x" * 2})
        core.get.assert_called_once_with("mcp/profiles/" + "mcp_x" * 2)
        service.credential({"clientId": "c1"})
        core.post.assert_called_with("mcp/profiles/c1/credential", {})
        service.set_enabled({"clientId": "c1", "enabled": False})
        core.post.assert_called_with("mcp/profiles/c1/enabled", {"enabled": False})
        service.set_mode({"clientId": "c1", "mode": "per_contact"})
        core.post.assert_called_with("mcp/profiles/c1/mode", {"mode": "per_contact"})
        service.set_global_management({"clientId": "c1", "enabled": True})
        core.post.assert_called_with("mcp/profiles/c1/global-management", {"enabled": True})
        service.revoke({"clientId": "c1"})
        core.post.assert_called_with("mcp/profiles/c1/revoke", {})
        service.set_master({"enabled": False})
        core.post.assert_called_with("mcp/master", {"enabled": False})
        service.disconnect_all({})
        core.post.assert_called_with("mcp/connections/disconnect", {})

    def test_create_and_contact_requests_are_validated_before_forwarding(self):
        service, core = admin()
        service.create_profile({"name": "桌面客户端", "mode": "per_contact", "globalManagement": False})
        core.post.assert_called_with("mcp/profiles", {"name": "桌面客户端", "mode": "per_contact",
                                                      "globalManagement": False})
        grant = {"clientId": "c1", "sourceFingerprint": FINGERPRINT, "accountId": "账号A",
                 "subjectType": "wechat_contact", "subjectId": "好友1"}
        service.grant_contact(grant)
        core.post.assert_called_with("mcp/contacts", grant)
        service.revoke_contact(grant)
        core.post.assert_called_with("mcp/contacts/revoke", grant)
        # The page never assembles a fingerprint, and groups are never a subject type.
        for broken in ({**grant, "sourceFingerprint": ""},
                       {**grant, "subjectType": "group"}, {**grant, "clientId": ""}):
            with self.assertRaises(RpcError):
                service.grant_contact(broken)
        core.post.assert_called_with("mcp/contacts/revoke", grant)

    def test_core_errors_reach_the_page_with_their_code(self):
        service, core = admin()
        core.post.side_effect = CoreError(403, "mcp-admin-forbidden", "MCP 授权只能由本机设置界面管理")
        with self.assertRaises(RpcError) as caught:
            service.revoke({"clientId": "c1"})
        self.assertEqual(caught.exception.code, "mcp-admin-forbidden")

    def test_settings_preselect_defaults_to_full_on_unexpected_shapes(self):
        service, core = admin({("GET", "settings"): {"values": {"defaultMode": "per_contact"}}})
        self.assertEqual(service.settings({}), {"defaultMode": "per_contact"})
        core.get.assert_called_once_with("settings")
        for reply in (None, {}, {"values": {}}, {"values": {"defaultMode": "everything"}}):
            service, core = admin({("GET", "settings"): reply})
            self.assertEqual(service.settings({})["defaultMode"], "full")

    def test_invalid_shapes_never_reach_core(self):
        service, core = admin()
        for method, params in (("profile", {"clientId": ""}), ("create_profile", {"name": 1, "mode": "full"}),
                               ("create_profile", {"name": "x", "mode": "everything"}),
                               ("set_enabled", {"clientId": "c1", "enabled": 1}),
                               ("set_master", {"enabled": "yes"}), ("calls", {"limit": 0})):
            with self.assertRaises(RpcError):
                getattr(service, method)(params)
        core.get.assert_not_called()
        core.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
