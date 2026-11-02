"""Boundary checks using only temporary stores and injected synthetic engines."""
# Modified by Know Your Friends, 2026-10-10 (Apache-2.0): compatible session reuse and recovery assertions.
import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from contracts import AdvisorError, char_budget, normalize_event
from kyf_plugin import CoreError
from support import TestProjection
from support import build_service
from store import AdvisorPathError
from support import TestStores
from support import FakeModelStore, FakeRuntime, FakeSource, make_message, wait_until

ACCOUNT = "synthetic-account-1"
USER = "user-a"
AGENT = "builtin:advisor"


class RetainedModelStore(FakeModelStore):
    def __init__(self, protocol="chat_completions", mode="local", key="synthetic-key"):
        super().__init__(mode=mode, key=key, context_tokens=32768)
        self.protocol = protocol
        self.model = "synthetic-model"
        self.retained = True



class DelayedRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def respond(self, config, request, on_event, cancel_event):
        self.respond_calls.append((config, request))
        self.entered.set()
        self.release.wait(5)
        on_event({"type": "text", "text": "late synthetic output"})
        return {"text": "late synthetic output", "runtimeSessionId": "native-synthetic"}

    def shutdown_account(self, account):
        super().shutdown_account(account)
        self.release.set()

    def close(self):
        super().close()
        self.release.set()


class ContextBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = FakeSource()
        self.stores = TestStores(self.root)
        self.projection = TestProjection(self.stores, self.source)
        self.addCleanup(self.stores.close)
        self.addCleanup(self.projection.close)

    def import_ready(self, user=USER):
        self.projection.prepare(ACCOUNT, user)
        row = wait_until(lambda: self.projection.snapshot(ACCOUNT, user)
                         if self.projection.snapshot(ACCOUNT, user)["state"] in ("ready", "error") else None)
        self.assertIsNotNone(row)
        return row







    def test_single_giant_message_compacts_all_pieces_inside_exact_budget(self):
        text = "0123456789" * 3000
        self.source.add_messages(USER, [make_message(1, text=text)])
        self.import_ready()
        seen = []
        def compact(piece, key, level, target):
            seen.append(piece)
            self.assertLessEqual(len(piece), 1000)
            return "neutral compressed fact"
        import model_context
        render_calls = []
        original_render = model_context._render
        def track_render(older, recent):
            render_calls.append([key for item in older + recent for key in item["coverage"]])
            return original_render(older, recent)
        with patch("model_context._render", side_effect=track_render):
            rendered = self.projection.build_model_context(ACCOUNT, USER, 1, 700, compact, "fp", compact_budget=1000)
        self.assertLessEqual(len(rendered), 700)
        # The final view preserves every original range through summaries or raw recent pieces.
        coverage = render_calls[-1]
        boundaries = [(int(key.split(":")[1]), int(key.split(":")[2])) for key in coverage]
        self.assertEqual(boundaries[0][0], 0)
        for previous, current in zip(boundaries, boundaries[1:]):
            self.assertEqual(previous[1], current[0])
        self.assertEqual(boundaries[-1][1], len(original_render([], [{"text": model_context._format_line(
            self.projection.messages(ACCOUNT, USER)[0])}]).split("\n", 1)[1]))
        rows = self.stores.account(ACCOUNT)._rows("SELECT coverage_json FROM summaries")
        coverage = [part for row in rows for part in json.loads(row["coverage_json"])]
        self.assertTrue(any(":0:" in key for key in coverage))
        self.assertEqual(self.projection.messages(ACCOUNT, USER)[0]["text"], text)

    def test_nonshrinking_or_oversized_summary_never_discards_history(self):
        self.source.seed_pages(USER, 100, text_size=80)
        self.import_ready()
        original = self.projection.messages(ACCOUNT, USER)
        with self.assertRaises(AdvisorError) as caught:
            self.projection.build_model_context(ACCOUNT, USER, None, 700,
                                                lambda text, _key, _level, _target: text, "fp")
        self.assertEqual(caught.exception.code, "compression-failed")
        self.assertEqual(self.projection.messages(ACCOUNT, USER), original)
        self.assertEqual(self.stores.account(ACCOUNT)._rows("SELECT * FROM summaries"), [])

    def test_byte_budget_preserves_fitting_originals_including_unicode_and_boundary(self):
        from model_context import _format_line, _render
        from media import MEDIA_BOUNDARY
        from stickers import NOTE as STICKER_NOTE
        original = "English " * 200 + "中文" * 100 + "🙂" * 100
        self.source.add_messages(USER, [make_message(1, text=original)])
        self.import_ready()
        row = self.projection.messages(ACCOUNT, USER)[0]
        boundary = MEDIA_BOUNDARY + "\n" + STICKER_NOTE + "\n"
        expected = boundary + _render([], [{"text": _format_line(row)}])
        byte_budget = len(expected.encode("utf-8"))

        def no_request(*_args):
            self.fail("the complete original fits the byte budget, even if the old character allowance does not")

        result = self.projection.build_model_context(
            ACCOUNT, USER, 1, byte_budget // 4, no_request, "fp", budget_bytes=byte_budget,
            media_observations={}, stickers={"synthetic-sticker": {}})
        self.assertEqual(result, expected)
        self.assertEqual(len(result.encode("utf-8")), byte_budget)
        self.assertIn(original, result)

        calls = []

        def compact(*_args):
            calls.append(True)
            return "Neutral facts."

        result = self.projection.build_model_context(
            ACCOUNT, USER, 1, byte_budget // 4, compact, "fp", budget_bytes=byte_budget - 1,
            media_observations={}, stickers={"synthetic-sticker": {}})
        self.assertEqual(len(calls), 1, "boundary bytes must count toward the actual ceiling")
        self.assertLessEqual(len(result.encode("utf-8")), byte_budget - 1)

    def test_managed_cost_checks_complete_boundary_and_rendered_body(self):
        from model_context import _format_line, _render
        from media import MEDIA_BOUNDARY
        self.source.add_messages(USER, [make_message(1, text="中文🙂English" * 300)])
        self.import_ready()
        row = self.projection.messages(ACCOUNT, USER)[0]
        boundary = MEDIA_BOUNDARY + "\n"
        expected = boundary + _render([], [{"text": _format_line(row)}])
        observed = []

        def managed_cost(text):
            observed.append(text)
            return len(text.encode("utf-8")) + 16 * sum(
                max(1, (len(line) + 699) // 700) for line in text.split("\n"))

        budget = managed_cost(expected)
        result = self.projection.build_model_context(
            ACCOUNT, USER, 1, budget // 4, lambda *_args: self.fail("original fits"), "fp",
            budget_bytes=budget, budget_cost=managed_cost, media_observations={})
        self.assertEqual(result, expected)
        self.assertEqual(observed[-1], expected, "final fit must include the complete boundary")

    def test_tiny_positive_byte_budget_uses_exact_render_instead_of_legacy_minimum(self):
        from model_context import _format_line, _render
        self.source.add_messages(USER, [make_message(1, name="F", text="x", time_ms=1)])
        self.import_ready()
        row = self.projection.messages(ACCOUNT, USER)[0]
        expected = _render([], [{"text": _format_line(row)}])
        budget = len(expected.encode("utf-8"))
        self.assertLessEqual(budget, 64)
        result = self.projection.build_model_context(
            ACCOUNT, USER, 1, 1, lambda *_args: self.fail("small original fits"), "fp", budget_bytes=budget)
        self.assertEqual(result, expected)

    def test_database_file_reparse_is_rejected_before_sqlite_writes(self):
        database = self.stores.account(ACCOUNT)
        import store
        original = store._reparse
        with patch("store._reparse", side_effect=lambda path: Path(path) == database.path or original(path)):
            with self.assertRaises(AdvisorPathError):
                database.context(USER)
        self.assertFalse(database.path.exists())

    def test_unrenderable_timestamp_keeps_original_and_text(self):
        self.source.add_messages(USER, [make_message(1, time_ms=9 * 10 ** 18, text="valid text")])
        self.import_ready()
        rendered = self.projection.build_model_context(ACCOUNT, USER, 1, 10000,
                                                       lambda *_args: "should not compact", "fp")
        self.assertIn("时间未知", rendered)
        self.assertIn("valid text", rendered)
        self.assertEqual(self.projection.messages(ACCOUNT, USER)[0]["time_ms"], 9 * 10 ** 18)





class ServiceBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = FakeSource()
        self.source.seed_pages(USER, 3)
        self.model_store = RetainedModelStore()
        self.runtime = FakeRuntime()
        self.service = build_service(self.source, self.model_store, self.root,
                                      runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)

    def completed(self, result):
        identifier = result["run"]["id"]
        terminal = wait_until(lambda: self.service._runs[identifier]
                              if self.service._runs[identifier].state != "running" else None)
        self.assertIsNotNone(terminal)
        # Publishing a terminal answer precedes the handler's finally cleanup.
        # Follow-up tests need the same drained session guard as a real next run,
        # not merely a visible answer while the previous worker is still alive.
        if terminal.worker is not None:
            terminal.worker.join(10)
            self.assertFalse(terminal.worker.is_alive(), "terminal run did not release its worker")
        return terminal

    def test_first_send_waits_for_all_pages_before_any_paid_call(self):
        self.source.users[USER] = []
        self.source.seed_pages(USER, 600)
        self.model_store.context_tokens = 1000000
        entered, release = threading.Event(), threading.Event()
        def block(_source):
            if not entered.is_set():
                entered.set()
                release.wait(5)
        self.source.page_hook = block
        result = self.service.start(ACCOUNT, USER, AGENT, None, "first send", "first")
        self.assertTrue(entered.wait(2))
        time.sleep(0.05)
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertEqual(self.runtime.compact_calls, [])
        release.set()
        run = self.completed(result)
        self.assertEqual(run.state, "done")
        request = self.runtime.respond_calls[0][1]
        self.assertEqual(run.database.context(USER)["read_count"], 600)
        self.assertNotIn("contextMessageCount", request)
        self.assertEqual(run.database.run(run.id)["context_revision"], request["contextRevision"])
        self.assertIn("消息内容 1 ", request["contextFileText"])
        self.assertIn("消息内容 600 ", request["contextFileText"])

    def test_cancel_while_first_import_blocks_prevents_generation(self):
        entered, release = threading.Event(), threading.Event()
        def block(_source):
            entered.set()
            release.wait(5)
        self.source.page_hook = block
        result = self.service.start(ACCOUNT, USER, AGENT, None, "stop first send", "stop-first")
        self.assertTrue(entered.wait(2))
        self.service.stop(ACCOUNT, USER, result["run"]["id"])
        release.set()
        self.completed(result).worker.join(2)
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertEqual(self.runtime.compact_calls, [])

    def test_retained_api_runs_while_analysis_is_local_without_switching(self):
        run = self.completed(self.service.start(ACCOUNT, USER, AGENT, None, "advice", "local-analysis"))
        self.assertEqual(run.state, "done")
        self.assertEqual(self.model_store.mode, "local")
        self.assertEqual(self.runtime.respond_calls[0][0]["id"], self.model_store.source_id)

    def test_keyless_ollama_runs(self):
        self.model_store.protocol = "ollama"
        self.model_store.key = None
        self.assertEqual(self.service.catalog()["engine"]["state"], "available")
        run = self.completed(self.service.start(ACCOUNT, USER, AGENT, None, "local generation", "ollama"))
        self.assertEqual(run.state, "done")
        self.assertNotIn("apiKey", self.runtime.respond_calls[0][0])

    def test_initial_retry_is_idempotent_even_without_thread_id(self):
        first = self.service.start(ACCOUNT, USER, AGENT, None, "same message", "idempotent")
        self.completed(first)
        duplicate = self.service.start(ACCOUNT, USER, AGENT, None, "same message", "idempotent")
        self.assertEqual(duplicate["threadId"], first["threadId"])
        self.assertEqual(duplicate["run"]["id"], first["run"]["id"])
        self.assertEqual(len(self.runtime.respond_calls), 1)
        with self.assertRaises(AdvisorError) as caught:
            self.service.start(ACCOUNT, USER, AGENT, None, "different message", "idempotent")
        self.assertEqual(caught.exception.code, "request-conflict")

    def test_native_history_is_not_duplicated_into_managed_context(self):
        first = self.service.start(ACCOUNT, USER, AGENT, None, "unique earlier user request", "history-1")
        self.completed(first)
        self.completed(self.service.start(ACCOUNT, USER, AGENT, first["threadId"], "new request", "history-2"))
        second = self.runtime.respond_calls[1][1]
        self.assertEqual(second["runtimeSessionId"], "sess-1")
        self.assertNotIn("unique earlier user request", second["contextFileText"])
        self.assertNotIn(self.runtime.text, second["contextFileText"])

    def test_new_model_reuses_compatible_native_session_and_updates_identity(self):
        first = self.service.start(ACCOUNT, USER, AGENT, None, "first model", "model-1")
        self.completed(first)
        self.model_store.model = "synthetic-second-model"
        self.model_store.source_id = "second-source"
        self.completed(self.service.start(ACCOUNT, USER, AGENT, first["threadId"], "second model", "model-2"))
        self.assertEqual(self.runtime.respond_calls[1][1]["runtimeSessionId"], "sess-1")
        payload = self.service.thread(ACCOUNT, USER, AGENT)["thread"]
        self.assertEqual(len([message for message in payload["messages"] if message["role"] == "assistant"]), 2)
        self.assertEqual(payload["sourceId"], "second-source")

    def test_rename_keeps_native_history(self):
        first = self.service.start(ACCOUNT, USER, AGENT, None, "first", "rename-1")
        self.completed(first)
        agent = self.service.stores.config.agent(AGENT)
        self.service.save_template({"action": "save", "agent": {key: agent[key] for key in
                                                               ("id", "name", "description", "prompt", "skillIds")}
                                    | {"name": "改名的军师"}})
        self.completed(self.service.start(ACCOUNT, USER, AGENT, first["threadId"], "second", "rename-2"))
        self.assertEqual(self.runtime.respond_calls[1][1]["runtimeSessionId"], "sess-1")

    def test_account_switch_ignores_late_output_without_old_account_commit(self):
        self.runtime = DelayedRuntime()
        result = self.service.start(ACCOUNT, USER, AGENT, None, "before switch", "switch")
        self.assertTrue(self.runtime.entered.wait(2))
        run = self.service._runs[result["run"]["id"]]
        before = run.database.thread_messages(run.thread_id)
        self.source.account = "synthetic-other-account"
        self.runtime.release.set()
        run.worker.join(2)
        self.assertEqual(run.state, "stopped")
        self.assertEqual(run.database.thread_messages(run.thread_id), before)

    def test_stop_and_clear_cannot_be_undone_by_late_callback(self):
        self.runtime = DelayedRuntime()
        result = self.service.start(ACCOUNT, USER, AGENT, None, "clear", "late-clear")
        self.assertTrue(self.runtime.entered.wait(2))
        run = self.service._runs[result["run"]["id"]]
        owned_directory = run.database.directory
        self.service.clear_account(ACCOUNT)
        self.service._on_event(run, {"type": "text", "text": "late callback"})
        self.service._finish_locked(run, "late answer", "late-session")
        self.service._fail_locked(run, "late failure", "error")
        self.service.resume_after_failed_account_clear()
        with self.assertRaises(AdvisorError):
            self.service.prepare_context(ACCOUNT, USER)
        self.assertFalse(owned_directory.exists())

    def test_failed_native_drain_does_not_delete_or_resume_account(self):
        self.completed(self.service.start(ACCOUNT, USER, AGENT, None, "retained", "drain-fail"))
        path = self.service.stores.account(ACCOUNT).directory
        def fail(_account):
            raise RuntimeError("synthetic drain failure")
        self.runtime.shutdown_account = fail
        with self.assertRaises(RuntimeError):
            self.service.clear_account(ACCOUNT)
        self.assertTrue(path.exists())
        with self.assertRaises(AdvisorError):
            self.service.thread(ACCOUNT, USER, AGENT)
        self.service.resume_after_failed_account_clear()
        with self.assertRaises(AdvisorError):
            self.service.thread(ACCOUNT, USER, AGENT)

    def test_raw_provider_secret_exception_is_not_rendered_or_stored(self):
        secret = "synthetic-secret-key-and-private-path"
        def fail(*_args):
            raise RuntimeError(secret)
        self.runtime.respond = fail
        result = self.service.start(ACCOUNT, USER, AGENT, None, "safe", "redact")
        run = self.completed(result)
        self.assertEqual(run.state, "error")
        self.assertNotIn(secret, json.dumps(self.service.events(ACCOUNT, USER, run.id), ensure_ascii=False))

    def test_native_retry_timing_is_preserved_without_invented_countdown(self):
        event = normalize_event({"type": "status", "state": "retry", "attempt": 2,
                                 "next": 1800000000000, "nativeType": "session.retry"}, 1)
        self.assertEqual((event["attempt"], event["next"], event["nativeType"]),
                         (2, 1800000000000, "session.retry"))
        invalid = normalize_event({"type": "status", "attempt": -1, "next": True}, 2)
        self.assertNotIn("attempt", invalid)
        self.assertNotIn("next", invalid)

    def test_changed_model_during_generation_blocks_old_commit(self):
        self.runtime = DelayedRuntime()
        result = self.service.start(ACCOUNT, USER, AGENT, None, "old config", "config-change")
        self.assertTrue(self.runtime.entered.wait(2))
        run = self.service._runs[result["run"]["id"]]
        self.model_store.model = "changed synthetic model"
        before = run.database.thread_messages(run.thread_id)
        self.runtime.release.set()
        run.worker.join(2)
        self.assertEqual(run.state, "stopped")
        self.assertEqual(run.database.thread_messages(run.thread_id), before)
        self.completed(self.service.start(ACCOUNT, USER, AGENT, result["threadId"], "new config", "config-new"))
        self.assertIsNone(self.runtime.respond_calls[1][1]["runtimeSessionId"])

    def test_source_epoch_reset_during_generation_keeps_the_answer_and_rebuilds_next(self):
        # Modified 2026-10-22: the model already has the old material; the next question rebuilds the discussion.
        self.runtime = DelayedRuntime()
        result = self.service.start(ACCOUNT, USER, AGENT, None, "old snapshot", "epoch-change")
        self.assertTrue(self.runtime.entered.wait(2))
        run = self.service._runs[result["run"]["id"]]
        self.source.users[USER] = [make_message(1, text="restored synthetic history")]
        self.service.prepare_context(ACCOUNT, USER)
        self.runtime.release.set()
        run.worker.join(2)
        self.assertEqual(run.state, "done")
        self.assertIn("late synthetic output", [message["text"] for message in run.database.thread_messages(run.thread_id)
                                                if message["role"] == "assistant"])
        self.completed(self.service.start(ACCOUNT, USER, AGENT, result["threadId"], "after reset", "epoch-next"))
        self.assertIsNone(self.runtime.respond_calls[1][1]["runtimeSessionId"])

    def test_new_message_during_generation_keeps_answering(self):
        # Added 2026-10-22: a friend's message arriving mid-answer leaves WeChat unreadable for a moment and
        # moves the 知己分析 stamp while Core imports it; neither stops the answer or its event stream.
        self.runtime = DelayedRuntime()
        result = self.service.start(ACCOUNT, USER, AGENT, None, "while they type", "new-message")
        self.assertTrue(self.runtime.entered.wait(2))
        run = self.service._runs[result["run"]["id"]]
        self.source.add_messages(USER, [make_message(99, text="对方刚发来的新消息")])
        self.service.prepare_context(ACCOUNT, USER)
        self.source.snapshot["stamp"] = "human-v2"
        unready = CoreError(409, "account-unavailable", "synthetic reader refreshing")
        with patch.object(self.source, "scope", return_value={"account": None}), \
                patch.object(self.source, "history_status", side_effect=unready):
            self.service._status_cache.clear()
            run.last_scope_check = 0
            self.service._on_event(run, {"type": "text", "text": "partial "})
            run.last_scope_check = 0
            self.assertEqual(self.service.events(ACCOUNT, USER, run.id)["run"]["state"], "running")
            self.service.human_changed(ACCOUNT, USER)
            self.assertEqual(run.state, "running")
        self.runtime.release.set()
        run.worker.join(2)
        self.assertEqual(run.state, "done")
        self.assertIn("late synthetic output", [message["text"] for message in run.database.thread_messages(run.thread_id)
                                                if message["role"] == "assistant"])

    def test_unready_wechat_at_question_time_waits_instead_of_failing(self):
        # Added 2026-10-22: the first history reads wait out a reader that is refreshing.
        calls, original = [], self.source.history_prepare
        def refreshing(account, user):
            calls.append(user)
            if len(calls) < 3:
                raise CoreError(409, "account-unavailable", "synthetic reader refreshing")
            return original(account, user)
        with patch.object(self.source, "history_prepare", side_effect=refreshing):
            run = self.completed(self.service.start(ACCOUNT, USER, AGENT, None, "right after their message", "unready"))
        self.assertEqual(run.state, "done")
        self.assertEqual(len(calls), 3)

    def test_changed_context_source_resets_native_session_visibly(self):
        first = self.service.start(ACCOUNT, USER, AGENT, None, "old source", "root-1")
        self.completed(first)
        self.source.workdir = "C:/synthetic-next-root"
        self.source.users[USER] = [make_message(1, text="new source conversation")]
        self.completed(self.service.start(ACCOUNT, USER, AGENT, first["threadId"], "new source", "root-2"))
        self.assertIsNone(self.runtime.respond_calls[1][1]["runtimeSessionId"])
        payload = self.service.thread(ACCOUNT, USER, AGENT)["thread"]
        self.assertTrue(any(message["role"] == "status" and "已重建上下文" in message["text"]
                            for message in payload["messages"]))

    def test_oversized_prompt_and_skill_fail_before_paid_requests(self):
        self.model_store.context_tokens = 4096
        created = self.service.save_template({"action": "save", "agent": {
            "name": "too long", "description": "", "prompt": "p" * 5000,
            "skillIds": []}})
        custom = [agent for agent in created["agents"] if not agent["builtin"]][0]
        run = self.completed(self.service.start(ACCOUNT, USER, custom["id"], None, "question", "too-long"))
        self.assertEqual(run.state, "error")
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertEqual(self.runtime.compact_calls, [])

    def test_cancel_during_compact_leaves_no_late_summary(self):
        self.source.users[USER] = []
        self.source.seed_pages(USER, 100, text_size=100)
        self.model_store.context_tokens = 8192
        entered, release = threading.Event(), threading.Event()
        def compact(config, request, on_event, cancel):
            entered.set()
            release.wait(5)
            return {"text": "neutral synthetic summary"}
        self.runtime.compact = compact
        result = self.service.start(ACCOUNT, USER, AGENT, None, "compact then stop", "stop-compact")
        self.assertTrue(entered.wait(2))
        run = self.service._runs[result["run"]["id"]]
        self.service.stop(ACCOUNT, USER, run.id)
        release.set()
        run.worker.join(2)
        self.assertEqual(run.database._rows("SELECT * FROM summaries"), [])
        self.assertEqual(self.runtime.respond_calls, [])

    def test_shutdown_drain_timeout_preserves_open_store_until_worker_finishes(self):
        self.runtime = DelayedRuntime()
        self.runtime.close = lambda: None
        result = self.service.start(ACCOUNT, USER, AGENT, None, "blocked synthetic engine", "shutdown")
        self.assertTrue(self.runtime.entered.wait(2))
        run = self.service._runs[result["run"]["id"]]
        with patch("service.WORKER_DRAIN_SECONDS", 0.01):
            with self.assertRaises(RuntimeError):
                self.service.shutdown()
        self.assertFalse(run.database._closed)
        self.assertFalse(self.service.stores._closed)
        self.runtime.release.set()
        run.worker.join(2)
        self.service.shutdown()
        self.assertTrue(run.database._closed)

    def test_unavailable_factory_never_claims_engine_ready(self):
        factory = lambda: self.runtime
        factory.available = lambda: False
        self.service.engine.available = lambda: False
        self.assertEqual(self.service.catalog()["engine"]["state"], "missing")
        with self.assertRaises(AdvisorError) as caught:
            self.service.start(ACCOUNT, USER, AGENT, None, "not installed", "missing-engine")
        self.assertEqual(caught.exception.code, "engine-unavailable")

    def test_failed_persistence_is_terminal_and_does_not_leak_exception(self):
        self.runtime = DelayedRuntime()
        result = self.service.start(ACCOUNT, USER, AGENT, None, "save failure", "persistence")
        self.assertTrue(self.runtime.entered.wait(2))
        run = self.service._runs[result["run"]["id"]]
        with patch.object(run.database, "finish_run", side_effect=OSError("synthetic-private-path")):
            self.runtime.release.set()
            run.worker.join(2)
        self.assertEqual(run.state, "error")
        self.assertNotIn("synthetic-private-path", run.error)
        self.assertIn("保存失败", run.error)

    def test_agent_payload_has_welcome_and_no_reading_flow_or_counts(self):
        entered, release = threading.Event(), threading.Event()
        def block(_source):
            entered.set()
            release.wait(5)
        self.source.page_hook = block
        payload = self.service.thread(ACCOUNT, USER, AGENT)
        self.assertEqual(payload["thread"]["welcome"], "嗨，我是你的狗头军师")
        result = self.service.start(ACCOUNT, USER, AGENT, payload["thread"]["id"], "first question", "silent-read")
        self.assertTrue(entered.wait(2))
        pending = self.service.events(ACCOUNT, USER, result["run"]["id"])
        self.assertEqual(pending["events"], [])
        self.assertEqual(set(self.service.thread(ACCOUNT, USER, AGENT)["context"]), {"revision"})
        self.assertEqual([row["role"] for row in self.service.stores.account(ACCOUNT).thread_messages(result["threadId"])], ["user"])
        release.set()
        self.completed(result)
        request = self.runtime.respond_calls[0][1]
        self.assertNotIn("context", request)
        self.assertNotIn("contextMessageCount", request)
        self.assertIn("每轮回答前", request["system"])
        self.assertIn("只读资料文件", request["system"])
        self.assertNotIn("消息内容", request["system"])
        self.assertIn("消息内容 1", request["contextFileText"])


if __name__ == "__main__":
    unittest.main()
