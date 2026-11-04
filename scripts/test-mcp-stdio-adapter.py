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
from mcp_test_ports import bindable_install_dir  # noqa: E402

START_LOCK_TIMEOUT = adapter_module.START_LOCK_TIMEOUT
AdapterError = adapter_module.AdapterError
install_start_lock = adapter_module.install_start_lock

ADAPTER = SCRIPTS / "mcp-stdio-adapter.py"
CLIENT_ID = "mcp_smoketest"
CREDENTIAL = "zmc_" + "a" * 64
OPEN_CAPABILITIES = ["zhiji_edit_self_timeline", "zhiji_find_contacts", "zhiji_get_contact",
                     "zhiji_get_contact_brief", "zhiji_get_friend_analysis", "zhiji_get_media",
                     "zhiji_get_memory_evidence", "zhiji_get_message_context",
                     "zhiji_get_message_insights", "zhiji_get_recent_messages",
                     "zhiji_get_self_analysis", "zhiji_get_task",
                     "zhiji_list_accounts", "zhiji_list_memories", "zhiji_list_recent_conversations",
                     "zhiji_ping",
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
            with self.assertRaises(adapter_module.InvalidParams):
                adapter.tool_call({"name": name, "arguments": {}})
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
        # action -> (HTTP status, error payload): scripted Core business failures.
        self.failures = {}
        # Actions whose scripted failure is answered once and then cleared.
        self.fail_once = set()

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
            if action in self.failures:
                failure = self.failures[action]
                if action in self.fail_once:
                    self.fail_once.discard(action)
                    del self.failures[action]
                return failure
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
        self.install = bindable_install_dir(launcher.default_port)
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
        self.assertEqual(schemas["zhiji_search_messages"]["required"], ["query"])
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
        # 未知工具是协议错误（-32602）；多余参数是工具执行错误，以 isError 结果交给模型。
        error = adapter.request(5, "tools/call", {"name": "zhiji_未知", "arguments": {}})
        self.assertEqual(error["error"]["code"], -32602)
        result = adapter.request(6, "tools/call",
                                 {"name": "zhiji_ping", "arguments": {"hostile": True}})["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid-params")
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

    def test_read_entry_tools_route_and_annotate(self):
        # G1–G3：新读取工具列出、带只读标注；跨联系人搜索不带 subjectId 时不发 subjects。
        adapter = self.spawn("--config", str(self.config_file))
        adapter.request(1, "initialize", {"protocolVersion": "2025-06-18"})
        tools = {tool["name"]: tool for tool in adapter.request(2, "tools/list")["result"]["tools"]}
        for name in ("zhiji_find_contacts", "zhiji_get_contact", "zhiji_get_contact_brief",
                     "zhiji_list_recent_conversations", "zhiji_get_recent_messages", "zhiji_search_messages"):
            with self.subTest(name=name):
                self.assertTrue(tools[name]["annotations"]["readOnlyHint"])
                self.assertFalse(tools[name]["annotations"]["destructiveHint"])
                self.assertLessEqual(len(tools[name]["description"]), 120)
        adapter.request(3, "tools/call", {"name": "zhiji_search_messages", "arguments": {"query": "搬家"}})
        body = self.core.rpc_bodies[-1]
        self.assertEqual(body, {"action": "zhiji_search_messages", "params": {"query": "搬家"}})
        adapter.request(4, "tools/call", {"name": "zhiji_search_messages",
                                          "arguments": {"query": "搬家", "accountId": "账号A", "limit": 5}})
        body = self.core.rpc_bodies[-1]
        self.assertEqual((body["account"], body["params"]), ("账号A", {"query": "搬家", "limit": 5}))
        self.assertNotIn("subjects", body)
        # 带 subjectId 但缺 accountId：信封校验失败，不打到 Core。
        before = len(self.core.rpc_bodies)
        result = adapter.request(5, "tools/call", {"name": "zhiji_search_messages",
                                                   "arguments": {"query": "搬家", "subjectId": "好友1"}})["result"]
        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid-params")
        self.assertEqual(len(self.core.rpc_bodies), before)
        for index, name in enumerate(("zhiji_get_contact", "zhiji_get_contact_brief",
                                      "zhiji_get_recent_messages"), start=6):
            with self.subTest(name=name):
                adapter.request(index, "tools/call", {"name": name,
                                                      "arguments": {"accountId": "账号A", "subjectId": "好友1"}})
                body = self.core.rpc_bodies[-1]
                self.assertEqual(body["subjects"], [{"accountId": "账号A", "subjectType": "wechat_contact",
                                                     "subjectId": "好友1"}])
                self.assertEqual(body["params"], {})
        adapter.request(10, "tools/call", {"name": "zhiji_find_contacts",
                                           "arguments": {"query": "张三", "accountId": "账号A"}})
        body = self.core.rpc_bodies[-1]
        self.assertEqual((body["account"], body["params"]), ("账号A", {"query": "张三"}))
        self.assertNotIn("subjects", body)
        adapter.request(11, "tools/call", {"name": "zhiji_list_recent_conversations", "arguments": {"limit": 5}})
        self.assertEqual(self.core.rpc_bodies[-1], {"action": "zhiji_list_recent_conversations",
                                                    "params": {"limit": 5}})

    def test_stale_authorization_rehandshakes_once_then_succeeds(self):
        adapter = self.spawn("--config", str(self.config_file))
        adapter.request(1, "initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(self.core.handshakes, 1)
        self.core.failures["zhiji_get_contact"] = (403, {"error": "stale-authorization", "message": "授权已变化"})
        self.core.fail_once.add("zhiji_get_contact")
        reply = adapter.request(2, "tools/call", {"name": "zhiji_get_contact",
                                                  "arguments": {"accountId": "账号A", "subjectId": "好友1"}})
        self.assertNotIn("isError", reply["result"])
        self.assertEqual(reply["result"]["structuredContent"]["action"], "zhiji_get_contact")
        self.assertEqual(self.core.handshakes, 2)
        # 重新配对后仍被拒：只重试一次，如实返回 isError。
        self.core.failures["zhiji_get_contact"] = (403, {"error": "stale-authorization", "message": "授权已变化"})
        result = adapter.request(3, "tools/call", {"name": "zhiji_get_contact",
                                                   "arguments": {"accountId": "账号A", "subjectId": "好友1"}})["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "stale-authorization")
        self.assertEqual(self.core.handshakes, 3)
        # 其他 403（如联系人未授权）不触发重新配对。
        self.core.failures["zhiji_get_contact"] = (403, {"error": "contact-not-authorized", "message": "未授权"})
        adapter.request(4, "tools/call", {"name": "zhiji_get_contact",
                                          "arguments": {"accountId": "账号A", "subjectId": "好友1"}})
        self.assertEqual(self.core.handshakes, 3)

    def test_unsupported_protocol_version_falls_back_to_the_server_latest(self):
        adapter = self.spawn("--config", str(self.config_file))
        reply = adapter.request(1, "initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        # 客户端声明的受支持旧版本按原样回显，不强制升级协议。
        reply = adapter.request(2, "initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(reply["result"]["protocolVersion"], "2024-11-05")

    def test_2026_07_28_is_negotiated_down_to_the_implemented_version(self):
        # G8：2026-07-28 的 discover/resultType 状态模型未实现，不得回显。
        adapter = self.spawn("--config", str(self.config_file))
        reply = adapter.request(1, "initialize", {"protocolVersion": "2026-07-28",
                                                  "capabilities": {}, "clientInfo": {"name": "新客户端"}})
        self.assertEqual(reply["result"]["protocolVersion"], "2025-06-18")
        self.assertNotIn("2026-07-28", adapter_module.KNOWN_PROTOCOL_VERSIONS)

    def test_initialize_carries_the_agent_instructions(self):
        # G5：不支持 Skill 的客户端从 instructions 得到调用顺序与安全约定。
        adapter = self.spawn("--config", str(self.config_file))
        result = adapter.request(1, "initialize", {"protocolVersion": "2025-06-18"})["result"]
        instructions = result["instructions"]
        self.assertEqual(instructions, adapter_module.SERVER_INSTRUCTIONS)
        self.assertLessEqual(len(instructions), 900)
        for needle in ("zhiji_find_contacts", "zhiji_get_contact_brief", "zhiji_search_messages",
                       "zhiji_get_task", "不当指令", "确认状态", "若可用",
                       "zhiji_open_window", "推断"):
            self.assertIn(needle, instructions)
        self.assertEqual(result["serverInfo"]["version"], adapter_module.SERVER_VERSION)

    def test_tools_list_entries_carry_annotations(self):
        # G7：每个列出的工具都带标注，与 tool_annotations() 一致。
        adapter = self.spawn("--config", str(self.config_file))
        adapter.request(1, "initialize", {"protocolVersion": "2025-06-18"})
        tools = adapter.request(2, "tools/list")["result"]["tools"]
        for tool in tools:
            self.assertEqual(tool["annotations"], adapter_module.tool_annotations(tool["name"]))
        by_name = {tool["name"]: tool["annotations"] for tool in tools}
        self.assertTrue(by_name["zhiji_search_messages"]["readOnlyHint"])
        self.assertFalse(by_name["zhiji_update_memory"]["readOnlyHint"])
        self.assertFalse(by_name["zhiji_update_memory"]["destructiveHint"])

    def test_business_failures_come_back_as_is_error_results(self):
        # G6：Core 业务错误、能力未开放与信封校验失败都是 isError 工具结果，不是 JSON-RPC 错误。
        adapter = self.spawn("--config", str(self.config_file))
        adapter.request(1, "initialize", {"protocolVersion": "2025-06-18"})
        cases = [
            ("zhiji_update_memory", 409, "version-conflict", "版本不符"),
            ("zhiji_search_messages", 403, "contact-not-authorized", "该联系人未授权"),
            ("zhiji_list_memories", 403, "stale-authorization", "授权已变化"),
            ("zhiji_get_task", 404, "not-found", "任务不存在"),
            ("zhiji_get_self_analysis", 403, "aggregate-blocked", "逐联系人模式不提供账号级汇总"),
            ("zhiji_get_friend_analysis", 418, "brand-new-code", "未登记的错误码"),
        ]
        arguments = {
            "zhiji_update_memory": {"accountId": "账号A", "subjectId": "好友1", "requestId": "r-1",
                                    "memoryId": "m", "text": "t", "tags": [], "eventTime": None,
                                    "expectedVersion": 1},
            "zhiji_search_messages": {"accountId": "账号A", "subjectId": "好友1", "query": "q"},
            "zhiji_list_memories": {"accountId": "账号A", "subjectId": "好友1"},
            "zhiji_get_task": {"taskId": "task-x"},
            "zhiji_get_self_analysis": {"accountId": "账号A"},
            "zhiji_get_friend_analysis": {"accountId": "账号A", "subjectId": "好友1"},
        }
        for index, (tool, status, code, message) in enumerate(cases, start=10):
            with self.subTest(code=code):
                self.core.failures[tool] = (status, {"error": code, "message": message})
                reply = adapter.request(index, "tools/call", {"name": tool, "arguments": arguments[tool]})
                self.assertNotIn("error", reply)
                result = reply["result"]
                self.assertIs(result["isError"], True)
                error = result["structuredContent"]["error"]
                self.assertEqual((error["code"], error["message"]), (code, message))
                expected_hint = adapter_module.HINTS.get(code, adapter_module.GENERIC_HINT)
                self.assertEqual(error["hint"], expected_hint)
                self.assertEqual(result["content"][0]["text"], f"{code}: {message}\n建议: {expected_hint}")
        # 能力未开放（不在 openCapabilities 里）：适配器本地拒绝，同样是 isError，且不打到 Core。
        before = len(self.core.rpc_bodies)
        result = adapter.request(30, "tools/call",
                                 {"name": "zhiji_create_memory", "arguments": {}})["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "capability-not-open")
        self.assertEqual(len(self.core.rpc_bodies), before)
        # 缺少必填参数：信封校验失败，isError，不打到 Core。
        result = adapter.request(31, "tools/call",
                                 {"name": "zhiji_search_messages",
                                  "arguments": {"accountId": "账号A", "subjectId": "好友1"}})["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "invalid-params")
        self.assertIn("query", result["structuredContent"]["error"]["message"])
        self.assertEqual(len(self.core.rpc_bodies), before)
        # 协议层错误仍是 JSON-RPC：未知方法 -32601，params/arguments 形状错误 -32602。
        self.assertEqual(adapter.request(32, "prompts/list")["error"]["code"], -32601)
        self.assertEqual(adapter.request(33, "tools/call", ["不是对象"])["error"]["code"], -32602)
        self.assertEqual(adapter.request(34, "tools/call",
                                         {"name": "zhiji_ping", "arguments": "x"})["error"]["code"],
                         -32602)
        self.assertEqual(adapter.request(35, "tools/call", {"arguments": {}})["error"]["code"], -32602)
        adapter.process.stdin.close()
        self.assertEqual(adapter.process.wait(timeout=15), 0)
        adapter.assert_stdout_is_pure_mcp()
        adapter.assert_stderr_clean(CREDENTIAL, "账号A", "好友1", "版本不符")

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


class ToolAnnotationTests(unittest.TestCase):
    """G7：每个目录工具都显式归类；新增工具漏归类会让这里失败。"""

    def test_every_catalog_tool_is_classified(self):
        unclassified = []
        for name in adapter_module.TOOL_CATALOG:
            try:
                annotations = adapter_module.tool_annotations(name)
            except ValueError:
                unclassified.append(name)
                continue
            self.assertEqual(set(annotations), {"title", "readOnlyHint", "destructiveHint",
                                                "idempotentHint", "openWorldHint"})
            self.assertTrue(annotations["title"])
        self.assertEqual(unclassified, [], "新工具必须在 READ_ONLY_TOOLS 或 WRITE_TOOLS 里归类并给出标题")

    def test_unclassified_names_raise(self):
        with self.assertRaises(ValueError):
            adapter_module.tool_annotations("zhiji_never_classified")

    def test_classification_sets_are_consistent(self):
        m = adapter_module
        self.assertEqual(m.READ_ONLY_TOOLS & m.WRITE_TOOLS, frozenset())
        self.assertLessEqual(m.DESTRUCTIVE_TOOLS, m.WRITE_TOOLS)
        self.assertLessEqual(m.NON_IDEMPOTENT_TOOLS, m.WRITE_TOOLS)
        self.assertLessEqual(m.OPEN_WORLD_TOOLS, m.WRITE_TOOLS)
        self.assertEqual(set(m.TOOL_TITLES), set(m.READ_ONLY_TOOLS | m.WRITE_TOOLS))

    def test_risk_classes(self):
        annotate = adapter_module.tool_annotations
        read = annotate("zhiji_get_message_context")
        self.assertEqual((read["readOnlyHint"], read["destructiveHint"], read["idempotentHint"],
                          read["openWorldHint"]), (True, False, True, False))
        self.assertFalse(any(annotate(name)["openWorldHint"] for name in adapter_module.TOOL_CATALOG))
        for name in ("zhiji_delete_account", "zhiji_clear_local_data", "zhiji_delete_memory",
                     "zhiji_remove_import_material"):
            with self.subTest(name=name):
                self.assertTrue(annotate(name)["destructiveHint"])
                self.assertFalse(annotate(name)["readOnlyHint"])
        for name in ("zhiji_update_memory", "zhiji_create_memory", "zhiji_stop_task"):
            with self.subTest(name=name):
                self.assertFalse(annotate(name)["destructiveHint"])
        self.assertFalse(annotate("zhiji_create_memory")["idempotentHint"])
        self.assertTrue(annotate("zhiji_update_memory")["idempotentHint"])
        # 规划中的打开窗口：有界面副作用、非破坏、可重复。
        window = annotate("zhiji_open_window")
        self.assertEqual((window["readOnlyHint"], window["destructiveHint"], window["idempotentHint"],
                          window["openWorldHint"]), (False, False, True, False))


class MediaResultTests(unittest.TestCase):
    """媒体静帧以 MCP image 内容返回，文本与结构化结果里不再夹带 base64。"""

    def test_frames_become_image_content(self):
        core = {"messageId": "m1", "available": True, "reason": None,
                "items": [{"mime": "image/jpeg", "data": "QUJD", "timeMs": 0},
                          {"mime": "image/png", "data": "REVG"},
                          {"mime": "image/bmp", "data": "R0hJ"}]}
        result = adapter_module.tool_result("zhiji_get_media", core)
        images = [block for block in result["content"] if block["type"] == "image"]
        self.assertEqual(images, [{"type": "image", "data": "QUJD", "mimeType": "image/jpeg"},
                                  {"type": "image", "data": "REVG", "mimeType": "image/png"}])
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertNotIn("QUJD", result["content"][0]["text"])
        self.assertEqual(result["structuredContent"]["items"],
                         [{"mime": "image/jpeg", "timeMs": 0, "image": 1}, {"mime": "image/png", "image": 2},
                          {"mime": "image/bmp", "omitted": "mime-unsupported"}])
        self.assertEqual(result["structuredContent"]["imageCount"], 2)
        self.assertEqual(core["items"][0]["data"], "QUJD")  # Core 的结果对象不被改写

    def test_unavailable_media_and_other_tools_stay_text(self):
        missing = adapter_module.tool_result("zhiji_get_media",
                                             {"available": False, "reason": "offline", "items": []})
        self.assertEqual([block["type"] for block in missing["content"]], ["text"])
        self.assertEqual(missing["structuredContent"]["imageCount"], 0)
        other = adapter_module.tool_result("zhiji_get_contact", {"items": [{"mime": "image/png", "data": "x"}]})
        self.assertEqual(other["structuredContent"], {"items": [{"mime": "image/png", "data": "x"}]})
        self.assertEqual(len(other["content"]), 1)


class ToolErrorMappingTests(unittest.TestCase):
    """G6：本地服务不可用与超时同样以 isError 工具结果交给模型。"""

    def adapter_with(self, call):
        connection = types.SimpleNamespace(
            probe=lambda: {"server": {"openCapabilities": ["zhiji_get_task"]}}, call=call)
        stdout = io.StringIO()
        adapter = adapter_module.StdioAdapter(connection, stdin=io.StringIO(), stdout=stdout)
        adapter.refresh_capabilities()
        return adapter, stdout

    def test_core_unavailable_and_timeout_are_is_error(self):
        for code in ("core-unavailable", "core-start-refused", "core-timeout"):
            with self.subTest(code=code):
                def call(*_args, **_kwargs):
                    raise AdapterError(code, "合成失败")
                adapter, stdout = self.adapter_with(call)
                adapter.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": "zhiji_get_task",
                                                   "arguments": {"taskId": "t"}}})
                reply = json.loads(stdout.getvalue().splitlines()[-1])
                self.assertNotIn("error", reply)
                self.assertTrue(reply["result"]["isError"])
                self.assertEqual(reply["result"]["structuredContent"]["error"]["hint"],
                                 adapter_module.HINTS[code])

    def test_socket_timeout_maps_to_core_timeout_without_retry(self):
        connection = adapter_module.CoreConnection(Path(tempfile.mkdtemp()), CLIENT_ID, CREDENTIAL)
        connection.url, connection.token = "http://127.0.0.1:1", "zmt_x"
        attempts = []

        def post(*_args, **_kwargs):
            attempts.append(1)
            raise adapter_module.socket.timeout("timed out")

        connection._post = post
        with self.assertRaises(AdapterError) as caught:
            connection.call("zhiji_delete_account", {}, timeout=5)
        self.assertEqual(caught.exception.code, "core-timeout")
        # 结果未知的调用绝不自动重发。
        self.assertEqual(len(attempts), 1)

    def test_probe_recovers_after_stale_authorization(self):
        # 30 秒重探针：授权变化后重新配对一次即恢复，新开的能力进入目录。
        connection = adapter_module.CoreConnection(Path(tempfile.mkdtemp()), CLIENT_ID, CREDENTIAL)
        connection.url, connection.token = "http://127.0.0.1:1", "zmt_old"
        state = {"handshakes": 0, "rpc": []}

        def post(url, path, body, token=None, timeout=None):
            if path == "/api/mcp/handshake":
                state["handshakes"] += 1
                return {"token": "zmt_new", "authVersion": 2}
            state["rpc"].append(token)
            if token != "zmt_new":
                raise adapter_module.HTTPError(url + path, 403, "Forbidden", {}, io.BytesIO(json.dumps(
                    {"error": "stale-authorization", "message": "授权已变化"}).encode("utf-8")))
            return {"ok": True, "result": {"server": {"openCapabilities": ["zhiji_ping", "zhiji_find_contacts"]}}}

        connection._post = post
        adapter = adapter_module.StdioAdapter(connection, stdin=io.StringIO(), stdout=io.StringIO())
        adapter.refresh_capabilities()
        self.assertEqual(adapter.open_capabilities(), {"zhiji_ping", "zhiji_find_contacts"})
        self.assertEqual((state["handshakes"], state["rpc"]), (1, ["zmt_old", "zmt_new"]))
        self.assertEqual(connection.auth_version, 2)

    def test_core_dying_during_repair_is_core_unavailable(self):
        # 401 与 403 stale-authorization 两条重新配对路径：Core 恰在此时退出（连接被断开、
        # 拒绝连接或握手超时）都给稳定的 core-unavailable，绝不落成 -32603。
        class DyingCore(FakeCore):
            def __init__(self, status, code):
                super().__init__()
                self.status, self.code, self.dead = status, code, False

            def handle(self, path, body, headers):
                if path == "/api/mcp/rpc":
                    self.rpc_bodies.append(body)
                    self.dead = True
                    return self.status, {"error": self.code, "message": "合成"}
                return super().handle(path, body, headers)

        def serve(core):
            base = make_handler(core)

            class Handler(base):
                def _run(self):
                    if core.dead and self.path == "/api/mcp/handshake":
                        self.close_connection = True
                        self.connection.close()  # 进程中途退出：不回任何应答
                        return
                    super()._run()

                do_GET = do_POST = _run

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            return f"http://127.0.0.1:{server.server_address[1]}"

        for status, code in ((401, "mcp-unauthorized"), (403, "stale-authorization")):
            with self.subTest(status=status):
                core = DyingCore(status, code)
                connection = adapter_module.CoreConnection(Path(tempfile.mkdtemp()), CLIENT_ID, CREDENTIAL)
                connection.url, connection.token = serve(core), "zmt_old"
                with self.assertRaises(AdapterError) as caught:
                    connection.call("zhiji_get_task", {"taskId": "t"})
                self.assertEqual(caught.exception.code, "core-unavailable")
                self.assertEqual(len(core.rpc_bodies), 1)
        stale = io.BytesIO(json.dumps({"error": "stale-authorization", "message": "合成"}).encode("utf-8"))
        for failure in (adapter_module.URLError("refused"), adapter_module.socket.timeout("timed out"),
                        ConnectionResetError("reset")):
            with self.subTest(failure=type(failure).__name__):
                connection = adapter_module.CoreConnection(Path(tempfile.mkdtemp()), CLIENT_ID, CREDENTIAL)
                connection.url, connection.token = "http://127.0.0.1:1", "zmt_old"
                stale.seek(0)

                def post(url, path, body, token=None, timeout=None, failure=failure):
                    if path == "/api/mcp/handshake":
                        raise failure
                    raise adapter_module.HTTPError(url + path, 403, "Forbidden", {}, stale)

                connection._post = post
                with self.assertRaises(AdapterError) as caught:
                    connection.call("zhiji_get_task", {"taskId": "t"})
                self.assertEqual(caught.exception.code, "core-unavailable")
                self.assertIsNone(connection.url)
        # Through the stdio adapter the failure is an isError result, not a JSON-RPC error.
        def call(*_args, **_kwargs):
            raise AdapterError("core-unavailable", "重新配对时本地服务无法连接")
        adapter, stdout = self.adapter_with(call)
        adapter.handle_message({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                "params": {"name": "zhiji_get_task", "arguments": {"taskId": "t"}}})
        self.assertTrue(json.loads(stdout.getvalue().splitlines()[-1])["result"]["isError"])

    def test_authorization_refusals_have_specific_hints(self):
        for code in ("write-not-enabled", "management-not-enabled", "aggregate-blocked"):
            with self.subTest(code=code):
                self.assertIn(code, adapter_module.HINTS)
                self.assertNotEqual(adapter_module.HINTS[code], adapter_module.GENERIC_HINT)
        # The aggregate hint covers accounts mode too, not only per-contact clients.
        self.assertIn("按账号", adapter_module.HINTS["aggregate-blocked"])

    def test_internal_bug_stays_a_json_rpc_error(self):
        def call(*_args, **_kwargs):
            raise RuntimeError("bug")
        adapter, stdout = self.adapter_with(call)
        adapter.handle_message({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                "params": {"name": "zhiji_get_task", "arguments": {"taskId": "t"}}})
        reply = json.loads(stdout.getvalue().splitlines()[-1])
        self.assertEqual(reply["error"]["code"], -32603)


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
        self.install = bindable_install_dir(launcher.default_port, prefix="kyf-mcp-install-")
        self.addCleanup(self.install.cleanup)
        root = Path(self.install.name)
        scripts = root / "scripts"
        scripts.mkdir()
        # 真实字节的安装版布局：scripts/ 里的适配器与启动器，以及它们引用的 bridge/ 模块
        #（与 staging 清单一致），package.json 在安装根。
        shutil.copy2(SCRIPTS / "mcp-stdio-adapter.py", scripts / "mcp-stdio-adapter.py")
        shutil.copy2(SCRIPTS / "start-real-client.py", scripts / "start-real-client.py")
        shutil.copy2(SCRIPTS / "zhiji_mcp_http.py", scripts / "zhiji_mcp_http.py")
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


def event(seq, subject="wxid_friend", count=2):
    return {"eventId": f"ev_{seq:024d}", "seq": seq, "type": "message.new", "accountId": "acc_1",
            "subject": {"accountId": "acc_1", "subjectType": "wechat_contact", "subjectId": subject},
            "isGroup": False, "newCount": count, "countExact": True, "firstTime": 1, "lastTime": 2,
            "observedAt": 3, "catchUp": False}


class ScriptedEvents:
    """In-process Core stand-in for the event pump: scripted zhiji_poll_events answers."""

    def __init__(self, answers, capabilities=("zhiji_ping", "zhiji_poll_events")):
        self.answers = list(answers)
        self.calls = []
        self.capabilities = list(capabilities)

    def probe(self):
        return {"server": {"openCapabilities": self.capabilities}}

    def call(self, action, params, account=None, subjects=None, request_id=None, timeout=None):
        self.calls.append((action, dict(params), timeout))
        if not self.answers:
            time.sleep(0.02)
            return {"events": [], "cursor": "c-idle", "baseline": False, "gap": False, "source": {"state": "live"}}
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def page(events, cursor, baseline=False):
    return {"events": events, "cursor": cursor, "baseline": baseline, "gap": False, "source": {"state": "live"}}


class EventPumpTests(unittest.TestCase):
    """G11：后台事件泵——资源订阅、去重、日志级别门控、重新基线与 stdin EOF 停止。"""

    def adapter(self, answers, **kwargs):
        connection = ScriptedEvents(answers, **kwargs)
        stdout = io.StringIO()
        adapter = adapter_module.StdioAdapter(connection, stdin=io.StringIO(), stdout=stdout)
        adapter.refresh_capabilities()
        return adapter, connection, stdout

    @staticmethod
    def messages(stdout):
        return [json.loads(line) for line in stdout.getvalue().splitlines() if line]

    def request(self, adapter, stdout, request_id, method, params=None):
        adapter.handle_message({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        return [message for message in self.messages(stdout) if message.get("id") == request_id][-1]

    def notifications(self, stdout, method):
        return [message for message in self.messages(stdout) if message.get("method") == method]

    def test_initialize_declares_resources_and_logging(self):
        adapter, _connection, stdout = self.adapter([], capabilities=("zhiji_ping",))
        reply = self.request(adapter, stdout, 1, "initialize", {"protocolVersion": "2025-06-18"})
        adapter.events.stop()
        capabilities = reply["result"]["capabilities"]
        self.assertEqual(capabilities["resources"], {"subscribe": True, "listChanged": False})
        self.assertEqual(capabilities["logging"], {})
        self.assertEqual(set(capabilities), {"tools", "resources", "logging"})  # no client-specific channel

    def test_resources_list_read_and_subscription_gate(self):
        adapter, connection, stdout = self.adapter([
            page([], "c0", baseline=True), page([event(1)], "c1"), page([event(1), event(2)], "c2")])
        listed = self.request(adapter, stdout, 1, "resources/list")["result"]["resources"]
        self.assertEqual([(item["uri"], item["mimeType"]) for item in listed],
                         [("zhiji://events/recent", "application/json")])
        pump = adapter.events
        self.assertEqual(pump.poll_once(), 0)  # baseline
        self.assertEqual(connection.calls[0], ("zhiji_poll_events",
                                               {"waitSeconds": 25, "consumer": "mcp-adapter", "resume": True},
                                               adapter_module.EVENT_HTTP_TIMEOUT))
        self.assertEqual(pump.poll_once(), 1)
        # Not subscribed: no resources/updated notification.
        self.assertEqual(self.notifications(stdout, "notifications/resources/updated"), [])
        self.assertEqual(connection.calls[1][1]["cursor"], "c0")
        self.request(adapter, stdout, 2, "resources/subscribe", {"uri": "zhiji://events/recent"})
        self.assertEqual(pump.poll_once(), 1)  # event 1 again is deduplicated
        self.assertEqual(self.notifications(stdout, "notifications/resources/updated"),
                         [{"jsonrpc": "2.0", "method": "notifications/resources/updated",
                           "params": {"uri": "zhiji://events/recent"}}])
        read = self.request(adapter, stdout, 3, "resources/read", {"uri": "zhiji://events/recent"})["result"]
        content = read["contents"][0]
        self.assertEqual((content["uri"], content["mimeType"]), ("zhiji://events/recent", "application/json"))
        body = json.loads(content["text"])
        self.assertEqual([item["eventId"] for item in body["events"]], [event(1)["eventId"], event(2)["eventId"]])
        self.assertTrue(body["available"])
        self.assertEqual(body["source"], {"state": "live"})
        missing = self.request(adapter, stdout, 4, "resources/read", {"uri": "zhiji://other"})
        self.assertEqual(missing["error"]["code"], -32002)
        self.request(adapter, stdout, 5, "resources/unsubscribe", {"uri": "zhiji://events/recent"})
        connection.answers.append(page([event(3)], "c3"))
        pump.poll_once()
        self.assertEqual(len(self.notifications(stdout, "notifications/resources/updated")), 1)

    def test_recent_events_are_bounded(self):
        adapter, _connection, _stdout = self.adapter(
            [page([event(index) for index in range(1, 151)], "a"),
             page([event(index) for index in range(151, 301)], "b")])
        adapter.events.poll_once()
        adapter.events.poll_once()
        recent = adapter.events.snapshot()["events"]
        self.assertEqual(len(recent), adapter_module.EVENT_RECENT_MAX)
        self.assertEqual(recent[-1]["seq"], 300)

    def test_log_notices_follow_the_client_level_and_carry_counts_only(self):
        adapter, connection, stdout = self.adapter(
            [page([event(1)], "a"), page([event(2)], "b"), page([event(3, "wxid_x", 4), event(4, "wxid_y", 1)], "c")])
        adapter.events.poll_once()  # no level set yet: silent
        self.assertEqual(self.request(adapter, stdout, 1, "logging/setLevel", {"level": "warning"})["result"], {})
        adapter.events.poll_once()  # warning > notice: silent
        self.assertEqual(self.notifications(stdout, "notifications/message"), [])
        self.request(adapter, stdout, 2, "logging/setLevel", {"level": "info"})
        adapter.events.poll_once()
        notices = self.notifications(stdout, "notifications/message")
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["params"]["level"], "notice")
        text = notices[0]["params"]["data"]
        self.assertIn("2 个会话", text)
        self.assertIn("5 条", text)
        for secret in ("wxid_x", "wxid_y", "acc_1", "ev_"):
            self.assertNotIn(secret, text)
        bad = self.request(adapter, stdout, 3, "logging/setLevel", {"level": "loud"})
        self.assertEqual(bad["error"]["code"], -32602)

    def test_stale_cursor_rebaselines_without_resending(self):
        adapter, connection, stdout = self.adapter([
            page([], "c0", baseline=True), page([event(1)], "c1"),
            AdapterError("cursor-stale", "授权已变更"), page([], "n0", baseline=True), page([event(2)], "n1")])
        self.request(adapter, stdout, 1, "resources/subscribe", {"uri": "zhiji://events/recent"})
        pump = adapter.events
        pump.poll_once()
        pump.poll_once()
        with self.assertRaises(AdapterError):
            pump.poll_once()
        pump.poll_once()
        # Re-baseline: no cursor and no resume, so nothing before the change comes back.
        self.assertEqual(connection.calls[3][1], {"waitSeconds": 25, "consumer": "mcp-adapter", "resume": False})
        pump.poll_once()
        self.assertEqual(connection.calls[4][1]["cursor"], "n0")
        self.assertEqual([item["seq"] for item in pump.snapshot()["events"]], [1, 2])
        self.assertEqual(len(self.notifications(stdout, "notifications/resources/updated")), 2)

    def test_pump_runs_after_initialize_and_stops_on_stdin_eof(self):
        connection = ScriptedEvents([page([], "c0", baseline=True), page([event(1)], "c1")])
        stdout = io.StringIO()
        line = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                           "params": {"protocolVersion": "2025-06-18"}})
        subscribe = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "resources/subscribe",
                                "params": {"uri": "zhiji://events/recent"}})

        class SlowStdin:
            """Two requests, a pause for the pump to run, then EOF."""

            def __init__(self):
                # Popped from the end: subscribe first, so no pump notification races it.
                self.lines = [line + "\n", subscribe + "\n"]

            def readline(self):
                if self.lines:
                    return self.lines.pop()
                deadline = time.monotonic() + 5
                while not connection.calls[1:] and time.monotonic() < deadline:
                    time.sleep(0.01)
                time.sleep(0.1)
                return ""

        adapter = adapter_module.StdioAdapter(connection, stdin=SlowStdin(), stdout=stdout)
        adapter.refresh_capabilities()
        adapter.serve()
        self.assertTrue(adapter.events.stop_event.is_set())
        adapter.events.thread.join(timeout=3)
        self.assertFalse(adapter.events.thread.is_alive())
        self.assertEqual(len(self.notifications(stdout, "notifications/resources/updated")), 1)
        count = len(stdout.getvalue())
        time.sleep(0.1)
        self.assertEqual(len(stdout.getvalue()), count, "nothing is written after EOF")

    def test_pump_idles_while_the_tool_is_closed(self):
        adapter, connection, _stdout = self.adapter([], capabilities=("zhiji_ping",))
        with mock.patch.object(adapter_module, "TOOLCHANGE_POLL_SECONDS", 0.02):
            adapter.events.start()
            time.sleep(0.15)
            adapter.events.stop()
            adapter.events.thread.join(timeout=2)
        self.assertEqual(connection.calls, [])


class ProfileCredentialTests(unittest.TestCase):
    """G12：--profile 读取 connect 保存的本机保护凭据；优先级 --profile > --config > 环境变量。"""

    def setUp(self):
        import os
        import zhiji_mcp_credentials as credentials
        self.credentials = credentials
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = mock.patch.dict(os.environ, {credentials.HOME_ENV: self.home.name,
                                               adapter_module.CLIENT_ID_ENV: "env-client",
                                               adapter_module.CREDENTIAL_ENV: "env-secret"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config = Path(self.home.name) / "client.json"
        self.config.write_text(json.dumps({"clientId": "file-client", "credential": "file-secret"}),
                               encoding="utf-8")

    def test_precedence(self):
        if not self.credentials.dpapi_available():
            self.skipTest("DPAPI unavailable")
        self.credentials.save_profile("zhiji", CLIENT_ID, CREDENTIAL)
        load = adapter_module.load_credentials
        self.assertEqual(load(str(self.config), "zhiji"), (CLIENT_ID, CREDENTIAL))
        self.assertEqual(load(str(self.config)), ("file-client", "file-secret"))
        self.assertEqual(load(None), ("env-client", "env-secret"))

    def test_missing_or_bad_profile_is_a_clean_adapter_error(self):
        for name, code in (("absent", "profile-not-found"), ("Bad Name", "invalid-profile-name")):
            with self.subTest(name=name):
                with self.assertRaises(AdapterError) as caught:
                    adapter_module.load_credentials(str(self.config), name)
                self.assertEqual(caught.exception.code, code)


class ProfileStartupTests(AdapterSmokeTests):
    """The real adapter process started with --profile pairs with the saved credential."""

    def setUp(self):
        super().setUp()
        import zhiji_mcp_credentials as credentials
        self.home = Path(self.root) / "credential-home"
        self.env = {**__import__("os").environ, credentials.HOME_ENV: str(self.home)}
        self.env.pop(adapter_module.CLIENT_ID_ENV, None)
        self.env.pop(adapter_module.CREDENTIAL_ENV, None)
        if not credentials.dpapi_available():
            self.skipTest("DPAPI unavailable")
        with mock.patch.dict(__import__("os").environ, {credentials.HOME_ENV: str(self.home)}):
            credentials.save_profile("zhiji-test", CLIENT_ID, CREDENTIAL)

    def test_profile_handshakes_and_missing_profile_fails_cleanly(self):
        adapter = self.spawn("--profile", "zhiji-test", "--config", str(self.root / "absent.json"), env=self.env)
        reply = adapter.request(1, "initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "zhiji-mcp")
        self.assertEqual(self.core.handshakes, 1)
        adapter.close()
        adapter.assert_stderr_clean(CREDENTIAL)
        missing = self.spawn("--profile", "nobody", env=self.env)
        self.assertEqual(missing.wait_quiet(), 1)
        time.sleep(0.1)
        self.assertTrue(any("profile-not-found" in line for line in missing.stderr), missing.stderr)

    # Inherited smoke tests are not repeated here.
    for _name in [name for name in dir(AdapterSmokeTests) if name.startswith("test_")]:
        locals()[_name] = None
    del _name


class WindowAndEventCatalogTests(unittest.TestCase):
    def test_open_window_modules_match_the_core_registry(self):
        sys.path.insert(0, str(SCRIPTS.parent / "bridge"))
        from core_ui_modules import openable_modules
        schema = adapter_module.TOOL_CATALOG["zhiji_open_window"]["inputSchema"]
        self.assertEqual(schema["properties"]["module"]["enum"], openable_modules())

    def test_descriptions_stay_compact(self):
        for name in ("zhiji_poll_events", "zhiji_open_window"):
            self.assertLessEqual(len(adapter_module.TOOL_CATALOG[name]["description"]), 120, name)

    def test_annotations(self):
        poll = adapter_module.tool_annotations("zhiji_poll_events")
        self.assertEqual((poll["readOnlyHint"], poll["destructiveHint"], poll["idempotentHint"]), (True, False, True))
        window = adapter_module.tool_annotations("zhiji_open_window")
        self.assertEqual((window["readOnlyHint"], window["idempotentHint"]), (False, True))

    def test_open_window_envelope(self):
        envelope = adapter_module.envelope
        self.assertEqual(envelope("zhiji_open_window", {"module": "app.main"}),
                         ("zhiji_open_window", {"module": "app.main"}, None, None, None))
        action, params, account, subjects, _request = envelope(
            "zhiji_open_window", {"module": "contact.overview", "accountId": "acc_1", "subjectId": "wxid_f"})
        self.assertEqual((params, account), ({"module": "contact.overview"}, "acc_1"))
        self.assertEqual(subjects, [{"accountId": "acc_1", "subjectType": "wechat_contact", "subjectId": "wxid_f"}])
        with self.assertRaises(AdapterError):
            envelope("zhiji_open_window", {"module": "contact.overview", "subjectId": "wxid_f"})
        self.assertEqual(envelope("zhiji_poll_events", {"waitSeconds": 5, "cursor": "c"}),
                         ("zhiji_poll_events", {"waitSeconds": 5, "cursor": "c"}, None, None, None))


if __name__ == "__main__":
    unittest.main()
