"""kyf_plugin.timeline: the timeline functions 自我剖析 shares with 知己分析 (added 2026-11-07).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import datetime
import json
import sqlite3
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend")]
from kyf_plugin import timeline as shared
from self_contracts import SelfError
import self_timeline
from self_timeline_store import BUILTIN_TRACKS

DAY = 86_400_000


def local(year, month=1, day=1):
    return int(time.mktime(datetime.date(year, month, day).timetuple())) * 1000


def item(index, text, said):
    return {"id": f"m{index}", "text": text, "time": said, "chat": "合成好友"}


def row(i, title, **changes):
    return {"i": i, "title": title, "track": "工作事业", "kind": "point", "status": "occurred", "importance": "general",
            "phrase": "", "when": "", "precision": "unknown", "place": "", "people": "", "reason": "合成", **changes}


def event(identifier, title, start, kind="start", track="我们", category="同学共事", **changes):
    return {"id": identifier, "title": title, "track": track, "category": category, "kind": kind, "startMs": start,
            "precision": "month", "saidMs": start, "evidence": [{"messageId": identifier, "time": start}], **changes}


class TimeTests(unittest.TestCase):
    def test_relative_time_is_computed_from_the_message_date(self):
        said = local(2024, 3, 10)
        self.assertEqual(shared.resolve_relative("去年一起去的日本", said), (local(2023), local(2024) - 1, "year"))
        self.assertEqual(shared.resolve_relative("上上周三", said)[2], "day")
        self.assertEqual(shared.resolve_relative("下个月", said)[:2], (local(2024, 4), local(2024, 5) - 1))

    def test_times_before_1970_are_rejected_without_an_error(self):
        self.assertIsNone(shared.parse_user_when("1965"))
        self.assertIsNone(shared.parse_user_when("1969.12-2024"))
        with self.assertRaises(shared.TimelineError) as caught:
            shared.validate_event_fields({"when": "1965"})
        self.assertEqual(caught.exception.code, "invalid-request")
        self.assertEqual(shared.parse_user_when("2024年5月")[2], "month")


class DefaultTests(unittest.TestCase):
    def test_self_analysis_wrappers_match_the_shared_defaults(self):
        said = local(2024, 3, 10)
        items = [item(0, "去年我入职了新公司", said), item(1, "下周搬家", said)]
        reply = json.dumps({"events": [row(0, "入职", kind="start", phrase="去年"), row(1, "搬家", status="planned"),
                                       row(1, "无效主线", track="我们")]}, ensure_ascii=False)
        ours = self_timeline.parse_events(items)(reply)
        self.assertEqual(ours, shared.parse_events(items, tracks=BUILTIN_TRACKS)(reply))
        self.assertEqual([event["title"] for event in ours["events"]], ["入职", "搬家"])
        self.assertNotIn("category", ours["events"][0])
        events = [event("a", "入学", local(2019, 9), track="学业", category=""), event("b", "毕业", local(2023, 6), kind="end", track="学业", category="")]
        self.assertEqual(self_timeline.build_periods(events), shared.build_periods(events, ("track",)))
        self.assertEqual(self_timeline.build_periods(events)[0]["endMs"], local(2023, 6))

    def test_errors_keep_the_plugins_own_class(self):
        with self.assertRaises(SelfError) as caught:
            self_timeline.parse_events([])('{"chats": []}')
        self.assertEqual((caught.exception.code, caught.exception.message), ("invalid-output", shared.ECHO))
        with self.assertRaises(SelfError):
            self_timeline.validate_event_fields({"status": "maybe"})
        with self.assertRaises(shared.TimelineError):
            shared.parse_assign(1)('{"x": 1}')


class FriendParameterTests(unittest.TestCase):
    CATEGORIES = ("见面聚会", "一起出行", "同学共事", "其他")

    def test_events_without_tracks_go_on_the_default_track_with_a_category(self):
        said = local(2024, 3, 10)
        items = [item(0, "去年我们一起去了日本", said)]
        reply = json.dumps({"events": [{**row(0, "一起去日本", category="一起出行"), "track": None},
                                       {**row(0, "乱分类", category="工作")}]}, ensure_ascii=False)
        parsed = shared.parse_events(items, categories=self.CATEGORIES, default_track="我们")(reply)["events"]
        self.assertEqual([(event["title"], event["track"], event["category"]) for event in parsed], [("一起去日本", "我们", "一起出行")])
        dropped = shared.parse_events(items, categories=self.CATEGORIES, default_track="我们",
                                      accept=lambda row, event: False)(reply)
        self.assertEqual(dropped["events"], [])

    def test_periods_group_by_track_and_category(self):
        events = [event("a", "成为同事", local(2019, 3)), event("b", "每周打球", local(2023, 1), category="一起做的事"),
                  event("c", "不再同事", local(2024, 6), kind="end")]
        by_track = shared.build_periods(events)
        by_both = shared.build_periods(events, ("track", "category"))
        work = next(period for period in by_both if period["title"] == "成为同事")
        self.assertEqual((work["category"], work["endMs"]), ("同学共事", local(2024, 6)))
        self.assertEqual(next(period for period in by_track if period["title"] == "成为同事")["endMs"], local(2023, 1))

    def test_edits_can_anchor_by_evidence_alone(self):
        moved = {**event("a", "一起打球", local(2023, 1)), "track": "运动"}
        edits = [{"id": "a", "anchors": ["a"], "track": "我们", "state": "edited", "fields": {"title": "每周打球"}}]
        self.assertIsNone(shared.apply_edits([moved], edits, [])[0][1])
        self.assertEqual(shared.apply_edits([moved], edits, [], match_track=False)[0][0]["title"], "每周打球")
        deleted = [{"anchors": {"a"}, "track": "我们"}]
        self.assertEqual(shared.apply_edits([moved], [], deleted, match_track=False), [])

    def test_validation_accepts_a_category_only_from_the_list(self):
        editable = (*shared.EDITABLE_EVENT_FIELDS, "category")
        self.assertEqual(shared.validate_event_fields({"category": "其他"}, categories=self.CATEGORIES, editable=editable),
                         {"category": "其他"})
        with self.assertRaises(shared.TimelineError):
            shared.validate_event_fields({"category": "工作"}, categories=self.CATEGORIES, editable=editable)
        with self.assertRaises(shared.TimelineError):
            shared.validate_event_fields({"category": "其他"})

    def test_pages_and_monthly_counts(self):
        events = [{"id": index, "text": "字" * 100} for index in range(10)]
        page, following = shared.page_events(events, 0, 800)
        self.assertTrue(0 < len(page) < 10)
        self.assertEqual(following, len(page))
        self.assertEqual(shared.page_events(events, 9, 1), ([events[9]], None))
        self.assertEqual(shared.monthly_counts([local(2024, 5, 2), local(2024, 5, 30), local(2023, 1, 1), 0]),
                         [{"month": "2023-01", "count": 1}, {"month": "2024-05", "count": 2}])


class UndoJournalTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        self.lock = threading.RLock()
        self.db.executescript("""
            CREATE TABLE edits(user TEXT, id TEXT, title TEXT, last_seq INTEGER, PRIMARY KEY(user, id));
            CREATE TABLE undo(seq INTEGER PRIMARY KEY AUTOINCREMENT, user TEXT, actor TEXT, agent TEXT, label TEXT,
                              changes TEXT, updated_ms INTEGER);""")
        self.bumps = 0

    def journal(self, user):
        def bump():
            self.bumps += 1
        return shared.UndoJournal(self.db, self.lock, "undo", {"edits": "id"}, scope={"user": user}, actors=True,
                                  seq_column="last_seq", on_change=bump)

    def write(self, user, identifier, title, actor="user", agent=""):
        journal = self.journal(user)
        before = journal.snapshot()
        with self.db:
            self.db.execute("INSERT INTO edits(user,id,title) VALUES(?,?,?) ON CONFLICT(user,id) DO UPDATE SET title=?",
                            (user, identifier, title, title))
        return journal.push(f"改成{title}", before, actor=actor, agent=agent, limit=3)

    def title(self, user, identifier):
        rows = self.db.execute("SELECT title FROM edits WHERE user=? AND id=?", (user, identifier)).fetchall()
        return rows[0][0] if rows else None

    def test_each_source_undoes_only_its_own_changes(self):
        self.write("A", "e1", "用户改")
        self.write("A", "e2", "Agent 改", actor="agent", agent="合成客户端")
        self.assertEqual(self.journal("A").recent("agent")[0], 1)
        entry = self.journal("A").undo("user")
        self.assertEqual((entry["label"], entry["undone"]), ("改成用户改", True))
        self.assertIsNone(self.title("A", "e1"))
        self.assertEqual(self.title("A", "e2"), "Agent 改")
        self.assertIsNone(self.journal("A").undo("user"))
        self.assertEqual(self.journal("A").undo("agent")["agent"], "合成客户端")
        self.assertEqual(self.bumps, 2)

    def test_a_row_changed_later_by_the_other_source_is_skipped(self):
        self.write("A", "e1", "Agent 改", actor="agent")
        self.write("A", "e1", "用户再改")
        entry = self.journal("A").undo("agent")
        self.assertEqual(entry["undone"], False)
        self.assertEqual(self.title("A", "e1"), "用户再改")
        self.assertIsNone(self.journal("A").undo("agent"))
        self.assertEqual(self.journal("A").undo("user")["undone"], True)
        self.assertEqual(self.title("A", "e1"), "Agent 改")

    def test_depth_is_per_source_and_friends_are_isolated(self):
        for index in range(5):
            self.write("A", f"u{index}", f"用户{index}")
            self.write("A", f"a{index}", f"Agent{index}", actor="agent")
        self.write("B", "x", "另一位好友")
        self.assertEqual(self.journal("A").recent("user")[0], 3)
        self.assertEqual(self.journal("A").recent("agent")[0], 3)
        self.assertEqual(self.journal("B").recent("user")[0], 1)
        self.assertEqual(self.journal("B").undo("user")["label"], "改成另一位好友")
        self.assertEqual(self.title("A", "u4"), "用户4")
        self.assertIsNone(self.journal("A").push("无变化", self.journal("A").snapshot()))


if __name__ == "__main__":
    unittest.main()
