"""Keep pairing secrets and RPC bearer tokens on the intended loopback server."""
import importlib.util
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from zhiji_mcp_http import post_json


def script(name):
    path = Path(__file__).resolve().with_name(name + ".py")
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class LoopbackTransportTests(unittest.TestCase):
    def server(self, response):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, self.headers.get("Authorization"), body))
                status, headers = response(self.path)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def do_GET(self):
                calls.append((self.path, self.headers.get("Authorization"), None))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", calls

    def test_all_three_callers_bypass_a_configured_proxy(self):
        proxy, proxy_calls = self.server(lambda _path: (502, {}))
        origin, core_calls = self.server(lambda _path: (200, {}))
        with patch.dict(os.environ, {"http_proxy": proxy, "HTTP_PROXY": proxy, "no_proxy": "", "NO_PROXY": ""}):
            cli = script("zhiji-mcp-cli")
            adapter = script("mcp-stdio-adapter")
            events = script("zhiji-events")
            self.assertEqual(cli.http_post(origin, "/api/mcp/pairing/poll", {"pollSecret": "synthetic"}),
                             (200, {"ok": True}))
            self.assertEqual(adapter.CoreConnection._post(None, origin, "/api/mcp/handshake",
                                                          {"credential": "synthetic"}), {"ok": True})
            self.assertEqual(events.Core._post(SimpleNamespace(url=origin), "/api/mcp/rpc", {}, 2,
                                               "synthetic-token"), {"ok": True})
        self.assertEqual(proxy_calls, [])
        self.assertEqual(core_calls, [
            ("/api/mcp/pairing/poll", None, {"pollSecret": "synthetic"}),
            ("/api/mcp/handshake", None, {"credential": "synthetic"}),
            ("/api/mcp/rpc", "Bearer synthetic-token", {}),
        ])

    def test_redirects_do_not_forward_credentials_or_bearer_tokens(self):
        other, other_calls = self.server(lambda _path: (200, {}))
        for redirect_status in (302, 307):
            with self.subTest(redirect_status=redirect_status):
                origin, _ = self.server(lambda _path: (redirect_status, {"Location": other + "/trap"}))
                with self.assertRaises(HTTPError) as caught:
                    post_json(origin, "/api/mcp/rpc", {"credential": "synthetic"}, timeout=2,
                              token="synthetic-token")
                self.assertEqual(caught.exception.code, redirect_status)
                caught.exception.close()
        self.assertEqual(other_calls, [])

    def test_non_loopback_override_is_rejected_before_connection(self):
        cli = script("zhiji-mcp-cli")
        for invalid in ("http://192.0.2.1:1234", "http://127.0.0.1:1234@example.invalid",
                        "http://127.0.0.1:1234/not-an-origin", "http://localhost:1234"):
            with self.subTest(invalid=invalid), patch.dict(os.environ, {cli.CORE_URL_ENV: invalid}):
                with self.assertRaises(cli.CliError):
                    cli.core_url(None)
                with self.assertRaises(URLError):
                    post_json(invalid, "/api/mcp/handshake", {}, timeout=2)


if __name__ == "__main__":
    unittest.main()
