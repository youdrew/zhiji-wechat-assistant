"""Plugin registry, supervisor and task lifecycle with real plugin processes."""
import json
import os
import site
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_plugins import DependencyError, ManifestError, PluginRegistry, parse_manifest  # noqa: E402
from core_store import CoreStore  # noqa: E402
from core_supervisor import PluginSupervisor  # noqa: E402
from core_plugin_process import PluginError, PluginUnavailable, launch_command  # noqa: E402
from core_runtime import python_environment, python_flags  # noqa: E402
from core_tasks import TaskError, TaskManager  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_MAIN = '''
import os, site, sys, time, threading
from kyf_plugin import Plugin, RpcError
plugin = Plugin()
STARTED = []

@plugin.method("echo")
def echo(params, context):
    return {"params": params, "context": context}

@plugin.method("fail")
def fail(params, context):
    raise RpcError("bad-thing", "坏了")

@plugin.method("sleep")
def sleep(params, context):
    time.sleep(params.get("seconds", 1))
    return "late"

@plugin.method("crash")
def crash(params, context):
    os._exit(3)

@plugin.method("started")
def started(params, context):
    return STARTED

@plugin.method("userSite")
def user_site(params, context):
    return site.getusersitepackages() if site.ENABLE_USER_SITE else None

@plugin.task("scheme")
def run(task):
    STARTED.append(task.task["id"])
    mode = task.task["params"].get("mode")
    if mode == "ignore-cancel":
        time.sleep(60)
    while not task.cancelled.wait(0.05):
        if mode == "quick":
            return "done"
    return None

plugin.run()
'''


def write_plugin(base, plugin_id="kyf.test", **overrides):
    folder = base / plugin_id
    (folder / "ui").mkdir(parents=True, exist_ok=True)
    (folder / "main.py").write_text(PLUGIN_MAIN, encoding="utf-8")
    (folder / "ui" / "index.html").write_text("<!doctype html>", encoding="utf-8")
    manifest = {"schema": 1, "id": plugin_id, "name": "测试插件", "version": "1.0.0", "core": {"api": [1]},
                "runtime": {"type": "python", "entry": "main.py"}, "events": ["friend.opened"],
                "contributes": {"schemes": [{"id": "default", "name": "默认方案", "page": "ui/index.html",
                                             "tabs": [{"id": "overview", "title": "概述"}, {"host": "memories"}]}]}}
    manifest.update(overrides)
    (folder / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return folder


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_is_normalized(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = write_plugin(Path(temp))
            manifest = parse_manifest(folder, "bundled")
            self.assertEqual(manifest["id"], "kyf.test")
            self.assertEqual(manifest["dependencies"], [])
            self.assertEqual(manifest["contributes"]["schemes"][0]["tabs"][1], {"host": "memories"})
            self.assertNotIn("incompatible", manifest)

    def test_rejects_paths_outside_the_plugin(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = write_plugin(Path(temp), runtime={"type": "python", "entry": "../main.py"})
            with self.assertRaises(ManifestError):
                parse_manifest(folder, "local")

    def test_external_distribution_in_plugins_starts_disabled_and_preserves_enable_state(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            write_plugin(base / "plugins")
            write_plugin(base / "plugins", plugin_id="kyf.external", distribution="external")
            write_plugin(base / ".local" / "plugins", plugin_id="kyf.external")
            registry = PluginRegistry(base)
            self.assertEqual(registry.get("kyf.external")["source"], "external")
            self.assertFalse(registry.enabled("kyf.external"))
            self.assertTrue(registry.enabled("kyf.test"))
            self.assertTrue(any("重复" in item["error"] for item in registry.problems))
            registry.set_enabled("kyf.external", True)
            registry.refresh()
            self.assertTrue(registry.enabled("kyf.external"))
            restored = PluginRegistry(base)
            self.assertTrue(restored.enabled("kyf.external"))
            restored.set_enabled("kyf.external", False)
            self.assertFalse(PluginRegistry(base).enabled("kyf.external"))

    def test_local_distribution_cannot_claim_bundled_defaults(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            write_plugin(base / ".local" / "plugins", distribution="bundled")
            registry = PluginRegistry(base)
            self.assertEqual(registry.get("kyf.test")["source"], "local")
            self.assertFalse(registry.enabled("kyf.test"))

    def test_invalid_distribution_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            for value in (None, True, "third-party", [], {}):
                with self.subTest(value=value):
                    folder = write_plugin(Path(temp), distribution=value)
                    with self.assertRaisesRegex(ManifestError, "distribution"):
                        parse_manifest(folder, "bundled")

    def test_incompatible_core_api_is_listed_not_started(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            write_plugin(base / "plugins", core={"api": [2]})
            registry = PluginRegistry(base, state_path=base / "state.json")
            listing = registry.listing()
            self.assertIn("incompatible", listing["plugins"][0])
            self.assertFalse(registry.enabled("kyf.test"))
            with self.assertRaises(ManifestError):
                registry.set_enabled("kyf.test", True)

    def test_local_plugins_start_disabled_and_cannot_shadow_bundled(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            write_plugin(base / "plugins")
            write_plugin(base / ".local" / "plugins", plugin_id="kyf.test")
            write_plugin(base / ".local" / "plugins", plugin_id="dev.mine")
            registry = PluginRegistry(base, state_path=base / "state.json")
            self.assertTrue(registry.enabled("kyf.test"))
            self.assertFalse(registry.enabled("dev.mine"))
            self.assertEqual(registry.get("kyf.test")["source"], "bundled")
            self.assertTrue(any("重复" in item["error"] for item in registry.problems))
            registry.set_enabled("dev.mine", True)
            self.assertTrue(PluginRegistry(base, state_path=base / "state.json").enabled("dev.mine"))

    def test_dependencies_are_strict_unique_plugin_ids_with_a_bounded_length(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            dependencies = [f"kyf.dep{index}" for index in range(32)]
            folder = write_plugin(base, dependencies=dependencies)
            self.assertEqual(parse_manifest(folder, "bundled")["dependencies"], dependencies)
            invalid = (None, True, "kyf.other", {}, ["kyf.test"], ["kyf.other", "kyf.other"],
                       [""], ["a"], ["kyf.Bad"], ["kyf..bad"], [" kyf.other"], ["kyf.other\n"],
                       [1], [[]], dependencies + ["kyf.extra"])
            for value in invalid:
                with self.subTest(dependencies=value):
                    folder = write_plugin(base, dependencies=value)
                    with self.assertRaisesRegex(ManifestError, "dependencies"):
                        parse_manifest(folder, "bundled")

    def test_startup_order_handles_a_shared_diamond_and_dependents_stop_consumers_first(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            write_plugin(base / "plugins", "kyf.a", dependencies=["kyf.c", "kyf.b"])
            write_plugin(base / "plugins", "kyf.b", dependencies=["kyf.shared"])
            write_plugin(base / "plugins", "kyf.c", dependencies=["kyf.shared"])
            write_plugin(base / "plugins", "kyf.shared")
            registry = PluginRegistry(base)
            self.assertEqual(registry.startup_order("kyf.a"), ["kyf.shared", "kyf.c", "kyf.b", "kyf.a"])
            self.assertEqual(registry.dependents("kyf.shared"), ["kyf.a", "kyf.b", "kyf.c"])
            self.assertEqual(registry.dependents("kyf.a"), [])
            self.assertEqual(registry.dependents("kyf.missing"), [])
            self.assertEqual(registry.public("kyf.a")["dependencies"], ["kyf.c", "kyf.b"])
            self.assertNotIn("dependencyError", registry.public("kyf.a"))
            self.assertEqual([item["id"] for item in registry.active()], ["kyf.a", "kyf.b", "kyf.c", "kyf.shared"])

    def test_missing_transitive_dependency_rejects_enable_without_writing_state(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            write_plugin(base / "plugins", "kyf.a", distribution="external", dependencies=["kyf.b"])
            write_plugin(base / "plugins", "kyf.b", dependencies=["kyf.missing"])
            write_plugin(base / "plugins", "kyf.other")
            registry = PluginRegistry(base)
            registry.set_enabled("kyf.other", False)
            previous_bytes = registry.state_path.read_bytes()
            previous_state = json.loads(json.dumps(registry.state))
            with self.assertRaises(DependencyError) as caught:
                registry.set_enabled("kyf.a", True)
            self.assertEqual(caught.exception.code, "dependency-missing")
            self.assertIn("kyf.a → kyf.b → kyf.missing", caught.exception.message)
            self.assertEqual(str(caught.exception), caught.exception.message)
            self.assertEqual(registry.state, previous_state)
            self.assertEqual(registry.state_path.read_bytes(), previous_bytes)
            self.assertFalse(registry.enabled("kyf.a"))
            self.assertTrue(registry.enabled("kyf.b"))
            self.assertEqual(registry.public("kyf.a")["dependencyError"], registry.dependency_error("kyf.a"))
            self.assertEqual(registry.dependents("kyf.missing"), ["kyf.a", "kyf.b"])
            self.assertEqual(registry.active(), [])

    def test_disabled_dependency_blocks_start_and_enable_without_changing_consumer_choices(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            write_plugin(base / "plugins", "kyf.a", dependencies=["kyf.b"])
            write_plugin(base / "plugins", "kyf.b", dependencies=["kyf.c"])
            write_plugin(base / "plugins", "kyf.c")
            registry = PluginRegistry(base)
            registry.set_enabled("kyf.c", False)
            previous_bytes = registry.state_path.read_bytes()
            for call in (lambda: registry.startup_order("kyf.a"), lambda: registry.set_enabled("kyf.a", True)):
                with self.assertRaises(DependencyError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "dependency-disabled")
                self.assertIn("kyf.a → kyf.b → kyf.c", caught.exception.message)
            self.assertEqual(registry.state_path.read_bytes(), previous_bytes)
            self.assertTrue(registry.enabled("kyf.a"))
            self.assertTrue(registry.enabled("kyf.b"))
            self.assertFalse(registry.enabled("kyf.c"))
            self.assertEqual(registry.active(), [])
            self.assertEqual(registry.dependents("kyf.c"), ["kyf.a", "kyf.b"])
            registry.set_enabled("kyf.c", True)
            self.assertEqual(registry.startup_order("kyf.a"), ["kyf.c", "kyf.b", "kyf.a"])
            self.assertEqual([item["id"] for item in registry.active()], ["kyf.a", "kyf.b", "kyf.c"])
            self.assertNotIn("dependencyError", registry.public("kyf.a"))

    def test_incompatible_transitive_dependency_reports_compatibility_before_disabled(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            write_plugin(base / "plugins", "kyf.a", dependencies=["kyf.b"])
            write_plugin(base / "plugins", "kyf.b", dependencies=["kyf.c"])
            write_plugin(base / "plugins", "kyf.c", core={"api": [2]})
            registry = PluginRegistry(base)
            with self.assertRaises(DependencyError) as caught:
                registry.set_enabled("kyf.a", True)
            self.assertEqual(caught.exception.code, "dependency-incompatible")
            self.assertIn("kyf.a → kyf.b → kyf.c", caught.exception.message)
            self.assertIn("Core", caught.exception.message)
            self.assertFalse(registry.state_path.exists())
            self.assertEqual(registry.state, {"enabled": {}})
            self.assertTrue(registry.enabled("kyf.a"))
            self.assertEqual(registry.active(), [])

    def test_cycle_is_diagnosed_without_recursing_forever_or_writing_enable_state(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            write_plugin(base / "plugins", "kyf.a", dependencies=["kyf.b"])
            write_plugin(base / "plugins", "kyf.b", dependencies=["kyf.c"])
            write_plugin(base / "plugins", "kyf.c", dependencies=["kyf.a"])
            registry = PluginRegistry(base)
            with self.assertRaises(DependencyError) as caught:
                registry.set_enabled("kyf.a", True)
            self.assertEqual(caught.exception.code, "dependency-cycle")
            self.assertIn("kyf.a → kyf.b → kyf.c → kyf.a", caught.exception.message)
            self.assertEqual(registry.dependency_error("kyf.a")["code"], "dependency-cycle")
            self.assertFalse(registry.state_path.exists())
            self.assertEqual(registry.state, {"enabled": {}})
            self.assertEqual(registry.active(), [])
            self.assertEqual(registry.dependents("kyf.a"), ["kyf.b", "kyf.c"])
            # Disabling never requires a working dependency graph.
            registry.set_enabled("kyf.a", False)
            self.assertFalse(registry.enabled("kyf.a"))

    def test_enable_validation_ignores_only_the_root_toggle_and_legacy_plugins_remain_independent(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            write_plugin(base / "plugins", "kyf.external", distribution="external")
            write_plugin(base / "plugins", "kyf.legacy")
            registry = PluginRegistry(base)
            self.assertEqual(registry.public("kyf.legacy")["dependencies"], [])
            self.assertEqual(registry.startup_order("kyf.legacy"), ["kyf.legacy"])
            self.assertIsNone(registry.dependency_error("kyf.external"))
            self.assertEqual(registry.startup_order("kyf.external", for_enable=True), ["kyf.external"])
            for plugin, code in (("kyf.external", "plugin-disabled"), ("kyf.missing", "plugin-missing")):
                with self.subTest(plugin=plugin), self.assertRaises(DependencyError) as caught:
                    registry.startup_order(plugin)
                self.assertEqual(caught.exception.code, code)
                self.assertIn(plugin, caught.exception.message)
            self.assertFalse(registry.state_path.exists())
            registry.set_enabled("kyf.external", True)
            self.assertEqual(registry.startup_order("kyf.external"), ["kyf.external"])


class PluginEnvironmentTests(unittest.TestCase):
    def test_bundled_runtime_never_receives_a_user_site(self):
        with tempfile.TemporaryDirectory() as temporary:
            (Path(temporary) / "python314._pth").write_text("Lib\\site-packages\nimport site\n", encoding="utf-8")
            self.assertEqual(python_environment(Path(temporary) / "python.exe"), {})

    def test_bundled_runtime_starts_plugins_without_the_user_site(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ):
            os.environ.pop("KYF_PYTHON", None)
            root = Path(temporary)
            python = root / "runtime" / "python" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"fixture")
            manifest = {"directory": str(root / "plugins" / "kyf.test"),
                        "runtime": {"type": "python", "entry": "main.py"}}
            tail = [str(root / "plugin-sdk" / "python" / "launch.py"), str(root / "plugins" / "kyf.test" / "main.py")]
            self.assertEqual(launch_command(manifest, root), [str(python), "-B", *tail])
            # The ._pth file marks the bundled runtime; -s keeps the user's own packages out.
            (python.parent / "python314._pth").write_text("Lib\\site-packages\nimport site\n", encoding="utf-8")
            self.assertEqual(python_flags(python), ["-s"])
            self.assertEqual(launch_command(manifest, root), [str(python), "-s", "-B", *tail])


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        write_plugin(base / "plugins")
        self.registry = PluginRegistry(base, state_path=base / "state.json")
        self.supervisor = PluginSupervisor(self.registry, root=ROOT, data_dir=base / "plugin-data",
                                           core_url="http://127.0.0.1:9")
        self.store = CoreStore(base / "core.sqlite3")
        self.tasks = TaskManager(self.store, self.supervisor, self.registry)

    def tearDown(self):
        self.supervisor.close(grace=1)
        self.store.close()
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.05)
        self.fail("condition not reached")

    def test_rpc_round_trip_and_errors(self):
        result = self.supervisor.rpc("kyf.test", "echo", {"a": 1}, {"account": "acct", "user": "u1"})
        self.assertEqual(result, {"params": {"a": 1}, "context": {"account": "acct", "user": "u1"}})
        with self.assertRaises(PluginError) as caught:
            self.supervisor.rpc("kyf.test", "fail")
        self.assertEqual(caught.exception.code, "bad-thing")
        with self.assertRaises(PluginUnavailable) as timeout:
            self.supervisor.rpc("kyf.test", "sleep", {"seconds": 2}, timeout=0.3)
        self.assertEqual(timeout.exception.code, "plugin-timeout")
        self.assertTrue((Path(self.temp.name) / "plugin-data" / "kyf.test").is_dir())

    def test_python_plugin_imports_from_the_same_user_site_as_core(self):
        # Plugins get no APPDATA; a system Python must still see packages installed with pip --user.
        expected = site.getusersitepackages() if site.ENABLE_USER_SITE else None
        self.assertEqual(self.supervisor.rpc("kyf.test", "userSite"), expected)

    def test_crash_fails_pending_calls_and_restarts_on_demand(self):
        first = self.supervisor.ensure("kyf.test")
        with self.assertRaises(PluginUnavailable):
            self.supervisor.rpc("kyf.test", "crash")
        self.wait_for(lambda: not first.alive())
        self.assertEqual(self.supervisor.rpc("kyf.test", "echo")["params"], {})
        self.assertNotEqual(self.supervisor.ensure("kyf.test").token, first.token)
        self.assertIsNone(self.supervisor.plugin_for_token(first.token))

    def test_repeated_crashes_block_until_reset(self):
        for _ in range(3):
            process = self.supervisor.ensure("kyf.test")
            with self.assertRaises(PluginUnavailable):
                self.supervisor.rpc("kyf.test", "crash")
            self.wait_for(lambda: not process.alive())
        with self.assertRaises(PluginUnavailable) as blocked:
            self.supervisor.ensure("kyf.test")
        self.assertEqual(blocked.exception.code, "plugin-crashed")
        self.supervisor.reset("kyf.test")
        self.assertEqual(self.supervisor.rpc("kyf.test", "echo")["params"], {})

    def test_scheme_conflict_queue_and_stop(self):
        first = self.tasks.create("kyf.test", kind="scheme", account="acct", user="u1", scheme="default",
                                  title="分析")["task"]
        self.assertEqual(first["state"], "running")
        with self.assertRaises(TaskError) as conflict:
            self.tasks.create("kyf.test", kind="scheme", account="acct", user="u1", scheme="default", title="再分析")
        self.assertEqual(conflict.exception.status, 409)
        queued = self.tasks.create("kyf.test", kind="scheme", account="acct", user="u1", scheme="default",
                                   title="排队", conflict="queue")["task"]
        self.assertEqual(queued["state"], "queued")
        stopping = self.tasks.stop(first["id"])
        self.assertIn(stopping["state"], ("stopping", "stopped"))
        self.wait_for(lambda: self.tasks.task(first["id"])["state"] == "stopped")
        self.wait_for(lambda: self.tasks.task(queued["id"])["state"] == "running")
        self.wait_for(lambda: queued["id"] in self.supervisor.rpc("kyf.test", "started"))

    def test_plugin_finish_and_late_writes_are_rejected(self):
        created = self.tasks.create("kyf.test", kind="background", account="acct", user="u1", title="后台",
                                    from_plugin=True)
        token = created["execution"]["token"]
        task_id = created["task"]["id"]
        self.tasks.progress(token, "kyf.test", task_id, {"done": 1, "total": 2})
        self.assertEqual(self.tasks.task(task_id)["progress"], {"done": 1, "total": 2})
        with self.assertRaises(TaskError):
            self.tasks.progress(token, "kyf.other", task_id, {"done": 2})
        self.tasks.finish(token, "kyf.test", task_id, "done")
        self.assertEqual(self.tasks.task(task_id)["state"], "done")
        with self.assertRaises(TaskError) as late:
            self.tasks.progress(token, "kyf.test", task_id, {"done": 2})
        self.assertEqual(late.exception.code, "execution-revoked")

    def test_unresponsive_stop_kills_only_the_plugin_group(self):
        task = self.tasks.create("kyf.test", kind="scheme", account="acct", user="u1", scheme="default",
                                 title="不配合", params={"mode": "ignore-cancel"})["task"]
        process = self.supervisor.ensure("kyf.test")
        import core_tasks
        original = core_tasks.STOP_GRACE
        core_tasks.STOP_GRACE = 0.5
        try:
            self.tasks.stop(task["id"])
        finally:
            core_tasks.STOP_GRACE = original
        self.wait_for(lambda: self.tasks.task(task["id"])["state"] == "stopped")
        self.wait_for(lambda: not process.alive())

    def test_a_plugin_not_reading_its_pipe_cannot_block_core_or_other_plugins(self):
        folder = Path(self.temp.name) / "plugins" / "kyf.test"
        (folder / "main.py").write_text(
            'import sys, time\nsys.stdin.readline()\n'
            'print(\'{"type":"ready","protocol":1}\', flush=True)\ntime.sleep(60)\n', encoding="utf-8")
        write_plugin(Path(self.temp.name) / "plugins", plugin_id="kyf.other")
        self.registry.refresh()
        process = self.supervisor.ensure("kyf.test")
        started = time.monotonic()
        with self.assertRaises(PluginUnavailable) as busy:
            for _ in range(40):
                process.send({"type": "event", "data": "x" * (1024 * 1024)})
        self.assertEqual(busy.exception.code, "plugin-busy")
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(self.supervisor.rpc("kyf.other", "echo", {"responsive": True})["params"],
                         {"responsive": True})
        process.shutdown(grace=.1)
        self.wait_for(lambda: not process.alive())

    def test_plugin_exit_interrupts_running_tasks_and_continue_starts_new_generation(self):
        task = self.tasks.create("kyf.test", kind="scheme", account="acct", user="u1", scheme="default",
                                 title="中断")["task"]
        process = self.supervisor.ensure("kyf.test")
        process.kill("测试结束")
        self.wait_for(lambda: self.tasks.task(task["id"])["state"] == "interrupted")
        resumed = self.tasks.continue_task(task["id"])["task"]
        self.assertEqual(resumed["state"], "running")
        executions = self.store.connection.execute(
            "SELECT generation, state FROM executions WHERE task_id=? ORDER BY generation", (task["id"],)).fetchall()
        self.assertEqual([row[0] for row in executions], [1, 2])
        self.assertEqual(executions[0][1], "revoked")

    def test_restart_marks_unfinished_tasks_interrupted(self):
        task = self.tasks.create("kyf.test", kind="scheme", account="acct", user="u1", scheme="default",
                                 title="重启")["task"]
        TaskManager(self.store, self.supervisor, self.registry)
        self.assertEqual(self.store.task(task["id"])["state"], "interrupted")


if __name__ == "__main__":
    unittest.main()
