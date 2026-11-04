"""MCP auth routes on the loopback server, using only a synthetic backend."""

import http.client
import json
import sys
import tempfile
import threading
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_mcp_proxy import McpServices
from plugin_api import PluginAPI
from real_http import CONTROL_TOKEN_HEADER, make_handler

FINGERPRINT = "synthetic-install-fingerprint"


class StubCore:
    def __init__(self, mcp):
        self.mcp = mcp
        self.supervisor = types.SimpleNamespace(plugin_for_token=lambda token: None)
        self.api = PluginAPI(self)


class StubBackend:
    def __init__(self, mcp):
        self._core = StubCore(mcp)

    def core_services(self):
        return self._core


def make_services():
    directory = tempfile.TemporaryDirectory()
    services = McpServices(directory.name, source_fingerprint=lambda: FINGERPRINT)
    services._directory = directory  # keep the temp directory alive
    return services


class McpRouteTests(unittest.TestCase):
    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(StubBackend(self.mcp)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            sent = {"Content-Type": "application/json; charset=utf-8", **(headers or {})}
            connection.request(method, path, body=payload, headers=sent)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def handshake(self, client_id, credential):
        return self.request("POST", "/api/mcp/handshake",
                            {"clientId": client_id, "credential": credential})

    def pair(self, name, mode="per_contact"):
        from test_core_mcp import legacy_capabilities
        created = self.mcp.store.create_profile(name, mode, **legacy_capabilities(mode))
        status, result = self.handshake(created["clientId"], created["credential"])
        self.assertEqual(status, 200)
        return created, result

    def test_community_build_has_no_message_dispatch_routes(self):
        for path in ("/api/control/wechat-send/status", "/api/control/wechat-send/prepare",
                     "/api/control/wechat-sdk/status"):
            with self.subTest(path=path):
                status, result = self.request("POST", path, {})
                self.assertEqual(status, 404)
                self.assertNotIn("result", result)

    def test_handshake_and_ping_roundtrip(self):
        created, token = self.pair("桌面客户端", mode="full")
        status, result = self.request("POST", "/api/mcp/rpc", {"action": "zhiji_ping", "params": {}},
                                      {"Authorization": "Bearer " + token["token"]})
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["result"]["profile"]["clientId"], created["clientId"])
        self.assertEqual(result["result"]["profile"]["mode"], "full")
        self.assertEqual(result["result"]["server"]["openCapabilities"],
                                                 ['zhiji_clear_local_data', 'zhiji_confirm_memory', 'zhiji_create_memory',
                          'zhiji_delete_account', 'zhiji_delete_assistant_skill', 'zhiji_delete_assistant_template',
                          'zhiji_delete_memory', 'zhiji_delete_observation', 'zhiji_edit_self_timeline',
                          'zhiji_enable_assistant_template',
                          'zhiji_find_contacts', 'zhiji_get_assistant_catalog', 'zhiji_get_assistant_run', 'zhiji_get_assistant_thread',
                          'zhiji_get_contact', 'zhiji_get_contact_brief', 'zhiji_get_contact_goal', 'zhiji_get_friend_analysis', 'zhiji_get_import_batch',
                          'zhiji_get_import_messages', 'zhiji_get_media', 'zhiji_get_memory_evidence',
                          'zhiji_get_message_context', 'zhiji_get_message_insights', 'zhiji_get_model_settings',
                          'zhiji_get_plugins', 'zhiji_get_recent_messages', 'zhiji_get_self_analysis', 'zhiji_get_self_profile',
                          'zhiji_get_task', 'zhiji_get_usage',
                          'zhiji_import_assistant_skill', 'zhiji_list_accounts', 'zhiji_list_assistant_friends',
                          'zhiji_list_assistant_threads', 'zhiji_list_import_batches', 'zhiji_list_import_candidates',
                          'zhiji_list_import_contacts', 'zhiji_list_memories', 'zhiji_list_observations',
                          'zhiji_list_recent_conversations', 'zhiji_new_assistant_thread', 'zhiji_ping', 'zhiji_remove_import_material',
                          'zhiji_save_assistant_template', 'zhiji_save_contact_goal', 'zhiji_save_import_batch',
                          'zhiji_save_model_settings', 'zhiji_save_observation', 'zhiji_save_self_profile',
                          'zhiji_search_messages', 'zhiji_set_plugin_enabled',
                          'zhiji_start_friend_analysis', 'zhiji_start_import_recognition',
                          'zhiji_start_message_insights', 'zhiji_start_self_analysis', 'zhiji_stop_assistant_run',
                          'zhiji_stop_import_recognition', 'zhiji_stop_self_analysis', 'zhiji_stop_task',
                          'zhiji_submit_assistant_question', 'zhiji_undo_self_timeline', 'zhiji_update_memory'])

    def test_handshake_needs_the_exact_content_type(self):
        created = self.mcp.store.create_profile("桌面客户端", "full")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            body = json.dumps({"clientId": created["clientId"], "credential": created["credential"]})
            connection.request("POST", "/api/mcp/handshake", body=body,
                               headers={"Content-Type": "text/plain"})
            response = connection.getresponse()
            self.assertEqual(response.status, 415)
        finally:
            connection.close()

    def test_invalid_handshakes_and_routes(self):
        self.assertEqual(self.handshake("mcp_unknown", "zmc_" + "0" * 64)[0], 401)
        self.assertEqual(self.request("POST", "/api/mcp/other", {})[0], 404)
        self.assertEqual(self.request("GET", "/api/mcp/handshake")[0], 405)
        self.assertEqual(self.request("POST", "/api/mcp/rpc", {"action": "zhiji_ping", "params": {}})[0], 401)
        self.assertEqual(self.request("POST", "/api/mcp/rpc", {"action": "zhiji_ping", "params": {}},
                                      {"Authorization": "Bearer zmt_" + "0" * 64})[0], 401)
        oversized = {"action": "zhiji_ping", "params": {"blob": "x" * 300_000}}
        try:
            status, _result = self.request("POST", "/api/mcp/rpc", oversized)
        except (OSError, http.client.HTTPException):
            # The server may abort while the client is still sending the oversized body.
            status, _result = 400, {}
        self.assertEqual(status, 400)

    def test_two_clients_never_borrow_each_others_identity(self):
        first, first_token = self.pair("客户端一", mode="per_contact")
        second, second_token = self.pair("客户端二", mode="full")
        self.assertNotEqual(first_token["token"], second_token["token"])
        for token, created in ((first_token, first), (second_token, second)):
            _status, result = self.request("POST", "/api/mcp/rpc", {"action": "zhiji_ping", "params": {}},
                                           {"Authorization": "Bearer " + token["token"]})
            self.assertEqual(result["result"]["profile"]["clientId"], created["clientId"])
        # The first client's credential cannot pair as the second client.
        self.assertEqual(self.handshake(second["clientId"], first["credential"])[0], 401)
        # The second client's grant must not appear in the first client's summary.
        self.mcp.store.grant_contact(second["clientId"], FINGERPRINT, "账号A", "wechat_contact", "好友1")
        _status, result = self.request("POST", "/api/mcp/rpc", {"action": "zhiji_ping", "params": {}},
                                       {"Authorization": "Bearer " + first_token["token"]})
        self.assertEqual(first["mode"], "per_contact")
        self.assertNotIn("账号A", repr(result))

    def test_other_tokens_and_routes_stay_out_of_reach(self):
        _created, token = self.pair("桌面客户端", mode="full")
        # A desktop control token is not an MCP identity.
        status, _ = self.request("POST", "/api/mcp/rpc", {"action": "zhiji_ping", "params": {}},
                                 {CONTROL_TOKEN_HEADER: "a" * 64})
        self.assertEqual(status, 401)
        # The MCP token is not a plugin token and cannot reach the plugin interface.
        self.assertEqual(self.request("POST", "/plugin-api/v1/scope", {},
                                      {"Authorization": "Bearer " + token["token"]})[0], 401)
        # The MCP token is not the desktop control token either.
        self.assertEqual(self.request("POST", "/api/control/shutdown", None,
                                      {CONTROL_TOKEN_HEADER: token["token"]})[0], 403)
        # Registered skeletons stay unreachable until their capability family lands.
        status, result = self.request("POST", "/api/mcp/rpc",
                                      {"action": "zhiji_find_contacts", "params": {}},
                                      {"Authorization": "Bearer " + token["token"]})
        self.assertEqual(status, 501)
        self.assertEqual(result["error"], "capability-not-open")
        # The offline reads are registered but the synthetic backend carries no
        # saved-account scope, so they degrade to the same stable error.
        status, result = self.request("POST", "/api/mcp/rpc",
                                      {"action": "zhiji_list_accounts", "params": {}},
                                      {"Authorization": "Bearer " + token["token"]})
        self.assertEqual(status, 501)
        self.assertEqual(result["error"], "capability-not-open")
        # Fixed exclusions are refused before anything runs, even in full mode.
        status, result = self.request("POST", "/api/mcp/rpc",
                                      {"action": "model.read_key_plaintext", "params": {}},
                                      {"Authorization": "Bearer " + token["token"]})
        self.assertEqual(status, 403)
        self.assertEqual(result["error"], "fixed-exclusion")
        records = self.mcp.store.recent_calls(10)
        self.assertEqual({record["action"] for record in records},
                         {"zhiji_find_contacts", "zhiji_list_accounts", "model.read_key_plaintext"})
        self.assertNotIn("params", repr(records))

    def test_revocation_takes_effect_immediately(self):
        created, token = self.pair("桌面客户端", mode="full")
        self.mcp.store.set_enabled(created["clientId"], False)
        status, result = self.request("POST", "/api/mcp/rpc", {"action": "zhiji_ping", "params": {}},
                                      {"Authorization": "Bearer " + token["token"]})
        self.assertEqual(status, 403)
        self.assertEqual(result["error"], "client-suspended")
        self.mcp.store.revoke_profile(created["clientId"])
        self.assertEqual(self.handshake(created["clientId"], created["credential"])[0], 401)


class LeaseBackend:
    """A backend with the shared-lease service, for the GUI's control routes."""

    def __init__(self):
        from core_leases import LeaseService
        self.leases = LeaseService(idle_seconds=300)
        self._core = None

    def background_status(self):
        return {"mcpConnections": 0, "guiLeases": self.leases.count("gui"),
                "holders": self.leases.holders()}


class ControlLeaseTests(unittest.TestCase):
    def setUp(self):
        self.backend = LeaseBackend()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend, None, "c" * 64))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server

    def request(self, path, body=None, token="c" * 64):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            payload = json.dumps(body).encode("utf-8") if body is not None else b""
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if token is not None:
                headers[CONTROL_TOKEN_HEADER] = token
            connection.request("POST", path, body=payload, headers=headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_gui_lease_acquires_releases_and_rejects_others(self):
        status, result = self.request("/api/control/lease", {"action": "acquire", "pid": 1234})
        self.assertEqual((status, result["ok"]), (200, True))
        self.assertEqual(self.backend.leases.count("gui"), 1)
        self.assertEqual(self.backend.leases.holders("gui")[0]["pid"], 1234)
        self.assertEqual(self.request("/api/control/lease", {"action": "release"})[1]["ok"], True)
        self.assertEqual(self.backend.leases.count("gui"), 0)
        # Without the desktop control token the lease is unreachable, like shutdown.
        self.assertEqual(self.request("/api/control/lease", {"action": "acquire"}, token=None)[0], 403)
        self.assertEqual(self.request("/api/control/lease", {"action": "acquire"}, token="d" * 64)[0], 403)
        self.assertEqual(self.request("/api/control/lease", {"action": "everything"})[0], 400)

    def test_background_status_reports_mcp_connections_without_building_core(self):
        status, result = self.request("/api/control/background", None)
        self.assertEqual(status, 200)
        self.assertEqual(result["mcpConnections"], 0)
        self.assertEqual(self.request("/api/control/background", {"extra": 1})[0], 400)


class McpPairingRouteTests(McpRouteTests):
    """G12 loopback pairing routes: request/poll/cancel/revoke only, never an approval."""

    def setUp(self):
        super().setUp()
        from core_mcp_pairing import McpPairingService
        self.mcp.pairing = McpPairingService(self.mcp, presenter=None)

    def test_request_poll_cancel_roundtrip(self):
        status, started = self.request("POST", "/api/mcp/pairing/request", {"name": "Claude Code", "client": "claude-code"})
        self.assertEqual(status, 200)
        self.assertEqual(started["state"], "pending")
        owned = {"pairingId": started["pairingId"], "pollSecret": started["pollSecret"]}
        status, polled = self.request("POST", "/api/mcp/pairing/poll", owned)
        self.assertEqual((status, polled["state"]), (200, "pending"))
        self.assertNotIn("credential", polled)
        status, wrong = self.request("POST", "/api/mcp/pairing/poll", {**owned, "pollSecret": "zps_x"})
        self.assertEqual((status, wrong["error"]), (403, "pairing-poll-invalid"))
        status, cancelled = self.request("POST", "/api/mcp/pairing/cancel", owned)
        self.assertEqual((status, cancelled["state"]), (200, "cancelled"))

    def test_disabling_mcp_blocks_pairing_approval_and_credential_delivery(self):
        from core_mcp_proxy import McpError
        for gate, code in (("master", "mcp-disabled"), ("plugin", "mcp-plugin-disabled")):
            with self.subTest(gate=gate):
                status, approved = self.request("POST", "/api/mcp/pairing/request",
                                                {"name": "approved", "client": "generic"})
                self.assertEqual(status, 200)
                self.mcp.pairing.decide(approved["pairingId"], True)
                status, pending = self.request("POST", "/api/mcp/pairing/request",
                                               {"name": "pending", "client": "generic"})
                self.assertEqual(status, 200)
                if gate == "master":
                    self.mcp.store.set_master_enabled(False)
                else:
                    self.mcp.plugin_gate = lambda: False
                try:
                    with self.assertRaises(McpError) as caught:
                        self.mcp.pairing.decide(pending["pairingId"], True)
                    self.assertEqual(caught.exception.code, code)
                    for ticket in (approved, pending):
                        owned = {key: ticket[key] for key in ("pairingId", "pollSecret")}
                        status, body = self.request("POST", "/api/mcp/pairing/poll", owned)
                        self.assertEqual((status, body["error"]), (403, code))
                        self.assertNotIn("credential", body)
                    self.assertEqual(self.mcp.store.list_profiles(), [])
                finally:
                    self.mcp.store.set_master_enabled(True)
                    self.mcp.plugin_gate = None

    def test_loopback_cannot_approve_and_shapes_are_checked(self):
        status, started = self.request("POST", "/api/mcp/pairing/request", {"name": "合成", "client": "generic"})
        owned = {"pairingId": started["pairingId"], "pollSecret": started["pollSecret"]}
        for route in ("decide", "approve", "request/extra"):
            status, body = self.request("POST", "/api/mcp/pairing/" + route,
                                        {**owned, "approve": True, "accounts": ["external-chat"]})
            self.assertEqual((status, body["error"]), (404, "not-found"), route)
        self.assertEqual(self.request("POST", "/api/mcp/pairing/poll", owned)[1]["state"], "pending")
        self.assertEqual(self.request("GET", "/api/mcp/pairing/poll")[0], 405)
        status, body = self.request("POST", "/api/mcp/pairing/request", {"name": "x"})
        self.assertEqual((status, body["error"]), (400, "invalid-request"))
        # An MCP connection token grants nothing on these routes either.
        created, token = self.pair("已配对客户端", mode="full")
        status, body = self.request("POST", "/api/mcp/pairing/decide", owned,
                                    headers={"Authorization": "Bearer " + token["token"]})
        self.assertEqual(status, 404)
        # Without the service the routes report unavailable instead of falling through.
        self.mcp.pairing = None
        status, body = self.request("POST", "/api/mcp/pairing/poll", owned)
        self.assertEqual((status, body["error"]), (503, "pairing-unavailable"))


if __name__ == "__main__":
    unittest.main()
