"""模拟对话·编码好友 tests on synthetic chats: sample library, style statistics, tendencies and layers.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import random
import tempfile
import unittest

import harness  # noqa: F401  (puts the plugin backend on the import path)
import friend_sim_encode as encode
from account import AccountData
from analysis_store import FriendStore

USER = "wxid_friend"
T0 = 1_700_000_000_000
MINUTE = 60_000


class Core:
    def __init__(self):
        self.calls = 0
        self.human = {"state": {}, "memories": [], "exclusions": [], "goal": {"goal": ""}, "profile": {},
                      "observations": []}

    def get(self, path, **_query):
        self.calls += 1
        return self.human


class Plugin:
    def __init__(self, data_dir):
        self.data_dir, self.core = data_dir, Core()


class EncodeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin = Plugin(self.tmp.name)
        self.db = AccountData(self.plugin, "acct")
        self.store = FriendStore(self.db)
        self.seq = 0

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, side, minute, text, kind="text", raw=None, msg_id=None):
        self.seq += 1
        with self.db._connection:
            self.db._connection.execute(
                "INSERT OR REPLACE INTO context_messages(user,seq,msg_id,sort_key,group_chat,side,sender_id,sender_name,"
                "kind,text,time_ms,raw) VALUES(?,?,?,?,0,?,'','',?,?,?,?)",
                (USER, self.seq, msg_id or "m%d" % self.seq, "%08d" % self.seq, side, kind, text,
                 T0 + int(minute * MINUTE), json.dumps(raw or {})))
        return self

    def ready(self, source="src-1", epoch="e1"):
        with self.db._connection:
            self.db._connection.execute(
                "INSERT OR REPLACE INTO context(user,state,revision,read_count,total_count,error,source_id,epoch,"
                "synced_at_ms) VALUES(?,'ready',0,?,?,NULL,?,?,0)", (USER, self.seq, self.seq, source, epoch))

    def library(self):
        return self.db._rows("SELECT * FROM friend_sim_samples WHERE user=? ORDER BY time_ms", (USER,))

    # -- sample library -----------------------------------------------------------------------------
    def test_samples_take_my_run_as_input_and_ta_run_within_30_minutes_as_reply(self):
        self.add("other", 0, "在吗").add("self", 1, "周末一起吃饭吗").add("self", 2, "有空吗")
        self.add("other", 5, "好啊").add("other", 6, "几点")
        self.add("self", 100, "到了").add("other", 140, "哦")                      # 40 minutes later: no pair
        self.add("self", 200, "看这个").add("other", 201, "转发的", raw={"forward": True})  # forward is a barrier
        self.add("other", 202, "哈哈")
        self.add("self", 300, "图").add("other", 301, "[图片]", kind="image")      # media is never a reply
        self.ready()
        found = encode.samples(self.db, USER, [{"side": "me", "messages": ["周末有空吗"]}])
        self.assertEqual([(item["input"], item["reply"]) for item in found],
                         [(["周末一起吃饭吗", "有空吗"], ["好啊", "几点"])])
        self.assertEqual((found[0]["timeMs"], found[0]["seq"], found[0]["messageId"]), (T0 + 6 * MINUTE, 4, "m4"))

    def test_before_ms_excludes_later_samples_and_statistics(self):
        self.add("self", 0, "吃饭了吗").add("other", 1, "吃了").add("self", 600, "吃饭了吗").add("other", 601, "还没呢！")
        self.ready()
        cut = T0 + 300 * MINUTE
        found = encode.samples(self.db, USER, [{"side": "me", "messages": ["吃饭了吗"]}], before_ms=cut)
        self.assertEqual([item["reply"] for item in found], [["吃了"]])
        self.assertEqual(len(encode.samples(self.db, USER, [{"side": "me", "messages": ["吃饭了吗"]}])), 2)
        profile = encode.encode(self.db, self.store, USER, before_ms=cut)
        self.assertEqual((profile["style"]["messages"], profile["range"]["samples"]), (1, 1))
        self.assertEqual(profile["bubbles"], {1: [1]}, "the later round is not counted")

    def test_library_grows_incrementally_and_rebuilds_on_mirror_reset(self):
        self.add("self", 0, "明天见").add("other", 1, "好")
        self.ready()
        encode.refresh(self.db, USER)
        first = self.library()
        self.assertEqual([json.loads(row["reply"]) for row in first], [["好"]])
        self.add("other", 2, "几点").add("self", 3, "十点")                        # ta's run continues next page
        self.ready()
        encode.refresh(self.db, USER)
        grown = self.library()
        self.assertEqual([(row["id"], json.loads(row["reply"])) for row in grown], [(first[0]["id"], ["好", "几点"])])
        with self.db._connection:                                                 # seq rollback with new ids
            self.db._connection.execute("DELETE FROM context_messages WHERE user=? AND seq>=2", (USER,))
        self.seq = 1
        self.add("other", 1, "不去了", msg_id="n2")
        self.ready()
        encode.refresh(self.db, USER)
        self.assertEqual([json.loads(row["reply"]) for row in self.library()], [["不去了"]])
        with self.db._connection:
            self.db._connection.execute("DELETE FROM context_messages WHERE user=?", (USER,))
        self.seq = 0
        self.add("self", 0, "你好").add("other", 1, "嗯")
        self.ready(source="src-2")
        encode.refresh(self.db, USER)
        self.assertEqual([json.loads(row["reply"]) for row in self.library()], [["嗯"]])

    def test_focus_samples_answer_my_last_messages_first(self):
        self.add("self", 0, "周末去爬山吗").add("other", 1, "太累了不去")
        self.add("self", 100, "这家火锅好吃").add("other", 101, "下次带我")
        self.ready()
        turns = [{"side": "me", "messages": ["火锅"]}, {"side": "friend", "messages": ["嗯"]},
                 {"side": "me", "messages": ["周末爬山"]}]
        found = encode.samples(self.db, USER, turns, limit=2)
        self.assertEqual([item["reply"] for item in found], [["太累了不去"], ["下次带我"]])

    # -- fallbacks -------------------------------------------------------------------------------------
    def test_without_samples_everything_falls_back(self):
        self.ready()
        self.assertEqual(encode.samples(self.db, USER, [{"side": "me", "messages": ["在吗"]}]), [])
        profile = encode.encode(self.db, self.store, USER)
        self.assertEqual((profile["bubbles"], profile["replyRate"]), ({}, None))
        self.assertIn("ta 回复我的样例还没有", profile["warning"])
        self.assertIsNone(encode.reply_probability(profile))
        rng = random.Random(3)
        self.assertTrue(all(encode.bubble_count(profile, 2, rng) in (1, 2) for _ in range(20)))
        self.assertTrue(encode.prefix(profile).startswith("【硬规则（不可违背）】\n你就是这位好友本人"))

    def test_unanalysed_friend_gets_layers_0_to_2_and_a_warning(self):
        self.add("self", 0, "在吗").add("other", 1, "在")
        self.ready()
        profile = encode.encode(self.db, self.store, USER)
        self.assertFalse(profile["analysed"])
        self.assertEqual([layer["key"] for layer in profile["layers"]], ["rules", "expression", "response"])
        self.assertTrue(profile["warning"].startswith("未做知己分析，模拟可能不准"))
        self.assertEqual(self.plugin.core.calls, 0, "no corrections or memories are read for an unanalysed friend")

    # -- style ---------------------------------------------------------------------------------------
    def test_style_never_list_and_bubble_groups(self):
        minute = 0
        for index in range(30):
            self.add("self", minute, "今天怎么样")
            self.add("other", minute + 1, "哈哈哈 还行吧").add("other", minute + 2, "你呢[捂脸]")
            self.add("self", minute + 3, "我也还行").add("self", minute + 4, "就是有点累")
            self.add("other", minute + 5, "早点睡哥")
            minute += 1000
        self.ready()
        profile = encode.encode(self.db, self.store, USER)
        style = profile["style"]
        self.assertEqual(style["messages"], 90)
        self.assertEqual(profile["bubbles"], {1: [2] * 30, 2: [1] * 30})
        self.assertEqual(style["never"], ["从不用句号", "从不用感叹号", "从不用波浪号", "从不用省略号", "从不用问号",
                                          "从不用“您”", "从不夹英文字母", "单条从不超过 6 字"])
        self.assertEqual([item["text"] for item in style["address"]], ["哥"])
        self.assertEqual(style["emoji"]["top"], ["[捂脸]"])
        self.assertEqual([item["text"] for item in style["particles"]], ["吧", "呢"])
        self.assertIn("哈哈哈", [item["text"] for item in style["phrases"]])
        rules = profile["layers"][0]["lines"]
        self.assertTrue(rules[-1].startswith("ta 的“从不”清单，必须遵守：从不用句号；"))
        self.assertNotIn("捂脸", [item["text"] for item in style["phrases"]], "emoji codes are not phrases")
        expression = "\n".join(profile["layers"][1]["lines"])
        self.assertIn("我一次发 1 条时，ta 回：2 条 100%（30 轮）", expression)
        self.assertIn("称呼我：“哥”", expression)
        rng = random.Random(1)
        self.assertEqual({encode.bubble_count(profile, 1, rng) for _ in range(10)}, {2})
        self.assertEqual({encode.bubble_count(profile, 9, rng) for _ in range(30)}, {1, 2}, "a thin group pools all")

    def test_advisor_texts_describe_ta_and_count_how_i_talk_to_ta(self):
        """分析与建议 (2026-11-09): no role-play rules; ta's habits marked as ta's; my own habits counted."""
        minute = 0
        for index in range(30):
            self.add("self", minute, "今天怎么样！")
            self.add("other", minute + 1, "哈哈哈 还行吧").add("other", minute + 2, "你呢[捂脸]")
            self.add("self", minute + 3, "我也还行。").add("self", minute + 4, "就是有点累哈")
            self.add("other", minute + 5, "早点睡哥")
            minute += 1000
        self.ready()
        profile = encode.encode(self.db, self.store, USER)
        text = encode.advisor_prefix(profile)
        self.assertNotIn("你就是这位好友本人", text)
        self.assertNotIn("硬规则", text)
        self.assertIn("【ta 的表达方式（这是 ta 的说话习惯，不是我的）】", text)
        rows = self.db.context_messages(USER, None)
        mine = encode.advisor_mine(rows, USER)
        self.assertTrue(mine.startswith("【我对 ta 说话的方式（我最近发给 ta 的 90 条文字消息）】"), mine)
        self.assertIn("我回 ta 时一次发：2 条 100%（30 轮）", mine)
        self.assertIn("句末：", mine)
        self.assertIn("感叹号", mine)
        self.assertNotIn("捂脸", mine, "ta's emoji are not mine")
        self.assertEqual(encode.advisor_mine(rows[:6], USER), "", "too few of my messages to count")
        self.assertLessEqual(len(encode.advisor_mine(rows, USER, budget=60)), 60)

    def test_small_history_has_no_never_list(self):
        self.add("self", 0, "嗨").add("other", 1, "嗨")
        self.ready()
        self.assertEqual(encode.encode(self.db, self.store, USER)["style"]["never"], [])

    def test_reply_probability_needs_five_rounds(self):
        for index in range(6):                                  # odd rounds: ta answers after more than 24 hours
            self.add("self", index * 5000, "在吗").add("other", index * 5000 + (1 if index % 2 == 0 else 2000), "在")
        self.ready()
        profile = encode.encode(self.db, self.store, USER, before_ms=T0 + 40000 * MINUTE)
        self.assertEqual(profile["replyRate"]["samples"], 6)
        self.assertAlmostEqual(encode.reply_probability(profile), 0.5)
        self.assertIsNone(encode.reply_probability({"replyRate": {"rate": 1.0, "samples": 4}}))

    # -- analysed friend ------------------------------------------------------------------------------
    def labelled(self):
        blocks, base = [], T0

        def pair(index, intent, response, invite=None, answered=True):
            mine = {"block_id": "b%d" % index, "side": "self", "intent": intent, "reply_to": None, "response": None,
                    "invite": None, "first_ms": base + index * 10 * MINUTE, "last_ms": base + index * 10 * MINUTE}
            blocks.append(mine)
            if answered:
                blocks.append({"block_id": "r%d" % index, "side": "other", "intent": "回应", "reply_to": mine["block_id"],
                               "response": response, "invite": invite, "first_ms": mine["last_ms"] + MINUTE,
                               "last_ms": mine["last_ms"] + MINUTE})
        for index, (response, invite) in enumerate([("接住并展开", "明确接受")] * 3 + [("明确婉拒", "明确婉拒")] * 2):
            pair(index, "邀请", response, invite)
        pair(5, "邀请", None, answered=False)
        for index in range(6, 10):
            pair(index, "提问", "简短回应")
        pair(10, "分享", "无法判断")
        pair(50, "邀请", "转移话题", "无法判断")                                     # after the cut below
        return blocks

    def test_tendencies_need_five_samples_and_respect_before_ms(self):
        trend = encode.tendencies(self.labelled(), before_ms=T0 + 200 * MINUTE)
        self.assertEqual([(group["intent"], [(item["label"], item["count"]) for item in group["items"]])
                          for group in trend["situational"]], [("邀请", [("接住并展开", 3), ("明确婉拒", 2)])])
        self.assertAlmostEqual(trend["situational"][0]["items"][0]["share"], 0.6)
        self.assertEqual(trend["overall"]["total"], 9, "无法判断 and unanswered rounds are not determinable")
        self.assertEqual([(item["label"], item["count"]) for item in trend["invites"]["items"]],
                         [("明确接受", 3), ("明确婉拒", 2), ("未明确答复", 1)])
        later = encode.tendencies(self.labelled())
        self.assertEqual(later["situational"][0]["total"], 6)

    def test_analysed_friend_layers_put_corrections_first_and_prefix_keeps_rules(self):
        self.add("self", 0, "在吗").add("other", 1, "在")
        self.ready()
        persona = {"dimensions": [
            {"id": "big5_e", "label": "偏低", "detail": "很少主动开话题", "confidence": "有一定依据"},
            {"id": "big5_a", "label": "偏高", "detail": "", "confidence": "较有把握"},
            {"id": "big5_o", "label": "偏高", "detail": "", "confidence": "把握较低"}],
            "selfReports": {"mbti": {"value": "INTP"}}, "values": [
                {"id": "money", "selfReport": {"text": "钱够用就行"}, "inference": None}]}
        with self.db._connection as connection:
            connection.execute("INSERT INTO friend_results(user,page,payload,deps,model_fp,created_at_ms) "
                               "VALUES(?,?,?,'{}','fp',?)", (USER, "persona", json.dumps(persona), T0))
            connection.execute("INSERT INTO friend_results(user,page,payload,deps,model_fp,created_at_ms) "
                               "VALUES(?,?,?,'{}','fp',?)", (USER, "summary", json.dumps({"summary": "最近在忙搬家"}), T0))
            connection.execute("INSERT INTO friend_scans(user,step,generation,published,source_fp,version,model_fp,"
                               "built,updated_at_ms) VALUES(?,'chat',1,1,'src-1','v','fp',1,0)", (USER,))
            for item in self.labelled():
                connection.execute(
                    "INSERT INTO friend_chat_blocks(user,generation,block_id,side,msg_ids,hashes,first_ms,last_ms,intent,"
                    "emotion,reply_to,response,invite) VALUES(?,1,?,?,'[]','[]',?,?,?,NULL,?,?,?)",
                    (USER, item["block_id"], item["side"], item["first_ms"], item["last_ms"], item["intent"],
                     item["reply_to"], item["response"], item["invite"]))
        self.plugin.core.human["observations"] = [{"id": "o1", "judgement": "嘴硬心软", "note": "",
                                                    "dimension": None, "updated_at_ms": T0 + 1}]
        self.plugin.core.human["memories"] = [{"id": "k1", "text": "怕猫", "tags_json": '["偏好与忌口"]',
                                                "status": "confirmed", "updated_at_ms": T0}]
        profile = encode.encode(self.db, self.store, USER)
        self.assertTrue(profile["analysed"])
        self.assertEqual([layer["key"] for layer in profile["layers"]],
                         ["rules", "expression", "response", "persona", "memory"])
        persona_lines = profile["layers"][3]["lines"]
        self.assertEqual(persona_lines[0], "我的观察（最可信）：嘴硬心软")
        self.assertEqual(persona_lines[1:4], ["MBTI（自述，仅作参考）：INTP", "宜人性 偏高（较有把握）",
                                              "外向性 偏低（有一定依据）：很少主动开话题"])
        self.assertNotIn("开放性", "\n".join(persona_lines), "low-confidence conclusions are left out")
        self.assertIn("金钱观（自述）：钱够用就行", persona_lines)
        self.assertEqual(profile["layers"][4]["lines"][0], "偏好与忌口：怕猫")
        self.assertIn("我邀请时：接住并展开 50%、明确婉拒 33%、转移话题 17%（6 轮）", profile["layers"][2]["lines"])
        short = encode.prefix(profile, budget=10)
        self.assertIn("没有依据时不主动道歉", short)
        self.assertNotIn("性格与三观", short)
        self.assertIn("【性格与三观】\n我的观察", encode.prefix(profile))
        self.assertEqual(profile["omittedLayers"], [])
        # A backtest (before_ms) cannot cut today's persona, values, memories and overview back in time:
        # layers 3–4 are left out whole, and the profile says so.
        cut = encode.encode(self.db, self.store, USER, before_ms=T0 + 10 * MINUTE)
        self.assertTrue(cut["analysed"])
        self.assertEqual([layer["key"] for layer in cut["layers"]], ["rules", "expression", "response"])
        self.assertEqual(cut["omittedLayers"], ["persona", "memory"])
        text = encode.prefix(cut)
        for leaked in ("嘴硬心软", "INTP", "怕猫", "搬家", "性格与三观", "关系记忆"):
            self.assertNotIn(leaked, text)


if __name__ == "__main__":
    unittest.main()
