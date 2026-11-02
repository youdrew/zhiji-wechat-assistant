"""The real Electron confirm-window presenter, on synthetic protocol lines only.

No Electron is launched here: the launcher seam receives a fake window process
whose stdout follows the protocol in scripts/confirm-window.cjs. A full-chain
test runs the real confirm service against it; the real-window smoke (opt-in,
real Electron) lives in scripts/confirm-window-smoke.cjs.
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_leases import LeaseService
from core_mcp_confirm import CONFIRM_TTL_SECONDS, ConfirmUnavailable, McpDeletionConfirmService
from core_mcp_confirm_window import CONFIRM_LEASE_KIND, ElectronConfirmPresenter
from core_mcp_proxy import McpError

CONFIRM_ID = "zcf_" + "a1b2c3d4e5f60718293a4b5c6d7e8f90"
CONFIRM_TOKEN = "b" * 64
FINGERPRINT = "synthetic-install-fingerprint"
CORE_URL = "http://127.0.0.1:8123/"


def payload(**overrides):
    base = {"confirmId": CONFIRM_ID, "token": CONFIRM_TOKEN, "action": "zhiji_delete_account",
            "kindLabel": "删除整个账号", "clientName": "合成客户端", "accountLabel": "合成账号甲",
            "impact": "将删除该账号的全部本机资料，不可恢复。",
            "expiresAtMs": int((time.time() + CONFIRM_TTL_SECONDS) * 1000)}
    base.update(overrides)
    return base


def line(event):
    return json.dumps(event).encode("utf-8") + b"\n"


class FakeWindowProcess:
    """Stand-in for the spawned window process; ``hang`` models a living silent window."""

    def __init__(self, events=(), *, hang=False):
        self.lines = deque(line(event) for event in events)
        self.hang = hang
        self.killed = False
        self.closed = False
        self._wake = threading.Event()

    @property
    def stdout(self):
        return self

    def readline(self):
        if self.lines:
            return self.lines.popleft()
        if self.hang:
            self._wake.wait(30)  # 活着但再也不说话：只有期限或 kill 能结束它。
            return b""
        return b""

    def poll(self):
        return None if self.hang or self.lines else 0

    def kill(self):
        self.killed = True
        self._wake.set()

    def wait(self, timeout=None):
        return None if self.hang else 0

    def close(self):
        self.closed = True


class PresenterProtocolTests(unittest.TestCase):
    """presenter 适配器的协议分类：决定、拒绝与如实上抛。"""

    def make_presenter(self, handle, *, root=None, admin_token=lambda: "synthetic-admin-token",
                       leases=None):
        self.handle = handle
        self.commands = []
        self.requests = []

        def launcher(command, request_line):
            self.commands.append(command)
            self.requests.append(json.loads(request_line.decode("utf-8")))
            return handle

        return ElectronConfirmPresenter(
            root or Path(tempfile.mkdtemp()), CORE_URL, admin_token, leases=leases,
            electron_path="synthetic-electron", script_path="confirm-window.cjs",
            launcher=launcher)

    def test_a_decision_line_becomes_the_presenter_return(self):
        handle = FakeWindowProcess([{"event": "shown"},
                                    {"event": "decided", "granted": True, "posted": True,
                                     "reason": "accept"}])
        presenter = self.make_presenter(handle)
        self.assertTrue(presenter(payload()))
        self.assertEqual(self.requests[0]["confirmId"], CONFIRM_ID)
        self.assertEqual(self.requests[0]["token"], CONFIRM_TOKEN)
        self.assertEqual(self.requests[0]["core"], CORE_URL)
        self.assertEqual(self.requests[0]["adminToken"], "synthetic-admin-token")

    def test_a_refused_decision_is_returned_as_false(self):
        handle = FakeWindowProcess([{"event": "shown"},
                                    {"event": "decided", "granted": False, "posted": True,
                                     "reason": "cancel"}])
        self.assertFalse(self.make_presenter(handle)(payload()))

    def test_crash_after_showing_denies_instead_of_guessing(self):
        handle = FakeWindowProcess([{"event": "shown"}])  # 然后立即退出
        self.assertFalse(self.make_presenter(handle)(payload()))

    def test_exit_without_any_line_is_reported_as_unavailable(self):
        handle = FakeWindowProcess([])
        with self.assertRaises(ConfirmUnavailable):
            self.make_presenter(handle)(payload())

    def test_an_error_line_before_showing_is_reported_as_unavailable(self):
        handle = FakeWindowProcess([{"event": "error", "message": "no interactive desktop"}])
        with self.assertRaises(ConfirmUnavailable):
            self.make_presenter(handle)(payload())

    def test_a_shown_window_going_silent_is_denied_at_the_deadline(self):
        # expiresAtMs 已过：适配器立即到期，杀掉挂着的窗口并拒绝。
        handle = FakeWindowProcess([{"event": "shown"}], hang=True)
        self.assertFalse(self.make_presenter(handle)(payload(expiresAtMs=int((time.time() - 5) * 1000))))
        self.assertTrue(handle.killed)

    def test_a_window_that_never_shows_is_reported_as_unavailable(self):
        handle = FakeWindowProcess([], hang=True)
        presenter = self.make_presenter(handle)
        with self.assertRaises(ConfirmUnavailable):
            presenter(payload(expiresAtMs=int((time.time() - 5) * 1000)))
        self.assertTrue(handle.killed)

    def test_a_missing_electron_runtime_is_reported_as_unavailable(self):
        presenter = self.make_presenter(FakeWindowProcess([]), root=Path(tempfile.mkdtemp()))
        presenter._electron = None  # 二进制缺失：绝不拉起任何进程。
        with self.assertRaises(ConfirmUnavailable):
            presenter(payload())

    def test_the_confirm_lease_is_held_for_the_presentation_only(self):
        leases = LeaseService()
        seen = {}

        handle = FakeWindowProcess([{"event": "shown"},
                                    {"event": "decided", "granted": True, "posted": True,
                                     "reason": "accept"}])
        presenter = self.make_presenter(handle, leases=leases)

        def observing_launcher(command, request_line):
            seen["during"] = leases.count(CONFIRM_LEASE_KIND)
            return handle

        presenter._launcher = observing_launcher
        self.assertTrue(presenter(payload()))
        self.assertEqual(seen["during"], 1)
        self.assertEqual(leases.count(CONFIRM_LEASE_KIND), 0)

    def test_an_admin_token_failure_only_drops_the_settings_channel(self):
        handle = FakeWindowProcess([{"event": "shown"},
                                    {"event": "decided", "granted": True, "posted": False,
                                     "reason": "accept"}])
        def broken():
            raise RuntimeError("supervisor gone")

        presenter = self.make_presenter(handle, admin_token=broken)
        self.assertTrue(presenter(payload()))
        self.assertEqual(self.requests[0]["adminToken"], "")


def wait_state(confirm, confirm_id, state, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if confirm.view(confirm_id)["state"] == state:
                return
        except McpError:
            return  # 已清理的记录同样视为结束。
        time.sleep(0.01)
    raise AssertionError(f"确认状态未到达 {state}")


class FullChainTests(unittest.TestCase):
    """真确认服务 + 真 presenter 适配器（假窗进程）：全链路按服务契约收尾。"""

    def setUp(self):
        self.handle = None
        self.presenter = None

    def presenter_for(self, events, *, hang=False):
        self.handle = FakeWindowProcess(events, hang=hang)
        self.presenter = ElectronConfirmPresenter(
            Path(tempfile.mkdtemp()), CORE_URL, lambda: "synthetic-admin-token",
            electron_path="synthetic-electron", script_path="confirm-window.cjs",
            launcher=lambda command, request_line: self.handle)
        return self.presenter

    def test_confirmed_through_the_window_decision(self):
        confirm = McpDeletionConfirmService(self.presenter_for(
            [{"event": "shown"}, {"event": "decided", "granted": True, "posted": True,
                                  "reason": "accept"}]))
        view = confirm.begin(action="zhiji_delete_account", kind="delete_account",
                             client_id="cli-a", client_name="合成客户端", auth_version=1,
                             fingerprint=FINGERPRINT, account_id="acc-a", params_hash="hash",
                             account_label="合成账号甲")
        wait_state(confirm, view["confirmId"], "confirmed")
        # 一次性校验照常绑定并消费。
        confirm.check_use(view["confirmId"], action="zhiji_delete_account", kind="delete_account",
                          client_id="cli-a", auth_version=1, fingerprint=FINGERPRINT,
                          account_id="acc-a", params_hash="hash")

    def test_denied_by_the_window(self):
        confirm = McpDeletionConfirmService(self.presenter_for(
            [{"event": "shown"}, {"event": "decided", "granted": False, "posted": True,
                                  "reason": "cancel"}]))
        view = confirm.begin(action="zhiji_delete_account", kind="delete_account",
                             client_id="cli-a", client_name="合成客户端", auth_version=1,
                             fingerprint=FINGERPRINT, account_id="acc-a", params_hash="hash",
                             account_label="合成账号甲")
        wait_state(confirm, view["confirmId"], "denied")
        with self.assertRaises(McpError) as refused:
            confirm.check_use(view["confirmId"], action="zhiji_delete_account",
                              kind="delete_account", client_id="cli-a", auth_version=1,
                              fingerprint=FINGERPRINT, account_id="acc-a", params_hash="hash")
        self.assertEqual(refused.exception.code, "confirmation-denied")

    def test_unavailable_desktop_never_confirms(self):
        confirm = McpDeletionConfirmService(self.presenter_for(
            [{"event": "error", "message": "no interactive desktop"}]))
        view = confirm.begin(action="zhiji_delete_account", kind="delete_account",
                             client_id="cli-a", client_name="合成客户端", auth_version=1,
                             fingerprint=FINGERPRINT, account_id="acc-a", params_hash="hash",
                             account_label="合成账号甲")
        wait_state(confirm, view["confirmId"], "unavailable")
        with self.assertRaises(McpError) as refused:
            confirm.check_use(view["confirmId"], action="zhiji_delete_account",
                              kind="delete_account", client_id="cli-a", auth_version=1,
                              fingerprint=FINGERPRINT, account_id="acc-a", params_hash="hash")
        self.assertEqual(refused.exception.code, "confirmation-unavailable")


class WiringTests(unittest.TestCase):
    """Core 启动即注入真 presenter；按需组件共享同一份租约。"""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()

    def test_core_services_wire_the_confirm_window_presenter(self):
        from core_services import CoreServices
        root = Path(__file__).resolve().parents[1]
        core = CoreServices(SimpleNamespace(source=None), root, "http://127.0.0.1:0",
                            local=Path(self.directory.name),
                            bundled_dir=root / "no-plugins",
                            local_dir=Path(self.directory.name) / "none")
        self.addCleanup(core.close)
        self.assertIs(core.mcp.leases, core.leases)
        self.assertIs(core.tasks.leases, core.leases)
        presenter = core.mcp.confirm.presenter
        self.assertIsInstance(presenter, ElectronConfirmPresenter)
        self.assertIs(presenter.leases, core.leases)
        self.assertEqual(presenter.core_url, "http://127.0.0.1:0")
        self.assertTrue(callable(presenter.admin_token))
        self.assertIsNone(presenter.admin_token())  # 没有运行中的插件进程时返回 None。


if __name__ == "__main__":
    unittest.main()
