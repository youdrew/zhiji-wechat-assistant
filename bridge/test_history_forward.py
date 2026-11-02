"""Synthetic forward history paging checks; no live WeChat or model calls.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import http.client
import itertools
import json
import sqlite3
import tempfile
import threading
import unittest
from collections import OrderedDict
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from backend_contracts import AccountChangedError
from backend_service import Backend
from history_browser import HISTORY_SCAN_LIMIT, browse, decode_cursor, encode_cursor
from real_http import make_handler


ACCOUNT = "synthetic-account"
USER = "synthetic-friend"
TABLE = "Msg_" + "a" * 32


class HistoryFixture:
    def __init__(self, root):
        self.root = root
        self.account = ACCOUNT
        self.lock = threading.RLock()
        self.issued_images = OrderedDict()
        self.paths = []

    def add(self, shard, records):
        path = self.root / shard
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.execute("INSERT INTO Name2Id VALUES (?)", (USER,))
            conn.execute(f"CREATE TABLE {TABLE} (local_id INTEGER, local_type INTEGER, "
                         "real_sender_id INTEGER, create_time INTEGER, message_content TEXT, "
                         "compress_content BLOB, server_id INTEGER, sort_seq INTEGER)")
            conn.executemany(f"INSERT INTO {TABLE} VALUES (?,?,1,0,'synthetic',NULL,0,?)", records)
        self.paths.append(path)

    def _db(self, fresh=True):
        return self

    def _msg_conns(self, user):
        assert user == USER
        return [(sqlite3.connect(path), TABLE) for path in self.paths]

    def _contacts(self, db):
        return {}

    def self_user(self, db):
        return "synthetic-self"

    def _render_row(self, db, user, item, contacts, own_user):
        record, shard, _senders = item
        if record[1] == 10000:
            return None
        position = (record[7], shard, record[0])
        return {"id": f"{shard}:{record[0]}", "kind": "text", "text": "synthetic",
                "historyCursor": encode_cursor(self.account, user, position),
                "_sort": position}


class ForwardHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = HistoryFixture(self.root)

    @staticmethod
    def cursor(seq, shard="message__message_0.db", local_id=None):
        return encode_cursor(ACCOUNT, USER, (seq, shard, seq if local_id is None else local_id))

    def page(self, **kwargs):
        return browse(self.source, ACCOUNT, USER, **kwargs)

    def test_forward_pages_merge_shards_without_duplicates_or_gaps(self):
        self.source.add("message__message_0.db", [(1, 1, 1), (2, 1, 2), (4, 1, 4)])
        self.source.add("message__message_1.db", [(2, 1, 2), (3, 1, 3), (5, 1, 5)])
        cursor = self.cursor(1)
        seen, flags = [], []
        while True:
            page = self.page(after=cursor, limit=2)
            seen.extend(message["id"] for message in page["messages"])
            flags.append(page["hasMoreAfter"])
            self.assertTrue(page["hasMoreBefore"])
            self.assertIsNone(page["focusId"])
            next_cursor = page["nextCursor"]
            self.assertGreater(decode_cursor(next_cursor, ACCOUNT, USER),
                               decode_cursor(cursor, ACCOUNT, USER))
            if not page["hasMoreAfter"]:
                break
            cursor = next_cursor
        self.assertEqual(seen, ["message__message_0.db:2", "message__message_1.db:2",
                                "message__message_1.db:3", "message__message_0.db:4",
                                "message__message_1.db:5"])
        self.assertEqual(flags, [True, True, False])

    def test_empty_scan_page_advances_past_4096_hidden_messages(self):
        final = HISTORY_SCAN_LIMIT + 2
        self.source.add("message__message_0.db", [(1, 1, 1)] +
                        [(seq, 10000, seq) for seq in range(2, final)] + [(final, 1, final)])
        first = self.page(after=self.cursor(1))
        self.assertEqual(first["messages"], [])
        self.assertTrue(first["hasMoreAfter"])
        self.assertIsNone(first["newestCursor"])
        self.assertEqual(decode_cursor(first["nextCursor"], ACCOUNT, USER)[0], final - 1)
        second = self.page(after=first["nextCursor"])
        self.assertEqual([item["id"] for item in second["messages"]],
                         [f"message__message_0.db:{final}"])
        self.assertFalse(second["hasMoreAfter"])

    def test_forward_end_is_empty_and_stops(self):
        self.source.add("message__message_0.db", [(1, 1, 1)])
        page = self.page(after=self.cursor(1))
        self.assertEqual(page["messages"], [])
        self.assertFalse(page["hasMoreAfter"])
        self.assertIsNone(page["nextCursor"])

    def test_forward_page_retains_scan_progress_after_visible_message(self):
        self.source.add("message__message_0.db", [(1, 1, 1), (2, 1, 2), (3, 10000, 3)])
        page = self.page(after=self.cursor(1))
        self.assertEqual(page["newestCursor"], self.cursor(2))
        self.assertEqual(page["nextCursor"], self.cursor(3))
        self.assertFalse(page["hasMoreAfter"])

    def test_cursor_directions_are_mutually_exclusive(self):
        for keys in itertools.combinations(("before", "around", "after"), 2):
            with self.subTest(keys=keys), self.assertRaisesRegex(ValueError, "cannot be combined"):
                self.page(**dict.fromkeys(keys, self.cursor(1)))
        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            self.page(before=self.cursor(1), around=self.cursor(1), after=self.cursor(1))

    def test_forward_cursor_scope_and_live_account_remain_checked(self):
        invalid = ["!", encode_cursor("other-account", USER, (1, "message__message_0.db", 1)),
                   encode_cursor(ACCOUNT, "other-friend", (1, "message__message_0.db", 1))]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.page(after=value)
        self.source.account = "changed-account"
        with self.assertRaisesRegex(ValueError, "account mismatch"):
            self.page(after=self.cursor(1))

    def test_before_and_around_keep_existing_order_and_cursors(self):
        self.source.add("message__message_0.db", [(seq, 1, seq) for seq in range(1, 8)])
        before = self.page(before=self.cursor(5), limit=3)
        self.assertEqual([item["_sort"][0] for item in before["messages"]], [2, 3, 4])
        self.assertEqual(before["nextCursor"], self.cursor(2))
        self.assertTrue(before["hasMoreBefore"])
        around = self.page(around=self.cursor(4), limit=5)
        self.assertEqual([item["_sort"][0] for item in around["messages"]], [2, 3, 4, 5, 6])
        self.assertEqual(around["nextCursor"], self.cursor(2))
        self.assertTrue(around["hasMoreBefore"])
        self.assertTrue(around["hasMoreAfter"])

    def service(self):
        backend = SimpleNamespace(source=self.source, _scoped_identity=lambda: (ACCOUNT, self.root),
                                  _assert_scope=Mock())
        backend.history = MethodType(Backend.history, backend)
        return backend

    def test_service_keeps_forward_cursor_and_revalidates_scope(self):
        self.source.add("message__message_0.db", [(1, 1, 1), (2, 1, 2)])
        backend = self.service()
        page = backend.history(ACCOUNT, USER, after=self.cursor(1))
        self.assertEqual(page["nextCursor"], self.cursor(2))
        self.assertNotIn("_sort", page["messages"][0])
        backend._assert_scope.assert_called_once_with((ACCOUNT, self.root))
        with self.assertRaises(AccountChangedError):
            backend.history("another-account", USER, after=self.cursor(1))
        backend._assert_scope.side_effect = AccountChangedError()
        with self.assertRaises(AccountChangedError):
            backend.history(ACCOUNT, USER, after=self.cursor(1))

    def test_http_forward_route_keeps_page_limit_and_direction_validation(self):
        self.source.add("message__message_0.db", [(seq, 1, seq) for seq in range(1, 203)])
        backend = self.service()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(backend))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(**params):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                query = urlencode({"account": ACCOUNT, "user": USER, "after": self.cursor(1), **params})
                connection.request("GET", "/api/history?" + query)
                response = connection.getresponse()
                return response.status, json.loads(response.read())
            finally:
                connection.close()

        status, page = request()
        self.assertEqual(status, 200)
        self.assertEqual(len(page["messages"]), 80)
        self.assertEqual(page["nextCursor"], self.cursor(81))
        status, page = request(limit=200)
        self.assertEqual(status, 200)
        self.assertEqual(len(page["messages"]), 200)
        self.assertTrue(page["hasMoreAfter"])
        for params in ({"limit": 201}, {"limit": 0}, {"limit": -1},
                       {"before": self.cursor(2)}, {"around": self.cursor(2)}):
            with self.subTest(params=params):
                self.assertEqual(request(**params)[0], 400)


if __name__ == "__main__":
    unittest.main()
