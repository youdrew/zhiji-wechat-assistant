"""Speech synthesis connection test against a local OpenAI-compatible stub."""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from model_probe import ModelProbe


class SpeechStub(BaseHTTPRequestHandler):
    reply = (200, "audio/mpeg", b"ID3fake")
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, self.headers.get("Authorization"), body))
        status, kind, data = type(self).reply
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class SpeechProbeTests(unittest.TestCase):
    def setUp(self):
        SpeechStub.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), SpeechStub)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}/v1/"
        self.probe = ModelProbe(Path(__file__).resolve().parents[1])

    def test_audio_reply_connects_with_the_key_model_and_voice(self):
        SpeechStub.reply = (200, "audio/mpeg", b"ID3fake")
        result = self.probe.speech_test(self.base, "tts-key", "tts-1", "alloy")
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["latencyMs"], 0)
        path, auth, body = SpeechStub.seen[-1]
        self.assertEqual((path, auth), ("/v1/audio/speech", "Bearer tts-key"))
        self.assertEqual((body["model"], body["voice"]), ("tts-1", "alloy"))
        self.assertTrue(body["input"])

    def test_failures_map_to_connector_error_codes(self):
        cases = [((401, "application/json", b"{}"), "auth"), ((404, "text/plain", b"x"), "unsupported"),
                 ((429, "text/plain", b"x"), "rate-limit"), ((500, "text/plain", b"x"), "provider-error"),
                 ((200, "application/json", b'{"error":1}'), "invalid-output"),
                 ((200, "audio/mpeg", b""), "empty-response")]
        for reply, code in cases:
            with self.subTest(code=code):
                SpeechStub.reply = reply
                with self.assertRaisesRegex(RuntimeError, f"^{code}$"):
                    self.probe.speech_test(self.base, None, "tts-1", "alloy")
        self.assertIsNone(SpeechStub.seen[-1][1])

    def test_unreachable_service_is_a_network_error(self):
        with self.assertRaisesRegex(RuntimeError, "^network$"):
            self.probe.speech_test("http://127.0.0.1:1/v1", "k", "tts-1", "alloy")


if __name__ == "__main__":
    unittest.main()
