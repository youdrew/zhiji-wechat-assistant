"""The real gateway process driven by Core: credentials, tokens, usage ledger and probes."""
import hashlib
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_gateway  # noqa: E402
from core_gateway import GatewayUnavailable, ModelGatewayHost, _ControlChannel  # noqa: E402
from usage_ledger import UsageLedger  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SEEN = {}
HOLD = {}  # An Event here keeps the next completion request open until it is set.


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        SEEN["list-auth"] = self.headers.get("Authorization")
        body = json.dumps({"object": "list", "data": [{"id": "model-a", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        SEEN["auth"] = self.headers.get("Authorization")
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if HOLD.get("event") is not None:
            HOLD["event"].wait(10)
        body = json.dumps({"id": "x", "object": "chat.completion", "model": "model-a",
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": "好的"},
                                        "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass  # A stopped gateway closed this held request.


class GatewayHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{self.upstream.server_port}/v1"
        self.services = [{"id": "a" * 32, "protocol": "chat_completions", "baseUrl": base, "model": "model-a",
                          "contextTokens": 32768, "apiKey": "REAL_SECRET"}]
        self.ledger = UsageLedger(Path(self.temp.name) / "usage.sqlite3")
        self.host = ModelGatewayHost(ROOT, services=lambda: self.services, ledger=self.ledger)

    def tearDown(self):
        self.host.close()
        self.ledger.close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.temp.cleanup()

    def call(self, url, token):
        request = urllib.request.Request(url + "/chat/completions", method="POST",
                                         data=json.dumps({"model": "model-a", "messages": []}).encode(),
                                         headers={"Authorization": f"Bearer {token}",
                                                  "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def test_token_lifecycle_and_usage_ledger(self):
        digest = hashlib.sha256(b"exec-token").hexdigest()
        binding = {"plugin": "kyf.test", "pluginVersion": "1.0.0", "task": "task-1", "execution": "exec-1",
                   "generation": 1, "account": "acct", "user": "friend", "kind": "background"}
        self.host.token("add", digest, binding)
        service = self.host.services()[0]
        self.assertNotIn("apiKey", service)
        self.assertEqual(service["concurrency"], 4)
        reply = self.call(service["url"], "exec-token")
        self.assertEqual(reply["choices"][0]["message"]["content"], "好的")
        self.assertEqual(SEEN["auth"], "Bearer REAL_SECRET")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.ledger.summary({"account": "acct"})["requests"]:
            time.sleep(0.05)
            self.ledger.flush()
        summary = self.ledger.summary({"account": "acct"})
        self.assertEqual((summary["requests"], summary["inputTokens"], summary["outputTokens"]), (1, 5, 2))
        friends = self.ledger.by_friend({"account": "acct"})
        self.assertEqual((friends[0]["user"], friends[0]["scope"]), ("friend", "friend"))
        records = self.ledger.records({"account": "acct", "friend": "friend"})["records"]
        self.assertEqual(records[0]["plugin"], "kyf.test")
        self.host.token("revoke", digest, binding)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.call(service["url"], "exec-token")
        self.assertEqual(caught.exception.code, 401)
        caught.exception.close()

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.ledger.flush()
            rows = self.ledger.records({"account": "acct"})["records"]
            if predicate(rows):
                return rows
            time.sleep(0.05)
        self.fail("ledger did not reach the expected state")

    def test_in_flight_attempts_are_listed_until_their_usage_row_replaces_them(self):
        digest = hashlib.sha256(b"slow-token").hexdigest()
        self.host.token("add", digest, {"plugin": "kyf.test", "task": "task-slow", "execution": "exec-slow",
                                        "account": "acct", "user": "friend", "kind": "interactive"})
        url = self.host.services()[0]["url"]
        HOLD["event"] = threading.Event()
        self.addCleanup(HOLD.clear)
        caller = threading.Thread(target=self.call, args=(url, "slow-token"), daemon=True)
        caller.start()
        rows = self.wait_for(lambda rows: rows and rows[0]["status"] == "running")
        self.assertEqual((rows[0]["task"], rows[0]["user"], rows[0]["scope"], rows[0]["totalTokens"]),
                         ("task-slow", "friend", "friend", None))
        self.assertEqual(self.ledger.summary({"account": "acct"})["requests"], 0)
        HOLD["event"].set()
        caller.join(10)
        rows = self.wait_for(lambda rows: rows and rows[0]["status"] == "ok")
        self.assertEqual(len(rows), 1)
        self.assertEqual(self.ledger.active, {})

    def test_a_stopped_gateway_generation_takes_its_in_flight_attempts_with_it(self):
        digest = hashlib.sha256(b"crash-token").hexdigest()
        self.host.token("add", digest, {"plugin": "kyf.test", "task": "task-crash", "execution": "exec-crash",
                                        "account": "acct", "user": None, "kind": "background"})
        url = self.host.services()[0]["url"]
        HOLD["event"] = threading.Event()
        self.addCleanup(HOLD.clear)
        self.addCleanup(lambda: HOLD.get("event") and HOLD["event"].set())
        def call():
            try:
                self.call(url, "crash-token")
            except Exception:
                pass  # The connection ends with the stopped gateway.
        threading.Thread(target=call, daemon=True).start()
        self.wait_for(lambda rows: rows and rows[0]["status"] == "running")
        channel = self.host._channel
        self.host.process.kill()
        self.assertTrue(channel.cleaned.wait(10))
        self.wait_for(lambda rows: rows == [])

    def test_probe_lists_models_through_the_gateway(self):
        result = self.host.probe({"cmd": "list", "protocol": "chat_completions",
                                  "baseUrl": self.services[0]["baseUrl"], "apiKey": "PROBE_KEY"})
        self.assertEqual([item["id"] for item in result["models"]], ["model-a"])
        self.assertEqual(SEEN["list-auth"], "Bearer PROBE_KEY")

    def test_public_service_reports_saved_core_capacity_after_reconfigure(self):
        self.services[0]["concurrency"] = 3
        self.assertEqual(self.host.services()[0]["concurrency"], 3)
        self.services[0]["concurrency"] = 1
        self.host.reconfigure()
        self.assertEqual(self.host.services()[0]["concurrency"], 1)

    def test_public_service_metadata_includes_deadline_without_credentials(self):
        # The public shape is built in Python; no upstream inference is needed.
        with patch.object(self.host, "ensure", return_value=12345), patch.object(
                self.host, "_refresh_services", side_effect=lambda: self.services):
            service = self.host.services()[0]
            self.assertEqual(service["timeoutMs"], 3_600_000)
            self.services[0]["timeoutMs"] = 10_800_000
            changed = self.host.services()[0]
            self.assertEqual(changed["timeoutMs"], 10_800_000)
            self.assertNotIn("apiKey", changed)
            self.assertNotIn("REAL_SECRET", json.dumps(changed))

    def test_gateway_restart_replays_tokens(self):
        digest = hashlib.sha256(b"exec-token-2").hexdigest()
        self.host.token("add", digest, {"plugin": "kyf.test", "task": "t", "execution": "e2", "account": "acct",
                                        "user": None, "kind": "interactive"})
        self.host.ensure()
        previous = self.host._channel
        self.host.process.kill()
        self.host.process.wait(5)
        url = self.host.services()[0]["url"]
        self.assertEqual(self.call(url, "exec-token-2")["object"], "chat.completion")
        self.assertIsNot(self.host._channel, previous)
        self.assertTrue(previous.cleaned.wait(5))
        self.assertIsNone(previous.group.job)


class GatewayTransportTests(unittest.TestCase):
    """Synthetic subprocesses exercise pipe stalls; buffered input exercises stale generations."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = UsageLedger(Path(self.temp.name) / "usage.sqlite3")
        self.addCleanup(self.ledger.close)
        self.host = ModelGatewayHost(ROOT, services=lambda: [], ledger=self.ledger)
        self.addCleanup(self.host.close)

    def test_account_scoped_self_analysis_tokens_carry_self_without_a_fake_contact(self):
        cases = (("kyf.self-analysis", None, False, "self"),
                 ("kyf.test", None, False, "none"),
                 ("kyf.self-analysis", "friend", False, "friend"),
                 ("kyf.test", None, True, "multi"))
        with patch.object(self.host, "ensure"), patch.object(self.host, "_send") as send:
            for index, (plugin, user, multi, expected) in enumerate(cases):
                binding = {"plugin": plugin, "task": "task", "execution": f"exec-{index}",
                           "account": "acct", "user": user, "multi": multi}
                self.host.token("add", str(index), binding)
                outgoing = send.call_args.args[0]["binding"]
                self.assertEqual((outgoing["scope"], outgoing["user"]), (expected, user))
                self.assertEqual(self.host.scopes[binding["execution"]], expected)

    def start_nonreading_process(self):
        # A real pipe fills because this child never reads it. No model, socket,
        # credentials, or existing runtime data participates in this fixture.
        original = subprocess.Popen
        script = 'import time; print(\'{"type":"ready","port":45678}\', flush=True); time.sleep(60)'

        def launch(_args, **kwargs):
            return original([sys.executable, "-u", "-c", script], **kwargs)

        with patch.object(core_gateway, "node_executable", return_value=sys.executable), \
                patch.object(core_gateway.subprocess, "Popen", side_effect=launch):
            self.assertEqual(self.host.ensure(), 45678)
        return self.host._channel

    def buffered_channel(self, messages):
        process = Mock()
        process.stdin = io.BytesIO()
        process.stdout = io.BytesIO(b"".join(json.dumps(item).encode() + b"\n" for item in messages))
        process.poll.return_value = None
        process.wait.return_value = 0
        channel = _ControlChannel(process, Mock())
        channel.writer_done.set()
        self.host._channels.add(channel)
        return channel

    def activate(self, channel, port=45678):
        self.host.process, self.host.group, self.host._channel = channel.process, channel.group, channel
        self.host.port = channel.port = port
        self.host.ready.set()
        channel.ready.set()

    def test_hung_pipe_does_not_block_core_and_fails_pending_probe(self):
        with patch.object(core_gateway, "WRITE_TIMEOUT", .2):
            channel = self.start_nonreading_process()
            outcome = []
            done = threading.Event()

            def probe():
                try:
                    self.host.probe({"payload": "x" * (1024 * 1024)}, timeout=5)
                except Exception as exc:
                    outcome.append(exc)
                finally:
                    done.set()

            worker = threading.Thread(target=probe, daemon=True)
            worker.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with channel.lock:
                    writing = channel.writing_since is not None
                if writing:
                    break
                time.sleep(.005)
            self.assertTrue(writing, "the fixture never filled the child pipe")
            started = time.monotonic()
            self.host._send({"type": "token.revoke", "hash": "synthetic-stopped-task"})
            self.assertLess(time.monotonic() - started, .5, "Core waited on the blocked OS pipe")
            self.assertTrue(done.wait(3), "transport failure did not wake the probe")
            worker.join(1)
            self.assertEqual(len(outcome), 1)
            self.assertIsInstance(outcome[0], GatewayUnavailable)
            self.assertEqual(outcome[0].code, "gateway-write-timeout")
            self.assertTrue(channel.cleaned.wait(5))
            self.assertIsNotNone(channel.process.poll())
            self.assertTrue(channel.writer_done.is_set())
            self.assertEqual(channel.bytes, 0)
            self.assertEqual(self.host.pending, {})

    def test_control_backpressure_explicitly_fails_and_terminates_generation(self):
        channel = self.start_nonreading_process()
        with patch.object(core_gateway, "MAX_OUTBOUND_BYTES", 1024):
            with self.assertRaises(GatewayUnavailable) as caught:
                self.host._send({"type": "config", "payload": "x" * 1024})
        self.assertEqual(caught.exception.code, "gateway-backpressure")
        self.assertTrue(channel.cleaned.wait(5))
        self.assertIsNotNone(channel.process.poll())
        self.assertFalse(self.host.ready.is_set())
        self.assertEqual(channel.bytes, 0)

    def test_old_reader_cannot_ready_or_fail_the_new_generation_probe(self):
        old = self.buffered_channel([
            {"type": "ready", "port": 12345},
            {"type": "probe.result", "id": "new-probe", "result": {"from": "old"}},
            {"type": "usage", "record": {"attemptId": "old-late", "plugin": "kyf.test", "account": "acct",
                                           "scope": "multi", "status": "aborted", "inputTokens": 7}},
        ])
        current = self.buffered_channel([])
        self.activate(current, 54321)
        waiter = {"event": threading.Event(), "result": None, "error": None, "channel": current}
        self.host.pending["new-probe"] = waiter
        self.host._read(old)
        self.assertTrue(old.cleaned.wait(2))
        self.assertTrue(self.host.ready.is_set())
        self.assertEqual(self.host.port, 54321)
        self.assertFalse(waiter["event"].is_set())
        self.assertIsNone(waiter["result"])
        self.assertIsNone(waiter["error"])
        old.group.close.assert_called_once_with()
        self.ledger.flush()
        late = self.ledger.by_friend({"account": "acct"})
        self.assertEqual(len(late), 1)
        self.assertEqual((late[0]["scope"], late[0]["inputTokens"]), ("multi", 7))
        self.host.pending.clear()

    def test_revoked_multi_execution_keeps_late_usage_scope(self):
        record = {"attemptId": "late-multi", "plugin": "kyf.test", "task": "multi-task",
                  "execution": "multi-execution", "account": "acct", "user": None,
                  "scope": "multi", "status": "aborted", "inputTokens": 12, "outputTokens": 3}
        channel = self.buffered_channel([{"type": "usage", "record": record}])
        self.activate(channel)
        binding = {"plugin": "kyf.test", "task": "multi-task", "execution": "multi-execution",
                   "account": "acct", "multi": True}
        with patch.object(self.host, "_send") as send:
            self.host.token("add", "multi-digest", binding)
            self.assertEqual(send.call_args.args[0]["binding"]["scope"], "multi")
            self.host.token("revoke", "multi-digest", binding)
        self.assertNotIn("multi-digest", self.host.bindings)
        self.assertNotIn("multi-execution", self.host.scopes)
        self.host._read(channel)
        self.assertTrue(channel.cleaned.wait(2))
        self.ledger.flush()
        rows = self.ledger.by_friend({"account": "acct"})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scope"], "multi")
        self.assertEqual((rows[0]["inputTokens"], rows[0]["outputTokens"]), (12, 3))

    def test_ledger_failure_reports_stage_without_private_exception_text(self):
        channel = self.buffered_channel([{"type": "attempt", "attempt": {"attemptId": "synthetic"}}])
        self.activate(channel)
        with patch.object(self.ledger, "track", side_effect=ValueError("private request and secret")), \
                self.assertLogs("core_gateway", level="WARNING") as output:
            self.host._read(channel)
            self.assertTrue(channel.cleaned.wait(2))
        failure = self.host.failures[-1]
        self.assertEqual(set(failure), {"at", "code", "stage", "exception", "exitCode", "closing"})
        self.assertEqual((failure["code"], failure["stage"], failure["exception"]),
                         ("gateway-unavailable", "ledger-track", "ValueError"))
        self.assertNotIn("private request", json.dumps(failure) + str(output.output))
        self.assertNotIn("secret", json.dumps(failure) + str(output.output))

    def test_native_gateway_exit_is_reported_and_failure_history_is_bounded(self):
        channel = self.buffered_channel([])
        channel.process.poll.return_value = 3221226505
        self.activate(channel)
        with self.assertLogs("core_gateway", level="WARNING"):
            self.host._read(channel)
            self.assertTrue(channel.cleaned.wait(2))
        self.assertEqual(self.host.failures[-1]["exitCode"], 3221226505)
        self.assertEqual(self.host.failures[-1]["stage"], "stdout-eof")
        with self.assertLogs("core_gateway", level="WARNING"):
            for index in range(30):
                failed = self.buffered_channel([])
                failed.process.poll.return_value = index
                self.host._fail_channel(failed)
                self.assertTrue(failed.cleaned.wait(2))
        self.assertEqual(len(self.host.failures), 20)
        self.assertEqual([failure["exitCode"] for failure in self.host.failures], list(range(10, 30)))
        for failure in self.host.failures:
            self.assertEqual(set(failure), {"at", "code", "stage", "exception", "exitCode", "closing"})

    def test_normal_close_neither_records_nor_logs_a_failure(self):
        channel = self.buffered_channel([])
        self.activate(channel)
        with self.assertNoLogs("core_gateway", level="WARNING"):
            self.host.close()
        self.assertTrue(channel.cleaned.is_set())
        self.assertIsNone(channel.failure)
        self.assertEqual(list(self.host.failures), [])

    def test_failure_is_published_as_a_final_snapshot_after_cleanup(self):
        channel = self.buffered_channel([])
        entered, finish = threading.Event(), threading.Event()

        def cleanup():
            entered.set()
            finish.wait(2)
            channel.process.poll.return_value = 3221226505

        channel.group.close.side_effect = cleanup
        with self.assertLogs("core_gateway", level="WARNING"):
            self.host._fail_channel(channel)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(list(self.host.failures), [])
                initial = channel.failure
                self.assertIsNone(initial["exitCode"])
            finally:
                finish.set()
            self.assertTrue(channel.cleaned.wait(2))
        self.assertIsNone(initial["exitCode"], "cleanup must not mutate an earlier snapshot")
        self.assertIsNot(initial, self.host.failures[-1])
        self.assertEqual(self.host.failures[-1]["exitCode"], 3221226505)

    def test_cleanup_timeout_does_not_erase_a_known_exit_code(self):
        channel = self.buffered_channel([])
        channel.process.poll.side_effect = [3221226505, None]
        channel.process.wait.side_effect = subprocess.TimeoutExpired("synthetic", 5)
        with self.assertLogs("core_gateway", level="WARNING"):
            self.host._fail_channel(channel)
            self.assertTrue(channel.cleaned.wait(2))
        self.assertEqual(self.host.failures[-1]["exitCode"], 3221226505)


if __name__ == "__main__":
    unittest.main()
