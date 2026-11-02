"""Mirror only scoped Core projection pages into the plugin's owned account database."""
import tempfile
import threading
import unittest
from pathlib import Path

from contracts import AdvisorError
from support import FakeSource, TestStores, make_message, wait_until as wait_for

A, U = "synthetic-account-1", "synthetic-friend"

class AccountMirrorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.stores = TestStores(self.root)
        self.addCleanup(self.stores.close)
        self.core = self.stores.plugin.core
        self.core.seed_pages(U, 600)
        self.database = self.stores.account(A)
        self.core.history_prepare(A, U)

    def test_page_mirror_preserves_order_and_incrementally_appends(self):
        first = self.database.sync(U)
        rows = self.database.context_messages(U, None)
        self.assertEqual(first["read_count"], 600)
        self.assertEqual([row["seq"] for row in rows], list(range(1, 601)))
        self.assertEqual([row["msg_id"] for row in rows], [row["id"] for row in self.core.users[U]])
        self.core.seed_pages(U, 5, start_seq=601)
        self.core.history_prepare(A, U)
        second = self.database.sync(U)
        self.assertEqual(second["read_count"], 605)
        self.assertEqual(second["epoch"], first["epoch"])
        self.assertEqual(self.database.context_messages(U, None)[:600], rows)

    def test_first_copy_keeps_summaries_migrated_from_core(self):
        self.database.save_summary(U, "range", "v1", "model", 1, "migrated synthetic summary")
        self.assertEqual(self.database.sync(U)["read_count"], 600)
        self.assertEqual(self.database.summary(U, "range", "v1", "model", 1), "migrated synthetic summary")

    def test_changed_source_replaces_mirror_and_invalidates_old_summaries(self):
        first = self.database.sync(U)
        self.database.save_summary(U, "range", "v1", "model", 1, "old synthetic summary")
        self.core.workdir = "C:/synthetic-replacement"
        self.core.users[U] = [make_message(1, text="new source only")]
        self.core.history_prepare(A, U)
        second = self.database.sync(U)
        self.assertNotEqual(second["source_id"], first["source_id"])
        self.assertNotEqual(second["epoch"], first["epoch"])
        self.assertEqual([row["text"] for row in self.database.context_messages(U, None)], ["new source only"])
        self.assertIsNone(self.database.summary(U, "range", "v1", "model", 1))

    def test_source_change_mid_page_retries_without_combining_epochs(self):
        read = self.core.history_messages
        changed = False
        def switch(account, user, **kwargs):
            nonlocal changed
            page = read(account, user, **kwargs)
            if not changed:
                changed = True
                self.core.users[U] = [make_message(1, text="replacement after read")]
                self.core.history_prepare(A, U)
            return page
        self.core.history_messages = switch
        state = self.database.sync(U)
        self.assertEqual(state["epoch"], self.core.history_status(A, U)["epoch"])
        self.assertEqual(state["read_count"], 1)
        self.assertEqual([row["text"] for row in self.database.context_messages(U, None)], ["replacement after read"])

    def test_concurrent_first_syncs_never_reset_rows_another_run_already_synced(self):
        calls, release, deleted, resume = [0], threading.Event(), threading.Event(), threading.Event()
        def second_run_check():
            calls[0] += 1
            if calls[0] == 3:  # The reset decision is taken; the delete comes next.
                release.wait(5)
            elif calls[0] == 4:
                deleted.set()
                resume.wait(5)
        results = {}
        second = threading.Thread(target=lambda: results.setdefault("second", self.database.sync(U, check=second_run_check)))
        second.start()
        self.assertTrue(wait_for(lambda: calls[0] >= 3))
        first = threading.Thread(target=lambda: results.setdefault("first", self.database.sync(U)))
        first.start()
        first.join(0.5)
        release.set()
        self.assertTrue(deleted.wait(5))
        if "first" in results:
            self.assertEqual(len(self.database.context_messages(U, results["first"]["read_count"])), 600)
        resume.set()
        first.join(5)
        second.join(5)
        self.assertEqual(results["first"]["read_count"], 600)
        self.assertEqual(len(self.database.context_messages(U, None)), 600)

    def test_stopped_run_waiting_for_another_runs_copy_ends_without_waiting(self):
        # Core kills the plugin when a stopped task does not end within its grace period.
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        read = self.core.history_messages
        def held(account, user, **kwargs):
            entered.set()
            release.wait(10)
            return read(account, user, **kwargs)
        self.core.history_messages = held
        first = threading.Thread(target=self.database.sync, args=(U,))
        first.start()
        self.assertTrue(entered.wait(5))
        def check():
            if stopped.is_set():
                raise AdvisorError("stopping", "synthetic stop")
        errors = []
        def second():
            try:
                self.database.sync(U, check=check)
            except AdvisorError as exc:
                errors.append(exc.code)
        waiter = threading.Thread(target=second)
        waiter.start()
        waiter.join(0.3)
        stopped.set()
        waiter.join(2)
        self.assertFalse(waiter.is_alive())
        self.assertEqual(errors, ["stopping"])
        release.set()
        first.join(5)
        self.assertEqual(len(self.database.context_messages(U, None)), 600)

    def test_cancel_during_core_read_rejects_page_before_writing(self):
        cancelled = threading.Event()
        read = self.core.history_messages
        def cancel_after_read(account, user, **kwargs):
            page = read(account, user, **kwargs)
            cancelled.set()
            return page
        def check():
            if cancelled.is_set():
                raise AdvisorError("stopping", "synthetic stop")
        self.core.history_messages = cancel_after_read
        with self.assertRaises(AdvisorError):
            self.database.sync(U, check=check)
        self.assertEqual(self.database.context_messages(U, None), [])
        self.assertIsNone(self.database.context(U))


if __name__ == "__main__":
    unittest.main()
