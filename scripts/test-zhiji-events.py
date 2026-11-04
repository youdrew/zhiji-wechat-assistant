"""zhiji-events CLI against a synthetic loopback Core (health, handshake, zhiji_poll_events)."""

import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

SCRIPTS = Path(__file__).resolve().parent
CLI = SCRIPTS / "zhiji-events.py"
sys.path.insert(0, str(SCRIPTS))

from mcp_test_ports import bindable_install_dir  # noqa: E402

_LAUNCHER_SPEC = importlib.util.spec_from_file_location("real_client_launcher_events_test",
                                                        SCRIPTS / "start-real-client.py")
launcher = importlib.util.module_from_spec(_LAUNCHER_SPEC)
_LAUNCHER_SPEC.loader.exec_module(launcher)
_CLI_SPEC = importlib.util.spec_from_file_location("zhiji_events_cli", CLI)
cli = importlib.util.module_from_spec(_CLI_SPEC)
_CLI_SPEC.loader.exec_module(cli)

CLIENT_ID = "mcp_eventtest"
CREDENTIAL = "zmc_" + "b" * 64


def token_for(version):
    return "zmt_" + str(version + 1) * 64


TOKEN = token_for(1)


def message_event(seq, subject, count=1, exact=True):
    return {"eventId": f"ev_{seq:024d}", "seq": seq, "type": "message.new", "accountId": "acc_1",
            "subject": {"accountId": "acc_1", "subjectType": "wechat_contact", "subjectId": subject},
            "isGroup": False, "newCount": count, "countExact": exact, "firstTime": 1_800_000_000_000,
            "lastTime": 1_800_000_000_000, "observedAt": 1_800_000_005_000, "catchUp": False}


class FakeCore:
    """Server semantics of zhiji_poll_events reduced to integer cursors and consumer acks."""

    def __init__(self):
        self.instance_id = None
        self.events = []
        self.acked = {}          # consumer -> seq
        self.calls = []
        self.stale_once = False
        self.version = 1
        self.revoked = False
        self.handshakes = 0
        self.lock = threading.Lock()

    def bump(self, revoke=False):
        """Authorization changed (capability shrink/expand, or revocation): old tokens are stale."""
        with self.lock:
            self.version += 1
            self.revoked = revoke
            self.stale_once = True

    def handle(self, path, body, headers):
        if path == "/api/health":
            return 200, {"version": launcher.VERSION, "instanceId": self.instance_id, "appVersion": "0.0.0-test"}
        if path == "/api/mcp/handshake":
            self.handshakes += 1
            if body.get("clientId") != CLIENT_ID or body.get("credential") != CREDENTIAL or self.revoked:
                return 401, {"error": "mcp-unauthorized", "message": "客户端凭据无效"}
            return 200, {"token": token_for(self.version), "clientId": CLIENT_ID, "authVersion": self.version,
                         "expiresAtMs": 0}
        if path == "/api/mcp/rpc":
            bearer = headers.get("Authorization") or ""
            if bearer != "Bearer " + token_for(self.version):
                if any(bearer == "Bearer " + token_for(old) for old in range(1, self.version)):
                    return 403, {"error": "stale-authorization", "message": "授权已变化，请重新配对后调用"}
                return 401, {"error": "mcp-unauthorized", "message": "令牌无效"}
            if body.get("action") != "zhiji_poll_events":
                return 404, {"error": "unknown-action", "message": "未知能力"}
            params = body.get("params") or {}
            with self.lock:
                self.calls.append(params)
                if self.stale_once and "cursor" in params:
                    self.stale_once = False
                    # Like the server: a new authorization version also voids saved positions.
                    self.acked.clear()
                    return 409, {"error": "cursor-stale", "message": "授权已变更"}
                consumer = params["consumer"]
                head = self.events[-1]["seq"] if self.events else 0
                if "cursor" in params:
                    start = int(params["cursor"])
                elif params.get("resume") and consumer in self.acked:
                    start = self.acked[consumer]
                else:
                    self.acked[consumer] = head
                    return 200, {"ok": True, "result": {"events": [], "cursor": str(head), "baseline": True,
                                                        "gap": False, "source": {"state": "live"}}}
                self.acked[consumer] = start
                fresh = [event for event in self.events if event["seq"] > start][:2]
            if not fresh and params.get("waitSeconds"):
                time.sleep(0.2)
            position = fresh[-1]["seq"] if fresh else start
            return 200, {"ok": True, "result": {"events": fresh, "cursor": str(position), "baseline": False,
                                                "gap": False, "source": {"state": "live"}}}
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


class CliTests(unittest.TestCase):
    def setUp(self):
        # The port derives from the install path; Windows reserves some ranges (WinError
        # 10013), so pick a temporary root whose port can actually be bound.
        self.install = bindable_install_dir(launcher.default_port)
        self.addCleanup(self.install.cleanup)
        self.root = Path(self.install.name)
        (self.root / "package.json").write_text(json.dumps({"version": "0.0.0-test"}), encoding="utf-8")
        (self.root / ".local").mkdir()
        self.config = self.root / "client.json"
        self.config.write_text(json.dumps({"clientId": CLIENT_ID, "credential": CREDENTIAL}), encoding="utf-8")
        self.core = FakeCore()
        self.core.instance_id = launcher.instance_id(self.root)

    def serve(self):
        server = ThreadingHTTPServer(("127.0.0.1", launcher.default_port(self.root)), make_handler(self.core))
        server.handle_error = lambda *_args: None  # a killed --follow client resets its socket
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def run_cli(self, *args, env_credentials=False):
        env = dict(os.environ)
        env.pop(cli.CLIENT_ID_ENV, None)
        env.pop(cli.CREDENTIAL_ENV, None)
        env["PYTHONIOENCODING"] = "utf-8"
        extra = []
        if env_credentials:
            env[cli.CLIENT_ID_ENV], env[cli.CREDENTIAL_ENV] = CLIENT_ID, CREDENTIAL
        else:
            extra = ["--config", str(self.config)]
        process = subprocess.run([sys.executable, str(CLI), "--root", str(self.root), *extra, *args],
                                 capture_output=True, env=env, timeout=60)
        return process.returncode, process.stdout.decode("utf-8"), process.stderr.decode("utf-8")

    def test_once_prints_pending_events_as_jsonl_and_acknowledges_them(self):
        self.serve()
        self.core.acked["cli"] = 0
        self.core.events = [message_event(1, "friend_a", 2), message_event(2, "friend_b"),
                            message_event(3, "friend_c")]
        code, out, err = self.run_cli("--once")
        self.assertEqual(code, 0, err)
        lines = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([event["seq"] for event in lines], [1, 2, 3])
        self.assertEqual(self.core.calls[0], {"waitSeconds": 0, "consumer": "cli", "resume": True})
        self.assertEqual(self.core.calls[1]["cursor"], "2")
        self.assertEqual(self.core.acked["cli"], 3)
        self.assertNotIn(CREDENTIAL, out + err)
        # Acknowledged: the next run prints nothing.
        code, out, _ = self.run_cli("--once", env_credentials=True)
        self.assertEqual((code, out), (0, ""))

    def test_first_run_is_a_silent_baseline(self):
        self.serve()
        self.core.events = [message_event(1, "friend_a")]
        code, out, _ = self.run_cli("--format", "claude-hook")
        self.assertEqual((code, out), (0, ""))
        self.assertEqual(self.core.acked["cli"], 1)

    def test_claude_hook_shape_and_silence_when_empty(self):
        self.serve()
        self.core.acked["hook"] = 0
        self.core.events = [message_event(1, "friend_a", 2), message_event(2, "friend_b", 1)]
        code, out, err = self.run_cli("--format", "claude-hook", "--consumer", "hook")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        context = payload["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(context.startswith("知己：2 个会话有新消息"))
        self.assertIn("acc_1/friend_a ×2", context)
        self.assertIn("需要时用 zhiji 工具读取", context)
        code, out, _ = self.run_cli("--format", "claude-hook", "--consumer", "hook")
        self.assertEqual((code, out), (0, ""))

    def test_hook_retry_shares_fetch_budget_and_ack_can_time_out_after_output(self):
        out = io.StringIO()
        core = cli.Core(self.root, CLIENT_ID, CREDENTIAL)
        core.url = "http://127.0.0.1:1"  # _post below is the whole transport for this test.
        clock = 100.0
        handshakes = 0
        timeouts = []

        def post(path, body, timeout, token=None):
            nonlocal clock, handshakes
            timeouts.append(timeout)
            if path == "/api/mcp/handshake":
                handshakes += 1
                clock += 1
                return {"token": token_for(handshakes)}
            if token == token_for(1):
                clock += 2
                raise HTTPError(core.url + path, 401, "expired", {}, io.BytesIO(b"{}"))
            params = body["params"]
            if "cursor" not in params:
                clock += 0.5
                return {"events": [message_event(1, "synthetic")], "cursor": "ack-this"}
            self.assertEqual(params["cursor"], "ack-this")
            self.assertIn("synthetic", json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])
            clock += timeout
            raise socket.timeout()

        with mock.patch.object(cli.time, "monotonic", side_effect=lambda: clock), \
                mock.patch.object(core, "_post", side_effect=post):
            self.assertEqual(cli.run_once(core, "synthetic-hook", "claude-hook", 0, out), 0)
        self.assertEqual(timeouts, [5, 4, 2, 1, 2])
        self.assertEqual(len(out.getvalue().splitlines()), 1)

    def test_text_format(self):
        self.serve()
        self.core.acked["cli"] = 0
        self.core.events = [message_event(1, "friend_a", 3, exact=False)]
        code, out, _ = self.run_cli("--format", "text")
        self.assertEqual(code, 0)
        self.assertIn("会话 acc_1/friend_a：约 3 条新消息", out)

    def test_core_down_prints_notice_and_never_starts_core(self):
        started = time.monotonic()
        code, out, err = self.run_cli("--once")
        self.assertEqual((code, out.strip()), (0, "知己未运行"))
        self.assertLess(time.monotonic() - started, 30)
        self.assertFalse((self.root / ".local" / "real-client-runtime").exists())
        code, out, err = self.run_cli("--format", "claude-hook")
        self.assertEqual((code, out), (0, ""))
        self.assertIn("知己未运行", err)

    def test_missing_credentials_fail_without_blocking_exit_code(self):
        self.serve()
        env = dict(os.environ)
        env.pop(cli.CLIENT_ID_ENV, None)
        env.pop(cli.CREDENTIAL_ENV, None)
        process = subprocess.run([sys.executable, str(CLI), "--root", str(self.root)], capture_output=True,
                                 env=env, timeout=60)
        self.assertEqual(process.returncode, 1)

    # The hook fallback uses the DPAPI connection saved by `zhiji-mcp-cli.py connect`.
    def test_profile_credentials_take_precedence_like_the_adapter(self):
        import zhiji_mcp_credentials as credentials
        if not credentials.dpapi_available():
            self.skipTest("DPAPI unavailable")
        home = self.root / "credential-home"
        env = {credentials.HOME_ENV: str(home), cli.CLIENT_ID_ENV: "env-client", cli.CREDENTIAL_ENV: "env-secret"}
        with mock.patch.dict(os.environ, env):
            credentials.save_profile("claude-code", CLIENT_ID, CREDENTIAL)
            other = self.root / "other.json"
            other.write_text(json.dumps({"clientId": "file-client", "credential": "file-secret"}), encoding="utf-8")
            self.assertEqual(cli.load_credentials(str(other), "claude-code"), (CLIENT_ID, CREDENTIAL))
            self.assertEqual(cli.load_credentials(str(other)), ("file-client", "file-secret"))
            self.assertEqual(cli.load_credentials(None), ("env-client", "env-secret"))
            for name, code in (("absent", "profile-not-found"), ("Bad Name", "invalid-profile-name")):
                with self.assertRaises(cli.CliError) as caught:
                    cli.load_credentials(str(other), name)
                self.assertEqual(caught.exception.code, code)
        # The real process: --profile alone is enough for the Claude Code hook.
        self.serve()
        self.core.acked["claude-hook"] = 0
        self.core.events = [message_event(1, "friend_a", 2)]
        process = subprocess.run(
            [sys.executable, str(CLI), "--root", str(self.root), "--once", "--format", "claude-hook",
             "--profile", "claude-code", "--consumer", "claude-hook"],
            capture_output=True, env=self.clean_env(**{credentials.HOME_ENV: str(home)}), timeout=60)
        out, err = process.stdout.decode("utf-8"), process.stderr.decode("utf-8")
        self.assertEqual(process.returncode, 0, err)
        self.assertIn("acc_1/friend_a ×2", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        self.assertNotIn(CREDENTIAL, out + err)
        missing = subprocess.run([sys.executable, str(CLI), "--root", str(self.root), "--profile", "nobody"],
                                 capture_output=True, env=self.clean_env(**{credentials.HOME_ENV: str(home)}),
                                 timeout=60)
        self.assertEqual(missing.returncode, 1)
        self.assertIn("profile-not-found", missing.stderr.decode("utf-8"))

    def clean_env(self, **extra):
        env = dict(os.environ)
        env.pop(cli.CLIENT_ID_ENV, None)
        env.pop(cli.CREDENTIAL_ENV, None)
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(extra)
        return env

    def test_invalid_format_is_rejected(self):
        code, _, err = self.run_cli("--format", "xml")
        self.assertEqual(code, 1)
        self.assertIn("invalid-args", err)

    def test_follow_streams_jsonl(self):
        self.serve()
        self.core.acked["cli"] = 0
        self.core.events = [message_event(1, "friend_a")]
        process = subprocess.Popen([sys.executable, str(CLI), "--root", str(self.root), "--config",
                                    str(self.config), "--follow"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(process.stderr.close)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait, 10)
        self.addCleanup(process.kill)
        first = json.loads(process.stdout.readline().decode("utf-8"))
        self.assertEqual(first["seq"], 1)
        with self.core.lock:
            self.core.events.append(message_event(2, "friend_b"))
        second = json.loads(process.stdout.readline().decode("utf-8"))
        self.assertEqual(second["seq"], 2)
        self.assertTrue(all(call["waitSeconds"] == 25 for call in self.core.calls))

    def test_follow_rebaselines_after_cursor_stale(self):
        self.serve()
        self.core.acked["cli"] = 0
        self.core.events = [message_event(1, "friend_a")]
        self.core.stale_once = True
        core = cli.Core(self.root, CLIENT_ID, CREDENTIAL)
        out = io.StringIO()
        rounds = iter(range(4))
        cli.run_follow(core, "cli", "jsonl", out, stop=lambda: next(rounds, None) is None)
        self.assertEqual([json.loads(line)["seq"] for line in out.getvalue().splitlines()], [1])
        # After the stale cursor the next call drops it; the server ignores resume across versions.
        stale_index = next(i for i, call in enumerate(self.core.calls) if "cursor" in call)
        self.assertEqual(self.core.calls[stale_index + 1], {"waitSeconds": 25, "consumer": "cli", "resume": True})

    # R2-02: an authorization version change (403 stale-authorization) re-pairs once and keeps following.
    def test_follow_survives_capability_changes(self):
        self.serve()
        self.core.acked["cli"] = 0
        self.core.events = [message_event(1, "friend_a")]
        core = cli.Core(self.root, CLIENT_ID, CREDENTIAL)
        out = io.StringIO()
        script = {1: self.core.bump,                                   # shrink after the first page
                  3: lambda: self.core.events.append(message_event(2, "friend_b")),
                  5: self.core.bump,                                   # expand again
                  7: lambda: self.core.events.append(message_event(3, "friend_c")),
                  9: None}
        counter = iter(range(100))

        def stop():
            step = next(counter)
            if step in script:
                if script[step] is None:
                    return True
                script[step]()
            return False

        self.assertEqual(cli.run_follow(core, "cli", "jsonl", out, stop=stop), 0)
        self.assertEqual([json.loads(line)["seq"] for line in out.getvalue().splitlines()], [1, 2, 3])
        self.assertEqual(self.core.handshakes, 3)  # initial + one bounded re-pairing per change

    def test_follow_exits_on_revocation(self):
        self.serve()
        self.core.acked["cli"] = 0
        self.core.events = [message_event(1, "friend_a")]
        core = cli.Core(self.root, CLIENT_ID, CREDENTIAL)
        out = io.StringIO()
        counter = iter(range(100))

        def stop():
            if next(counter) == 1:
                self.core.bump(revoke=True)
            return False

        with self.assertRaises(cli.CliError) as caught:
            cli.run_follow(core, "cli", "jsonl", out, stop=stop)
        self.assertEqual(caught.exception.code, "mcp-unauthorized")
        self.assertEqual(self.core.handshakes, 2)

    # P2-9: --once pages are capped; the printed pages are acknowledged and the rest is announced.
    def test_once_page_cap_never_repeats_and_announces_the_rest(self):
        self.serve()
        self.core.acked["cli"] = 0
        self.core.events = [message_event(seq, f"friend_{seq}") for seq in range(1, 51)]  # 25 pages of 2
        code, out, err = self.run_cli("--once")
        self.assertEqual(code, 0, err)
        first = [json.loads(line)["seq"] for line in out.splitlines()]
        self.assertEqual(first, list(range(1, 41)))
        self.assertEqual(self.core.acked["cli"], 40)
        self.assertIn("还有未读完的事件", err)
        code, out, err = self.run_cli("--once")
        second = [json.loads(line)["seq"] for line in out.splitlines()]
        self.assertEqual(second, list(range(41, 51)))
        self.assertEqual(self.core.acked["cli"], 50)
        self.assertNotIn("还有未读完的事件", err)


if __name__ == "__main__":
    unittest.main()
