"""Protocol smoke test for the MCP stdio adapter, against a synthetic Core.

Spawns the adapter as a real subprocess pointed at a temporary installation whose
loopback port is answered by a fake Core (health, handshake, rpc), then drives
initialize, tools/list and tools/call over stdio. Zero tokens, synthetic data.
"""

import io
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import importlib.util

_LAUNCHER_SPEC = importlib.util.spec_from_file_location("real_client_launcher_test",
                                                        SCRIPTS / "start-real-client.py")
launcher = importlib.util.module_from_spec(_LAUNCHER_SPEC)
sys.modules[_LAUNCHER_SPEC.name] = launcher
_LAUNCHER_SPEC.loader.exec_module(launcher)
_ADAPTER_SPEC = importlib.util.spec_from_file_location("mcp_stdio_adapter",
                                                       SCRIPTS / "mcp-stdio-adapter.py")
adapter_module = importlib.util.module_from_spec(_ADAPTER_SPEC)
sys.modules[_ADAPTER_SPEC.name] = adapter_module
_ADAPTER_SPEC.loader.exec_module(adapter_module)
START_LOCK_TIMEOUT = adapter_module.START_LOCK_TIMEOUT
AdapterError = adapter_module.AdapterError
install_start_lock = adapter_module.install_start_lock

ADAPTER = SCRIPTS / "mcp-stdio-adapter.py"
CLIENT_ID = "mcp_smoketest"
CREDENTIAL = "zmc_" + "a" * 64
OPEN_CAPABILITIES = ["zhiji_edit_self_timeline", "zhiji_get_friend_analysis", "zhiji_get_media",
                     "zhiji_get_memory_evidence", "zhiji_get_message_context",
                     "zhiji_get_message_insights", "zhiji_get_self_analysis", "zhiji_get_task",
                     "zhiji_list_accounts", "zhiji_list_memories", "zhiji_ping",
                     "zhiji_search_messages", "zhiji_start_friend_analysis",
                     "zhiji_start_message_insights", "zhiji_start_self_analysis",
                     "zhiji_stop_self_analysis", "zhiji_stop_task", "zhiji_undo_self_timeline",
                     "zhiji_update_memory"]


class CommunityCatalogTests(unittest.TestCase):
    def test_send_tools_are_unavailable_even_if_a_core_advertises_them(self):
        names = ("zhiji_send_wechat_text", "zhiji_get_send_result")
        connection = mock.Mock()
        adapter = adapter_module.StdioAdapter(connection, stdin=io.StringIO(), stdout=io.StringIO())
        adapter._capabilities = {"server": {"openCapabilities": list(names)}}
        self.assertEqual(adapter.tool_list(), [])
        for name in names:
            self.assertNotIn(name, adapter_module.TOOL_CATALOG)
            with self.assertRaises(AdapterError) as caught:
                adapter.tool_call({"name": name, "arguments": {}})
            self.assertEqual(caught.exception.code, "unknown-tool")
        connection.call.assert_not_called()

    def test_deletion_keeps_its_long_timeout_and_request_identity(self):
        connection = mock.Mock()
        connection.call.return_value = {"accepted": True}
        adapter = adapter_module.StdioAdapter(connection, stdin=io.StringIO(), stdout=io.StringIO())
        adapter._capabilities = {"server": {"openCapabilities": ["zhiji_delete_account"]}}
        adapter.tool_call({"name": "zhiji_delete_account", "arguments": {
            "accountId": "synthetic-account", "requestId": "deletion-1"}})
        self.assertEqual(connection.call.call_args.kwargs["timeout"], adapter_module.LONG_CALL_TIMEOUT)
        self.assertEqual(connection.call.call_args.kwargs["request_id"], "deletion-1")


class FakeCore:
    """The synthetic loopback Core: identity answers, pairing and the probe."""

    def __init__(self):
        self.rpc_bodies = []
        self.handshakes = 0
        self.instance_id = None

    def handle(self, path, body, headers):
        if path == "/api/health":
            # What launcher.health() expects of a ready Core of this installation.
            return 200, {"version": launcher.VERSION, "instanceId": self.instance_id,
                         "appVersion": "0.0.0-test"}
        if path == "/api/mcp/handshake":
            self.handshakes += 1
            if headers.get("Authorization"):
                return 401, {"error": "mcp-unauthorized", "message": "不接受其他令牌"}
            if body.get("clientId") != CLIENT_ID or body.get("credential") != CREDENTIAL:
                return 401, {"error": "mcp-unauthorized", "message": "客户端凭据无效"}
            return 200, {"token": "zmt_" + "1" * 64, "clientId": CLIENT_ID,
                         "authVersion": 1, "expiresAtMs": 0}
        if path == "/api/mcp/rpc":
            self.rpc_bodies.append(body)
            if headers.get("Authorization") != "Bearer zmt_" + "1" * 64:
                return 401, {"error": "mcp-unauthorized", "message": "MCP 令牌无效或已过期"}
            action = body.get("action")
            if action == "zhiji_ping":
                return 200, {"ok": True, "result": {"service": "zhiji-mcp", "protocol": 1,
                                                    "profile": {"clientId": CLIENT_ID, "mode": "full",
                                                                "scope": "installation", "authVersion": 1},
                                                    "server": {"openCapabilities": OPEN_CAPABILITIES}}}
            return 200, {"ok": True, "result": {"action": action, "params": body.get("params"),
                                                "account": body.get("account"),
                                                "subjects": body.get("subjects")}}
        return 404, {"error": "not-found", "message": "接口不存在"}


def make_handler(core):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _run(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            status, payload = core.handle(self.path, body, self.headers)
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = _run

    return Handler


def fake_core_server(root, core):
    # Let Windows select an available port instead of deriving one that may be
    # inside an OS-excluded range. The normal installation port marker tells
    # the unmodified adapter subprocess where this synthetic Core is listening.
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(core))
    runtime = root / ".local" / "real-client-runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "selected-port.json").write_text(json.dumps({
        "schema": 1, "instanceId": core.instance_id, "port": server.server_port,
    }), encoding="utf-8")
    return server


class AdapterProcess:
    """Line-based stdio conversation with the spawned adapter, with a watchdog."""

    def __init__(self, process):
        self.process = process
        self.lines = queue.Queue()
        # Every raw stdout line, kept so the purity assertions can check the whole
        # transcript instead of only the replies this test happened to read.
        self.stdout_lines = []
        self.stderr = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for line in self.process.stdout:
            self.stdout_lines.append(line.decode("utf-8").strip())
            self.lines.put(self.stdout_lines[-1])

    def _read_stderr(self):
        for line in self.process.stderr:
            text = line.decode("utf-8").strip()
            self.stderr.append(text)

    def send(self, message):
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def send_raw(self, text):
        """One stdin line that is not JSON, for the parse-error path."""
        self.process.stdin.write((text + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def receive(self, timeout=15):
        try:
            return json.loads(self.lines.get(timeout=timeout))
        except queue.Empty:
            raise AssertionError("适配器未在时限内应答") from None

    def request(self, request_id, method, params=None):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        reply = self.receive()
        if reply.get("id") != request_id:
            raise AssertionError(f"应答标识不符：{reply}")
        return reply

    def wait_quiet(self, timeout=15):
        """Wait until the adapter has exited, then return its exit code."""
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            raise

    def assert_stdout_is_pure_mcp(self):
        """验收断言：stdout 整段可解析，每行都是一条 JSON-RPC 消息（含通知），没有杂项日志。"""
        if not self.stdout_lines:
            raise AssertionError("适配器没有产生任何 stdout 输出")
        for index, line in enumerate(self.stdout_lines):
            try:
                message = json.loads(line)
            except ValueError:
                raise AssertionError(f"stdout 第 {index + 1} 行不是 JSON：{line!r}") from None
            if not isinstance(message, dict):
                raise AssertionError(f"stdout 第 {index + 1} 行不是 JSON-RPC 消息：{line!r}")
            if message.get("jsonrpc") != "2.0":
                raise AssertionError(f"stdout 第 {index + 1} 行缺少 jsonrpc=2.0：{line!r}")

    def assert_stderr_clean(self, *secrets):
        """stderr 只允许诊断；凭据与私人正文永远不出现。"""
        text = "\n".join(self.stderr)
        for secret in secrets:
            if secret in text:
                raise AssertionError(f"stderr 泄漏了敏感内容：{secret!r}；stderr 为：{text!r}")

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()


class AdapterSmokeTests(unittest.TestCase):
    def setUp(self):
        self.install = tempfile.TemporaryDirectory()
        self.addCleanup(self.install.cleanup)
        root = Path(self.install.name)
        (root / "package.json").write_text(json.dumps({"version": "0.0.0-test"}), encoding="utf-8")
        (root / ".local").mkdir()
        self.root = root
        self.core = FakeCore()
        self.core.instance_id = launcher.instance_id(root)
        self.server = fake_core_server(root, self.core)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config_file = root / "mcp-client.json"
        self.config_file.write_text(json.dumps({"clientId": CLIENT_ID, "credential": CREDENTIAL}),
                                    encoding="utf-8")

    def spawn(self, *extra_args, env=None):
        process = subprocess.Popen(
            [sys.executable, str(ADAPTER), "--root", str(self.root), *extra_args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        adapter = AdapterProcess(process)
        self.addCleanup(adapter.close)
        return adapter

    def test_initialize_tools_list_and_ping_over_real_stdio(self):
        adapter = self.spawn("--config", str(self.config_file))
        reply = adapter.request(1, "initialize", {"protocolVersion": "2025-06-18",
                                                  "capabilities": {}, "clientInfo": {"name": "合成客户端"}})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(reply["result"]["serverInfo"]["name"], "zhiji-mcp")
        self.assertEqual(reply["result"]["capabilities"]["tools"]["listChanged"], True)
        # The adapter reused the healthy synthetic Core without launching anything.
        self.assertEqual(self.core.handshakes, 1)
        tools = adapter.request(2, "tools/list")["result"]["tools"]
        self.assertEqual(sorted(tool["name"] for tool in tools), OPEN_CAPABILITIES)
        schemas = {tool["name"]: tool["inputSchema"] for tool in tools}
        self.assertEqual(schemas["zhiji_search_messages"]["required"],
                         ["accountId", "subjectId", "query"])
        # The probe call maps onto the capability proxy envelope with the pid attached
        # at pairing and the credential never on the wire again.
        result = adapter.request(3, "tools/call",
                                 {"name": "zhiji_ping", "arguments": {}})["result"]
        body = self.core.rpc_bodies[-1]
        self.assertEqual(body, {"action": "zhiji_ping", "params": {}})
        self.assertEqual(result["structuredContent"]["profile"]["clientId"], CLIENT_ID)
        self.assertEqual(result["content"][0]["type"], "text")
        # A subject tool flattens into account + subjects for Core's per-contact check.
        adapter.request(4, "tools/call",
                        {"name": "zhiji_search_messages",
                         "arguments": {"accountId": "账号A", "subjectId": "好友1",
                                       "query": "合成", "limit": 5}})
        body = self.core.rpc_bodies[-1]
        self.assertEqual(body["account"], "账号A")
        self.assertEqual(body["subjects"], [{"accountId": "账号A", "subjectType": "wechat_contact",
                                             "subjectId": "好友1"}])
        self.assertEqual(body["params"], {"query": "合成", "limit": 5})
        # 媒体与记忆读取同属主体工具：帧数等契约参数进 params，账号与联系人进 subjects。
        adapter.request(7, "tools/call",
                        {"name": "zhiji_get_media",
                         "arguments": {"accountId": "账号A", "subjectId": "好友1",
                                       "messageId": "mv1", "frames": 2}})
        body = self.core.rpc_bodies[-1]
        self.assertEqual(body["action"], "zhiji_get_media")
        self.assertEqual(body["account"], "账号A")
        self.assertEqual(body["subjects"], [{"accountId": "账号A", "subjectType": "wechat_contact",
                                             "subjectId": "好友1"}])
        self.assertEqual(body["params"], {"messageId": "mv1", "frames": 2})
        adapter.request(8, "tools/call",
                        {"name": "zhiji_list_memories",
                         "arguments": {"accountId": "账号A", "subjectId": "好友1",
                                       "status": "confirmed", "limit": 10}})
        body = self.core.rpc_bodies[-1]
        self.assertEqual(body["action"], "zhiji_list_memories")
        self.assertEqual(body["params"], {"status": "confirmed", "limit": 10})
        # 写工具的 requestId 进调用信封顶层（幂等），绝不混入业务参数。
        adapter.request(9, "tools/call",
                        {"name": "zhiji_update_memory",
                         "arguments": {"accountId": "账号A", "subjectId": "好友1",
                                       "requestId": "req-write-9", "memoryId": "mem-1",
                                       "text": "合成改写", "tags": ["经历"], "eventTime": None,
                                       "expectedVersion": 1}})
        body = self.core.rpc_bodies[-1]
        self.assertEqual(body["action"], "zhiji_update_memory")
        self.assertEqual(body["requestId"], "req-write-9")
        self.assertEqual(body["params"], {"memoryId": "mem-1", "text": "合成改写", "tags": ["经历"],
                                          "eventTime": None, "expectedVersion": 1})
        # 分析任务：主体工具、任务工具与账号级工具的三种信封形状。
        adapter.request(10, "tools/call",
                        {"name": "zhiji_start_friend_analysis",
                         "arguments": {"accountId": "账号A", "subjectId": "好友1",
                                       "requestId": "req-task-1"}})
        body = self.core.rpc_bodies[-1]
        self.assertEqual(body["action"], "zhiji_start_friend_analysis")
        self.assertEqual(body["requestId"], "req-task-1")
        self.assertEqual(body["account"], "账号A")
        self.assertEqual(body["subjects"], [{"accountId": "账号A", "subjectType": "wechat_contact",
                                             "subjectId": "好友1"}])
        self.assertEqual(body["params"], {})
        adapter.request(11, "tools/call", {"name": "zhiji_get_task", "arguments": {"taskId": "task-1"}})
        body = self.core.rpc_bodies[-1]
        self.assertEqual((body["action"], body["params"]), ("zhiji_get_task", {"taskId": "task-1"}))
        self.assertNotIn("subjects", body)
        self.assertNotIn("account", body)
        adapter.request(12, "tools/call",
                        {"name": "zhiji_get_self_analysis",
                         "arguments": {"accountId": "账号A", "section": "persona"}})
        body = self.core.rpc_bodies[-1]
        self.assertEqual((body["action"], body["account"], body["params"]),
                         ("zhiji_get_self_analysis", "账号A", {"section": "persona"}))
        self.assertNotIn("subjects", body)
        adapter.request(13, "tools/call",
                        {"name": "zhiji_edit_self_timeline",
                         "arguments": {"accountId": "账号A", "requestId": "r-13", "target": "event", "action": "edit",
                                       "id": "e1", "fields": {"when": "2024.05"}, "expectedVersion": 3}})
        body = self.core.rpc_bodies[-1]
        self.assertEqual((body["action"], body["account"], body["requestId"]),
                         ("zhiji_edit_self_timeline", "账号A", "r-13"))
        self.assertEqual(body["params"], {"target": "event", "action": "edit", "id": "e1",
                                          "fields": {"when": "2024.05"}, "expectedVersion": 3})
        # Unknown tools and unexpected arguments are stable JSON-RPC errors.
        error = adapter.request(5, "tools/call", {"name": "zhiji_未知", "arguments": {}})
        self.assertEqual(error["error"]["code"], -32000)
        error = adapter.request(6, "tools/call",
                                {"name": "zhiji_ping", "arguments": {"hostile": True}})
        self.assertEqual(error["error"]["code"], -32000)
        # 带私密文本的参数只出现在 stdout 的 MCP 回包里；stderr 诊断不携带它。
        adapter.request(13, "tools/call",
                        {"name": "zhiji_search_messages",
                         "arguments": {"accountId": "账号A", "subjectId": "好友1",
                                       "query": "私密正文样本"}})
        # 无法解析的输入行得到稳定的解析错误回包，而不是把诊断写进 stdout。
        adapter.send_raw("这不是JSON")
        parse_error = adapter.receive()
        self.assertEqual(parse_error["error"]["code"], -32700)
        # Closing stdin ends the adapter cleanly.
        adapter.process.stdin.close()
        self.assertEqual(adapter.process.wait(timeout=15), 0)
        # 阶段 6 验收：stdout 整段可解析且只含 JSON-RPC 消息（含通知），没有杂项
        # 日志；stderr 只允许诊断，凭据、账号、联系人名与私人正文都不出现。
        adapter.assert_stdout_is_pure_mcp()
        adapter.assert_stderr_clean(CREDENTIAL, "私密正文样本", "账号A", "好友1")

    def test_unsupported_protocol_version_falls_back_to_the_server_latest(self):
        adapter = self.spawn("--config", str(self.config_file))
        reply = adapter.request(1, "initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        # 客户端声明的受支持旧版本按原样回显，不强制升级协议。
        reply = adapter.request(2, "initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(reply["result"]["protocolVersion"], "2024-11-05")

    def test_missing_credentials_fail_on_stderr_without_them(self):
        process = subprocess.Popen([sys.executable, str(ADAPTER), "--root", str(self.root)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env={"SYSTEMROOT": os_environ().get("SYSTEMROOT", "")})
        _stdout, stderr = process.communicate(input=b"", timeout=30)
        self.assertEqual(process.returncode, 1)
        text = stderr.decode("utf-8")
        self.assertIn("missing-credentials", text)
        self.assertNotIn(CREDENTIAL, text)


def os_environ():
    import os
    return os.environ


class StartLockTests(unittest.TestCase):
    def test_installation_lock_serializes_cold_starts(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            with install_start_lock(runtime, timeout=1):
                with self.assertRaises(AdapterError) as caught:
                    with install_start_lock(runtime, timeout=0.3):
                        pass
                self.assertEqual(caught.exception.code, "start-lock-timeout")
            # After release the next starter proceeds (the wait-then-retry reuse).
            with install_start_lock(runtime, timeout=START_LOCK_TIMEOUT):
                pass


class AutoStartPathTests(unittest.TestCase):
    """The adapter's automatic start goes through the launcher's recovery semantics."""

    def patch_launcher(self, health_state, ensure_service):
        """Swap the adapter's launcher module for a stub; the real install metadata stays."""
        recorded = {"recovery": None}

        class StubLauncher:
            VERSION = launcher.VERSION
            Config = launcher.Config
            LauncherError = launcher.LauncherError
            default_port = staticmethod(launcher.default_port)
            selected_port = staticmethod(launcher.selected_port)
            checked_runtime_dir = staticmethod(launcher.checked_runtime_dir)

            @staticmethod
            def health(config):
                return health_state[0]

            @staticmethod
            def ensure_service(config, recovery=False):
                recorded["recovery"] = recovery
                return ensure_service(config, recovery)

        original = adapter_module.launcher
        adapter_module.launcher = StubLauncher
        self.addCleanup(setattr, adapter_module, "launcher", original)
        return recorded

    def test_auto_start_uses_the_recovery_path_and_never_clears_the_latch(self):
        state = ["unavailable"]

        def ensure(config, recovery):
            state[0] = "ready"  # the headless start succeeded
            return True, None

        recorded = self.patch_launcher(state, ensure)
        connection = adapter_module.CoreConnection(Path(tempfile.mkdtemp()), CLIENT_ID, CREDENTIAL)
        url = connection.resolve()
        self.assertTrue(url.startswith("http://127.0.0.1:"))
        self.assertTrue(recorded["recovery"],
                        "自动 MCP 启动必须走 recovery 语义：闩锁生效且不清除")

    def test_latch_refusal_maps_to_a_stable_error(self):
        state = ["unavailable"]

        def ensure(config, recovery):
            raise launcher.LauncherError(
                "Automatic recovery is disabled after clearing the current account")

        self.patch_launcher(state, ensure)
        connection = adapter_module.CoreConnection(Path(tempfile.mkdtemp()), CLIENT_ID, CREDENTIAL)
        with self.assertRaises(AdapterError) as caught:
            connection.resolve()
        self.assertEqual(caught.exception.code, "core-start-refused")
        self.assertIn("Automatic recovery", caught.exception.message)

    def test_outdated_install_stays_passive_for_the_auto_path(self):
        state = ["outdated"]
        recorded = self.patch_launcher(state, lambda config, recovery: (False, None))
        connection = adapter_module.CoreConnection(Path(tempfile.mkdtemp()), CLIENT_ID, CREDENTIAL)
        with self.assertRaises(AdapterError) as caught:
            connection.resolve()
        self.assertEqual(caught.exception.code, "core-unavailable")
        self.assertEqual(recorded["recovery"], True)


class ToolChangeNotificationTests(unittest.TestCase):
    """notifications/tools/list_changed：能力目录或档案权限变化恰好通知一次。"""

    def run_adapter(self):
        sequence = [["zhiji_ping"], ["zhiji_ping", "zhiji_list_memories"]]
        calls = [0]

        def probe():
            index = min(calls[0], len(sequence) - 1)
            calls[0] += 1
            return {"server": {"openCapabilities": sequence[index]}}

        connection = types.SimpleNamespace(probe=probe)
        stdout = io.StringIO()
        with mock.patch.object(adapter_module, "TOOLCHANGE_POLL_SECONDS", 0.02):
            adapter = adapter_module.StdioAdapter(connection, stdin=io.StringIO(), stdout=stdout)
            adapter.refresh_capabilities()
            adapter.start_polling()
            try:
                deadline = time.monotonic() + 5.0
                while "notifications/tools/list_changed" not in stdout.getvalue():
                    if time.monotonic() > deadline:
                        raise AssertionError("能力变化未产生 list_changed 通知")
                    time.sleep(0.02)
                # 轮询继续：能力不再变化就不再重复通知。
                time.sleep(0.15)
            finally:
                adapter.stop_polling()
                adapter._poll_thread.join(timeout=5)
        lines = [line for line in stdout.getvalue().splitlines() if line]
        notifications = []
        for line in lines:
            message = json.loads(line)  # 进程内写出同样逐行可解析
            if message.get("method") == "notifications/tools/list_changed":
                notifications.append(message)
        self.assertEqual(len(notifications), 1)
        self.assertEqual({line["jsonrpc"] for line in map(json.loads, lines)}, {"2.0"})
        # 目录随探针刷新：变化后的能力集含两个工具。
        adapter.refresh_capabilities()
        self.assertEqual(sorted(tool["name"] for tool in adapter.tool_list()),
                         ["zhiji_list_memories", "zhiji_ping"])

    def test_capability_changes_emit_list_changed_once(self):
        self.run_adapter()


class InstalledLayoutTests(unittest.TestCase):
    """阶段 6：安装版布局——适配器默认以自身所在安装为根，无界面完成握手与探针。"""

    def setUp(self):
        self.install = tempfile.TemporaryDirectory(prefix="kyf-mcp-install-")
        self.addCleanup(self.install.cleanup)
        root = Path(self.install.name)
        scripts = root / "scripts"
        scripts.mkdir()
        # 真实字节的安装版布局：scripts/ 里的适配器与启动器，以及它们引用的 bridge/ 模块
        #（与 staging 清单一致），package.json 在安装根。
        shutil.copy2(SCRIPTS / "mcp-stdio-adapter.py", scripts / "mcp-stdio-adapter.py")
        shutil.copy2(SCRIPTS / "start-real-client.py", scripts / "start-real-client.py")
        bridge = root / "bridge"
        bridge.mkdir()
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("stage_for_install_test",
                                             SCRIPTS / "stage-real-client.py")
        _stage = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_stage)
        source_root = SCRIPTS.parent
        for name in _stage.BRIDGE:
            if name.endswith(".py"):
                shutil.copy2(source_root / "bridge" / name, bridge / name)
        (root / "package.json").write_text(json.dumps({"version": "0.0.0-test"}), encoding="utf-8")
        (root / ".local").mkdir()
        self.root = root
        self.core = FakeCore()
        self.core.instance_id = launcher.instance_id(root)
        self.server = fake_core_server(root, self.core)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.config_file = root / "mcp-client.json"
        self.config_file.write_text(json.dumps({"clientId": CLIENT_ID, "credential": CREDENTIAL}),
                                    encoding="utf-8")

    def test_adapter_without_explicit_root_serves_the_installation_it_sits_in(self):
        process = subprocess.Popen(
            [sys.executable, str(self.root / "scripts" / "mcp-stdio-adapter.py"),
             "--config", str(self.config_file)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        adapter = AdapterProcess(process)
        self.addCleanup(adapter.close)
        # 显式 --root 缺省时解析为适配器所在安装；复用健康 Core，不新起进程。
        reply = adapter.request(1, "initialize", {"protocolVersion": "2025-06-18",
                                                  "capabilities": {}, "clientInfo": {"name": "安装版"}})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "zhiji-mcp")
        self.assertEqual(self.core.handshakes, 1)
        result = adapter.request(2, "tools/call", {"name": "zhiji_ping", "arguments": {}})["result"]
        self.assertEqual(result["structuredContent"]["server"]["openCapabilities"],
                         OPEN_CAPABILITIES)
        tools = adapter.request(3, "tools/list")["result"]["tools"]
        self.assertEqual(sorted(tool["name"] for tool in tools), OPEN_CAPABILITIES)
        adapter.process.stdin.close()
        self.assertEqual(adapter.process.wait(timeout=15), 0)
        adapter.assert_stdout_is_pure_mcp()
        adapter.assert_stderr_clean(CREDENTIAL)
        # 安装根之外没有写入：本安装目录就是唯一被触碰的数据位置。
        self.assertEqual(self.core.rpc_bodies[-1]["action"], "zhiji_ping")


if __name__ == "__main__":
    unittest.main()
