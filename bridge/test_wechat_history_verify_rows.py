"""Exact source verification uses one fresh read per selected message shard.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from backend_contracts import AccountChangedError, MessagesUnavailableError
from wechat_source import WeChatSource


class Connection:
    def __init__(self, path, queries):
        self.connection = sqlite3.connect(path)
        self.connection.set_trace_callback(queries.append)
        self.closed = False

    def execute(self, *args):
        return self.connection.execute(*args)

    def close(self):
        self.closed = True
        self.connection.close()


class HistoryVerifyRowsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user = "synthetic-friend"
        self.table = "Msg_" + hashlib.md5(self.user.encode()).hexdigest()
        self.paths, self.queries, self.connections = {}, [], []
        for index in (0, 1, 2):
            path = self.root / f"message__message_{index}.db"
            rel = f"message/message_{index}.db"
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
                conn.executemany("INSERT INTO Name2Id VALUES(?)", [("self-user",), (self.user,)])
                conn.execute(f"CREATE TABLE {self.table}(local_id INTEGER PRIMARY KEY,local_type INTEGER,"
                             "real_sender_id INTEGER,create_time INTEGER,message_content TEXT,"
                             "compress_content BLOB,server_id INTEGER,sort_seq INTEGER)")
                conn.executemany(f"INSERT INTO {self.table} VALUES(?,?,?,?,?,?,?,?)", [
                    (1, 1, 2, 1700000000, f"synthetic input {index}", None, 100 + index, 10),
                    (2, 1, 1, 1700000001, f"synthetic reply {index}", None, 200 + index, 11),
                    (3, 1, 1, 1700000002, "unrequested original", None, 300 + index, 12),
                ])
            self.paths[rel] = path
        self.db = SimpleNamespace(account="synthetic-account", workdir=str(self.root), messages_ready=True,
                                  _db_files=[(rel, str(path), 0) for rel, path in self.paths.items()],
                                  _open=self.open,
                                  _msg_conns=Mock(side_effect=AssertionError("must not scan all shards")))
        self.source = WeChatSource(factory=lambda: self.db)
        self.source._db = Mock(return_value=self.db)
        self.source._contacts = Mock(return_value={})
        self.source.self_user = Mock(return_value="self-user")
        self.source._classified_content = lambda _db, _kind, text, _compressed: ("文本", "text", text)

    def open(self, rel):
        connection = Connection(self.paths[rel], self.queries)
        self.connections.append((rel, connection))
        return connection

    @staticmethod
    def position(index, seq=10, local_id=1):
        return seq, f"message__message_{index}.db", local_id

    def test_exact_locators_are_batched_and_only_requested_shards_open(self):
        positions = [self.position(1), self.position(0), self.position(0, 11, 2), self.position(0)]
        found = self.source.history_verify_rows(self.user, positions)
        self.assertEqual(set(found), set(positions))
        self.assertEqual(found[self.position(0)]["text"], "synthetic input 0")
        self.assertEqual(found[self.position(1)]["text"], "synthetic input 1")
        self.assertEqual(found[self.position(0, 11, 2)]["side"], "self")
        self.assertEqual(found[self.position(0)]["_sort"], list(self.position(0)))
        self.assertEqual(sorted(rel for rel, _ in self.connections), ["message/message_0.db", "message/message_1.db"])
        self.assertTrue(all(conn.closed for _, conn in self.connections))
        selects = [query for query in self.queries if "SELECT local_id," in query]
        self.assertEqual(len(selects), 2)
        self.assertTrue(all("WHERE local_id IN (" in query for query in selects))
        self.assertEqual(self.source._db.call_count, 2)
        self.assertTrue(all(call.kwargs == {"fresh": True} for call in self.source._db.call_args_list))
        self.db._msg_conns.assert_not_called()

    def test_new_reads_observe_edits_deletes_and_wrong_sequences(self):
        position = self.position(0)
        first = self.source.history_verify_rows(self.user, [position])[position]
        with closing(sqlite3.connect(self.paths["message/message_0.db"])) as conn, conn:
            conn.execute(f"UPDATE {self.table} SET message_content='edited original' WHERE local_id=1")
        edited = self.source.history_verify_rows(self.user, [position])[position]
        self.assertNotEqual(first["text"], edited["text"])
        self.assertEqual(edited["text"], "edited original")
        self.assertEqual(self.source.history_verify_rows(self.user, [self.position(0, 9)]), {})
        with closing(sqlite3.connect(self.paths["message/message_0.db"])) as conn, conn:
            conn.execute(f"DELETE FROM {self.table} WHERE local_id=1")
        self.assertEqual(self.source.history_verify_rows(self.user, [position]), {})

    def test_missing_shard_table_and_message_are_absent(self):
        with closing(sqlite3.connect(self.paths["message/message_1.db"])) as conn, conn:
            conn.execute(f"DROP TABLE {self.table}")
        found = self.source.history_verify_rows(self.user, [self.position(99), self.position(1), self.position(0, 15, 99)])
        self.assertEqual(found, {})
        self.assertEqual(len(self.connections), 2)
        self.assertTrue(all(conn.closed for _, conn in self.connections))

    def test_unreadable_known_shard_does_not_become_missing_evidence(self):
        self.db._open = Mock(side_effect=sqlite3.OperationalError("synthetic unreadable snapshot"))
        with self.assertRaises(sqlite3.OperationalError):
            self.source.history_verify_rows(self.user, [self.position(0)])

    def test_invalid_locators_are_rejected_before_source_access(self):
        for positions in [None, [self.position(0)] * 501, [(1, "../message__message_0.db", 1)],
                          [(True, "message__message_0.db", 1)], [(1, "message__message_0.db", False)],
                          [(1, "message__message_0.db")], [(1, "message__message_0.db", "1")],
                          [(-1, "message__message_0.db", 1)], [(1, "message__message_0.db", 2**63)]]:
            with self.subTest(positions=positions):
                with self.assertRaises(ValueError):
                    self.source.history_verify_rows(self.user, positions)
        self.source._db.assert_not_called()
        self.assertEqual(self.source.history_verify_rows(self.user, []), {})

    def test_reader_replacement_or_account_mutation_discards_entire_batch(self):
        self.source._db.side_effect = [self.db, SimpleNamespace()]
        with self.assertRaises(AccountChangedError):
            self.source.history_verify_rows(self.user, [self.position(0)])
        self.assertTrue(all(conn.closed for _, conn in self.connections))
        self.source._db.side_effect = None
        self.source.self_user.side_effect = lambda _db: setattr(self.db, "account", "other-account") or "self-user"
        with self.assertRaises(AccountChangedError):
            self.source.history_verify_rows(self.user, [self.position(0)])

    def test_unready_live_reader_cannot_return_partial_evidence(self):
        self.source.live_account = True
        self.db.messages_ready = False
        with self.assertRaises(MessagesUnavailableError):
            self.source.history_verify_rows(self.user, [self.position(0)])
        self.assertEqual(self.connections, [])

    def test_wrong_opened_shard_is_rejected_and_closed(self):
        self.db._open = lambda _rel: self.open("message/message_1.db")
        with self.assertRaises(ValueError):
            self.source.history_verify_rows(self.user, [self.position(0)])
        self.assertTrue(all(conn.closed for _, conn in self.connections))

    def add_contact(self, user):
        table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
        with closing(sqlite3.connect(self.paths["message/message_0.db"])) as conn, conn:
            conn.execute(f"CREATE TABLE {table} AS SELECT * FROM {self.table}")
            conn.execute(f"UPDATE {table} SET message_content='second-contact original',server_id=999 WHERE local_id=1")
        return table

    def test_many_contacts_share_fresh_checks_and_shard_without_loading_names(self):
        other = "synthetic-second-friend"
        other_table = self.add_contact(other)
        self.source._contacts.side_effect = AssertionError("verification must not load display data")
        found = self.source.history_verify_rows_many([
            {"user": self.user, "positions": [self.position(0), self.position(0, 11, 2)]},
            {"user": other, "positions": [self.position(0)]},
        ])
        first, second = found[self.user][self.position(0)], found[other][self.position(0)]
        self.assertEqual(first["text"], "synthetic input 0")
        self.assertEqual(second["text"], "second-contact original")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(set(found[other]), {self.position(0)})
        self.assertEqual(found[self.user][self.position(0, 11, 2)]["side"], "self")
        self.assertEqual(len(self.connections), 1)
        self.assertTrue(self.connections[0][1].closed)
        self.assertEqual(self.source._db.call_count, 2)
        self.source._contacts.assert_not_called()
        self.source.self_user.assert_called_once()
        selects = [query for query in self.queries if "SELECT local_id," in query]
        self.assertEqual(len(selects), 2)
        self.assertTrue(any(other_table in query for query in selects))

    def test_live_batch_reuses_ordinary_reader_and_its_strict_check_refreshes_the_lease(self):
        self.source.live_account = True
        self.source._ownership_cache_enabled = True
        self.source.db = self.db
        found = self.source.history_verify_rows_many([{"user": self.user, "positions": [self.position(0)]}])
        self.assertIn(self.position(0), found[self.user])
        self.assertEqual([call.kwargs for call in self.source._db.call_args_list],
                         [{"fresh": True, "reuse_ownership": True}, {"fresh": True}])
        self.source._db.reset_mock()
        self.assertEqual(self.source.advisor_identity(), ("synthetic-account", self.root.resolve()))
        self.source._db.assert_not_called()

    def test_many_groups_have_one_total_bound_and_unique_contacts(self):
        valid = {"user": self.user, "positions": [self.position(0)]}
        for groups in [None, [valid] * 17, [valid, valid], [{"user": "", "positions": []}],
                       [{"user": self.user, "positions": (), "extra": 1}],
                       [{"user": self.user, "positions": [self.position(0)] * 251},
                        {"user": "other", "positions": [self.position(0)] * 250}]]:
            with self.subTest(groups=groups):
                with self.assertRaises(ValueError):
                    self.source.history_verify_rows_many(groups)
        self.source._db.assert_not_called()
        self.assertEqual(self.source.history_verify_rows_many([]), {})
        self.assertEqual(self.source.history_verify_rows_many([{"user": self.user, "positions": []}]), {self.user: {}})

    def test_many_contacts_fail_whole_batch_when_later_table_unreadable(self):
        other = "synthetic-second-friend"
        self.add_contact(other)
        # A later read failure must discard previously rendered contacts too.
        original = self.source._render_row
        def render(db, user, *args):
            if user == other:
                raise sqlite3.DatabaseError("synthetic unreadable requested evidence")
            return original(db, user, *args)
        self.source._render_row = render
        with self.assertRaises(sqlite3.DatabaseError):
            self.source.history_verify_rows_many([
                {"user": self.user, "positions": [self.position(0)]},
                {"user": other, "positions": [self.position(0)]},
            ])
        self.assertTrue(all(conn.closed for _, conn in self.connections))


if __name__ == "__main__":
    unittest.main()
