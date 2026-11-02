"""Public plugin RPC dispatch with synthetic Core responses and task engines."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from kyf_plugin import CoreError, RpcError
from support import build_service, FakeModelStore, FakeRuntime, FakeSource, wait_until

class AdvisorRpcTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = FakeSource()
        self.source.seed_pages("synthetic-chat", 600)
        self.runtime = FakeRuntime()
        self.service = build_service(self.source, FakeModelStore(), self.root, runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)
        self.scope = {"account": self.source.account, "user": "synthetic-chat"}
        self.registry = patch.dict(main.state, {"service": self.service}, clear=True)
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def call(self, method, params=None, context=None):
        return main.dispatch(method, params or {}, self.scope if context is None else context)

    def test_sticker_start_takes_only_request_id_and_host_scope(self):
        with patch.object(self.service.stickers, "start_requested", return_value={"state": "no-work", "taskId": None}) as start:
            self.assertEqual(self.call("stickers.start", {"completionRequestId": "synthetic-media"})["state"], "no-work")
            start.assert_called_once_with(self.scope["account"], self.scope["user"], "synthetic-media")
            for extra in ({"prompt": "forbidden"}, {"kind": "interactive"}, {"purpose": "reply"}, {"user": "spoofed"}):
                with self.assertRaises(RpcError):
                    self.call("stickers.start", {"completionRequestId": "synthetic-media", **extra})
            with self.assertRaises(RpcError):
                self.call("stickers.start", {})
        self.assertFalse(self.runtime.respond_calls)

    def start(self, **extra):
        # Like the panel, a question is sent into the selected discussion.
        thread_id = extra.pop("threadId", None) or self.call("thread.get", {"agentId": "builtin:advisor"})["thread"]["id"]
        return self.call("run.start", {"agentId": "builtin:advisor", "message": "请给我一条建议",
                                       "requestId": "synthetic-rpc-run", "threadId": thread_id, **extra})

    def finished(self, run_id):
        result = wait_until(lambda: (value if value["run"]["state"] != "running" else None)
                           if (value := self.call("run.events", {"runId": run_id})) else None)
        self.assertIsNotNone(result)
        return result

    def test_manual_rpc_cannot_spoof_auto_run_or_replace_history(self):
        for extra in ({"trigger": "auto-reply"}, {"messages": [{"text": "untrusted replacement"}]},
                      {"apiKey": "synthetic-not-a-real-key"}):
            with self.subTest(extra=extra), self.assertRaises(RpcError) as caught:
                self.start(**extra)
            self.assertEqual(caught.exception.code, "invalid-request")
        self.assertEqual(self.runtime.respond_calls, [])

    def test_latest_turn_replacement_uses_scoped_rpc_and_returns_retained_versions(self):
        first = self.start()
        self.assertEqual(self.finished(first["run"]["id"])["run"]["state"], "done")
        self.assertTrue(wait_until(lambda: not any(thread.is_alive() for thread in self.service.plugin.threads)))
        scope = {"agentId": "builtin:advisor", "threadId": first["threadId"]}
        again = self.call("run.start", {**scope, "message": "请给我一条建议", "requestId": "rpc-regenerate",
                                        "regenerateRunId": first["run"]["id"]})
        self.assertEqual(self.finished(again["run"]["id"])["run"]["state"], "done")
        self.assertTrue(wait_until(lambda: not any(thread.is_alive() for thread in self.service.plugin.threads)))
        edited = self.call("run.start", {**scope, "message": "请换一个角度建议", "requestId": "rpc-edit",
                                         "editRunId": again["run"]["id"]})
        self.assertEqual(self.finished(edited["run"]["id"])["run"]["state"], "done")
        messages = self.call("thread.get", scope)["thread"]["messages"]
        self.assertTrue(any(item.get("supersededBy") and item["role"] == "user" for item in messages))
        self.assertEqual([item["text"] for item in messages if item["role"] == "user" and not item.get("supersededBy")],
                         ["请换一个角度建议"])
        self.assertTrue(self.service.plugin.tasks[-1].task["params"]["notify"])
        with self.assertRaises(RpcError) as caught:
            self.call("run.start", {**scope, "message": "冲突", "requestId": "rpc-conflict",
                                     "regenerateRunId": edited["run"]["id"], "editRunId": edited["run"]["id"]})
        self.assertEqual(caught.exception.code, "invalid-request")

    def test_picker_grant_and_idempotent_commit_return_complete_catalog(self):
        folder = self.root / "synthetic-skill"
        folder.mkdir()
        (folder / "SKILL.md").write_text("---\nname: RPC assistant\ndescription: Synthetic\n---\nUse only supplied context.", encoding="utf-8")
        with self.assertRaises(RpcError):
            self.call("import.preview", {"source": {"kind": "local", "path": str(folder)}})
        grant = self.source.issue_grant(folder)
        response = self.call("import.preview", {"source": {"kind": "local", **grant}})
        with self.assertRaises(RpcError):
            self.call("import.preview", {"source": {"kind": "local", **grant}})
        payload = {"previewId": response["preview"]["id"], "requestId": "synthetic-import", "agent": {}}
        first, second = self.call("import.commit", payload), self.call("import.commit", payload)
        self.assertEqual(first["importedAgentId"], second["importedAgentId"])
        self.assertEqual(len(first["agents"]), 4)
        self.assertEqual(self.runtime.respond_calls, [])

    def test_first_run_waits_for_managed_history_and_uses_task_execution(self):
        self.assertTrue(self.call("catalog.get")["permissions"]["readOnly"])
        started = self.start()
        terminal = self.finished(started["run"]["id"])
        self.assertEqual(terminal["run"]["state"], "done", terminal)
        self.assertEqual(len(self.runtime.respond_calls), 1)
        request = self.runtime.respond_calls[0][1]
        self.assertIn("消息内容 1 ", request["contextFileText"] + "\n".join(item["text"] for item in self.runtime.compact_calls))
        self.assertIn("消息内容 600 ", request["contextFileText"])
        self.assertNotIn("context", request)
        self.assertNotIn("apiKey", json.dumps(terminal))
        task = self.service.plugin.tasks[0]
        self.assertEqual(task.task["kind"], "interactive")
        self.assertEqual(task.task["title"], "聊天助手回答")
        self.assertTrue(all(token == task.token for _, token, _ in self.service.engine.calls))
        self.assertIs(request["thinking"], True)

    def test_thinking_mode_is_boolean_and_reaches_every_model_call(self):
        with self.assertRaises(RpcError):
            self.start(thinking="off")
        self.assertEqual(self.runtime.respond_calls, [])
        terminal = self.finished(self.start(thinking=False)["run"]["id"])
        self.assertEqual(terminal["run"]["state"], "done", terminal)
        self.assertIs(self.runtime.respond_calls[0][1]["thinking"], False)
        self.assertTrue(self.runtime.compact_calls)
        self.assertTrue(all(call["thinking"] is False for call in self.runtime.compact_calls))

    def test_task_start_failure_is_a_clear_error_and_never_leaves_a_running_turn(self):
        thread = self.call("thread.get", {"agentId": "builtin:advisor"})["thread"]
        def refuse(*args, **kwargs):
            raise CoreError(409, "account-cleared", "synthetic refusal")
        with patch.object(self.service.plugin, "start_task", refuse), self.assertRaises(RpcError) as caught:
            self.start(threadId=thread["id"])
        self.assertEqual(caught.exception.code, "task-unavailable")
        read = self.call("thread.get", {"agentId": "builtin:advisor", "threadId": thread["id"]})
        self.assertEqual(read["run"]["state"], "error")
        self.assertEqual(self.runtime.respond_calls, [])

    def test_required_identifiers_keep_the_former_http_validation(self):
        thread_id = self.call("thread.get", {"agentId": "builtin:advisor"})["thread"]["id"]
        scope = {"agentId": "builtin:advisor", "threadId": thread_id}
        for method, params in (("run.start", {"agentId": "builtin:advisor", "message": "问题", "requestId": "synthetic-no-thread"}),
                               ("run.start", {**scope, "message": "没有请求编号的问题"}),
                               ("thread.new", {"agentId": "builtin:advisor"}),
                               ("thread.rename", {**scope, "title": "没有请求编号"}),
                               ("thread.delete", scope), ("thread.compact", scope),
                               ("draft.save", {**scope, "text": "草稿"}),
                               ("import.commit", {"previewId": "imp_" + "0" * 32, "agent": {}}),
                               ("catalog.enableAgent", {"id": "builtin:advisor"}),
                               ("operation.get", {"threadId": "thread:synthetic"}),
                               ("operation.get", {"threadId": "thread:synthetic", "operationId": "op_x", "requestId": "req-x"})):
            with self.subTest(method=method, params=params), self.assertRaises(RpcError) as caught:
                self.call(method, params)
            self.assertEqual(caught.exception.code, "invalid-request")
        self.assertEqual(self.service.plugin.tasks, [])

    def test_mentions_join_through_rpc_and_are_removed_by_participant(self):
        self.source.seed_pages("synthetic-other", 3)
        self.source.friend_list = [{"user": "synthetic-chat", "name": "当前好友", "isGroup": False},
                                   {"user": "synthetic-other", "name": "另一位", "isGroup": False}]
        self.assertEqual(self.call("friends.list"), {"friends": [{"user": "synthetic-other", "name": "另一位"}]})
        for mentions in ("synthetic-other", [""], [7]):
            with self.subTest(mentions=mentions), self.assertRaises(RpcError) as caught:
                self.start(mentions=mentions)
            self.assertEqual(caught.exception.code, "invalid-request")
        self.service.engine.participant_root = lambda account: (self.service.stores.directory(account) /
                                                                "engine" / "engine" / "workspace" / "participants")
        terminal = self.finished(self.start(mentions=["synthetic-other"])["run"]["id"])
        self.assertEqual(terminal["thread"]["participants"], [{"user": "synthetic-other", "name": "另一位"}])
        scope = {"agentId": "builtin:advisor", "threadId": terminal["thread"]["id"]}
        with self.assertRaises(RpcError):
            self.call("thread.removeParticipant", scope)
        removed = self.call("thread.removeParticipant", {**scope, "participant": "synthetic-other"})
        self.assertEqual(removed["thread"]["participants"], [])

    def test_draft_mentions_persist_through_rpc_without_becoming_participants(self):
        self.source.friend_list = [{"user": "synthetic-chat", "name": "当前好友", "isGroup": False},
                                   {"user": "synthetic-other", "name": "同名好友", "isGroup": False},
                                   {"user": "synthetic-third", "name": "同名好友", "isGroup": False}]
        thread = self.call("thread.get", {"agentId": "builtin:advisor"})["thread"]
        self.assertEqual(thread["draft"]["mentions"], [])
        scope = {"agentId": "builtin:advisor", "threadId": thread["id"]}
        selected = [{"user": "synthetic-third", "name": "同名好友"}]
        saved = self.call("draft.save", {**scope, "text": "@同名好友 问题", "version": 0, "mentions": selected})
        self.assertEqual(saved["draft"]["mentions"], selected)
        reread = self.call("thread.get", scope)["thread"]
        self.assertEqual(reread["draft"], saved["draft"])
        self.assertEqual(reread["participants"], [])
        self.assertEqual(self.runtime.respond_calls, [])
        with self.assertRaises(RpcError):
            self.call("draft.save", {**scope, "text": "其他账号", "version": 1,
                                     "mentions": [{"user": "foreign-contact", "name": "同名好友"}]})
        self.assertEqual(self.call("thread.get", scope)["thread"]["draft"], saved["draft"])

    def test_unknown_method_and_malformed_payload_are_rejected(self):
        for method, params, scope in (("execute", {}, self.scope), ("run.start", [], self.scope),
                                     ("run.start", {}, []), ("thread.get", {}, {})):
            with self.assertRaises(RpcError):
                main.dispatch(method, params, scope)

    def test_initial_thread_has_stable_identity_without_model_work(self):
        params = {"agentId": "builtin:advisor"}
        first, second = self.call("thread.get", params), self.call("thread.get", params)
        self.assertTrue(first["thread"]["id"])
        self.assertEqual(first["thread"]["id"], second["thread"]["id"])
        self.assertEqual(first["thread"]["messages"], [])
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertEqual(self.runtime.compact_calls, [])

    def test_host_scope_is_required_and_foreign_account_cannot_read(self):
        with self.assertRaises(RpcError):
            self.call("context.prepare", context={"account": "foreign", "user": "synthetic-chat"})
        with self.assertRaises(RpcError):
            self.call("thread.get", {"agentId": "builtin:advisor"}, context={})

    def test_only_owned_live_run_can_be_stopped_after_account_switch(self):
        self.runtime.pause_after_first_chunk = True
        started = self.start()
        self.assertTrue(wait_until(self.runtime.paused.is_set))
        self.source.account = "another-synthetic-account"
        with self.assertRaises(RpcError) as caught:
            self.call("run.stop", {"runId": started["run"]["id"]}, context={**self.scope, "user": "foreign-chat"})
        self.assertEqual(caught.exception.code, "run-unknown")
        stopped = self.call("run.stop", {"runId": started["run"]["id"]})
        self.assertEqual(stopped["run"]["state"], "stopped")
        self.assertTrue(self.service.plugin.tasks[0].cancelled.is_set())

    def test_entrypoint_and_registered_methods_match_plugin_contract(self):
        manifest = json.loads((Path(__file__).parents[1] / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["runtime"], {"type": "python", "entry": "backend/main.py"})
        self.assertEqual(set(main.METHODS), set(main.METHOD_FIELDS))
        self.assertIn("interactive", main.plugin.task_handlers)
        self.assertTrue(set(main.METHODS) <= set(main.plugin.methods))

    def test_queue_rpc_preserves_options_and_supports_edit_remove_pause_resume(self):
        thread = self.call("thread.get", {"agentId": "builtin:advisor"})["thread"]
        scope = {"agentId": "builtin:advisor", "threadId": thread["id"]}
        self.call("queue.pause", scope)
        payload = {**scope, "message": "排队问题", "requestId": "queued-rpc", "thinking": False, "mentions": []}
        accepted = self.call("queue.enqueue", payload)
        item = accepted["item"]
        edited = self.call("queue.edit", {**scope, "itemId": item["id"], "version": item["version"], "message": "修改问题"})
        self.assertFalse(edited["item"]["thinking"])
        self.assertEqual(self.call("queue.enqueue", payload)["item"]["id"], item["id"])
        self.assertEqual(self.call("thread.get", scope)["thread"]["queue"]["items"][0]["message"], "修改问题")
        extra = self.call("queue.enqueue", {**scope, "message": "移除问题", "requestId": "queued-remove"})["item"]
        self.call("queue.remove", {**scope, "itemId": extra["id"], "version": extra["version"]})
        self.call("queue.resume", scope)
        self.assertTrue(wait_until(lambda: self.service.stores.account(self.source.account).latest_run(thread["id"])))
        run = self.service.stores.account(self.source.account).latest_run(thread["id"])
        self.finished(run["id"])
        self.assertEqual([request["message"] for _, request in self.runtime.respond_calls], ["修改问题"])

    def test_queue_rpc_rejects_injected_scope_dispatch_fields_and_bad_versions(self):
        thread = self.call("thread.get", {"agentId": "builtin:advisor"})["thread"]
        scope = {"agentId": "builtin:advisor", "threadId": thread["id"]}
        self.call("queue.pause", scope)
        payload = {**scope, "message": "排队问题", "requestId": "queued-validation"}
        for extra in ({"account": "other"}, {"user": "other"}, {"runId": "run_foreign"},
                      {"dispatchRequestId": "forged"}, {"thinking": "off"}, {"mentions": [7]}):
            with self.subTest(extra=extra), self.assertRaises(RpcError):
                self.call("queue.enqueue", {**payload, **extra})
        item = self.call("queue.enqueue", payload)["item"]
        for method in ("queue.edit", "queue.remove"):
            with self.assertRaises(RpcError):
                self.call(method, {**scope, "itemId": item["id"], "version": True,
                                  **({"message": "update"} if method == "queue.edit" else {})})
        for method in ("queue.get", "queue.pause", "queue.resume"):
            with self.subTest(method=method), self.assertRaises(RpcError):
                self.call(method, scope, context={**self.scope, "user": "foreign-chat"})


if __name__ == "__main__":
    unittest.main()
