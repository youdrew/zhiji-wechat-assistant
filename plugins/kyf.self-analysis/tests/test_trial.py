"""聊天试用 reply jobs, synthetic data and a fake model only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend"),
                str(ROOT / "plugins/kyf.self-analysis/tests")]
from kyf_plugin import CoreError, ModelError
from self_contracts import SelfError
from self_persona import PersonaStore
from self_retrieval import Retrieval
from self_service import Service
from self_trial import (MAX_MESSAGES, MAX_TURNS, MESSAGE_CHARS, NO_SAMPLE_COUNTS, TURN_CHARS, bubble_count, conversation,
                        fit, focus, parse, parse_route, query)
from self_values import ValuesStore
from self_timeline_store import TimelineStore
from test_draft import Models, Plugin
from test_self_analysis import BASE, Core, dialogue

REPLY = json.dumps({"messages": ["好呀", "周六中午行不"]}, ensure_ascii=False)


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = Core({"friend-a": dialogue(prefix="好呀"), "friend-b": dialogue(topic="帮我看下作业", prefix="晚点看")})
        self.models = Models([REPLY])
        self.plugin = Plugin(core=self.core, data_dir=Path(self.temp.name), models=self.models, tasks=[], threads=[])
        self.service = Service(self.plugin)
        self.account = self.core.account
        # 条数抽样有专门的用例；其余用例固定两条，和假模型的 REPLY 一致。
        count = mock.patch("self_trial.bubble_count", return_value=2)
        self.count = count.start()
        self.addCleanup(count.stop)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def build(self):
        self.service.build(self.account, "incremental")
        _, worker = self.service.accounts[self.account]
        worker.thread.join(5)

    def finish(self):
        for thread in self.plugin.threads:
            thread.join(5)
        return self.service.reply_status(self.account)

    def test_reply_uses_profile_and_retrieved_own_replies_and_returns_its_sources(self):
        self.build()
        turns = [{"from": "friend", "text": "在吗"}, {"from": "me", "text": "在的"},
                 {"from": "friend", "text": "周末一起吃饭吗"}]
        started = self.service.reply(self.account, {"messages": turns})
        self.assertEqual(started["state"], "running")
        self.assertEqual(self.plugin.tasks, [{"account": self.account, "user": None, "kind": "interactive",
                                              "title": "聊天试用"}])
        status = self.finish()
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(status["result"], {"messages": ["好呀", "周六中午行不"]})
        request = self.models.requests[0]
        self.assertEqual(request["step"], "self-trial")
        self.assertTrue(request["jsonMode"])
        self.assertNotIn("thinking", request["options"], "the reply keeps the model's own reasoning")
        self.assertEqual(request["options"]["maxOutputTokens"], 8192)
        prompt = request["prompt"]
        self.assertEqual(prompt["conversation"], turns)
        self.assertEqual(prompt["profile"], {"background": "不应进入风格上下文的背景", "style": "自然简短"})
        self.assertTrue(prompt["examples"], "retrieved own replies are passed as examples")
        self.assertTrue(all(set(row) == {"time", "context", "reply"} for row in prompt["examples"]))
        self.assertTrue(all(len(row["time"]) == 10 for row in prompt["examples"]))
        self.assertEqual((prompt["facts"], prompt["tags"]), ([], []), "nothing analysed yet, nothing claimed")
        self.assertTrue(any(row["reply"].startswith("好呀") for row in prompt["examples"]))
        self.assertEqual([row["reply"] for row in status["sources"]], [row["reply"] for row in prompt["examples"]])
        self.assertTrue(all(isinstance(row["id"], str) and row["name"] for row in status["sources"]))
        self.assertEqual(self.core.version, 1, "trial never saves the Core-owned profile")

    def test_the_code_sets_the_bubble_count_and_the_model_cannot_exceed_it(self):
        self.build()
        self.models.replies = [json.dumps({"messages": ["哈哈哈", "这题太难了", "你先说"]}, ensure_ascii=False)]
        self.count.return_value = 1
        self.service.reply(self.account, {"messages": [{"from": "friend", "text": "周末一起吃饭吗"}]})
        status = self.finish()
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(self.models.requests[-1]["prompt"]["count"], 1)
        self.assertEqual(status["result"], {"messages": ["哈哈哈 这题太难了 你先说"]})
        # 抽样的是本次检索到的样例本身，所以模型读到的 examples 和条数同源。
        self.assertEqual([row["reply"] for row in self.count.call_args.args[0]],
                         [row["reply"] for row in self.models.requests[-1]["prompt"]["examples"]])

    def test_reply_knows_the_analysed_self_and_retrieves_for_the_last_question_first(self):
        self.build()
        store, _ = self.service.accounts[self.account]
        persona, values = PersonaStore(store), ValuesStore(store)
        with store.lock, store.db:
            store.db.execute("INSERT INTO persona_ai VALUES(?,?,?)",
                             ("mbti", json.dumps({"text": "ENFP", "confidence": "本人自述"}), None))
            store.db.execute("INSERT INTO persona_ai VALUES(?,?,?)",
                             ("big5_e", None, json.dumps({"text": "偏外向，和熟人聊得多", "confidence": "有一定依据"})))
            store.db.execute("INSERT INTO values_ai VALUES(?,?,?)",
                             ("love", None, json.dumps({"text": "看重长期陪伴", "confidence": "把握较低"})))
        persona.edit("big5_o", "喜欢尝试新东西", persona.meta("version", 0))
        _, tags = self.service._tags(self.account)
        tags.edit("add", "健身", "运动健身")
        # The earlier turns are about dinner; the question is about homework.
        turns = [{"from": "friend", "text": "周末一起吃饭吗"}, {"from": "me", "text": "好呀"},
                 {"from": "friend", "text": "帮我看下作业"}]
        with mock.patch.object(Retrieval, "search", autospec=True, side_effect=Retrieval.search) as search:
            self.service.reply(self.account, {"messages": turns})
            self.assertEqual(self.finish()["state"], "done")
        queries = [call.args[1]["query"] for call in search.call_args_list]
        self.assertEqual(queries, ["对方：帮我看下作业", "对方：周末一起吃饭吗\n本人：好呀\n对方：帮我看下作业"])
        prompt = self.models.requests[0]["prompt"]
        self.assertTrue(prompt["examples"][0]["reply"].startswith("晚点看"), prompt["examples"])
        self.assertLessEqual(len(prompt["examples"]), 8)
        self.assertEqual(len({row["context"] + row["reply"] for row in prompt["examples"]}), len(prompt["examples"]))
        self.assertEqual(prompt["facts"], ["MBTI（自述）：ENFP", "性格·开放性（本人补充）：喜欢尝试新东西",
                                           "性格·外向性（推测，有一定依据）：偏外向，和熟人聊得多",
                                           "三观·感情观（推测，把握较低）：看重长期陪伴"])
        self.assertEqual(prompt["tags"], ["运动健身：健身"])

    def test_reply_depth_from_the_plugin_settings_sets_how_much_is_read_and_researched(self):
        self.build()
        turns = [{"from": "friend", "text": "周末一起吃饭吗"}, {"from": "me", "text": "好呀"},
                 {"from": "friend", "text": "帮我看下作业"}]
        self.core.plugin_settings = {"analysisDepth": "deep", "replyDepth": "basic", "analysisGrain": "6"}
        with mock.patch.object(Retrieval, "search", autospec=True, side_effect=Retrieval.search) as search:
            self.service.reply(self.account, {"messages": turns})
            self.assertEqual(self.finish()["state"], "done")
        basic = self.models.requests[-1]
        self.assertEqual((basic["step"], basic["options"]["thinking"], basic["options"]["maxOutputTokens"]),
                         ("self-trial", False, 1024))
        self.assertLessEqual(len(basic["prompt"]["examples"]), 4)
        self.assertTrue(all(call.args[1]["limit"] == 4 for call in search.call_args_list))
        self.assertEqual(self.service.reply_status(self.account)["depth"], "basic")

        # 深度: the model names what to look up, each lookup is searched, then the reply thinks.
        self.core.plugin_settings = {"analysisDepth": "deep", "replyDepth": "deep", "analysisGrain": "6"}
        self.models.replies = [json.dumps({"queries": ["晚点看", " 晚点看 ", "作业", 3]}, ensure_ascii=False), REPLY]
        before = len(self.models.requests)
        with mock.patch.object(Retrieval, "search", autospec=True, side_effect=Retrieval.search) as search:
            self.service.reply(self.account, {"messages": turns})
            status = self.finish()
        self.assertEqual(status["state"], "done", status)
        plan, final = self.models.requests[before:]
        self.assertEqual((plan["step"], plan["options"]["thinking"]), ("self-trial-plan", False))
        self.assertEqual(plan["prompt"]["conversation"], turns)
        self.assertEqual(status["queries"], ["晚点看", "作业"], "planned lookups are cleaned and deduplicated")
        queries = [call.args[1]["query"] for call in search.call_args_list]
        self.assertEqual(queries, ["对方：帮我看下作业", "晚点看", "作业", "对方：周末一起吃饭吗\n本人：好呀\n对方：帮我看下作业"])
        self.assertEqual(final["step"], "self-trial")
        self.assertNotIn("thinking", final["options"])
        self.assertLessEqual(len(final["prompt"]["examples"]), 16)
        self.assertEqual(len({row["id"] for row in status["sources"]}), len(status["sources"]))

        # A plan the model cannot produce only narrows the search; the reply still comes.
        self.models.replies = [ModelError("invalid-output", "坏"), ModelError("invalid-output", "坏"),
                               ModelError("invalid-output", "坏"), REPLY]
        self.service.reply(self.account, {"messages": turns})
        status = self.finish()
        self.assertEqual((status["state"], status["queries"]), ("done", []), status)

        with self.assertRaises(SelfError) as caught:
            self.service.reply(self.account, {"messages": turns, "depth": "extreme"})
        self.assertEqual(caught.exception.code, "invalid-request")

    def test_auto_routes_depth_and_researches_the_timeline_for_personal_questions(self):
        self.build()
        store, _ = self.service.accounts[self.account]
        TimelineStore(store)  # create the timeline tables for the synthetic store
        with store.lock, store.db:
            store.db.execute("INSERT INTO timeline_tracks(name,builtin,position,visible) VALUES('经历',1,0,1)")
            store.db.execute("INSERT INTO timeline_events(id,title,track,start_ms,precision) "
                             "VALUES('e1','开始备考雅思','经历',?,'day')", (BASE + 5000,))
        self.core.plugin_settings = {"analysisDepth": "deep", "replyDepth": "auto", "analysisGrain": "6"}
        turns = [{"from": "friend", "text": "周末一起吃饭吗"}, {"from": "me", "text": "好呀"},
                 {"from": "friend", "text": "你未来的打算是什么？"}]
        route = json.dumps({"depth": "deep", "research": {"events": ["e1", "ghost"], "periods": [],
                                                          "queries": ["备考雅思", " 最近打算 "]}}, ensure_ascii=False)
        self.models.replies = [route, REPLY]
        before = len(self.models.requests)
        with mock.patch.object(Retrieval, "search", autospec=True, side_effect=Retrieval.search) as search:
            self.service.reply(self.account, {"messages": turns})
            status = self.finish()
        self.assertEqual(status["state"], "done", status)
        routing, final = self.models.requests[before:]
        self.assertEqual((routing["step"], routing["jsonMode"], routing["options"]["thinking"],
                          routing["options"]["maxOutputTokens"]), ("self-trial-route", True, False, 512))
        self.assertEqual(routing["prompt"]["conversation"], turns)
        self.assertEqual([row["id"] for row in routing["prompt"]["timeline"]["events"]], ["e1"],
                         "only catalogued ids exist; the ghost id is dropped later")
        self.assertEqual((final["step"], final["options"]["maxOutputTokens"]), ("self-trial", 8192))
        facts = final["prompt"]["facts"]
        self.assertEqual(len(facts), 1)
        self.assertTrue(facts[0].startswith("时间线·") and facts[0].endswith("：开始备考雅思"), facts)
        self.assertEqual((status["depth"], status["research"], status["queries"]), ("deep", 1, ["备考雅思", "最近打算"]))
        self.assertEqual([call.args[1]["query"] for call in search.call_args_list],
                         ["对方：你未来的打算是什么？", "备考雅思", "最近打算",
                          "对方：周末一起吃饭吗\n本人：好呀\n对方：你未来的打算是什么？"])

        # 一眼能答的问题不调研：路由选 basic，时间线不进 facts，检索只有聚焦查询。
        self.models.replies = [json.dumps({"depth": "basic", "research": {"events": [], "periods": [], "queries": []}},
                                          ensure_ascii=False), REPLY]
        self.service.reply(self.account, {"messages": [{"from": "friend", "text": "你的账号密码是多少啊"}]})
        status = self.finish()
        self.assertEqual((status["state"], status["depth"], status["research"]), ("done", "basic", 0), status)
        final = self.models.requests[-1]
        self.assertEqual(final["prompt"]["facts"], [])
        self.assertLessEqual(len(final["prompt"]["examples"]), 4)

        # 评测防泄漏：beforeTimeMs 同样截断路由目录，选中的条目随之消失。
        self.models.replies = [route, REPLY]
        self.service.reply(self.account, {"messages": turns, "beforeTimeMs": BASE + 1000})
        status = self.finish()
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(self.models.requests[-2]["prompt"]["timeline"], {"events": [], "periods": []})
        self.assertEqual((status["depth"], status["research"]), ("deep", 0))

        # 路由失败只降级：按标准档、不调研地直接回。
        self.models.replies = [ModelError("invalid-output", "坏")] * 3 + [REPLY]
        self.service.reply(self.account, {"messages": [{"from": "friend", "text": "你未来的打算是什么？"}]})
        status = self.finish()
        self.assertEqual((status["state"], status["depth"], status["research"]), ("done", "standard", 0), status)

    def test_parse_route_cleans_the_router_answer(self):
        route = parse_route('前言 {"depth":"turbo","research":{"events":["a",3],"periods":[],'
                            '"queries":["  最近 ", "最近", 5]}} 后记')
        self.assertEqual(route, {"depth": "standard", "events": ["a"], "periods": [], "queries": ["最近"]})

    def test_a_small_context_window_drops_the_last_examples_first(self):
        self.build()
        self.models.context_tokens = 4096
        self.service.reply(self.account, {"messages": [{"from": "friend", "text": "周末一起吃饭吗"}]})
        self.assertEqual(self.finish()["state"], "done")
        request = self.models.requests[-1]
        self.assertEqual(request["options"]["maxOutputTokens"], 1365, "the output cap fits the window")
        self.assertLessEqual(len(request["system"]) + len(json.dumps(request["prompt"], ensure_ascii=False)),
                             4096 - 1365 - 1000)
        self.models.context_tokens, self.models.replies = 2048, [REPLY]  # too small to be real: the usual window is assumed
        self.service.reply(self.account, {"messages": [{"from": "friend", "text": "周末一起吃饭吗"}]})
        self.assertEqual(self.finish()["state"], "done")
        self.assertEqual(self.models.requests[-1]["options"]["maxOutputTokens"], 8192)

    def test_preconditions_fail_fast_without_starting_a_task(self):
        friend = {"messages": [{"from": "friend", "text": "在吗"}]}
        with self.assertRaises(SelfError) as caught:
            self.service.reply(self.account, friend)
        self.assertEqual(caught.exception.code, "no-samples")
        self.build()
        self.models.configured = False
        with self.assertRaises(SelfError) as caught:
            self.service.reply(self.account, friend)
        self.assertEqual(caught.exception.message, "尚未配置回复模型，请先在模型设置中配置")
        for bad in ({}, {"messages": []}, {"messages": [{"from": "me", "text": "我先说"}]},
                    {"messages": [{"from": "friend", "text": "  "}]},
                    {"messages": [{"from": "stranger", "text": "嗨"}]},
                    {"messages": [{"from": "friend", "text": "长" * (TURN_CHARS + 1)}]},
                    {"messages": [{"from": "friend", "text": "嗨"}] * (MAX_TURNS + 1)}):
            with self.assertRaises(SelfError) as caught:
                self.service.reply(self.account, bad)
            self.assertEqual(caught.exception.code, "invalid-request", bad)
        self.assertEqual(self.plugin.tasks, [])
        self.assertEqual(self.service.reply_status(self.account), {"state": "idle"})

    def test_a_gateway_blip_before_start_is_retried_and_a_lasting_one_is_named(self):
        self.build()
        friend = {"messages": [{"from": "friend", "text": "周末一起吃饭吗"}]}
        ready, logs = self.models.service, []
        self.plugin.log = lambda message, level="info": logs.append((level, message))
        # Core's model listing answers 503 when the gateway's /models probe misses its deadline.
        failures = [CoreError(503, "upstream-unavailable", "统一模型网关暂时不可用或正在切换模型")]

        def flaky(refresh=False):
            if failures:
                raise failures.pop()
            return ready(refresh)
        self.models.service = flaky
        with mock.patch("self_model.time.sleep") as sleep:
            self.service.reply(self.account, friend)
            self.assertEqual(self.finish()["state"], "done")
            sleep.assert_called_once()
            self.models.service = lambda refresh=False: (_ for _ in ()).throw(
                CoreError(503, "upstream-unavailable", "统一模型网关暂时不可用或正在切换模型"))
            with self.assertRaises(SelfError) as caught:
                self.service.reply(self.account, friend)
        self.assertEqual(caught.exception.code, "upstream-unavailable")
        self.assertEqual(caught.exception.message, "模型服务连接失败或繁忙，请稍后重试")
        self.assertEqual(logs, [("error", "model service check failed: upstream-unavailable")])
        self.assertEqual(len(self.plugin.tasks), 1, "the lasting failure starts no task")

    def test_failures_are_short_and_a_running_reply_is_reused_then_cancelled_on_close(self):
        self.build()
        friend = {"messages": [{"from": "friend", "text": "周末一起吃饭吗"}]}
        self.models.replies = [ModelError("auth", "provider said PRIVATE", http_status=401)]
        self.service.reply(self.account, friend)
        status = self.finish()
        self.assertEqual((status["state"], status["error"]), ("error", "模型服务认证失败，请检查模型设置"))
        self.assertNotIn("PRIVATE", json.dumps(status, ensure_ascii=False))
        self.models.replies, self.models.gate = [REPLY], threading.Event()
        first = self.service.reply(self.account, friend)
        second = self.service.reply(self.account, {"messages": [{"from": "friend", "text": "换个问题"}]})
        self.assertEqual(first["id"], second["id"])
        self.service.close()
        self.models.gate.set()
        status = self.finish()
        self.assertEqual(status["state"], "cancelled")
        self.assertNotIn("result", status)

    def test_eval_guards_default_to_unchanged_retrieval_and_pass_through(self):
        self.build()
        turns = [{"from": "friend", "text": "周末一起吃饭吗"}]
        # 不传评测参数时行为不变：检索请求不带截断或排除键。
        with mock.patch.object(Retrieval, "search", autospec=True, side_effect=Retrieval.search) as search:
            self.service.reply(self.account, {"messages": turns})
            self.assertEqual(self.finish()["state"], "done")
        self.assertTrue(search.call_args_list)
        for call in search.call_args_list:
            self.assertNotIn("beforeTimeMs", call.args[1])
            self.assertNotIn("excludeMessageIds", call.args[1])
        status = self.service.reply_status(self.account)
        self.assertTrue(status["sources"])
        for bad in ({"beforeTimeMs": "x"}, {"beforeTimeMs": 0}, {"excludeSampleIds": "m2"},
                    {"excludeMessageIds": [1]}, {"excludeSampleIds": ["a"] * 501}):
            with self.assertRaises(SelfError) as caught:
                self.service.reply(self.account, {"messages": turns, **bad})
            self.assertEqual(caught.exception.code, "invalid-request")

    def test_eval_before_time_hides_samples_from_on_or_after_the_reply(self):
        self.build()
        turns = [{"from": "friend", "text": "周末一起吃饭吗"}]
        with mock.patch.object(Retrieval, "search", autospec=True, side_effect=Retrieval.search) as search:
            self.service.reply(self.account, {"messages": turns, "beforeTimeMs": BASE + 3000})
            self.assertEqual(self.finish()["state"], "done")
        self.assertTrue(search.call_args_list)
        for call in search.call_args_list:
            self.assertEqual(call.args[1].get("beforeTimeMs"), BASE + 3000)
        status = self.service.reply_status(self.account)
        self.assertTrue(status["sources"], "the earliest exchange stays visible")
        # 合成对话的回复时间依次是 BASE+2s/4s/6s，截断后只剩每段对话的第一条。
        self.assertTrue(all(row["reply"].endswith("0") for row in status["sources"]), status["sources"])

    def test_eval_exclude_sample_ids_drops_that_sample_from_the_examples(self):
        self.build()
        turns = [{"from": "friend", "text": "周末一起吃饭吗"}]
        self.service.reply(self.account, {"messages": turns})
        self.assertEqual(self.finish()["state"], "done")
        top = self.service.reply_status(self.account)["sources"][0]["id"]
        self.models.replies = [REPLY]
        with mock.patch.object(Retrieval, "search", autospec=True, side_effect=Retrieval.search) as search:
            self.service.reply(self.account, {"messages": turns, "excludeSampleIds": [top],
                                              "excludeMessageIds": ["m2", "m1"]})
            self.assertEqual(self.finish()["state"], "done")
        for call in search.call_args_list:
            self.assertEqual(call.args[1].get("excludeMessageIds"), ["m1", "m2"], "ids are passed through sorted")
        self.assertNotIn(top, {row["id"] for row in self.service.reply_status(self.account)["sources"]})

    def test_query_parse_and_conversation_bounds(self):
        turns = conversation({"messages": [{"from": "friend", "text": " 在吗 "}, {"from": "me", "text": "在"},
                                           {"from": "friend", "text": "吃饭吗"}]})
        self.assertEqual(turns[0]["text"], "在吗")
        self.assertEqual(query(turns), "对方：在吗\n本人：在\n对方：吃饭吗")
        self.assertEqual(focus(turns), turns[2:])
        self.assertEqual(focus(turns[:1] + turns[2:]), [turns[0], turns[2]], "consecutive friend messages stay together")
        self.assertEqual(parse('前言 {"messages":["好呀"," ",3,"走起"]} 后记'), ["好呀", "走起"])
        self.assertEqual(parse('{"messages":"好"}'), ["好"])
        long = parse(json.dumps({"messages": ["长" * (MESSAGE_CHARS + 9)] * (MAX_MESSAGES + 2)}))
        self.assertEqual((len(long), len(long[0])), (MAX_MESSAGES, MESSAGE_CHARS))
        for raw in ("", "[]", '{"messages":[]}', '{"reply":"好"}'):
            with self.assertRaises(SelfError):
                parse(raw)

    def test_bubble_count_follows_the_real_samples_not_a_fixed_shape(self):
        rows = [{"reply": "好"}, {"reply": "哈哈\n行"}, {"reply": "一\n二\n三\n四"}, {"reply": "\n".join("八" * 8)}]
        self.assertEqual({bubble_count(rows) for _ in range(400)}, {1, 2, 4, MAX_MESSAGES})
        self.assertEqual(bubble_count(rows[:1]), 1)
        self.assertEqual({bubble_count([]) for _ in range(200)}, set(NO_SAMPLE_COUNTS))

    def test_fit_merges_surplus_and_keeps_shorter_replies(self):
        self.assertEqual(fit(["一", "二", "三"], 2), ["一", "二 三"])
        self.assertEqual(fit(["一", "二", "三"], 1), ["一 二 三"])
        self.assertEqual(fit(["一"], 3), ["一"])
        self.assertEqual(len(fit(["长" * MESSAGE_CHARS, "短"], 1)[0]), MESSAGE_CHARS)


if __name__ == "__main__":
    unittest.main()
