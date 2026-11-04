"""UiWindowService: opening Agent windows through a fake JSONL UI host (B2)."""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_leases import LeaseService
from core_ui_host import (HOST_LEASE, UiGuardError, UiWindowService, default_launcher, window_key)
from core_ui_modules import UiError

ACCOUNT_ID, ACCOUNT, FRIEND, OTHER = "acc-1", "wxid_self", "wxid_friend", "wxid_other"
FINGERPRINT = "fp-1"


class FakeScope:
    def __init__(self, accounts=None, contacts=None):
        self._accounts, self._contacts = accounts, contacts

    def accounts(self):
        return self._accounts

    def allows(self, account, subject_type, subject_id):
        if subject_type != "wechat_contact":
            return False
        if self._accounts is not None and account not in self._accounts:
            return False
        return self._contacts is None or (account, subject_id) in self._contacts


class FakeStdin:
    def __init__(self, handle):
        self.handle = handle

    def write(self, line):
        if self.handle.closed:
            raise OSError("pipe closed")
        message = json.loads(line.decode("utf-8"))
        self.handle.received.append(message)
        self.handle.behavior(self.handle, message)

    def flush(self):
        pass


class FakeHost:
    """In-memory UI host: stdout is a real pipe, stdin is parsed and answered by ``behavior``."""

    def __init__(self, behavior, start="ready"):
        read, write = os.pipe()
        self.stdout = os.fdopen(read, "rb")
        self._out = os.fdopen(write, "wb", buffering=0)
        self.stdin = FakeStdin(self)
        self.behavior = behavior
        self.received, self.windows = [], set()
        self.closed = self.killed = False
        self.pid = None
        self._lock = threading.Lock()
        if start == "ready":
            self.emit({"event": "ready"})
        elif start == "no-display":
            self.emit({"event": "error", "code": "no-display"})

    def emit(self, event):
        with self._lock:
            if not self.closed:
                self._out.write(json.dumps(event).encode("utf-8") + b"\n")

    def eof(self):
        with self._lock:
            if not self.closed:
                self.closed = True
                self._out.close()

    def poll(self):
        return 0 if self.closed else None

    def kill(self):
        self.killed = True
        self.eof()

    def ops(self, name):
        return [message for message in self.received if message["op"] == name]


def answering(host, message):
    if message["op"] == "open":
        event = "focused" if message["key"] in host.windows else "opened"
        host.windows.add(message["key"])
        host.emit({"event": event, "key": message["key"], "reqId": message["reqId"]})
    elif message["op"] == "close":
        host.windows.discard(message["key"])


def silent(host, message):
    pass


class Harness:
    def __init__(self, test, *, behavior=answering, start="ready", scope=None, enabled=True, live=ACCOUNT,
                 desktop=True, electron="electron", launcher_error=None, shell=None, **options):
        self.hosts, self.now = [], 1000.0
        self.scope = scope or FakeScope()
        self.enabled, self.live, self.desktop = enabled, live, desktop
        self.auth = {"c1": {"authVersion": 1, "fingerprint": FINGERPRINT},
                     "c2": {"authVersion": 1, "fingerprint": FINGERPRINT}}
        self.leases = LeaseService(idle_seconds=0)
        self.tasks = {}

        def launcher(command):
            self.command = command
            if launcher_error:
                raise launcher_error
            host = FakeHost(behavior, start)
            self.hosts.append(host)
            return host

        options.setdefault("ready_timeout", 0.5)
        options.setdefault("reply_timeout", 0.5)
        self.service = UiWindowService(
            launcher=launcher, desktop_probe=lambda: self.desktop,
            registry_lookup=lambda account_id: {"account": ACCOUNT} if account_id == ACCOUNT_ID else None,
            scope_for=lambda principal: self.scope, plugin_enabled=lambda plugin, account: self.enabled,
            live_account=lambda: self.live, leases=self.leases, clock=lambda: self.now,
            current_auth=lambda client_id: self.auth.get(client_id), task_lookup=self.tasks.get,
            client_url="http://127.0.0.1:5000/", electron=lambda: electron, shell_launcher=shell, **options)
        test.addCleanup(self.service.shutdown)

    @property
    def host(self):
        return self.hosts[-1]

    def open(self, module="contact.overview", subject=FRIEND, client="c1", account_id=ACCOUNT_ID, advance=3.0):
        self.now += advance
        principal = {"clientId": client, "authVersion": self.auth[client]["authVersion"], "fingerprint": FINGERPRINT}
        return self.service.open(principal, self.scope, module, account_id, subject)

    def token(self, index=-1):
        return self.host.ops("open")[index]["token"]


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class OpenTests(unittest.TestCase):
    def test_first_open_opens_repeat_focuses_and_sibling_selects_tab(self):
        h = Harness(self)
        first = h.open()
        self.assertEqual(first["status"], "opened")
        self.assertEqual(first["windowKey"], window_key("c1", "friend", ACCOUNT_ID, FRIEND))
        message = h.host.ops("open")[0]
        self.assertTrue(message["url"].endswith("/friend.html"))
        self.assertIn("account=wxid_self", message["hash"])
        self.assertIn("user=wxid_friend", message["hash"])
        self.assertEqual(message["tab"], "overview")
        self.assertRegex(message["token"], r"^zus_[0-9a-f]{64}$")
        self.assertEqual(h.leases.holders("ui"), [{"kind": "ui", "key": "host", "pid": None}])
        self.assertEqual(h.command[1:], ["scripts/ui-host.cjs", "--client-url", "http://127.0.0.1:5000/"])

        self.assertEqual(h.open()["status"], "focused")
        self.assertEqual(h.host.ops("select-tab"), [])
        self.assertEqual(h.open("contact.memory")["status"], "focused")
        self.assertEqual(h.host.ops("select-tab"), [{"op": "select-tab", "key": first["windowKey"], "tab": "memories"}])
        self.assertEqual(len(h.hosts), 1)
        self.assertEqual(len({message["token"] for message in h.host.ops("open")}), 1)
        self.assertEqual(h.service.windows("c1"), [{"windowKey": first["windowKey"], "module": "contact.memory"}])

    def test_second_client_gets_its_own_window(self):
        h = Harness(self)
        first, second = h.open(client="c1"), h.open(client="c2")
        self.assertEqual((first["status"], second["status"]), ("opened", "opened"))
        self.assertNotEqual(first["windowKey"], second["windowKey"])
        self.assertNotEqual(h.token(0), h.token(1))

    def test_unknown_and_unopenable_modules_raise(self):
        h = Harness(self)
        for module, code in (("contact.nope", "module-unknown"), ("http://evil/", "module-unknown"),
                             ("app.settings.models", "module-not-openable")):
            with self.assertRaises(UiError) as caught:
                h.open(module)
            self.assertEqual(caught.exception.code, code)
        self.assertEqual(h.hosts, [])

    def test_contact_without_grant_and_other_account_are_refused(self):
        h = Harness(self, scope=FakeScope(accounts=frozenset({ACCOUNT_ID}), contacts={(ACCOUNT_ID, FRIEND)}))
        with self.assertRaises(UiError) as caught:
            h.open(subject=OTHER)
        self.assertEqual(caught.exception.code, "contact-not-authorized")
        with self.assertRaises(UiError) as caught:
            h.open(account_id="acc-2")
        self.assertEqual(caught.exception.code, "account-not-authorized")
        self.assertEqual(h.hosts, [])

    def test_unknown_saved_account_is_refused(self):
        h = Harness(self)
        with self.assertRaises(UiError) as caught:
            h.open(account_id="acc-9")
        self.assertEqual(caught.exception.code, "account-unknown")

    def test_unavailable_preconditions(self):
        cases = [({"enabled": False}, "plugin-disabled"), ({"live": "wxid_someone_else"}, "account-not-live"),
                 ({"desktop": False}, "no-interactive-desktop"), ({"electron": None}, "runtime-missing"),
                 ({"launcher_error": OSError("boom")}, "spawn-failed")]
        for options, reason in cases:
            h = Harness(self, **options)
            result = h.open()
            self.assertEqual((result["status"], result["reason"]), ("unavailable", reason), options)
            self.assertEqual(h.leases.count("ui"), 0)

    def test_no_display_kills_host(self):
        h = Harness(self, start="no-display")
        result = h.open()
        self.assertEqual((result["status"], result["reason"]), ("unavailable", "no-display"))
        self.assertTrue(h.host.killed)
        self.assertEqual(h.leases.count("ui"), 0)

    def test_host_never_ready_is_killed(self):
        h = Harness(self, start=None, ready_timeout=0.2)
        result = h.open()
        self.assertEqual((result["status"], result["reason"]), ("unavailable", "host-not-ready"))
        self.assertTrue(h.host.killed)
        self.assertEqual(h.leases.count("ui"), 0)

    def test_open_without_reply_fails_and_revokes(self):
        h = Harness(self, behavior=silent, reply_timeout=0.2)
        result = h.open()
        self.assertEqual((result["status"], result["reason"]), ("failed", "host-timeout"))
        token = h.token()
        self.assertEqual(h.host.ops("close"), [{"op": "close", "key": result["windowKey"]}])
        with self.assertRaises(UiGuardError):
            h.service.guard(token, "GET", "/friend.html", {}, None)

    def test_rate_limit_and_window_cap(self):
        h = Harness(self, max_windows=2)
        self.assertEqual(h.open()["status"], "opened")
        limited = h.open(subject="wxid_b", advance=1.0)
        self.assertEqual((limited["status"], limited["reason"]), ("failed", "rate-limited"))
        self.assertEqual(h.open(subject="wxid_b")["status"], "opened")
        capped = h.open(subject="wxid_c")
        self.assertEqual((capped["status"], capped["reason"]), ("failed", "window-limit"))
        self.assertEqual(h.open(subject="wxid_c", client="c2")["status"], "opened")

    def test_version_change_closes_windows_and_rejects_token(self):
        h = Harness(self)
        result = h.open()
        token = h.token()
        h.service.guard(token, "GET", "/friend.html", {}, None)
        h.open(client="c2")
        h.service.on_version_change("c1")
        self.assertEqual(h.host.ops("close"), [{"op": "close", "key": result["windowKey"]}])
        with self.assertRaises(UiGuardError):
            h.service.guard(token, "GET", "/friend.html", {}, None)
        self.assertEqual(len(h.service.windows("c2")), 1)

    def test_close_all_closes_every_clients_windows(self):
        h = Harness(self)
        first = h.open()
        token = h.token()
        second = h.open(client="c2", subject="wxid_b")
        self.assertEqual(h.service.close_all(), 2)
        self.assertEqual(sorted(op["key"] for op in h.host.ops("close")),
                         sorted([first["windowKey"], second["windowKey"]]))
        self.assertEqual((h.service.windows("c1"), h.service.windows("c2")), ([], []))
        with self.assertRaises(UiGuardError):
            h.service.guard(token, "GET", "/friend.html", {}, None)
        self.assertEqual(h.service.close_all(), 0)

    def test_host_closed_event_drops_the_window(self):
        h = Harness(self)
        result = h.open()
        token = h.token()
        h.host.emit({"event": "closed", "key": result["windowKey"]})
        self.assertTrue(wait_for(lambda: h.service.windows("c1") == []))
        with self.assertRaises(UiGuardError):
            h.service.guard(token, "GET", "/friend.html", {}, None)

    def test_host_eof_drops_sessions_and_releases_lease(self):
        h = Harness(self)
        h.open()
        token = h.token()
        self.assertEqual(h.leases.count("ui"), 1)
        h.host.eof()
        self.assertTrue(wait_for(lambda: h.leases.count("ui") == 0))
        with self.assertRaises(UiGuardError):
            h.service.guard(token, "GET", "/friend.html", {}, None)
        self.assertEqual(h.service.windows("c1"), [])
        self.assertEqual(h.open()["status"], "opened")  # a fresh host is spawned on demand
        self.assertEqual(len(h.hosts), 2)
        self.assertEqual(h.leases.count("ui"), 1)


class MainWindowTests(unittest.TestCase):
    def test_packaged_app_opens_friend_host_and_main_settings(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        install = Path(directory.name)
        root = install / "resources" / "client"
        root.mkdir(parents=True)
        (root.parent / "app.asar").touch()
        executable = install / "KnowYourFriends.exe"
        executable.touch()
        shell_commands = []
        h = Harness(self, root=root, electron=None, shell=shell_commands.append)
        with patch.dict("os.environ", {"KYF_ELECTRON": ""}):
            self.assertEqual(h.open()["status"], "opened")
            self.assertEqual(h.command, [str(executable), "--kyf-sidecar", "ui-host",
                                         "--client-url", "http://127.0.0.1:5000/"])
            h.leases.acquire("gui", "main")
            result = h.open("app.settings", subject=None, account_id=None)
        self.assertEqual(result["status"], "focused")
        # P2-3: a running GUI gets the shell directly, not the desktop launcher's bridge start.
        self.assertEqual(shell_commands, [[str(executable), "--kyf-shell", "--client-url", "http://127.0.0.1:5000/",
                                           "--open-module", "settings"]])

    def test_packaged_cold_start_keeps_desktop_launch(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        install = Path(directory.name)
        root = install / "resources" / "client"
        root.mkdir(parents=True)
        (root.parent / "app.asar").touch()
        executable = install / "KnowYourFriends.exe"
        executable.touch()
        holder = {"commands": []}

        def shell(command):
            holder["commands"].append(command)
            threading.Timer(0.05, lambda: holder["leases"].acquire("gui", "main")).start()

        h = Harness(self, root=root, electron=None, shell=shell)
        holder["leases"] = h.leases
        with patch.dict("os.environ", {"KYF_ELECTRON": ""}):
            result = h.open("app.settings", subject=None, account_id=None)
            self.assertEqual(result["status"], "opened")
            self.assertEqual(holder["commands"], [[str(executable), "--open-module", "settings"]])
            self.assertEqual(h.open("app.main", subject=None, account_id=None)["status"], "focused")
        self.assertEqual(holder["commands"][1], [str(executable), "--kyf-shell", "--client-url",
                                                 "http://127.0.0.1:5000/"])

    def test_running_gui_is_focused(self):
        commands = []
        h = Harness(self, shell=commands.append)
        h.leases.acquire("gui", "main")
        result = h.open("app.settings", subject=None, account_id=None)
        self.assertEqual((result["status"], result["windowKey"]), ("focused", "main"))
        self.assertEqual(commands[0][1:], ["scripts/real-client-shell.cjs", "--client-url",
                                           "http://127.0.0.1:5000/", "--open-module", "settings"])
        self.assertEqual(h.hosts, [])

    def test_new_gui_opens_once_lease_appears(self):
        holder = {}

        def shell(command):
            holder["command"] = command
            threading.Timer(0.1, lambda: holder["leases"].acquire("gui", "main")).start()

        h = Harness(self, shell=shell)
        holder["leases"] = h.leases
        result = h.open("app.main", subject=None, account_id=None)
        self.assertEqual(result["status"], "opened")
        self.assertNotIn("--open-module", holder["command"])

    def test_conversation_opens_in_the_restricted_host_even_with_a_main_window(self):
        commands = []
        h = Harness(self, shell=commands.append)
        h.leases.acquire("gui", "main")
        result = h.open("contact.conversation")
        self.assertEqual(result["status"], "opened")
        self.assertEqual(h.open("contact.conversation")["status"], "focused")
        self.assertNotEqual(result["windowKey"], "main")
        opened = h.host.ops("open")[0]
        self.assertEqual(opened["surface"], "chat")
        self.assertEqual(opened["url"], "http://127.0.0.1:5000/history.html")
        self.assertIn(f"account={ACCOUNT}&user={FRIEND}", opened["hash"])
        self.assertEqual(commands, [])
        self.assertEqual(h.service.windows("c1"), [{"windowKey": result["windowKey"], "module": "contact.conversation"}])

    def test_conversation_uses_window_load_result_instead_of_gui_lease(self):
        def load_failed(host, message):
            if message["op"] == "open":
                host.emit({"event": "error", "code": "load-failed", "key": message["key"], "reqId": message["reqId"]})

        h = Harness(self, behavior=load_failed)
        h.leases.acquire("gui", "main")
        result = h.open("contact.conversation")
        self.assertEqual((result["status"], result["reason"]), ("failed", "load-failed"))
        self.assertEqual(h.service.windows("c1"), [])

    def test_conversation_revokes_token_and_closes_only_its_generation(self):
        h = Harness(self)
        first = h.open("contact.conversation")
        token = h.token()
        h.auth["c1"]["authVersion"] += 1
        h.service.on_version_change("c1")
        self.assertEqual(h.service.windows("c1"), [])
        self.assertEqual(h.host.ops("close"), [{"op": "close", "key": first["windowKey"], "token": token}])
        second = h.open("contact.conversation")
        self.assertEqual(second["status"], "opened")
        h.host.emit({"event": "closed", "key": first["windowKey"], "token": token})
        with self.assertRaises(UiGuardError):
            h.service.guard(token, "GET", "/history.html", {}, None)
        self.assertNotEqual(h.token(), token)
        h.service.guard(h.token(), "GET", "/history.html", {}, None)
        self.assertEqual(len(h.service.windows("c1")), 1)

    def test_conversation_revocation_during_open_cancels_the_pending_result(self):
        submitted = threading.Event()

        def behavior(host, message):
            if message["op"] == "open":
                submitted.set()
            else:
                answering(host, message)

        h = Harness(self, behavior=behavior, reply_timeout=5)
        results = []
        attempt = threading.Thread(target=lambda: results.append(h.open("contact.conversation")))
        attempt.start()
        self.assertTrue(submitted.wait(2))
        h.service.on_version_change("c1")
        attempt.join(2)
        self.assertFalse(attempt.is_alive())
        self.assertEqual((results[0]["status"], results[0]["reason"]), ("failed", "authorization-changed"))
        self.assertEqual(h.host.ops("close")[0]["token"], h.token())
        self.assertEqual(h.service.windows("c1"), [])

    def test_conversation_keeps_the_contact_authorization_checks(self):
        commands = []
        h = Harness(self, shell=commands.append, scope=FakeScope(accounts=frozenset({ACCOUNT_ID}),
                                                                 contacts={(ACCOUNT_ID, FRIEND)}))
        h.leases.acquire("gui", "main")
        for subject, account_id, code in ((OTHER, ACCOUNT_ID, "contact-not-authorized"),
                                          (FRIEND, "acc-2", "account-not-authorized"),
                                          ("123@chatroom", ACCOUNT_ID, "context-invalid"),
                                          (None, ACCOUNT_ID, "context-invalid")):
            with self.assertRaises(UiError) as caught:
                h.open("contact.conversation", subject=subject, account_id=account_id)
            self.assertEqual(caught.exception.code, code)
        h.live = OTHER
        result = h.open("contact.conversation")
        self.assertEqual((result["status"], result["reason"]), ("unavailable", "account-not-live"))
        self.assertEqual(commands, [])

    def test_gui_that_never_appears_fails(self):
        h = Harness(self, shell=lambda command: None, shell_timeout=0.2)
        result = h.open("app.main", subject=None, account_id=None)
        self.assertEqual((result["status"], result["reason"]), ("failed", "shell-timeout"))


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self, scope=FakeScope(accounts=frozenset({ACCOUNT_ID}), contacts={(ACCOUNT_ID, FRIEND)}),
                         scope_cache_seconds=2.0)
        self.h.open()
        self.token = self.h.token()

    def allow(self, method, path, query=None, body=None):
        self.assertIsNone(self.h.service.guard(self.token, method, path, query or {}, body))

    def deny(self, method, path, query=None, body=None, code="ui-session-forbidden"):
        with self.assertRaises(UiGuardError) as caught:
            self.h.service.guard(self.token, method, path, query or {}, body)
        self.assertEqual(caught.exception.code, code)

    def test_reads_of_the_bound_contact_pass(self):
        scoped = {"account": ACCOUNT, "user": FRIEND}
        for path in ("/friend.html", "/friend.css", "/friend-core.js", "/friend-memory.js", "/friend-run.js",
                     "/plugin-frame.js", "/friend-scheme.js", "/friend.js", "/plugin-sdk/web/kyf-host.js",
                     "/plugin-pages/kyf.friend-analysis/ui/index.html"):
            self.allow("GET", path)
        for path in ("/api/friend/overview", "/api/friend/memories", "/api/friend/memory", "/api/friend/evidence",
                     "/api/friend/message", "/api/schemes", "/api/legacy-portrait"):
            self.allow("GET", path, scoped)
        self.allow("GET", "/api/tasks", {**scoped, "plugin": "kyf.friend-analysis"})
        self.allow("GET", "/api/media", {"user": FRIEND, "id": "m1"})
        self.allow("POST", "/api/plugins/rpc", body={"plugin": "kyf.friend-analysis", "method": "page.persona",
                                                     "params": {}, **scoped})
        self.allow("POST", "/api/plugins/rpc", body={"plugin": "kyf.friend-analysis", "method": "scheme.status",
                                                     **scoped})

    def test_other_contacts_and_endpoints_are_refused(self):
        self.deny("GET", "/api/friend/overview", {"account": ACCOUNT, "user": OTHER})
        self.deny("GET", "/api/friend/overview", {"account": "wxid_x", "user": FRIEND})
        self.deny("GET", "/api/friend/overview", {"user": FRIEND})
        self.deny("GET", "/api/friend/drafts", {"account": ACCOUNT, "user": FRIEND})
        self.deny("GET", "/api/tasks", {"account": ACCOUNT})
        self.deny("GET", "/api/tasks", {"account": ACCOUNT, "user": FRIEND, "plugin": "kyf.advisor"})
        self.deny("GET", "/api/sessions")
        self.deny("GET", "/api/mcp/clients")
        self.deny("GET", "/plugin-api/v1/x")
        self.deny("GET", "/plugin-pages/kyf.advisor/ui/index.html")
        self.deny("GET", "/api/media", {"user": OTHER, "id": "m1"})
        self.deny("GET", "/.env")

    def test_account_level_settings_are_refused_for_a_per_contact_window(self):
        # R2-03: /api/friend/settings carries the account-wide accountProfile, which MCP
        # only exposes as an account-level aggregate; a per-contact window must not read it.
        self.deny("GET", "/api/friend/settings", {"account": ACCOUNT, "user": FRIEND})

    def test_global_plugin_listing_is_refused(self):
        # P2-10: the restricted friend page never lists plugins.
        self.deny("GET", "/api/plugins")
        self.deny("GET", "/api/plugins", {"account": ACCOUNT})

    def test_only_the_friend_window_static_files_pass(self):
        # P2-11: the main client's code and other chatui pages are not part of this window.
        for path in ("/", "/index.html", "/app.js", "/settings-window.js", "/model-settings.js",
                     "/history.html", "/assets/app-icon.png", "/favicon.ico", "/friend.html/x",
                     "/plugin-sdk/web/a/b.js", "/plugin-sdk/web/"):
            self.deny("GET", path)

    def test_task_list_is_limited_to_module_plugins(self):
        # R2-07: without a plugin filter the list would return every plugin's tasks.
        scoped = {"account": ACCOUNT, "user": FRIEND}
        self.deny("GET", "/api/tasks", scoped)
        self.deny("GET", "/api/tasks", {**scoped, "plugin": "kyf.advisor"})
        self.deny("GET", "/api/tasks", {**scoped, "plugin": ""})

    def test_single_task_of_another_plugin_is_refused(self):
        # R2-07: same account and friend, but a kyf.advisor task.
        self.h.tasks["fa"] = {"account": ACCOUNT, "user": FRIEND, "plugin": "kyf.friend-analysis"}
        self.h.tasks["adv"] = {"account": ACCOUNT, "user": FRIEND, "plugin": "kyf.advisor"}
        self.h.tasks["bare"] = {"account": ACCOUNT, "user": FRIEND}
        self.allow("GET", "/api/task", {"id": "fa"})
        self.deny("GET", "/api/task", {"id": "adv"})
        self.deny("GET", "/api/task", {"id": "bare"})

    def test_disabling_the_module_plugin_revokes_and_closes_the_window(self):
        # R2-04: plugin state is rechecked with the same cadence as scope.
        key = self.h.service.windows("c1")[0]["windowKey"]
        self.allow("GET", "/friend.html")
        self.h.enabled = False
        self.h.now += 2.5
        self.deny("GET", "/friend.html")
        self.assertEqual(self.h.host.ops("close"), [{"op": "close", "key": key}])
        self.assertEqual(self.h.service.windows("c1"), [])
        self.h.enabled = True
        self.h.now += 2.5
        self.deny("GET", "/friend.html")  # the revoked token stays revoked
        self.assertEqual(self.h.open()["status"], "opened")
        self.assertNotEqual(self.h.token(), self.token)
        self.assertIsNone(self.h.service.guard(self.h.token(), "GET", "/friend.html", {}, None))

    def test_plugin_disabled_hook_closes_every_window_using_it(self):
        self.h.open(client="c2")
        self.h.service.on_plugin_disabled("kyf.advisor")
        self.assertEqual(self.h.host.ops("close"), [])
        self.h.service.on_plugin_disabled("kyf.friend-analysis")
        self.assertEqual(len(self.h.host.ops("close")), 2)
        self.assertEqual((self.h.service.windows("c1"), self.h.service.windows("c2")), ([], []))
        self.deny("GET", "/friend.html")

    def test_writes_are_read_only(self):
        scoped = {"account": ACCOUNT, "user": FRIEND}
        for path in ("/api/friend/open", "/api/friend/memory", "/api/friend/draft", "/api/tasks",
                     "/api/friend-opened", "/api/control/lease"):
            self.deny("POST", path, body=scoped, code="ui-session-read-only")
        self.deny("DELETE", "/api/accounts/x", code="ui-session-read-only")
        self.deny("POST", "/api/plugins/rpc", body={"plugin": "kyf.friend-analysis", "method": "scheme.estimate",
                                                    **scoped}, code="ui-session-read-only")
        self.deny("POST", "/api/plugins/rpc", body={"plugin": "kyf.advisor", "method": "page.x", **scoped})
        self.deny("POST", "/api/plugins/rpc", body={"plugin": "kyf.friend-analysis", "method": "page.persona",
                                                    "account": ACCOUNT, "user": OTHER})
        self.deny("POST", "/api/plugins/rpc", body=None)

    def test_task_must_belong_to_the_window(self):
        self.h.tasks["t1"] = {"account": ACCOUNT, "user": FRIEND, "plugin": "kyf.friend-analysis"}
        self.h.tasks["t2"] = {"account": ACCOUNT, "user": OTHER, "plugin": "kyf.friend-analysis"}
        self.allow("GET", "/api/task", {"id": "t1"})
        self.deny("GET", "/api/task", {"id": "t2"})
        self.deny("GET", "/api/task", {"id": "missing"})

    def test_auth_change_and_scope_revocation_are_rechecked(self):
        self.allow("GET", "/friend.html")
        self.h.auth["c1"] = {"authVersion": 2, "fingerprint": FINGERPRINT}
        self.allow("GET", "/friend.html")  # still inside the 2 s cache
        self.h.now += 2.5
        self.deny("GET", "/friend.html")
        self.h.auth["c1"] = {"authVersion": 1, "fingerprint": FINGERPRINT}
        self.h.scope = FakeScope(accounts=frozenset({ACCOUNT_ID}), contacts=set())
        self.h.now += 2.5
        self.deny("GET", "/friend.html")

    def test_malformed_or_unknown_tokens(self):
        for token in (None, "", "zus_" + "0" * 64, "abc", self.token.upper()):
            with self.assertRaises(UiGuardError):
                self.h.service.guard(token, "GET", "/friend.html", {}, None)

    def test_without_auth_lookup_every_request_fails_closed(self):
        self.h.service._current_auth = None
        self.h.service._checked.clear()
        self.deny("GET", "/friend.html")


class ConcurrencyTests(unittest.TestCase):
    """P2-12: one client's slow host startup / slow open reply must not serialize other clients."""

    def test_second_client_is_not_blocked_behind_a_slow_reply(self):
        slow_key = window_key("c1", "friend", ACCOUNT_ID, FRIEND)

        def behavior(host, message):
            if message["op"] == "open" and message["key"] == slow_key:
                threading.Timer(1.5, answering, (host, message)).start()
            else:
                answering(host, message)

        h = Harness(self, behavior=behavior, start=None, ready_timeout=5, reply_timeout=5)
        original = h.service._launcher

        def launcher(command):
            host = original(command)
            threading.Timer(0.3, host.emit, ({"event": "ready"},)).start()
            return host
        h.service._launcher = launcher
        results = {}
        first = threading.Thread(target=lambda: results.setdefault("c1", h.open(client="c1")))
        first.start()
        time.sleep(0.05)
        started = time.monotonic()
        results["c2"] = h.open(client="c2")
        elapsed = time.monotonic() - started
        first.join(5)
        self.assertEqual((results["c1"]["status"], results["c2"]["status"]), ("opened", "opened"))
        self.assertLess(elapsed, 1.0)  # host-ready (~0.3 s), not c1's 1.5 s reply
        self.assertEqual(len(h.hosts), 1)
        self.assertEqual(h.leases.count("ui"), 1)

    def test_concurrent_opens_of_one_window_share_one_session(self):
        def behavior(host, message):
            threading.Timer(0.2, answering, (host, message)).start()

        h = Harness(self, behavior=behavior, min_interval=0, reply_timeout=3)
        results = []
        threads = [threading.Thread(target=lambda: results.append(h.open(advance=0))) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(sorted(result["status"] for result in results), ["focused"] * 3 + ["opened"])
        self.assertEqual(len({message["token"] for message in h.host.ops("open")}), 1)
        self.assertEqual(len(h.hosts), 1)
        self.assertEqual(len(h.service.windows("c1")), 1)


class RealProcessTests(unittest.TestCase):
    """The default launcher against a tiny Python stand-in for ui-host.cjs."""

    def test_python_host_round_trip_and_kill_on_shutdown(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "scripts").mkdir()
        (root / "scripts" / "ui-host.cjs").write_text(
            "import json, sys\n"
            "print(json.dumps({'event': 'ready'}), flush=True)\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            "    if message['op'] == 'open':\n"
            "        print(json.dumps({'event': 'opened', 'key': message['key'], 'reqId': message['reqId']}),"
            " flush=True)\n"
            "    elif message['op'] == 'quit':\n"
            "        break\n", encoding="utf-8")
        leases = LeaseService(idle_seconds=0)
        service = UiWindowService(
            launcher=default_launcher(root), desktop_probe=lambda: True,
            registry_lookup=lambda account_id: {"account": ACCOUNT}, scope_for=lambda principal: FakeScope(),
            plugin_enabled=lambda plugin, account: True, live_account=lambda: ACCOUNT, leases=leases,
            current_auth=lambda client_id: {"authVersion": 1, "fingerprint": FINGERPRINT},
            client_url="http://127.0.0.1:5000", root=root, electron=lambda: sys.executable,
            ready_timeout=10, reply_timeout=10)
        self.addCleanup(service.shutdown)
        principal = {"clientId": "c1", "authVersion": 1, "fingerprint": FINGERPRINT}
        result = service.open(principal, FakeScope(), "contact.trends", ACCOUNT_ID, FRIEND)
        self.assertEqual(result["status"], "opened")
        holder = leases.holders("ui")[0]
        self.assertIsInstance(holder["pid"], int)
        service.shutdown()
        self.assertEqual(leases.count("ui"), 0)


if __name__ == "__main__":
    unittest.main()
