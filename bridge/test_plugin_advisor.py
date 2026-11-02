"""Real advisor process + Core RPC/task integration, with synthetic data and no model calls.

The temporary app root contains an explicit, non-executable engine fixture, like
the native runtime unit tests. Preparation is gated before the plugin can launch
that engine. The production manifest, main.py, plugin SDK, Core and supervisor all
run normally; no account, credential, or engine state from the machine is read.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend_service import Backend  # noqa: E402
from conversation_selection import ConversationSelectionStore  # noqa: E402
from core_account_db import AccountStores  # noqa: E402
from core_history import HistoryService  # noqa: E402
from core_human import HumanService  # noqa: E402
from core_projection import ContextProjection  # noqa: E402
from core_services import CoreServices  # noqa: E402
from real_http import make_handler  # noqa: E402
from test_support import FakeModelStore, FakeSource, make_message  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT, FRIEND, AGENT = "synthetic-advisor-account", "synthetic-advisor-friend", "builtin:advisor"
PLUGIN = "kyf.advisor"


class GatedHistory(HistoryService):
    def __init__(self, stores, context):
        super().__init__(stores, context)
        self.entered = threading.Event()
        self.release = threading.Event()

    def prepare(self, account, user, refresh=False):
        self.entered.set()
        if not self.release.wait(15):
            raise RuntimeError("synthetic history gate timed out")
        return super().prepare(account, user, refresh)


class AdvisorPluginIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        # Only plugin Python imports are needed: the cancelled turn must never
        # execute the Node worker, the tsx loader or this engine fixture.
        app = base / "app"
        shutil.copytree(ROOT / "plugin-sdk" / "python", app / "plugin-sdk" / "python",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        sdk_marker = app / "node_modules" / "@opencode-ai" / "sdk" / "package.json"
        sdk_marker.parent.mkdir(parents=True)
        sdk_marker.write_text('{"name":"synthetic-sdk-marker"}', encoding="utf-8")
        engine = app / "runtime" / "opencode"
        engine.mkdir(parents=True)
        stub = b"Synthetic integration fixture. Must never be executed."
        (engine / "opencode.exe").write_bytes(stub)
        (engine / "manifest.json").write_text(json.dumps({"schema": 1, "version": "1.18.34",
            "file": "opencode.exe", "bytes": len(stub), "sha256": hashlib.sha256(stub).hexdigest()}), encoding="utf-8")
        self.source = FakeSource(account=ACCOUNT, workdir=str(base / "synthetic-source"))
        self.source.add_messages(FRIEND, [make_message(1, text="合成好友消息，仅用于插件集成测试")])
        self.source.messages = lambda user, limit: list(self.source.users.get(user, []))[-limit:]
        self.source.sessions = lambda: {"account": ACCOUNT, "sessions": [
            {"username": FRIEND, "name": "合成好友", "isGroup": False}]}
        selection = ConversationSelectionStore(base / "selection")
        selection.set_all_selected(ACCOUNT, [FRIEND])
        self.backend = Backend(self.source, model_source_store=FakeModelStore(context_tokens=32768),
                               selection_store=selection)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.stores = AccountStores(base / "synthetic-data")
        self.context = ContextProjection(self.stores, self.source)
        self.core = CoreServices(self.backend, ROOT, self.url, local=base / ".local",
                                 bundled_dir=ROOT / "plugins", local_dir=base / "no-development-plugins")
        self.core.supervisor.root = app
        self.history = GatedHistory(self.stores, self.context)
        self.core._history = self.history
        self.core._human = HumanService(self.stores, source=self.source)
        self.backend._core = self.core
        self.addCleanup(self.close_services)

    def close_services(self):
        self.history.release.set()
        # Keep HTTP alive while the plugin sends task cancellation and shutdown.
        self.core.close()
        self.context.close()
        self.stores.close()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(3)

    def rpc(self, method, params=None, account=ACCOUNT, user=FRIEND):
        payload = {"plugin": PLUGIN, "method": method, "params": params or {}, "account": account, "user": user}
        request = urllib.request.Request(self.url + "/api/plugins/rpc", method="POST",
            data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())["result"]

    def wait_task(self, identifier, states):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            task = self.core.store.task(identifier)
            if task and task["state"] in states:
                return task
            time.sleep(.025)
        self.fail("synthetic advisor task did not reach expected state")

    def test_actual_entry_loads_catalog_scoped_sessions_and_drafts(self):
        manifest = self.core.registry.get(PLUGIN)
        self.assertEqual(manifest["runtime"]["entry"], "backend/main.py")
        catalog = self.rpc("catalog.get")
        self.assertTrue(any(agent["id"] == AGENT for agent in catalog["agents"]))
        self.assertEqual(catalog["engine"]["state"], "available")
        process = self.core.supervisor.running(PLUGIN)
        self.assertIsNotNone(process)
        self.assertTrue(process.ready.is_set())
        self.assertNotEqual(process.process.pid, os.getpid())
        initial = self.rpc("thread.get", {"agentId": AGENT})
        thread = initial["thread"]
        self.assertEqual((thread["account"], thread["user"]), (ACCOUNT, FRIEND))
        self.assertEqual(thread["messages"], [])
        saved = self.rpc("draft.save", {"agentId": AGENT, "threadId": thread["id"],
            "text": "合成草稿", "version": 0, "scrollTop": 12})
        self.assertEqual(saved["draft"]["text"], "合成草稿")
        renamed = self.rpc("thread.rename", {"agentId": AGENT, "threadId": thread["id"],
            "title": "集成测试讨论", "requestId": "synthetic-rename"})
        self.assertEqual(renamed["thread"]["title"], "集成测试讨论")
        new = self.rpc("thread.new", {"agentId": AGENT, "requestId": "synthetic-new"})
        self.assertNotEqual(new["thread"]["id"], thread["id"])
        listing = self.rpc("threads.list", {"agentId": AGENT})
        self.assertEqual({row["id"] for row in listing["threads"]}, {thread["id"], new["thread"]["id"]})
        self.assertEqual(self.rpc("thread.select", {"agentId": AGENT, "threadId": thread["id"]})["thread"]["draft"],
                         saved["draft"])
        self.assertFalse(self.history.entered.is_set(), "browsing discussion metadata does not start history/model work")
        self.assertEqual(self.core.store.tasks(account=ACCOUNT), [])
        self.assertEqual(self.core.ledger.records({"account": ACCOUNT})["records"], [])
        with self.assertRaises(urllib.error.HTTPError) as stale:
            self.rpc("thread.get", {"agentId": AGENT}, account="another-synthetic-account")
        with stale.exception as response:
            error = json.loads(response.read())
        self.assertEqual(stale.exception.code, 422)
        self.assertEqual(error["error"], "account-changed")

    # Added by Know Your Friends, 2026-10-16 (Apache-2.0): queue RPCs use the actual
    # isolated plugin process, with synthetic history blocked before model access.
    def test_durable_queue_edit_remove_and_scope_through_real_plugin_rpc(self):
        thread = self.rpc("thread.get", {"agentId": AGENT})["thread"]
        scope = {"agentId": AGENT, "threadId": thread["id"]}
        self.assertTrue(self.rpc("queue.pause", scope)["queue"]["paused"])
        payload = {**scope, "message": "合成等待问题", "requestId": "synthetic-queue-accept", "thinking": False}
        accepted = self.rpc("queue.enqueue", payload)
        item = accepted["item"]
        self.assertEqual(self.rpc("queue.enqueue", payload)["item"]["id"], item["id"])
        edited = self.rpc("queue.edit", {**scope, "itemId": item["id"], "version": item["version"],
                                         "message": "编辑后的合成问题"})["queue"]["items"][0]
        self.assertEqual(edited["message"], "编辑后的合成问题")
        self.assertIs(edited["thinking"], False)
        self.rpc("queue.enqueue", payload)
        current = self.rpc("thread.get", scope)
        self.assertEqual(current["thread"]["queue"]["items"][0]["message"], edited["message"])
        self.assertEqual(current["thread"]["messages"], [], "waiting questions are not discussion turns yet")
        with self.assertRaises(urllib.error.HTTPError) as foreign:
            self.rpc("queue.get", scope, user="synthetic-foreign-contact")
        foreign.exception.close()
        self.assertEqual(self.rpc("queue.remove", {**scope, "itemId": item["id"],
                                                   "version": edited["version"]})["queue"]["items"], [])
        self.rpc("queue.enqueue", payload)
        self.assertEqual(self.rpc("queue.get", scope)["queue"]["items"], [], "retry cannot revive removed content")
        self.assertFalse(self.history.entered.is_set())
        self.assertEqual(self.core.store.tasks(account=ACCOUNT), [])

    def test_queue_resume_and_stop_preserve_followups_without_model_access(self):
        thread = self.rpc("thread.get", {"agentId": AGENT})["thread"]
        scope = {"agentId": AGENT, "threadId": thread["id"]}
        self.rpc("queue.pause", scope)
        for index in (1, 2):
            self.rpc("queue.enqueue", {**scope, "message": f"合成排队问题 {index}",
                                       "requestId": f"synthetic-queue-{index}", "thinking": False})
        self.rpc("queue.resume", scope)
        self.assertTrue(self.history.entered.wait(5))
        current = self.rpc("thread.get", scope)
        self.assertEqual(current["run"]["state"], "running")
        self.assertEqual([row["text"] for row in current["thread"]["messages"] if row["role"] == "user"],
                         ["合成排队问题 1"])
        self.rpc("run.stop", {"runId": current["run"]["id"]})
        queued = self.rpc("queue.get", scope)["queue"]
        self.assertTrue(queued["paused"])
        self.assertEqual([row["message"] for row in queued["items"]], ["合成排队问题 2"])
        self.assertFalse(self.history.release.is_set(), "stopping must not wait for history preparation")
        self.history.release.set()
        task = next(task for task in self.core.store.tasks(account=ACCOUNT) if task["plugin"] == PLUGIN)
        self.wait_task(task["id"], {"stopped"})
        self.assertEqual(len(self.core.store.tasks(account=ACCOUNT)), 1, "paused followup creates no new task")
        self.assertEqual(self.core.ledger.records({"account": ACCOUNT})["records"], [])

    def test_run_start_registers_execution_and_stop_revokes_it_before_history_returns(self):
        thread = self.rpc("thread.get", {"agentId": AGENT})["thread"]
        started = self.rpc("run.start", {"agentId": AGENT, "threadId": thread["id"],
            "message": "请回答这个合成问题", "requestId": "synthetic-run", "thinking": False})
        self.assertTrue(self.history.entered.wait(5))
        task = next(task for task in self.core.store.tasks(account=ACCOUNT) if task["plugin"] == PLUGIN)
        self.assertEqual((task["kind"], task["state"], task["user"]), ("interactive", "running", FRIEND))
        self.assertEqual(task["params"]["runId"], started["run"]["id"])
        self.assertFalse(task["params"]["thinking"])
        with self.core.tasks.lock:
            self.assertEqual({binding["task"] for binding in self.core.tasks.live.values()}, {task["id"]})
        stopped = self.rpc("run.stop", {"runId": started["run"]["id"]})
        self.assertEqual(stopped["run"]["state"], "stopped")
        self.assertFalse(self.history.release.is_set(), "stop must not wait for Core history preparation")
        with self.core.tasks.lock:
            self.assertEqual(self.core.tasks.live, {}, "Core revokes the task execution before acknowledging stop")
        self.history.release.set()
        self.assertEqual(self.wait_task(task["id"], {"stopped"})["state"], "stopped")
        events = self.rpc("run.events", {"runId": started["run"]["id"], "after": 0})
        self.assertEqual(events["run"]["state"], "stopped")
        self.assertTrue(any(event["type"] == "done" for event in events["events"]))
        transcript = events["thread"]["messages"]
        self.assertEqual([item["text"] for item in transcript if item["role"] == "user"], ["请回答这个合成问题"])
        self.assertFalse(any(item["role"] == "assistant" for item in transcript))
        self.assertEqual(self.core.ledger.records({"account": ACCOUNT})["records"], [])
        self.assertTrue(self.core.supervisor.running(PLUGIN).alive())


if __name__ == "__main__":
    unittest.main()
