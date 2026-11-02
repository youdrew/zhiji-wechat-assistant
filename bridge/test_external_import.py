"""Synthetic integration tests: no real chat data or configured model is accessed."""
from __future__ import annotations

import contextlib
import http.client
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from PIL import Image
from core_external_import import ExternalImportService
from core_external_media import evidence_frames, inspect_material, model_image
from core_tasks import TaskError
from model_source import MAX_MODEL_TIMEOUT_MS


class Tasks:
    def __init__(self):
        self.rows, self.tokens = {}, set()

    def create(self, plugin, **params):
        identifier = str(len(self.rows) + 1)
        task = {"id": identifier, "state": "running", "plugin": plugin, **params}
        self.rows[identifier] = task
        self.tokens.add(identifier)
        return {"task": task, "execution": {"id": identifier, "token": identifier}}

    def verify(self, token, plugin, task_id=None):
        if token not in self.tokens:
            raise TaskError("execution-revoked", "stopped", 409)

    @contextlib.contextmanager
    def submission(self, token, plugin, task_id=None):
        self.verify(token, plugin, task_id)
        yield

    def progress(self, token, plugin, task_id, progress):
        self.verify(token, plugin, task_id)
        self.rows[task_id]["progress"] = progress

    def task(self, task_id):
        return self.rows[task_id]

    def finish(self, token, plugin, task_id, state, error=None):
        self.verify(token, plugin, task_id)
        self._close(task_id, state)

    def _close(self, task_id, state, error=None):
        self.tokens.discard(task_id)
        self.rows[task_id]["state"] = state

    def stop(self, task_id):
        self._close(task_id, "stopping")


class Supervisor:
    def __init__(self):
        self.calls = 0
        self.block = None
        self.entered = threading.Event()
        self.simulated_recognition_seconds = 0
        self.recognition_timeouts = []

    def rpc(self, plugin, method, params, context, timeout=None):
        if method == "transcription.plan":
            return {"schema": 1, "screenshot": {"tileHeight": 800, "overlap": 200}}
        self.calls += 1
        self.recognition_timeouts.append(timeout)
        if timeout < self.simulated_recognition_seconds:
            raise TimeoutError("synthetic recognition outlasted its RPC deadline")
        self.entered.set()
        if self.block:
            self.block.wait(5)
        return {"schema": 1, "messages": [{"text": f"合成消息{self.calls}", "sender": "other", "issues": [],
            "timeLabel": None, "timestamp": None, "kind": "text"}, {"text": "缺□", "sender": "unknown", "issues": ["unclear_text"]}]}


class ExternalImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.core = SimpleNamespace(tasks=Tasks(), supervisor=Supervisor(), backend=SimpleNamespace(),
            registry=SimpleNamespace(enabled=lambda _: True, get=lambda _: {"name": "导入"}, dependency_error=lambda _: None),
            main_concurrency=lambda: 1)
        self.service = ExternalImportService(self.core, Path(self.temp.name) / "data")
        self.core.external_import = self.service
        self.addCleanup(self.service.close)
        friend = self.service.store.create_friend("合成测试好友")
        self.batch = self.service.store.create_batch(friend["id"], "synthetic-source", "测试本人", "明确会话")
        self.picture = io.BytesIO()
        Image.new("RGB", (600, 1400), "white").save(self.picture, "PNG")

    def upload(self):
        return self.service.upload(self.batch["id"], "synthetic.png", io.BytesIO(self.picture.getvalue()), len(self.picture.getvalue()))

    def wait_done(self):
        deadline = time.monotonic() + 8
        while self.service.runs and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.service.runs)
        return self.service.store.get_batch(self.batch["id"])

    def test_import_partial_save_resume_idempotency_and_evidence(self):
        self.upload()
        self.service.start(self.batch["id"])
        batch = self.wait_done()
        self.assertEqual(batch["state"], "review", batch.get("error"))
        self.assertEqual(len(batch["segments"]), 2)
        self.assertTrue(all(self.service.store.evidence_path(s["id"]).is_file() for s in batch["segments"]))
        saved = self.service.store.save_batch(batch["id"])
        self.assertEqual(saved["added"], 2)
        self.assertEqual(saved["pending"], 2)
        self.assertEqual(self.service.store.save_batch(batch["id"])["added"], 0)
        self.service.start(batch["id"])
        self.wait_done()
        self.assertEqual(self.core.supervisor.calls, 2, "completed frames must not call the model again")
        history = self.service.store.messages(batch["friendId"])
        self.assertEqual(len(history["messages"]), 2)
        self.assertEqual(len(history["gaps"]), 2)
        self.assertTrue(all(m["time"] is None for m in history["messages"]))

    def test_status_keeps_dependency_failure_unavailable_until_repaired(self):
        error = {"code": "dependency-missing", "message": "插件 synthetic.provider 未安装"}
        self.core.registry.dependency_error = lambda _: error
        plugin = self.service.status()["plugin"]
        self.assertFalse(plugin["enabled"])
        self.assertEqual(plugin["dependencyError"], error)
        self.core.registry.dependency_error = lambda _: None
        plugin = self.service.status()["plugin"]
        self.assertTrue(plugin["enabled"])
        self.assertNotIn("dependencyError", plugin)

    def test_stop_rejects_late_model_result_then_manual_resume(self):
        self.upload()
        gate = self.core.supervisor.block = threading.Event()
        self.service.start(self.batch["id"])
        self.assertTrue(self.core.supervisor.entered.wait(3))
        self.service.stop(self.batch["id"])
        gate.set()
        batch = self.wait_done()
        self.assertEqual(batch["state"], "interrupted")
        self.assertEqual(batch["candidates"], [])
        self.core.supervisor.block = None
        self.service.start(batch["id"])
        self.assertEqual(self.wait_done()["state"], "review")

    def test_slow_recognition_is_not_cut_off_by_the_old_three_minute_rpc_limit(self):
        # Simulate elapsed model time without making the suite wait three hours.
        # The actual model SDK enforces each request's configured deadline.
        self.core.supervisor.simulated_recognition_seconds = MAX_MODEL_TIMEOUT_MS / 1000 + 60
        self.upload()
        self.service.start(self.batch["id"])
        batch = self.wait_done()
        self.assertEqual(batch["state"], "review", batch.get("error"))
        self.assertEqual(len(batch["segments"]), 2)
        self.assertTrue(all(timeout > MAX_MODEL_TIMEOUT_MS / 1000
                            for timeout in self.core.supervisor.recognition_timeouts))

    def test_long_recognition_rpc_still_discards_results_after_revocation(self):
        self.core.supervisor.simulated_recognition_seconds = 181
        gate = self.core.supervisor.block = threading.Event()
        self.upload()
        self.service.start(self.batch["id"])
        self.assertTrue(self.core.supervisor.entered.wait(3))
        run = self.service.runs[self.batch["id"]]
        self.core.tasks.stop(run["task"]["id"])
        gate.set()
        batch = self.wait_done()
        self.assertEqual(batch["state"], "interrupted")
        self.assertEqual(batch["candidates"], [])
        self.assertEqual(self.core.tasks.task(run["task"]["id"])["state"], "stopped")

    def test_upload_validates_bytes_and_path_and_incomplete_stream(self):
        for name, content, size in [("../escape.png", b"abc", 3), ("fake.png", b"abc", 3),
                                     ("short.png", self.picture.getvalue(), len(self.picture.getvalue()) + 1)]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.service.upload(self.batch["id"], name, io.BytesIO(content), size)
        self.assertEqual(self.service.store.get_batch(self.batch["id"])["materials"], [])

    def test_http_upload_and_origin_guard_and_cleared_evidence(self):
        from http.server import ThreadingHTTPServer
        from real_http import make_handler
        backend = SimpleNamespace(core_services=lambda: self.core)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(backend))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, body=None, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                conn.request(method, path, body, headers or {})
                response = conn.getresponse()
                return response.status, response.read()
            finally:
                conn.close()
        path = f"/api/external-import/upload?batchId={self.batch['id']}&name=synthetic.png"
        self.assertEqual(request("POST", path, self.picture.getvalue(), {"Content-Type": "application/octet-stream", "Origin": "https://example.com"})[0], 403)
        status, result = request("POST", path, self.picture.getvalue(), {"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 200, result)
        material = json.loads(result)["materials"][0]
        self.assertEqual(request("GET", "/api/external-import/material?id=" + material["id"])[0], 200)
        self.assertEqual(request("GET", "/api/external-import/evidence?id=missing")[0], 404)

    def test_video_frames_follow_explicit_direction_and_preserve_positions(self):
        import cv2
        import numpy as np
        path = Path(self.temp.name) / "synthetic.mp4"
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (320, 240))
        if not writer.isOpened():
            self.skipTest("test encoder unavailable")
        try:
            for index in range(20):
                frame = np.zeros((240, 320, 3), dtype=np.uint8)
                frame[:, :] = (0, 0, 220) if index < 10 else (220, 0, 0)
                writer.write(frame)
        finally:
            writer.release()
        self.assertEqual(inspect_material(path)["kind"], "video")
        forward = list(evidence_frames(path, "video", {}, Path(self.temp.name) / "forward"))
        reverse = list(evidence_frames(path, "video", {}, Path(self.temp.name) / "reverse", direction="reverse"))
        self.assertLess(forward[0]["videoMs"], forward[-1]["videoMs"])
        self.assertGreater(reverse[0]["videoMs"], reverse[-1]["videoMs"])
        self.assertTrue(model_image(forward[0]["path"], max_width=320).startswith("data:image/jpeg;base64,"))
        with Image.open(forward[0]["path"]) as first, Image.open(reverse[0]["path"]) as last:
            self.assertGreater(first.getpixel((30, 30))[0], 200)
            self.assertGreater(last.getpixel((30, 30))[2], 200)


if __name__ == "__main__":
    unittest.main()
