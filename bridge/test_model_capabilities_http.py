"""Bounded credentialed metadata discovery against a loopback-only synthetic gateway."""
import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from model_capabilities import ModelCapabilitiesUnavailable, UnifiedModelCapabilities  # noqa: E402


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        if self.path != "/v1/models":
            self.send_response(500)
            self.end_headers()
            return
        if self.server.redirect:
            self.send_response(302)
            self.send_header("Location", "/must-not-forward-credential")
            self.end_headers()
            return
        body = json.dumps({"data": [{"id": "local-default", "unified_gateway": True, "ready": True,
                                     "actual_model": "synthetic-http-model", "backend_id": "fixture",
                                     "route_revision": 4, "context_length": 1048576,
                                     "max_output_tokens": 32768, "supports_vision": False}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.server.drip:
            try:
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    if self.server.stop_drip.wait(.025):
                        break
            except OSError:
                pass
        else:
            self.wfile.write(body)


class MetadataHTTPTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        self.server.seen, self.server.redirect = [], False
        self.server.drip = False
        self.server.stop_drip = threading.Event()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.api = {"protocol": "chat_completions", "model": "local-default",
                    "baseUrl": f"http://127.0.0.1:{self.server.server_port}/v1"}

    def tearDown(self):
        self.server.stop_drip.set()
        self.server.shutdown()
        self.server.server_close()

    def test_real_metadata_transport_has_no_chat_payload_and_uses_current_in_memory_key(self):
        capabilities = UnifiedModelCapabilities().lookup(self.api, "synthetic-http-key")
        self.assertEqual(capabilities["actual_model"], "synthetic-http-model")
        self.assertEqual(capabilities["context_length"], 1048576)
        self.assertEqual(self.server.seen, [("/v1/models", "Bearer synthetic-http-key")])

    def test_redirect_is_unavailable_and_does_not_forward_the_key(self):
        self.server.redirect = True
        with self.assertRaises(ModelCapabilitiesUnavailable):
            UnifiedModelCapabilities().lookup(self.api, "synthetic-http-key")
        self.assertEqual(self.server.seen, [("/v1/models", "Bearer synthetic-http-key")])

    def test_slow_drip_is_bounded_by_total_read_deadline_and_releases_discovery_lock(self):
        self.server.drip = True
        capabilities = UnifiedModelCapabilities()
        started = time.monotonic()
        with patch("model_capabilities.REQUEST_TIMEOUT", .15):
            with self.assertRaises(ModelCapabilitiesUnavailable):
                capabilities.lookup(self.api, "synthetic-http-key")
        self.assertLess(time.monotonic() - started, .6, "socket activity extended the total read deadline")
        self.assertTrue(capabilities.lock.acquire(blocking=False), "failed discovery retained the cache lock")
        capabilities.lock.release()
        with self.assertRaises(ModelCapabilitiesUnavailable):
            capabilities.lookup(self.api, "synthetic-http-key")
        self.assertEqual(len(self.server.seen), 1, "the bounded failure cache should prevent another drip request")


if __name__ == "__main__":
    unittest.main()
