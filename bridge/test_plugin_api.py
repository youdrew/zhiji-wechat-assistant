"""The plugin interface over loopback HTTP with a real plugin process and synthetic chat data.

Modified 2026-10-13: a plugin stops its own tasks and claims host-picked files (kyf.advisor needs both).
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend_service import Backend  # noqa: E402
from conversation_selection import ConversationSelectionStore  # noqa: E402
from core_account_db import AccountStores  # noqa: E402
from core_history import HistoryService  # noqa: E402
from core_human import HumanService  # noqa: E402
from core_projection import ContextProjection  # noqa: E402
from core_services import GRANT_TTL, CoreServices  # noqa: E402
from core_tasks import TaskError  # noqa: E402
from real_http import make_handler  # noqa: E402
from test_support import FakeModelStore, FakeSource, make_message  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FRIEND = "wxid_friend_a"
PLUGIN_MAIN = r'''
import time
from kyf_plugin import CoreError, Plugin
plugin = Plugin()
SEEN = {}
LONG = {}

@plugin.method("long")
def long_task(params, context):
    def work(task):
        while not task.cancelled.wait(0.05):
            pass
        SEEN["long-token"] = bool(task.token)
        task.check()
    task, ctx = plugin.start_task(context["account"], context["user"], title="长任务", handler=work)
    LONG[task["id"]] = ctx
    return task["id"]

@plugin.method("stop")
def stop_task(params, context):
    return LONG[params["id"]].stop()["task"]["state"]

@plugin.method("claim")
def claim(params, context):
    try:
        return plugin.core.claim_file(params["token"])
    except CoreError as exc:
        return {"error": exc.code, "status": exc.status}

@plugin.method("seen")
def seen(params, context):
    return SEEN

@plugin.method("settings")
def settings(params, context):
    return plugin.core.settings()

@plugin.method("background")
def background(params, context):
    created = plugin.core.post("tasks", {"kind": "interactive", "account": context["account"],
                                         "user": context["user"], "title": "插件自己的任务"})
    def work(task):
        task.progress(1, 2, "一半")
        return "done"
    plugin.run_task(created["task"], created["execution"], work)
    return created["task"]["id"]

@plugin.task("scheme")
def run(task):
    core = task.core
    SEEN["scope"] = core.scope()
    SEEN["friends"] = core.get("friends", account=task.account)
    core.history_prepare(task.account, task.user)
    for _ in range(200):
        status = core.history_status(task.account, task.user)
        if status["state"] == "ready":
            break
        time.sleep(0.05)
    SEEN["status"] = status
    page = core.history_messages(task.account, task.user, limit=2)
    SEEN["page"] = page
    SEEN["export"] = list(core.history_export(task.account, task.user))
    first = page["messages"][0]
    SEEN["verify"] = core.post("history/verify", {"account": task.account, "user": task.user,
        "items": [{"id": first["id"], "hash": first["hash"]}, {"id": first["id"], "hash": "0" * 64},
                  {"id": "missing", "hash": "x"}]})
    SEEN["verify-many"] = core.history_verify_many(task.account, [{"user": task.user, "items": [
        {"id": first["id"], "hash": first["hash"]}, {"id": first["id"], "hash": "0" * 64},
        {"id": "missing", "hash": "x"}]}])
    SEEN["around"] = core.get("history/context", account=task.account, user=task.user, id=first["id"], before=1, after=1)
    SEEN["candidates"] = core.post("human/candidates", {"account": task.account, "user": task.user, "candidates": [
        {"text": "喜欢周末看展", "tags": ["爱好"], "proposalKey": "k1",
         "evidence": [{"sourceFp": status["sourceFingerprint"], "msgId": first["id"], "hash": first["hash"],
                       "role": "other", "excerpt": first["text"]}]},
        {"text": "喜欢周末看展！", "tags": ["爱好"], "proposalKey": "k2", "evidence": []}]})
    SEEN["human"] = core.human(task.account, task.user)
    SEEN["snapshot"] = core.human_snapshot(task.account, task.user)
    SEEN["media"] = core.media(task.account, task.user, first["id"])
    task.progress(3, 3, "完成")
    return "done"

plugin.run()
'''


class PluginApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        folder = base / "plugins" / "kyf.api-test"
        folder.mkdir(parents=True)
        (folder / "main.py").write_text(PLUGIN_MAIN, encoding="utf-8")
        (folder / "page.html").write_text("<!doctype html>", encoding="utf-8")
        (folder / "plugin.json").write_text(json.dumps({
            "schema": 1, "id": "kyf.api-test", "name": "接口测试", "version": "1.0.0", "core": {"api": [1]},
            "runtime": {"type": "python", "entry": "main.py"},
            "contributes": {"schemes": [{"id": "default", "name": "默认", "page": "page.html",
                                         "tabs": [{"id": "a", "title": "甲"}]}]}}), encoding="utf-8")
        other = base / "plugins" / "kyf.other-test"
        other.mkdir(parents=True)
        (other / "main.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (other / "plugin.json").write_text(json.dumps({
            "schema": 1, "id": "kyf.other-test", "name": "其他插件", "version": "1.0.0", "core": {"api": [1]},
            "runtime": {"type": "python", "entry": "main.py"}}), encoding="utf-8")
        self.picked = base / "picked" / "技能包.zip"
        self.picked.parent.mkdir()
        self.picked.write_bytes(b"PK")
        self.source = FakeSource(account="synthetic-account-1")
        self.source.add_messages(FRIEND, [make_message(index, text=f"第 {index} 条") for index in range(1, 6)])
        self.source.messages = lambda user, limit: list(self.source.users.get(user, []))[-limit:]
        self.source.sessions = lambda: {"account": "synthetic-account-1", "sessions": [
            {"username": FRIEND, "name": "小明", "isGroup": False}]}
        selection = ConversationSelectionStore(base / "selection")
        selection.set_all_selected("synthetic-account-1", [FRIEND])
        self.backend = Backend(self.source, model_source_store=FakeModelStore(), selection_store=selection)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        core = CoreServices(self.backend, ROOT, self.url, local=base / ".local", bundled_dir=base / "plugins",
                            local_dir=base / "none")
        stores = self.stores = AccountStores(base)
        self.context = ContextProjection(stores, self.source)
        core._history = HistoryService(stores, self.context)
        core._human = HumanService(stores)
        self.backend._core = core
        self.core = core

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.core.close()
        self.context.close()
        self.stores.close()
        self.temp.cleanup()

    def post(self, path, body):
        request = urllib.request.Request(self.url + path, data=json.dumps(body).encode("utf-8"), method="POST",
                                         headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())

    def get(self, path):
        with urllib.request.urlopen(self.url + path, timeout=30) as response:
            return json.loads(response.read())

    def wait_task(self, task_id):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            task = self.get(f"/api/task?id={task_id}")["task"]
            if task["state"] not in ("queued", "running", "stopping"):
                return task
            time.sleep(0.05)
        self.fail("task did not finish")

    def test_scheme_task_reads_history_and_submits_candidates(self):
        created = self.post("/api/tasks", {"plugin": "kyf.api-test", "account": "synthetic-account-1",
                                           "user": FRIEND, "scheme": "default", "title": "测试"})
        task = self.wait_task(created["task"]["id"])
        self.assertEqual(task["state"], "done")
        self.assertEqual(task["progress"]["label"], "完成")
        seen = self.post("/api/plugins/rpc", {"plugin": "kyf.api-test", "method": "seen"})["result"]
        self.assertEqual(seen["scope"]["account"], "synthetic-account-1")
        self.assertEqual(seen["friends"]["friends"], [{"user": FRIEND, "name": "小明", "isGroup": False}])
        self.assertEqual(seen["status"]["readCount"], 5)
        self.assertEqual(seen["status"]["watermark"]["seq"], 5)
        self.assertEqual(len(seen["page"]["messages"]), 2)
        self.assertEqual(seen["page"]["next"], 2)
        self.assertEqual([line["record"] for line in seen["export"]], ["message"] * 5 + ["end"])
        self.assertEqual([item["state"] for item in seen["verify"]["results"]], ["same", "changed", "missing"])
        self.assertEqual(seen["verify-many"]["results"][0]["user"], FRIEND)
        self.assertEqual([item["state"] for item in seen["verify-many"]["results"][0]["results"]],
                         ["same", "changed", "missing"])
        self.assertEqual(len(seen["around"]["messages"]), 2)
        self.assertEqual(len(seen["candidates"]["inserted"]), 1)
        self.assertEqual(seen["candidates"]["skipped"], 1)
        candidate = seen["human"]["candidates"][0]
        self.assertEqual(candidate["plugin"], "kyf.api-test")
        self.assertEqual(candidate["evidence"][0]["excerpt"], "第 1 条")
        self.assertTrue(seen["snapshot"]["stamp"].startswith("friend-v2-"))
        self.assertEqual(seen["snapshot"]["data"]["memories"][0]["text"], "喜欢周末看展")
        self.assertEqual(seen["media"], {"error": "unsupported"}, "the synthetic source reads no media")

    def test_plugin_initiated_task_and_token_checks(self):
        task_id = self.post("/api/plugins/rpc", {"plugin": "kyf.api-test", "method": "background",
                                                 "account": "synthetic-account-1", "user": FRIEND})["result"]
        task = self.get(f"/api/task?id={task_id}")["task"]
        self.assertEqual((task["state"], task["kind"]), ("done", "interactive"))
        request = urllib.request.Request(self.url + "/plugin-api/v1/scope")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 401)
        token = self.core.supervisor.running("kyf.api-test").token
        late = urllib.request.Request(self.url + f"/plugin-api/v1/tasks/{task_id}/progress", method="POST",
                                      data=b'{"done": 2}', headers={"Authorization": f"Bearer {token}",
                                                                    "X-KYF-Execution": "stale",
                                                                    "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as revoked:
            urllib.request.urlopen(late, timeout=10)
        self.assertEqual(revoked.exception.code, 409)

    def test_settings_use_the_host_routes_and_sdk_without_a_task_or_business_start(self):
        manifest_path = Path(self.temp.name) / "plugins" / "kyf.api-test" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["contributes"]["settings"] = {"schema": 1, "groups": [{"id": "behavior", "title": "行为", "fields": [
            {"key": "autoUpdate", "title": "自动更新", "type": "boolean", "default": True}]}]}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.core.registry.refresh()
        self.post("/api/plugins/enable", {"id": "kyf.api-test", "enabled": False})
        initial = self.get("/api/plugins/settings?id=kyf.api-test")
        saved = self.post("/api/plugins/settings", {"id": "kyf.api-test", "revision": initial["revision"],
            "definition": initial["definition"], "values": {"autoUpdate": False}})
        self.assertFalse(saved["enabled"])
        self.assertIsNone(self.core.supervisor.running("kyf.api-test"))
        self.post("/api/plugins/enable", {"id": "kyf.api-test", "enabled": True})
        received = self.post("/api/plugins/rpc", {"plugin": "kyf.api-test", "method": "settings"})["result"]
        self.assertEqual((received["values"], received["revision"]), ({"autoUpdate": False}, 1))
        self.assertEqual(received["definition"], initial["definition"])
        self.assertEqual(self.core.store.tasks(plugin="kyf.api-test"), [])

    def test_plugin_settings_reports_corrupt_or_incompatible_saved_values_over_http(self):
        manifest_path = Path(self.temp.name) / "plugins" / "kyf.api-test" / "plugin.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["contributes"]["settings"] = {"schema": 1, "groups": [{"id": "behavior", "title": "行为", "fields": [
            {"key": "autoUpdate", "title": "自动更新", "type": "boolean", "default": True}]}]}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.core.registry.refresh()
        process = self.core.supervisor.ensure("kyf.api-test")
        settings_path = self.core.supervisor.data_directory("kyf.api-test") / "settings.json"
        for content, code in [("broken json", "settings-corrupt"),
                              (json.dumps({"schema": 1, "revision": 1, "values": {"autoUpdate": "invalid"}}),
                               "settings-schema-changed")]:
            with self.subTest(code=code):
                settings_path.write_text(content, encoding="utf-8")
                request = urllib.request.Request(self.url + "/plugin-api/v1/settings",
                    headers={"Authorization": f"Bearer {process.token}"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=10)
                with caught.exception as response:
                    self.assertEqual(response.code, 409)
                    body = json.loads(response.read())
                self.assertEqual(body["error"], code)
                self.assertTrue(body["message"])
                self.assertEqual(settings_path.read_text(encoding="utf-8"), content)

    def rpc(self, method, params=None):
        return self.post("/api/plugins/rpc", {"plugin": "kyf.api-test", "method": method, "params": params or {},
                                              "account": "synthetic-account-1", "user": FRIEND})["result"]

    def test_plugin_stops_only_its_own_tasks(self):
        task_id = self.rpc("long")
        self.assertEqual(self.get(f"/api/task?id={task_id}")["task"]["state"], "running")
        with self.assertRaises(TaskError) as other:
            self.core.api._route("kyf.other-test", "POST", ["tasks", task_id, "stop"], {}, {}, {})
        self.assertEqual((other.exception.status, other.exception.code), (404, "not-found"))
        self.assertEqual(self.get(f"/api/task?id={task_id}")["task"]["state"], "running")
        self.assertIn(self.rpc("stop", {"id": task_id}), ("stopping", "stopped"))
        task = self.wait_task(task_id)
        self.assertEqual(task["state"], "stopped")
        self.assertTrue(self.rpc("seen")["long-token"])
        self.assertEqual(self.core.tasks.live, {}, "the execution token is revoked")
        with self.assertRaises(TaskError) as missing:
            self.core.api._route("kyf.api-test", "POST", ["tasks", "no-such-task", "stop"], {}, {}, {})
        self.assertEqual(missing.exception.status, 404)

    def test_picked_files_are_claimed_once_by_their_plugin_before_expiry(self):
        files = self.core.files
        granted = files.grant("kyf.api-test", str(self.picked))
        self.assertTrue(granted["token"].startswith("fg_"))
        self.assertEqual(granted["label"], "技能包.zip")
        claimed = self.rpc("claim", {"token": granted["token"]})
        self.assertEqual(claimed, {"path": str(self.picked), "label": "技能包.zip", "kind": "file"})
        self.assertEqual(self.rpc("claim", {"token": granted["token"]}), {"error": "not-found", "status": 404})
        foreign = files.grant("kyf.other-test", str(self.picked.parent))
        self.assertEqual(self.rpc("claim", {"token": foreign["token"]})["status"], 404)
        self.assertEqual(files.claim("kyf.other-test", foreign["token"])["kind"], "directory")
        expiring = files.grant("kyf.api-test", str(self.picked))
        start = time.monotonic()
        files.clock = lambda: start + GRANT_TTL + 1
        self.assertEqual(self.rpc("claim", {"token": expiring["token"]})["status"], 404)
        files.clock = time.monotonic
        tokens = [files.grant("kyf.api-test", str(self.picked))["token"] for _ in range(20)]
        self.assertEqual(sum(1 for grant in files.grants.values() if grant["plugin"] == "kyf.api-test"), 16)
        self.assertEqual(self.rpc("claim", {"token": tokens[0]})["status"], 404)
        self.assertEqual(self.rpc("claim", {"token": tokens[-1]})["kind"], "file")
        for bad in ("relative/path.zip", str(self.picked) + ".missing"):
            with self.assertRaises(ValueError):
                files.grant("kyf.api-test", bad)
        with self.assertRaises(KeyError):
            files.grant("kyf.unknown", str(self.picked))

    def test_core_calls_never_go_through_a_proxy(self):
        sys.path.insert(0, str(ROOT / "plugin-sdk" / "python"))
        from kyf_plugin.core import CoreClient, CoreError
        # urllib applies environment and Windows system proxies; their bypass lists may miss 127.0.0.1.
        unreachable = {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9", "NO_PROXY": "",
                       "no_proxy": ""}
        with mock.patch.dict(os.environ, unreachable), mock.patch.object(urllib.request, "_opener", None):
            with self.assertRaises(CoreError) as caught:
                CoreClient(self.url + "/plugin-api/v1", "not-a-plugin-token").scope()
        self.assertEqual((caught.exception.status, caught.exception.code), (401, "unauthorized"),
                         "Core itself answered, not the unreachable proxy")

    def test_oversized_rpc_result_becomes_an_error_instead_of_a_dropped_line(self):
        sys.path.insert(0, str(ROOT / "plugin-sdk" / "python"))
        import io
        from kyf_plugin.channel import MAX_LINE_BYTES, Plugin as ChannelPlugin
        plugin = ChannelPlugin(workers=1)
        plugin.method("big")(lambda params, context: "字" * (MAX_LINE_BYTES // 3 + 1))
        output = io.StringIO()
        with mock.patch.object(sys, "stdout", output):
            plugin._rpc({"id": "r1", "method": "big"})
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(lines[-1], {"type": "rpc.result", "id": "r1",
                                     "error": {"code": "response-too-large", "message": "插件返回的内容过大"}})
        plugin.pool.shutdown()

    def test_conflict_is_reported_to_the_host(self):
        first = self.post("/api/tasks", {"plugin": "kyf.api-test", "account": "synthetic-account-1",
                                         "user": FRIEND, "scheme": "default"})["task"]
        listing = self.get("/api/plugins")
        self.assertEqual(listing["plugins"][0]["id"], "kyf.api-test")
        self.wait_task(first["id"])
        tasks = self.get(f"/api/tasks?account=synthetic-account-1&user={FRIEND}")["tasks"]
        self.assertEqual(tasks[0]["id"], first["id"])


if __name__ == "__main__":
    unittest.main()
