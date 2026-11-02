"""Full stack: a plugin task calls a model through the SDK worker and Core's gateway."""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend_service import Backend  # noqa: E402
from conversation_selection import ConversationSelectionStore  # noqa: E402
from core_services import CoreServices  # noqa: E402
from real_http import make_handler  # noqa: E402
from test_support import FakeSource  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = {}
PLUGIN_MAIN = r'''
from kyf_plugin import Plugin, ModelError
plugin = Plugin()
RESULT = {}

@plugin.method("result")
def result(params, context):
    return RESULT

@plugin.task("scheme")
def run(task):
    RESULT.clear()
    try:
        params = task.task.get("params") or {}
        reply = plugin.models.generate(task, {"system": params.get("system", "只回答 JSON"),
                                              "prompt": params.get("prompt", "你好"), "jsonMode": True,
                                              "maxOutputTokens": 256}, step="overview")
        RESULT["text"] = reply["text"]
    except ModelError as exc:
        RESULT["error"] = exc.code
        RESULT["httpStatus"] = exc.http_status
        RESULT["message"] = exc.message
        return "failed"
    return "done"

plugin.run()
'''


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        UPSTREAM["auth"] = self.headers.get("Authorization")
        UPSTREAM["path"] = self.path
        UPSTREAM["body"] = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        status = UPSTREAM.get("responseStatus", 200)
        if status != 200:
            body = json.dumps({"error": {"code": "gateway_error", "message": "SYNTHETIC_PROVIDER_DETAIL"}}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({"id": "c1", "object": "chat.completion", "model": "model-a", "created": 1,
                           "choices": [{"index": 0, "message": {"role": "assistant", "content": UPSTREAM.get("responseText", "{\"ok\": true}")},
                                        "finish_reason": "stop"}],
                           "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ModelStore:
    def __init__(self, base_url):
        self.base_url = base_url

    def saved_selection(self):
        return {"selectedMode": "api", "sourceId": "svc-test", "api": {
            "protocol": "chat_completions", "baseUrl": self.base_url, "model": "model-a", "contextTokens": 32768,
            "encryptedKey": "x"}}

    def resolve_key(self, protocol, base_url, supplied, model_id=None):
        return "PROVIDER_SECRET"

    def models(self):
        return {"models": [{"id": "svc-test", "concurrency": 4}],
                "roles": {"main": "svc-test", "judge": None, "image": None}}

    def public(self, *args, **kwargs):
        return {}


class ModelErrorMetadataTests(unittest.TestCase):
    def test_http_status_is_optional_and_strictly_numeric(self):
        sys.path.insert(0, str(ROOT / "plugin-sdk" / "python"))
        from kyf_plugin import ModelError
        for value in (400, 429, 502, 599):
            self.assertEqual(ModelError("provider-error", http_status=value).http_status, value)
        for value in (None, True, "502", 200, 399, 600, 502.5, {}):
            self.assertIsNone(ModelError("provider-error", http_status=value).http_status)
        self.assertIsNone(ModelError("network").http_status)


class PluginModelTests(unittest.TestCase):
    def setUp(self):
        UPSTREAM.clear()
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        folder = base / "plugins" / "kyf.model-test"
        folder.mkdir(parents=True)
        (folder / "main.py").write_text(PLUGIN_MAIN, encoding="utf-8")
        (folder / "page.html").write_text("<!doctype html>", encoding="utf-8")
        (folder / "plugin.json").write_text(json.dumps({
            "schema": 1, "id": "kyf.model-test", "name": "模型测试", "version": "1.0.0", "core": {"api": [1]},
            "runtime": {"type": "python", "entry": "main.py"},
            "contributes": {"schemes": [{"id": "default", "name": "默认", "page": "page.html",
                                         "tabs": [{"id": "a", "title": "甲"}]}]}}), encoding="utf-8")
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        source = FakeSource(account="acct-1")
        source.sessions = lambda: {"account": "acct-1", "sessions": [{"username": "wxid_f", "name": "小红"}]}
        selection = ConversationSelectionStore(base / "selection")
        selection.set_all_selected("acct-1", ["wxid_f"])
        store = ModelStore(f"http://127.0.0.1:{self.provider.server_port}/v1")
        self.backend = Backend(source, model_source_store=store, selection_store=selection)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.backend._core = self.core = CoreServices(self.backend, ROOT, self.url, local=base / ".local",
                                                      bundled_dir=base / "plugins", local_dir=base / "none")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.core.close()
        self.provider.shutdown()
        self.provider.server_close()
        self.temp.cleanup()

    def post(self, path, body):
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())

    def generated(self, prompt, system="合成系统提示"):
        task = self.post("/api/tasks", {"plugin": "kyf.model-test", "account": "acct-1", "user": "wxid_f",
                                        "scheme": "default", "params": {"prompt": prompt, "system": system}})["task"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.core.tasks.task(task["id"])["state"] in ("queued", "running"):
            time.sleep(0.05)
        self.assertNotIn(self.core.tasks.task(task["id"])["state"], ("queued", "running"),
                         "SDK worker must return a response matching this request ID")
        result = self.post("/api/plugins/rpc", {"plugin": "kyf.model-test", "method": "result"})["result"]
        return task, result

    def test_unicode_line_separators_roundtrip_through_real_worker_without_changing_content(self):
        values = ("plain ASCII", "中文与 emoji 😀", "\u2028leading and trailing\u2028",
                  "\u2029leading and trailing\u2029", "\u2028\u2029" * 4 + "合成\r\n😀\u2028middle\u2029end")
        for value in values:
            with self.subTest(value=value):
                system = "系统\u2028\u2029" + value
                reply = "合成回复：" + value
                UPSTREAM["responseText"] = reply
                task, result = self.generated(value, system)
                self.assertEqual(self.core.tasks.task(task["id"])["state"], "done")
                self.assertEqual(result, {"text": reply.strip()})
                messages = UPSTREAM["body"]["messages"]
                self.assertEqual(messages[0]["content"], system)
                self.assertEqual(messages[1]["content"], value)

    def test_unicode_request_failure_does_not_block_the_next_request(self):
        value = "合成\u2028\u2029😀请求"
        UPSTREAM["responseStatus"] = 502
        failed, result = self.generated(value)
        self.assertEqual(self.core.tasks.task(failed["id"])["state"], "failed")
        self.assertEqual((result["error"], result["httpStatus"]), ("provider-error", 502))
        self.assertEqual(UPSTREAM["body"]["messages"][1]["content"], value)
        UPSTREAM["responseStatus"] = 200
        succeeded, result = self.generated(value + "\u2029后续")
        self.assertEqual(self.core.tasks.task(succeeded["id"])["state"], "done")
        self.assertEqual(result, {"text": "{\"ok\": true}"})

    def test_plugin_generation_goes_through_the_gateway(self):
        task = self.post("/api/tasks", {"plugin": "kyf.model-test", "account": "acct-1", "user": "wxid_f",
                                        "scheme": "default"})["task"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and self.core.tasks.task(task["id"])["state"] == "running":
            time.sleep(0.1)
        result = self.post("/api/plugins/rpc", {"plugin": "kyf.model-test", "method": "result"})["result"]
        self.assertEqual(result, {"text": "{\"ok\": true}"})
        self.assertEqual(self.core.tasks.task(task["id"])["state"], "done")
        self.assertEqual(UPSTREAM["auth"], "Bearer PROVIDER_SECRET")
        self.assertEqual(UPSTREAM["path"], "/v1/chat/completions")
        self.core.ledger.flush()
        records = self.core.ledger.records({"account": "acct-1"})["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual((records[0]["plugin"], records[0]["task"], records[0]["user"], records[0]["step"]),
                         ("kyf.model-test", task["id"], "wxid_f", "overview"))
        self.assertEqual((records[0]["inputTokens"], records[0]["outputTokens"]), (9, 4))
        summary = json.loads(urllib.request.urlopen(self.url + "/api/usage/summary?account=acct-1").read())
        self.assertEqual(summary["requests"], 1)
        self.assertEqual([(item["plugin"], item["requests"], item["inputTokens"]) for item in summary["plugins"]],
                         [("kyf.model-test", 1, 9)])

    def test_upstream_http_status_reaches_python_plugin_without_provider_response_text(self):
        for status, code in ((502, "provider-error"), (429, "rate-limit")):
            with self.subTest(status=status):
                UPSTREAM["responseStatus"] = status
                task = self.post("/api/tasks", {"plugin": "kyf.model-test", "account": "acct-1", "user": "wxid_f",
                                                "scheme": "default"})["task"]
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and self.core.tasks.task(task["id"])["state"] == "running":
                    time.sleep(0.05)
                result = self.post("/api/plugins/rpc", {"plugin": "kyf.model-test", "method": "result"})["result"]
                self.assertEqual((result["error"], result["httpStatus"]), (code, status))
                self.assertNotIn("SYNTHETIC_PROVIDER_DETAIL", result["message"])
                self.assertNotIn("PROVIDER_SECRET", result["message"])
                self.assertEqual(self.core.tasks.task(task["id"])["state"], "failed")


if __name__ == "__main__":
    unittest.main()
