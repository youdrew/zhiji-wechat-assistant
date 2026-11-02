"""Real Python HTTP ↔ Node capture pipeline using synthetic local providers only."""
from contextlib import nullcontext
from http.server import ThreadingHTTPServer
from pathlib import Path
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from types import SimpleNamespace

from model_traces import ModelTraceService, TraceError
from real_http import make_handler

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "project Node required")
class ModelTraceIntegrationTests(unittest.TestCase):
    def test_ten_concurrent_contacts_real_http_and_clear_fence(self):
        with tempfile.TemporaryDirectory() as folder:
            service = ModelTraceService(Path(folder), current_account=lambda: "synthetic-account")
            observed = []
            original_ingest = service.ingest
            def observe_ingest(token, event):
                result = original_ingest(token, event)
                observed.append((event.get("type"), result))
                return result
            service.ingest = observe_ingest
            backend = SimpleNamespace(model_traces=service, request_lease=nullcontext)
            handler = make_handler(backend)
            http_errors = []
            original_send = handler.send
            def observed_send(self, status, body, *args):
                if status != 200:
                    http_errors.append((self.path, status, body))
                return original_send(self, status, body, *args)
            handler.send = observed_send
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            service.transport_url = "http://127.0.0.1:" + str(server.server_port)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                service.update_settings({"enabled": True})
                contexts = [service.register_execution({"accountId": "synthetic-account",
                    "contactId": "contact-" + str(index), "source": "synthetic", "trigger": "manual"}) for index in range(10)]
                result = subprocess.run([shutil.which("node"), "--import", "tsx",
                    str(ROOT / "tests/fixtures/model-trace-producer.ts")], cwd=ROOT,
                    input=json.dumps(contexts), capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr[:1000])
                self.assertEqual(json.loads(result.stdout)["requests"], 10)
                service.flush()
                session = service.create_session("synthetic-desktop")
                token = session["token"]
                listing = service.read(token, "records")
                self.assertEqual(len(listing["records"]), 10, (json.loads(result.stdout), service.snapshot(), http_errors, observed))
                self.assertEqual({row["contactId"] for row in listing["records"]}, {"contact-" + str(i) for i in range(10)})
                for row in listing["records"]:
                    request = service.read(token, "body", {"recordId": row["recordId"], "kind": "request"})
                    self.assertIn(row["contactId"], request["text"])
                    self.assertNotIn("synthetic-secret-key", request["text"])
                    self.assertTrue(row["complete"], row)
                    self.assertEqual(row["status"], "completed")
                service.delete_scope(all_accounts=True)
                self.assertEqual(service.read(token, "records")["records"], [])
                old = contexts[0]
                self.assertFalse(service.ingest(old["producerId"], {"type": "start", "recordId": "late",
                    "eventSeq": 0, "collectionVersion": old["collectionVersion"],
                    "deleteVersion": old["deleteVersion"], "meta": {}})["accepted"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)
                service.close()


if __name__ == "__main__":
    unittest.main()
