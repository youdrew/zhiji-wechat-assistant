"""Hard dependency lifecycle with synthetic plugins and real child processes only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_plugin_process import PluginProcess, PluginUnavailable  # noqa: E402
from core_plugins import PluginRegistry  # noqa: E402
from core_services import CoreServices  # noqa: E402
from core_store import CoreStore  # noqa: E402
from core_supervisor import PluginSupervisor  # noqa: E402
from core_tasks import TaskManager  # noqa: E402
from core_http import HostError, post as host_post  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MAIN = '''
import json, os, sys, time
from pathlib import Path

hello = json.loads(sys.stdin.readline())
plugin_id = hello["plugin"]["id"]
folder = Path(hello["plugin"]["directory"])
options = json.loads((folder / "synthetic.json").read_text(encoding="utf-8"))
audit = Path(hello["dataDir"]).parent / "audit"
audit.mkdir(exist_ok=True)
with (audit / (plugin_id + ".starts")).open("a", encoding="utf-8") as stream:
    stream.write(str(os.getpid()) + "\\n")
(audit / (plugin_id + ".started")).write_text(str(time.monotonic_ns()), encoding="utf-8")
if options.get("crashBeforeReady"):
    os._exit(3)
if options.get("gate"):
    while not (folder / "ready-gate").exists():
        time.sleep(.01)
time.sleep(options.get("delay", 0))
(audit / (plugin_id + ".ready")).write_text(str(time.monotonic_ns()), encoding="utf-8")
print(json.dumps({"type": "ready", "protocol": 1}), flush=True)
for line in sys.stdin:
    message = json.loads(line)
    kind = message.get("type")
    if kind == "shutdown":
        break
    if kind == "rpc":
        if message["method"] == "crash":
            os._exit(3)
        print(json.dumps({"type": "rpc.result", "id": message["id"],
            "result": {"id": plugin_id, "pid": os.getpid()}}), flush=True)
    if kind == "event":
        (audit / (plugin_id + ".event")).write_text(message["event"], encoding="utf-8")
        print(json.dumps({"type": "event.ack", "id": message["id"]}), flush=True)
'''


class DependencyProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kyf-plugin-dependencies-")
        self.base = Path(self.temporary.name)
        self.registry = None
        self.supervisor = None
        self.store = None

    def tearDown(self):
        if self.supervisor is not None:
            self.supervisor.close(grace=.5)
        if self.store is not None:
            self.store.close()
        self.temporary.cleanup()

    def plugin(self, plugin_id, dependencies=(), *, api=1, **options):
        folder = self.base / "plugins" / plugin_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "main.py").write_text(PLUGIN_MAIN, encoding="utf-8")
        (folder / "synthetic.json").write_text(json.dumps(options), encoding="utf-8")
        manifest = {"schema": 1, "id": plugin_id, "name": plugin_id, "version": "1.0.0",
                    "core": {"api": [api]}, "runtime": {"type": "python", "entry": "main.py"},
                    "dependencies": list(dependencies), "events": ["friend.opened"]}
        (folder / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        return folder

    def launch(self):
        self.registry = PluginRegistry(self.base)
        self.supervisor = PluginSupervisor(self.registry, root=ROOT, data_dir=self.base / "plugin-data",
                                           core_url="http://127.0.0.1:9")

    def marker(self, plugin_id, suffix):
        return self.base / "plugin-data" / "audit" / (plugin_id + "." + suffix)

    def starts(self, plugin_id):
        path = self.marker(plugin_id, "starts")
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = predicate()
            if result:
                return result
            time.sleep(.02)
        self.fail("synthetic plugin condition was not reached")

    def assert_ready_before_start(self, dependency, dependent):
        ready = int(self.marker(dependency, "ready").read_text(encoding="utf-8"))
        started = int(self.marker(dependent, "started").read_text(encoding="utf-8"))
        self.assertLessEqual(ready, started)

    def test_rpc_starts_transitive_dependencies_before_their_dependents(self):
        self.plugin("synthetic.app", ["synthetic.middle"])
        self.plugin("synthetic.middle", ["synthetic.leaf"])
        self.plugin("synthetic.leaf", delay=.05)
        self.launch()
        self.assertEqual(self.supervisor.rpc("synthetic.app", "echo")["id"], "synthetic.app")
        self.assert_ready_before_start("synthetic.leaf", "synthetic.middle")
        self.assert_ready_before_start("synthetic.middle", "synthetic.app")

    def test_wait_false_still_waits_for_dependencies_to_be_ready(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        leaf = self.plugin("synthetic.leaf", gate=True)
        self.launch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.supervisor.ensure, "synthetic.app", False)
            try:
                self.wait_for(lambda: self.marker("synthetic.leaf", "started").exists())
                self.assertFalse(pending.done())
                self.assertIsNone(self.supervisor.running("synthetic.app"))
                self.assertFalse(self.marker("synthetic.app", "started").exists())
            finally:
                (leaf / "ready-gate").touch()
            process = pending.result(timeout=10)
            process.wait_ready()
        self.assert_ready_before_start("synthetic.leaf", "synthetic.app")

    def test_diamond_dependency_starts_shared_plugin_once(self):
        self.plugin("synthetic.app", ["synthetic.left", "synthetic.right"])
        self.plugin("synthetic.left", ["synthetic.leaf"])
        self.plugin("synthetic.right", ["synthetic.leaf"])
        self.plugin("synthetic.leaf")
        self.launch()
        self.supervisor.ensure("synthetic.app")
        for plugin_id in ("synthetic.app", "synthetic.left", "synthetic.right", "synthetic.leaf"):
            self.assertEqual(len(self.starts(plugin_id)), 1)
        self.assert_ready_before_start("synthetic.leaf", "synthetic.left")
        self.assert_ready_before_start("synthetic.leaf", "synthetic.right")
        self.assert_ready_before_start("synthetic.left", "synthetic.app")
        self.assert_ready_before_start("synthetic.right", "synthetic.app")

    def test_full_graph_is_checked_before_starting_even_a_valid_first_branch(self):
        self.plugin("synthetic.app", ["synthetic.present", "synthetic.middle"])
        self.plugin("synthetic.present")
        self.plugin("synthetic.middle", ["synthetic.missing"])
        self.launch()
        with self.assertRaises(PluginUnavailable) as caught:
            self.supervisor.ensure("synthetic.app")
        self.assertIn("dependency-missing", caught.exception.code)
        self.assertIn("synthetic.missing", caught.exception.message)
        self.assertFalse(self.supervisor.processes)
        self.assertEqual(self.starts("synthetic.present"), [])

    def test_cycle_blocks_every_process(self):
        self.plugin("synthetic.app", ["synthetic.left"])
        self.plugin("synthetic.left", ["synthetic.right"])
        self.plugin("synthetic.right", ["synthetic.left"])
        self.launch()
        with self.assertRaises(PluginUnavailable) as caught:
            self.supervisor.ensure("synthetic.app")
        self.assertIn("dependency-cycle", caught.exception.code)
        self.assertIn("synthetic.left", caught.exception.message)
        self.assertFalse(self.supervisor.processes)

    def test_disabled_dependency_is_not_automatically_enabled(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        self.plugin("synthetic.leaf")
        self.launch()
        self.registry.set_enabled("synthetic.leaf", False)
        with self.assertRaises(PluginUnavailable) as caught:
            self.supervisor.ensure("synthetic.app")
        self.assertIn("dependency-disabled", caught.exception.code)
        self.assertFalse(self.registry.enabled("synthetic.leaf"))
        self.assertFalse(self.supervisor.processes)

    def test_incompatible_dependency_is_reported_before_start(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        self.plugin("synthetic.leaf", api=2)
        self.launch()
        with self.assertRaises(PluginUnavailable) as caught:
            self.supervisor.ensure("synthetic.app")
        self.assertIn("dependency-incompatible", caught.exception.code)
        self.assertIn("synthetic.leaf", caught.exception.message)
        self.assertFalse(self.supervisor.processes)

    def test_dependency_exit_before_ready_never_starts_parent(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        self.plugin("synthetic.leaf", crashBeforeReady=True)
        self.launch()
        with self.assertRaises(PluginUnavailable) as caught:
            self.supervisor.ensure("synthetic.app")
        self.assertIn("synthetic.leaf", caught.exception.message)
        self.assertFalse(self.marker("synthetic.app", "started").exists())
        self.assertIsNone(self.supervisor.running("synthetic.app"))

    def test_concurrent_start_requests_reuse_the_same_shared_dependency_and_parent(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        self.plugin("synthetic.other", ["synthetic.leaf"])
        leaf = self.plugin("synthetic.leaf", gate=True)
        self.launch()
        barrier = threading.Barrier(3)

        def ensure(plugin_id):
            barrier.wait(timeout=5)
            return self.supervisor.ensure(plugin_id)

        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = [pool.submit(ensure, plugin_id)
                       for plugin_id in ("synthetic.app", "synthetic.app", "synthetic.other")]
            try:
                self.wait_for(lambda: self.marker("synthetic.leaf", "started").exists())
                self.assertEqual(len(self.starts("synthetic.leaf")), 1)
            finally:
                (leaf / "ready-gate").touch()
            processes = [future.result(timeout=10) for future in pending]
        self.assertIs(processes[0], processes[1])
        for plugin_id in ("synthetic.app", "synthetic.other", "synthetic.leaf"):
            self.assertEqual(len(self.starts(plugin_id)), 1)

    def test_disabling_dependency_suspends_parent_and_restores_on_demand(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        self.plugin("synthetic.leaf")
        self.launch()
        first = self.supervisor.ensure("synthetic.app")
        leaf = self.supervisor.running("synthetic.leaf")
        self.registry.set_enabled("synthetic.leaf", False)
        self.supervisor.reconcile_dependencies()
        self.wait_for(lambda: not first.alive() and not leaf.alive())
        self.assertTrue(self.registry.enabled("synthetic.app"))
        with self.assertRaises(PluginUnavailable):
            self.supervisor.ensure("synthetic.app")
        self.registry.set_enabled("synthetic.leaf", True)
        resumed = self.supervisor.ensure("synthetic.app")
        self.assertNotEqual(resumed.token, first.token)
        self.assert_ready_before_start("synthetic.leaf", "synthetic.app")

    def test_removing_dependency_suspends_parent_and_rediscovery_can_resume_it(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        folder = self.plugin("synthetic.leaf")
        self.launch()
        first = self.supervisor.ensure("synthetic.app")
        manifest = folder / "plugin.json"
        hidden = folder / "manifest.hidden"
        manifest.rename(hidden)
        self.registry.refresh()
        self.supervisor.reconcile_dependencies()
        self.wait_for(lambda: not first.alive())
        self.assertIn("synthetic.leaf", self.registry.public("synthetic.app")["dependencyError"]["message"])
        hidden.rename(manifest)
        self.registry.refresh()
        resumed = self.supervisor.ensure("synthetic.app")
        self.assertNotEqual(resumed.token, first.token)

    def test_crashed_dependency_interrupts_parent_task_and_can_be_restarted_on_demand(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        self.plugin("synthetic.leaf")
        self.launch()
        self.store = CoreStore(self.base / "core.sqlite3")
        tasks = TaskManager(self.store, self.supervisor, self.registry)
        task = tasks.create("synthetic.app", kind="background", account="synthetic-account", title="synthetic task")["task"]
        self.assertEqual(task["state"], "running")
        first = self.supervisor.running("synthetic.app")
        with self.assertRaises(PluginUnavailable):
            self.supervisor.rpc("synthetic.leaf", "crash")
        self.wait_for(lambda: not first.alive())
        self.wait_for(lambda: tasks.task(task["id"])["state"] == "interrupted")
        status = self.supervisor.status("synthetic.app")
        self.assertEqual(status["state"], "dependency-blocked")
        self.assertIn("synthetic.leaf", status["reason"])
        resumed = self.supervisor.ensure("synthetic.app")
        self.assertNotEqual(resumed.token, first.token)
        self.assertEqual(self.supervisor.status("synthetic.app")["state"], "ready")
        self.assertEqual(tasks.task(task["id"])["state"], "interrupted")

    def test_event_delivery_uses_the_same_dependency_start_order(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        self.plugin("synthetic.leaf")
        self.launch()
        self.supervisor.send("synthetic.app", {"type": "event", "id": "synthetic-event", "event": "friend.opened"})
        self.wait_for(lambda: self.marker("synthetic.app", "event").exists())
        self.assert_ready_before_start("synthetic.leaf", "synthetic.app")

    def test_manifest_dependency_changes_suspend_the_old_parent_generation(self):
        folder = self.plugin("synthetic.app")
        self.plugin("synthetic.leaf")
        self.launch()
        first = self.supervisor.ensure("synthetic.app")
        manifest_path = folder / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["dependencies"] = ["synthetic.leaf"]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.registry.refresh()
        self.supervisor.reconcile_dependencies()
        self.wait_for(lambda: not first.alive())
        second = self.supervisor.ensure("synthetic.app")
        self.assertNotEqual(first.token, second.token)
        self.assert_ready_before_start("synthetic.leaf", "synthetic.app")
        leaf = self.supervisor.running("synthetic.leaf")
        manifest["dependencies"] = []
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.registry.refresh()
        self.supervisor.reconcile_dependencies()
        self.wait_for(lambda: not second.alive())
        third = self.supervisor.ensure("synthetic.app")
        self.assertNotEqual(second.token, third.token)
        self.assertIs(self.supervisor.running("synthetic.leaf"), leaf)

    def test_one_reconciliation_suspends_all_transitive_users_of_an_exited_leaf(self):
        self.plugin("synthetic.app", ["synthetic.middle"])
        self.plugin("synthetic.middle", ["synthetic.leaf"])
        self.plugin("synthetic.leaf")
        self.launch()
        parent = self.supervisor.ensure("synthetic.app")
        middle = self.supervisor.running("synthetic.middle")
        leaf = self.supervisor.running("synthetic.leaf")
        reconcile = self.supervisor.reconcile_dependencies
        # Drive exactly one pass ourselves instead of relying on the monitor's scheduling.
        with mock.patch.object(self.supervisor, "reconcile_dependencies"):
            leaf.kill("synthetic dependency exit")
            self.wait_for(lambda: not leaf.alive())
            reconcile()
            self.assertFalse(parent.alive())
            self.assertFalse(middle.alive())
        self.assertTrue(self.registry.enabled("synthetic.app"))
        self.assertTrue(self.registry.enabled("synthetic.middle"))

    def test_a_repeatedly_crashed_dependency_blocks_parent_with_an_actionable_status(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        self.plugin("synthetic.leaf")
        self.launch()
        for number in range(1, 4):
            leaf = self.supervisor.ensure("synthetic.leaf")
            with self.assertRaises(PluginUnavailable):
                self.supervisor.rpc("synthetic.leaf", "crash")
            self.wait_for(lambda: not leaf.alive())
            self.wait_for(lambda: len(self.supervisor.crashes["synthetic.leaf"]) >= number)
        self.wait_for(lambda: "synthetic.leaf" in self.supervisor.blocked)
        with self.assertRaises(PluginUnavailable) as caught:
            self.supervisor.ensure("synthetic.app")
        self.assertIn("synthetic.leaf", caught.exception.message)
        self.assertEqual(self.starts("synthetic.app"), [])
        status = self.supervisor.status("synthetic.app")
        self.assertEqual(status["state"], "dependency-blocked")
        self.assertIn("synthetic.leaf", status["reason"])
        self.supervisor.reset("synthetic.leaf")
        self.supervisor.ensure("synthetic.app")
        self.assertEqual(self.supervisor.status("synthetic.app")["state"], "ready")

    def test_ensure_cannot_return_a_parent_suspended_while_its_ready_wait_completed(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        self.plugin("synthetic.leaf")
        self.launch()
        observed = threading.Event()
        release = threading.Event()
        original_wait = PluginProcess.wait_ready

        def pause_after_ready(process, *args, **kwargs):
            original_wait(process, *args, **kwargs)
            if process.id == "synthetic.app":
                observed.set()
                if not release.wait(5):
                    raise RuntimeError("synthetic concurrent reconcile did not finish")

        with mock.patch.object(PluginProcess, "wait_ready", pause_after_ready):
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(self.supervisor.ensure, "synthetic.app")
                try:
                    self.assertTrue(observed.wait(5))
                    self.registry.set_enabled("synthetic.leaf", False)
                    self.supervisor.reconcile_dependencies()
                finally:
                    release.set()
                with self.assertRaises(PluginUnavailable):
                    pending.result(timeout=10)

    def test_host_enable_preserves_state_and_returns_the_missing_dependency_chain(self):
        self.plugin("synthetic.app", ["synthetic.middle"])
        self.plugin("synthetic.middle", ["synthetic.missing"])
        self.launch()
        self.registry.set_enabled("synthetic.app", False)
        before = self.registry.state_path.read_bytes()
        reset = mock.Mock()
        core = SimpleNamespace(registry=self.registry, listing=self.registry.listing,
                               events=SimpleNamespace(lock=threading.RLock(), reenabled=mock.Mock()),
                               supervisor=SimpleNamespace(reset=reset))
        core.set_enabled = lambda plugin_id, enabled: CoreServices.set_enabled(core, plugin_id, enabled)
        with self.assertRaises(HostError) as caught:
            host_post(core, "/api/plugins/enable", {"id": "synthetic.app", "enabled": True})
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "dependency-missing"))
        message = caught.exception.message
        self.assertIn("synthetic.app → synthetic.middle → synthetic.missing", message)
        self.assertEqual(self.registry.state_path.read_bytes(), before)
        self.assertFalse(self.registry.enabled("synthetic.app"))
        reset.assert_not_called()

    def test_host_refresh_runs_core_reconciliation_before_returning_the_listing(self):
        self.plugin("synthetic.app", ["synthetic.leaf"])
        leaf = self.plugin("synthetic.leaf")
        self.launch()
        first = self.supervisor.ensure("synthetic.app")
        (leaf / "plugin.json").rename(leaf / "manifest.hidden")
        core = SimpleNamespace(registry=self.registry, supervisor=self.supervisor, listing=self.registry.listing)
        core.refresh_plugins = mock.Mock(side_effect=lambda: CoreServices.refresh_plugins(core))
        result = host_post(core, "/api/plugins/refresh", {})
        core.refresh_plugins.assert_called_once_with()
        self.assertFalse(first.alive())
        parent = next(plugin for plugin in result["plugins"] if plugin["id"] == "synthetic.app")
        self.assertEqual(parent["dependencyError"]["code"], "dependency-missing")


if __name__ == "__main__":
    unittest.main()
