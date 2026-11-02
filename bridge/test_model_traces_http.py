"""Synthetic real HTTP authorization tests for diagnostics-only capabilities."""
import http.client
import json
import tempfile
import threading
import unittest
from types import SimpleNamespace

from model_traces import ModelTraceService
from real_http import LocalHTTPServer, make_handler


class TraceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.account = "synthetic-a"
        self.service = ModelTraceService(self.temp.name, lambda: self.account)
        self.backend = SimpleNamespace(model_traces=self.service)
        self.control = "a" * 64
        self.server = LocalHTTPServer(("127.0.0.1", 0), make_handler(self.backend, control_token=self.control))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()
        self.temp.cleanup()

    def request(self, method, path, data=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            body = json.dumps(data).encode() if data is not None else None
            client.request(method, "/api/model-traces/" + path, body=body,
                           headers={**({"Content-Type": "application/json"} if body else {}), **(headers or {})})
            response = client.getresponse()
            return response.status, json.loads(response.read())
        finally:
            client.close()

    def session(self):
        status, data = self.request("POST", "session", {"windowId": "1"}, {"X-KYF-Control-Token": self.control})
        self.assertEqual(status, 200)
        return {"X-Model-Trace-Session": data["token"]}

    def test_process_bootstrap_and_viewer_producer_capabilities_are_separate(self):
        self.assertEqual(self.request("POST", "session", {"windowId": "1"})[0], 403)
        self.assertEqual(self.request("GET", "records")[0], 409)
        viewer = self.session()
        self.assertEqual(self.request("GET", "records", headers=viewer)[0], 200)
        self.assertEqual(self.request("POST", "state", {"enabled": True}, viewer)[0], 200)
        self.assertEqual(self.request("POST", "ingest", {"type": "start"}, viewer)[0], 403)
        producer = self.service.register_execution({"accountId": self.account})
        writer = {"X-Model-Trace-Producer": producer["producerId"]}
        self.assertEqual(self.request("GET", "producer-state", headers=writer)[0], 200)
        self.assertEqual(self.request("GET", "records", headers=writer)[0], 409)
        self.assertEqual(self.request("POST", "clear", {"confirmAllAccounts": True}, writer)[0], 409)

    def test_account_change_and_forged_account_targets_are_rejected(self):
        viewer = self.session()
        self.assertEqual(self.request("GET", "records?account=other", headers=viewer)[0], 403)
        self.account = "synthetic-b"
        self.assertEqual(self.request("GET", "records", headers=viewer)[0], 409)
        status, data = self.request("GET", "records", headers=self.session())
        self.assertEqual(status, 200)
        self.assertEqual(data["records"], [])

    def test_external_origin_even_with_capability_is_rejected(self):
        viewer = self.session()
        self.assertEqual(self.request("GET", "records", headers={**viewer, "Origin": "https://synthetic.invalid"})[0], 403)
        self.assertEqual(self.request("POST", "clear", {}, viewer)[0], 400)
        self.assertEqual(self.request("POST", "clear", {"confirmAllAccounts": True}, viewer)[0], 200)


if __name__ == "__main__":
    unittest.main()
