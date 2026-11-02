"""Synthetic category/date/history paging checks; no account or model access.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from backend_contracts import AccountChangedError, QUOTED_REPLY_TYPE
from backend_service import Backend
from chat_server import classify
from history_browser import SEARCH_SCAN_LIMIT, decode_cursor, encode_cursor, search
from real_http import make_handler
from wechat_source import WeChatSource


ACCOUNT = "synthetic-account"
USER = "synthetic-friend"
TABLE = "Msg_" + "a" * 32


def app(subtype, title="synthetic card", extra=""):
    return f"<msg><appmsg><title>{title}</title><type>{subtype}</type>{extra}</appmsg></msg>"


class HistoryFixture:
    account = ACCOUNT
    wxid = "synthetic-self"

    def __init__(self, root):
        self.root = root
        self.paths = []
        self.source = WeChatSource(factory=lambda: self, classifier=classify)
        self.source._contacts = lambda _db: {}

    def add(self, records, shard="message__message_0.db"):
        path = self.root / shard
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.execute("INSERT INTO Name2Id VALUES (?)", (USER,))
            conn.execute(f"CREATE TABLE {TABLE} (local_id INTEGER,local_type INTEGER,"
                         "real_sender_id INTEGER,create_time INTEGER,message_content BLOB,"
                         "compress_content BLOB,server_id INTEGER,sort_seq INTEGER)")
            conn.executemany(f"INSERT INTO {TABLE} VALUES (?,?,1,?,?,?,0,?)", [
                (seq, kind, created, content, compressed, seq)
                for seq, kind, content, created, compressed in records])
        self.paths.append(path)

    def _msg_conns(self, user):
        assert user == USER
        return [(sqlite3.connect(path), TABLE) for path in self.paths]

    def get_self_info(self):
        return {"username": self.wxid}

    @staticmethod
    def _msg_type_name(value):
        return {1: "文本", 3: "图片", 34: "语音", 43: "视频", 47: "表情",
                49: "应用消息", 11000: "表情"}.get(value & 0xFFFFFFFF, "系统消息")

    @staticmethod
    def _friendly_content(value, _kind):
        return app(6, "decoded attachment") if value == b"synthetic-compressed" else value.decode()


class HistoryCategoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = HistoryFixture(self.root)

    def add(self, records, **kwargs):
        self.fixture.add([(seq, kind, content, 0, None) for seq, kind, content in records], **kwargs)

    def search(self, **kwargs):
        return search(self.fixture.source, ACCOUNT, USER, **kwargs)

    @staticmethod
    def sequences(page):
        return [item["_sort"][0] for item in page["messages"]]

    def service(self):
        backend = SimpleNamespace(source=self.fixture.source,
                                  _scoped_identity=lambda: (ACCOUNT, self.root), _assert_scope=Mock())
        backend.history_search = MethodType(Backend.history_search, backend)
        return backend

    def test_blank_search_browses_latest_without_date_or_keyword(self):
        self.add([(seq, 1, f"synthetic message {seq}") for seq in range(1, 5)])
        first = self.search(query="  ", day="", limit=2)
        self.assertEqual(self.sequences(first), [4, 3])
        self.assertTrue(first["hasMore"])
        second = self.search(before=first["nextCursor"], limit=2)
        self.assertEqual(self.sequences(second), [2, 1])
        self.assertFalse(second["hasMore"])
        self.assertIsNone(second["nextCursor"])

    def test_categories_use_packed_and_xml_subtypes_and_text_links(self):
        self.add([(1, 1, "ordinary text"), (2, 3, "[图片]"), (3, 34, "[语音]"),
                  (4, 43, "[视频]"), (5, 47, "[表情]"), (6, (17 << 32) | 11000, "[表情]"),
                  (7, 49, app(6)), (8, (6 << 32) | 49, "[应用消息]"),
                  (9, 49, app(5)), (10, 1, "见 HTTPS://example.invalid/synthetic"),
                  (11, 49, app(3)), (12, (76 << 32) | 49, "[应用消息]"),
                  (13, 49, app(33)), (14, (36 << 32) | 49, "[应用消息]"),
                  (15, 49, app(51)), (16, (51 << 32) | 49, "[应用消息]"),
                  (17, QUOTED_REPLY_TYPE, app(57, "reply", "<refermsg>" + app(6) + "</refermsg>")),
                  (18, 49, app(57, "reply", "<refermsg>" + app(5) + "</refermsg>"))])
        expected = {"media": [6, 5, 4, 3, 2], "files": [8, 7], "links": [10, 9],
                    "music": [12, 11], "mini-programs": [14, 13], "channels": [16, 15]}
        for category, sequences in expected.items():
            with self.subTest(category=category):
                page = self.search(category=category)
                self.assertEqual(self.sequences(page), sequences)
                self.assertTrue(all(item["historyCategory"] == category for item in page["messages"]))
        self.assertEqual(self.sequences(self.search()), list(range(18, 0, -1)))

    def test_legacy_subtypes_use_decoded_content_without_leaking_xml(self):
        self.fixture.add([(1, 49, "[应用消息]", 0, b"synthetic-compressed"),
                          (2, 49, b"synthetic-friend:\n" + app(6).encode(), 0, None)])
        page = self.search(category="files")
        self.assertEqual(self.sequences(page), [2, 1])
        self.assertEqual(page["messages"][1]["text"], "decoded attachment")
        self.assertNotIn("appmsg", json.dumps(page))

    def test_malformed_or_oversized_app_xml_cannot_match_a_category(self):
        self.add([(1, 49, "<msg><appmsg><type>6</type>"),
                  (2, 49, '<!DOCTYPE msg [<!ENTITY x "6">]><msg><appmsg><type>&x;</type></appmsg></msg>'),
                  (3, 49, app("6" * 10000)), (4, 49, app(6, "x" * 128000)),
                  (5, 49, app(999)), (6, 49, app("²"))])
        self.assertEqual(self.search(category="files")["messages"], [])

    def test_empty_sparse_page_advances_scan_cursor_to_older_match(self):
        self.add([(1, 49, app(6, "old attachment"))] + [
            (seq, 1, "unrelated") for seq in range(2, SEARCH_SCAN_LIMIT + 2)])
        first = self.search(category="files", limit=1)
        self.assertEqual(first["messages"], [])
        self.assertTrue(first["hasMore"])
        self.assertEqual(decode_cursor(first["nextCursor"], ACCOUNT, USER)[0], 2)
        second = self.search(category="files", before=first["nextCursor"], limit=1)
        self.assertEqual(self.sequences(second), [1])
        self.assertFalse(second["hasMore"])

    def test_category_filter_runs_before_result_limit_across_shards(self):
        self.add([(1, 49, app(6)), (2, 1, "text"), (3, 49, app(6))])
        self.add([(1, 49, app(6)), (2, 49, app(6)), (4, 1, "text")],
                 shard="message__message_1.db")
        seen, before = [], None
        while True:
            page = self.search(category="files", before=before, limit=1)
            seen.extend(tuple(item["_sort"]) for item in page["messages"])
            if not page["hasMore"]:
                break
            before = page["nextCursor"]
        self.assertEqual(seen, [(3, "message__message_0.db", 3), (2, "message__message_1.db", 2),
                               (1, "message__message_1.db", 1), (1, "message__message_0.db", 1)])

    def test_keyword_category_and_date_are_intersected_with_both_timestamp_units(self):
        begin = int(datetime(2026, 10, 13, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
        self.fixture.add([(1, 49, app(6, "Travel plan"), begin, None),
                          (2, 49, app(6, "TRAVEL photos"), (begin + 86399) * 1000, None),
                          (3, 49, app(5, "Travel link"), begin + 100, None),
                          (4, 49, app(6, "Budget"), begin + 100, None),
                          (5, 49, app(6, "Travel next day"), begin + 86400, None),
                          (6, 49, app(6, "Travel before"), begin - 1, None)])
        self.assertEqual(self.sequences(self.search(category="files", query=" TrAvEl ", day="2026-10-13")), [2, 1])

    def test_category_date_query_and_cursor_validation_is_preserved(self):
        for value in ("", "unknown", None, 3, {}):
            with self.subTest(category=value), self.assertRaisesRegex(ValueError, "invalid history category"):
                self.search(category=value)
        for value in ("2026-02-30", "2026-1-01", "2026-10-13T00:00:00", 12):
            with self.subTest(day=value), self.assertRaisesRegex(ValueError, "invalid history date"):
                self.search(day=value)
        for value in ("x" * 257, "line\nfeed", 12):
            with self.subTest(query=value), self.assertRaisesRegex(ValueError, "invalid history query"):
                self.search(query=value)
        for value in ("!", encode_cursor("foreign-account", USER, (1, "message__message_0.db", 1)),
                      encode_cursor(ACCOUNT, "foreign-user", (1, "message__message_0.db", 1))):
            with self.subTest(cursor=value), self.assertRaisesRegex(ValueError, "invalid history cursor"):
                self.search(before=value)

    def test_category_search_retains_media_issuance_cap(self):
        self.add([(seq, 3, "[图片]") for seq in range(1, 9)])
        page = self.search(category="media", max_issued_images=3)
        self.assertEqual(len(page["messages"]), 8)
        self.assertEqual(len(self.fixture.source.issued_images), 3)
        self.assertEqual([key[2] for key in self.fixture.source.issued_images],
                         [item["id"] for item in page["messages"][-3:]])

    def test_service_passes_category_and_revalidates_account_scope(self):
        self.add([(1, 49, app(6)), (2, 1, "text")])
        backend = self.service()
        page = backend.history_search(ACCOUNT, USER, category="files")
        self.assertEqual(len(page["messages"]), 1)
        self.assertEqual(page["messages"][0]["historyCategory"], "files")
        self.assertNotIn("_sort", page["messages"][0])
        backend._assert_scope.assert_called_once_with((ACCOUNT, self.root))
        with self.assertRaises(AccountChangedError):
            backend.history_search("foreign-account", USER, category="files")
        backend._assert_scope.side_effect = AccountChangedError()
        with self.assertRaises(AccountChangedError):
            backend.history_search(ACCOUNT, USER, category="files")

    def test_http_category_route_accepts_latest_and_keeps_input_and_page_caps(self):
        self.add([(seq, (6 << 32) | 49, "[应用消息]") for seq in range(1, 102)])
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.service()))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(**params):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                query = urlencode({"account": ACCOUNT, "user": USER, **params})
                connection.request("GET", "/api/history/search?" + query)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        status, page = request(q="", date="")
        self.assertEqual(status, 200)
        self.assertEqual(len(page["messages"]), 50)
        status, page = request(category="files", limit=100)
        self.assertEqual(status, 200)
        self.assertEqual(len(page["messages"]), 100)
        self.assertTrue(page["hasMore"])
        self.assertTrue(all(item["historyCategory"] == "files" for item in page["messages"]))
        for params in ({"category": "unknown"}, {"category": ""}, {"limit": 101}, {"limit": 0},
                       {"date": "2026-02-30"}, {"before": "!"}):
            with self.subTest(params=params):
                self.assertEqual(request(**params)[0], 400)


if __name__ == "__main__":
    unittest.main()
