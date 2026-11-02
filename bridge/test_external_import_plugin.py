"""External import through the real plugin process and gateway, with synthetic vision responses."""
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend_service import Backend  # noqa: E402
from conversation_selection import ConversationSelectionStore  # noqa: E402
from core_http import post as host_post  # noqa: E402
from core_services import CoreServices  # noqa: E402
from core_tasks import TaskError  # noqa: E402
from real_http import make_handler  # noqa: E402
from test_support import FakeSource  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = "kyf.external-chat-import"
TSX_LOADER = ROOT / "node_modules" / "tsx" / "dist" / "loader.mjs"


def recognized():
    return {"messages": [
        {"text": "Synthetic hello", "sender": "other", "kind": "text", "timeLabel": "10月15日 20:30",
         "timestamp": 123456789, "issues": ["incomplete_time"], "bbox": [0.1, 0.2, 0.6, 0.1]},
        {"text": "Synthetic reply", "sender": "self", "kind": "text", "timeLabel": None,
         "issues": [], "bbox": [0.3, 0.4, 0.6, 0.1], "quote": "Synthetic hello"},
        {"text": "unclear □", "sender": "unknown", "kind": "text", "timeLabel": None,
         "issues": [], "bbox": [0.1, 0.6, 0.6, 0.1]},
    ], "warnings": []}


class VisionProvider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def send_json(self, body):
        payload = json.dumps(body, ensure_ascii=False).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # Cancelling the gateway request deliberately closes this connection.

    def do_GET(self):
        self.send_json({"data": [{"id": "local-default", "unified_gateway": True, "ready": True,
            "actual_model": "synthetic-vision", "backend_id": "test-backend", "route_revision": 1,
            "context_length": 32768, "max_output_tokens": 8192, "supports_vision": self.server.vision}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        review = any(message.get("role") == "system" and "本次只复核 target" in str(message.get("content"))
                     for message in body.get("messages", []))
        self.server.calls.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": body,
                                  "review": review})
        self.server.entered.set()
        if review:
            self.server.review_entered.set()
        if self.server.hold or (review and self.server.hold_review):
            self.server.release.wait(15)
        content = self.server.review_reply if review else self.server.reply
        self.send_json({"id": "synthetic-response", "object": "chat.completion", "created": 1,
            "model": "synthetic-vision", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": content if isinstance(content, str) else json.dumps(content)}}],
            "usage": {"prompt_tokens": 91, "completion_tokens": 37, "total_tokens": 128}})
        if review:
            self.server.review_replied.set()


class ModelStore:
    def __init__(self, url):
        self.url = url

    def saved_selection(self):
        return {"selectedMode": "api", "sourceId": "synthetic-import-service", "api": {
            "protocol": "chat_completions", "baseUrl": self.url, "model": "local-default",
            "contextTokens": 32768, "encryptedKey": "synthetic-cipher"}}

    def resolve_key(self, *_args):
        return "synthetic-provider-token"


class ExternalImportPluginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The gateway and plugin host are Node processes. Without the project's
        # dependencies every test only times out waiting for the gateway to start.
        if not TSX_LOADER.is_file():
            raise RuntimeError(f"project Node dependencies missing ({TSX_LOADER}); install or link node_modules first")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), VisionProvider)
        self.provider.calls, self.provider.reply, self.provider.vision = [], recognized(), True
        self.provider.hold = self.provider.hold_review = False
        self.provider.review_reply = {"resolved": False}
        self.provider.entered, self.provider.release, self.provider.review_entered = threading.Event(), threading.Event(), threading.Event()
        self.provider.review_replied = threading.Event()
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        # This source is never read for imports: the friend has no WeChat binding.
        self.backend = Backend(FakeSource(account="synthetic-wechat"),
            selection_store=ConversationSelectionStore(base / "selection"),
            model_source_store=ModelStore(f"http://127.0.0.1:{self.provider.server_port}/v1"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.backend._core = self.core = CoreServices(self.backend, ROOT, self.url, local=base / ".local",
            bundled_dir=ROOT / "plugins", local_dir=base / "none")
        self.service = self.core.external_import
        self.friend = self.service.store.create_friend("合成好友")
        self.batch = self.service.store.create_batch(self.friend["id"], "synthetic-source", "synthetic-self", "synthetic-chat")
        picture = Image.new("RGB", (500, 800), "#f5f5f5")
        draw = ImageDraw.Draw(picture)
        draw.rectangle((50, 160, 350, 240), fill="white")
        draw.text((65, 185), "Synthetic hello", fill="black")
        buffer = io.BytesIO()
        picture.save(buffer, "PNG")
        raw = buffer.getvalue()
        self.service.upload(self.batch["id"], "synthetic-chat.png", io.BytesIO(raw), len(raw))

    def tearDown(self):
        self.provider.release.set()
        self.core.close()
        self.server.shutdown()
        self.server.server_close()
        self.provider.shutdown()
        self.provider.server_close()
        self.temp.cleanup()

    def wait_until(self, check, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = check()
            if value:
                return value
            time.sleep(.05)
        self.fail("synthetic import did not reach expected state")

    def wait_batch(self):
        self.wait_until(lambda: self.batch["id"] not in self.service.runs)
        return self.service.store.get_batch(self.batch["id"])

    def usage(self):
        self.core.ledger.flush()
        return self.core.ledger.records({"account": "external-chat"})["records"]

    def test_actual_plugin_image_call_is_metered_and_only_clear_candidates_save(self):
        self.backend.source.account = ""
        selected = self.service.status()["model"]
        self.assertTrue(selected["supportsVision"])
        self.assertEqual(selected["model"], "synthetic-vision", "show the primary gateway's actual model, not its alias")
        self.service.start(self.batch["id"])
        batch = self.wait_batch()
        self.assertEqual(batch["state"], "review", batch.get("error"))
        self.assertEqual(batch["counts"], {"total": 3, "ready": 2, "pending": 1, "saved": 0, "ignored": 0})
        candidates = batch["candidates"]
        self.assertIsNone(candidates[0]["time"])
        self.assertEqual(candidates[0]["dateLabel"], "10月15日 20:30")
        self.assertEqual(candidates[1]["quote"], "Synthetic hello")
        self.assertIn("unknown_sender", candidates[2]["issues"])
        self.assertIn("unclear_text", candidates[2]["issues"])
        call = self.provider.calls[0]
        self.assertEqual(call["authorization"], "Bearer synthetic-provider-token")
        self.assertEqual(call["path"], "/v1/chat/completions")
        image = next(part for part in call["body"]["messages"][-1]["content"] if part["type"] == "image_url")
        self.assertTrue(image["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual([call["review"] for call in self.provider.calls], [False, True])
        review_images = [part for part in self.provider.calls[1]["body"]["messages"][-1]["content"] if part["type"] == "image_url"]
        self.assertGreaterEqual(len(review_images), 1)
        self.assertLessEqual(len(review_images), 3)
        self.assertTrue(all(part["image_url"]["url"].startswith("data:image/png;base64,") for part in review_images))
        self.assertTrue(candidates[2]["autoReviewAttempted"])
        self.assertFalse(candidates[2].get("autoReviewed", False))
        records = self.wait_until(lambda: (rows if len(rows := self.usage()) == 2 else None))
        by_step = {record["step"]: record for record in records}
        self.assertEqual(set(by_step), {"transcription", "transcription.review"})
        for record in records:
            self.assertEqual((record["plugin"], record["user"]), (PLUGIN, self.friend["id"]))
            self.assertEqual((record["inputTokens"], record["outputTokens"]), (91, 37))
        self.assertEqual(len({record["task"] for record in records}), 1)
        # The visible import page can inspect its own usage without a WeChat scope.
        with urllib.request.urlopen(self.url + "/api/external-import/usage?friendId=" + self.friend["id"], timeout=5) as response:
            visible_usage = json.loads(response.read())
        self.assertEqual(visible_usage["summary"]["requests"], 2)
        self.assertEqual(visible_usage["summary"]["totalInputTokens"], 182)
        self.assertEqual(visible_usage["summary"]["totalOutputTokens"], 74)
        self.assertEqual(visible_usage["records"][0]["task"], records[0]["task"])
        self.assertEqual(visible_usage["records"][0]["plugin"], PLUGIN)
        with self.assertRaises(urllib.error.HTTPError) as missing_friend:
            urllib.request.urlopen(self.url + "/api/external-import/usage?friendId=unknown-friend", timeout=5)
        self.assertEqual(missing_friend.exception.code, 404)
        missing_friend.exception.close()
        task = self.core.tasks.task(records[0]["task"])
        self.assertEqual((task["kind"], task["state"]), ("background", "done"))
        saved = self.service.store.save_batch(self.batch["id"])
        self.assertEqual((saved["added"], saved["pending"], saved["batch"]["state"]), (2, 1, "partial"))
        history = self.service.store.messages(self.friend["id"])
        self.assertEqual([row["text"] for row in history["messages"]], ["Synthetic hello", "Synthetic reply"])
        self.assertEqual(len(history["gaps"]), 1)
        plugin_token = self.core.supervisor.running(PLUGIN).token
        request = urllib.request.Request(self.url + "/plugin-api/v1/external/history?friendId=" + self.friend["id"],
            headers={"Authorization": "Bearer " + plugin_token})
        with urllib.request.urlopen(request, timeout=5) as response:
            plugin_history = json.loads(response.read())
        self.assertEqual(plugin_history["coverage"], history["coverage"])
        self.assertEqual(plugin_history["coverage"]["ordering"], "evidence-sequence")
        self.assertTrue(plugin_history["confirmedRecordsOnly"])
        self.assertFalse(plugin_history["completeHistory"])
        self.assertEqual(len(plugin_history["messages"]), 2)
        self.assertEqual(len(plugin_history["gaps"]), 1)
        self.assertEqual(len(self.core.tasks.tasks(account="external-chat", limit=20)), 1)

    def test_stopping_during_reread_keeps_primary_observations_and_revokes_late_review(self):
        self.provider.hold_review = True
        self.service.start(self.batch["id"])
        self.assertTrue(self.provider.review_entered.wait(15), "fake provider did not receive the local reread")
        run = self.service.runs[self.batch["id"]]
        before = self.service.store.get_batch(self.batch["id"])
        self.assertEqual(before["counts"]["total"], 3)
        self.assertFalse(before["candidates"][2].get("autoReviewAttempted", False))
        self.service.stop(self.batch["id"])
        with self.assertRaises(TaskError) as revoked:
            self.core.tasks.verify(run["execution"]["token"], PLUGIN, run["task"]["id"])
        self.assertEqual(revoked.exception.code, "execution-revoked")
        stopped = self.wait_batch()
        self.assertEqual(stopped["state"], "interrupted")
        self.assertEqual(stopped["counts"]["total"], 3)
        self.assertEqual(self.core.tasks.task(run["task"]["id"])["state"], "stopped")
        self.provider.release.set()
        self.assertTrue(self.provider.review_replied.wait(5), "fake provider did not release the late reread response")
        after = self.service.store.get_batch(self.batch["id"])
        self.assertEqual(after["candidates"], stopped["candidates"])
        self.assertFalse(after["candidates"][2].get("autoReviewAttempted", False))
        self.assertEqual(self.service.store.messages(self.friend["id"])["messages"], [])
        self.assertEqual(len(self.provider.calls), 2)

    def test_stopping_revokes_the_model_token_and_late_response_cannot_publish(self):
        self.provider.hold = True
        self.service.start(self.batch["id"])
        self.assertTrue(self.provider.entered.wait(15), "fake provider was not called")
        run = self.service.runs[self.batch["id"]]
        self.service.stop(self.batch["id"])
        with self.assertRaises(TaskError) as revoked:
            self.core.tasks.verify(run["execution"]["token"], PLUGIN, run["task"]["id"])
        self.assertEqual(revoked.exception.code, "execution-revoked")
        batch = self.wait_batch()
        self.assertEqual(batch["state"], "interrupted")
        self.assertEqual(batch["candidates"], [])
        self.assertEqual(self.core.tasks.task(run["task"]["id"])["state"], "stopped")
        self.provider.release.set()
        self.assertEqual(self.service.store.get_batch(self.batch["id"])["candidates"], [])
        self.assertEqual(self.service.store.messages(self.friend["id"])["messages"], [])

    def test_invalid_model_json_leaves_retryable_frame_and_retry_uses_existing_evidence(self):
        self.provider.reply = '{"messages": ['
        self.service.start(self.batch["id"])
        failed = self.wait_batch()
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["candidates"], [])
        self.assertEqual(failed["segments"][0]["state"], "failed")
        self.assertEqual(len(self.provider.calls), 2)  # One bounded JSON-format retry.
        segment_id = failed["segments"][0]["id"]
        self.provider.reply = recognized()
        self.service.start(self.batch["id"])
        retried = self.wait_batch()
        self.assertEqual(retried["state"], "review", retried.get("error"))
        self.assertEqual([row["id"] for row in retried["segments"]], [segment_id])
        self.assertEqual(len(retried["candidates"]), 3)
        self.assertEqual(len(self.provider.calls), 4)

    def test_global_task_stop_acknowledges_revocation_and_continue_uses_core_orchestrator(self):
        self.provider.hold = True
        self.backend.source.account = ""
        self.service.start(self.batch["id"])
        self.assertTrue(self.provider.entered.wait(15))
        run = self.service.runs[self.batch["id"]]
        original_task = run["task"]["id"]
        self.core.tasks.stop(original_task)
        stopped = self.wait_batch()
        self.assertEqual(stopped["state"], "interrupted")
        self.assertEqual(self.core.tasks.task(original_task)["state"], "stopped")
        self.assertNotIn(original_task, self.core.tasks.deadlines)
        self.assertIsNotNone(self.core.supervisor.running(PLUGIN))
        self.provider.hold = False
        self.provider.release.set()
        resumed = host_post(self.core, "/api/tasks/continue", {"id": original_task})
        self.assertNotEqual(resumed["task"]["id"], original_task)
        completed = self.wait_batch()
        self.assertEqual(completed["state"], "review", completed.get("error"))
        self.assertEqual(len(completed["candidates"]), 3)
        self.assertEqual(self.core.tasks.task(resumed["task"]["id"])["state"], "done")
        self.assertEqual(len(self.provider.calls), 3)

    def test_known_text_only_model_fails_without_sending_image_upstream(self):
        self.provider.vision = False
        self.assertFalse(self.service.status()["model"]["available"])
        self.service.start(self.batch["id"])
        failed = self.wait_batch()
        self.assertEqual(failed["state"], "failed")
        self.assertIn("当前图片模型", failed["error"])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(failed["candidates"], [])

    def test_saved_concurrency_allows_two_batches_and_waiting_batch_can_stop(self):
        def concurrency(value):
            settings = self.core.model_settings()
            self.core.save_model_settings(settings["revision"], {**settings["values"], "maxConcurrentTasks": value})

        def another_batch(key):
            batch = self.service.store.create_batch(self.friend["id"], "synthetic-source", "synthetic-self", key)
            material = self.service.store.get_batch(self.batch["id"])["materials"][0]
            source = self.service.store.material_path(material["id"])
            with source.open("rb") as stream:
                self.service.upload(batch["id"], "synthetic-chat.png", stream, source.stat().st_size)
            return batch

        second = another_batch("synthetic-chat-2")
        third = another_batch("synthetic-chat-3")
        concurrency(2)
        self.provider.hold = True
        self.service.start(self.batch["id"])
        self.service.start(second["id"])
        self.wait_until(lambda: len(self.provider.calls) == 2)
        self.assertEqual(self.service.processing, 2)
        self.assertEqual(self.service.store.get_batch(second["id"])["state"], "running")
        concurrency(1)
        self.service.start(third["id"])
        self.service.stop(third["id"])
        self.wait_until(lambda: third["id"] not in self.service.runs)
        self.assertEqual(self.service.store.get_batch(third["id"])["state"], "interrupted")
        self.assertEqual(len(self.provider.calls), 2)
        self.provider.hold = False
        self.provider.release.set()
        self.wait_until(lambda: not self.service.runs)
        self.assertEqual(self.service.processing, 0)
        for batch_id in (self.batch["id"], second["id"]):
            self.assertEqual(self.service.store.get_batch(batch_id)["state"], "review")


if __name__ == "__main__":
    unittest.main()
