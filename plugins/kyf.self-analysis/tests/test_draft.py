"""AI first draft of 本人资料, synthetic data and a fake model only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import sys
import tempfile
import threading
import unittest
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend"),
                str(ROOT / "plugins/kyf.self-analysis/tests")]
from kyf_plugin import Cancelled, CoreError, ModelError
from self_contracts import SelfError
from self_draft import DEFAULT_DEPTH, FIELD_CHARS, depth, parse
from self_service import Service
from test_self_analysis import Core, dialogue, message

DRAFT = json.dumps({"background": "我在读研，常和朋友约饭。", "style": "回复简短，常用“好呀”。"}, ensure_ascii=False)


class Context:
    def __init__(self):
        self.cancelled = threading.Event()
        self.finished = None
        self.progress_events = []

    def progress(self, **metrics):
        self.progress_events.append(metrics)

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def finish(self, state="done", error=None):
        self.finished = self.finished or (state, error)


class Models:
    def __init__(self, replies=(), context_tokens=32768, configured=True):
        self.replies, self.context_tokens, self.configured = list(replies), context_tokens, configured
        self.requests = []
        self.gate = None
        self.hook = None

    def service(self, refresh=False):
        if not self.configured:
            raise ModelError("api-not-configured", "尚未配置生成模型，请先在设置中保存模型")
        return {"contextTokens": self.context_tokens}

    def generate(self, task, request, step=None, timeout=None):
        record = {"step": step, "system": request["system"], "prompt": json.loads(request["prompt"]),
                  "jsonMode": request["jsonMode"], "succeeded": False,
                  "options": {key: value for key, value in request.items() if key not in ("system", "prompt")}}
        self.requests.append(record)
        if self.hook is not None:
            self.hook(task, record)
        if self.gate is not None:
            self.gate.wait(5)
        task.check()
        reply = self.replies.pop(0) if self.replies else DRAFT
        if isinstance(reply, Exception):
            raise reply
        record["succeeded"] = True
        return {"text": reply}


class Plugin(SimpleNamespace):
    def start_task(self, account, user, *, kind, title, params, handler):
        context = Context()
        self.tasks.append({"account": account, "user": user, "kind": kind, "title": title})

        def run():
            try:
                outcome = handler(context)
                if not context.finished and not context.cancelled.is_set():
                    context.finish(outcome)
            except Cancelled:
                pass
        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return {"id": "task"}, context


class DraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = Core({"friend-a": dialogue(prefix="好呀"), "friend-b": dialogue(topic="帮我看下作业", prefix="晚点看")})
        self.models = Models()
        self.plugin = Plugin(core=self.core, data_dir=Path(self.temp.name), models=self.models, tasks=[], threads=[])
        self.service = Service(self.plugin)
        self.account = self.core.account

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def finish(self):
        for thread in self.plugin.threads:
            thread.join(15)
            self.assertFalse(thread.is_alive(), "synthetic profile task did not finish")
        return self.service.draft_status(self.account)

    def source_requests(self, succeeded=True):
        return [request for request in self.models.requests if request["step"] == "profile-draft" and
                (not succeeded or request["succeeded"])]

    def reconstructed(self, requests=None):
        found = defaultdict(list)
        for request in requests if requests is not None else self.source_requests():
            for item in request["prompt"]["messages"]:
                found[item["id"]].append(item)
        return {key: "".join(item["text"] for item in sorted(parts, key=lambda part: part["part"]))
                for key, parts in found.items()}

    def test_draft_needs_no_reply_library_and_only_returns_unsaved_text(self):
        started = self.service.draft(self.account, {"background": "我是工程师", "style": ""})
        self.assertIn(started["state"], ("running", "done"))
        self.assertEqual(self.plugin.tasks, [{"account": self.account, "user": None, "kind": "background",
                                              "title": "生成本人资料初稿"}])
        status = self.finish()
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(status["result"], json.loads(DRAFT))
        coverage = status["coverage"]
        self.assertEqual((coverage["ownMessages"], coverage["analyzedMessages"], coverage["messagesRead"]), (6, 6, 12))
        self.assertEqual((coverage["conversations"], coverage["readConversations"]), (2, 2))
        self.assertTrue(coverage["catalogComplete"])
        self.assertEqual(coverage["gaps"], [])
        self.assertEqual(self.models.requests[-1]["step"], "profile-draft-final")
        self.assertEqual(self.models.requests[-1]["prompt"]["existing"], {"background": "我是工程师", "style": ""})
        self.assertTrue(all(request["jsonMode"] for request in self.models.requests))
        self.assertEqual(self.service.accounts[self.account][0].counts()["samples"], 0)
        self.assertEqual(self.core.version, 1, "the plugin never saves the Core-owned profile")

    def test_all_private_group_opening_short_and_full_long_messages_reach_model(self):
        long_text = ("完整长消息包含换行和转义\n\"" + chr(92) + "末尾不能丢" * 80) * 12
        self.core.live = {"friend": [message(1, "self", "好"), message(2, "self", "？"), message(3, "self", "🙂"),
                                     message(4, "other", "这是对方的问题"), message(5, "self", long_text)],
                          "group@chatroom": [message(1, "self", "群聊主动开场"), message(2, "other", "群友的话"), message(3, "self", "嗯")]}
        self.models.context_tokens = 4096
        self.service.draft(self.account, {"depth": "deep"})
        status = self.finish()
        self.assertEqual(status["state"], "done", status)
        expected = {json.dumps([user, row["id"]], ensure_ascii=False): row["text"]
                    for user, rows in self.core.live.items() for row in rows if row["side"] == "self"}
        self.assertEqual(self.reconstructed(), expected, "all parts reconstruct the entire source text exactly")
        source = [item for request in self.source_requests() for item in request["prompt"]["messages"]]
        self.assertTrue(any(item["group"] for item in source))
        self.assertTrue(any(item["parts"] > 1 for item in source))
        self.assertTrue(any("这是对方的问题" in json.dumps(item["context"], ensure_ascii=False) for item in source))
        self.assertEqual((status["coverage"]["privateMessages"], status["coverage"]["groupMessages"]), (4, 2))
        self.assertEqual(status["coverage"]["analyzedMessages"], len(expected))
        self.assertEqual(status["coverage"]["analyzedParts"], status["coverage"]["totalParts"])
        self.assertGreater(status["coverage"]["totalParts"], len(expected))

    def test_more_than_1500_messages_and_four_batches_are_all_analyzed_and_merged(self):
        self.core.live = {"friend": [message(index, "self", f"第{index}句") for index in range(1, 1508)]}
        self.models.context_tokens = 4096
        self.service.draft(self.account, {"depth": "basic"})
        status = self.finish()
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(len(self.reconstructed()), 1507)
        self.assertEqual(status["coverage"]["analyzedMessages"], 1507)
        self.assertGreater(len(self.source_requests()), 64, "neither source nor hierarchy page bounds stop the analysis")
        store, _ = self.service.accounts[self.account]
        levels = store.rows("SELECT level,count(*) n,max(end_position) covered FROM profile_draft_notes GROUP BY level ORDER BY level")
        self.assertGreater(levels[-1]["level"], 1, "source summaries require several hierarchy levels")
        for previous, current in zip(levels, levels[1:]):
            self.assertEqual(current["covered"], previous["n"], "every note enters the next merge level")
        self.assertEqual(levels[-1]["n"], 1)
        self.assertEqual(self.models.requests[-1]["prompt"]["coverage"]["analyzedMessages"], 1507)

    def test_three_depths_use_different_batch_sizes_and_deep_rechecks_every_batch(self):
        self.core.live = {"friend": [message(index, "self", f"第{index}句" + "详细表达" * 110) for index in range(1, 41)]}
        sizes = {}
        for selected_depth in ("basic", "standard", "deep"):
            self.models.requests.clear()
            self.service.draft(self.account, {"depth": selected_depth})
            status = self.finish()
            self.assertEqual(status["state"], "done", status)
            sizes[selected_depth] = len(self.source_requests())
            reviews = [request for request in self.models.requests if request["step"] == "profile-draft-review"]
            self.assertEqual(len(reviews), sizes[selected_depth] if selected_depth == "deep" else 0)
            if selected_depth == "deep":
                self.assertEqual([request["prompt"]["messages"] for request in reviews],
                                 [request["prompt"]["messages"] for request in self.source_requests()])
            self.assertTrue(all(request["prompt"]["depth"] == selected_depth for request in self.models.requests))
            self.assertEqual(status["coverage"]["analyzedMessages"], 40)
        self.assertLess(sizes["basic"], sizes["standard"])
        self.assertLess(sizes["standard"], sizes["deep"])

    def test_deep_extract_and_review_receive_the_same_structured_before_and_after_context(self):
        user = "synthetic-group@chatroom"
        rows = [message(index, "other", "群友前文" + str(index), senderName="合成群友甲")
                for index in range(1, 14)]
        rows[1] = message(2, "other", "很长的群友背景" * 110, senderName="合成群友甲")
        rows += [message(14, "self", "我超爱篮球的"),
                 message(15, "other", "你说的是自己吗", senderName="合成群友乙"),
                 message(16, "self", "刚才是玩笑，我其实不喜欢篮球，只是替朋友问"),
                 *[message(index, "other", "群友后文" + str(index), senderName="合成群友乙")
                   for index in range(17, 21)]]
        self.core.live = {user: rows}
        self.service.draft(self.account, {"depth": "deep"})
        status = self.finish()
        self.assertEqual(status["state"], "done", status)
        source = self.source_requests()
        reviews = [request for request in self.models.requests if request["step"] == "profile-draft-review"]
        self.assertEqual([request["prompt"]["messages"] for request in source],
                         [request["prompt"]["messages"] for request in reviews],
                         "review receives the original evidence and metadata, not a reconstructed summary")
        target = next(item for request in source for item in request["prompt"]["messages"]
                      if item["id"] == json.dumps([user, "m14"], ensure_ascii=False))
        context = target["context"]
        self.assertTrue(target["group"])
        self.assertEqual((context["windowMinutes"], context["beforeLimit"], context["afterLimit"]), (60, 12, 6))
        self.assertFalse(context["timeUnknown"])
        self.assertEqual([json.loads(row["id"])[1] for row in context["before"]], [f"m{i}" for i in range(2, 14)])
        self.assertEqual([json.loads(row["id"])[1] for row in context["after"]], [f"m{i}" for i in range(15, 21)])
        self.assertTrue(all(row["speaker"].endswith("合成群友甲") for row in context["before"]),
                        "speaker labels preserve the actual group member name")
        self.assertEqual({row["side"] for row in context["before"]}, {"other"})
        clarification = context["after"][1]
        self.assertEqual((clarification["side"], clarification["text"]),
                         ("self", "刚才是玩笑，我其实不喜欢篮球，只是替朋友问"))
        self.assertIn(clarification["speaker"], ("本人", "我"))
        for row in context["before"] + context["after"]:
            self.assertTrue(row["id"])
            self.assertIsInstance(row["time"], int)
            self.assertIsInstance(row["truncated"], bool)
            self.assertLessEqual(len(row["text"]), 600)
        self.assertTrue(context["before"][0]["truncated"])
        self.assertTrue(context["truncated"], "a shortened contextual utterance remains visibly incomplete")

    def test_small_context_budget_marks_omitted_evidence_and_preserves_the_entire_own_message(self):
        own = "我不喜欢篮球，下面都是转述朋友的话。" + ("完整长正文\n\"\\" * 180) + "最后澄清：只是偶尔陪同，不能当成我的兴趣。"
        rows = [message(index, "other", ("只是在替朋友问，不是本人的爱好。" + str(index)) * 20)
                for index in range(1, 13)]
        rows += [message(13, "self", own)]
        rows += [message(index, "other", ("请保留后文的否定，不要当成稳定兴趣。" + str(index)) * 20)
                 for index in range(14, 20)]
        self.core.live = {"friend": rows}
        self.models.context_tokens = 4096
        self.service.draft(self.account, {"depth": "deep"})
        status = self.finish()
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(list(self.reconstructed().values()), [own], "budgeting partitions rather than cuts the owner's text")
        items = [item for request in self.source_requests() for item in request["prompt"]["messages"]]
        self.assertGreater(len(items), 1)
        self.assertTrue(any(item["context"]["omittedBefore"] + item["context"]["omittedAfter"] > 0 for item in items))
        for item in items:
            context = item["context"]
            self.assertTrue(context["truncated"], "a small input budget cannot silently imply complete context")
            self.assertTrue(context["omittedBefore"] or context["omittedAfter"] or
                            any(row["truncated"] for row in context["before"] + context["after"]))
            for row in context["before"] + context["after"]:
                self.assertTrue(row["id"] and row["speaker"])
                self.assertIn(row["side"], ("self", "other"))
            frame = item["messageFrame"]
            self.assertTrue(frame["head"] and own.startswith(frame["head"]))
            self.assertTrue(frame["tail"] and own.endswith(frame["tail"]))
        reviews = [request["prompt"]["messages"] for request in self.models.requests if request["step"] == "profile-draft-review"]
        self.assertEqual(reviews, [request["prompt"]["messages"] for request in self.source_requests()])

    def test_deep_review_handoff_uses_reviewed_notes_and_keeps_weak_evidence_rules_in_every_stage(self):
        # Scripted replies verify data flow only. They do not measure a model's
        # ability to recognize jokes, attribution or genuine preferences.
        self.core.live = {"friend": [message(1, "other", "你朋友一直很喜欢篮球吧"),
                                     message(2, "self", "我热爱篮球"),
                                     message(3, "self", "开玩笑的，是我朋友喜欢，我只偶尔陪同") ]}
        proposed = {"background": "我热爱篮球。", "style": "用玩笑回答。"}
        reviewed = {"background": "仅提及朋友的篮球爱好，自己偶尔参与；撤回前句，不能判断为明确兴趣。", "style": "会在后文澄清玩笑。"}
        final = {"background": "我偶尔陪朋友打球，目前不足以判断为稳定爱好。", "style": "会用玩笑说法，再补充澄清。"}
        self.models.replies = [json.dumps(value, ensure_ascii=False) for value in (proposed, reviewed, final)]
        self.service.draft(self.account, {"depth": "deep"})
        status = self.finish()
        self.assertEqual(status["state"], "done", status)
        review = next(request for request in self.models.requests if request["step"] == "profile-draft-review")
        self.assertEqual(review["prompt"]["draft"], proposed)
        self.assertIn("开玩笑的，是我朋友喜欢，我只偶尔陪同", json.dumps(review["prompt"]["messages"], ensure_ascii=False))
        self.assertEqual(self.models.requests[-1]["prompt"]["notes"], [reviewed])
        self.assertEqual(status["result"], final)
        for request in self.models.requests:
            for relation in ("仅提及", "偶尔参与", "明确兴趣", "明确特征", "待判断"):
                self.assertIn(relation, request["system"], "all stages preserve the strength of source evidence")

    def test_depth_comes_from_the_plugin_settings_and_a_request_may_name_one(self):
        self.assertEqual(DEFAULT_DEPTH, "deep")
        self.assertEqual(depth(), "deep")
        self.assertEqual(self.service.analysis_settings(self.account, {})["depth"], "deep")
        self.core.plugin_settings = {"analysisDepth": "standard", "replyDepth": "standard", "analysisGrain": "6"}
        self.assertEqual(self.service.analysis_settings(self.account, {})["depth"], "standard")
        for bad in ("", "fast", "DEEP", 3, [], {}):
            with self.subTest(bad=bad):
                with self.assertRaises(SelfError):
                    self.service.draft(self.account, {"depth": bad})
        self.service.draft(self.account, {})
        self.assertEqual(self.finish()["depth"], "standard")
        self.service.draft(self.account, {"depth": "basic"})
        self.assertEqual(self.finish()["depth"], "basic")
        self.core.plugin_settings = {"analysisDepth": "fast", "replyDepth": "standard", "analysisGrain": "6"}
        with self.assertRaises(CoreError) as caught:
            self.service.draft(self.account, {})
        self.assertEqual(caught.exception.code, "invalid-settings", "a broken saved value is never replaced by a default")
        self.assertNotIn("depth", self.service.analysis_settings(self.account, {}))

    def test_model_profile_and_account_preconditions_are_safe(self):
        self.models.configured = False
        with self.assertRaises(SelfError) as caught:
            self.service.draft(self.account, {})
        self.assertEqual(caught.exception.message, "尚未配置回复模型，请先在模型设置中配置")
        with self.assertRaises(SelfError) as caught:
            self.service.draft(self.account, {"background": "长" * (FIELD_CHARS + 1)})
        self.assertEqual(caught.exception.code, "invalid-request")
        self.core.account = "another-account"
        with self.assertRaises(SelfError) as caught:
            self.service.draft_status("synthetic-account")
        self.assertEqual(caught.exception.code, "account-changed")
        self.assertEqual(self.plugin.tasks, [])

    def test_no_own_text_reports_coverage_without_fabricating_a_profile(self):
        self.core.live = {"friend": [message(1, "other", "只收到一条对方消息")]}
        self.service.draft(self.account, {})
        status = self.finish()
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["coverage"]["messagesRead"], 1)
        self.assertEqual(status["coverage"]["ownMessages"], 0)
        self.assertNotIn("result", status)
        self.assertEqual(self.models.requests, [])

    def test_malformed_output_retries_twice_then_reports_without_provider_text(self):
        self.models.replies = ["不是 JSON", "```json\n" + DRAFT + "\n```"]
        self.service.draft(self.account, {})
        self.assertEqual(self.finish()["state"], "done")
        before = len(self.models.requests)
        self.models.replies = ["PRIVATE_PROVIDER_BODY", "{}", "PRIVATE_PROVIDER_BODY"]
        self.service.draft(self.account, {})
        status = self.finish()
        self.assertEqual((status["state"], status["error"]), ("error", "模型返回的内容无法使用，请重试"))
        self.assertEqual(len(self.models.requests), before + 3, "unusable output now gets three tries")
        self.assertNotIn("PRIVATE_PROVIDER_BODY", json.dumps(status, ensure_ascii=False))
        before = len(self.models.requests)
        self.models.replies = [ModelError("auth", "provider said PRIVATE", http_status=401)]
        self.service.draft(self.account, {})
        status = self.finish()
        self.assertEqual(status["error"], "模型服务认证失败，请检查模型设置")
        self.assertEqual(len(self.models.requests), before + 1, "authentication failures are not retried")

    def test_failed_batch_resumes_without_reanalyzing_completed_batches(self):
        self.core.live = {"friend": [message(index, "self", f"第{index}句" + "合成文本" * 20) for index in range(1, 21)]}
        self.models.context_tokens = 4096
        self.models.replies = [DRAFT, ModelError("auth", "PRIVATE_AUTH", http_status=401)]
        self.service.draft(self.account, {"depth": "basic"})
        failed = self.finish()
        self.assertEqual(failed["state"], "error", failed)
        self.assertGreater(failed["coverage"]["analyzedMessages"], 0)
        self.assertLess(failed["coverage"]["analyzedMessages"], 20)
        completed = self.source_requests()
        self.assertEqual(len(completed), 1)
        completed_ids = {item["id"] for item in completed[0]["prompt"]["messages"]}
        before = len(self.models.requests)
        self.service.draft(self.account, {"depth": "basic"})
        final = self.finish()
        self.assertEqual(final["state"], "done", final)
        resumed = [item["id"] for request in self.models.requests[before:] if request["step"] == "profile-draft"
                   for item in request["prompt"]["messages"]]
        self.assertTrue(completed_ids.isdisjoint(resumed))
        self.assertEqual(final["coverage"]["analyzedMessages"], 20)

    def test_deep_review_failure_does_not_mark_unreviewed_batch_complete(self):
        self.models.replies = [DRAFT, ModelError("auth", "PRIVATE_AUTH", http_status=401)]
        self.service.draft(self.account, {"depth": "deep"})
        failed = self.finish()
        self.assertEqual(failed["state"], "error")
        self.assertEqual(failed["coverage"]["analyzedParts"], 0)
        self.service.draft(self.account, {"depth": "deep"})
        self.assertEqual(self.finish()["state"], "done")
        self.assertEqual(len(self.source_requests()), 2, "an unreviewed source batch is read again")

    def test_source_or_epoch_switch_discards_result_and_keeps_local_history(self):
        for change in ("source", "epoch"):
            with self.subTest(change=change):
                original_source = self.core.source
                def switch_source(task, record):
                    if change == "source":
                        self.core.source += "-changed"
                    else:
                        for user in self.core.epochs:
                            self.core.epochs[user] += 1

                self.models.hook = switch_source
                self.service.draft(self.account, {"depth": "basic"})
                failed = self.finish()
                self.assertEqual(failed["state"], "error", failed)
                self.assertTrue(failed.get("restartRequired"))
                self.assertNotIn("result", failed)
                self.models.hook = None
                if change == "source":
                    # Switching sources must not erase the durable old corpus.
                    self.service.draft(self.account, {"depth": "basic"})
                    self.assertEqual(self.finish()["state"], "error")
                    self.core.source = original_source
                before = len(self.models.requests)
                self.service.draft(self.account, {"depth": "basic"})
                recovered = self.finish()
                self.assertEqual(recovered["state"], "done", recovered)
                self.assertEqual(recovered["coverage"]["analyzedMessages"], 6)
                self.assertGreater(len(self.models.requests), before)

    def test_version_two_checkpoints_and_two_line_context_are_rebuilt_instead_of_resumed(self):
        self.service.draft(self.account, {"depth": "deep"})
        self.assertEqual(self.finish()["state"], "done")
        store, _ = self.service.accounts[self.account]
        stale = "STALE_V2_TWO_LINE_CONTEXT_AND_SUMMARY"
        with store.lock, store.db:
            saved = {**store.meta("profileDraftRun"), "version": 2, "state": "cancelled"}
            store._meta("profileDraftRun", saved)
            store._meta("profilePartsReady", True)
            store.db.execute("UPDATE profile_corpus_messages SET context=?", (stale,))
            store.db.execute("UPDATE profile_draft_parts SET payload=?,done=1", (json.dumps({
                "id": "legacy-v2", "text": stale, "context": stale, "part": 1, "parts": 1}),))
            store.db.execute("UPDATE profile_draft_notes SET payload=?", (json.dumps({"background": stale, "style": stale}),))
        self.service.close()
        self.service = Service(self.plugin)
        self.assertEqual(self.service.draft_status(self.account), {"state": "idle"}, "v2 results are not current analysis state")
        self.core.live["friend-a"] += [message(7, "other", "刚才说喜欢只是替朋友转述吧"),
                                       message(8, "self", "对，是朋友的爱好，不是我的")]
        self.models.requests.clear()
        previous_pages = len(self.core.pages)
        self.service.draft(self.account, {"depth": "deep"})
        status = self.finish()
        self.assertEqual(status["state"], "done", status)
        self.assertGreater(len(self.core.pages), previous_pages, "legacy partsReady cannot bypass fresh source context")
        expected = {json.dumps([user, row["id"]], ensure_ascii=False): row["text"]
                    for user, rows in self.core.live.items() for row in rows if row["side"] == "self"}
        self.assertEqual(self.reconstructed(), expected)
        self.assertNotIn(stale, json.dumps(self.models.requests, ensure_ascii=False))
        current_store, _ = self.service.accounts[self.account]
        self.assertEqual(current_store.meta("profileDraftRun")["version"], 3)
        self.assertNotIn(stale, json.dumps(current_store.rows("SELECT payload FROM profile_draft_notes"), ensure_ascii=False))

    def test_pause_resume_survives_service_restart_and_keeps_completed_batches(self):
        self.core.live = {"friend": [message(index, "self", f"第{index}句" + "合成文本" * 20) for index in range(1, 21)]}
        self.models.context_tokens = 4096
        blocked, release = threading.Event(), threading.Event()

        def stop_second_batch(task, record):
            if record["step"] == "profile-draft" and len(self.source_requests(succeeded=False)) == 2:
                blocked.set()
                release.wait(5)

        self.models.hook = stop_second_batch
        self.service.draft(self.account, {"depth": "basic"})
        self.assertTrue(blocked.wait(5))
        paused = self.service.draft_pause(self.account)
        self.assertTrue(paused.get("stopping"))
        release.set()
        cancelled = self.finish()
        self.assertEqual(cancelled["state"], "cancelled")
        completed_ids = set(self.reconstructed())
        self.assertTrue(completed_ids)
        self.service.close()
        self.service = Service(self.plugin)
        restored = self.service.draft_status(self.account)
        self.assertEqual(restored["state"], "cancelled")
        self.assertEqual(restored["coverage"]["analyzedMessages"], len(completed_ids))
        self.assertNotIn("profile", restored)
        self.models.hook = None
        before, read_pages = len(self.models.requests), len(self.core.pages)
        self.service.draft(self.account, {"depth": "basic"})
        final = self.finish()
        self.assertEqual(final["state"], "done", final)
        resumed = {item["id"] for request in self.models.requests[before:] if request["step"] == "profile-draft"
                   for item in request["prompt"]["messages"]}
        self.assertTrue(completed_ids.isdisjoint(resumed))
        self.assertEqual(final["coverage"]["analyzedMessages"], 20)
        self.assertEqual(len(self.core.pages), read_pages, "completed source corpus is reused after restart")

    def test_deep_pause_and_restart_preserve_the_exact_pending_context_and_review_input(self):
        self.core.live = {"friend": [row for index in range(20) for row in (
            message(index * 3 + 1, "other", "这是你自己的爱好吗，还是替朋友转述"),
            message(index * 3 + 2, "self", "只是偶尔陪朋友去，不代表我喜欢，第" + str(index) + "次说明"),
            message(index * 3 + 3, "other", "明白，后面要保留这个否定"))]}
        self.models.context_tokens = 4096
        blocked, release = threading.Event(), threading.Event()
        pending = []

        def stop_second_batch(task, record):
            if record["step"] == "profile-draft" and len(self.source_requests(succeeded=False)) == 2:
                pending.extend(record["prompt"]["messages"])
                blocked.set()
                release.wait(5)

        self.models.hook = stop_second_batch
        self.service.draft(self.account, {"depth": "deep"})
        self.assertTrue(blocked.wait(5))
        self.service.draft_pause(self.account)
        release.set()
        paused = self.finish()
        self.assertEqual(paused["state"], "cancelled", paused)
        self.assertGreater(paused["coverage"]["analyzedParts"], 0)
        self.assertTrue(pending and all(isinstance(item["context"], dict) for item in pending))
        self.service.close()
        self.service = Service(self.plugin)
        self.models.hook = None
        previous, read_pages = len(self.models.requests), len(self.core.pages)
        self.service.draft(self.account, {"depth": "deep"})
        final = self.finish()
        self.assertEqual(final["state"], "done", final)
        resumed = [request for request in self.models.requests[previous:] if request["step"] == "profile-draft"]
        self.assertEqual(resumed[0]["prompt"]["messages"], pending,
                         "the exact packed evidence and missing-context flags survive service restart")
        reviews = [request for request in self.models.requests if request["step"] == "profile-draft-review"]
        self.assertEqual([request["prompt"]["messages"] for request in reviews],
                         [request["prompt"]["messages"] for request in self.source_requests()])
        self.assertEqual(len(self.core.pages), read_pages, "resuming reviewed work does not reread its source snapshot")
        self.assertEqual(final["coverage"]["analyzedMessages"], 20)

    def test_interrupted_long_message_counts_parts_until_every_fragment_is_analyzed(self):
        text = ("逐字保留" + chr(10) + chr(92) + chr(34)) * 1000
        self.core.live = {"friend": [message(1, "self", text)]}
        self.models.context_tokens = 4096
        blocked, release = threading.Event(), threading.Event()
        contexts = []

        def second_part(task, record):
            if task not in contexts:
                contexts.append(task)
            if record["step"] == "profile-draft" and len(self.source_requests(succeeded=False)) == 2:
                blocked.set()
                release.wait(5)

        self.models.hook = second_part
        self.service.draft(self.account, {"depth": "basic"})
        self.assertTrue(blocked.wait(5))
        self.service.draft_pause(self.account)
        release.set()
        paused = self.finish()
        self.assertEqual(paused["state"], "cancelled")
        self.assertEqual(paused["coverage"]["ownMessages"], 1)
        self.assertEqual(paused["coverage"]["analyzedMessages"], 0, "one completed fragment is not a complete message")
        self.assertGreater(paused["coverage"]["analyzedParts"], 0)
        self.assertLess(paused["coverage"]["analyzedParts"], paused["coverage"]["totalParts"])
        self.assertEqual((paused["unit"], paused["done"], paused["total"]), ("条", 0, 1))
        source_progress = [row for row in contexts[0].progress_events if row.get("phase") == "extracting"]
        self.assertTrue(source_progress)
        self.assertTrue(all(row["done"] == row["analyzedMessages"] == 0 and row["total"] == row["ownMessages"] == 1
                            for row in source_progress))
        self.models.hook = None
        self.service.draft(self.account, {"depth": "basic"})
        final = self.finish()
        self.assertEqual(final["state"], "done", final)
        self.assertEqual(final["coverage"]["analyzedMessages"], 1)
        self.assertEqual(final["coverage"]["analyzedParts"], final["coverage"]["totalParts"])
        self.assertEqual((final["analyzedMessages"], final["ownMessages"]), (1, 1))
        self.assertEqual(list(self.reconstructed().values()), [text], "newlines, backslashes and quotes all survive partitioning and resume")

    def test_running_job_is_reused_and_cancelled_on_close(self):
        self.models.gate = threading.Event()
        first = self.service.draft(self.account, {})
        second = self.service.draft(self.account, {"background": "改了"})
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(self.plugin.tasks), 1)
        self.service.close()
        self.models.gate.set()
        for thread in self.plugin.threads:
            thread.join(5)
        status = self.service.drafts[self.account].view()
        self.assertEqual(status["state"], "cancelled")
        self.assertNotIn("result", status)

    def test_uninformative_batch_can_be_read_without_inventing_notes(self):
        self.models.replies = [json.dumps({"background": "", "style": ""}), DRAFT]
        self.service.draft(self.account, {"depth": "basic"})
        status = self.finish()
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(status["coverage"]["analyzedMessages"], 6)
        self.assertEqual(self.models.requests[-1]["prompt"]["notes"], [{"background": "", "style": ""}])

    def test_parse_bounds_fields_and_rejects_empty_drafts(self):
        value = parse(json.dumps({"background": " " + "背" * (FIELD_CHARS + 50), "style": 3}))
        self.assertEqual((len(value["background"]), value["style"]), (FIELD_CHARS, ""))
        self.assertEqual(parse('前言 {"background":"","style":"简短"} 后记')["style"], "简短")
        for raw in ("", "[]", '{"background":"","style":"  "}'):
            with self.assertRaises(SelfError):
                parse(raw)


if __name__ == "__main__":
    unittest.main()
