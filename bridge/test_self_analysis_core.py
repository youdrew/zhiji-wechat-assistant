"""Private-chat discovery and the narrowly scoped expression service, synthetic data only."""
import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin-sdk" / "python"))

import core_http
from backend_contracts import AccountChangedError
from core_account_db import AccountStores
from core_history import DIRECTORY_TTL, HistoryService
from core_human import HumanService
from core_plugin_process import PluginError, PluginUnavailable
from core_projection import ContextProjection
from core_tasks import TaskError
from plugin_api import ApiError, PluginAPI
from test_support import FakeSource, make_message
from wechat_source import WeChatSource

ACCOUNT, FRIEND = "synthetic-account-1", "wxid_friend"


class ConversationDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = {}
        self.add_shard(1, ["visible", "hidden", "history-only", "group@chatroom", "gh_public", "me", "filehelper"],
                       empty=["no-messages"], orphan=True)
        self.add_shard(2, ["hidden", "other@openim"])
        files = [(name, str(path), 0) for name, path in self.paths.items()]
        self.db = SimpleNamespace(account=ACCOUNT, messages_ready=True, _db_files=files,
            _open=lambda rel: sqlite3.connect(self.paths[rel]))
        self.source = WeChatSource(factory=lambda: self.db)
        self.source._ordinary_db = lambda: self.db
        self.source._db = lambda **_kwargs: self.db
        self.source.self_user = lambda _db: "me"
        self.source._contacts = lambda _db: {"hidden": {"name": "Hidden fixture", "avatar": ""}}

    def add_shard(self, number, users, empty=(), orphan=False):
        path = Path(self.temp.name) / f"shard-{number}.db"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
            for user in [*users, *empty]:
                connection.execute("INSERT INTO Name2Id VALUES(?)", (user,))
                table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
                connection.execute(f'CREATE TABLE "{table}"(local_id INTEGER)')
                if user not in empty:
                    connection.execute(f'INSERT INTO "{table}" VALUES(1)')
            if orphan:
                connection.execute('CREATE TABLE "Msg_00000000000000000000000000000000"(local_id INTEGER)')
                connection.execute('INSERT INTO "Msg_00000000000000000000000000000000" VALUES(1)')
        self.paths[f"message/message_{number}.db"] = path

    def test_inventory_uses_retained_tables_not_visible_or_selected_sessions(self):
        self.source.sessions = Mock(side_effect=AssertionError("session UI must not bound discovery"))
        result = self.source.history_conversations()
        self.assertEqual([(item["user"], item["isGroup"]) for item in result["conversations"]],
                         [("group@chatroom", True), ("hidden", False), ("history-only", False),
                          ("other@openim", False), ("visible", False)])
        self.assertEqual(result["conversations"][1]["name"], "Hidden fixture")
        self.assertEqual(result["account"], ACCOUNT)
        self.assertEqual(result["coverage"], {"state": "partial", "readableShards": 2,
            "failedShards": [], "unresolvedTables": 1})
        self.source.sessions.assert_not_called()

    def test_broken_shard_is_visible_and_readable_shards_survive(self):
        self.db._db_files.append(("message/message_3.db", "unused", 0))
        result = self.source.history_conversations()
        self.assertEqual(result["coverage"]["failedShards"], [{"shard": "message/message_3.db", "error": "unreadable"}])
        self.assertTrue(result["conversations"])

    def test_directory_latest_matches_history_highwater_across_shards(self):
        root = Path(self.temp.name) / "named"
        root.mkdir()
        rows = {1: {"both": [(1, 10), (2, 30)], "one": [(1, 5)]}, 2: {"both": [(7, 30), (3, 20)]}}
        paths = {}
        for number, users in rows.items():
            path = root / f"message__message_{number}.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
                for user, records in users.items():
                    connection.execute("INSERT INTO Name2Id VALUES(?)", (user,))
                    table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
                    connection.execute(f'CREATE TABLE "{table}"(local_id INTEGER PRIMARY KEY,local_type INTEGER,'
                                       "real_sender_id INTEGER,create_time INTEGER,message_content TEXT,"
                                       "compress_content BLOB,server_id INTEGER,sort_seq INTEGER)")
                    connection.executemany(f'INSERT INTO "{table}" VALUES(?,1,1,1700000000,?,NULL,?,?)',
                                           [(local, "synthetic", local, seq) for local, seq in records])
            paths[f"message/message_{number}.db"] = path
        def conns(user):
            table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
            return [(sqlite3.connect(path), table) for number, path in sorted(paths.items())
                    if user in rows[int(number.split("_")[-1].split(".")[0])]]
        self.db._db_files = [(name, str(path), 0) for name, path in paths.items()]
        self.db._open = lambda rel: sqlite3.connect(paths[rel])
        self.db._msg_conns = conns
        result = {row["user"]: row["latest"] for row in self.source.history_conversations()["conversations"]}
        # Equal sort_seq is ordered by shard name, then local id, exactly like history pages.
        self.assertEqual(result["both"], [30, "message__message_2.db", 7])
        self.assertEqual(result["one"], [5, "message__message_1.db", 1])
        for user in ("both", "one"):
            self.assertEqual(result[user], list(self.source.history_highwater(user)))
        self.db._db_files.append(("message/message_3.db", "unused", 0))
        self.assertTrue(all(row["latest"] is None for row in self.source.history_conversations()["conversations"]),
                        "an unreadable shard may hold a newer row")

    def test_account_replacement_cannot_return_old_directory(self):
        self.source._db = lambda **_kwargs: object()
        with self.assertRaises(AccountChangedError):
            self.source.history_conversations()


class HistoryContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stores = AccountStores(Path(self.temp.name))
        self.addCleanup(self.stores.close)
        self.source = FakeSource()
        self.source.add_messages(FRIEND, [make_message(index, local_id=index) for index in range(1, 4)])
        self.context = ContextProjection(self.stores, self.source)
        self.addCleanup(self.context.close)
        self.history = HistoryService(self.stores, self.context)

    def ready(self, rebuild=False):
        self.history.prepare(ACCOUNT, FRIEND, refresh=True, rebuild=rebuild)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = self.history.status(ACCOUNT, FRIEND)
            if status["state"] == "ready":
                return status
            time.sleep(0.01)
        self.fail("synthetic history did not become ready")

    def test_directory_pages_are_stable_and_account_bound(self):
        directory = [{"user": user, "name": user, "isGroup": False} for user in ("a", "b", "c")]
        self.source.history_conversations = lambda: {"account": ACCOUNT, "conversations": list(directory),
            "coverage": {"state": "complete", "readableShards": 1, "failedShards": [], "unresolvedTables": 0}}
        first = self.history.conversations(ACCOUNT, limit=1)
        directory[:] = [{"user": "new", "name": "New fixture", "isGroup": False}]
        second = self.history.conversations(ACCOUNT, first["next"], limit=1)
        third = self.history.conversations(ACCOUNT, second["next"], limit=1)
        self.assertEqual([first["conversations"][0]["user"], second["conversations"][0]["user"],
                          third["conversations"][0]["user"]], ["a", "b", "c"])
        self.assertEqual(first["revision"], third["revision"])
        self.assertIsNone(third["next"])
        self.assertEqual(self.history.conversations(ACCOUNT)["total"], 1)
        self.source.account = "another-account"
        with self.assertRaisesRegex(ValueError, "directory-expired"):
            self.history.conversations("another-account", first["next"])

    def test_directory_latest_and_status_highwater_compare_equal_for_an_unchanged_chat(self):
        status = self.ready()
        latest = list(self.source.history_highwater(FRIEND))
        self.source.history_conversations = lambda: {"account": ACCOUNT, "conversations": [
            {"user": FRIEND, "name": "Fixture", "isGroup": False, "latest": latest},
            {"user": "unknown", "name": "Fixture", "isGroup": False, "latest": None}],
            "coverage": {"state": "complete", "readableShards": 1, "failedShards": [], "unresolvedTables": 0}}
        rows = {row["user"]: row["latest"] for row in self.history.conversations(ACCOUNT)["conversations"]}
        self.assertIsInstance(status["highwater"], str)
        self.assertEqual(rows[FRIEND], status["highwater"])
        self.assertIsNone(rows["unknown"])
        batched = self.history.status_many(ACCOUNT, [FRIEND])["results"][0]
        self.assertEqual((batched["highwater"], batched["epoch"], batched["watermark"]),
                         (status["highwater"], status["epoch"], status["watermark"]))
        self.source.add_messages(FRIEND, [make_message(9, local_id=9)])
        self.assertNotEqual(list(self.source.history_highwater(FRIEND)), latest)

    def test_directory_expiry_and_unsupported_are_not_empty_successes(self):
        unsupported = self.history.conversations(ACCOUNT)
        self.assertEqual(unsupported["coverage"]["state"], "unsupported")
        self.source.history_conversations = lambda: {"account": ACCOUNT, "conversations": [
            {"user": "a"}, {"user": "b"}], "coverage": {"state": "partial"}}
        first = self.history.conversations(ACCOUNT, limit=1)
        with patch("core_history.time.monotonic", return_value=time.monotonic() + DIRECTORY_TTL + 1):
            with self.assertRaisesRegex(ValueError, "directory-expired"):
                self.history.conversations(ACCOUNT, first["next"])

    def test_fresh_checks_find_edits_deletions_and_rebuild_removes_stale_projection(self):
        before = self.ready()
        page = self.history.messages(ACCOUNT, FRIEND)
        self.assertEqual((page["epoch"], page["sourceFingerprint"]), (before["epoch"], before["sourceFingerprint"]))
        refs = [{"id": item["id"], "hash": item["hash"]} for item in page["messages"]]
        self.assertEqual([item["state"] for item in self.history.verify(ACCOUNT, FRIEND, refs, fresh=True)["results"]],
                         ["same", "same", "same"])
        self.source.users[FRIEND][0]["text"] = "Synthetic edited original"
        del self.source.users[FRIEND][1]
        self.assertEqual([item["state"] for item in self.history.verify(ACCOUNT, FRIEND, refs)["results"]],
                         ["same", "same", "same"], "ordinary verify checks the projection only")
        self.assertEqual([item["state"] for item in self.history.verify(ACCOUNT, FRIEND, refs, fresh=True)["results"]],
                         ["changed", "missing", "same"])
        after = self.ready(rebuild=True)
        self.assertNotEqual(before["epoch"], after["epoch"])
        current = self.history.messages(ACCOUNT, FRIEND)["messages"]
        self.assertEqual(len(current), 2)
        self.assertEqual(current[0]["text"], "Synthetic edited original")


class ExpressionServiceTests(unittest.TestCase):
    def setUp(self):
        self.ready = threading.Event()
        self.ready.set()
        self.result = {"available": True, "revision": "r1", "generation": 1, "profileVersion": 0,
                       "sourceFingerprint": "fixture-source", "sources": [], "text": "Synthetic style"}
        self.process = SimpleNamespace(ready=self.ready, call=Mock(return_value=self.result))
        self.binding = {"account": ACCOUNT, "user": FRIEND}
        self.core = SimpleNamespace(
            scope=Mock(return_value={"account": ACCOUNT, "ready": True}), ensure_account=Mock(),
            require_plugin=Mock(), tasks=SimpleNamespace(verify=Mock(return_value=self.binding)),
            registry=SimpleNamespace(get=Mock(return_value={"id": "kyf.self-analysis"}), enabled=Mock(return_value=True)),
            supervisor=SimpleNamespace(plugin_for_token=Mock(return_value="kyf.message-insights"),
                                       ensure=Mock(return_value=self.process)),
            history=SimpleNamespace(rows=Mock(return_value={"messages": [{"id": "m1", "side": "other",
                "group": False, "time": 1700000000000, "seq": 9}]}),
                context=SimpleNamespace(source_scope=Mock(return_value="fixture-source"))))
        self.api = PluginAPI(self.core)
        self.headers = {"Authorization": "Bearer synthetic-process", "X-KYF-Execution": "synthetic-execution"}
        self.body = {"account": ACCOUNT, "user": FRIEND, "messageId": "m1", "query": "Synthetic query"}

    def call(self, **changes):
        return self.api.handle("POST", "/plugin-api/v1/services/self-context", {}, self.headers,
                               {**self.body, **changes})[1]

    def test_fixed_provider_and_scope_are_derived_from_authenticated_task_and_target(self):
        self.assertEqual(self.call(), self.result)
        method, params, context = self.process.call.call_args.args
        self.assertEqual(method, "self.context")
        self.assertEqual(context, {"account": ACCOUNT, "user": FRIEND})
        self.assertEqual((params["beforeTimeMs"], params["beforeSeq"], params["sourceFingerprint"]),
                         (1700000000000, 9, "fixture-source"))
        self.assertLessEqual(self.process.call.call_args.kwargs["timeout"], 6)
        self.core.supervisor.ensure.assert_called_once_with("kyf.self-analysis", wait=False)

    def test_no_generic_rpc_or_other_plugin_can_use_the_service(self):
        with self.assertRaises(ApiError):
            self.call(plugin="kyf.advisor", method="run.start")
        self.core.supervisor.plugin_for_token.return_value = "kyf.advisor"
        with self.assertRaises(ApiError) as caught:
            self.call()
        self.assertEqual(caught.exception.status, 403)
        self.process.call.assert_not_called()

    def test_invalid_auth_execution_scope_and_target_fail_without_provider_call(self):
        self.core.supervisor.plugin_for_token.return_value = None
        with self.assertRaises(ApiError) as caught:
            self.call()
        self.assertEqual(caught.exception.status, 401)
        self.core.supervisor.plugin_for_token.return_value = "kyf.message-insights"
        self.core.tasks.verify.side_effect = TaskError("execution-revoked", "Synthetic revoked", 409)
        with self.assertRaises(ApiError) as caught:
            self.call()
        self.assertEqual(caught.exception.code, "execution-revoked")
        self.core.tasks.verify.side_effect = None
        with self.assertRaises(ApiError) as caught:
            self.call(user="another-friend")
        self.assertEqual(caught.exception.code, "scope-mismatch")
        with self.assertRaises(ApiError) as caught:
            self.call(account="another-account")
        self.assertEqual(caught.exception.code, "account-changed")
        self.core.history.rows.return_value = {"messages": []}
        with self.assertRaises(ApiError):
            self.call()
        self.process.call.assert_not_called()

    def test_provider_disabled_slow_crashed_and_oversized_results_fall_back(self):
        self.core.registry.enabled.return_value = False
        self.assertFalse(self.call()["available"])
        self.process.call.assert_not_called()
        self.core.registry.enabled.return_value = True
        self.ready.clear()
        self.assertEqual(self.call()["reason"], "service-warming")
        self.ready.set()
        for error in (PluginUnavailable("plugin-timeout"), PluginError("fixture-failed", "Synthetic failure")):
            self.process.call.side_effect = error
            self.assertFalse(self.call()["available"])
        self.process.call.side_effect = None
        self.process.call.return_value = {**self.result, "text": "x" * 12001}
        self.assertEqual(self.call()["reason"], "invalid-result")
        self.process.call.return_value = {**self.result, "sourceFingerprint": "another-source"}
        self.assertEqual(self.call()["reason"], "source-changed")

    def test_late_results_cannot_survive_stop_or_account_switch(self):
        def stopped(*_args, **_kwargs):
            self.core.tasks.verify.side_effect = TaskError("execution-revoked", "Stopped", 409)
            return self.result
        self.process.call.side_effect = stopped
        with self.assertRaises(ApiError) as caught:
            self.call()
        self.assertEqual(caught.exception.code, "execution-revoked")
        self.core.tasks.verify.side_effect = None
        def switched(*_args, **_kwargs):
            self.core.scope.return_value = {"account": "another-account", "ready": True}
            return self.result
        self.process.call.side_effect = switched
        with self.assertRaises(ApiError) as caught:
            self.call()
        self.assertEqual(caught.exception.code, "account-changed")

    def test_precise_validation_refs_are_bounded_and_cannot_override_source_scope(self):
        self.call(validateOnly=True, generation=2, profileVersion=3, sources=[{"id": "sample", "hash": "digest"}])
        params = self.process.call.call_args.args[1]
        self.assertEqual((params["validateOnly"], params["generation"], params["profileVersion"]), (True, 2, 3))
        for bad in ([{"id": "x", "hash": "y", "account": "other"}], [{"id": "x", "hash": "y"}] * 11):
            with self.assertRaises(ApiError):
                self.call(validateOnly=True, generation=2, profileVersion=3, sources=bad)
        with self.assertRaises(ApiError):
            self.call(sourceFingerprint="another-source")


class SelfProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.stores = AccountStores(Path(self.temp.name))
        self.addCleanup(self.stores.close)
        self.changed = Mock()
        self.human = HumanService(self.stores, on_change=self.changed)
        self.core = SimpleNamespace(scope=Mock(return_value={"account": ACCOUNT, "ready": True}),
            tasks=SimpleNamespace(account_blocked=Mock(return_value=False)), ensure_account=Mock(), human=self.human)

    def test_account_editor_uses_core_owned_version_and_emits_account_wide_change(self):
        first = core_http.get(self.core, "/api/account/self-profile", {"account": ACCOUNT})
        request = {"account": ACCOUNT, "background": "Synthetic background", "style": "Synthetic style",
                   "expectedVersion": first["version"]}
        saved = core_http.post(self.core, "/api/account/self-profile", request)
        self.assertEqual(saved["version"], first["version"] + 1)
        self.assertEqual(self.human.read(ACCOUNT, FRIEND)["self"]["background"], request["background"])
        self.changed.assert_called_once_with(ACCOUNT, None, saved["version"])
        with self.assertRaises(core_http.HostError) as caught:
            core_http.post(self.core, "/api/account/self-profile", request)
        self.assertEqual(caught.exception.code, "request-conflict")
        self.assertEqual(self.human.self_profile(ACCOUNT), saved)

    def test_wrong_or_unavailable_account_cannot_read_or_mutate_profile(self):
        for account in ("another-account", ""):
            with self.assertRaises(core_http.HostError):
                core_http.get(self.core, "/api/account/self-profile", {"account": account})
        self.core.tasks.account_blocked.return_value = True
        with self.assertRaises(core_http.HostError) as caught:
            core_http.get(self.core, "/api/account/self-profile", {"account": ACCOUNT})
        self.assertEqual(caught.exception.code, "account-paused")
        self.core.tasks.account_blocked.return_value = False
        self.core.scope.return_value = {"account": None, "ready": False}
        with self.assertRaises(core_http.HostError):
            core_http.post(self.core, "/api/account/self-profile", {"account": ACCOUNT,
                "background": "x", "style": "x", "expectedVersion": 0})
        self.assertEqual(self.human.self_profile(ACCOUNT)["version"], 0)


if __name__ == "__main__":
    unittest.main()
