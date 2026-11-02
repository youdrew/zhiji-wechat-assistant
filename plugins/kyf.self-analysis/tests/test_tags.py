"""本人标签 analysis, synthetic chats (private and group) and a rule-based fake model only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend"),
                str(ROOT / "plugins/kyf.self-analysis/tests")]
from kyf_plugin import Cancelled, ModelError
from self_contracts import SelfError
from self_service import Service
from self_store import Store
from self_tags import candidates, parse_merge, parse_review, parse_tags
from test_self_analysis import BASE, Core, message

DAY = 86_400_000
RULES = (("打球", "打篮球", "运动健身"), ("篮球", "篮球", "运动健身"), ("象棋", "象棋", "游戏棋牌"),
         ("文明6", "战略类游戏", "游戏棋牌"), ("香菜", "不吃香菜", "美食饮食"))


def model_tag(name, category, evidence):
    return {"name": name, "category": category, "evidence": evidence}


def support(index, quote, relation="mention", reason="合成测试证据，按原文保留关系"):
    return {"i": index, "relation": relation, "quote": quote, "reason": reason}


def answer(*tags):
    return json.dumps({"tags": list(tags)}, ensure_ascii=False)


def model_item(index, text, context="", after="", truncated=False):
    return {"i": index, "chat": "合成朋友", "ctx": context, "me": text, "after": after, "truncated": truncated}


def chats():
    return {
        "friend-a": [message(1, "other", "周末干嘛呢"), message(2, "self", "我周末一般去打球"),
                     message(3, "other", "可以啊"), message(4, "self", "好的"),
                     message(5, "self", "我超喜欢篮球的", time=BASE + 2 * DAY)],
        "group-1@chatroom": [message(1, "other", "谁来下象棋", senderName="张三", time=BASE + DAY),
                             message(2, "self", "我来！象棋我最擅长了", time=BASE + DAY + 1000),
                             message(3, "self", "[图片]", kind="image", time=BASE + DAY + 2000),
                             message(4, "other", "我喜欢篮球", senderName="李四", time=BASE + DAY + 3000)],
        "friend-b": [message(1, "self", "最近又在玩文明6玩到凌晨", time=BASE + 3 * DAY),
                     message(2, "self", "我也爱打球的", time=BASE + 4 * DAY)],
    }


class GroupCore(Core):
    def history_conversations(self, account, after=None, limit=500):
        page = super().history_conversations(account, after, limit)
        for row in page["conversations"]:
            row["isGroup"] = row["user"].endswith("@chatroom")
        return page


class Models:
    """Deterministic contract fixture; it does not evaluate a real model's language understanding."""
    def __init__(self):
        self.requests, self.replies, self.gate, self.configured = [], [], None, True

    def service(self, refresh=False):
        if not self.configured:
            raise ModelError("api-not-configured", "尚未配置生成模型，请先在设置中保存模型")
        return {"contextTokens": 32768}

    def generate(self, task, request, step=None, timeout=None):
        prompt = json.loads(request["prompt"])
        self.requests.append({"step": step, "system": request["system"], "prompt": prompt})
        if self.gate is not None:
            self.gate.wait(5)
        task.check()
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return {"text": reply}
        if step == "self-tags":
            tags = {}
            for item in prompt["messages"]:
                for word, name, category in RULES:
                    if word in item["me"]:
                        relation = "interest" if any(word in item["me"] for word in ("喜欢", "爱打球")) else \
                            "trait" if any(word in item["me"] for word in ("擅长", "不吃香菜")) else "occasional"
                        tags.setdefault(name, model_tag(name, category, []))["evidence"].append(
                            support(item["i"], item["me"], relation))
            return {"text": json.dumps({"tags": list(tags.values())}, ensure_ascii=False)}
        if step == "self-tags-review":
            return {"text": json.dumps({"tags": prompt["tags"]}, ensure_ascii=False)}
        names = {tag["name"] for tag in prompt["tags"]}
        merge = [{"into": "篮球", "from": ["打篮球"]}] if {"篮球", "打篮球"} <= names else []
        return {"text": json.dumps({"merge": merge}, ensure_ascii=False)}


class Context:
    def __init__(self):
        self.cancelled, self.finished, self.reports = threading.Event(), None, []

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def progress(self, **values):
        self.check()
        self.reports.append(values)

    def finish(self, state="done", error=None):
        self.finished = self.finished or (state, error)


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


class TagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = GroupCore(chats())
        self.models = Models()
        self.plugin = Plugin(core=self.core, data_dir=Path(self.temp.name), models=self.models, tasks=[], threads=[])
        self.service = Service(self.plugin)
        self.account = self.core.account

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def analyze(self):
        self.service.tags_analyze(self.account)
        for thread in self.plugin.threads:
            thread.join(5)
        return self.service.tags(self.account)

    def tag(self, status, name):
        return next((tag for tag in status["tags"] if tag["name"] == name), None)

    def test_reads_own_messages_from_private_and_group_chats_and_keeps_evidence(self):
        status = self.analyze()
        self.assertEqual(status["run"]["state"], "complete", status["run"])
        self.assertEqual(self.plugin.tasks[0]["kind"], "background")
        self.assertEqual((status["counts"]["conversations"], status["counts"]["groups"]), (3, 1))
        sent = [item["me"] for request in self.models.requests if request["step"] == "self-tags"
                for item in request["prompt"]["messages"]]
        self.assertEqual(sorted(sent), sorted(["我周末一般去打球", "我超喜欢篮球的", "我来！象棋我最擅长了",
                                               "最近又在玩文明6玩到凌晨", "我也爱打球的", "好的"]),
                         "short typed replies reach the model with context; other people's text and media are not sources")
        group = next(item for request in self.models.requests if request["step"] == "self-tags"
                     for item in request["prompt"]["messages"] if "象棋" in item["me"])
        self.assertEqual((group["chat"], group["ctx"]), ("合成朋友group-1@chatroom（群）", "张三：谁来下象棋"))
        self.assertEqual([(tag["name"], tag["category"], tag["level"]) for tag in status["tags"]],
                         [("篮球", "运动健身", "strong"), ("象棋", "游戏棋牌", "strong"), ("战略类游戏", "游戏棋牌", "possible")])
        self.assertTrue(any(request["step"] == "self-tags-review" for request in self.models.requests))
        basketball = self.tag(status, "篮球")
        self.assertEqual((basketball["mentions"], basketball["chats"], basketball["days"]), (3, 2, 3),
                         "打篮球 was merged into 篮球 with all of its evidence")
        evidence = self.service.tag_evidence(self.account, {"name": "篮球"})
        self.assertEqual(evidence["total"], 3)
        self.assertEqual([item["text"] for item in evidence["items"]], ["我也爱打球的", "我超喜欢篮球的", "我周末一般去打球"])
        self.assertEqual(evidence["items"][1]["context"], "", "context does not cross a two-day gap")
        self.assertTrue(all(item["quote"] in item["text"] and item["reason"] for item in evidence["items"]))
        self.assertEqual(self.core.version, 1, "tags never write the Core profile")

    def test_later_runs_read_only_new_messages_and_edits_survive_reanalysis(self):
        self.analyze()
        self.service.tag_edit(self.account, {"action": "delete", "name": "象棋"})
        self.service.tag_edit(self.account, {"action": "confirm", "name": "战略类游戏"})
        status = self.service.tag_edit(self.account, {"action": "add", "name": "#夜猫子 ", "category": "性格习惯"})
        self.assertIsNone(self.tag(status, "象棋"))
        self.assertEqual(self.tag(status, "战略类游戏")["level"], "confirmed")
        self.assertEqual({key: self.tag(status, "夜猫子")[key] for key in ("category", "level", "manual", "mentions")},
                         {"category": "性格习惯", "level": "confirmed", "manual": True, "mentions": 0})
        requests = len(self.models.requests)
        self.core.live["group-1@chatroom"].append(message(5, "self", "今晚继续下象棋", time=BASE + 5 * DAY))
        self.core.live["friend-a"].append(message(6, "self", "我从来不吃香菜", time=BASE + 5 * DAY))
        status = self.analyze()
        extracted = [item["me"] for request in self.models.requests[requests:] if request["step"] == "self-tags"
                     for item in request["prompt"]["messages"]]
        self.assertEqual(sorted(extracted), sorted(["我从来不吃香菜", "今晚继续下象棋"]))
        known = next(request["prompt"]["known"] for request in self.models.requests[requests:] if request["step"] == "self-tags")
        self.assertIn("篮球", known)
        self.assertNotIn("象棋", known, "deleted tags are not suggested back to the model")
        self.assertIsNone(self.tag(status, "象棋"), "a deleted tag stays deleted after re-analysis")
        self.assertEqual(self.tag(status, "不吃香菜")["category"], "美食饮食")
        self.assertEqual(self.tag(status, "战略类游戏")["level"], "confirmed")
        status = self.service.tag_edit(self.account, {"action": "reset", "name": "象棋"})
        self.assertEqual(self.tag(status, "象棋")["mentions"], 2, "restoring a deleted tag brings back its evidence")
        requests = len(self.models.requests)
        self.analyze()
        self.assertEqual(self.models.requests[requests:], [], "nothing new means no model calls")
        for bad in ({"action": "add", "name": "夜猫子", "category": "不存在"}, {"action": "add", "name": " "},
                    {"action": "rename", "name": "篮球"}, {"action": "add", "name": "长" * 21, "category": "其他"}):
            with self.assertRaises(SelfError) as caught:
                self.service.tag_edit(self.account, bad)
            self.assertEqual(caught.exception.code, "invalid-request", bad)

    def test_merging_never_moves_an_edited_tag_under_another_name(self):
        self.core.live = {"friend-a": chats()["friend-a"]}
        self.core.live["friend-a"][1]["text"] = "我喜欢打球，周末一般去"
        extraction = answer(model_tag("打篮球", "运动健身", [support(0, "我喜欢打球，周末一般去", "interest")]),
                            model_tag("篮球", "运动健身", [support(2, "我超喜欢篮球的", "interest")]))
        self.models.replies = [extraction, extraction, json.dumps({"merge": []})]
        initial = self.analyze()
        self.assertEqual(initial["run"]["state"], "complete", initial["run"])
        self.service.tag_edit(self.account, {"action": "confirm", "name": "打篮球"})
        self.core.live["friend-a"].append(message(6, "self", "篮球周末继续", time=BASE + 5 * DAY))
        status = self.analyze()
        merge = [request for request in self.models.requests if request["step"] == "self-tags-merge"][-1]
        self.assertEqual({tag["name"] for tag in merge["prompt"]["tags"]}, {"打篮球", "篮球"})
        self.assertEqual(self.tag(status, "打篮球")["level"], "confirmed")
        self.assertEqual(self.tag(status, "打篮球")["mentions"], 1)

    def test_pause_keeps_progress_and_the_next_run_continues_without_rereading_chats(self):
        self.models.gate = threading.Event()
        started = self.service.tags_analyze(self.account)
        self.assertEqual(started["run"]["state"], "running")
        for _ in range(100):
            if self.models.requests:
                break
            threading.Event().wait(0.02)
        paused = self.service.tags_pause(self.account)
        self.assertTrue(paused["run"]["stopping"])
        self.models.gate.set()
        for thread in self.plugin.threads:
            thread.join(5)
        status = self.service.tags(self.account)
        self.assertEqual(status["run"]["state"], "paused", status["run"])
        self.assertGreater(status["counts"]["pendingMessages"], 0)
        pages = len(self.core.pages)
        self.models.gate = None
        status = self.analyze()
        self.assertEqual(status["run"]["state"], "complete")
        self.assertEqual(len(self.core.pages), pages, "a resumed run does not read chats again")
        self.assertEqual(status["counts"]["pendingMessages"], 0)

    def test_failures_are_short_and_a_broken_chat_does_not_stop_the_others(self):
        self.models.configured = False
        with self.assertRaises(SelfError) as caught:
            self.service.tags_analyze(self.account)
        self.assertEqual(caught.exception.message, "尚未配置回复模型，请先在模型设置中配置")
        self.assertEqual(self.plugin.tasks, [])
        self.models.configured = True
        self.core.fail.add("friend-b")
        self.models.replies = ["PRIVATE_PROVIDER_BODY", "还是不是 JSON", "PRIVATE_PROVIDER_BODY"]
        status = self.analyze()
        self.assertEqual((status["run"]["state"], status["run"]["error"]), ("failed", "模型返回的内容无法使用，请重试"))
        self.assertNotIn("PRIVATE", json.dumps(status, ensure_ascii=False))
        self.assertEqual(status["counts"]["failedConversations"], 1)
        self.core.fail.clear()
        status = self.analyze()
        self.assertEqual(status["run"]["state"], "complete", status["run"])
        self.assertEqual(status["counts"]["failedConversations"], 0, "a later run reads the failed chat again")
        self.assertIsNotNone(self.tag(status, "战略类游戏"))

    def test_a_new_source_clears_read_messages_but_keeps_edits(self):
        self.analyze()
        self.service.tag_edit(self.account, {"action": "delete", "name": "篮球"})
        self.core.source = "another-source"
        status = self.analyze()
        self.assertEqual(status["run"]["state"], "complete", status["run"])
        self.assertIsNone(self.tag(status, "篮球"))
        self.assertEqual(self.service.tag_evidence(self.account, {"name": "象棋"})["total"], 1)

    def test_candidates_parsing_and_bounds(self):
        rows = [message(1, "other", "你不是喜欢下棋吗", senderName="王五"), message(2, "self", "对啊"),
                message(3, "self", "<appmsg>分享</appmsg>"),
                message(5, "self", "转发的长文章内容", raw=json.dumps({"forwarded": True})),
                message(6, "self", "😀😀😀😀"), message(7, "self", "周末下棋去")]
        found, tail = candidates(rows, [{"line": "我：上一页的话", "time": BASE}], "g@chatroom", True)
        self.assertEqual([row["text"] for row in found], ["对啊", "周末下棋去"],
                         "short acknowledgements are candidates, forwarded markup and emoji-only rows are not")
        self.assertEqual(found[0]["id"], "g@chatroom/m2")
        self.assertIn("王五：你不是喜欢下棋吗", found[0]["context"])
        self.assertNotIn("分享", found[1]["context"])
        self.assertNotIn("转发的长文章", found[1]["context"])
        self.assertEqual([row["line"] for row in tail][-2:], ["我：😀😀😀😀", "我：周末下棋去"])
        items = [model_item(0, "我喜欢篮球"), model_item(1, "朋友问我篮球比分")]
        parsed = parse_tags(items)(answer(model_tag(" #篮球 ", "运动健身", [
            support(0, "喜欢篮球", "interest"), support(1, "篮球比分", "mention")])))
        self.assertEqual(parsed[0]["name"], "篮球")
        self.assertEqual([(item["i"], item["relation"]) for item in parsed[0]["evidence"]],
                         [(0, "interest"), (1, "mention")])
        self.assertEqual(parse_tags(items)('{"tags":[]}'), [])
        merge = parse_merge({"篮球", "打篮球", "NBA"}, {"打篮球", "NBA"})
        self.assertEqual(merge('{"merge":[{"into":"篮球","from":["打篮球","篮球","足球"]},{"into":"足球","from":["NBA"]}]}'),
                         [("篮球", ["打篮球"])])
        for raw in ("", "[]", '{"tags":"篮球"}'):
            with self.assertRaises(SelfError):
                parse_tags(items)(raw)

    def test_frequency_across_chats_and_days_never_becomes_interest(self):
        self.core.live = {
            "friend-a": [message(index + 1, "self", f"篮球比赛比分是{index}分", time=BASE + index * DAY)
                         for index in range(4)],
            "friend-b": [message(1, "self", "今天篮球比赛几点开始", time=BASE + 5 * DAY)],
        }
        texts = [row["text"] for user in sorted(self.core.live) for row in self.core.live[user]]
        extraction = answer(model_tag("篮球", "运动健身", [support(index, text, "mention")
                                                                 for index, text in enumerate(texts)]))
        self.models.replies = [extraction, extraction]
        status = self.analyze()
        tag = self.tag(status, "篮球")
        self.assertEqual((tag["mentions"], tag["chats"], tag["days"]), (5, 2, 5))
        self.assertEqual((tag["level"], tag["relation"]), ("possible", "mention"))

    def test_occasional_participation_stays_possible_and_clear_interest_is_strong(self):
        cases = [("昨天陪同事打了一次篮球", "occasional", "possible"),
                 ("我喜欢篮球，平时一直会主动去打", "interest", "strong")]
        for text, relation, level in cases:
            with self.subTest(relation=relation):
                self.core.source = "source-" + relation
                self.core.live = {"friend-a": [message(1, "self", text)]}
                self.core.projected.clear()
                extraction = answer(model_tag("篮球", "运动健身", [support(0, text, relation)]))
                self.models.replies = [extraction, extraction]
                tag = self.tag(self.analyze(), "篮球")
                self.assertEqual((tag["relation"], tag["level"]), (relation, level))

    def test_identity_habits_and_negative_preferences_remain_distinct_from_interest(self):
        rows = [message(1, "self", "我的职业是软件工程师"),
                message(2, "self", "我每天凌晨两点才睡觉"), message(3, "self", "我从来不喝咖啡")]
        self.core.live = {"friend-a": rows}
        extraction = answer(model_tag("软件工程师", "身份生活", [support(0, rows[0]["text"], "trait")]),
                            model_tag("夜猫子", "性格习惯", [support(1, rows[1]["text"], "trait")]),
                            model_tag("不喝咖啡", "美食饮食", [support(2, rows[2]["text"], "trait")]))
        self.models.replies = [extraction, extraction]
        status = self.analyze()
        for name in ("软件工程师", "夜猫子", "不喝咖啡"):
            with self.subTest(name=name):
                self.assertEqual((self.tag(status, name)["relation"], self.tag(status, name)["level"]),
                                 ("trait", "strong"))
        self.assertIsNone(self.tag(status, "咖啡"))

    def test_review_can_remove_or_downgrade_quoted_jokes_hearsay_denial_and_uncertainty(self):
        # These structured responses exercise review/storage policy, not a fake model's semantic accuracy.
        rows = [message(1, "self", "是朋友喜欢篮球，不是我"),
                message(2, "self", "说我喜欢象棋是开玩笑的"),
                message(3, "self", "我不喜欢咖啡"), message(4, "self", "我不清楚自己喜不喜欢游戏")]
        self.core.live = {"friend-a": rows}
        first = answer(*(model_tag(name, category, [support(index, row["text"], "interest")])
                         for index, (name, category, row) in enumerate([
                             ("篮球", "运动健身", rows[0]), ("象棋", "游戏棋牌", rows[1]),
                             ("咖啡", "美食饮食", rows[2]), ("游戏", "游戏棋牌", rows[3])])))
        reviewed = answer(model_tag("篮球", "运动健身", [support(0, rows[0]["text"], "mention")]),
                          model_tag("游戏", "游戏棋牌", [support(3, rows[3]["text"], "uncertain")]))
        self.models.replies = [first, reviewed]
        status = self.analyze()
        self.assertEqual(status["run"]["state"], "complete", status["run"])
        self.assertEqual((self.tag(status, "篮球")["level"], self.tag(status, "篮球")["relation"]),
                         ("possible", "mention"))
        self.assertEqual((self.tag(status, "游戏")["level"], self.tag(status, "游戏")["relation"]),
                         ("possible", "uncertain"))
        self.assertIsNone(self.tag(status, "象棋"))
        self.assertIsNone(self.tag(status, "咖啡"))

    def test_review_cannot_invent_new_tags_or_evidence_or_upgrade_mentions(self):
        self.core.live = {"friend-a": [message(1, "self", "篮球的比赛结果出来了"),
                                        message(2, "self", "另外今天吃什么呢")]}
        first = answer(model_tag("篮球", "运动健身", [support(0, "篮球的比赛结果", "mention")]))
        reviewed = answer(model_tag("篮球", "运动健身", [support(0, "篮球的比赛结果", "interest"),
                                                            support(1, "今天吃什么", "interest")]),
                          model_tag("美食", "美食饮食", [support(1, "今天吃什么", "interest")]))
        self.models.replies = [first, reviewed, reviewed, reviewed]
        status = self.analyze()
        self.assertEqual(status["run"]["state"], "failed", status["run"])
        self.assertNotEqual((self.tag(status, "篮球") or {}).get("level"), "strong")
        self.assertIsNone(self.tag(status, "美食"))
        self.assertEqual(status["counts"]["pendingMessages"], 2, "an invalid review never commits the batch")
        items = [model_item(0, "篮球的比赛结果出来了"), model_item(1, "另外今天吃什么呢")]
        proposed = parse_tags(items)(first)
        invalid = [model_tag("篮球", "运动健身", [support(0, "篮球的比赛结果", "interest")]),
                   model_tag("篮球", "运动健身", [support(1, "今天吃什么", "mention")]),
                   model_tag("美食", "美食饮食", [support(1, "今天吃什么", "mention")])]
        for tag in invalid:
            with self.subTest(tag=tag), self.assertRaises(SelfError):
                parse_review(items, proposed)(answer(tag))

    def test_each_message_retains_its_own_relation_quote_and_reason(self):
        rows = [message(1, "self", "我很喜欢篮球"), message(2, "self", "朋友在讨论篮球比分")]
        self.core.live = {"friend-a": rows}
        extraction = answer(model_tag("篮球", "运动健身", [support(0, "很喜欢篮球", "interest", "本人直接表达偏好"),
                                                            support(1, "讨论篮球比分", "mention", "仅讨论比赛")]))
        self.models.replies = [extraction, extraction]
        status = self.analyze()
        self.assertEqual((self.tag(status, "篮球")["relation"], self.tag(status, "篮球")["level"]),
                         ("interest", "strong"))
        evidence = self.service.tag_evidence(self.account, {"name": "篮球"})["items"]
        self.assertEqual([(item["relation"], item["quote"], item["reason"]) for item in evidence],
                         [("mention", "讨论篮球比分", "仅讨论比赛"), ("interest", "很喜欢篮球", "本人直接表达偏好")])

    def test_structural_evidence_requires_original_quote_relation_and_reason(self):
        items = [model_item(0, "我喜欢篮球")]
        malformed = [0, support(1, "篮球", "interest"), support(True, "篮球", "interest"),
                     support(0, "原文没有这句话", "interest"), support(0, "", "interest"),
                     support(0, "篮球", "unknown"), support(0, "篮球", "interest", ""),
                     support(0, "篮球", "interest", "长" * 201)]
        for evidence in malformed:
            with self.subTest(evidence=evidence):
                with self.assertRaises(SelfError) as caught:
                    parse_tags(items)(answer(model_tag("篮球", "运动健身", [evidence])))
                self.assertEqual(caught.exception.code, "invalid-output")

    def test_legacy_explicit_flag_cannot_upgrade_a_mention_and_truncated_text_is_uncertain(self):
        tag = model_tag("篮球", "运动健身", [support(0, "篮球比赛", "mention")])
        tag["explicit"] = True
        parsed = parse_tags([model_item(0, "篮球比赛几点")])(answer(tag))
        self.assertEqual(parsed[0]["evidence"][0]["relation"], "mention")
        parsed = parse_tags([model_item(0, "我喜欢篮球", truncated=True)])(
            answer(model_tag("篮球", "运动健身", [support(0, "喜欢篮球", "interest")])))
        self.assertEqual(parsed[0]["evidence"][0]["relation"], "uncertain")
        found, _ = candidates([message(1, "self", "我喜欢篮球" + "长" * 1400)], [], "friend-a", False)
        self.assertTrue(found[0]["truncated"])
        self.assertEqual(len(found[0]["text"]), 1200)

    def test_context_is_bounded_by_time_and_preserved_across_pagination_with_followup(self):
        rows = [message(1, "other", "你是真的喜欢篮球吗"), message(2, "self", "对啊"),
                message(3, "other", "那周末一起打吧"), message(4, "self", "好呀一起去")]
        first, carry = candidates(rows[:3], [], "friend-a", False, consume=2)
        self.assertEqual([row["text"] for row in first], ["对啊"])
        self.assertEqual(first[0]["context"], "对方：你是真的喜欢篮球吗")
        self.assertIn("对方：那周末一起打吧", first[0]["followup"])
        second, _ = candidates(rows[2:], carry, "friend-a", False)
        self.assertEqual([row["text"] for row in second], ["好呀一起去"])
        self.assertEqual(second[0]["context"].count("对方：那周末一起打吧"), 1)
        self.assertIn("我：对啊", second[0]["context"])
        late, _ = candidates([message(5, "self", "对啊", time=BASE + DAY)], carry, "friend-a", False)
        self.assertEqual(late[0]["context"], "", "a short answer must not inherit yesterday's preference question")
        isolated, _ = candidates([message(1, "self", "对啊")], [], "friend-a", False)
        self.assertEqual(isolated[0]["context"], "")
        dense = [message(index, "other", "上下文" + "字" * 300) for index in range(1, 9)] + \
            [message(9, "self", "好的"), message(10, "other", "后文一"), message(11, "other", "后文二"),
             message(12, "other", "后文三")]
        bounded, _ = candidates(dense, [], "friend-a", False)
        self.assertEqual(len(bounded[0]["context"].splitlines()), 6)
        self.assertEqual(len(bounded[0]["followup"].splitlines()), 2)
        self.assertTrue(all(len(line.split("：", 1)[-1]) <= 240 for line in bounded[0]["context"].splitlines()))

    def test_merging_synonyms_counts_each_message_once(self):
        self.core.live = {"friend-a": [message(1, "self", "我喜欢篮球，也喜欢打篮球")]}
        extraction = answer(model_tag("篮球", "运动健身", [support(0, "喜欢篮球", "interest")]),
                            model_tag("打篮球", "运动健身", [support(0, "喜欢打篮球", "interest")]))
        self.models.replies = [extraction, extraction]
        status = self.analyze()
        self.assertEqual(self.tag(status, "篮球")["mentions"], 1)
        self.assertEqual(self.service.tag_evidence(self.account, {"name": "篮球"})["total"], 1)

    def test_real_history_page_boundary_retains_acknowledgement_context_and_followup(self):
        rows = [message(index, "other", "前面的聊天内容") for index in range(1, 499)]
        rows += [message(499, "other", "你是真的喜欢篮球吗"), message(500, "self", "对啊"),
                 message(501, "other", "那周末一起打吧"), message(502, "other", "我来订场地"),
                 message(503, "other", "到时再联系")]
        self.core.live = {"friend-a": rows}
        extraction = answer(model_tag("篮球", "运动健身", [support(0, "对啊", "interest", "确认前文对本人偏好的询问")]))
        self.models.replies = [extraction, extraction]
        status = self.analyze()
        self.assertEqual(status["run"]["state"], "complete", status["run"])
        self.assertGreater(len(self.core.pages), 1)
        sent = [item for request in self.models.requests if request["step"] == "self-tags"
                for item in request["prompt"]["messages"]]
        self.assertEqual(len(sent), 1, "overlapping pages must not duplicate candidates")
        self.assertEqual(sent[0]["me"], "对啊")
        self.assertIn("对方：你是真的喜欢篮球吗", sent[0]["ctx"])
        self.assertEqual(sent[0]["after"], "对方：那周末一起打吧\n对方：我来订场地")
        self.assertEqual(self.tag(status, "篮球")["mentions"], 1)

    def test_appended_retraction_invalidates_and_reviews_the_previous_tail_message(self):
        original = "我最爱篮球"
        self.core.live = {"friend-a": [message(1, "self", original)]}
        first = answer(model_tag("篮球", "运动健身", [support(0, original, "interest")]))
        self.models.replies = [first, first]
        self.assertEqual(self.tag(self.analyze(), "篮球")["level"], "strong")
        self.service.tag_edit(self.account, {"action": "add", "name": "手动保留", "category": "其他"})
        self.core.live["friend-a"].append(message(2, "self", "开玩笑的，我从来不打"))
        reviewed = answer(model_tag("篮球", "运动健身", [support(0, original, "mention", "后文明确撤回了兴趣表达")]))
        self.models.replies = [first, reviewed]
        self.models.gate = threading.Event()
        self.addCleanup(self.models.gate.set)
        requests = len(self.models.requests)
        self.service.tags_analyze(self.account)
        for _ in range(100):
            if len(self.models.requests) > requests:
                break
            threading.Event().wait(0.02)
        try:
            self.assertGreater(len(self.models.requests), requests)
            pending = self.service.tags(self.account)
            self.assertEqual(pending["counts"]["pendingMessages"], 2)
            self.assertIsNone(self.tag(pending, "篮球"), "stale interest must disappear while updated context is being reviewed")
            self.assertEqual(self.service.tag_evidence(self.account, {"name": "篮球"})["total"], 0)
        finally:
            self.models.gate.set()
        for thread in self.plugin.threads:
            thread.join(5)
        status = self.service.tags(self.account)
        self.assertEqual(status["run"]["state"], "complete", status["run"])
        self.assertEqual((self.tag(status, "篮球")["relation"], self.tag(status, "篮球")["level"]),
                         ("mention", "possible"))
        self.assertEqual(self.tag(status, "手动保留")["level"], "confirmed")
        evidence = self.service.tag_evidence(self.account, {"name": "篮球"})["items"]
        self.assertEqual(evidence[0]["followup"], "我：开玩笑的，我从来不打")
        update = next(request for request in self.models.requests[requests:] if request["step"] == "self-tags")
        self.assertEqual([item["me"] for item in update["prompt"]["messages"]], [original, "开玩笑的，我从来不打"])
        requests = len(self.models.requests)
        self.analyze()
        self.assertEqual(len(self.models.requests), requests, "no new messages must mean no extraction or review calls")

    def test_two_saved_followup_lines_do_not_reopen_an_old_evidence_message(self):
        self.core.live = {"friend-a": [message(1, "self", "我很喜欢篮球"), message(2, "other", "这周一起去吧"),
                                        message(3, "other", "我来订场地")]}
        self.assertEqual(self.tag(self.analyze(), "篮球")["level"], "strong")
        evidence = self.service.tag_evidence(self.account, {"name": "篮球"})["items"][0]
        self.core.live["friend-a"].append(message(4, "other", "再确认一下时间"))
        requests = len(self.models.requests)
        self.analyze()
        self.assertEqual(len(self.models.requests), requests)
        self.assertEqual(self.service.tag_evidence(self.account, {"name": "篮球"})["items"][0]["followup"], evidence["followup"])
        self.core.live["friend-a"].append(message(5, "self", "我先处理一下工作"))
        self.models.replies = [answer()]
        status = self.analyze()
        self.assertEqual(status["run"]["state"], "complete", status["run"])
        sent = [item["me"] for request in self.models.requests[requests:] if request["step"] == "self-tags"
                for item in request["prompt"]["messages"]]
        self.assertEqual(sent, ["我先处理一下工作"], "a full context tail must not be analyzed again on every update")
        self.assertEqual(self.tag(status, "篮球")["mentions"], 1)

    def test_appended_text_outside_the_time_window_does_not_reopen_old_evidence(self):
        self.core.live = {"friend-a": [message(1, "self", "我很喜欢篮球")]}
        self.assertEqual(self.tag(self.analyze(), "篮球")["level"], "strong")
        requests = len(self.models.requests)
        self.core.live["friend-a"].append(message(2, "other", "这是新的聊天话题", time=BASE + 31 * 60_000))
        status = self.analyze()
        self.assertEqual(status["run"]["state"], "complete", status["run"])
        self.assertEqual(len(self.models.requests), requests)
        evidence = self.service.tag_evidence(self.account, {"name": "篮球"})["items"]
        self.assertEqual(evidence[0]["followup"], "")

    def test_media_at_the_page_edge_does_not_hide_later_plain_text_followup(self):
        rows = [message(index, "other", "前面的聊天内容") for index in range(1, 498)]
        rows += [message(498, "self", "我只是说一下"), message(499, "other", "后文第一句"),
                 message(500, "other", "[图片]", kind="image"), message(501, "other", "后文第二句"),
                 message(502, "other", "后文第三句")]
        self.core.live = {"friend-a": rows}
        self.models.replies = [answer()]
        status = self.analyze()
        self.assertEqual(status["run"]["state"], "complete", status["run"])
        sent = [item for request in self.models.requests if request["step"] == "self-tags"
                for item in request["prompt"]["messages"]]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["after"], "对方：后文第一句\n对方：后文第二句")
        _, tags = self.service._tags(self.account)
        row = tags.rows("SELECT followup_count,followup_seq FROM tag_messages")[0]
        self.assertEqual((row["followup_count"], row["followup_seq"]), (2, 501),
                         "overlapping lookahead must not append the first line twice")

    def test_legacy_store_requires_new_analysis_and_preserves_human_edits(self):
        store = Store(self.plugin.data_dir, self.account)
        store._meta("tagSource", self.core.source)
        store._meta("tagRun", {"state": "complete", "merged": True, "catalogComplete": True})
        path = store.path
        store.close()
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""
                CREATE TABLE tag_conversations(user TEXT PRIMARY KEY,name TEXT NOT NULL DEFAULT '',
                 is_group INTEGER NOT NULL DEFAULT 0,generation INTEGER NOT NULL DEFAULT 0,
                 epoch TEXT,after_seq INTEGER NOT NULL DEFAULT 0,carry TEXT NOT NULL DEFAULT '[]',
                 state TEXT NOT NULL DEFAULT 'pending');
                CREATE TABLE tag_messages(id TEXT PRIMARY KEY,user TEXT NOT NULL,seq INTEGER NOT NULL,
                 time_ms INTEGER NOT NULL,text TEXT NOT NULL,context TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending');
                CREATE TABLE tag_mentions(message_id TEXT NOT NULL,key TEXT NOT NULL,name TEXT NOT NULL,
                 category TEXT NOT NULL,explicit INTEGER NOT NULL,PRIMARY KEY(message_id,key));
                CREATE TABLE tag_aliases(key TEXT PRIMARY KEY,name TEXT NOT NULL);
                CREATE TABLE tag_edits(key TEXT PRIMARY KEY,name TEXT NOT NULL,category TEXT NOT NULL,
                 state TEXT NOT NULL,updated_ms INTEGER NOT NULL);
            """)
            db.execute("INSERT INTO tag_conversations VALUES(?,?,?,?,?,?,?,?)",
                       ("friend-a", "旧合成朋友", 0, 1, "1", 1, "[]", "done"))
            db.execute("INSERT INTO tag_messages VALUES(?,?,?,?,?,?,?)",
                       ("friend-a/m1", "friend-a", 1, BASE, "篮球比赛几点开始", "", "done"))
            for name in ("篮球", "已确认", "已删除"):
                db.execute("INSERT INTO tag_mentions VALUES(?,?,?,?,?)",
                           ("friend-a/m1", name, name, "运动健身", 1))
            for name, state in (("已确认", "confirmed"), ("已删除", "deleted"), ("手动添加", "confirmed")):
                db.execute("INSERT INTO tag_edits VALUES(?,?,?,?,?)", (name, name, "运动健身", state, BASE))
        status = self.service.tags(self.account)
        self.assertTrue(status["run"]["needsReview"])
        self.assertEqual((self.tag(status, "篮球")["level"], self.tag(status, "篮球")["relation"]),
                         ("possible", "uncertain"), "legacy explicit booleans do not establish reviewed interest")
        self.assertEqual(self.tag(status, "已确认")["level"], "confirmed")
        self.assertIsNone(self.tag(status, "已删除"))
        self.assertTrue(self.tag(status, "手动添加")["manual"])
        self.core.live = {"friend-a": [message(1, "other", "篮球比赛几点开始"),
                                        message(2, "self", "我只是帮朋友问一下")]}
        self.models.replies = [answer()]
        updated = self.analyze()
        self.assertEqual(updated["run"]["state"], "complete", updated["run"])
        self.assertTrue(self.core.pages, "migration must reread richer context rather than trust legacy one-line evidence")
        self.assertFalse(updated["run"].get("needsReview"))
        self.assertIsNone(self.tag(updated, "篮球"))
        self.assertEqual(self.tag(updated, "已确认")["level"], "confirmed")
        self.assertEqual(self.tag(updated, "手动添加")["level"], "confirmed")
        self.assertIsNone(self.tag(updated, "已删除"))


if __name__ == "__main__":
    unittest.main()
