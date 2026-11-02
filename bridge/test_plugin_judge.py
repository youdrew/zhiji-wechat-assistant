"""Python judge calls use the real shared Node worker with synthetic loopback data."""
# Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
import json
import os
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugin-sdk" / "python"))
sys.path.insert(0, str(ROOT / "bridge"))
from core_runtime import node_executable
from kyf_plugin.models import ModelClient, ModelError
from kyf_plugin.channel import Cancelled

QUESTIONS = {"has_content": {"type": "noul", "instructions": "是否提及具体经历？"}}


class Task:
    token = "synthetic-execution-token"

    def __init__(self):
        self.cancelled = threading.Event()

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((self.path, self.headers.get("Authorization"), data))
        if self.server.status != 200:
            body = json.dumps({"error": {"code": "execution-revoked" if self.server.status == 409 else "private-diagnostics"}}).encode()
        else:
            result = {"answers": {"has_content": {"type": "noul", "noul": self.server.probability}}}
            if self.path.endswith("chat/completions"):
                result = {"choices": [{"message": {"content": json.dumps(result)}, "finish_reason": "stop"}]}
            body = json.dumps(result).encode()
        self.send_response(self.server.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PluginJudgeTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.server.requests, self.server.status, self.server.probability = [], 200, 0.75
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.services = [{"id": "synthetic-main", "url": "http://127.0.0.1:1", "model": "main", "roles": ["main"]},
                         {"id": "synthetic-judge", "url": f"http://127.0.0.1:{self.server.server_port}/s/synthetic",
                          "model": "judge", "protocol": "systemone", "roles": ["judge"], "fingerprint": "judge-fp"}]
        self.client = ModelClient(SimpleNamespace(core=SimpleNamespace(model_services=lambda: {"services": self.services})))
        self.environment = patch.dict(os.environ, {"KYF_NODE": node_executable(ROOT),
                                     "KYF_TSX_LOADER": (ROOT / "node_modules" / "tsx" / "dist" / "loader.mjs").as_uri(),
                                     "KYF_PLUGIN_SDK": str(ROOT / "plugin-sdk")})
        self.environment.start()

    def tearDown(self):
        process = self.client.process
        self.client.close()
        if process is not None:
            process.stdout.close()
        self.server.shutdown()
        self.server.server_close()
        self.environment.stop()

    def test_native_and_generation_judge_share_worker_and_route_to_assigned_purpose(self):
        task = Task()
        native = self.client.judge(task, "昨天我搬家了。", QUESTIONS, step="judge-window-filter")
        worker = self.client.process
        self.assertEqual(native, {"answers": {"has_content": {"type": "noul", "noul": 0.75}},
                                  "engine": "systemone", "fingerprint": "judge-fp"})
        self.services[1]["protocol"] = "chat_completions"
        generated = self.client.judge(task, "昨天我搬家了。", QUESTIONS, step="judge-window-filter")
        self.assertIs(self.client.process, worker)
        self.assertEqual(generated["answers"], native["answers"])
        self.assertEqual(generated["engine"], "generate")
        self.assertEqual([request[0] for request in self.server.requests],
                         ["/s/synthetic~judge-window-filter/v1/systemone", "/s/synthetic~judge-window-filter/chat/completions"])
        self.assertEqual([request[1] for request in self.server.requests], ["Bearer synthetic-execution-token"] * 2)

    def test_invalid_output_and_state_limit_reach_python_without_provider_details(self):
        self.server.probability = True
        with self.assertRaises(ModelError) as caught:
            self.client.judge(Task(), "合成窗口", QUESTIONS)
        self.assertEqual(caught.exception.code, "judge-invalid-output")
        with self.assertRaises(ModelError) as caught:
            self.client.judge(Task(), "中" * 6001, QUESTIONS)
        self.assertEqual(caught.exception.code, "judge-state-too-long")
        self.server.status = 501
        with self.assertRaises(ModelError) as caught:
            self.client.judge(Task(), "合成窗口", QUESTIONS)
        self.assertEqual((caught.exception.code, caught.exception.http_status), ("judge-unavailable", 501))
        self.assertNotIn("private-diagnostics", caught.exception.message)

    def test_unconfigured_and_stopped_tasks_do_not_start_worker(self):
        self.services.clear()
        with self.assertRaises(ModelError) as caught:
            self.client.judge(Task(), "合成窗口", QUESTIONS)
        self.assertEqual(caught.exception.code, "judge-not-configured")
        task = Task()
        task.cancelled.set()
        with self.assertRaises(Cancelled):
            self.client.judge(task, "合成窗口", QUESTIONS)
        self.assertIsNone(self.client.process)

    def test_revoked_judge_is_cancellation_even_before_task_event_arrives(self):
        self.server.status = 409
        with self.assertRaises(Cancelled):
            self.client.judge(Task(), "合成窗口", QUESTIONS)


if __name__ == "__main__":
    unittest.main()
