"""Synthetic advisor transport and Node framing; no engine, model or WeChat data."""
import hashlib
import io
import json
import os
import queue
import shutil
import subprocess
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import engine_client
from engine_client import EngineClient, EngineError


class FakeProcess:
    def __init__(self):
        self.output = queue.Queue()
        self.stdout = iter(self.output.get, None)
        self.stdin = None

    def poll(self):
        return None

    def emit(self, envelope):
        self.output.put(json.dumps(envelope) + "\n")


class EngineClientTransportTests(unittest.TestCase):
    def test_unicode_line_separators_remain_inside_one_json_command(self):
        runtime = EngineClient(".")
        stream = io.StringIO()
        runtime.process = SimpleNamespace(stdin=stream, poll=lambda: None)
        command = {"id": "synthetic-separators", "cmd": "respond", "payload": {
            "contextFileText": "正文\u2028下一行\u2029下一段\n正常换行\r回车😀\\u2028",
            "nested\u2029key": ["\u2028\u2029", "literal \\u2029"],
        }}

        runtime._send(command)

        wire = stream.getvalue()
        self.assertTrue(wire.endswith("\n"))
        self.assertEqual(len(wire.splitlines()), 1)
        self.assertNotIn("\u2028", wire)
        self.assertNotIn("\u2029", wire)
        self.assertEqual(json.loads(wire), command)

    def test_large_unicode_command_survives_real_node_readline_with_followup(self):
        node = os.environ.get("KYF_NODE") or shutil.which("node")
        if not node:
            self.skipTest("Node is unavailable for the cross-language framing check")
        runtime = EngineClient(".")
        stream = io.StringIO()
        runtime.process = SimpleNamespace(stdin=stream, poll=lambda: None)
        text = "合成资料😀" * 40_000 + "\u2028\u2029\u2028\u2029\u2028"
        for identifier, content in (("large", text), ("followup", "下一条合成命令")):
            runtime._send({"id": identifier, "cmd": "respond", "payload": {"contextFileText": content}})
        # Use the worker's actual line reader options. A valid JSON string can
        # still be split at a literal U+2028/U+2029 before JSON.parse sees it.
        script = """
const readline = require('node:readline');
const { createHash } = require('node:crypto');
const lines = readline.createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false });
lines.on('line', raw => {
  try {
    const value = JSON.parse(raw);
    console.log(JSON.stringify({ id: value.id,
      digest: createHash('sha256').update(value.payload.contextFileText, 'utf8').digest('hex') }));
  } catch { console.log(JSON.stringify({ invalidFrame: true })); }
});
"""
        result = subprocess.run([node, "-e", script], input=stream.getvalue(), text=True, encoding="utf-8",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([json.loads(line) for line in result.stdout.splitlines()], [
            {"id": identifier, "digest": hashlib.sha256(content.encode("utf-8")).hexdigest()}
            for identifier, content in (("large", text), ("followup", "下一条合成命令"))
        ])

    def runtime(self):
        runtime = EngineClient(".")
        process = FakeProcess()
        runtime.process = process
        runtime._start = lambda: None
        sent = queue.Queue()
        runtime._send = sent.put
        threading.Thread(target=runtime._read, args=(process,), daemon=True).start()
        self.addCleanup(process.output.put, None)
        return runtime, process, sent

    @staticmethod
    def call(runtime, on_event, outcome, key, cancel=None, method="respond", service=None):
        def run():
            try:
                outcome[key] = getattr(runtime, method)(service or {}, "synthetic-token", {}, on_event, cancel or threading.Event())
            except EngineError as exc:
                outcome[key] = exc.code
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_blocked_event_callback_cannot_stall_other_runs(self):
        runtime, process, sent = self.runtime()
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def blocked(_event):
            entered.set()
            release.wait(5)

        outcome = {}
        first = self.call(runtime, blocked, outcome, "first")
        first_id = sent.get(timeout=1)["id"]
        second = self.call(runtime, lambda _event: None, outcome, "second")
        second_id = sent.get(timeout=1)["id"]
        process.emit({"id": first_id, "event": {"type": "text", "text": "a"}})
        self.assertTrue(entered.wait(1))
        process.emit({"id": second_id, "result": {"text": "b"}})
        second.join(1)
        self.assertEqual(outcome.get("second"), {"text": "b"})
        release.set()
        process.emit({"id": first_id, "result": {"text": "a"}})
        first.join(1)
        self.assertEqual(outcome.get("first"), {"text": "a"})

    def test_events_reach_the_caller_thread_in_order_before_the_result(self):
        runtime, process, sent = self.runtime()
        seen, outcome = [], {}
        thread = self.call(runtime, lambda event: seen.append((event["text"], threading.get_ident())), outcome, "result")
        identifier = sent.get(timeout=1)["id"]
        for text in ("one", "two"):
            process.emit({"id": identifier, "event": {"type": "text", "text": text}})
        process.emit({"id": identifier, "result": {"text": "done"}})
        thread.join(2)
        self.assertEqual(outcome, {"result": {"text": "done"}})
        self.assertEqual([text for text, _ident in seen], ["one", "two"])
        self.assertEqual({ident for _text, ident in seen}, {thread.ident})

    def test_cancelled_call_still_records_a_queued_native_session(self):
        runtime, process, sent = self.runtime()
        cancel, seen, outcome = threading.Event(), [], {}
        thread = self.call(runtime, seen.append, outcome, "result", cancel)
        identifier = sent.get(timeout=1)["id"]
        cancel.set()
        self.assertEqual(sent.get(timeout=1)["cmd"], "cancel")
        created = {"type": "status", "state": "native-session-created", "runtimeSessionId": "ses_synthetic"}
        process.emit({"id": identifier, "event": created})
        process.emit({"id": identifier, "error": {"code": "cancelled"}})
        thread.join(2)
        self.assertEqual(outcome, {"result": "advisor-cancelled"})
        self.assertEqual(seen, [created])

    def test_all_model_commands_outlast_five_minutes_and_respect_configured_deadline(self):
        runtime, process, sent = self.runtime()
        now, outcome = [0.0], {}
        with patch.object(engine_client, "time", SimpleNamespace(monotonic=lambda: now[0])):
            calls = []
            for method in ("respond", "compact", "summarize_discussion", "compact_session", "inspect_media"):
                thread = self.call(runtime, lambda _event: None, outcome, method, method=method,
                                   service={"timeoutMs": 7_200_000})
                identifier = sent.get(timeout=1)["id"]
                calls.append((method, identifier, thread))
            now[0] = 301.0
            for method, identifier, thread in calls[:-1]:
                process.emit({"id": identifier, "result": {"text": "late but whole"}})
                thread.join(1)
                self.assertEqual(outcome.get(method), {"text": "late but whole"})
            now[0] = 7_200 + engine_client.MODEL_CLEANUP_SECONDS + 1
            calls[-1][2].join(2)
            self.assertEqual(outcome.get("inspect_media"), "advisor-timeout")
            self.assertEqual(sent.get(timeout=1)["cmd"], "cancel")

    def test_missing_timeout_uses_one_hour_and_invalid_timeout_never_dispatches(self):
        runtime, process, sent = self.runtime()
        now, outcome = [0.0], {}
        with patch.object(engine_client, "time", SimpleNamespace(monotonic=lambda: now[0])):
            call = self.call(runtime, lambda _: None, outcome, "default", method="compact")
            sent.get(timeout=1)
            now[0] = 3600 + engine_client.MODEL_CLEANUP_SECONDS + 1
            call.join(2)
            self.assertEqual(outcome.get("default"), "advisor-timeout")
            self.assertEqual(sent.get(timeout=1)["cmd"], "cancel")
        for value in (True, 0, 59_999, 10_800_001, "3600000"):
            with self.subTest(value=value), self.assertRaises(EngineError) as caught:
                runtime.compact({"timeoutMs": value}, "synthetic-token", {}, lambda _: None, threading.Event())
            self.assertEqual(caught.exception.code, "invalid-request")
        self.assertTrue(sent.empty())


if __name__ == "__main__":
    unittest.main()
