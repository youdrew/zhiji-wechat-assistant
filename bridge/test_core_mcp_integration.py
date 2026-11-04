"""G11/G13 integration on synthetic data: the proxy's zhiji_poll_events / zhiji_open_window
and their Core wiring (event store + outlet, UI window service, version listener)."""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_leases import LeaseService
from core_mcp_events import McpEventOutlet, McpEventStore
from core_mcp_proxy import REGISTRY, McpError
from core_ui_host import UiWindowService, window_key
from core_ui_modules import UiError
from test_core_mcp import FINGERPRINT, headers, make_services
from test_core_ui_host import FakeHost, answering

CONTACT_A = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友1"}]
CONTACT_B = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友2"}]


def message_row(seq, subject, account_id="id-A", fingerprint=FINGERPRINT):
    return {"key": f"k{seq:04d}-{subject}", "type": "message.new", "account": "wxid_" + account_id,
            "accountId": account_id, "subjectId": subject, "isGroup": False, "newCount": 2, "countExact": True,
            "firstTime": 1_800_000_000_000, "lastTime": 1_800_000_000_000, "observedAt": int(time.time() * 1000),
            "fingerprint": fingerprint}


class Harness(unittest.TestCase):
    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.store = self.mcp.store
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.events = McpEventStore(Path(directory.name) / "events.sqlite3")
        self.addCleanup(self.events.close)
        self.outlet = McpEventOutlet(lambda: None, self.events, Path(directory.name) / "accounts.json",
                                     self.mcp.fingerprint, self.store.cursor_key, lambda: True)
        self.addCleanup(self.outlet.close)

    def pair(self, mode="per_contact", grants=(), **capabilities):
        created = self.store.create_profile("合成客户端", mode, **capabilities)
        for account_id, subject_id in grants:
            self.store.grant_contact(created["clientId"], FINGERPRINT, account_id, "wechat_contact", subject_id)
        return self.connect(created), created

    def connect(self, created):
        token = self.mcp.handshake({"clientId": created["clientId"], "credential": created["credential"]})
        return self.mcp.authenticate(headers(token["token"]))

    def poll(self, principal, **params):
        return self.mcp.call(principal, {"action": "zhiji_poll_events", "params": params})

    def refused(self, principal, body):
        with self.assertRaises(McpError) as caught:
            self.mcp.call(principal, body)
        return caught.exception

    def logged(self, action):
        return [row for row in self.store.recent_calls(500) if row["action"] == action]


class PollEventsTests(Harness):
    def setUp(self):
        super().setUp()
        self.mcp.events = self.outlet

    def test_ping_lists_both_tools_only_when_wired(self):
        principal, _created = self.pair("full")
        self.mcp.ui = SimpleNamespace(open=lambda *args, **kwargs: {})
        tools = self.mcp.call(principal, {"action": "zhiji_ping", "params": {}})["server"]["openCapabilities"]
        self.assertIn("zhiji_poll_events", tools)
        self.assertIn("zhiji_open_window", tools)
        self.mcp.events = self.mcp.ui = None
        tools = self.mcp.call(principal, {"action": "zhiji_ping", "params": {}})["server"]["openCapabilities"]
        self.assertNotIn("zhiji_poll_events", tools)
        self.assertNotIn("zhiji_open_window", tools)
        error = self.refused(principal, {"action": "zhiji_poll_events", "params": {}})
        self.assertEqual((error.status, error.code), (501, "capability-not-open"))

    def test_registry_contract(self):
        entry = REGISTRY["zhiji_poll_events"]
        self.assertEqual((entry["capability"], entry["subjects"], entry["aggregate"], entry["recomputable"],
                          entry["quiet"]), ("read", False, True, True, True))
        self.assertEqual(REGISTRY["zhiji_open_window"]["capability"], "read")

    def test_events_are_filtered_by_the_callers_scope(self):
        principal, _created = self.pair(grants=[("id-A", "好友1")])
        cursor = self.poll(principal)["cursor"]
        self.events.add_events([message_row(1, "好友1"), message_row(2, "好友2"),
                                message_row(3, "好友1", account_id="id-B")], int(time.time() * 1000))
        result = self.poll(principal, cursor=cursor)
        self.assertEqual([event["subject"]["subjectId"] for event in result["events"]], ["好友1"])
        self.assertEqual(result["events"][0]["accountId"], "id-A")
        self.assertEqual(result["requestScope"], "authorized_contacts")
        # Metadata only: no names or text ever come back.
        self.assertNotIn("text", json.dumps(result["events"]))

    def test_the_fingerprint_is_always_the_server_side_one(self):
        principal, _created = self.pair("full")
        seen = []
        real_read = self.outlet.read

        def read(info, scope, **kwargs):
            seen.append(dict(info))
            return real_read(info, scope, **kwargs)

        self.outlet.read = read
        self.poll(principal)
        self.assertEqual(seen[-1], {"clientId": principal["clientId"], "authVersion": principal["authVersion"],
                                    "fingerprint": FINGERPRINT})
        # A request can never name a fingerprint (or any other identity) of its own.
        error = self.refused(principal, {"action": "zhiji_poll_events", "params": {"fingerprint": "other-root"}})
        self.assertEqual((error.status, error.code), (400, "invalid-request"))
        # Rows recorded under another data root stay invisible.
        cursor = self.poll(principal)["cursor"]
        self.events.add_events([message_row(9, "好友1", fingerprint="other-root")], int(time.time() * 1000))
        self.assertEqual(self.poll(principal, cursor=cursor)["events"], [])

    def test_only_polls_with_events_and_failures_are_logged(self):
        principal, _created = self.pair("full")
        cursor = self.poll(principal)["cursor"]
        cursor = self.poll(principal, cursor=cursor, waitSeconds=0)["cursor"]
        self.assertEqual(self.logged("zhiji_poll_events"), [])
        self.events.add_events([message_row(1, "好友1")], int(time.time() * 1000))
        self.assertEqual(len(self.poll(principal, cursor=cursor)["events"]), 1)
        self.assertEqual([row["resultCode"] for row in self.logged("zhiji_poll_events")], ["ok"])
        self.refused(principal, {"action": "zhiji_poll_events", "params": {"waitSeconds": 99}})
        self.assertEqual([row["resultCode"] for row in self.logged("zhiji_poll_events")],
                         ["invalid-params", "ok"])

    def test_error_mapping(self):
        principal, created = self.pair("full")
        error = self.refused(principal, {"action": "zhiji_poll_events", "params": {"waitSeconds": 99}})
        self.assertEqual((error.status, error.code), (400, "invalid-params"))
        error = self.refused(principal, {"action": "zhiji_poll_events", "params": {"cursor": "bogus"}})
        self.assertEqual((error.status, error.code), (409, "cursor-invalid"))
        error = self.refused(principal, {"action": "zhiji_poll_events", "params": {}, "subjects": CONTACT_A})
        self.assertEqual((error.status, error.code), (400, "invalid-request"))
        cursor = self.poll(principal)["cursor"]
        self.store.set_mode(created["clientId"], "accounts")  # bumps the authorization version
        stale = self.refused(principal, {"action": "zhiji_poll_events", "params": {"cursor": cursor}})
        self.assertEqual(stale.code, "stale-authorization")
        fresh = self.connect(created)
        error = self.refused(fresh, {"action": "zhiji_poll_events", "params": {"cursor": cursor}})
        self.assertEqual((error.status, error.code), (409, "cursor-stale"))

    def test_long_polls_hold_no_lock_and_wake_together(self):
        first, _a = self.pair("full")
        second, _b = self.pair("full")
        cursors = {key: self.poll(principal)["cursor"] for key, principal in (("a", first), ("b", second))}
        results, started = {}, time.monotonic()

        def wait(key, principal):
            results[key] = (self.poll(principal, cursor=cursors[key], waitSeconds=10), time.monotonic())

        threads = [threading.Thread(target=wait, args=item) for item in (("a", first), ("b", second))]
        for thread in threads:
            thread.start()
        time.sleep(0.2)
        # While both long-polls wait, other calls (and the call log) proceed at once.
        before = time.monotonic()
        self.mcp.call(first, {"action": "zhiji_ping", "params": {}})
        self.assertLess(time.monotonic() - before, 1.0)
        self.events.add_events([message_row(1, "好友1")], int(time.time() * 1000))
        with self.outlet.condition:
            self.outlet.condition.notify_all()
        for thread in threads:
            thread.join(timeout=10)
        for key in ("a", "b"):
            self.assertEqual(len(results[key][0]["events"]), 1)
            self.assertLess(results[key][1] - started, 5.0)

    def test_activity_hook(self):
        clock = [1000.0]
        self.mcp.monotonic = lambda: clock[0]
        self.assertFalse(self.mcp.recently_active())
        principal, _created = self.pair("full")
        self.assertTrue(self.mcp.recently_active())
        clock[0] += 31 * 60
        self.assertFalse(self.mcp.recently_active())
        self.mcp.call(principal, {"action": "zhiji_ping", "params": {}})
        self.assertTrue(self.mcp.recently_active())


class FakeUi:
    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = [], result, error

    def open(self, principal_info, scope, module, account_id=None, subject_id=None):
        self.calls.append((dict(principal_info), module, account_id, subject_id))
        if self.error is not None:
            raise self.error
        return dict(self.result or {"status": "opened", "module": module, "windowKey": "k"})


class OpenWindowTests(Harness):
    def open(self, principal, module, subjects=None, account=None):
        body = {"action": "zhiji_open_window", "params": {"module": module}}
        if subjects is not None:
            body["subjects"] = subjects
        if account is not None:
            body["account"] = account
        return self.mcp.call(principal, body)

    def test_contact_module_reaches_ui_with_server_identity(self):
        ui = self.mcp.ui = FakeUi()
        principal, _created = self.pair(grants=[("id-A", "好友1")])
        result = self.open(principal, "contact.persona", CONTACT_A, account="id-A")
        self.assertEqual(result["status"], "opened")
        self.assertEqual(ui.calls, [({"clientId": principal["clientId"], "authVersion": principal["authVersion"],
                                      "fingerprint": FINGERPRINT}, "contact.persona", "id-A", "好友1")])

    def test_unauthorized_contact_is_refused_before_the_ui(self):
        ui = self.mcp.ui = FakeUi()
        principal, _created = self.pair(grants=[("id-A", "好友1")])
        error = self.refused(principal, {"action": "zhiji_open_window", "params": {"module": "contact.overview"},
                                         "subjects": CONTACT_B})
        self.assertEqual((error.status, error.code), (403, "contact-not-authorized"))
        self.assertEqual(ui.calls, [])

    def test_ui_errors_map_to_their_status(self):
        principal, _created = self.pair("full")
        for code, status in (("module-unknown", 404), ("module-not-openable", 403), ("context-invalid", 400),
                             ("contact-not-authorized", 403), ("account-unknown", 404)):
            with self.subTest(code=code):
                self.mcp.ui = FakeUi(error=UiError(code))
                error = self.refused(principal, {"action": "zhiji_open_window", "params": {"module": "app.main"}})
                self.assertEqual((error.status, error.code), (status, code))

    def test_unavailable_and_failed_pass_through(self):
        principal, _created = self.pair("per_contact")
        self.mcp.ui = FakeUi({"status": "unavailable", "module": "app.main", "windowKey": "main",
                              "reason": "runtime-missing"})
        result = self.open(principal, "app.main")
        self.assertEqual((result["status"], result["reason"]), ("unavailable", "runtime-missing"))

    def test_disabled_profile_never_reaches_the_ui(self):
        ui = self.mcp.ui = FakeUi()
        principal, created = self.pair("full")
        self.store.set_enabled(created["clientId"], False)
        error = self.refused(principal, {"action": "zhiji_open_window", "params": {"module": "app.main"}})
        self.assertEqual(error.code, "client-suspended")
        self.assertEqual(ui.calls, [])


class OpenWindowEndToEndTests(Harness):
    """Real UiWindowService behind the proxy: scope from readable_scope, revocation closes."""

    def setUp(self):
        super().setUp()
        self.hosts = []

        def launcher(command):
            host = FakeHost(answering)
            self.hosts.append(host)
            return host

        def current_auth(client_id):
            profile = self.store.client(client_id)
            if profile is None or not profile["enabled"]:
                return None
            return {"authVersion": profile["auth_version"], "fingerprint": self.mcp.fingerprint() or ""}

        self.ui = UiWindowService(
            launcher=launcher, desktop_probe=lambda: True,
            registry_lookup=lambda account_id: {"account": "wxid_self"} if account_id == "id-A" else None,
            scope_for=self.mcp.readable_scope, plugin_enabled=lambda plugin, account=None: True,
            live_account=lambda: "wxid_self", leases=LeaseService(idle_seconds=0), current_auth=current_auth,
            client_url="http://127.0.0.1:5000/", electron=lambda: "electron", min_interval=0,
            ready_timeout=2, reply_timeout=2)
        self.addCleanup(self.ui.shutdown)
        self.mcp.ui = self.ui
        self.store.add_version_listener(self.ui.on_version_change)

    def test_open_reject_and_revoke_closes(self):
        principal, created = self.pair(grants=[("id-A", "好友1")])
        body = {"action": "zhiji_open_window", "params": {"module": "contact.overview"}, "subjects": CONTACT_A}
        result = self.mcp.call(principal, body)
        self.assertEqual(result["status"], "opened")
        key = window_key(created["clientId"], "friend", "id-A", "好友1")
        self.assertEqual(result["windowKey"], key)
        error = self.refused(principal, {**body, "subjects": CONTACT_B})
        self.assertEqual(error.code, "contact-not-authorized")
        self.store.revoke_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        self.assertIn({"op": "close", "key": key}, self.hosts[-1].received)
        self.assertEqual(self.ui.windows(created["clientId"]), [])

    def test_master_switch_off_closes_every_agent_window(self):
        from core_mcp_admin import _master
        principal, created = self.pair(grants=[("id-A", "好友1")])
        body = {"action": "zhiji_open_window", "params": {"module": "contact.overview"}, "subjects": CONTACT_A}
        key = self.mcp.call(principal, body)["windowKey"]
        _master(self.mcp, {"enabled": True})
        self.assertEqual(len(self.ui.windows(created["clientId"])), 1)
        _master(self.mcp, {"enabled": False})
        self.assertIn({"op": "close", "key": key}, self.hosts[-1].received)
        self.assertEqual(self.ui.windows(created["clientId"]), [])


class CoreWiringTests(unittest.TestCase):
    """CoreServices creates the event store + outlet and the window service and follows MCP changes."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        from core_services import CoreServices
        root = Path(__file__).resolve().parents[1]
        self.core = CoreServices(SimpleNamespace(source=None, closing=False), root, "http://127.0.0.1:5000",
                                 local=Path(self.directory.name), bundled_dir=root / "no-plugins",
                                 local_dir=Path(self.directory.name) / "none")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(lambda: self.core is not None and self.core.close())

    def test_wiring(self):
        core = self.core
        self.assertTrue((Path(self.directory.name) / "core" / "mcp" / "events.sqlite3").is_file())
        self.assertIs(core.mcp.events, core.mcp_events)
        self.assertIs(core.mcp.ui, core.ui)
        self.assertIsInstance(core.ui, UiWindowService)
        self.assertEqual(core.ui.client_url, "http://127.0.0.1:5000/")
        self.assertIsNotNone(core.mcp_events.thread)
        self.assertTrue(core.mcp_events.thread.is_alive())
        # The outlet watches only while an MCP client was active recently.
        self.assertFalse(core.mcp.recently_active())

    def test_corrupt_event_store_leaves_core_and_pairing_available(self):
        from core_services import CoreServices
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event_file = root / ".local" / "core" / "mcp" / "events.sqlite3"
            event_file.parent.mkdir(parents=True)
            damaged = b"not an event database"
            event_file.write_bytes(damaged)
            with self.assertLogs("core_services", level="WARNING") as captured:
                core = CoreServices(SimpleNamespace(source=None, closing=False), root, "http://127.0.0.1:0")
            try:
                self.assertIn("MCP events unavailable", captured.output[0])
                self.assertIsNone(core.mcp_event_store)
                self.assertIsNone(core.mcp.events)
                core.mcp.plugin_gate = lambda: True
                core.mcp.pairing.presenter = None
                ticket = core.mcp.pairing.request({"name": "合成配对", "client": "generic"})
                self.assertEqual(ticket["state"], "pending")
                core.mcp.pairing.decide(ticket["pairingId"], True)
                delivery = core.mcp.pairing.poll({key: ticket[key] for key in ("pairingId", "pollSecret")})
                session = core.mcp.handshake({key: delivery[key] for key in ("clientId", "credential")})
                principal = core.mcp.authenticate(headers(session["token"]))
                ping = core.mcp.call(principal, {"action": "zhiji_ping", "params": {}})
                self.assertIn("zhiji_open_window", ping["server"]["openCapabilities"])
                self.assertNotIn("zhiji_poll_events", ping["server"]["openCapabilities"])
            finally:
                core.close()
            self.assertEqual(event_file.read_bytes(), damaged)

    def test_clear_account_forgets_events(self):
        store = self.core.mcp_event_store
        store.add_events([message_row(1, "好友1")], int(time.time() * 1000))
        self.assertEqual(len(store.rows_after(0, 10)), 1)
        self.core.mcp_event_store.forget_account("wxid_id-A")  # what clear_account calls
        self.assertEqual(store.rows_after(0, 10), [])
        store.add_events([message_row(2, "好友1")], int(time.time() * 1000))
        self.core.clear_account("wxid_id-A")
        self.assertEqual(store.rows_after(0, 10), [])

    def test_version_listener_forgets_cursors_and_closes_windows(self):
        core = self.core
        closed = []
        core.ui.on_version_change = closed.append
        created = core.mcp.store.create_profile("合成客户端", "full")
        core.mcp_event_store.save_consumer_cursor(created["clientId"], "mcp-adapter", 1, FINGERPRINT, 3, 0)
        self.assertIsNotNone(core.mcp_event_store.consumer_cursor(created["clientId"], "mcp-adapter"))
        core.mcp.store.set_mode(created["clientId"], "accounts")
        self.assertIsNone(core.mcp_event_store.consumer_cursor(created["clientId"], "mcp-adapter"))
        self.assertEqual(closed, [created["clientId"]])

    def test_pairing_service_is_wired(self):
        from core_mcp_pairing import McpPairingService
        from core_mcp_pairing_window import ElectronPairingPresenter
        self.assertIsInstance(self.core.mcp.pairing, McpPairingService)
        self.assertIsInstance(self.core.mcp.pairing.presenter, ElectronPairingPresenter)

    def test_unavailable_plugins_close_their_windows(self):
        closed = []
        self.core.ui.on_plugin_disabled = closed.append
        original_enabled = self.core.registry.enabled
        self.core.registry.enabled = lambda plugin_id: plugin_id == "kyf.mcp" or original_enabled(plugin_id)
        self.core._close_windows_of_unavailable_plugins("kyf.friend-analysis")
        self.assertEqual(closed, ["kyf.friend-analysis"])  # absent from this synthetic registry
        closed.clear()
        self.core.registry.plugins["kyf.friend-analysis"] = object()
        try:
            self.core.registry.enabled = lambda plugin_id: True
            self.core.registry.dependency_error = lambda plugin_id: None
            self.core._close_windows_of_unavailable_plugins("kyf.friend-analysis")
            self.assertEqual(closed, [])  # enabled and complete: windows stay
        finally:
            self.core.registry.plugins.pop("kyf.friend-analysis", None)

    def test_disabling_the_mcp_plugin_closes_every_agent_window(self):
        closed, per_plugin = [], []
        self.core.ui.close_all = lambda: closed.append("all")
        self.core.ui.on_plugin_disabled = per_plugin.append
        enabled = {"kyf.mcp": True}
        self.core.registry.enabled = lambda plugin_id: enabled.get(plugin_id, True)
        self.core.registry.dependency_error = lambda plugin_id: None
        self.core._close_windows_of_unavailable_plugins("kyf.friend-analysis")
        self.assertEqual(closed, [])
        enabled["kyf.mcp"] = False
        self.core._close_windows_of_unavailable_plugins("kyf.mcp")
        self.assertEqual(closed, ["all"])

    def test_close_stops_the_outlet(self):
        thread = self.core.mcp_events.thread
        core, self.core = self.core, None
        core.close()
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
