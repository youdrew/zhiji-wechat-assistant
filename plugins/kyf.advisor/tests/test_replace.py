"""Regenerating the latest answer and asking the latest question again (synthetic data only)."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from contracts import AdvisorError
from support import FakeModelStore, FakeRuntime, FakeSource, build_service, wait_until

ACCOUNT = "synthetic-account-1"
USER = "user-a"
AGENT = "builtin:advisor"


class NumberedRuntime(FakeRuntime):
    """Each answer is distinct and names its native question, like the engine since 2026-10-15."""

    def __init__(self):
        super().__init__()
        self.count = 0
        self.refuse_revert = False
        self.fail_next = False

    def respond(self, config, request, on_event, cancel_event):
        self.respond_calls.append((config, request))
        if self.fail_next:
            self.fail_next = False
            error = RuntimeError("synthetic provider failure")
            error.code = "provider-error"
            raise error
        if self.refuse_revert and request.get("revertMessageId"):
            self.refuse_revert = False
            error = RuntimeError("native-session-incompatible")
            error.code = "native-session-incompatible"
            raise error
        self.count += 1
        text = f"第{self.count}个回答"
        on_event({"type": "text", "text": text})
        if self.finish_gate is not None:
            self.finish_gate.wait(10)
        return {"text": text, "runtimeSessionId": "sess-1", "userMessageId": f"msg_q{self.count}"}


class ReplaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = FakeSource(account=ACCOUNT)
        self.source.seed_pages(USER, 20)
        self.runtime = NumberedRuntime()
        self.root = Path(temporary.name)
        self.models = FakeModelStore(context_tokens=32768)
        self.service = build_service(self.source, self.models, self.root,
                                     runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)
        self.thread_id = self.service.thread(ACCOUNT, USER, AGENT)["thread"]["id"]

    def ask(self, message, request_id, **options):
        started = self.service.start(ACCOUNT, USER, AGENT, self.thread_id, message, request_id, **options)
        ended = wait_until(lambda: (value if value["run"]["state"] != "running" else None)
                           if (value := self.service.events(ACCOUNT, USER, started["run"]["id"], 0)) else None)
        self.assertIsNotNone(ended, "run did not end")
        wait_until(lambda: not any(thread.is_alive() for thread in self.service.plugin.threads))
        return started["run"]["id"], ended

    def transcript(self):
        thread = self.service.thread(ACCOUNT, USER, AGENT, self.thread_id)["thread"]
        return [(message["role"], message["text"], "supersededBy" in message) for message in thread["messages"]
                if message["role"] in ("user", "assistant")]

    def requests(self):
        # Copies taken at each call: the service reuses one request dict for its rebuild attempt.
        return [request for method, _token, request in self.service.engine.calls if method == "respond"]

    def test_regenerate_replaces_only_the_answer_and_reverts_its_native_turn(self):
        first, _ = self.ask("她为什么不回我", "q-1")
        second, ended = self.ask("她为什么不回我", "q-1-again", regenerate_run_id=first)
        self.assertEqual(ended["run"]["state"], "done")
        self.assertNotEqual(second, first)
        self.assertEqual(self.transcript(), [("user", "她为什么不回我", False), ("assistant", "第1个回答", True),
                                             ("assistant", "第2个回答", False)])
        again = self.requests()[1]
        self.assertEqual((again["runtimeSessionId"], again["revertMessageId"], again["message"]), ("sess-1", "msg_q1", "她为什么不回我"))
        # The list preview and the next question skip the replaced answer.
        rows = self.service.threads(ACCOUNT, USER, AGENT)["threads"]
        self.assertEqual(next(row for row in rows if row["id"] == self.thread_id)["preview"], "第2个回答")
        self.ask("那我该怎么办", "q-2")
        self.assertNotIn("revertMessageId", self.requests()[2])

    def test_edit_replaces_the_question_and_its_answer(self):
        first, _ = self.ask("周末约她吃饭合适吗", "e-1")
        title = self.service.thread(ACCOUNT, USER, AGENT, self.thread_id)["thread"]["title"]
        self.ask("周末约她看电影合适吗", "e-2", edit_run_id=first)
        self.assertEqual(self.transcript(), [("user", "周末约她吃饭合适吗", True), ("assistant", "第1个回答", True),
                                             ("user", "周末约她看电影合适吗", False), ("assistant", "第2个回答", False)])
        self.assertEqual(self.requests()[1]["revertMessageId"], "msg_q1")
        self.assertEqual(self.service.thread(ACCOUNT, USER, AGENT, self.thread_id)["thread"]["title"], title)
        # A stopped turn can be asked again in other words, but there is no answer to regenerate.
        self.runtime.finish_gate = threading.Event()
        started = self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "再问一句", "e-3")
        self.assertTrue(wait_until(lambda: len(self.runtime.respond_calls) == 3))
        self.service.stop(ACCOUNT, USER, started["run"]["id"])
        self.runtime.finish_gate.set()
        wait_until(lambda: not any(thread.is_alive() for thread in self.service.plugin.threads))
        with self.assertRaises(AdvisorError) as caught:
            self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "再问一句", "e-4", regenerate_run_id=started["run"]["id"])
        self.assertEqual(caught.exception.code, "request-conflict")
        self.ask("换个问法", "e-5", edit_run_id=started["run"]["id"])
        self.assertEqual(self.transcript()[-2:], [("user", "换个问法", False), ("assistant", "第4个回答", False)])

    def test_only_the_latest_turn_and_only_while_nothing_runs_or_waits(self):
        first, _ = self.ask("问题一", "l-1")
        second, _ = self.ask("问题二", "l-2")
        for options in ({"regenerate_run_id": first}, {"edit_run_id": first}):
            with self.subTest(options=options), self.assertRaises(AdvisorError) as caught:
                self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "问题一", "l-x-" + next(iter(options)), **options)
            self.assertEqual(caught.exception.code, "request-conflict")
        self.runtime.finish_gate = threading.Event()
        running = self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "问题三", "l-3")
        self.assertTrue(wait_until(lambda: len(self.runtime.respond_calls) == 3))
        with self.assertRaises(AdvisorError) as caught:
            self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "问题二", "l-4", regenerate_run_id=second)
        self.assertEqual(caught.exception.code, "run-active")
        self.runtime.finish_gate.set()
        self.assertEqual(wait_until(lambda: self.service.events(ACCOUNT, USER, running["run"]["id"], 0)["run"]["state"] == "done"), True)
        for options in ({"regenerate_run_id": second, "edit_run_id": second}, {"regenerate_run_id": second, "retry_run_id": second}):
            with self.subTest(options=options), self.assertRaises(AdvisorError) as caught:
                self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "问题二", "l-5", **options)
            self.assertEqual(caught.exception.code, "invalid-request")

    def test_a_turn_without_its_native_question_is_answered_from_a_rebuilt_discussion(self):
        self.ask("问题一", "r-1")
        second, _ = self.ask("问题二", "r-2")
        database = self.service.stores.account(ACCOUNT)
        metadata = json.loads(database.run(second)["session_metadata_json"])
        metadata.pop("native_message_id")
        with database.lock:
            database._connect().execute("UPDATE runs SET session_metadata_json=? WHERE id=?", (json.dumps(metadata), second))
            database._connect().commit()
        self.ask("问题二", "r-3", regenerate_run_id=second)
        rebuilt = self.requests()[2]
        self.assertIsNone(rebuilt["runtimeSessionId"])
        self.assertNotIn("revertMessageId", rebuilt)
        self.assertIn("问题一", rebuilt["recoveryText"])
        self.assertIn("第1个回答", rebuilt["recoveryText"])
        self.assertNotIn("第2个回答", rebuilt["recoveryText"], "the replaced answer is not part of the discussion")

    def test_a_native_session_that_lost_the_turn_rebuilds_once(self):
        self.ask("问题一", "n-1")
        second, _ = self.ask("问题二", "n-2")
        self.runtime.refuse_revert = True
        _run, ended = self.ask("问题二", "n-3", regenerate_run_id=second)
        self.assertEqual(ended["run"]["state"], "done")
        refused, rebuilt = self.requests()[2], self.requests()[3]
        self.assertEqual(refused["revertMessageId"], "msg_q2")
        self.assertIsNone(rebuilt["runtimeSessionId"])
        self.assertNotIn("revertMessageId", rebuilt)
        self.assertNotIn("第2个回答", rebuilt["recoveryText"])

    def test_the_same_request_returns_the_same_replacement(self):
        first, _ = self.ask("问题一", "i-1")
        self.runtime.finish_gate = threading.Event()
        started = self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "问题一", "i-2", regenerate_run_id=first)
        again = self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "问题一", "i-2", regenerate_run_id=first)
        self.assertEqual(again["run"]["id"], started["run"]["id"])
        self.runtime.finish_gate.set()
        wait_until(lambda: not any(thread.is_alive() for thread in self.service.plugin.threads))
        self.assertEqual(len(self.runtime.respond_calls), 2)

    def test_consecutive_regenerations_revert_the_newest_native_turn_and_rebuild_only_active_versions(self):
        first, _ = self.ask("问题一", "chain-1")
        second, _ = self.ask("问题一", "chain-2", regenerate_run_id=first)
        third, _ = self.ask("问题一", "chain-3", regenerate_run_id=second)
        self.assertEqual([request.get("revertMessageId") for request in self.requests()], [None, "msg_q1", "msg_q2"])
        database = self.service.stores.account(ACCOUNT)
        self.assertEqual(database.run(first)["superseded_by"], second)
        self.assertEqual(database.run(second)["superseded_by"], third)
        database.set_runtime_session(self.thread_id, None)
        _run, ended = self.ask("新问题", "chain-4")
        self.assertEqual(ended["run"]["state"], "done")
        restored = self.requests()[-1]["recoveryText"]
        self.assertIn("第3个回答", restored)
        self.assertNotIn("第1个回答", restored)
        self.assertNotIn("第2个回答", restored)

    def test_failed_regeneration_can_retry_without_restoring_the_replaced_done_answer(self):
        first, _ = self.ask("原问题", "retry-1")
        self.runtime.fail_next = True
        failed, ended = self.ask("原问题", "retry-2", regenerate_run_id=first)
        self.assertEqual(ended["run"]["state"], "error")
        database = self.service.stores.account(ACCOUNT)
        self.assertEqual(database.session_run_metadata(failed)["pending_revert"]["messageId"], "msg_q1")
        retry, ended = self.ask("原问题", "retry-3", retry_run_id=failed)
        self.assertEqual(ended["run"]["state"], "done")
        self.assertEqual(self.requests()[-1]["revertMessageId"], "msg_q1")
        self.assertIsNone(database.session_run_metadata(retry)["pending_revert"])
        self.assertIsNone(database.thread(self.thread_id)["pending_replacement"])
        replay = self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "原问题", "retry-3", retry_run_id=failed)
        self.assertEqual(replay["run"]["id"], retry)

    def test_task_start_failure_preserves_edit_revert_across_service_restart(self):
        first, _ = self.ask("修改前问题", "restart-1")
        with patch.object(self.service.plugin, "start_task", side_effect=RuntimeError("synthetic task failure")):
            with self.assertRaises(AdvisorError):
                self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "修改后问题", "restart-2", edit_run_id=first)
        database = self.service.stores.account(ACCOUNT)
        failed = database.latest_run(self.thread_id)["id"]
        self.assertEqual(database.session_run_metadata(failed)["pending_revert"],
                         {"messageId": "msg_q1", "runtimeSessionId": "sess-1"})
        self.service.shutdown()
        self.service = build_service(self.source, self.models, self.root, runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)
        _run, ended = self.ask("修改后问题", "restart-3", retry_run_id=failed)
        self.assertEqual(ended["run"]["state"], "done")
        self.assertEqual(self.requests()[-1]["revertMessageId"], "msg_q1")
        self.assertEqual([item for item in self.transcript() if not item[2]],
                         [("user", "修改后问题", False), ("assistant", "第2个回答", False)])

    def test_a_different_question_after_failed_edit_rebuilds_before_using_native_history(self):
        first, _ = self.ask("修改前问题", "follow-1")
        self.runtime.fail_next = True
        failed, ended = self.ask("修改后问题", "follow-2", edit_run_id=first)
        self.assertEqual(ended["run"]["state"], "error")
        with self.assertRaises(AdvisorError) as caught:
            self.service.compact_thread(ACCOUNT, USER, AGENT, self.thread_id, "failed-edit-compact")
        self.assertEqual(caught.exception.code, "rebuild-required")
        _run, ended = self.ask("接着聊", "follow-3")
        self.assertEqual(ended["run"]["state"], "done")
        self.assertIsNone(self.requests()[-1]["runtimeSessionId"])
        restored = self.requests()[-1]["recoveryText"]
        self.assertIn("修改后问题", restored)
        self.assertNotIn("修改前问题", restored)
        self.assertNotIn("第1个回答", restored)

    def test_paused_pending_queue_blocks_replacement_without_changing_history(self):
        first, _ = self.ask("原问题", "queue-1")
        self.service.queue_pause(ACCOUNT, USER, AGENT, self.thread_id)
        self.service.queue_enqueue(ACCOUNT, USER, AGENT, self.thread_id, "排队问题", "queue-next")
        before = self.transcript()
        for options in ({"edit_run_id": first}, {"regenerate_run_id": first}):
            with self.assertRaises(AdvisorError) as caught:
                self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "原问题", "queue-replace", **options)
            self.assertEqual(caught.exception.code, "run-active")
        self.assertEqual(self.transcript(), before)

    def test_legacy_unlinked_replaced_answer_does_not_make_recovery_report_missing_history(self):
        first, _ = self.ask("旧版问题", "legacy-1")
        database = self.service.stores.account(ACCOUNT)
        with database.lock, database._connect() as connection:
            connection.execute("UPDATE messages SET run_id=NULL WHERE role='assistant' AND thread_id=?", (self.thread_id,))
        self.ask("旧版问题", "legacy-2", regenerate_run_id=first)
        database.set_runtime_session(self.thread_id, None)
        _run, ended = self.ask("新问题", "legacy-3")
        self.assertEqual(ended["run"]["state"], "done")
        self.assertIn("第2个回答", self.requests()[-1]["recoveryText"])
        self.assertNotIn("第1个回答", self.requests()[-1]["recoveryText"])

    def test_replacement_request_identity_and_original_question_are_checked(self):
        first, _ = self.ask("原问题", "identity-1")
        with self.assertRaises(AdvisorError):
            self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "另一个问题", "identity-x", regenerate_run_id=first)
        second, _ = self.ask("原问题", "identity-2", regenerate_run_id=first)
        with self.assertRaises(AdvisorError):
            self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "原问题", "identity-2", edit_run_id=first)
        with self.assertRaises(AdvisorError):
            self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "原问题", "identity-3", regenerate_run_id=first)
        third, _ = self.ask("编辑后的问题", "identity-4", edit_run_id=second)
        with self.assertRaises(AdvisorError):
            self.service.start(ACCOUNT, USER, AGENT, self.thread_id, "原问题", "identity-5", retry_run_id=first)
        self.assertNotEqual(third, second)


if __name__ == "__main__":
    unittest.main()
