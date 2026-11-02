"""Core's chat projection: multi-shard import, shared windows, source identity and media metadata.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Cases moved from test_advisor_context.py (imports), test_advisor_shared_context.py,
test_advisor_source_identity.py and test_advisor_media.py (metadata), 2026-10-13. All data is synthetic.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend_contracts import AccountUnavailableError, MessageWindow, MessageWindowBatch, MessagesUnavailableError
from backend_service import Backend
from core_account_db import AccountStores
from core_accounts import AccountDataService
from core_projection import ContextProjection, media_metadata
from test_support import FakeSource, make_message, wait_until
from wechat_source import WeChatSource

ACCOUNT = "synthetic-account-1"
USER = "user-a"


def _wait_ready(projection, account, user):
    def ready():
        snapshot = projection.snapshot(account, user)
        return snapshot if snapshot["state"] == "ready" else None

    return wait_until(ready)


class ImportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = FakeSource()
        self.stores = AccountStores(self.root)
        self.projection = ContextProjection(self.stores, self.source)
        self.addCleanup(self.stores.close)
        self.addCleanup(self.projection.close)

    def test_full_import_covers_multi_shard_history(self):
        self.source.seed_pages("user-a", 600, shards=["message__message_1.db",
                                                     "message__message_2.db",
                                                     "message__message_3.db"])
        snapshot = self.projection.prepare("synthetic-account-1", "user-a")
        self.assertIn(snapshot["state"], ("reading", "ready"))
        ready = _wait_ready(self.projection, "synthetic-account-1", "user-a")
        self.assertIsNotNone(ready, "import did not become ready")
        self.assertEqual(ready["readCount"], 600)
        self.assertGreaterEqual(ready["revision"], 3)
        self.assertGreaterEqual(len(self.source.page_calls), 3)
        rows = self.projection.messages("synthetic-account-1", "user-a")
        self.assertEqual([row["seq"] for row in rows], list(range(1, 601)))
        self.assertEqual([row["msg_id"] for row in rows],
                         ["msg-message__message_%d.db-%d-%d" % (((seq - 1) % 3) + 1, seq, seq)
                          for seq in range(1, 601)])
        raw = json.loads(rows[0]["raw"])
        self.assertEqual(raw["_sort"], [1, "message__message_1.db", 1])
        self.assertEqual(rows[0]["text"], "消息内容 1 " + "x" * 16)

    def test_incremental_append_reads_only_new_messages(self):
        self.source.seed_pages("user-a", 300)
        self.projection.prepare("synthetic-account-1", "user-a")
        _wait_ready(self.projection, "synthetic-account-1", "user-a")
        first = self.projection.snapshot("synthetic-account-1", "user-a")
        before = [row["msg_id"] for row in self.projection.messages("synthetic-account-1", "user-a")]
        old_highwater = self.source.history_highwater("user-a")
        self.source.seed_pages("user-a", 5, start_seq=301, text_size=4)
        self.projection.prepare("synthetic-account-1", "user-a")
        ready = _wait_ready(self.projection, "synthetic-account-1", "user-a")
        self.assertEqual(ready["readCount"], 305)
        self.assertGreater(ready["revision"], first["revision"])
        after = [row["msg_id"] for row in self.projection.messages("synthetic-account-1", "user-a")]
        self.assertEqual(after[:300], before)
        self.assertEqual(len(after), 305)
        self.assertIn(old_highwater, [call["after"] for call in self.source.page_calls])

    def test_identity_switch_mid_import_saves_no_late_page(self):
        self.source.seed_pages("user-a", 600)
        other = "synthetic-account-2"

        def switch_on_second_page(source):
            if len(source.page_calls) == 2:
                source.account = other

        self.source.page_hook = switch_on_second_page
        self.projection.prepare("synthetic-account-1", "user-a")
        failed = wait_until(lambda: self.projection.snapshot("synthetic-account-1", "user-a")
                            if self.projection.snapshot("synthetic-account-1", "user-a")["state"]
                            == "error" else None)
        self.assertIsNotNone(failed, "identity switch was not detected")
        self.assertIn("账号", failed["error"])
        self.assertEqual(failed["readCount"], 256)
        rows = self.projection.messages("synthetic-account-1", "user-a")
        self.assertEqual(len(rows), 256)
        data_root = self.root / ".local" / "advisor-data"
        digests = sorted(path.name for path in data_root.iterdir())
        self.assertEqual(digests, [hashlib.sha256(b"synthetic-account-1").hexdigest()])
        self.assertFalse((data_root / hashlib.sha256(other.encode()).hexdigest()).exists())

    def test_cancel_before_commit_discards_the_read_page(self):
        self.source.seed_pages("user-a", 600)
        fired = threading.Event()

        def cancel_after_read(_source):
            if not fired.is_set():
                fired.set()
                entry = self.projection._imports[("synthetic-account-1", "user-a")]
                entry.cancel.set()

        self.source.page_hook = cancel_after_read
        self.projection.prepare("synthetic-account-1", "user-a")
        self.assertTrue(wait_until(fired.is_set))
        snapshot = self.projection.snapshot("synthetic-account-1", "user-a")
        self.assertEqual(snapshot["readCount"], 0)
        self.assertEqual(self.projection.messages("synthetic-account-1", "user-a"), [])

    def test_wait_ready_reports_scope_and_epoch(self):
        self.source.seed_pages("user-a", 10)
        self.projection.prepare("synthetic-account-1", "user-a")
        ready = self.projection.wait_ready("synthetic-account-1", "user-a", threading.Event(), lambda: None)
        self.assertEqual(ready["readCount"], 10)
        self.assertEqual(ready["sourceFingerprint"],
                         self.projection.scope_fingerprint(self.source.account, self.source.workdir))
        self.assertTrue(ready["epoch"])


class TrustedSource(FakeSource):
    advisor_binding_generation = 1

    def __init__(self):
        super().__init__()
        self.fresh_calls = 0

    def advisor_identity(self, *, messages=True):
        return self.account, self.workdir

    def verified_identity(self, *, messages=False):
        self.fresh_calls += 1
        return self.identity()


class SharedContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = TrustedSource()
        self.service = AccountDataService(self.source, self.root)
        self.context = self.service.context
        self.addCleanup(self.service.shutdown)

    def observe(self, messages, more=False):
        return self.service.observe_message_window(ACCOUNT, self.source.workdir, USER, messages, more)

    def test_warm_prepares_use_the_loaded_chat_without_source_queries(self):
        messages = [make_message(index) for index in range(1, 7)]
        self.assertTrue(self.observe(messages))
        self.source.history_highwater = Mock(side_effect=AssertionError("warm prepare rescanned highwater"))
        self.source.history_page = Mock(side_effect=AssertionError("warm prepare queried source history"))
        for _ in range(2):
            self.assertEqual(self.service.prepare_context(ACCOUNT, USER), {"context": {"revision": 1}})
        self.assertEqual(self.context.snapshot(ACCOUNT, USER)["readCount"], 6)
        self.source.history_highwater.assert_not_called()
        self.source.history_page.assert_not_called()

    def test_trusted_overlapping_tail_appends_new_message_without_rescanning(self):
        messages = [make_message(index) for index in range(1, 7)]
        self.observe(messages)
        self.source.history_highwater = Mock(side_effect=AssertionError("redundant highwater"))
        self.source.history_page = Mock(side_effect=AssertionError("redundant history"))
        self.assertTrue(self.observe(messages[-2:] + [make_message(7)], more=True))
        snapshot = self.context.prepare(ACCOUNT, USER)
        self.assertEqual((snapshot["state"], snapshot["readCount"]), ("ready", 7))
        self.assertEqual([row["msg_id"] for row in self.context.messages(ACCOUNT, USER)],
                         [item["id"] for item in messages + [make_message(7)]])

    def test_first_partial_window_is_a_seed_and_does_not_scan_all_chats(self):
        self.source.seed_pages(USER, 120)
        self.assertTrue(self.observe(self.source.users[USER][-10:], more=True))
        self.assertEqual(self.source.page_calls, [])
        self.assertEqual(self.context.snapshot(ACCOUNT, USER)["state"], "idle")
        self.context.prepare(ACCOUNT, USER)
        self.assertTrue(wait_until(lambda: self.context.snapshot(ACCOUNT, USER)["state"] == "ready"))
        self.assertEqual(self.context.snapshot(ACCOUNT, USER)["readCount"], 120)

    def test_empty_complete_context_cannot_turn_a_partial_tail_into_complete_history(self):
        self.observe([])
        self.source.seed_pages(USER, 120)
        self.observe(self.source.users[USER][-10:], more=True)
        self.assertTrue(wait_until(lambda: self.context.snapshot(ACCOUNT, USER)["state"] == "ready"))
        self.assertEqual(self.context.snapshot(ACCOUNT, USER)["readCount"], 120)

    def test_complete_window_removes_deleted_older_facts(self):
        messages = [make_message(index) for index in range(1, 7)]
        self.observe(messages)
        self.observe(messages[1:])
        self.assertEqual(self.context.snapshot(ACCOUNT, USER)["readCount"], 5)
        self.assertEqual(self.context.messages(ACCOUNT, USER)[0]["msg_id"], messages[1]["id"])

    def test_changed_reader_binding_requires_new_observation(self):
        messages = [make_message(1)]
        self.source.add_messages(USER, messages)
        self.observe(messages)
        self.source.advisor_binding_generation += 1
        original = self.source.history_highwater
        self.source.history_highwater = Mock(wraps=original)
        self.context.prepare(ACCOUNT, USER)
        self.source.history_highwater.assert_called_once()

    def test_complete_pending_window_after_import_does_not_keep_removed_records(self):
        messages = [make_message(index) for index in range(1, 7)]
        self.observe(messages)
        with self.context.lock:
            rows = self.context._page_rows(USER, messages[1:], 0, trusted_identity=(ACCOUNT, self.source.workdir))
            self.context._apply_window_locked(ACCOUNT, USER, {
                "fingerprint": self.context.scope_fingerprint(ACCOUNT, self.source.workdir), "rows": rows,
                "hasMoreBefore": False, "latest": tuple(messages[-1]["_sort"])
            }, after_import=True)
        self.assertEqual(self.context.snapshot(ACCOUNT, USER)["readCount"], 5)

    def test_import_failure_keeps_error_without_spawning_an_unbounded_retry_loop(self):
        self.source.seed_pages(USER, 120)
        self.observe(self.source.users[USER][-10:], more=True)
        self.source.history_page = Mock(side_effect=OSError("synthetic unavailable source"))
        self.context.prepare(ACCOUNT, USER)
        self.assertTrue(wait_until(lambda: self.context.snapshot(ACCOUNT, USER)["state"] == "error"))
        time.sleep(0.05)
        self.assertEqual(self.source.history_page.call_count, 1)
        self.assertEqual(self.context.snapshot(ACCOUNT, USER)["state"], "error")

    def test_voice_transcript_update_starts_a_new_epoch(self):
        messages = [make_message(1), make_message(2, kind="voice", text="[语音]")]
        messages[1]["media"] = {"kind": "voice", "transcript": "周末一起喝咖啡", "transcriptSource": "wechat"}
        self.observe(messages)
        previous = self.service.stores.account(ACCOUNT).context(USER)
        messages[1]["media"] = {**messages[1]["media"], "transcript": "下周再约"}
        self.observe(messages)
        self.assertNotEqual(self.service.stores.account(ACCOUNT).context(USER)["epoch"], previous["epoch"])
        self.assertEqual(json.loads(self.context.messages(ACCOUNT, USER)[1]["raw"])["media"]["transcript"], "下周再约")

    def test_account_clear_rejects_late_trusted_windows_without_recreating_database(self):
        self.observe([make_message(1)])
        directory = self.service.stores.account(ACCOUNT).directory
        self.assertEqual(self.service.clear_account(ACCOUNT), {"deleted": True})
        self.assertFalse(directory.exists())
        self.assertFalse(self.observe([make_message(2)]))
        self.assertFalse(directory.exists())
        self.assertEqual(self.service.clear_account(ACCOUNT), {"deleted": False})

    def test_failed_clear_resumes_the_account(self):
        self.observe([make_message(1)])
        self.service.pause_for_account_clear(ACCOUNT)
        self.assertFalse(self.observe([make_message(2)]))
        self.service.resume_after_failed_account_clear()
        self.assertTrue(self.observe([make_message(1), make_message(2)]))
        self.assertEqual(self.context.snapshot(ACCOUNT, USER)["readCount"], 2)


class ChatReuseHookTests(unittest.TestCase):
    def backend(self):
        backend = object.__new__(Backend)
        backend.closing = False
        backend._accounts = Mock()
        backend._scoped_identity = Mock(return_value=(ACCOUNT, "C:/synthetic-workdir"))
        backend._assert_scope = Mock()
        backend.source = SimpleNamespace()
        return backend

    def test_chat_hook_receives_decoded_raw_rows_before_private_sort_is_removed(self):
        backend = self.backend()
        window = MessageWindow([make_message(1)], False)
        backend.source.messages = Mock(return_value=window)
        payload = backend.messages(USER, 80)
        observed = backend._accounts.observe_message_window.call_args.args
        self.assertIs(observed[3], window)
        self.assertIn("_sort", observed[3][0])
        self.assertNotIn("_sort", payload["messages"][0])
        self.assertEqual(observed[-1], False)

    def test_projection_failure_cannot_break_the_existing_chat_response(self):
        backend = self.backend()
        backend._accounts.observe_message_window.side_effect = OSError("synthetic projection failure")
        backend.source.messages = Mock(return_value=MessageWindow([make_message(1)], False))
        self.assertEqual(len(backend.messages(USER, 80)["messages"]), 1)

    def test_preloaded_windows_are_observed_without_extra_history_reads(self):
        backend = self.backend()
        batch = MessageWindowBatch({USER: [make_message(1)]}, {USER: True})
        batch.verified_scope = (ACCOUNT, "C:/synthetic-workdir")
        backend.source.message_windows = Mock(return_value=batch)
        backend._scoped_identity.side_effect = AssertionError("verified batch was rediscovered")
        backend.message_windows(ACCOUNT, [USER])
        observed = backend._accounts.observe_message_window.call_args.args
        self.assertEqual(observed[2], USER)
        self.assertEqual(observed[-1], True)
        backend._scoped_identity.assert_not_called()


class MediaMetadataTests(unittest.TestCase):
    def test_metadata_never_copies_arbitrary_bytes_paths_or_non_wechat_transcripts(self):
        metadata = media_metadata({"kind": "voice", "bytes": b"\x89PNG", "path": "private/file", "data": "base64",
                                   "transcript": "untrusted", "transcriptSource": "guessed"})
        self.assertEqual(metadata, {"kind": "voice"})

    def test_known_kinds_and_bounded_numbers_only(self):
        self.assertEqual(media_metadata({"kind": "sticker", "width": 40, "url": "not-persisted"}),
                         {"kind": "sticker", "width": 40})
        self.assertEqual(media_metadata({"kind": "video", "durationMs": 1500, "height": -1}),
                         {"kind": "video", "durationMs": 1500})
        self.assertEqual(media_metadata({"kind": "voice", "transcript": "x" * 9000, "transcriptSource": "wechat"}),
                         {"kind": "voice", "transcript": "x" * 8000, "transcriptSource": "wechat"})
        self.assertIsNone(media_metadata({"kind": "file"}))
        self.assertIsNone(media_metadata("image"))

    def test_projection_keeps_media_metadata_but_never_bytes(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        source = FakeSource()
        message = make_message(1, kind="image", text="[图片]")
        message["media"] = {"kind": "image", "bytes": b"\x89PNG", "width": 10}
        source.add_messages(USER, [message])
        stores = AccountStores(Path(temp.name))
        projection = ContextProjection(stores, source)
        self.addCleanup(stores.close)
        self.addCleanup(projection.close)
        projection.prepare(ACCOUNT, USER)
        _wait_ready(projection, ACCOUNT, USER)
        raw = json.loads(projection.messages(ACCOUNT, USER)[0]["raw"])
        self.assertEqual(raw["media"], {"kind": "image", "width": 10})


class SourceIdentityTests(unittest.TestCase):
    def source(self):
        source = WeChatSource(factory=lambda **_kwargs: None)
        source.live_account = True
        reader = SimpleNamespace(account="synthetic-account", workdir="C:/synthetic-owned-cache", messages_ready=True)
        source.db = reader
        calls = []

        def trusted_db(fresh=False):
            self.assertTrue(fresh)
            calls.append(fresh)
            return source.db

        source._db = trusted_db
        return source, reader, calls

    def test_first_advisor_lookup_uses_the_original_fresh_verification(self):
        source, reader, calls = self.source()
        result = source.advisor_identity()
        self.assertEqual(result, (reader.account, Path(reader.workdir).resolve()))
        self.assertEqual(calls, [True])
        self.assertEqual(source.advisor_identity(), result)
        self.assertEqual(calls, [True])

    def test_verified_chat_identity_refreshes_a_lease_without_holding_source_lock(self):
        source, reader, calls = self.source()
        expected = source.verified_identity(messages=True)
        held, release = threading.Event(), threading.Event()

        def hold_source():
            with source.lock:
                held.set()
                release.wait(2)

        thread = threading.Thread(target=hold_source)
        thread.start()
        try:
            self.assertTrue(held.wait(1))
            source._db = lambda **_kwargs: self.fail("warm Advisor lease called the blocked reader")
            started = time.monotonic()
            self.assertEqual(source.advisor_identity(), expected)
            self.assertLess(time.monotonic() - started, 0.1)
        finally:
            release.set()
            thread.join(2)
        self.assertEqual(calls, [True])

    def test_expired_lease_reverifies_and_observes_account_switch(self):
        source, reader, calls = self.source()
        with patch("wechat_source.time.monotonic", return_value=100.0):
            source.verified_identity(messages=True)
        generation = source.advisor_binding_generation
        reader.account = "synthetic-other-account"
        with patch("wechat_source.time.monotonic", return_value=102.01):
            self.assertEqual(source.advisor_identity()[0], reader.account)
        self.assertEqual(calls, [True, True])
        self.assertGreater(source.advisor_binding_generation, generation)

    def test_reader_replacement_and_explicit_invalidation_cannot_reuse_old_binding(self):
        source, reader, calls = self.source()
        source.verified_identity(messages=True)
        source.db = SimpleNamespace(account="replacement", workdir="C:/synthetic-new-cache", messages_ready=True)
        self.assertEqual(source.advisor_identity()[0], "replacement")
        generation = source.advisor_binding_generation
        source.invalidate_advisor_identity()
        self.assertGreater(source.advisor_binding_generation, generation)
        self.assertEqual(source.advisor_identity()[0], "replacement")
        self.assertEqual(len(calls), 3)

    def test_fresh_failure_and_readiness_change_invalidate_the_warm_lease(self):
        source, reader, calls = self.source()
        source.verified_identity(messages=True)
        reader.messages_ready = False
        with self.assertRaises(MessagesUnavailableError):
            source.advisor_identity()
        self.assertIsNone(source._advisor_binding)
        reader.messages_ready = True
        source.verified_identity(messages=True)
        source._db = lambda **_kwargs: (_ for _ in ()).throw(AccountUnavailableError())
        with self.assertRaises(AccountUnavailableError):
            source.verified_identity(messages=True)
        self.assertIsNone(source._advisor_binding)

    def test_plugin_scope_reuses_a_recent_verification_but_never_a_changed_account(self):
        from core_services import CoreServices
        source, reader, calls = self.source()
        backend = SimpleNamespace(source=source, closing=False,
                                  _scoped_identity=lambda: self.fail("plugin scope verified through the backend"))
        core = CoreServices.__new__(CoreServices)
        core.backend, core.source = backend, source
        now = [100.0]
        with patch("wechat_source.time.monotonic", side_effect=lambda: now[0]):
            for _ in range(20):  # e.g. one draft save per keystroke
                self.assertEqual(core.scope(), {"account": reader.account, "ready": True})
            self.assertEqual(calls, [True])
            reader.account = "synthetic-other-account"
            now[0] = 102.01
            self.assertEqual(core.scope()["account"], "synthetic-other-account", "an expired lease re-verifies")
            now[0] = 102.5
            source.db = SimpleNamespace(account="replacement", workdir="C:/synthetic-new-cache", messages_ready=True)
            self.assertEqual(core.scope()["account"], "replacement", "a replaced reader is never reused")
            source.invalidate_advisor_identity(reset_ownership=False)
            reader.account = "synthetic-third-account"
            source.db = reader
            self.assertEqual(core.scope()["account"], "synthetic-third-account")
        self.assertEqual(len(calls), 4)
        backend.closing = True
        backend._scoped_identity = Mock(side_effect=AccountUnavailableError())
        self.assertEqual(core.scope(), {"account": None, "ready": False})

    def test_default_verified_identity_is_still_fresh_for_every_call(self):
        source, reader, calls = self.source()
        source.verified_identity()
        source.verified_identity(messages=True)
        source.identity()
        self.assertEqual(calls, [True, True, True])

    def test_release_forget_and_close_block_stale_advisor_bindings(self):
        for operation in ("release", "forget", "close"):
            with self.subTest(operation=operation):
                source, reader, calls = self.source()
                source.verified_identity(messages=True)
                generation = source.advisor_binding_generation
                if operation == "release":
                    source._release_db()
                elif operation == "forget":
                    source.forget_account(reader.account)
                else:
                    source.close()
                self.assertIsNone(source._advisor_binding)
                self.assertGreater(source.advisor_binding_generation, generation)
                if operation == "close":
                    with self.assertRaises(AccountUnavailableError):
                        source.advisor_identity()

    def test_parallel_expired_advisor_requests_share_one_fresh_check(self):
        source, reader, calls = self.source()
        source.verified_identity(messages=True)
        source.invalidate_advisor_identity()
        entered, release = threading.Event(), threading.Event()
        original = source._db

        def slow(fresh=False):
            entered.set()
            release.wait(2)
            return original(fresh=fresh)

        source._db = slow
        results = []
        threads = [threading.Thread(target=lambda: results.append(source.advisor_identity())) for _ in range(5)]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(entered.wait(1))
        finally:
            release.set()
            for thread in threads:
                thread.join(2)
        self.assertEqual(len(results), 5)
        self.assertEqual(len(calls), 2)

    def test_source_lock_holder_and_expired_lease_waiter_cannot_deadlock(self):
        # Context import re-checks identity while holding the source lock; the
        # worker event reader concurrently takes the single-flight lock first.
        source, reader, _calls = self.source()
        source.verified_identity(messages=True)
        source.invalidate_advisor_identity()
        holding = threading.Event()
        results = {}

        def importer():
            with source.lock:
                holding.set()
                time.sleep(0.2)  # the reader now owns the single-flight lock and waits for ours
                results["import"] = source.advisor_identity()

        def event_reader():
            holding.wait(1)
            results["reader"] = source.advisor_identity()

        threads = [threading.Thread(target=importer, daemon=True),
                   threading.Thread(target=event_reader, daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertFalse(any(thread.is_alive() for thread in threads), "identity checks deadlocked")
        expected = (reader.account, Path(reader.workdir).resolve())
        self.assertEqual(results, {"import": expected, "reader": expected})

    def test_ordinary_reader_opt_in_never_leaks_into_verified_identity(self):
        with patch("live_source.active_account_snapshot") as locator:
            source = WeChatSource()
            source._active_selection(fresh=False)
            locator.assert_called_once_with(fresh=False)
            locator.reset_mock()
            source._active_selection()
            locator.assert_called_once_with(fresh=True)
        reader = SimpleNamespace(account="synthetic-account", workdir="C:/synthetic-cache", messages_ready=True)
        source.db = reader
        with patch.object(source, "_db", return_value=reader) as read:
            source._ordinary_db()
            read.assert_called_once_with(fresh=True, reuse_ownership=True)
            read.reset_mock()
            source.verified_identity(messages=True)
            read.assert_called_once_with(fresh=True)

    def test_only_the_first_check_of_a_source_hold_may_share_a_newer_ownership_query(self):
        with patch("live_source.active_account_snapshot") as locator:
            source = WeChatSource()
            source._active_selection(fresh=True, since=5.0)
            locator.assert_called_once_with(fresh=True, since=5.0)
        seen = []
        def locate(*, fresh=True, since=None):
            seen.append(since)
            raise OSError("synthetic: no live account")
        source._active_selection = locate
        with source.lock:
            for _ in range(2):
                with self.assertRaises(AccountUnavailableError):
                    source._db(fresh=True)
        with self.assertRaises(AccountUnavailableError):
            source._db(fresh=True)
        # First check of each hold: the hold's request time; a later check follows reads.
        self.assertIsInstance(seen[0], float)
        self.assertIsNone(seen[1])
        self.assertIsInstance(seen[2], float)
        self.assertGreaterEqual(seen[2], seen[0])

    def test_shared_ownership_answer_starts_the_lease_when_it_finished(self):
        source, reader, calls = self.source()
        def shared_db(fresh=False):
            calls.append(fresh)
            source._proved_at = 7.0
            return source.db
        source._db = shared_db
        with patch("wechat_source.time.monotonic", return_value=8.5):
            source.verified_identity(messages=True)
        with patch("wechat_source.time.monotonic", return_value=8.9):
            source.advisor_identity()
        self.assertEqual(calls, [True])
        with patch("wechat_source.time.monotonic", return_value=9.01):
            source.advisor_identity()
        self.assertEqual(calls, [True, True])

    def test_expired_advisor_lease_restarts_from_strong_check_not_ownership_cache(self):
        source, reader, calls = self.source()
        import live_source
        with patch("live_source.file_owners", return_value=((42, 10.0),)):
            live_source._owned_processes(["synthetic-account.db"], fresh=False)
        live_source.reset_ownership_cache()
        with patch("wechat_source.time.monotonic", return_value=10):
            source.verified_identity(messages=True)
        with patch("wechat_source.time.monotonic", return_value=12.01):
            source.advisor_identity()
        self.assertEqual(calls, [True, True])


class HistoryRevisionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.table = "Msg_" + "a" * 32
        self.shards = []
        db_files = []
        for index in (0, 1):
            rel = "message/message_%d.db" % index
            source_path = self.root / "db_storage" / rel
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_bytes(b"synthetic encrypted sentinel")
            destination = self.cache / ("message__message_%d.db" % index)
            with closing(sqlite3.connect(destination)) as connection, connection:
                connection.execute("CREATE TABLE " + self.table + "(local_id INTEGER,sort_seq INTEGER,message_content TEXT)")
                connection.execute("INSERT INTO " + self.table + " VALUES(?,?,?)", (1, 10, "synthetic unread text"))
            self.shards.append(destination)
            db_files.append((rel.replace("/", os.sep), str(source_path), source_path.stat().st_size))
        db = SimpleNamespace(account="synthetic-account", workdir=str(self.cache), account_dir=str(self.root),
                             _db_files=db_files, _msg_conns=lambda _user: [(sqlite3.connect(path), self.table) for path in self.shards])
        self.source = WeChatSource(factory=lambda: db)
        self.source.db = db

    def test_metadata_revision_is_stable_and_changes_for_source_wal_and_snapshot(self):
        before = self.source.history_revision("friend")
        self.assertEqual(before, self.source.history_revision("friend"))
        source_file = Path(self.source.db._db_files[0][1])
        Path(str(source_file) + "-wal").write_bytes(b"synthetic wal marker")
        with_wal = self.source.history_revision("friend")
        self.assertNotEqual(before, with_wal)
        with self.shards[0].open("ab") as stream:
            stream.write(b"synthetic changed snapshot")
        self.assertNotEqual(with_wal, self.source.history_revision("friend"))
        self.assertNotEqual(before, self.source.history_revision("other-friend"))

    def test_prefix_aggregates_ignore_new_tail_but_detect_older_inserts(self):
        ceiling = (10, "message__message_1.db", 1)
        before = self.source.history_prefix_signature("friend", ceiling)
        with closing(sqlite3.connect(self.shards[1])) as connection, connection:
            connection.execute("INSERT INTO " + self.table + " VALUES(?,?,?)", (2, 11, "new synthetic tail"))
        self.assertEqual(before, self.source.history_prefix_signature("friend", ceiling))
        with closing(sqlite3.connect(self.shards[0])) as connection, connection:
            connection.execute("INSERT INTO " + self.table + " VALUES(?,?,?)", (2, 5, "late synthetic old record"))
        self.assertNotEqual(before, self.source.history_prefix_signature("friend", ceiling))

    def test_equal_sort_seq_uses_same_cross_shard_ceiling_as_history_reader(self):
        ceiling = (10, "message__message_0.db", 1)
        before = self.source.history_prefix_signature("friend", ceiling)
        with closing(sqlite3.connect(self.shards[1])) as connection, connection:
            connection.execute("INSERT INTO " + self.table + " VALUES(?,?,?)", (2, 10, "same seq excluded later shard"))
        self.assertEqual(before, self.source.history_prefix_signature("friend", ceiling))
        with closing(sqlite3.connect(self.shards[1])) as connection, connection:
            connection.execute("INSERT INTO " + self.table + " VALUES(?,?,?)", (3, 9, "lower seq included later shard"))
        self.assertNotEqual(before, self.source.history_prefix_signature("friend", ceiling))

    def test_prefix_signature_reads_only_numeric_columns(self):
        seen = []
        def connections(_user):
            result = []
            for path in self.shards:
                connection = sqlite3.connect(path)
                connection.set_trace_callback(seen.append)
                result.append((connection, self.table))
            return result
        self.source.db._msg_conns = connections
        signature = self.source.history_prefix_signature("friend", (10, "message__message_1.db", 1))
        self.assertEqual(len(signature), 64)
        self.assertTrue(any("COUNT(*)" in query for query in seen))
        self.assertFalse(any("message_content" in query for query in seen))


if __name__ == "__main__":
    unittest.main()
