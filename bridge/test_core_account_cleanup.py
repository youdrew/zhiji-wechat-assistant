"""Account deletion cannot release queued work or leave model execution tokens alive."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core_account_db import AccountStores, _long_path
from core_plugin_process import PluginUnavailable
from core_services import CoreServices
from core_store import CoreStore
from core_tasks import TaskError, TaskManager
from test_core_tasks import Supervisor

# Removes the Core account folder and the plugins' account folders like an account clear does, in a child process
# behaving like a default Windows install (LongPathsEnabled=0): it clears its own PEB.IsLongPathAwareProcess flag,
# so plain paths longer than MAX_PATH (an engine's npm cache) fail there as they do for most users.
SHORT_PATH_CLEAR = r'''
import ctypes, json, sys
sys.path.insert(0, sys.argv[1])
ntdll = ctypes.WinDLL("ntdll")
class Basic(ctypes.Structure):
    _fields_ = [("status", ctypes.c_void_p), ("peb", ctypes.c_void_p), ("rest", ctypes.c_void_p * 4)]
basic = Basic()
ntdll.NtQueryInformationProcess(ctypes.c_void_p(-1), 0, ctypes.byref(basic), ctypes.sizeof(basic), None)
flags = ctypes.c_ubyte.from_address(basic.peb + 3)
flags.value &= ~0x80
ntdll.RtlAreLongPathsEnabled.restype = ctypes.c_ubyte
from types import SimpleNamespace
from core_account_db import AccountStores
from core_services import CoreServices
root, account = sys.argv[2], sys.argv[3]
core = CoreServices.__new__(CoreServices)
core.local = __import__("pathlib").Path(root) / ".local"
core.registry, core.tasks = SimpleNamespace(plugins={}), SimpleNamespace(quiesce_account=lambda _account: None)
core.store = SimpleNamespace(delete_account=lambda _account: None)
core.ledger = SimpleNamespace(clear=lambda _account: None)
stores = AccountStores(root)
result = {"shortPaths": not ntdll.RtlAreLongPathsEnabled()}
for name, action in (("core", lambda: stores.remove_account(account)),
                     ("plugins", lambda: core.clear_account(account))):
    try:
        action()
        result[name] = "removed"
    except OSError as exc:
        result[name] = type(exc).__name__
print(json.dumps(result))
'''


class ExitingSupervisor(Supervisor):
    def ensure(self, _plugin):
        if self.process is None:
            self.process = self.new_process("replacement")
        return self.process

    def stop(self, plugin, _reason):
        process, self.process = self.process, None
        if process is not None:
            self.tasks._plugin_exit(plugin, True, process.token)


class AccountCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = CoreStore(self.root / "core.sqlite3")
        self.supervisor = ExitingSupervisor()
        registry = SimpleNamespace(plugins={"test.plugin": {}}, enabled=lambda _: True, get=lambda _: {
            "version": "1", "contributes": {"schemes": [{"id": "default"}]}})
        self.tasks = TaskManager(self.store, self.supervisor, registry)
        self.supervisor.tasks = self.tasks
        self.cleared = []
        self.core = CoreServices.__new__(CoreServices)
        self.core.registry, self.core.supervisor = registry, self.supervisor
        self.core.store, self.core.tasks = self.store, self.tasks
        self.core.local = self.root
        self.core.ledger = SimpleNamespace(clear=self.cleared.append)
        self.account_dir = self.root / "plugin-data" / "test.plugin" / "accounts" / self.core.account_key("a")
        self.account_dir.mkdir(parents=True)
        (self.account_dir / "synthetic.txt").write_text("synthetic", encoding="utf-8")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def create(self, account="a", **extra):
        return self.tasks.create("test.plugin", kind="scheme", account=account, user="u",
                                 scheme="default", title="synthetic", **extra)

    def test_clear_revokes_running_and_queued_tasks_before_exit_can_restart_them(self):
        first = self.create()
        self.create(conflict="queue")
        self.create(conflict="queue")
        token = self.supervisor.messages[0][1]["execution"]["token"]

        self.core.clear_account("a")

        self.assertEqual(self.store.tasks(account="a"), [])
        self.assertEqual(self.tasks.live, {})
        self.assertEqual(self.tasks.owners, {})
        self.assertEqual(self.tasks.deadlines, {})
        self.assertIsNone(self.supervisor.process)
        starts = [message for _plugin, message in self.supervisor.messages if message["type"] == "task.start"]
        self.assertEqual([message["task"]["id"] for message in starts], [first["task"]["id"]])
        with self.assertRaises(TaskError):
            self.tasks.verify(token, "test.plugin")
        self.assertFalse(self.account_dir.exists())
        self.assertEqual(self.cleared, ["a"])

    def test_cleanup_blocks_late_creation_resume_and_plugin_access_only_for_that_account(self):
        previous = self.create(from_plugin=True)
        self.tasks.finish(previous["execution"]["token"], "test.plugin", previous["task"]["id"], "partial")
        self.tasks.quiesce_account("a")
        with self.assertRaises(TaskError) as continued:
            self.tasks.continue_task(previous["task"]["id"])
        self.assertEqual(continued.exception.code, "account-cleared")

        self.core.clear_account("a")

        with self.assertRaises(TaskError) as created:
            self.create()
        self.assertEqual(created.exception.code, "account-cleared")
        with self.assertRaises(PluginUnavailable) as accessed:
            self.core.require_plugin("test.plugin", "a")
        self.assertEqual(accessed.exception.code, "account-cleared")
        self.assertEqual(self.create(account="b")["task"]["state"], "running")

    @unittest.skipUnless(os.name == "nt", "MAX_PATH is a Windows limit")
    def test_clear_removes_engine_trees_deeper_than_max_path(self):
        account = "synthetic-account-1"
        stores = AccountStores(self.root)
        self.addCleanup(stores.close)
        database = stores.account(account)
        database._connect()
        stores.release_account(account)
        # The old engine state (runtime/) and the assistant plugin's engine state both hold npm caches.
        cache = ("home", "AppData", "Local", "npm-cache", "_cacache", "content-v2", "sha512", "00", "68", "ab" * 62)
        plugin = self.root / ".local" / "plugin-data" / "kyf.advisor" / "accounts" / self.core.account_key(account)
        for path in (database.directory / "runtime", plugin / "engine" / "engine"):
            os.makedirs(_long_path(path.joinpath(*cache[:-1])))
            with open(_long_path(path.joinpath(*cache)), "w", encoding="utf-8") as stream:
                stream.write("cached package")
        completed = subprocess.run([sys.executable, "-c", SHORT_PATH_CLEAR, str(Path(__file__).parent),
                                    str(self.root), account], capture_output=True, text=True, timeout=120)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"shortPaths": True, "core": "removed", "plugins": "removed"})
        self.assertFalse(os.path.lexists(_long_path(database.directory)))
        self.assertFalse(os.path.lexists(_long_path(plugin)))

    def test_cleanup_covers_more_than_one_task_query_page(self):
        for number in range(501):
            self.store.insert_task({"id": str(number), "plugin": "test.plugin", "plugin_version": "1",
                "kind": "background", "account": "a", "user": "u", "scheme": None,
                "title": "synthetic", "state": "queued"})
        self.tasks.quiesce_account("a")
        self.assertEqual(self.store.tasks(account="a", states=("queued", "running", "stopping")), [])
        with self.store.lock:
            stopped = self.store.connection.execute("SELECT COUNT(*) FROM tasks WHERE state='stopped'").fetchone()[0]
        self.assertEqual(stopped, 501)


if __name__ == "__main__":
    unittest.main()
