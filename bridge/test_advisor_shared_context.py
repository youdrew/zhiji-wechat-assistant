"""Reuse decoded chat windows without source reads; all records are synthetic."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from advisor_context import ContextProjection
from advisor_service import AdvisorService
from advisor_store import AdvisorStoreRoot
from backend_service import Backend
from backend_contracts import MessageWindow, MessageWindowBatch
from test_advisor_support import FakeSource, FakeRuntime, FakeModelStore, make_message, wait_until

ACCOUNT = "synthetic-account-1"
USER = "user-a"


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
        self.runtime = FakeRuntime()
        self.service = AdvisorService(self.source, FakeModelStore(context_tokens=1000000), self.root,
                                      runtime_factory=lambda: self.runtime)
        self.context = self.service.context
        self.addCleanup(self.service.shutdown)

    def observe(self, messages, more=False):
        return self.service.observe_message_window(ACCOUNT, self.source.workdir, USER, messages, more)

    def finish(self, result):
        run = self.service._runs[result["run"]["id"]]
        self.assertTrue(wait_until(lambda: not run.worker.is_alive()))
        self.assertEqual(run.state, "done", run.error)
        return run

    def test_two_warm_sends_use_complete_loaded_chat_without_source_queries(self):
        messages = [make_message(index) for index in range(1, 7)]
        self.assertTrue(self.observe(messages))
        self.source.history_highwater = Mock(side_effect=AssertionError("warm send rescanned highwater"))
        self.source.history_page = Mock(side_effect=AssertionError("warm send queried source history"))
        first = self.service.start(ACCOUNT, USER, "builtin:advisor", None, "问题一", "req-warm-first")
        self.finish(first)
        second = self.service.start(ACCOUNT, USER, "builtin:advisor", first["threadId"], "问题二", "req-warm-second")
        self.finish(second)
        self.assertEqual(len(self.runtime.respond_calls), 2)
        self.assertEqual(self.context.snapshot(ACCOUNT, USER)["readCount"], 6)
        self.source.history_highwater.assert_not_called()
        self.source.history_page.assert_not_called()
        self.assertEqual(self.source.fresh_calls, 2, "only final commit forces full source validation")

    def test_trusted_overlapping_tail_appends_new_message_without_rescanning(self):
        messages = [make_message(index) for index in range(1, 7)]
        self.observe(messages)
        self.source.history_highwater = Mock(side_effect=AssertionError("redundant highwater"))
        self.source.history_page = Mock(side_effect=AssertionError("redundant history"))
        self.assertTrue(self.observe(messages[-2:] + [make_message(7)], more=True))
        snapshot = self.context.prepare(ACCOUNT, USER)
        self.assertEqual((snapshot["state"], snapshot["readCount"]), ("ready", 7))
        self.assertEqual([row["msg_id"] for row in self.context.messages(ACCOUNT, USER)], [item["id"] for item in messages + [make_message(7)]])

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

    def test_account_clear_rejects_late_trusted_windows_without_recreating_database(self):
        self.observe([make_message(1)])
        self.service.clear_account(ACCOUNT)
        self.assertFalse(self.observe([make_message(2)]))
        self.assertFalse(any((self.root / ".local" / "advisor-data").glob("*/state.sqlite3")))


class ChatReuseHookTests(unittest.TestCase):
    def backend(self):
        backend = object.__new__(Backend)
        backend.closing = False
        backend._advisor = Mock()
        backend._scoped_identity = Mock(return_value=(ACCOUNT, "C:/synthetic-workdir", None))
        backend._assert_scope = Mock()
        backend.source = SimpleNamespace()
        return backend

    def test_chat_hook_receives_decoded_raw_rows_before_private_sort_is_removed(self):
        backend = self.backend()
        window = MessageWindow([make_message(1)], False)
        backend.source.messages = Mock(return_value=window)
        payload = backend.messages(USER, 80)
        observed = backend._advisor.observe_message_window.call_args.args
        self.assertIs(observed[3], window)
        self.assertIn("_sort", observed[3][0])
        self.assertNotIn("_sort", payload["messages"][0])
        self.assertEqual(observed[-1], False)

    def test_optional_advisor_failure_cannot_break_the_existing_chat_response(self):
        backend = self.backend()
        backend._advisor.observe_message_window.side_effect = OSError("synthetic assistant failure")
        backend.source.messages = Mock(return_value=MessageWindow([make_message(1)], False))
        self.assertEqual(len(backend.messages(USER, 80)["messages"]), 1)

    def test_preloaded_windows_are_observed_without_extra_history_reads(self):
        backend = self.backend()
        batch = MessageWindowBatch({USER: [make_message(1)]}, {USER: True})
        batch.verified_scope = (ACCOUNT, "C:/synthetic-workdir")
        backend.source.message_windows = Mock(return_value=batch)
        backend._scoped_identity.side_effect = AssertionError("verified batch was rediscovered")
        backend.message_windows(ACCOUNT, [USER])
        observed = backend._advisor.observe_message_window.call_args.args
        self.assertEqual(observed[2], USER)
        self.assertEqual(observed[-1], True)
        backend._scoped_identity.assert_not_called()


if __name__ == "__main__":
    unittest.main()
