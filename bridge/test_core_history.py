"""Plugin history reads use stable, bounded WAL snapshots instead of the writer lock."""
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from backend_contracts import AccountChangedError, AccountPreparationPending, AccountUnavailableError, MessagesUnavailableError
from core_account_db import AccountStores
from core_contracts import AdvisorError
from core_history import HistoryService
from test_core_account_db import context_row


class HistorySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stores = AccountStores(Path(self.temp.name))
        self.database = self.stores.account("a")
        self.database.context_begin("u", None, "saved-source")
        self.database.context_append_page("u", [context_row(i, f"original-{i}") for i in range(1, 602)], None, None)
        self.context = SimpleNamespace(source_scope=lambda _account, fresh=False: "fresh-source" if fresh else "live-source",
                                       importing=lambda *_: False)
        self.history = HistoryService(self.stores, self.context)

    def tearDown(self):
        self.stores.close()
        self.temp.cleanup()

    def test_export_remains_one_snapshot_when_core_replaces_projection_between_pages(self):
        stream = self.history.export("a", "u")
        first = json.loads(next(stream))
        self.database.context_begin("u", None, "new-source", reset=True)
        self.database.context_append_page("u", [context_row(i, f"changed-{i}") for i in range(1, 603)], None, None)
        rest = [json.loads(line) for line in stream]
        self.assertEqual(first["text"], "original-1")
        self.assertEqual(rest[-2]["text"], "original-601")
        self.assertEqual(rest[-1]["count"], 601)
        self.assertEqual(rest[-1]["watermark"]["seq"], 601)
        self.assertEqual(self.history.messages("a", "u", after=600)["messages"][0]["text"], "changed-601")

    def test_paged_plugin_reads_do_not_wait_for_core_writer_lock(self):
        finished = threading.Event()
        result = []
        with self.database.lock:
            reader = threading.Thread(target=lambda: (result.append(self.history.messages("a", "u", limit=1)),
                                                       finished.set()))
            reader.start()
            completed = finished.wait(1)
        reader.join(3)
        self.assertTrue(completed, "history query was serialized behind the Core writer lock")
        self.assertEqual(result[0]["messages"][0]["text"], "original-1")

    def test_account_close_releases_a_snapshot_even_if_consumer_stopped_reading(self):
        stream = self.history.export("a", "u")
        next(stream)
        self.database.close()
        with self.assertRaises(AdvisorError):
            list(stream)
        self.stores.remove_account("a")
        self.assertFalse(self.database.directory.exists())

    def test_status_distinguishes_saved_source_from_current_and_fresh_identity(self):
        status = self.history.status("a", "u")
        self.assertEqual((status["sourceFingerprint"], status["currentSourceFingerprint"]),
                         ("saved-source", "live-source"))
        self.assertEqual(self.history.status("a", "u", fresh=True)["currentSourceFingerprint"], "fresh-source")
        self.assertEqual(self.history.status("a", "missing")["currentSourceFingerprint"], "live-source")

    def test_status_many_matches_ordinary_status_without_reading_source_metadata(self):
        single = {key: value for key, value in self.history.status("a", "u").items() if key != "sourceRevision"}
        revision, prefix = Mock(return_value="revision"), Mock(return_value="prefix")
        self.context.source = SimpleNamespace(history_revision=revision, history_prefix_signature=prefix)
        result = self.history.status_many("a", ["u", "missing"])
        self.assertEqual(result["currentSourceFingerprint"], "live-source")
        self.assertEqual(result["results"][0], {"user": "u", **single})
        self.assertEqual(result["results"][1]["state"], "idle")
        revision.assert_not_called()
        prefix.assert_not_called()
        for invalid in (None, "u", ["u", "u"], [""], ["a/b"], [3], ["u"] * 501):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.history.status_many("a", invalid)

    def test_status_many_rejects_a_source_switch_during_the_sweep(self):
        scopes = iter(["source-before", "source-after"])
        self.context.source_scope = lambda _account, fresh=False: next(scopes)
        with self.assertRaises(ValueError):
            self.history.status_many("a", ["u"])

    def test_status_source_revision_detects_source_edits_without_importing_or_reading_text(self):
        read = Mock(side_effect=["source-before", "source-edited", "source-edited"])
        self.context.source = SimpleNamespace(history_revision=read,
                                             history_page=Mock(side_effect=AssertionError("must not read text")))
        before = self.history.status("a", "u")
        edited = self.history.status("a", "u")
        self.assertEqual((before["sourceRevision"], edited["sourceRevision"]), ("source-before", "source-edited"))
        self.assertEqual((before["revision"], before["highwater"], before["epoch"]),
                         (edited["revision"], edited["highwater"], edited["epoch"]))
        self.assertEqual(self.history.status("a", "missing")["sourceRevision"], "source-edited")
        self.assertEqual(read.call_args_list, [(("u",),), (("u",),), (("missing",),)])
        self.context.source.history_page.assert_not_called()

    def test_status_only_audits_saved_prefix_when_requested(self):
        ceiling = json.dumps([500, "message__message_0.db", 50])
        self.database.context_begin("u", ceiling, "saved-source")
        prefix = Mock(return_value="prefix-signature")
        self.context.source = SimpleNamespace(history_revision=lambda _user: "source-revision",
                                             history_prefix_signature=prefix)
        status = self.history.status("a", "u")
        self.assertNotIn("sourcePrefixSignature", status)
        prefix.assert_not_called()
        audit = self.history.status("a", "u", audit=True)
        self.assertEqual((audit["sourcePrefixSignature"], audit["sourcePrefixCeiling"]),
                         ("prefix-signature", ceiling))
        prefix.assert_called_once_with("u", (500, "message__message_0.db", 50))
        idle = self.history.status("a", "missing", audit=True)
        self.assertIsNone(idle["sourcePrefixSignature"])
        self.assertIsNone(idle["sourcePrefixCeiling"])
        self.assertEqual(prefix.call_count, 1)

    def test_status_audits_requested_old_prefix_after_projection_has_advanced(self):
        previous = json.dumps([400, "message__message_0.db", 40])
        current = json.dumps([500, "message__message_0.db", 50])
        self.database.context_begin("u", current, "saved-source")
        prefix = Mock(return_value="old-prefix-signature")
        self.context.source = SimpleNamespace(history_prefix_signature=prefix)
        audit = self.history.status("a", "u", audit=True, audit_highwater=previous)
        self.assertEqual((audit["highwater"], audit["sourcePrefixCeiling"]), (current, previous))
        self.assertEqual(audit["sourcePrefixSignature"], "old-prefix-signature")
        prefix.assert_called_once_with("u", (400, "message__message_0.db", 40))
        for invalid in ("", "null", "[]", "not-json", 3, [400, "shard", 40], " " * 513):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.history.status("a", "u", audit=True, audit_highwater=invalid)
        with self.assertRaises(ValueError):
            self.history.status("a", "u", audit_highwater=previous)

    def test_status_metadata_failures_and_unsupported_sources_are_explicitly_unknown(self):
        self.assertIsNone(self.history.status("a", "u")["sourceRevision"])
        ceiling = json.dumps([500, "message__message_0.db", 50])
        self.database.context_begin("u", ceiling, "saved-source")
        self.context.source = SimpleNamespace(history_revision=Mock(side_effect=OSError("PRIVATE_SOURCE_PATH")),
            history_prefix_signature=Mock(side_effect=RuntimeError("PRIVATE_SOURCE_DETAIL")))
        audit = self.history.status("a", "u", audit=True)
        self.assertIsNone(audit["sourceRevision"])
        self.assertIsNone(audit["sourcePrefixSignature"])
        self.assertNotIn("PRIVATE", json.dumps(audit))
        for invalid in (None, "", [], 3):
            self.context.source.history_revision = lambda _user, value=invalid: value
            self.assertIsNone(self.history.status("a", "u")["sourceRevision"])

    def test_status_metadata_propagates_account_and_source_failures(self):
        source_failure = RuntimeError("synthetic source change")
        source_failure.code = "source-changed"
        for failure in (AccountChangedError(), AccountPreparationPending(), AccountUnavailableError(),
                        MessagesUnavailableError(), source_failure):
            with self.subTest(failure=type(failure).__name__):
                self.context.source = SimpleNamespace(history_revision=Mock(side_effect=failure))
                with self.assertRaises(type(failure)):
                    self.history.status("a", "u")

    def test_status_metadata_read_is_bracketed_by_source_identity_even_when_read_fails(self):
        for failure in (False, True):
            with self.subTest(read_failure=failure):
                scopes = iter(("source-before", "source-after"))
                self.context.source_scope = lambda _account, fresh=False: next(scopes)
                self.context.source = SimpleNamespace(history_revision=Mock(
                    side_effect=OSError("synthetic failure") if failure else None, return_value="revision"))
                with self.assertRaisesRegex(ValueError, "source changed"):
                    self.history.status("a", "u", fresh=True)

    def test_status_prefix_audit_also_rejects_identity_switch_without_revision_capability(self):
        self.database.context_begin("u", json.dumps([500, "message__message_0.db", 50]), "saved-source")
        scopes = iter(("source-before", "source-after"))
        self.context.source_scope = lambda _account, fresh=False: next(scopes)
        self.context.source = SimpleNamespace(history_prefix_signature=lambda *_: "prefix-signature")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.history.status("a", "u", audit=True)

    def test_sticker_identities_are_asked_only_for_projected_sticker_messages(self):
        sticker = json.dumps({"_sort": [700, "s", 700], "media": {"kind": "sticker"}})
        self.database.context_append_page("u", [context_row(700, "[表情]", msg_id="m-700", raw=sticker,
                                                            sort_key='[700, "s", 700]')], None, None)
        asked = []
        self.context.source = SimpleNamespace(sticker_details=lambda user, refs: (
            asked.append((user, refs)) or {refs[0][2]: {"id": "a" * 32}}))
        result = self.history.stickers("a", "u", ["m-700", "m-1", "missing"])
        self.assertEqual(result, {"stickers": {"m-700": {"id": "a" * 32}}})
        self.assertEqual(asked, [("u", [("s", 700, "m-700")])])
        self.assertEqual(self.history.stickers("a", "u", ["m-1"]), {"stickers": {}})
        for ids in ([], "m-700", ["x"] * 1001, [7]):
            with self.assertRaises(ValueError):
                self.history.stickers("a", "u", ids)
        scopes = iter(("before", "after"))
        self.context.source_scope = lambda _account, fresh=False: next(scopes)
        with self.assertRaises(ValueError):
            self.history.stickers("a", "u", ["m-700"])
        self.context.source = SimpleNamespace()
        self.assertEqual(self.history.stickers("a", "u", ["m-700"])["error"], "unsupported")

    def test_reader_limit_reports_busy_and_releases_capacity(self):
        with ExitStack() as stack:
            for _ in range(8):
                stack.enter_context(self.database.read_snapshot())
            with self.assertRaises(AdvisorError) as busy:
                self.history.messages("a", "u", limit=1)
            self.assertEqual(busy.exception.code, "data-busy")
            # Bounded plugin readers cannot prevent the Core writer from advancing.
            self.database.context_append_page("u", [context_row(602, "new")], None, None)
        self.assertEqual(self.history.messages("a", "u", after=601)["messages"][0]["text"], "new")

    def test_sender_verification_reads_exact_original_even_for_old_projection(self):
        calls = []
        def page(user, upto, **kwargs):
            calls.append((user, upto, kwargs))
            return ([{"id": "m-1", "_senderResolved": True, "senderId": "wxid", "side": "other", "kind": "text"}], upto)
        self.context.source = SimpleNamespace(history_page=page)
        before = self.history.rows("a", "u", ["m-1"])["messages"][0]
        verified = self.history.rows("a", "u", ["m-1"], verify_sender=True)["messages"][0]
        self.assertTrue(verified["senderResolved"])
        self.assertNotIn("senderResolved", before)
        self.assertEqual((verified["raw"], verified["hash"]), (before["raw"], before["hash"]))
        self.assertEqual(calls, [("u", (1, "s", 1), {"after": (1, "s", 0), "page_size": 1})])

    def test_unresolved_fallback_and_changed_live_sender_are_not_trusted(self):
        for changes in ({"_senderResolved": False}, {"side": "self"}, {"senderId": "someone-else"},
                        {"id": "another-message"}, {"kind": "system"}):
            with self.subTest(changes=changes):
                live = {"id": "m-1", "_senderResolved": True, "senderId": "wxid", "side": "other", "kind": "text", **changes}
                self.context.source = SimpleNamespace(history_page=lambda *_args, **_kwargs: ([live], None))
                result = self.history.rows("a", "u", ["m-1"], verify_sender=True)
                self.assertFalse(result["messages"][0]["senderResolved"])

    def test_sender_verification_rejects_source_switch_and_multi_target(self):
        def page(*_args, **_kwargs):
            self.context.source_scope = lambda *_args, **_kwargs: "changed-source"
            return [], None
        self.context.source = SimpleNamespace(history_page=page)
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.history.rows("a", "u", ["m-1"], verify_sender=True)
        with self.assertRaises(ValueError):
            self.history.rows("a", "u", ["m-1", "m-2"], verify_sender=True)


if __name__ == "__main__":
    unittest.main()
