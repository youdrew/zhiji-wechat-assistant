"""Bounded parallel scheduling with controlled real threads and synthetic stores."""
import sys
import tempfile
import threading
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugin-sdk" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from insights import Insights
from kyf_plugin import CoreError


class ControlledWorker:
    def __init__(self, service):
        self.service, self.jobs = service, []

    def start(self, account, user, ids, spec):
        release, started, cancelled = threading.Event(), threading.Event(), threading.Event()
        job = SimpleNamespace(account=account, user=user, ids=ids, spec=spec, release=release,
                              cancelled=cancelled, cancel=cancelled.set)
        def work():
            started.set()
            release.wait(5)
            self.service.store(account).finish(user, ids, spec["generation"], "stale" if cancelled.is_set() else "error")
            self.service.wake.set()
        job.thread = threading.Thread(target=work, daemon=True)
        job.thread.start()
        if not started.wait(1):
            raise AssertionError("synthetic worker did not start")
        self.jobs.append(job)
        return job

    @staticmethod
    def finish(job):
        job.release.set()
        job.thread.join(1)
        if job.thread.is_alive():
            raise AssertionError("synthetic worker did not stop")


class ParallelDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.values = {"concurrency": 2}
        self.plugin_values = {"replyStyle": "natural"}
        self.account = "synthetic-account"
        self.refreshes = []
        self.gateway_error = False
        self.source_times = {}
        service = self.service = Insights.__new__(Insights)
        service.plugin = SimpleNamespace(data_dir=Path(self.temp.name), models=SimpleNamespace(services=self.services),
            core=SimpleNamespace(scope=lambda: {"account": self.account}, get=self.get))
        service.models = service.plugin.models
        service.lock = threading.RLock()
        service.stores, service.entries, service.subscriptions = {}, {}, {}
        service.queue, service.active = OrderedDict(), {}
        service.wake = threading.Event()
        service.closed = False
        service.dispatch_limit = None
        service.clock = lambda: 200.0
        service.worker = self.worker = ControlledWorker(service)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.service.close()
        for job in self.worker.jobs:
            self.worker.finish(job)
        for store in self.service.stores.values():
            store.close()
        self.temp.cleanup()

    def get(self, path, **_query):
        if path == "settings":
            return {"values": dict(self.plugin_values), "revision": 0}
        if path == "models/settings":
            return {"values": dict(self.values), "revision": 0}
        raise AssertionError(path)

    def services(self, refresh=False):
        self.refreshes.append(refresh)
        if self.gateway_error:
            raise CoreError(503, "unavailable", "synthetic gateway unavailable")
        return [{"id": "synthetic-model", "concurrency": self.values["concurrency"]}]

    def enqueue(self, user, *ids, force=False, source_messages=None, automatic=True):
        store = self.service.store(self.account)
        setting = store.settings(user)
        if not setting["unified_enabled"]:
            setting = store.set(user, True)
        if source_messages is None:
            for message_id in ids:
                self.source_times.setdefault((user, message_id), 100000 - len(self.source_times))
            source_messages = [{"id": message_id, "time": self.source_times[(user, message_id)]} for message_id in ids]
        with self.service.lock:
            self.service.enqueue(self.account, user, list(ids), setting, automatic=automatic, unified=True, force=force,
                                 source_messages=source_messages)

    def test_multiple_messages_run_concurrently_and_fill_freed_slots(self):
        self.enqueue("friend", "one", "two", "three", "four")
        self.service._dispatch()
        self.assertEqual([job.ids for job in self.worker.jobs], [["one"], ["two"]])
        self.assertTrue(all(job.thread.is_alive() for job in self.worker.jobs))
        self.assertTrue(all(job.spec["parts"] == ("analysis", "suggestions") for job in self.worker.jobs))
        self.service._dispatch()
        self.assertEqual(len(self.worker.jobs), 2)
        self.worker.finish(self.worker.jobs[0])
        self.service._dispatch()
        self.assertEqual(self.worker.jobs[-1].ids, ["three"])
        self.assertEqual(len(self.service.active), 2)
        self.assertTrue(self.refreshes and all(self.refreshes))

    def test_limit_increase_fills_immediately_and_lowering_does_not_cancel(self):
        self.values["concurrency"] = 1
        self.enqueue("friend", "one", "two", "three", "four")
        self.service._dispatch()
        self.values["concurrency"] = 3
        self.service._dispatch()
        self.assertEqual(len(self.worker.jobs), 3)
        self.values["concurrency"] = 1
        self.service._dispatch()
        self.assertFalse(any(job.cancelled.is_set() for job in self.worker.jobs))
        self.worker.finish(self.worker.jobs[0])
        self.worker.finish(self.worker.jobs[1])
        self.service._dispatch()
        self.assertEqual(len(self.worker.jobs), 3)
        self.worker.finish(self.worker.jobs[2])
        self.service._dispatch()
        self.assertEqual(self.worker.jobs[-1].ids, ["four"])
        self.assertTrue(self.refreshes and all(self.refreshes))

    def test_latest_messages_across_friends_take_the_next_slots(self):
        self.enqueue("first", "a", "b", "c", source_messages=[
            {"id": "a", "time": 100}, {"id": "b", "time": 300}, {"id": "c", "time": 200}])
        self.enqueue("second", "d", "e", source_messages=[{"id": "d", "time": 500}, {"id": "e", "time": 400}])
        self.service._dispatch()
        self.assertEqual([(job.user, job.ids) for job in self.worker.jobs], [("second", ["d"]), ("second", ["e"])])

    def test_cancel_stops_all_friend_jobs_and_preserves_other_friend(self):
        self.values["concurrency"] = 3
        self.enqueue("first", "a", "b", "c")
        self.enqueue("second", "d", "e", source_messages=[{"id": "d", "time": 100001}, {"id": "e", "time": 1}])
        self.service._dispatch()
        self.service.cancel(self.account, "first")
        self.assertTrue(all(job.cancelled.is_set() for job in self.worker.jobs if job.user == "first"))
        self.assertFalse(any(job.cancelled.is_set() for job in self.worker.jobs if job.user == "second"))
        self.assertNotIn((self.account, "first"), self.service.queue)
        self.assertEqual(self.service.store(self.account).rows("first", ["c"])[0]["state"], "error")
        self.service._dispatch()
        self.assertEqual(len(self.worker.jobs), 3, "cancelled calls retain slots until their threads exit")

    def test_same_message_new_version_waits_for_older_thread_to_exit(self):
        self.enqueue("friend", "one")
        self.service._dispatch()
        previous = self.worker.jobs[0]
        self.enqueue("friend", "one", force=True)
        current = self.service.store(self.account).rows("friend", ["one"])[0]["generation"]
        self.assertNotEqual(previous.spec["generation"], current)
        self.service._dispatch()
        self.assertEqual(len(self.worker.jobs), 1)
        self.worker.finish(previous)
        self.assertEqual(self.service.store(self.account).rows("friend", ["one"])[0]["generation"], current)
        self.service._dispatch()
        self.assertEqual(len(self.worker.jobs), 2)
        self.assertEqual(self.worker.jobs[1].spec["generation"], current)

    def test_switch_off_suggestions_during_work_keeps_finished_analysis_and_reschedules_only_missing_analysis(self):
        self.enqueue("friend", "one", "two")
        self.service._dispatch()
        store = self.service.store(self.account)
        self.service.subscriptions[(self.account, "friend")] = store.settings("friend")["revision"]
        first, second = self.worker.jobs
        analysis = dict.fromkeys(("summary", "meaning", "uncertainty", "responseDirection"), "synthetic")
        analysis["evidence"] = [{"text": "synthetic"}]
        store.finish("friend", first.ids, first.spec["generation"], "ready", results={"one": analysis}, part="analysis")
        self.service.set(self.account, "friend", False, "suggestions")
        self.assertTrue(first.cancelled.is_set() and second.cancelled.is_set())
        queue = self.service.queue[(self.account, "friend")]
        self.assertEqual(queue["ids"], ["two"])
        self.assertEqual(queue["jobs"]["two"]["parts"], ("analysis",))
        for job in (first, second):
            self.worker.finish(job)
        self.service._dispatch()
        row, = store.rows("friend", ["one"])
        self.assertEqual((row["state"], row["analysis_state"], row["suggestions_state"]), ("ready", "ready", "none"))
        self.assertEqual(self.worker.jobs[-1].spec["parts"], ("analysis",))

    def test_repeated_queueing_does_not_duplicate_running_or_queued_messages(self):
        self.enqueue("friend", "one", "two", "three")
        self.service._dispatch()
        self.enqueue("friend", "one", "two", "three")
        self.assertEqual(len(self.service.active), 2)
        self.assertEqual(self.service.queue[(self.account, "friend")]["ids"], ["three"])

    def test_account_switch_cancels_all_old_work_before_new_dispatch(self):
        self.enqueue("friend", "one", "two", "three")
        self.service._dispatch()
        old_account, self.account = self.account, "different-account"
        self.service._dispatch()
        self.assertTrue(all(job.cancelled.is_set() for job in self.worker.jobs))
        self.assertFalse(self.service.queue)
        self.assertEqual(self.service.store(old_account).rows("friend", ["three"])[0]["state"], "error")

    def test_changed_setting_revision_rejects_queued_generation(self):
        self.enqueue("friend", "one")
        self.service.store(self.account).set("friend", False)
        self.service._dispatch()
        self.assertFalse(self.worker.jobs)
        self.assertEqual(self.service.store(self.account).rows("friend", ["one"])[0]["state"], "error")

    def test_gateway_refresh_failure_preserves_active_and_retries_before_fill(self):
        self.enqueue("friend", "one", "two", "three")
        self.service._dispatch()
        self.values["concurrency"] = 3
        self.gateway_error = True
        with self.assertRaises(CoreError):
            self.service._dispatch()
        self.assertEqual(len(self.worker.jobs), 2)
        self.assertFalse(any(job.cancelled.is_set() for job in self.worker.jobs))
        self.gateway_error = False
        self.service._dispatch()
        self.assertEqual(len(self.worker.jobs), 3)
        self.assertTrue(self.refreshes and all(self.refreshes))

    def test_invalid_capacity_falls_back_to_default_and_keeps_dispatching(self):
        self.enqueue("friend", "one", "two", "three", "four", "five")
        for limit in (0, 9, True, 1.5, "2"):
            self.values["concurrency"] = limit
            with self.subTest(limit=limit):
                self.service._dispatch()
                # 非法并发不再拒绝工作：按默认 4 继续派发。
        self.assertEqual(len(self.worker.jobs), 4)

    def test_legacy_plugin_capacity_cannot_override_core_model_capacity(self):
        self.plugin_values["concurrency"] = 8
        self.values["concurrency"] = 1
        self.enqueue("friend", "one", "two", "three")
        self.service._dispatch()
        self.assertEqual(len(self.worker.jobs), 1)
        self.plugin_values["concurrency"] = 0
        self.values["concurrency"] = 3
        self.service._dispatch()
        self.assertEqual(len(self.worker.jobs), 3)

    def event(self, user, *messages):
        store = self.service.store(self.account)
        if not store.settings(user)["unified_enabled"]:
            store.set(user, True)
        self.service.on_message({"account": self.account, "user": user, "liveSince": 0, "data": {"messages": [
            {"senderResolved": True, "side": "other", "senderId": user, "kind": "text", **message}
            for message in messages]}})

    def round_ready(self, user, message):
        """A round whose quiet period has passed: one job under its last message."""
        self.enqueue(user, message["id"], source_messages=[message])

    def test_new_message_supersedes_the_friends_unfinished_automatic_rounds(self):
        self.enqueue("friend", "one", "two")
        self.enqueue("friend", "manual", automatic=False)
        self.enqueue("other", "elsewhere")
        self.service._dispatch()
        running = list(self.worker.jobs)
        self.assertEqual([job.ids for job in running], [["one"], ["two"]])
        self.event("friend", {"id": "newest", "time": 150000})
        self.assertTrue(all(job.cancelled.is_set() for job in running), "the friend is still typing")
        self.assertEqual(self.service.queue[(self.account, "friend")]["ids"], ["manual"])
        self.assertEqual(self.service.queue[(self.account, "other")]["ids"], ["elsewhere"])
        self.assertEqual(self.service.entries[(self.account, "friend")]["due"], 220.0)
        store = self.service.store(self.account)
        self.assertEqual({row["message_id"]: row["state"] for row in store.rows("friend", ["one", "two"])},
                         {"one": "error", "two": "error"})

    def test_same_time_cross_friend_new_arrival_wins_but_duplicate_does_not_gain_priority(self):
        self.values["concurrency"] = 1
        self.round_ready("first", {"id": "a", "time": 100000, "sortKey": [99, "s", 99]})
        original = self.service.queue[(self.account, "first")]["jobs"]["a"].copy()
        self.round_ready("second", {"id": "b", "time": 100000, "sortKey": [1, "s", 1]})
        self.round_ready("first", {"id": "a", "time": 100000, "sortKey": [99, "s", 99]})
        self.assertEqual(self.service.queue[(self.account, "first")]["jobs"]["a"], original)
        self.service._dispatch()
        self.assertEqual((self.worker.jobs[-1].user, self.worker.jobs[-1].ids), ("second", ["b"]))

    def test_manual_history_request_keeps_original_time_and_waits_behind_new_message(self):
        self.values["concurrency"] = 1
        self.round_ready("friend", {"id": "newest", "time": 150000})
        self.enqueue("friend", "historical", source_messages=[{"id": "historical", "time": 50000}], automatic=False)
        self.service._dispatch()
        self.assertEqual(self.worker.jobs[-1].ids, ["newest"])
        self.worker.finish(self.worker.jobs[-1])
        self.service._dispatch()
        self.assertEqual(self.worker.jobs[-1].ids, ["historical"])

    def test_unknown_time_falls_behind_known_time_and_equal_unknowns_are_stable(self):
        self.values["concurrency"] = 1
        self.enqueue("friend", "unknown", "known", "later-unknown", source_messages=[{"id": "known", "time": 1}])
        self.service._dispatch()
        self.assertEqual(self.worker.jobs[-1].ids, ["known"])
        self.worker.finish(self.worker.jobs[-1])
        self.service._dispatch()
        self.assertEqual(self.worker.jobs[-1].ids, ["later-unknown"])


if __name__ == "__main__":
    unittest.main()
