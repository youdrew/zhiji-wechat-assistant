"""Execution-generation and queue races, without a provider or real account."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from core_store import CoreStore
from core_plugin_process import PluginUnavailable
from core_tasks import TaskError, TaskManager


class Supervisor:
    def __init__(self):
        self.messages = []
        self.process = self.new_process("process-1")

    def new_process(self, token):
        return SimpleNamespace(token=token, send=lambda message: self.messages.append(("test.plugin", message)))

    def on(self, *_args): pass
    def on_exit(self, *_args): pass
    def add_ticker(self, *_args): pass
    def ensure(self, _plugin): return self.process
    def running(self, _plugin): return self.process
    def send(self, plugin, message, **_kwargs): self.messages.append((plugin, message))


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = CoreStore(Path(self.temp.name) / "core.sqlite3")
        self.supervisor = Supervisor()
        registry = SimpleNamespace(enabled=lambda _: True, get=lambda _: {
            "version": "1.0.0", "contributes": {"schemes": [{"id": "default"}]}})
        self.tasks = TaskManager(self.store, self.supervisor, registry)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create(self, **extra):
        return self.tasks.create("test.plugin", kind="scheme", account="a", user="u",
                                 scheme="default", title="分析", **extra)

    def end_stop(self, task_id):
        self.tasks._ended("test.plugin", {"execution": self.tasks.owners[task_id][0]})

    def test_cancelled_queue_entry_cannot_start_its_successor_while_original_runs(self):
        first = self.create()["task"]["id"]
        second = self.create(conflict="queue")["task"]["id"]
        third = self.create(conflict="queue")["task"]["id"]
        self.tasks.stop(second)
        self.assertEqual(self.tasks.task(third)["state"], "queued")
        self.assertEqual(self.tasks.task(third)["afterTask"], first)
        self.tasks.stop(first)
        self.end_stop(first)
        self.assertEqual(self.tasks.task(third)["state"], "running")
        self.assertEqual(len(self.tasks.tasks(states=("running",), kind="scheme")), 1)

    def test_stop_other_targets_the_running_task_even_with_an_existing_queue(self):
        first = self.create()["task"]["id"]
        second = self.create(conflict="queue")["task"]["id"]
        third = self.create(conflict="stop-other")["task"]["id"]
        self.assertEqual(self.tasks.task(first)["state"], "stopping")
        self.assertEqual(self.tasks.task(third)["state"], "queued")
        self.end_stop(first)
        self.assertEqual(self.tasks.task(second)["state"], "running")
        self.assertEqual(self.tasks.task(third)["state"], "queued")
        self.assertEqual(len(self.tasks.tasks(states=("running",), kind="scheme")), 1)

    def test_old_end_signal_cannot_finish_a_resumed_execution_being_stopped(self):
        task_id = self.create()["task"]["id"]
        original = self.tasks.owners[task_id][0]
        self.tasks.stop(task_id)
        self.end_stop(task_id)
        self.tasks.continue_task(task_id)
        self.tasks.stop(task_id)
        self.tasks._ended("test.plugin", {"execution": original})
        self.assertEqual(self.tasks.task(task_id)["state"], "stopping")
        self.end_stop(task_id)
        self.assertEqual(self.tasks.task(task_id)["state"], "stopped")

    def test_continue_stop_other_cancels_running_task_without_discarding_queue(self):
        previous = self.create(from_plugin=True)
        old_id = previous["task"]["id"]
        self.tasks.finish(previous["execution"]["token"], "test.plugin", old_id, "partial")
        active = self.create()["task"]["id"]
        second = self.create(conflict="queue")["task"]["id"]
        third = self.create(conflict="queue")["task"]["id"]

        resumed = self.tasks.continue_task(old_id, conflict="stop-other")["task"]

        self.assertEqual(self.tasks.task(active)["state"], "stopping")
        self.assertEqual([self.tasks.task(item)["state"] for item in (second, third)], ["queued", "queued"])
        self.assertEqual(resumed["state"], "queued")
        cancelled = [message["task"] for _plugin, message in self.supervisor.messages
                     if message["type"] == "task.cancel"]
        self.assertEqual(cancelled, [active])
        self.end_stop(active)
        self.assertEqual(self.tasks.task(old_id)["state"], "running")
        self.assertEqual(self.tasks.task(second)["state"], "queued")
        self.assertEqual(len(self.tasks.tasks(states=("running",), kind="scheme")), 1)
        self.tasks.stop(old_id)
        self.end_stop(old_id)
        self.assertEqual(self.tasks.task(second)["state"], "running")
        self.assertEqual(self.tasks.task(third)["state"], "queued")

    def test_late_process_exit_cannot_interrupt_a_replacement_process_task(self):
        first = self.create()["task"]["id"]
        self.tasks._plugin_exit("test.plugin", False, "process-1")
        self.supervisor.process = self.supervisor.new_process("process-2")
        self.tasks.continue_task(first)
        self.tasks._plugin_exit("test.plugin", False, "process-1")
        self.assertEqual(self.tasks.task(first)["state"], "running")

    def test_process_replacement_between_binding_and_delivery_never_receives_old_execution(self):
        original = self.supervisor.process

        def reject(_message):
            raise PluginUnavailable()

        def replace(action, _digest, _binding):
            if action == "add":
                self.supervisor.process = self.supervisor.new_process("process-2")
                original.send = reject

        self.tasks.on_token(replace)
        result = self.create()["task"]
        self.assertEqual(result["state"], "interrupted")
        self.assertEqual(self.supervisor.messages, [])
        self.assertEqual(self.tasks.live, {})
        self.assertEqual(self.tasks.owners, {})

    def test_stop_and_core_submission_have_one_commit_order(self):
        created = self.create(from_plugin=True)
        task_id, token = created["task"]["id"], created["execution"]["token"]
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

        def write():
            with self.tasks.submission(token, "test.plugin"):
                entered.set()
                release.wait(3)

        writer = threading.Thread(target=write)
        writer.start()
        self.assertTrue(entered.wait(1))
        stopper = threading.Thread(target=lambda: (self.tasks.stop(task_id), stopped.set()))
        stopper.start()
        try:
            self.assertFalse(stopped.wait(.1))
        finally:
            release.set()
            writer.join(3)
            stopper.join(3)
        self.assertTrue(stopped.is_set())
        with self.assertRaises(TaskError):
            with self.tasks.submission(token, "test.plugin"):
                self.fail("revoked submission was accepted")


class TaskAttentionTests(unittest.TestCase):
    """Added 2026-10-15: red dots for notify tasks that finished while their panel was not showing the friend."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "core.sqlite3"
        self.store = CoreStore(self.path)
        self.serial = 0

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def task(self, state, finished, *, user="u", notify=True, kind="interactive", plugin="kyf.advisor", account="a"):
        self.serial += 1
        self.store.insert_task({"id": f"task-{self.serial}", "plugin": plugin, "plugin_version": "1", "kind": kind,
            "account": account, "user": user, "scheme": None, "title": "回答", "state": state,
            "params": {"runId": "r", **({"notify": True} if notify else {})}, "finished_at_ms": finished})

    def seen(self, plugin="kyf.advisor", account="a", user="u", task_ids=None):
        if task_ids is None:
            task_ids = [task_id for item in self.store.attention(account)
                        if item["plugin"] == plugin and item["user"] == user for task_id in item["taskIds"]]
        return self.store.attention_seen(plugin, account, user, task_ids)

    def test_only_unseen_finished_notify_tasks_count_and_seeing_clears_them(self):
        self.task("done", 100)
        self.task("failed", 200, user="v")
        self.task("stopped", 300, user="w")
        self.task("running", None, user="x")
        self.task("done", 400, user="y", notify=False)
        self.task("done", 500, user="z", kind="background")
        self.task("done", 600, user="u", account="other")
        self.assertEqual(sorted((item["user"], item["state"]) for item in self.store.attention("a")),
                         [("u", "done"), ("v", "failed")])
        self.seen()
        self.assertEqual([item["user"] for item in self.store.attention("a")], ["v"])
        # A later answer brings the dot back; seeing another plugin's panel does not clear it.
        self.task("done", 10**13)
        self.seen("other.plugin", task_ids=["task-7"])
        self.assertEqual(sorted(item["user"] for item in self.store.attention("a")), ["u", "v"])

    def test_seen_state_survives_a_restart_and_goes_with_the_account(self):
        self.task("done", 100)
        self.seen()
        self.store.close()
        self.store = CoreStore(self.path)
        self.assertEqual(self.store.attention("a"), [])
        self.store.delete_account("a")
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM task_attention_seen").fetchone()[0], 0)

    def test_acknowledging_snapshot_keeps_concurrent_completion_even_at_same_millisecond(self):
        self.task("done", 100)
        observed = self.store.attention("a")[0]["taskIds"]
        self.task("done", 100)
        self.seen(task_ids=observed)
        self.assertEqual(self.store.attention("a")[0]["taskIds"], ["task-2"])
        self.seen(task_ids=observed)
        self.assertEqual(self.store.attention("a")[0]["taskIds"], ["task-2"])

    def test_acknowledgement_cannot_cross_scope_or_acknowledge_running_tasks(self):
        self.task("done", 100)
        self.task("done", 100, account="other")
        self.task("done", 100, user="other")
        self.task("done", 100, plugin="other")
        self.task("running", None)
        self.seen(task_ids=[f"task-{number}" for number in range(1, 6)])
        self.assertEqual(self.store.connection.execute("SELECT task_id FROM task_attention_seen").fetchall(), [("task-1",)])
        self.store.update_task("task-5", state="done", finished_at_ms=100)
        self.assertIn("task-5", [task_id for item in self.store.attention("a") for task_id in item["taskIds"]])

    def test_host_routes_validate_their_fields(self):
        import core_http
        core = SimpleNamespace(store=self.store)
        self.task("done", 100)
        self.assertEqual([item["user"] for item in core_http.get(core, "/api/attention", {"account": "a"})["items"]], ["u"])
        self.assertIn("seenAt", core_http.post(core, "/api/attention/seen", {"plugin": "kyf.advisor", "account": "a", "user": "u", "taskIds": ["task-1"]}))
        self.assertEqual(core_http.get(core, "/api/attention", {"account": "a"})["items"], [])
        for path, request in (("/api/attention/seen", {"plugin": "kyf.advisor", "account": "a"}),
                              ("/api/attention/seen", {"plugin": "", "account": "a", "user": "u"})):
            with self.subTest(request=request), self.assertRaises(core_http.HostError):
                core_http.post(core, path, request)
        for task_ids in (None, [], [""], [1], ["task-1"] * 2001):
            with self.subTest(task_ids=task_ids), self.assertRaises(core_http.HostError):
                core_http.post(core, "/api/attention/seen", {"plugin": "kyf.advisor", "account": "a", "user": "u", "taskIds": task_ids})
        with self.assertRaises(core_http.HostError):
            core_http.get(core, "/api/attention", {})


if __name__ == "__main__":
    unittest.main()
