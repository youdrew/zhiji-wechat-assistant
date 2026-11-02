"""Synthetic relationship trend tests: per-message facts, turns, reply rules and coverage.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import unittest
from datetime import datetime

import harness  # noqa: F401  (puts the plugin backend on the import path)
from friend_trends import START, TURN, relationship_trends

READY = {"state": "ready", "readCount": 0}


def stamp(*parts):
    return int(datetime(*parts).astimezone().timestamp() * 1000)


class Rows:
    def __init__(self):
        self.rows = []

    def add(self, side, when, text="好", kind="text", type_name="文本"):
        self.rows.append({"seq": len(self.rows) + 1, "side": side, "time_ms": when, "kind": kind, "text": text,
                          "type": type_name})
        return self


def times(result):
    total, values = 0, []
    for delta in result["messages"]["time"]:
        total += delta
        values.append(total)
    return values


class TrendTests(unittest.TestCase):
    now = stamp(2026, 10, 15, 12, 0)

    def test_long_history_is_sent_whole_in_time_order(self):
        rows = Rows().add("self", stamp(2026, 10, 1, 9)).add("other", stamp(2021, 3, 5, 9))
        result = relationship_trends(rows.rows, context_state=READY, now_ms=self.now)
        self.assertEqual(times(result), [stamp(2021, 3, 5, 9), stamp(2026, 10, 1, 9)],
                         "delta-encoded times decode to the whole readable history, oldest first")
        self.assertEqual(result["messages"]["side"], [1, 0])
        self.assertEqual((result["history"]["firstMessageMs"], result["history"]["lastMessageMs"]),
                         (stamp(2021, 3, 5, 9), stamp(2026, 10, 1, 9)))
        self.assertTrue(result["history"]["complete"])
        self.assertEqual(result["insufficient"], [])

    def test_characters_count_text_only_and_kinds_name_the_rest(self):
        base = stamp(2026, 10, 1, 10)
        rows = Rows().add("other", base, "今天 好累 [流泪]\n").add("self", base + 1000, "[图片]", "image", "图片")
        rows.add("self", base + 2000, "[语音]", "other", "语音").add("other", base + 3000, "", "other", "动画表情")
        rows.add("other", base + 4000, "[位置]", "other", "位置").add("self", base + 5000, "收到", "text", "文件/链接/卡片")
        result = relationship_trends(rows.rows, context_state=READY, now_ms=self.now)
        kinds = [result["kinds"][index] for index in result["messages"]["kind"]]
        self.assertEqual(kinds, ["text", "image", "voice", "sticker", "other", "text"])
        self.assertEqual(result["messages"]["chars"], [5, 0, 0, 0, 0, 2],
                         "spaces are ignored, an inline emoji code is one character, media has no characters")

    def test_turns_merge_bursts_and_restart_after_a_long_gap(self):
        base = stamp(2026, 10, 1, 10)
        rows = Rows().add("other", base).add("other", base + 5000).add("other", base + 9000)   # one burst
        rows.add("self", base + 60_000).add("self", base + 70_000)                              # my turn
        rows.add("self", base + 9 * 3600_000)                                                   # new conversation
        result = relationship_trends(rows.rows, context_state=READY, now_ms=self.now)
        flags = result["messages"]["flags"]
        self.assertEqual([bool(flag & TURN) for flag in flags], [True, False, False, True, False, True])
        self.assertEqual([bool(flag & START) for flag in flags], [False, False, False, False, False, True],
                         "the very first message is left-censored; an 8 hour gap starts a conversation")
        self.assertEqual(result["replies"], [[3, 51]], "my first message after the burst replies to its last one")

    def test_reply_rules_never_guess_across_unknown_or_ambiguous_messages(self):
        base = stamp(2026, 10, 1, 10)
        rows = Rows().add("other", base).add("self", base + 60_000)          # one valid reply
        rows.add("other", base + 120_000).add("system", base + 150_000).add("self", base + 180_000)  # barrier
        rows.add("other", base + 240_000).add("self", base + 240_000)        # same second: ambiguous
        rows.add("other", base + 300_000).add("other", None).add("self", base + 360_000)  # untimed barrier
        rows.add("other", base + 2 * 86400_000)                               # a new conversation, not a reply
        result = relationship_trends(rows.rows, context_state=READY, now_ms=self.now)
        self.assertEqual([seconds for _index, seconds in result["replies"]], [60, 60],
                         "me after the first message, then the other person after me; nothing across a barrier")
        self.assertEqual(result["coverage"]["unknownDirection"], 1)
        self.assertEqual(result["coverage"]["untimed"], 1)
        self.assertEqual(result["coverage"]["ambiguousSameTime"], 1)
        flags = result["messages"]["flags"]
        same_second = [index for index, delta in enumerate(result["messages"]["time"]) if delta == 0]
        self.assertEqual(len(same_second), 1)
        self.assertTrue(flags[same_second[0]] & TURN and flags[same_second[0] - 1] & TURN,
                        "both authors of an ambiguous second get a turn")

    def test_reply_waits_from_the_first_burst_and_not_across_a_new_conversation(self):
        base = stamp(2026, 10, 1, 10)
        rows = Rows().add("other", base).add("other", base + 20_000)            # one burst, waiting from +20s
        rows.add("other", base + 2 * 3600_000)                                   # a follow-up two hours later
        rows.add("self", base + 2 * 3600_000 + 60_000)                           # I answer a minute after it
        rows.add("other", base + 2 * 3600_000 + 90_000)                          # they answer me in 30 seconds
        rows.add("self", base + 12 * 3600_000)                                   # 10 hours later: a new conversation
        result = relationship_trends(rows.rows, context_state=READY, now_ms=self.now)
        self.assertEqual([seconds for _index, seconds in result["replies"]], [2 * 3600 + 40, 30],
                         "my wait counts from their first burst; the follow-up does not restart it")
        self.assertTrue(result["messages"]["flags"][-1] & START, "after 8 hours the message opens a conversation")

    def test_future_messages_are_excluded(self):
        rows = Rows().add("other", stamp(2026, 10, 15, 11)).add("self", self.now + 3600_000)
        result = relationship_trends(rows.rows, context_state=READY, now_ms=self.now)
        self.assertEqual((len(result["messages"]["time"]), result["coverage"]["future"]), (1, 1))

    def test_unready_and_empty_history_report_insufficient_data(self):
        reading = relationship_trends(Rows().add("other", stamp(2026, 9, 1, 9)).rows,
                                      context_state={"state": "reading", "readCount": 1}, now_ms=self.now)
        self.assertFalse(reading["history"]["complete"])
        self.assertIn("聊天历史仍在读取或读取失败，统计可能不完整", reading["insufficient"])
        empty = relationship_trends([], context_state=READY, now_ms=self.now)
        self.assertEqual((empty["messages"]["time"], empty["history"]["firstMessageMs"]), ([], None))
        self.assertIn("可读历史中暂无可统计的消息", empty["insufficient"])


if __name__ == "__main__":
    unittest.main()
