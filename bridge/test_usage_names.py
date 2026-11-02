"""Usage names stay passive, account scoped, and independent of chat reads."""
import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from backend_service import Backend
from core_services import CoreServices
from real_http import make_handler
from usage_ledger import UsageLedger
from usage_names import UsageNameCache


def metadata(account="a", name="合成昵称"):
    return {"account": account, "sessions": [{"username": "friend", "name": name,
            "preview": "不得缓存的合成正文", "avatar": "不得缓存的图片"}]}


class UsageNameCacheTests(unittest.TestCase):
    def test_account_isolation_minimal_fields_copy_and_bounded_eviction(self):
        cache = UsageNameCache(account_limit=2)
        cache.observe(metadata("a", "甲"), cache.ticket())
        cache.observe(metadata("b", "乙"), cache.ticket())
        names = cache.get("a")
        self.assertEqual(names, {"friend": "甲"})
        names["friend"] = "外部修改"
        self.assertEqual(cache.get("a"), {"friend": "甲"})
        self.assertEqual(cache.get("b"), {"friend": "乙"})
        cache.get("a")
        cache.observe(metadata("c", "丙"), cache.ticket())
        self.assertEqual(cache.get("b"), {})
        self.assertEqual(cache.get("missing"), {})
        self.assertEqual(set(cache.names), {"a", "c"})

    def test_clear_and_close_reject_responses_started_before_invalidation(self):
        cache = UsageNameCache()
        ticket = cache.ticket()
        cache.observe(metadata("a"), ticket)
        cache.clear("a")
        cache.observe(metadata("a", "晚返回"), ticket)
        self.assertEqual(cache.get("a"), {})
        cache.observe(metadata("a", "新读取"), cache.ticket())
        self.assertEqual(cache.get("a"), {"friend": "新读取"})
        ticket = cache.ticket()
        cache.close()
        cache.observe(metadata("a", "关闭后晚返回"), ticket)
        self.assertEqual(cache.get("a"), {})
        self.assertIsNone(cache.ticket())

    def test_backend_lifecycle_revokes_cache_tickets(self):
        backend = Backend(SimpleNamespace(), model_source_store=SimpleNamespace(), selection_store=SimpleNamespace())
        ticket = backend.usage_names.ticket()
        backend.usage_names.observe(metadata(), ticket)
        backend.pause_for_account_clear("a")
        backend.usage_names.observe(metadata(), ticket)
        self.assertEqual(backend.usage_names.get("a"), {})
        backend.resume_after_failed_account_clear()
        ticket = backend.usage_names.ticket()
        backend.shutdown()
        backend.usage_names.observe(metadata(), ticket)
        self.assertEqual(backend.usage_names.get("a"), {})


class UsagePassiveNamesHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = UsageLedger(Path(self.temp.name) / "usage.sqlite3")
        self.source = SimpleNamespace(sessions=Mock(return_value=metadata()))
        self.backend = Backend(self.source, model_source_store=SimpleNamespace(),
                               selection_store=SimpleNamespace(get=lambda account: {"selectedSessions": ["friend"]}))
        self.core = CoreServices.__new__(CoreServices)
        self.core.backend, self.core.source, self.core.ledger = self.backend, self.source, self.ledger
        self.core.close = lambda: None
        self.backend._core = self.core
        self.ledger.record({"attemptId": "synthetic", "at": 1, "plugin": "chat", "account": "a",
                            "user": "friend", "status": "ok", "protocol": "chat_completions",
                            "inputTokens": 10, "outputTokens": 2})
        self.assertTrue(self.ledger.flush())
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.backend.shutdown()
        self.ledger.close()
        self.temp.cleanup()

    def request(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_usage_never_calls_source_and_normal_sessions_response_populates_names(self):
        self.source.sessions.side_effect = AssertionError("usage must not read slow chat metadata")
        status, usage = self.request("/api/usage/friends?account=a")
        self.assertEqual(status, 200)
        self.assertIsNone(usage["friends"][0]["name"])
        self.source.sessions.assert_not_called()
        self.source.sessions.side_effect = None
        self.assertEqual(self.request("/api/sessions")[0], 200)
        self.assertEqual(self.request("/api/usage/friends?account=a")[1]["friends"][0]["name"], "合成昵称")
        self.assertEqual(self.source.sessions.call_count, 1)
        self.assertEqual(self.core.usage_friend_names("b"), {})

    def test_business_friend_reads_observe_actual_account_without_changing_return(self):
        self.source.sessions.return_value = metadata("b", "另一账号")
        result = self.core.friends("a")
        self.assertEqual(result, [{"user": "friend", "name": "另一账号", "isGroup": False}])
        self.assertEqual(self.core.usage_friend_names("a"), {})
        self.assertEqual(self.core.usage_friend_names("b"), {"friend": "另一账号"})

    def test_late_session_response_cannot_restore_cleared_or_closed_names(self):
        for operation in ("clear", "close"):
            with self.subTest(operation=operation):
                self.backend.usage_names = UsageNameCache()
                entered, release = threading.Event(), threading.Event()
                responses = []

                def blocked():
                    entered.set()
                    release.wait(2)
                    return metadata()

                self.source.sessions.side_effect = blocked
                reader = threading.Thread(target=lambda: responses.append(self.request("/api/sessions")))
                reader.start()
                try:
                    self.assertTrue(entered.wait(1))
                    if operation == "close":
                        self.backend.usage_names.close()
                    else:
                        self.backend.usage_names.clear("a")
                finally:
                    release.set()
                    reader.join(3)
                self.assertEqual(responses[0][0], 200)
                self.assertEqual(self.core.usage_friend_names("a"), {})


if __name__ == "__main__":
    unittest.main()
