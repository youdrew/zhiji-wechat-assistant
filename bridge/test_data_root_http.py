"""HTTP contract checks for /api/data-root endpoints."""

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import nullcontext
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_root_source import CUSTOM_ROOT_ENV, DataRootSource
from real_backend import Backend
from real_http import make_handler


class DataRootHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DataRootSource(self.root)
        self.backend = object.__new__(Backend)
        self.backend.data_root_store = self.store
        self.backend.request_lease = nullcontext
        self.backend.source = None
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(CUSTOM_ROOT_ENV, None)

    def server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def request(server, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            connection.request(method, path, body=encoded,
                               headers={"Content-Type": "application/json; charset=utf-8"})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_get_initial_state(self):
        server = self.server()
        status, body = self.request(server, "GET", "/api/data-root")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"state": "unset", "path": "", "exists": False, "accounts": 0})

    def test_post_valid_path(self):
        server = self.server()
        data_dir = self.root / "xwechat_files"
        data_dir.mkdir()
        (data_dir / "acct" / "db_storage").mkdir(parents=True)
        status, body = self.request(server, "POST", "/api/data-root", {"path": str(data_dir)})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "ready")
        self.assertEqual(body["accounts"], 1)
        status, body = self.request(server, "GET", "/api/data-root")
        self.assertEqual(body["path"], str(data_dir.resolve()))

    def test_post_relative_path_rejected(self):
        server = self.server()
        status, body = self.request(server, "POST", "/api/data-root", {"path": "relative/path"})
        self.assertEqual(status, 400)

    def test_post_nonexistent_rejected(self):
        server = self.server()
        status, body = self.request(server, "POST", "/api/data-root",
                                    {"path": str(self.root / "missing")})
        self.assertEqual(status, 400)

    def test_post_wrong_keys_rejected(self):
        server = self.server()
        status, body = self.request(server, "POST", "/api/data-root", {"wrong": "x"})
        self.assertEqual(status, 400)

    def test_post_too_long_rejected(self):
        server = self.server()
        status, body = self.request(server, "POST", "/api/data-root", {"path": "x" * 5000})
        self.assertEqual(status, 400)

    def test_post_control_chars_rejected(self):
        server = self.server()
        status, body = self.request(server, "POST", "/api/data-root", {"path": "bad\x00path"})
        self.assertEqual(status, 400)

    def test_clear(self):
        server = self.server()
        data_dir = self.root / "xwechat_files"
        data_dir.mkdir()
        self.request(server, "POST", "/api/data-root", {"path": str(data_dir)})
        status, body = self.request(server, "POST", "/api/data-root/clear", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "unset")

    def test_clear_nonempty_body_rejected(self):
        server = self.server()
        status, body = self.request(server, "POST", "/api/data-root/clear", {"extra": 1})
        self.assertEqual(status, 400)

    def test_bad_origin_forbidden(self):
        server = self.server()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        try:
            connection.request("GET", "/api/data-root",
                               headers={"Origin": "http://evil.example"})
            response = connection.getresponse()
            self.assertEqual(response.status, 403)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
