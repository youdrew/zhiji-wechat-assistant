"""User-confirmed CLI pairing (G12): approval channels, one-time delivery, expiry and repair.

Synthetic only: no Electron is launched; the presenter seam returns scripted decisions
and the window presenter reads fake protocol lines.
"""

import json
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_leases import LeaseService
from core_mcp_admin import McpAdminError, handle_admin
from core_mcp_pairing import (DELIVERY_TTL_SECONDS, PAIRING_PREFIX, PAIRING_TTL_SECONDS, McpPairingService,
                              PairingUnavailable, handle_pairing, handle_pairing_path)
from core_mcp_pairing_window import PAIRING_LEASE_KIND, ElectronPairingPresenter
from core_mcp_proxy import EXTERNAL_ACCOUNT, McpError, McpServices

FINGERPRINT = "synthetic-install-fingerprint"
ACCOUNTS = [{"accountId": "acct-a", "label": "合成账号甲", "contacts": []},
            {"accountId": "acct-b", "label": "合成账号乙", "contacts": []}]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ScriptedPresenter:
    """Records payloads; returns the scripted decision (or raises) after reporting shown."""

    def __init__(self, decision=None, *, unavailable=False):
        self.decision, self.unavailable, self.payloads = decision, unavailable, []

    def __call__(self, payload, on_shown):
        self.payloads.append(payload)
        if self.unavailable:
            raise PairingUnavailable("no desktop")
        on_shown()
        return self.decision


def make(presenter=None):
    directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    clock = Clock()
    mcp = McpServices(directory.name, source_fingerprint=lambda: FINGERPRINT, monotonic=clock)
    mcp._tmp = directory
    mcp.directory = lambda: {"accounts": ACCOUNTS}
    mcp.pairing = McpPairingService(mcp, presenter, monotonic=clock, spawn=lambda target: target())
    return mcp, clock


def call(mcp, route, body):
    return handle_pairing(mcp, "POST", [route], body)


def request(mcp, name="Claude Code", client="claude-code", **extra):
    return call(mcp, "request", {"name": name, "client": client, **extra})


def poll(mcp, ticket):
    return call(mcp, "poll", {"pairingId": ticket["pairingId"], "pollSecret": ticket["pollSecret"]})


APPROVE_A = {"approved": True, "accounts": ["acct-a"], "capabilities": {}}


class PairingFlowTests(unittest.TestCase):
    def test_window_approval_delivers_the_credential_once_to_the_poll_secret(self):
        mcp, _clock = make(ScriptedPresenter(APPROVE_A))
        ticket = request(mcp)
        self.assertNotIn("credential", ticket)
        delivered = poll(mcp, ticket)
        self.assertEqual(delivered["state"], "approved")
        self.assertTrue(delivered["credential"].startswith("zmc_"))
        self.assertEqual(delivered["mode"], "accounts")
        self.assertEqual(delivered["capabilities"], {"write": False, "send": False, "manage": False})
        self.assertEqual([item["accountId"] for item in delivered["accounts"]], ["acct-a"])
        # The credential works, the grants are exactly the selection, all capabilities off.
        token = mcp.handshake({"clientId": delivered["clientId"], "credential": delivered["credential"]})
        self.assertTrue(token["token"].startswith("zmt_"))
        self.assertEqual([g["accountId"] for g in mcp.store.account_grants(delivered["clientId"])], ["acct-a"])
        row = mcp.store.client(delivered["clientId"])
        self.assertEqual((row["allow_write"], row["allow_send"], row["global_management"]), (0, 0, 0))
        # Second poll: never the credential again.
        again = poll(mcp, ticket)
        self.assertEqual(again["state"], "delivered")
        self.assertNotIn("credential", again)
        self.assertEqual(len(mcp.store.list_profiles()), 1)

    def test_window_payload_lists_accounts_and_external_without_contacts(self):
        presenter = ScriptedPresenter(None)
        mcp, _clock = make(presenter)
        request(mcp)
        accounts = presenter.payloads[0]["accounts"]
        self.assertEqual([item["accountId"] for item in accounts], ["acct-a", "acct-b", EXTERNAL_ACCOUNT])
        self.assertTrue(all(set(item) == {"accountId", "label"} for item in accounts))
        self.assertNotIn("pollSecret", json.dumps(presenter.payloads[0]))

    def test_denied_or_closed_window_never_creates_a_profile(self):
        for decision in ({"approved": False}, None):
            mcp, _clock = make(ScriptedPresenter(decision))
            ticket = request(mcp)
            self.assertEqual(poll(mcp, ticket)["state"], "denied")
            self.assertEqual(mcp.store.list_profiles(), [])

    def test_wrong_secret_and_unknown_ids_are_refused(self):
        mcp, _clock = make(ScriptedPresenter(APPROVE_A))
        ticket = request(mcp)
        with self.assertRaises(McpError) as caught:
            call(mcp, "poll", {"pairingId": ticket["pairingId"], "pollSecret": "zps_" + "0" * 64})
        self.assertEqual(caught.exception.code, "pairing-poll-invalid")
        with self.assertRaises(McpError) as caught:
            call(mcp, "poll", {"pairingId": "zpr_" + "0" * 32, "pollSecret": ticket["pollSecret"]})
        self.assertEqual(caught.exception.code, "pairing-unknown")
        # The wrong secret did not consume the delivery.
        self.assertIn("credential", poll(mcp, ticket))

    def test_pending_request_expires(self):
        mcp, clock = make(ScriptedPresenter(unavailable=True))
        ticket = request(mcp)
        self.assertEqual(poll(mcp, ticket)["state"], "pending")
        clock.now += PAIRING_TTL_SECONDS + 1
        self.assertEqual(poll(mcp, ticket)["state"], "expired")
        with self.assertRaises(McpError) as caught:
            mcp.pairing.decide(ticket["pairingId"], True, ["acct-a"], {})
        self.assertIn(caught.exception.code, ("pairing-expired", "pairing-unknown"))

    def test_approval_not_collected_in_time_expires_without_a_profile(self):
        mcp, clock = make(ScriptedPresenter(APPROVE_A))
        ticket = request(mcp)
        clock.now += DELIVERY_TTL_SECONDS + 1
        self.assertEqual(poll(mcp, ticket)["state"], "expired")
        self.assertEqual(mcp.store.list_profiles(), [])

    def test_cancel_by_the_poll_secret_holder(self):
        mcp, _clock = make(ScriptedPresenter(unavailable=True))
        ticket = request(mcp)
        self.assertEqual(call(mcp, "cancel", dict(pairingId=ticket["pairingId"],
                                                  pollSecret=ticket["pollSecret"]))["state"], "cancelled")
        self.assertEqual(poll(mcp, ticket)["state"], "cancelled")


class NoSelfIssueTests(unittest.TestCase):
    def test_loopback_routes_cannot_approve(self):
        mcp, _clock = make(ScriptedPresenter(unavailable=True))
        ticket = request(mcp)
        for route in ("decide", "approve", "pairing"):
            with self.assertRaises(McpError) as caught:
                handle_pairing(mcp, "POST", [route], {"pairingId": ticket["pairingId"], "approve": True})
            self.assertEqual(caught.exception.code, "not-found")
        with self.assertRaises(McpError):
            handle_pairing(mcp, "GET", ["poll"], {})
        # A request cannot smuggle in its own approval, accounts or capabilities.
        for extra in ({"approve": True}, {"accounts": ["acct-a"]}, {"capabilities": {"send": True}}):
            with self.assertRaises(McpError) as caught:
                request(mcp, **extra)
            self.assertEqual(caught.exception.code, "invalid-request")
        self.assertEqual(poll(mcp, ticket)["state"], "pending")

    def test_an_existing_mcp_credential_grants_nothing(self):
        mcp, _clock = make(ScriptedPresenter(unavailable=True))
        created = mcp.store.create_profile("已有客户端", "full", True, allow_write=True, allow_send=True)
        token = mcp.handshake({"clientId": created["clientId"], "credential": created["credential"]})
        ticket = request(mcp, name="新客户端")
        # Neither the credential nor the MCP token is an accepted field anywhere on the route.
        for body in ({"pairingId": ticket["pairingId"], "pollSecret": created["credential"]},
                     {"pairingId": ticket["pairingId"], "pollSecret": token["token"]}):
            with self.assertRaises(McpError) as caught:
                call(mcp, "poll", body)
            self.assertEqual(caught.exception.code, "pairing-poll-invalid")
        # And the MCP RPC route has no pairing action: the proxy only knows its tools.
        with self.assertRaises(McpError):
            mcp.http_handle("POST", "/api/mcp/pairing", {"Authorization": "Bearer " + token["token"]}, {})
        self.assertEqual(poll(mcp, ticket)["state"], "pending")

    def test_settings_channel_is_the_no_desktop_fallback(self):
        mcp, _clock = make(ScriptedPresenter(unavailable=True))
        ticket = request(mcp)
        self.assertEqual(poll(mcp, ticket)["ui"], "unavailable")
        listed = handle_admin(mcp, "GET", ["pairing"], {}, {})
        self.assertEqual([item["pairingId"] for item in listed["pairings"]], [ticket["pairingId"]])
        self.assertNotIn("pollSecret", json.dumps(listed))
        decided = handle_admin(mcp, "POST", ["pairing", "decide"], {},
                               {"pairingId": ticket["pairingId"], "approve": True, "accounts": ["acct-b"],
                                "capabilities": {"write": True}})
        self.assertEqual(decided["pairing"]["state"], "approved")
        delivered = poll(mcp, ticket)
        self.assertEqual(delivered["capabilities"], {"write": True, "send": False, "manage": False})
        with self.assertRaises(McpAdminError):
            handle_admin(mcp, "POST", ["pairing", "decide"], {}, {"pairingId": ticket["pairingId"], "approve": False})

    def test_selection_is_validated(self):
        mcp, _clock = make(ScriptedPresenter(unavailable=True))
        ticket = request(mcp)
        for accounts, capabilities in ((["acct-x"], {}), (["acct-a", "acct-a"], {}), (["acct-a"], {"admin": True}),
                                       (["acct-a"], {"send": "yes"})):
            with self.assertRaises(McpError):
                mcp.pairing.decide(ticket["pairingId"], True, accounts, capabilities)
        self.assertEqual(poll(mcp, ticket)["state"], "pending")

    def test_master_switch_and_plugin_gate(self):
        mcp, _clock = make(ScriptedPresenter(APPROVE_A))
        mcp.store.set_master_enabled(False)
        with self.assertRaises(McpError) as caught:
            request(mcp)
        self.assertEqual(caught.exception.code, "mcp-disabled")
        mcp.store.set_master_enabled(True)
        mcp.plugin_gate = lambda: False
        with self.assertRaises(McpError) as caught:
            request(mcp)
        self.assertEqual(caught.exception.code, "mcp-plugin-disabled")

    def test_without_service_the_route_is_unavailable(self):
        with self.assertRaises(McpError) as caught:
            handle_pairing(SimpleNamespace(), "POST", ["request"], {})
        self.assertEqual(caught.exception.code, "pairing-unavailable")
        with self.assertRaises(McpError):
            handle_pairing_path(SimpleNamespace(), "POST", "/api/mcp/rpc", {})
        self.assertTrue(PAIRING_PREFIX.startswith("/api/mcp/"))


class RepairTests(unittest.TestCase):
    def test_re_pairing_the_same_name_repairs_in_place(self):
        mcp, _clock = make(ScriptedPresenter({"approved": True, "accounts": ["acct-a", "acct-b"],
                                              "capabilities": {"send": True}}))
        first = poll(mcp, request(mcp))
        mcp.pairing.presenter = ScriptedPresenter(APPROVE_A)
        ticket = request(mcp)
        self.assertTrue(ticket["replaces"])
        second = poll(mcp, ticket)
        self.assertTrue(second["repaired"])
        self.assertEqual(second["clientId"], first["clientId"])
        self.assertEqual(len(mcp.store.list_profiles()), 1)
        # The old credential is dead; permissions follow the new selection, nothing inherited.
        with self.assertRaises(McpError):
            mcp.handshake({"clientId": first["clientId"], "credential": first["credential"]})
        mcp.handshake({"clientId": second["clientId"], "credential": second["credential"]})
        row = mcp.store.client(second["clientId"])
        self.assertEqual(row["allow_send"], 0)
        self.assertEqual([g["accountId"] for g in mcp.store.account_grants(second["clientId"])], ["acct-a"])

    def test_replaces_client_id_targets_a_renamed_or_disabled_profile(self):
        mcp, _clock = make(ScriptedPresenter(APPROVE_A))
        created = mcp.store.create_profile("旧名字", "per_contact")
        mcp.store.set_enabled(created["clientId"], False)
        delivered = poll(mcp, request(mcp, replacesClientId=created["clientId"]))
        self.assertEqual(delivered["clientId"], created["clientId"])
        row = mcp.store.client(created["clientId"])
        self.assertEqual((row["enabled"], row["mode"]), (1, "accounts"))
        # A stale id that no longer exists simply creates a new profile.
        mcp.store.revoke_profile(created["clientId"])
        fresh = poll(mcp, request(mcp, name="另一个", replacesClientId=created["clientId"]))
        self.assertNotEqual(fresh["clientId"], created["clientId"])

    def test_self_revoke_needs_the_credential(self):
        mcp, _clock = make(ScriptedPresenter(APPROVE_A))
        delivered = poll(mcp, request(mcp))
        with self.assertRaises(McpError) as caught:
            call(mcp, "revoke", {"clientId": delivered["clientId"], "credential": "zmc_wrong"})
        self.assertEqual(caught.exception.code, "mcp-unauthorized")
        self.assertEqual(call(mcp, "revoke", {"clientId": delivered["clientId"],
                                              "credential": delivered["credential"]}), {"revoked": True})
        self.assertIsNone(mcp.store.client(delivered["clientId"]))

    def test_model_readiness_is_reported_with_the_delivery(self):
        for connection, expected in (({"mode": "api", "api": {"hasKey": True}}, True),
                                     ({"mode": "none"}, False), (None, None)):
            mcp, _clock = make(ScriptedPresenter(APPROVE_A))
            mcp.manage = SimpleNamespace(model_settings=lambda c=connection: {"connection": c})
            self.assertEqual(poll(mcp, request(mcp))["models"], {"ready": expected})


# -- window presenter protocol ------------------------------------------------------------

class FakeWindow:
    def __init__(self, events):
        self.lines = deque(json.dumps(event).encode("utf-8") + b"\n" for event in events)
        self.killed = self.closed = False

    @property
    def stdout(self):
        return self

    def readline(self):
        return self.lines.popleft() if self.lines else b""

    def poll(self):
        return None if self.lines else 0

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0

    def close(self):
        self.closed = True


def window_payload():
    import time
    return {"pairingId": "zpr_" + "a" * 32, "clientName": "Claude Code", "clientKind": "claude-code",
            "clientLabel": "Claude Code", "replaces": None,
            "accounts": [{"accountId": "acct-a", "label": "甲"}],
            "expiresAtMs": int((time.time() + 60) * 1000)}


class PresenterTests(unittest.TestCase):
    def presenter(self, window, leases=None):
        self.spawned = []

        def launcher(command, line):
            self.spawned.append((command, json.loads(line.decode("utf-8"))))
            return window
        return ElectronPairingPresenter(Path(tempfile.mkdtemp()), leases=leases, electron_path="electron",
                                        script_path="pairing-window.cjs", launcher=launcher)

    def test_decision_line_returns_the_selection_and_reports_shown(self):
        leases = LeaseService()
        shown = []
        window = FakeWindow([{"event": "shown"}, {"event": "decided", "approved": True, "accounts": ["acct-a"],
                                                  "capabilities": {"write": False}, "reason": "accept"}])
        result = self.presenter(window, leases)(window_payload(), lambda: shown.append(True))
        self.assertEqual(result, {"approved": True, "accounts": ["acct-a"], "capabilities": {"write": False}})
        self.assertEqual(shown, [True])
        self.assertEqual(self.spawned[0][0], ["electron", "pairing-window.cjs"])
        self.assertEqual(leases.count(PAIRING_LEASE_KIND), 0)

    def test_refusal_and_malformed_approval_are_not_approvals(self):
        for event in ({"event": "decided", "approved": False},
                      {"event": "decided", "approved": True, "accounts": "acct-a", "capabilities": {}},
                      {"event": "decided", "approved": "yes"}):
            result = self.presenter(FakeWindow([{"event": "shown"}, event]))(window_payload())
            self.assertEqual(result, {"approved": False})

    def test_cannot_show_raises_unavailable_and_exit_after_show_is_none(self):
        with self.assertRaises(PairingUnavailable):
            self.presenter(FakeWindow([{"event": "error", "message": "no desktop"}]))(window_payload())
        with self.assertRaises(PairingUnavailable):
            self.presenter(FakeWindow([]))(window_payload())
        window = FakeWindow([{"event": "shown"}])
        self.assertIsNone(self.presenter(window)(window_payload()))
        self.assertTrue(window.killed)

    def test_missing_runtime_is_unavailable(self):
        presenter = ElectronPairingPresenter(Path(tempfile.mkdtemp()) / "nowhere")
        with self.assertRaises(PairingUnavailable):
            presenter(window_payload())


if __name__ == "__main__":
    unittest.main()
