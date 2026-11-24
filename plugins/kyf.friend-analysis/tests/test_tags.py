"""Synthetic tests for 好友标签: the scan step really runs, edits win, evidence rules, memory links, habits.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from harness import Friends, Harness

import friend_sim_encode
import friend_tags
from contracts import AdvisorError
from test_analysis import ACCOUNT, USER, ScriptedRuntime, WindowSource, msg
from test_support import FakeModelStore, wait_until

DAY = 86_400_000
BASE = 1_700_000_000_000
LINE = re.compile(r"^\[(\d+)\] \S+ \S+ (我|对方): (.*)$", re.M)
# (text in a line, tag the scripted model proposes for that line)
RULES = [
    ("我每周都去打篮球", {"name": "篮球", "category": "运动健身", "relation": "interest", "quote": "我每周都去打篮球"}),
    ("打球真开心", {"name": "打篮球", "category": "运动健身", "relation": "interest", "quote": "打球真开心"}),
    ("对", {"name": "不吃辣", "category": "美食饮食", "relation": "trait", "quote": "对"}),
    ("去年去了日本", {"name": "去年去日本", "category": "旅行出行", "relation": "occasional", "quote": "去年去了日本"}),
    ("去年去了日本", {"name": "日本", "category": "旅行出行", "relation": "mention", "quote": "日本"}),
    ("我喜欢象棋", {"name": "象棋", "category": "游戏棋牌", "relation": "interest", "quote": "我喜欢象棋"}),
    ("我朋友超爱象棋", {"name": "象棋", "category": "游戏棋牌", "relation": "mention", "quote": "我朋友超爱象棋"}),
    ("我不吃香菜", {"name": "不吃香菜", "category": "美食饮食", "relation": "trait", "quote": "我从来不吃香菜"}),
]
MEMORIES = [("我不吃香菜", {"text": "对方不吃香菜", "tags": ["偏好与忌口"]}),
            ("去年去了日本", {"text": "对方去年去了日本", "tags": ["经历"]})]


class TagRuntime(ScriptedRuntime):
    def __init__(self):
        super().__init__()
        self.review = None

    def answer_tags(self, message):
        tags = []
        for number, speaker, text in LINE.findall(message.split("聊天片段：\n", 1)[1]):
            for keyword, rule in RULES:
                if (text == keyword if keyword == "对" else keyword in text):
                    tags.append({"name": rule["name"], "category": rule["category"], "evidence": [
                        {"i": int(number), "relation": rule["relation"], "quote": rule["quote"], "reason": "合成依据"}]})
        return {"tags": tags}

    def answer_tags_review(self, message):
        proposed = json.loads(message.rsplit("tags: ", 1)[1])
        return {"tags": self.review(proposed) if self.review else proposed}

    def answer_tags_merge(self, message):
        names = [item["name"] for item in json.loads(message)["tags"]]
        return {"merge": [{"into": "篮球", "from": ["打篮球"]}] if {"篮球", "打篮球"} <= set(names) else []}

    def answer_extract(self, message):
        found = []
        for number, speaker, text in LINE.findall(message):
            for keyword, item in MEMORIES:
                if speaker == "对方" and keyword in text:
                    found.append({**item, "evidence": [int(number)], "eventTime": None})
        return {"memories": found}

    def prompts(self, kind):
        return [message for item, message in self.calls if item == kind]


def conversation():
    lines = [("self", "你周末一般干嘛"), ("other", "我每周都去打篮球，超喜欢"), ("self", "你不是不吃辣吗"), ("other", "对"),
             ("other", "去年去了日本"), ("self", "我喜欢象棋"), ("other", "我朋友超爱象棋"), ("other", "打球真开心"),
             ("other", "我不吃香菜"), ("other", "哈哈哈"), ("self", "好"), ("other", "哈哈哈"), ("other", "哈哈哈")]
    return [msg(index + 1, side, text, BASE + index * 10 * 60_000)
            for index, (side, text) in enumerate(lines)]


class TagCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = WindowSource(account=ACCOUNT)
        self.source.add_messages(USER, conversation())
        self.runtime = TagRuntime()
        self.harness = Harness(self.source, FakeModelStore(context_tokens=32768), self.runtime, Path(temp.name),
                               ACCOUNT, USER)
        self.addCleanup(self.harness.close)
        self.friends = Friends(self.harness)

    def open_ready(self):
        self.friends.open(ACCOUNT, USER)
        self.assertTrue(wait_until(lambda: self.friends.overview(ACCOUNT, USER)["history"]["state"] == "ready" and
                                   not self.friends.overview(ACCOUNT, USER)["history"]["refreshing"]))

    def wait_run(self):
        def settled():
            run = self.friends.analysis_status(ACCOUNT, USER)["run"]
            tasks = self.harness.core.tasks.tasks(account=ACCOUNT, user=USER, states=("queued", "running", "stopping"))
            return run if run and run["state"] not in ("queued", "running") and not tasks and \
                not self.friends.analysis.active(ACCOUNT, USER) else None
        run = wait_until(settled, timeout=20)
        self.assertIsNotNone(run, "run did not finish")
        return run

    def analyse(self, mode="update"):
        self.open_ready()
        self.friends.analysis_start(ACCOUNT, USER, None, mode)
        return self.wait_run()

    def page(self):
        return friend_tags.view(self.friends.pages, ACCOUNT, USER)

    def tags(self):
        return {tag["name"]: tag for tag in self.page()["tags"]}

    def edit(self, **params):
        return friend_tags.edit(self.friends.pages, ACCOUNT, USER, params)

    def agent_edit(self, **params):
        return friend_tags.edit(self.friends.pages, ACCOUNT, USER, params, actor="agent", agent="合成 Agent")

    def undo(self, source="user", actor="user", **params):
        return friend_tags.undo(self.friends.pages, ACCOUNT, USER, params, actor, source)

    def profile(self, before_ms=None):
        _account, _user, _fp, database, store = self.friends.pages._scope(ACCOUNT, USER)
        profile = friend_sim_encode.encode(database, store, USER, before_ms=before_ms)
        return profile, {layer["key"]: layer["lines"] for layer in profile["layers"]}


class StepTests(TagCase):
    def test_estimate_counts_the_tags_step_before_any_call(self):
        self.open_ready()
        step = next(item for item in self.friends.analysis_estimate(ACCOUNT, USER)["steps"] if item["key"] == "tags")
        self.assertGreater(step["tokens"][1], 0)
        self.assertEqual(self.runtime.calls, [])

    def test_full_run_really_runs_the_tags_step_and_an_empty_update_calls_nothing(self):
        run = self.analyse()
        self.assertEqual(run["state"], "done", run)
        step = next(item for item in run["steps"] if item["key"] == "tags")
        self.assertEqual(step["state"], "done", step)
        self.assertGreater(step["calls"], 0)
        self.assertGreater(self.runtime.count("tags"), 0)
        self.assertEqual(self.runtime.count("tags_merge"), 1)
        tags = self.tags()
        self.assertEqual((tags["篮球"]["relation"], tags["篮球"]["level"], tags["篮球"]["mentions"]),
                         ("interest", "strong", 2), "the synonym 打篮球 was merged into 篮球")
        self.assertNotIn("打篮球", tags)
        self.assertEqual((tags["不吃辣"]["relation"], tags["不吃辣"]["level"]), ("trait", "strong"))
        self.assertNotIn("去年去日本", tags, "a name with a time word is an event, not a tag")
        self.assertEqual((tags["象棋"]["relation"], tags["象棋"]["mentions"]), ("mention", 1),
                         "my own line is never ta's evidence")
        self.assertEqual((tags["不吃香菜"]["relation"], tags["不吃香菜"]["level"]), ("uncertain", "possible"),
                         "a quote that is not in the message is kept only as uncertain")
        calls = len(self.runtime.calls)
        estimate = self.friends.analysis_estimate(ACCOUNT, USER)
        self.assertEqual(next(item for item in estimate["steps"] if item["key"] == "tags")["tokens"], [0, 0])
        self.friends.analysis_start(ACCOUNT, USER, None, "update")
        self.assertEqual(self.wait_run()["state"], "done")
        self.assertEqual(len(self.runtime.calls), calls, "no new messages: no scan, no merge")

    def test_new_messages_scan_only_the_new_segment(self):
        self.analyse()
        before = len(self.runtime.prompts("tags"))
        later = BASE + 300 * DAY
        self.source.add_messages(USER, [msg(100, "self", "最近呢", later), msg(101, "other", "打球真开心", later + 60_000)])
        self.friends.analysis_start(ACCOUNT, USER, None, "update")
        self.assertEqual(self.wait_run()["state"], "done")
        prompts = self.runtime.prompts("tags")[before:]
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("我每周都去打篮球", prompts[0])
        self.assertEqual(self.tags()["篮球"]["mentions"], 3)

    def test_evidence_shows_my_question_as_context_and_ta_line_as_the_quote(self):
        self.analyse()
        result = friend_tags.evidence(self.friends.pages, ACCOUNT, USER, {"name": "不吃辣"})
        self.assertEqual(result["total"], 1)
        item = result["items"][0]
        self.assertEqual((item["text"], item["quote"], item["relation"], item["status"]), ("对", "对", "trait", "ok"))
        self.assertIn("我：你不是不吃辣吗", item["context"])


class EditTests(TagCase):
    def test_user_edits_survive_a_rebuild_and_deleted_names_leave_known(self):
        self.analyse()
        self.edit(action="confirm", name="不吃香菜")
        self.edit(action="delete", name="象棋")
        self.edit(action="add", name="爱看书", category="阅读学习")
        before = len(self.runtime.prompts("tags"))
        self.assertEqual(self.analyse("rebuild")["state"], "done")
        prompts = self.runtime.prompts("tags")[before:]
        self.assertTrue(prompts, "a rebuild reads every segment again")
        for prompt in prompts:
            known = json.loads(prompt.split("\n\n聊天片段", 1)[0][len("known: "):])
            self.assertNotIn("象棋", known)
            self.assertIn("篮球", known)
        tags = self.tags()
        self.assertEqual(tags["不吃香菜"]["level"], "confirmed")
        self.assertNotIn("象棋", tags)
        self.assertEqual((tags["爱看书"]["level"], tags["爱看书"]["manual"], tags["爱看书"]["category"]),
                         ("confirmed", True, "阅读学习"))
        self.edit(action="reset", name="象棋")
        self.assertIn("象棋", self.tags(), "undoing a delete shows the tag again")

    def test_edit_records_its_source(self):
        self.analyse()
        friend_tags.edit(self.friends.pages, ACCOUNT, USER, {"action": "confirm", "name": "篮球"}, actor="agent")
        row = self.harness.store()._row("SELECT actor FROM friend_tag_edits WHERE user=? AND key=?", (USER, "篮球"))
        self.assertEqual(row["actor"], "agent")
        with self.assertRaises(Exception):
            self.edit(action="add", name="新标签", category="不存在")

    def test_review_can_only_keep_downgrade_or_delete(self):
        def review(proposed):
            changed = []
            for tag in proposed:
                for proof in tag["evidence"]:
                    proof["relation"] = "mention" if tag["name"] == "篮球" else "interest" if tag["name"] == "象棋" else \
                        proof["relation"]
                changed.append(tag)
            first = proposed[0]["evidence"][0]
            return changed + [{"name": "围棋", "category": "游戏棋牌", "evidence": [dict(first)]}]
        self.runtime.review = review
        self.analyse()
        tags = self.tags()
        self.assertEqual(tags["篮球"]["relation"], "mention", "a downgrade is kept")
        self.assertEqual(tags["象棋"]["relation"], "mention", "an upgrade falls back to the extraction")
        self.assertNotIn("围棋", tags, "the review cannot add a tag")


class MemoryLinkTests(TagCase):
    def confirm_memories(self):
        core = self.harness.service.friends
        pending = core.memories(ACCOUNT, USER, status="pending")["items"]
        self.assertEqual({item["text"] for item in pending}, {"对方不吃香菜", "对方去年去了日本"})
        for item in pending:
            core.memory_action(ACCOUNT, USER, {"action": "confirm", "id": item["id"]})
            self.wait_run()
        return {item["text"]: item["id"] for item in pending}

    def test_trait_memory_with_shared_evidence_is_linked_until_unlinked(self):
        self.analyse()
        ids = self.confirm_memories()
        tags = self.tags()
        self.assertEqual(tags["不吃香菜"]["memories"], [{"id": ids["对方不吃香菜"], "text": "对方不吃香菜"}])
        self.assertEqual((tags["不吃香菜"]["level"], tags["不吃香菜"]["relation"]), ("strong", "trait"),
                         "a linked confirmed memory makes the tag firm, never 待判断 beside 较可靠")
        self.assertEqual(tags["日本"]["memories"], [], "经历 memories are never linked")
        self.edit(action="unlink", name="不吃香菜", memoryId=ids["对方不吃香菜"])
        tags = self.tags()
        self.assertEqual((tags["不吃香菜"]["memories"], tags["不吃香菜"]["level"], tags["不吃香菜"]["relation"]),
                         ([], "possible", "uncertain"))


class HabitTests(TagCase):
    def test_habits_hide_and_add_are_kept_and_show_evidence(self):
        self.analyse()
        page = self.page()
        self.assertEqual(page["categories"][-1], "说话习惯")
        self.assertEqual([item["key"] for item in page["habits"] if item["label"] == "哈哈哈"], ["phrase:哈哈哈"],
                         "a phrase that is also a whole message shows once")
        habit = next(item for item in page["habits"] if item["label"] == "哈哈哈")
        evidence = friend_tags.evidence(self.friends.pages, ACCOUNT, USER, {"key": habit["key"]})
        self.assertEqual(len(evidence["items"]), 3)
        self.assertTrue(all(item["text"] == "哈哈哈" for item in evidence["items"]))
        self.edit(action="hide", key=habit["key"])
        self.assertTrue(next(item for item in self.page()["habits"] if item["key"] == habit["key"])["hidden"])
        self.edit(action="add", name="从不发语音", category="说话习惯")
        self.edit(action="hide", key="manual:从不发语音")
        self.edit(action="unhide", key="manual:从不发语音")
        habits = {item["key"]: item for item in self.page()["habits"]}
        self.assertEqual((habits["manual:从不发语音"]["detail"], habits["manual:从不发语音"]["hidden"]), ("我的补充", False))
        self.assertTrue(habits[habit["key"]]["hidden"], "a hidden habit stays hidden when counted again")
        self.edit(action="unhide", key=habit["key"])
        self.assertFalse(next(item for item in self.page()["habits"] if item["key"] == habit["key"])["hidden"])


class UndoTests(TagCase):
    def test_each_source_undoes_only_its_own_edits_with_versions(self):
        self.analyse()
        version = self.page()["version"]
        with self.assertRaises(AdvisorError) as caught:
            self.edit(action="confirm", name="篮球", expectedVersion=version + 1)
        self.assertEqual(caught.exception.code, "conflict")
        mine = self.edit(action="delete", name="象棋", expectedVersion=version)
        self.assertEqual((mine["version"], mine["editedId"], mine["editLabel"]), (version + 1, "象棋", "删除标签：象棋"))
        agent = self.agent_edit(action="add", name="爱看书", category="阅读学习", expectedVersion=mine["version"])
        self.assertEqual((agent["undo"]["user"]["count"], agent["undo"]["agent"]["count"]), (1, 1))
        self.assertEqual(agent["undo"]["agent"]["items"][0]["agent"], "合成 Agent")
        row = self.harness.store()._row("SELECT actor FROM friend_tag_edits WHERE user=? AND key=?", (USER, "爱看书"))
        self.assertEqual(row["actor"], "agent")
        with self.assertRaises(AdvisorError) as caught:
            self.undo("user", "agent")
        self.assertEqual(caught.exception.code, "forbidden", "an Agent cannot reach the user's stack")
        result = self.undo("user")
        self.assertEqual((result["undone"], result["skipped"]), ("删除标签：象棋", None))
        names = {tag["name"] for tag in result["tags"]}
        self.assertIn("象棋", names)
        self.assertIn("爱看书", names, "the Agent's edit stays")
        result = self.undo("agent", "agent", expectedVersion=result["version"])
        self.assertEqual(result["undone"], "添加标签：爱看书")
        self.assertNotIn("爱看书", {tag["name"] for tag in result["tags"]})
        self.assertIsNone(self.undo("agent", "agent")["undone"], "nothing left on the Agent's stack")

    def test_a_later_user_edit_makes_the_agent_undo_skip_and_agents_cannot_unlink(self):
        self.analyse()
        self.agent_edit(action="confirm", name="篮球")
        self.edit(action="delete", name="篮球")
        result = self.undo("agent", "agent")
        self.assertIsNone(result["undone"])
        self.assertEqual(result["skipped"]["label"], "确认标签：篮球")
        self.assertEqual(result["undo"]["agent"]["count"], 0, "the stale entry is dropped")
        self.assertNotIn("篮球", {tag["name"] for tag in result["tags"]}, "the user's delete stays")
        with self.assertRaises(AdvisorError) as caught:
            self.agent_edit(action="unlink", name="不吃香菜", memoryId="m1")
        self.assertEqual(caught.exception.code, "forbidden")
        habit = self.edit(action="hide", key="phrase:哈哈哈")
        self.assertEqual(habit["editLabel"], "隐藏说话习惯：哈哈哈")
        self.assertEqual(self.undo("user")["undone"], "隐藏说话习惯：哈哈哈")

    def test_undo_is_per_friend(self):
        self.analyse()
        self.edit(action="delete", name="象棋")
        other = friend_tags.journal(self.harness.accounts.account(ACCOUNT), "wxid_other_friend")
        self.assertEqual(other.recent("user"), (0, []))
        self.assertIsNone(other.undo(actor="user"))
        self.assertEqual(self.page()["undo"]["user"]["count"], 1)


class ProfileTests(TagCase):
    """模拟档案 2a: firm tags only, the user's first, a linked memory written once, habit edits, backtests cut."""

    confirm_memories = MemoryLinkTests.confirm_memories

    def test_layer_2a_lists_firm_tags_and_links_memories_once(self):
        self.analyse()
        _profile, layers = self.profile()
        self.assertEqual(list(layers)[:4], ["rules", "expression", "response", "tags"])
        self.assertIn("运动健身：篮球", layers["tags"])
        self.assertIn("美食饮食：不吃辣", layers["tags"])
        for left_out in ("象棋", "日本", "不吃香菜", "哈哈哈"):
            self.assertNotIn(left_out, "\n".join(layers["tags"]), "mentions, uncertain tags and 说话习惯 stay out")
        self.edit(action="add", name="爱看书", category="阅读学习")
        self.edit(action="add", name="面食", category="美食饮食")
        self.edit(action="delete", name="不吃辣")
        self.confirm_memories()
        _profile, layers = self.profile()
        self.assertIn("美食饮食：面食、不吃香菜（关系记忆：对方不吃香菜）", layers["tags"],
                      "the user's tags first; a linked memory follows its tag")
        self.assertIn("阅读学习：爱看书", layers["tags"])
        memory = "\n".join(layers["memory"])
        self.assertNotIn("对方不吃香菜", memory, "layer 4 skips a memory 2a already wrote")
        self.assertIn("对方去年去了日本", memory, "ta's own 经历 stays in layer 4")

    def test_habit_edits_reach_layers_0_and_1(self):
        self.analyse()
        _profile, layers = self.profile()
        self.assertIn("哈哈哈", "\n".join(layers["expression"]))
        for item in self.page()["habits"]:
            if "哈哈哈" in item["label"]:
                self.edit(action="hide", key=item["key"])
        self.edit(action="add", name="从不发语音", category="说话习惯")
        self.edit(action="add", name="爱用倒装句", category="说话习惯")
        _profile, layers = self.profile()
        self.assertNotIn("哈哈哈", "\n".join(layers["expression"]), "a deleted habit is not in the profile")
        self.assertEqual(layers["expression"][0], "我的补充（最可信）：爱用倒装句")
        self.assertIn("从不发语音", layers["rules"][-1])
        self.assertNotIn("从不发语音", "\n".join(layers["expression"]))

    def test_backtest_uses_only_mentions_before_the_cut_and_no_edits(self):
        self.analyse()
        self.edit(action="add", name="爱看书", category="阅读学习")
        self.edit(action="add", name="从不发语音", category="说话习惯")
        profile, layers = self.profile(before_ms=BASE + 25 * 60_000)  # after 篮球 (line 2), before 不吃辣 (line 4)
        self.assertEqual(layers["tags"], ["运动健身：篮球"])
        self.assertEqual(profile["omittedLayers"], ["persona", "memory"])
        text = friend_sim_encode.prefix(profile)
        for leaked in ("不吃辣", "爱看书", "从不发语音"):
            self.assertNotIn(leaked, text)
        _profile, layers = self.profile(before_ms=BASE)
        self.assertNotIn("tags", layers, "nothing said before the cut: no tag layer")

    def test_prefix_keeps_layer_order_within_the_budget(self):
        self.analyse()
        profile, _layers = self.profile()
        text = friend_sim_encode.prefix(profile)
        self.assertLess(text.index("【回应倾向】"), text.index("【ta 是什么样的人（标签）】"))
        self.assertEqual(friend_sim_encode.PROFILE_BUDGET, 3600)
        short = friend_sim_encode.prefix(profile, budget=sum(map(len, profile["layers"][1]["lines"])) + 12)
        self.assertNotIn("篮球", short, "2a comes after layers 1–2 when the budget is short")


if __name__ == "__main__":
    unittest.main()
