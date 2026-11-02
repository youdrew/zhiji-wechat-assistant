"""One shared source snapshot, resumable branches and preserved manual data.

All chats/models are synthetic and the database is temporary.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import threading
import unittest
from unittest.mock import patch
import test_draft as fixture
from test_draft_incremental import AuditedCore
from test_self_analysis import dialogue, message
from self_contracts import SelfError
from self_service import Service
from self_evidence import window_messages
from kyf_plugin import ModelError


class UnifiedModels(fixture.Models):
    def generate(self, task, request, step=None, timeout=None):
        if step == "self-evidence-extract":
            messages = window_messages(json.loads(request["prompt"]))
            self.replies = [json.dumps({"profile": [
                {"i": index, "section": "style", "text": "表达简短，常回应对方的问题", "relation": "trait",
                 "quote": item["text"][:200], "reason": "合成的表达方式观察"} for item in messages for index in [item["i"]]],
                "tags": [], "persona": [], "values": []})]
        elif step == "self-evidence-review":
            proposed = json.loads(request["prompt"])["proposed"]
            self.replies = [json.dumps({key: [{"n": index} for index, _ in enumerate(rows)] for key, rows in proposed.items()})]
        elif step.startswith("self-tags"):
            self.replies = [json.dumps({"tags": [], "merge": []})]
        elif step == "self-timeline-extract":
            self.replies = [json.dumps({"events": []})]
        elif step == "self-timeline-merge":
            self.replies = [json.dumps({"merge": []})]
        elif "persona" in step:
            self.replies = [json.dumps({"evidence": [], "claims": [], "items": [], "dimensions": []})]
        else:
            self.replies = [fixture.DRAFT]
        return super().generate(task, request, step, timeout)


class Embedding:
    thread = None
    def __init__(self, store):
        self.store = store
    def start(self):
        with self.store.lock, self.store.db:
            self.store._meta("embeddingRun", {"state": "ready"})
    def pause(self):
        pass
    def close(self):
        return True


class UnifiedTests(unittest.TestCase):
    setUp = fixture.DraftTests.setUp
    tearDown = fixture.DraftTests.tearDown

    def setup_unified(self, live=None, configured=True, reuse_recent_sync=False):
        if not reuse_recent_sync:
            # These runs follow source edits within milliseconds; a real user's
            # repeat run within FRESH_SYNC_MS reuses the last complete sync.
            patcher = patch("self_profile_corpus.FRESH_SYNC_MS", -1)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service.close()
        self.core = AuditedCore(live or {"friend-a": dialogue(), "group@chatroom": dialogue(prefix="群聊本人")})
        self.models = UnifiedModels(configured=configured)
        self.plugin.core, self.plugin.models = self.core, self.models
        self.plugin.tasks, self.plugin.threads = [], []
        self.service = Service(self.plugin)
        store, _ = self.service._account(self.account)
        self.service.embeddings[self.account] = Embedding(store)
        return store

    def finish(self):
        for thread in self.plugin.threads:
            thread.join(15)
            self.assertFalse(thread.is_alive())
        return self.service.analysis_status(self.account)

    def run_analysis(self, **params):
        self.service.analyze(self.account, {"depth": "basic", **params})
        return self.finish()

    def test_one_task_one_read_shared_by_all_four_outputs(self):
        store = self.setup_unified()
        result = self.run_analysis()
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(len(self.plugin.tasks), 1)
        self.assertEqual(sorted(self.core.pages), [("friend-a", 0), ("group@chatroom", 0)])
        self.assertEqual(result["coverage"]["ownMessages"], 6)
        self.assertEqual(result["coverage"]["evidence"]["analyzedMessages"], 6)
        self.assertEqual(store.counts()["samples"], 3)  # groups are context, never reply exemplars
        self.assertEqual(self.service.draft_status(self.account)["state"], "done")
        self.assertEqual(self.service.tags(self.account)["run"]["state"], "complete")
        self.assertEqual(len(self.service.persona(self.account)["dimensions"]), 19)
        self.assertEqual([item["label"] for item in self.service.values(self.account)["dimensions"]],
                         ["感情观", "家庭观", "金钱观", "人生规划", "价值底色"])
        for step in result["steps"]:
            if step["key"] in ("evidence", "profile", "tags", "persona", "values"):
                self.assertEqual((step["done"], step["total"], step["unit"]), (6, 6, "条"), step)
                if step["key"] in ("evidence", "profile"):
                    self.assertGreater(step["modelCalls"], 0)
                    self.assertIsNone(step["activeCallStartedAt"])
        names = [item["step"] for item in self.models.requests]
        self.assertEqual(names.count("self-evidence-extract"), 1)
        self.assertFalse(any(name in ("profile-draft", "self-tags", "self-persona-evidence") for name in names))
        history = result["steps"][0]
        self.assertEqual((history["sourceMessagesRead"], history["currentChatDone"], history["currentChatTotal"]), (12, 6, 6))
        reports = self.service.analyses[self.account].context.progress_events
        self.assertTrue(any(report.get("sourceMessagesRead") == 12 for report in reports))

    def test_a_chat_rebuilt_soon_after_a_complete_sync_does_not_block_the_next_run(self):
        self.setup_unified(reuse_recent_sync=True)
        self.assertEqual(self.run_analysis()["state"], "done")
        self.core.epochs["friend-a"] = 9
        self.assertEqual(self.run_analysis()["state"], "done")

    def test_repeat_run_soon_after_a_complete_sync_reads_only_reopened_chats(self):
        self.setup_unified(reuse_recent_sync=True)
        self.assertEqual(self.run_analysis()["state"], "done")
        audits = []
        original = self.core.history_status
        def status(account, user, **options):
            if options.get("audit"):
                audits.append(user)
            return original(account, user, **options)
        self.core.history_status = status
        self.assertEqual(self.run_analysis()["state"], "done")
        self.assertEqual(audits, [])
        store, _ = self.service._account(self.account)
        store.mark_stale("friend-a")
        self.assertEqual(self.run_analysis()["state"], "done")
        self.assertEqual(audits, ["friend-a"])

    def test_explicit_history_refresh_checks_chats_even_with_a_recent_sync(self):
        self.setup_unified(reuse_recent_sync=True)
        self.assertEqual(self.run_analysis(outputs=["library"])["state"], "done")
        self.core.live["friend-a"].append(message(7, "self", "合成新增发言，用于完整同步检查"))
        result = self.run_analysis(outputs=["library"], refreshHistory=True)
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(result["coverage"]["ownMessages"], 7)
        self.assertTrue(result["refreshHistory"])
        with self.assertRaises(SelfError):
            self.service.analyze(self.account, {"refreshHistory": "true"})

    def test_ordinary_update_observes_core_projection_changes_even_without_a_message_event(self):
        self.setup_unified(reuse_recent_sync=True)
        first = self.run_analysis(outputs=['library'])
        self.core.live['friend-a'].append(message(7, 'self', '合成的已投影新发言'))
        self.core.history_prepare(self.account, 'friend-a', refresh=True)
        result = self.run_analysis(outputs=['library'])
        self.assertFalse(result['refreshHistory'])
        self.assertEqual(result['state'], 'done')
        self.assertEqual((first['coverage']['ownMessages'], result['coverage']['ownMessages']), (6, 7))

    def test_running_analysis_does_not_silently_accept_different_outputs(self):
        self.setup_unified()
        reached, release = threading.Event(), threading.Event()
        self.models.hook = lambda _context, request: (reached.set(), release.wait(5)) if request["step"] == "self-evidence-extract" else None
        try:
            first = self.service.analyze(self.account, {"depth": "basic", "outputs": ["profile"]})
            self.assertTrue(reached.wait(5))
            same = self.service.analyze(self.account, {"depth": "basic", "outputs": ["profile"]})
            self.assertEqual(first["id"], same["id"])
            for params in ({"depth": "basic", "outputs": ["profile", "worldview"]},
                           {"depth": "deep", "outputs": ["profile"]},
                           {"depth": "basic", "outputs": ["profile"], "refreshHistory": True}):
                with self.assertRaises(SelfError) as caught:
                    self.service.analyze(self.account, params)
                self.assertEqual(caught.exception.code, "analysis-busy")
        finally:
            release.set()
            self.finish()

    def test_saved_external_chat_reaches_every_output_but_not_reply_examples(self):
        store = self.setup_unified()
        records = [{"id": f"m{index}", "sourceId": "source-a", "order": f"000000000000:{index:012d}", "kind": "text",
                    "sender": sender, "text": text, "time": None}
                   for index, (sender, text) in enumerate([("other", "最近在忙什么"), ("self", "在准备马拉松"),
                                                            ("other", "厉害"), ("self", "每周跑三次")], 1)]
        routes = {"settings": self.core.get("settings"),
                  "external/friends": {"friends": [{"id": "friend-x", "name": "合成外部好友", "wechatAccount": None}]},
                  "external/history": {"messages": records, "sources": [{"id": "source-a", "platform": "合成来源"}]}}
        self.core.get = lambda path, **query: json.loads(json.dumps(routes[path]))
        result = self.run_analysis()
        self.assertEqual(result["state"], "done", result)
        self.assertEqual((result["coverage"]["ownMessages"], result["coverage"]["externalMessages"]), (8, 2))
        self.assertEqual(result["coverage"]["evidence"]["analyzedMessages"], 8)
        self.assertEqual(store.counts()["samples"], 3)  # external chats have no reply exemplars
        extracted = [item for request in self.models.requests if request["step"] == "self-evidence-extract"
                     for item in window_messages(request["prompt"])]
        marathon = next(item for item in extracted if item["text"] == "在准备马拉松")
        self.assertEqual(marathon["chat"], "合成外部好友（合成来源·外部聊天）")

    def test_history_failure_updates_terminal_coverage_and_preserves_saved_messages(self):
        store = self.setup_unified({"friend-a": dialogue()})
        self.assertEqual(self.run_analysis()["state"], "done")
        saved, calls = store.counts()["samples"], len(self.models.requests)
        def unavailable(*_args, **_kwargs):
            raise SelfError("history-error", "合成读取暂不可用")
        self.core.history_prepare = unavailable
        # Unchanged chats are skipped without preparing; a new message requires a read.
        self.core.live["friend-a"].append(message(7, "other", "还在吗"))
        result = self.run_analysis()
        self.assertEqual(result["state"], "partial")
        self.assertEqual(result["coverage"]["failedConversations"], 1)
        self.assertTrue(result["coverage"]["gaps"])
        # A failed chat no longer stops the round; it is reported and retried next run.
        self.assertIn("1 个聊天读取失败", result["steps"][0]["detail"])
        self.assertEqual(store.counts()["samples"], saved)

    def test_timeline_only_run_executes_both_timeline_steps(self):
        # 回归：时间线两步曾被 eligible("timeline") 的“提取完成”前置挡住，
        # 只选时间线的轮次会秒完 done 且两步保持 pending。
        self.setup_unified()
        result = self.run_analysis(outputs=["timeline"])
        self.assertEqual(result["state"], "done", result)
        states = {step["key"]: step["state"] for step in result["steps"]}
        self.assertEqual((states["timeline-extract"], states["timeline"]), ("done", "done"), states)
        self.assertIn("self-timeline-extract", [item["step"] for item in self.models.requests])
        view = self.service.timeline(self.account)
        self.assertEqual((view["events"], view["periods"]), ([], []))

    def test_summary_finishes_before_long_life_event_extraction(self):
        self.setup_unified()
        result = self.run_analysis(outputs=["profile", "timeline"])
        self.assertEqual(result["state"], "done", result)
        steps = [request["step"] for request in self.models.requests]
        self.assertLess(steps.index("profile-draft-final"), steps.index("self-timeline-extract"))

    def test_shadow_judgments_wait_until_all_extraction_and_summary_steps_finish(self):
        self.setup_unified()
        descriptor = {"roles": ["judge"], "fingerprint": "synthetic-judge-fp", "stamp": "synthetic-judge-stamp"}
        self.models.service_for = lambda role, refresh=False: descriptor
        counts = []
        def judge(context, state, questions, step=None):
            steps = [request["step"] for request in self.models.requests]
            self.assertIn("self-timeline-extract", steps)
            self.assertIn("self-evidence-extract", steps)
            counts.append(len(steps))
            return {"answers": {"has_content": {"noul": .1}}, "engine": "generate",
                    "fingerprint": descriptor["fingerprint"]}
        self.models.judge = judge
        result = self.run_analysis(outputs=["profile", "timeline"])
        self.assertEqual(result["state"], "done", result)
        self.assertTrue(counts)
        self.assertEqual(set(counts), {len(self.models.requests)})
        summary = self.service.judge_shadow(self.account)
        self.assertEqual(summary["mode"], "shadow")
        self.assertIsNone(summary["threshold"])
        self.assertEqual({group["kind"] for group in summary["engines"]}, {"evidence", "timeline"})

    def test_shadow_status_rechecks_account_after_reading(self):
        self.setup_unified()
        from self_judge_shadow import JudgeShadow
        original = JudgeShadow.summary
        def switched(shadow, source, revision):
            result = original(shadow, source, revision)
            self.core.account = "different-account"
            return result
        with patch.object(JudgeShadow, "summary", switched), self.assertRaises(SelfError) as error:
            self.service.judge_shadow(self.account)
        self.assertEqual(error.exception.code, "account-changed")

    def test_registered_shadow_status_rpc_reads_the_real_service(self):
        self.setup_unified()
        import importlib.util
        from pathlib import Path
        path = Path(__file__).parents[1] / "backend" / "main.py"
        spec = importlib.util.spec_from_file_location("self_shadow_rpc_under_test", path)
        entry = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(entry)
        self.addCleanup(entry.plugin.pool.shutdown)
        entry.service = self.service
        result = entry.plugin.methods["self.judgeShadow"]({}, {"account": self.account})
        self.assertEqual(result["mode"], "shadow")
        self.assertEqual(result["counts"], {"total": 0, "done": 0, "pending": 0, "error": 0})

    def test_unchanged_update_does_not_reread_or_call_model(self):
        self.setup_unified()
        self.assertEqual(self.run_analysis()["state"], "done")
        pages, calls = len(self.core.pages), len(self.models.requests)
        self.assertEqual(self.run_analysis()["state"], "done")
        self.assertEqual(len(self.core.pages), pages)
        self.assertEqual(len(self.models.requests), calls)

    def test_missing_model_still_builds_local_library_and_marks_partial(self):
        store = self.setup_unified(configured=False)
        result = self.run_analysis()
        self.assertEqual(result["state"], "partial", result)
        states = {item["key"]: item["state"] for item in result["steps"]}
        self.assertEqual((states["history"], states["library"], states["semantic"]), ("done", "done", "done"))
        self.assertEqual(store.counts()["samples"], 3)
        self.assertFalse(self.models.requests)

    def test_append_and_edit_update_examples_preserve_exclusions_manual_tags_and_persona(self):
        store = self.setup_unified({"friend-a": dialogue()})
        self.run_analysis()
        sample = store.rows("SELECT id FROM samples ORDER BY to_seq")[0]["id"]
        store.exclude(sample, True)
        self.service.tag_edit(self.account, {"action": "add", "name": "手动习惯", "category": "性格习惯"})
        persona = self.service.persona(self.account)
        self.service.persona_edit(self.account, {"key": "big5_e", "text": "我自己填写的类型", "expectedVersion": persona["version"]})
        self.core.live["friend-a"].extend([message(7, "other", "明天一起吗"), message(8, "self", "好呀明天见")])
        pages = len(self.core.pages)
        self.assertEqual(self.run_analysis()["state"], "done")
        self.assertEqual(self.core.pages[pages:], [("friend-a", 6)])
        self.assertEqual((store.counts()["storedSamples"], store.counts()["excludedSamples"]), (4, 1))
        self.core.live["friend-a"][1] = message(2, "self", "改为周六再约")
        # An old edit is found when the chat next changes; an unchanged chat is skipped.
        self.core.live["friend-a"].append(message(9, "other", "好的"))
        self.assertEqual(self.run_analysis()["state"], "done")
        self.assertTrue(store.rows("SELECT id FROM samples WHERE reply='改为周六再约'"))
        self.assertEqual(store.counts()["excludedSamples"], 1)
        self.assertTrue(any(item["name"] == "手动习惯" for item in self.service.tags(self.account)["tags"]))
        self.assertEqual(self.service.persona(self.account)["dimensions"][2]["manual"]["text"], "我自己填写的类型")

    def test_pause_does_not_commit_late_generation_and_can_resume(self):
        store = self.setup_unified({"friend-a": dialogue()})
        reached, release = threading.Event(), threading.Event()
        def hook(task, request):
            if request["step"] == "self-evidence-extract":
                reached.set()
                release.wait(5)
        self.models.hook = hook
        self.service.analyze(self.account, {"depth": "deep"})
        self.assertTrue(reached.wait(5))
        self.service.analysis_pause(self.account)
        release.set()
        self.assertEqual(self.finish()["state"], "paused")
        self.assertEqual(store.counts()["samples"], 3)
        self.models.hook = None
        self.assertEqual(self.run_analysis(depth="deep")["state"], "done")

    def test_running_is_idempotent_and_legacy_parallel_writes_blocked(self):
        self.setup_unified({"friend-a": dialogue()})
        reached, release = threading.Event(), threading.Event()
        self.models.hook = lambda _task, _request: (reached.set(), release.wait(5))
        first = self.service.analyze(self.account, {"depth": "basic"})
        self.assertTrue(reached.wait(5))
        self.assertEqual(self.service.analyze(self.account, {"depth": "basic"})["id"], first["id"])
        self.assertEqual(len(self.plugin.tasks), 1)
        for action in (lambda: self.service.draft(self.account, {}), lambda: self.service.tags_analyze(self.account),
                       lambda: self.service.build(self.account), lambda: self.service.reset(self.account)):
            with self.assertRaises(SelfError) as error:
                action()
            self.assertEqual(error.exception.code, "analysis-busy")
        release.set()
        self.finish()

    def test_stored_running_task_returns_interrupted_after_restart(self):
        store = self.setup_unified()
        with store.lock, store.db:
            store._meta("selfAnalysisRun", {"state": "running", "steps": [{"key": "tags", "state": "running"}]})
        self.assertEqual(self.service.analysis_status(self.account)["state"], "interrupted")

    def test_one_failed_model_branch_does_not_stop_other_results(self):
        store = self.setup_unified()
        with patch("self_evidence_summaries.TagSummary.work", side_effect=ModelError("invalid-output", "SYNTHETIC_PRIVATE_RESPONSE")):
            result = self.run_analysis()
        states = {item["key"]: item["state"] for item in result["steps"]}
        self.assertEqual(result["state"], "partial")
        self.assertEqual(states["tags"], "error")
        self.assertEqual((states["profile"], states["persona"], states["semantic"]), ("done", "done", "done"))
        self.assertNotIn("SYNTHETIC_PRIVATE_RESPONSE", json.dumps(result))
        self.assertEqual(store.counts()["samples"], 3)

    def test_clear_library_rebuilds_from_shared_snapshot_without_source_reread(self):
        store = self.setup_unified()
        self.run_analysis()
        pages, calls = len(self.core.pages), len(self.models.requests)
        self.service.reset(self.account)
        self.assertEqual(store.counts()["samples"], 0)
        self.assertEqual(self.run_analysis()["state"], "done")
        self.assertEqual((len(self.core.pages), len(self.models.requests)), (pages, calls))
        self.assertEqual(store.counts()["samples"], 3)

    def test_restart_reuses_persisted_sources_and_all_model_results(self):
        self.setup_unified()
        self.assertEqual(self.run_analysis()["state"], "done")
        pages, calls = len(self.core.pages), len(self.models.requests)
        self.service.close()
        self.service = Service(self.plugin)
        store, _ = self.service._account(self.account)
        self.service.embeddings[self.account] = Embedding(store)
        self.assertEqual(self.run_analysis()["state"], "done")
        self.assertEqual((len(self.core.pages), len(self.models.requests)), (pages, calls))

    def test_unchanged_recovered_chat_refreshes_its_display_name_and_failure_state(self):
        store = self.setup_unified({"friend-a": dialogue()})
        self.run_analysis()
        store.finish_conversation("friend-a", "合成旧失败")
        original = self.core.history_conversations
        def catalog(*args, **kwargs):
            result = original(*args, **kwargs)
            result["conversations"] = [{**item, "name": "新名字"} for item in result["conversations"]]
            return result
        self.core.history_conversations = catalog
        self.assertEqual(self.run_analysis()["state"], "done")
        self.assertEqual(store.conversation("friend-a")["name"], "新名字")
        self.assertEqual(store.counts()["failed"], 0)

    def test_nontext_and_quotes_remain_pairing_barriers_without_media_payload(self):
        store = self.setup_unified({"friend-a": [
            message(1, "other", "你要去吗"),
            message(2, "other", "SYNTHETIC_MEDIA_PAYLOAD", kind="image"),
            message(3, "self", "我明天去"),
            message(4, "other", "下次一起吃饭吗"),
            message(5, "self", "好呀改天约"),
        ]})
        self.assertEqual(self.run_analysis()["state"], "done")
        self.assertEqual(store.counts()["samples"], 1)
        payloads = store.rows("SELECT payload FROM profile_corpus_reply_rows")
        self.assertNotIn("SYNTHETIC_MEDIA_PAYLOAD", json.dumps(payloads))
        self.assertEqual(json.loads(payloads[1]["payload"])["kind"], "barrier")

    def test_account_change_stops_all_remaining_branches(self):
        self.setup_unified()
        self.models.hook = lambda _task, _request: setattr(self.core, "account", "another-account")
        self.service.analyze(self.account, {"depth": "basic"})
        for thread in self.plugin.threads:
            thread.join(10)
        job = self.service.analyses[self.account].view()
        self.assertEqual(job["state"], "partial")
        self.assertTrue(all(item["state"] == "pending" for item in job["steps"] if item["key"] in ("tags", "persona")))
        self.core.account = self.account

    def test_selection_settings_are_account_local_and_never_start_work(self):
        self.setup_unified()
        self.assertEqual(self.service.analysis_settings(self.account, {})["outputs"], ["profile", "tags", "persona", "values", "library"])
        self.assertEqual(self.service.analysis_settings(self.account, {"outputs": ["tags"]})["outputs"], ["tags"])
        self.service.analysis_settings(self.account, {"outputs": []})
        for invalid in ([], ["unknown"], ["tags", "tags"], "tags", [True]):
            with self.assertRaises(SelfError):
                self.service.analyze(self.account, {"outputs": invalid})
        self.assertFalse(self.plugin.tasks)
        self.assertFalse(self.models.requests)
        self.assertEqual(self.service.analysis_status(self.account)["state"], "idle")

    def test_saved_unavailable_outputs_do_not_block_supported_analysis_or_erase_preferences(self):
        store = self.setup_unified(configured=False)
        with store.lock, store.db:
            store._meta("selfAnalysisOutputs", ["library", "retired-output"])
            store._meta("selfAnalysisRun", {"state": "paused", "outputs": ["library", "retired-output"],
                                           "steps": [{"key": "library", "state": "pending"},
                                                     {"key": "retired-output", "state": "pending"}]})
        self.assertEqual(self.service.analysis_settings(self.account, {})["outputs"], ["library"])
        self.assertEqual(store.meta("selfAnalysisOutputs"), ["library", "retired-output"])
        restored = self.service.analysis_status(self.account)
        self.assertEqual(restored["outputs"], ["library"])
        self.assertEqual([step["key"] for step in restored["steps"]], ["library"])
        self.service.analyze(self.account, {"depth": "basic"})
        self.finish()
        self.assertEqual(self.service.analysis_status(self.account)["state"], "done")
        self.assertFalse(self.models.requests)

    def test_library_only_skips_all_ai_stages_without_a_model(self):
        store = self.setup_unified(configured=False)
        result = self.run_analysis(outputs=["library"])
        self.assertEqual(result["state"], "done", result)
        states = {step["key"]: step["state"] for step in result["steps"]}
        self.assertTrue(all(states[key] == "skipped" for key in ("evidence", "profile", "tags", "persona", "values")))
        self.assertTrue(all(states[key] == "done" for key in ("history", "library", "semantic")))
        self.assertEqual(store.counts()["samples"], 3)
        self.assertFalse(self.models.requests)

    def test_pausing_an_ai_only_run_does_not_change_the_completed_library(self):
        store = self.setup_unified()
        self.assertEqual(self.run_analysis(outputs=["library"])["state"], "done")
        self.assertEqual(store.meta("build")["state"], "complete")
        reached, release = threading.Event(), threading.Event()
        self.models.hook = lambda _context, request: (reached.set(), release.wait(5)) if request["step"] == "self-evidence-extract" else None
        self.service.analyze(self.account, {"outputs": ["profile"], "depth": "basic"})
        self.assertTrue(reached.wait(5))
        self.service.analysis_pause(self.account)
        release.set()
        self.assertEqual(self.finish()["state"], "paused")
        self.assertEqual(store.meta("build")["state"], "complete")

    def test_choosing_another_summary_reuses_the_same_evidence_and_source_snapshot(self):
        self.setup_unified()
        first = self.run_analysis(outputs=["profile"])
        self.assertEqual(first["state"], "done", first)
        pages = len(self.core.pages)
        extract = sum(request["step"] == "self-evidence-extract" for request in self.models.requests)
        second = self.run_analysis(outputs=["tags"])
        self.assertEqual(second["state"], "done", second)
        self.assertEqual(second["outputs"], ["tags"])
        self.assertEqual(sum(request["step"] == "self-evidence-extract" for request in self.models.requests), extract)
        self.assertEqual(len(self.core.pages), pages)
        self.assertEqual(next(step for step in second["steps"] if step["key"] == "profile")["state"], "skipped")
        self.assertEqual(self.service.draft_status(self.account)["state"], "done")

    def test_pausing_a_pending_output_keeps_other_outputs_running_and_can_resume_it_alone(self):
        self.setup_unified()
        reached, release = threading.Event(), threading.Event()
        def hold(_context, request):
            if request["step"] == "self-evidence-extract":
                reached.set()
                release.wait(5)
        self.models.hook = hold
        self.service.analyze(self.account, {"depth": "basic"})
        self.assertTrue(reached.wait(5))
        self.service.analysis_pause(self.account, {"output": "tags"})
        release.set()
        result = self.finish()
        self.assertEqual(result["state"], "paused", result)
        states = {step["key"]: step["state"] for step in result["steps"]}
        self.assertEqual(states["tags"], "paused")
        self.assertTrue(all(states[key] == "done" for key in ("evidence", "profile", "persona", "values", "library", "semantic")))
        self.assertEqual(result["pausedOutputs"], ["tags"])
        self.models.hook = None
        calls = len(self.models.requests)
        self.service.analysis_resume(self.account, {"output": "tags", "depth": "basic"})
        self.assertEqual(self.finish()["state"], "done")
        self.assertEqual(len(self.models.requests), calls)

    def test_pausing_the_only_ai_output_cancels_common_extraction_and_ignores_late_result(self):
        from self_evidence import EvidenceStore
        store = self.setup_unified()
        reached, release = threading.Event(), threading.Event()
        self.models.hook = lambda _context, request: (reached.set(), release.wait(5)) if request["step"] == "self-evidence-extract" else None
        self.service.analyze(self.account, {"depth": "basic", "outputs": ["profile"]})
        self.assertTrue(reached.wait(5))
        self.service.analysis_pause(self.account, {"output": "profile"})
        release.set()
        result = self.finish()
        self.assertEqual(result["state"], "paused", result)
        self.assertEqual(EvidenceStore(store).coverage()["analyzedMessages"], 0)
        self.assertFalse(any(request["step"].startswith("profile-draft") for request in self.models.requests))
        self.models.hook = None
        self.assertEqual(self.run_analysis(outputs=["profile"])["state"], "done")

    def test_pause_and_resume_pending_output_during_another_summary_uses_one_core_task(self):
        self.setup_unified()
        reached, release = threading.Event(), threading.Event()
        self.models.hook = lambda _context, request: (reached.set(), release.wait(5)) if request["step"] == "profile-draft-final" else None
        self.service.analyze(self.account, {"depth": "basic", "outputs": ["profile", "tags"]})
        self.assertTrue(reached.wait(5))
        self.service.analysis_pause(self.account, {"output": "tags"})
        self.service.analysis_resume(self.account, {"output": "tags"})
        release.set()
        result = self.finish()
        self.assertEqual(result["state"], "done", result)
        self.assertEqual(result["pausedOutputs"], [])
        self.assertEqual(len(self.plugin.tasks), 1)

    def test_pausing_current_summary_does_not_publish_a_late_result(self):
        self.setup_unified()
        reached, release = threading.Event(), threading.Event()
        self.models.hook = lambda _context, request: (reached.set(), release.wait(5)) if request["step"] == "profile-draft-final" else None
        self.service.analyze(self.account, {"depth": "basic", "outputs": ["profile", "tags"]})
        self.assertTrue(reached.wait(5))
        self.service.analysis_pause(self.account, {"output": "profile"})
        release.set()
        result = self.finish()
        self.assertEqual(result["state"], "paused", result)
        self.assertEqual(self.service.draft_status(self.account)["state"], "cancelled")
        self.assertEqual(next(step for step in result["steps"] if step["key"] == "tags")["state"], "done")
        self.models.hook = None
        self.assertEqual(self.run_analysis(outputs=["profile"])["state"], "done")
        self.assertEqual(sum(request["step"] == "profile-draft-final" for request in self.models.requests), 2)


if __name__ == "__main__":
    unittest.main()
