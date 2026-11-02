"""Batched fresh evidence checks keep exact source identity; synthetic data only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from backend_contracts import AccountChangedError
from core_account_db import AccountStores
from core_history import HistoryService
from core_projection import ContextProjection
from test_support import FakeSource, make_message


ACCOUNT = "synthetic-account-1"
FRIEND = "wxid_synthetic_friend"


class BatchSource(FakeSource):
    def __init__(self):
        super().__init__()
        self.batch_calls = []
        self.batch_hook = None

    def history_verify_rows(self, user, positions):
        self.batch_calls.append((user, list(positions)))
        if self.batch_hook:
            self.batch_hook(self)
        selected = set(positions)
        return {tuple(message["_sort"]): copy.deepcopy(message)
                for message in self.users.get(user, []) if tuple(message["_sort"]) in selected}


class ManyBatchSource(BatchSource):
    def __init__(self):
        super().__init__()
        self.many_calls = []
        self.many_hook = None

    def history_verify_rows_many(self, groups):
        self.many_calls.append(copy.deepcopy(groups))
        if self.many_hook:
            self.many_hook(self)
        return {group["user"]: {
            tuple(message["_sort"]): copy.deepcopy(message)
            for message in self.users.get(group["user"], [])
            if tuple(message["_sort"]) in group["positions"]} for group in groups}


class HistoryVerifyBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stores = AccountStores(Path(self.temp.name))
        self.addCleanup(self.stores.close)
        self.source = BatchSource()
        self.source.add_messages(FRIEND, [
            make_message(index, local_id=index, text="Synthetic original %d" % index)
            for index in range(1, 5)
        ])
        self.context = ContextProjection(self.stores, self.source)
        self.addCleanup(self.context.close)
        self.history = HistoryService(self.stores, self.context)
        self.database = self.stores.account(ACCOUNT)
        self.database.context_begin(FRIEND, None, self.context.source_scope(ACCOUNT))
        self.database.context_append_page(FRIEND, self.context._page_rows(
            FRIEND, self.source.users[FRIEND], 0), None, None)
        self.refs = [{"id": row["id"], "hash": row["hash"]}
                     for row in self.history.messages(ACCOUNT, FRIEND)["messages"]]
        self.source.history_page = Mock(side_effect=AssertionError("batch source must not page history"))

    def verify(self, refs=None, fresh=True):
        return self.history.verify(ACCOUNT, FRIEND, self.refs if refs is None else refs,
                                   fresh=fresh)["results"]

    def test_one_batch_preserves_request_order_and_duplicate_expected_hashes(self):
        refs = [self.refs[2], self.refs[0], {**self.refs[2], "hash": "wrong-hash"}, self.refs[1]]
        results = self.verify(refs)
        self.assertEqual([item["id"] for item in results], [ref["id"] for ref in refs])
        self.assertEqual([item["state"] for item in results], ["same", "same", "changed", "same"])
        self.assertEqual(len(self.source.batch_calls), 1)
        user, positions = self.source.batch_calls[0]
        self.assertEqual(user, FRIEND)
        self.assertEqual(set(positions), {tuple(row["_sort"]) for row in self.source.users[FRIEND][:3]})
        self.source.history_page.assert_not_called()

    def test_fresh_reads_detect_original_edits_and_deletions_without_reimport(self):
        self.source.users[FRIEND][0]["text"] = "Synthetic edited original"
        del self.source.users[FRIEND][1]
        self.assertEqual([item["state"] for item in self.verify()],
                         ["changed", "missing", "same", "same"])
        self.source.batch_calls.clear()
        self.assertEqual([item["state"] for item in self.verify(fresh=False)], ["same"] * 4)
        self.assertFalse(self.source.batch_calls, "ordinary verification should read only the projection")

    def test_sender_side_and_kind_changes_cannot_reuse_a_saved_hash(self):
        for changes in ({"senderId": "different-sender"}, {"side": "self"}, {"kind": "other"}):
            with self.subTest(changes=changes):
                original = copy.deepcopy(self.source.users[FRIEND][0])
                self.source.users[FRIEND][0].update(changes)
                self.assertEqual(self.verify([self.refs[0]])[0]["state"], "changed")
                self.source.users[FRIEND][0] = original

    def test_projection_missing_items_are_not_asked_from_the_source(self):
        refs = [{"id": "not-in-projection", "hash": "invented"}, self.refs[0]]
        self.assertEqual([item["state"] for item in self.verify(refs)], ["missing", "same"])
        self.assertEqual(self.source.batch_calls, [(FRIEND, [tuple(self.source.users[FRIEND][0]["_sort"])])])

    def test_empty_or_projection_missing_request_does_not_open_source_rows(self):
        self.assertEqual(self.verify([]), [])
        self.assertEqual(self.verify([{"id": "missing", "hash": "invented"}]),
                         [{"id": "missing", "state": "missing"}])
        self.assertFalse(self.source.batch_calls)

    def test_mismatched_returned_identity_cannot_validate_the_projection(self):
        original_batch = self.source.history_verify_rows
        for changes in ({"id": "reused-local-id-with-another-server-id"},
                        {"_sort": [99, "message__message_1.db", 1]},
                        {"_sort": [1, "message__message_2.db", 1]}):
            with self.subTest(changes=changes):
                def changed(user, positions):
                    rows = original_batch(user, positions)
                    for row in rows.values():
                        row.update(changes)
                    return rows
                self.source.history_verify_rows = changed
                self.assertNotEqual(self.verify([self.refs[0]])[0]["state"], "same")

    def test_invalid_saved_locator_is_unready_without_unsafe_source_lookup(self):
        malformed = ("not-json", "[true, \"message__message_1.db\", 1]",
                     "[-1, \"message__message_1.db\", 1]", "[1, 7, 1]", "[1, \"s\", false]")
        for locator in malformed:
            with self.subTest(locator=locator):
                with self.database.lock:
                    connection = self.database._connect()
                    with connection:
                        connection.execute("UPDATE context_messages SET sort_key=? WHERE user=? AND msg_id=?",
                                           (locator, FRIEND, self.refs[0]["id"]))
                self.source.batch_calls.clear()
                self.assertEqual(self.verify([self.refs[0]])[0]["state"], "unready")
                self.assertFalse(self.source.batch_calls)

    def test_parse_failure_is_unready_and_never_retries_legacy_page_reader(self):
        for failure in (ValueError("synthetic malformed row"), TypeError("synthetic malformed row"),
                        KeyError("synthetic malformed row")):
            with self.subTest(failure=type(failure).__name__):
                self.source.history_verify_rows = Mock(side_effect=failure)
                self.assertTrue(all(item["state"] == "unready" for item in self.verify()))
                self.source.history_page.assert_not_called()

    def test_unreadable_source_does_not_fall_back_to_stored_projection(self):
        self.source.history_verify_rows = Mock(side_effect=OSError("synthetic unreadable shard"))
        with self.assertRaises(OSError):
            self.verify()
        self.source.history_page.assert_not_called()

    def test_account_or_source_switch_during_batch_cannot_return_old_success(self):
        for field, value, error in (("account", "another-account", AccountChangedError),
                                    ("workdir", "C:/synthetic-replaced-source", ValueError)):
            with self.subTest(field=field):
                original = getattr(self.source, field)
                self.source.batch_hook = lambda source: setattr(source, field, value)
                with self.assertRaises(error):
                    self.verify()
                setattr(self.source, field, original)
        self.source.history_page.assert_not_called()

    def test_source_without_batch_capability_keeps_fresh_page_verification(self):
        legacy = FakeSource()
        legacy.users = copy.deepcopy(self.source.users)
        self.context.source = legacy
        self.assertEqual([item["state"] for item in self.verify()], ["same"] * 4)
        self.assertEqual(len(legacy.page_calls), 4)
        legacy.users[FRIEND][0]["text"] = "Synthetic edited legacy original"
        self.assertEqual(self.verify([self.refs[0]])[0]["state"], "changed")


class HistoryVerifyManyTests(unittest.TestCase):
    OTHER = "wxid_synthetic_other"

    def setUp(self):
        HistoryVerifyBatchTests.setUp(self)
        previous = self.source
        self.source = ManyBatchSource()
        self.source.users = copy.deepcopy(previous.users)
        self.source.history_page = Mock(side_effect=AssertionError("multi batch must not page history"))
        self.source.history_verify_rows = Mock(side_effect=AssertionError("multi batch must not read contacts separately"))
        self.context.source = self.source
        # IDs and locators deliberately collide across conversations. Only the
        # account + conversation binding distinguishes these synthetic facts.
        other_messages = copy.deepcopy(self.source.users[FRIEND])
        for row in other_messages:
            row["text"] = "Synthetic other conversation " + row["text"]
        self.source.add_messages(self.OTHER, other_messages)
        self.database.context_begin(self.OTHER, None, self.context.source_scope(ACCOUNT))
        self.database.context_append_page(self.OTHER, self.context._page_rows(
            self.OTHER, other_messages, 0), None, None)
        self.other_refs = [{"id": row["id"], "hash": row["hash"]}
                           for row in self.history.messages(ACCOUNT, self.OTHER)["messages"]]

    def groups(self):
        return [{"user": self.OTHER, "items": self.other_refs[:2]},
                {"user": FRIEND, "items": self.refs[:2]}]

    def test_multiple_contacts_share_one_source_read_and_leased_scope_bracket(self):
        groups = self.groups()
        groups[1]["items"] = [self.refs[1], self.refs[0], {**self.refs[1], "hash": "wrong"}]
        with patch.object(self.context, "source_scope", wraps=self.context.source_scope) as scopes:
            result = self.history.verify_many(ACCOUNT, groups)["results"]
        self.assertEqual([group["user"] for group in result], [self.OTHER, FRIEND])
        self.assertEqual([[row["state"] for row in group["results"]] for group in result],
                         [["same", "same"], ["same", "same", "changed"]])
        self.assertEqual([row["id"] for row in result[1]["results"]],
                         [ref["id"] for ref in groups[1]["items"]])
        # The source read checks ownership strictly after reading; the bracket reuses that proof.
        self.assertEqual(scopes.call_args_list, [call(ACCOUNT), call(ACCOUNT)])
        self.assertEqual(len(self.source.many_calls), 1)
        self.source.history_verify_rows.assert_not_called()
        self.source.history_page.assert_not_called()

    def test_colliding_ids_and_locators_cannot_cross_conversation_boundaries(self):
        groups = [{"user": self.OTHER, "items": [self.refs[0]]},
                  {"user": FRIEND, "items": [self.refs[0]]}]
        result = self.history.verify_many(ACCOUNT, groups)["results"]
        self.assertEqual([group["results"][0]["state"] for group in result], ["changed", "same"])
        del self.source.users[self.OTHER][0]
        result = self.history.verify_many(ACCOUNT, self.groups())["results"]
        self.assertEqual([group["results"][0]["state"] for group in result], ["missing", "same"])

    def test_single_contact_verify_uses_the_shared_reader_when_available(self):
        result = self.history.verify(ACCOUNT, FRIEND, self.refs[:1], fresh=True)
        self.assertEqual(result, {"results": [{"id": self.refs[0]["id"], "state": "same"}]})
        self.assertEqual(self.source.many_calls, [[{"user": FRIEND,
            "positions": [tuple(self.source.users[FRIEND][0]["_sort"])]}]])
        self.source.history_verify_rows.assert_not_called()

    def test_malformed_or_oversized_groups_are_rejected_before_io(self):
        invalid = [None, {}, [None], [{"user": FRIEND}], [{"user": FRIEND, "items": [], "extra": 1}],
                   [{"user": "", "items": []}], [{"user": "../other", "items": []}],
                   [{"user": FRIEND, "items": None}], [{"user": FRIEND, "items": [None]}],
                   [{"user": FRIEND, "items": [{"id": 3}]}],
                   [{"user": FRIEND, "items": []}, {"user": FRIEND, "items": []}],
                   [{"user": "user-%d" % index, "items": []} for index in range(17)],
                   [{"user": FRIEND, "items": [self.refs[0]] * 500},
                    {"user": self.OTHER, "items": [self.other_refs[0]]}]]
        with patch.object(self.context, "source_scope", side_effect=AssertionError("invalid request did I/O")), \
                patch.object(self.history, "_database", side_effect=AssertionError("invalid request read database")):
            for groups in invalid:
                with self.subTest(groups=groups):
                    with self.assertRaises(ValueError):
                        self.history.verify_many(ACCOUNT, groups)
        self.assertFalse(self.source.many_calls)

    def test_limits_allow_sixteen_contacts_and_five_hundred_total_refs(self):
        groups = [{"user": "user-%d" % index, "items": []} for index in range(15)]
        groups.append({"user": FRIEND, "items": [self.refs[0]] * 500})
        result = self.history.verify_many(ACCOUNT, groups)["results"]
        self.assertEqual(len(result), 16)
        self.assertEqual(len(result[-1]["results"]), 500)
        self.assertTrue(all(row["state"] == "same" for row in result[-1]["results"]))
        self.assertEqual(len(self.source.many_calls[0]), 1)
        self.assertEqual(len(self.source.many_calls[0][0]["positions"]), 1)

    def test_empty_and_unknown_contacts_do_not_read_source(self):
        self.assertEqual(self.history.verify_many(ACCOUNT, []), {"results": []})
        groups = [{"user": FRIEND, "items": [{"id": "unknown", "hash": "invented"}]},
                  {"user": self.OTHER, "items": []}]
        self.assertEqual(self.history.verify_many(ACCOUNT, groups), {"results": [
            {"user": FRIEND, "results": [{"id": "unknown", "state": "missing"}]},
            {"user": self.OTHER, "results": []}]})
        self.assertFalse(self.source.many_calls)

    def test_source_failures_never_validate_saved_projection_or_retry_old_paths(self):
        for failure in (ValueError("synthetic malformed batch"), TypeError("synthetic malformed batch"),
                        KeyError("synthetic malformed batch")):
            with self.subTest(failure=type(failure).__name__):
                self.source.history_verify_rows_many = Mock(side_effect=failure)
                result = self.history.verify_many(ACCOUNT, self.groups())["results"]
                self.assertTrue(all(row["state"] == "unready" for group in result for row in group["results"]))
        self.source.history_verify_rows_many = Mock(side_effect=OSError("synthetic unreadable shard"))
        with self.assertRaises(OSError):
            self.history.verify_many(ACCOUNT, self.groups())
        self.source.history_verify_rows.assert_not_called()
        self.source.history_page.assert_not_called()

    def test_account_and_source_switch_during_multi_read_are_rejected(self):
        for field, value, error in (("account", "another-account", AccountChangedError),
                                    ("workdir", "C:/synthetic-source-replaced", ValueError)):
            with self.subTest(field=field):
                original = getattr(self.source, field)
                self.source.many_hook = lambda source: setattr(source, field, value)
                with self.assertRaises(error):
                    self.history.verify_many(ACCOUNT, self.groups())
                setattr(self.source, field, original)

    def test_sources_without_multi_reader_use_existing_verified_single_contact_path(self):
        legacy_batch = BatchSource()
        legacy_batch.users = copy.deepcopy(self.source.users)
        legacy_batch.history_page = Mock(side_effect=AssertionError("single batch reader should be used"))
        self.context.source = legacy_batch
        result = self.history.verify_many(ACCOUNT, self.groups())["results"]
        self.assertTrue(all(row["state"] == "same" for group in result for row in group["results"]))
        self.assertEqual([user for user, _positions in legacy_batch.batch_calls], [self.OTHER, FRIEND])
        legacy_batch.history_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
