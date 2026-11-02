"""Local judge lifecycle, resource gates and privacy using synthetic assets only."""
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from core_judge import BUILTIN, JudgeService
from core_model_assets import ModelAssetError
from model_source import ModelSourceStore, ModelSourceUnavailable


class Response(io.BytesIO):
    status = 200
    headers = {}
    def geturl(self):
        return "https://modelscope.cn/synthetic.gguf"


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        self.data = b"synthetic-public-model"
        self.manifest = {"schema": 1, "modelId": "synthetic/Julia", "revision": "fixed-test",
            "allowedHosts": ["modelscope.cn"], "files": [{"name": "synthetic.gguf",
                "bytes": len(self.data), "sha256": hashlib.sha256(self.data).hexdigest(),
                "url": "https://modelscope.cn/synthetic.gguf"}]}
        (self.root / "scripts/judge-model.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        self.directory = self.root / ".local/core/judge/julia-1"
        self.store = ModelSourceStore(self.root / "model-source.json", protect=lambda key: key.encode(),
            unprotect=lambda data: data.decode())
        self.main = self.store.save_api("ollama", "http://127.0.0.1:11434", "main", None)
        self.gateway = Mock()
        self.gateway.local_judge.return_value = {"formatValid": True, "latencyMs": 700,
            "correct": 1, "samples": 3, "qualityVerified": False}
        self.opener = Mock()
        self.opener.open.side_effect = lambda *_, **__: Response(self.data)
        self.changed = Mock()
        self.service = JudgeService(self.root, self.directory, self.gateway, self.store,
            self.changed, opener=self.opener, supported=True)
        self.service._check_resources = Mock()
        self.addCleanup(self.service.close)

    def finish(self):
        self.service.worker.join(5)
        self.assertFalse(self.service.worker.is_alive())
        return self.service.status()

    def ready(self):
        self.service.prepare()
        status = self.finish()
        self.assertTrue(status["ready"], status)
        return status

    def test_status_is_read_only_and_does_not_construct_core_or_start_gateway(self):
        status = JudgeService.unprepared_status(self.root, self.store)
        self.assertEqual(status["state"], "missing")
        self.assertFalse(status["ready"])
        self.assertFalse(self.directory.exists())
        self.opener.open.assert_not_called()
        self.gateway.local_judge.assert_not_called()

    def test_prepare_verifies_registers_without_auto_assignment_or_quality_claim(self):
        status = self.ready()
        self.assertFalse(status["qualityVerified"])
        self.assertTrue(status["backgroundOnly"])
        self.assertEqual(status["latencyMs"], 700)
        self.assertEqual(self.store.models()["roles"], {"main": self.main, "judge": None, "image": None})
        entry = next(x for x in self.store.models()["models"] if x.get("builtin") == BUILTIN)
        self.assertEqual(entry["concurrency"], 1)
        self.service.prepare()
        self.assertEqual(self.gateway.local_judge.call_count, 1)
        for key in ("runtimePath", "modelPath", "apiKey", "local"):
            self.assertNotIn(key, json.dumps(status))

    def test_restart_restores_verified_ready_or_slow_without_running_selftest_again(self):
        self.gateway.local_judge.return_value["latencyMs"] = 2500
        first = self.ready()
        self.assertEqual(first["state"], "slow")
        restored = JudgeService.unprepared_status(self.root, self.store)
        self.assertEqual(restored, first)
        self.assertEqual(self.gateway.local_judge.call_count, 1)
        (self.directory / "synthetic.gguf").write_bytes(b"corrupt")
        self.assertFalse(JudgeService.unprepared_status(self.root, self.store)["ready"])

    def test_invalid_selftest_never_registers_and_can_be_retried_without_redownloading(self):
        for latency in (float("nan"), float("inf"), True, -1):
            with self.subTest(latency=latency):
                self.gateway.local_judge.return_value = {"formatValid": True, "latencyMs": latency}
                self.service.prepare()
                self.assertFalse(self.finish()["ready"])
                self.assertFalse(any("builtin" in x for x in self.store.models()["models"]))
        self.gateway.local_judge.return_value = {"formatValid": True, "latencyMs": 25}
        self.ready()
        self.assertEqual(self.opener.open.call_count, 1)

    def test_resource_failure_download_failure_and_raw_errors_are_sanitized(self):
        self.service._check_resources.side_effect = ModelAssetError("judge-memory", "可用内存不足 2 GB")
        self.service.prepare()
        status = self.finish()
        self.assertEqual(status["state"], "error")
        self.opener.open.assert_not_called()
        self.service._check_resources.side_effect = None
        self.opener.open.side_effect = RuntimeError("SYNTHETIC_PRIVATE_DETAIL")
        self.service.prepare()
        status = self.finish()
        self.assertEqual(status["state"], "error")
        self.assertNotIn("SYNTHETIC_PRIVATE_DETAIL", json.dumps(status))

    def test_delete_reverts_roles_removes_files_and_blocks_prepare_until_finished(self):
        status = self.ready()
        self.store.set_roles(judge=status["modelEntryId"])
        entered, release = threading.Event(), threading.Event()
        def stop(operation, **kwargs):
            self.assertEqual(operation, "stop")
            entered.set()
            self.assertTrue(release.wait(5))
            return {"stopped": True}
        self.gateway.local_judge.side_effect = stop
        deletion = threading.Thread(target=self.service.delete)
        deletion.start()
        try:
            self.assertTrue(entered.wait(5))
            with self.assertRaises(ModelAssetError):
                self.service.prepare()
        finally:
            release.set()
            deletion.join(5)
        self.assertFalse(deletion.is_alive())
        self.assertEqual(self.store.models()["roles"], {"main": self.main, "judge": None, "image": None})
        self.assertEqual(list(self.directory.iterdir()), [])
        self.assertEqual(self.service.status()["state"], "missing")

    def test_cancel_during_selftest_cannot_promote_late_result(self):
        entered = threading.Event()
        def selftest(operation, config, *, check, **kwargs):
            entered.set()
            while not self.service.cancelled.wait(.01):
                check()
            check()
        self.gateway.local_judge.side_effect = selftest
        self.service.prepare()
        self.assertTrue(entered.wait(5))
        self.service.cancel()
        self.assertFalse(self.service.worker.is_alive())
        self.assertFalse(self.service.status()["ready"])
        self.assertFalse((self.directory / "selftest-result.json").exists())
        self.assertFalse(any("builtin" in x for x in self.store.models()["models"]))

    def test_cancelled_late_opener_does_not_recreate_partial_after_delete(self):
        entered, release = threading.Event(), threading.Event()
        def opening(*_, **__):
            entered.set()
            self.assertTrue(release.wait(10))
            return Response(self.data)
        self.opener.open.side_effect = opening
        self.service.prepare()
        self.assertTrue(entered.wait(5))
        try:
            status = self.service.delete()
            self.assertEqual(status["state"], "error")  # Still-open downloader makes deletion busy.
            self.assertFalse(status["ready"])
        finally:
            release.set()
            self.service.assets.thread.join(5)
        self.service.delete()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_builtin_is_judge_only_and_service_identity_ignores_roles(self):
        status = self.ready()
        entry = next(x for x in self.store.models()["models"] if x.get("builtin") == BUILTIN)
        for role in ("main", "image"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                self.store.set_roles(**{role: entry["id"]})
        self.assertIsNone(self.service.service(entry, ["image"]))
        first = self.service.service(entry, ["judge"])
        self.store.set_roles(judge=entry["id"])
        self.assertEqual(self.service.service(entry, ["judge"])["fingerprint"], first["fingerprint"])
        self.assertEqual(first["local"]["threads"], max(1, __import__("psutil").cpu_count(logical=False) // 2))
        saved = json.loads(self.store.path.read_text(encoding="utf-8"))
        saved["roles"]["image"] = entry["id"]
        self.store.path.write_text(json.dumps(saved), encoding="utf-8")
        with self.assertRaises(ModelSourceUnavailable):
            self.store.models()
        saved["roles"]["image"] = None
        self.store.path.write_text(json.dumps(saved), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
