"""Synthetic tests for 好友时间线: the step really runs, only shared events, edits win, memory links,
custom tracks and undo kept per source.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import datetime
import json
import re
import tempfile
import unittest
from pathlib import Path

from harness import Friends, Harness

import friend_sim_encode
import friend_timeline
import friend_timeline_view as tl
from contracts import AdvisorError
from friend_timeline_store import TimelineData
from kyf_plugin.timeline import COLORS
from test_analysis import ACCOUNT, USER, ScriptedRuntime, WindowSource, msg
from test_support import FakeModelStore, wait_until

DAY = 86_400_000
BASE = 1_700_000_000_000  # 2023-11-14
LINE = re.compile(r"^\[(\d+)\] \S+ \S+ (我|对方): (.*)$", re.M)
AGENT = {"account": ACCOUNT, "user": USER, "actor": "agent", "agent": "合成 Agent"}
PAGE = {"account": ACCOUNT, "user": USER}


def event(title, category="一起做的事", status="occurred", importance="general", phrase="", when="", extra=()):
    return {"title": title, "category": category, "kind": "point", "status": status, "importance": importance,
            "phrase": phrase, "when": when, "precision": "unknown", "place": "", "reason": "合成依据",
            "extra": list(extra)}


# (keyword of the line, proposed event); ``extra`` adds earlier lines containing those keywords.
RULES = [
    ("好呀，下周六一起去", event("一起去爬山", status="planned", phrase="下周六", extra=["要不要下周六"])),
    ("我换工作了", event("对方换工作", category="其他", importance="important")),
    ("一起去冲绳吧", event("一起去冲绳", category="一起出行")),
    ("去年一起去的日本", event("一起去日本", category="一起出行", importance="important", phrase="去年")),
    ("爬山爬完腿都软了", event("一起去爬山", extra=["爬山那天"])),
    ("你要去吃饭吗", event("一起吃饭", category="见面聚会")),
    ("我们和好吧", event("吵架后和好", category="情感", extra=["是我不对"])),
    ("1965年的老照片", event("一起看老照片", when="1965")),
    ("那个展真不错", event("一起看展", when="2023-10")),
]


class TimelineRuntime(ScriptedRuntime):
    def __init__(self):
        super().__init__()
        self.rename = {}
        self.memories = []

    def answer_timeline(self, message):
        lines = LINE.findall(message)
        events = []
        for number, speaker, text in lines:
            for keyword, rule in RULES:
                if keyword in text:
                    cited = [int(other) for other, _speaker, body in lines
                             if any(word in body for word in rule["extra"])] + [int(number)]
                    item = {key: value for key, value in rule.items() if key != "extra"}
                    item["title"] = self.rename.get(item["title"], item["title"])
                    events.append({**item, "i": cited})
        return {"events": events}

    @staticmethod
    def answer_timeline_review(message):
        proposed = json.loads(message.rsplit("proposed: ", 1)[1])["events"]
        return {"events": [{"n": item["n"]} for item in proposed if item["title"] == "对方换工作"]}

    @staticmethod
    def answer_timeline_merge(message):
        group = json.loads(message)["groups"][0]
        anchors = [item["n"] for item in group if item.get("source") == "关系记忆" and "展" in item["title"]]
        shows = [item["n"] for item in group if item.get("source") is None and item["title"] == "一起看展"]
        return {"merge": [{"into": shows[0], "from": anchors}] if anchors and shows else []}

    @staticmethod
    def answer_timeline_assign(message):
        value = json.loads(message)
        names = [track["name"] for track in value["custom"]]
        return {"assign": [{"n": item["n"], "track": "爬山"} for item in value["events"]
                           if "爬山" in names and "爬山" in item["title"]]}

    def answer_extract(self, message):
        found = []
        for number, speaker, text in LINE.findall(message):
            for keyword, memory in self.memories:
                if speaker == "对方" and keyword in text:
                    found.append({**memory, "evidence": [int(number)], "sharedEventConfirmed": True})
        return {"memories": found}


def conversation():
    lines = [("self", "要不要下周六一起去爬山？"), ("other", "好呀，下周六一起去"), ("other", "我换工作了"),
             ("self", "一起去冲绳吧"), ("other", "去年一起去的日本真好玩"), ("self", "爬山那天太累了"),
             ("other", "是啊，爬山爬完腿都软了"), ("other", "你要去吃饭吗？"), ("self", "对不起，昨天是我不对"),
             ("other", "没事，我们和好吧"), ("other", "1965年的老照片"), ("other", "那个展真不错")]
    return [msg(index + 1, side, text, BASE + index * DAY // 2) for index, (side, text) in enumerate(lines)]


class TimelineCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = WindowSource(account=ACCOUNT)
        self.source.add_messages(USER, conversation())
        self.runtime = TimelineRuntime()
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

    def page(self, **params):
        return tl.view(self.friends.pages, ACCOUNT, USER, params)

    def events(self):
        return {item["title"]: item for item in self.page()["events"]}

    def edit(self, context=PAGE, **params):
        params.setdefault("expectedVersion", self.page()["version"])
        return tl.edit(self.friends.pages, ACCOUNT, USER, params, context)

    def undo(self, source="user", context=PAGE):
        return tl.undo(self.friends.pages, ACCOUNT, USER, {}, context, source)


class StepTests(TimelineCase):
    def test_estimate_counts_the_timeline_step_before_any_call(self):
        self.open_ready()
        step = next(item for item in self.friends.analysis_estimate(ACCOUNT, USER)["steps"] if item["key"] == "timeline")
        self.assertGreater(step["tokens"][1], 0)
        self.assertEqual(self.runtime.calls, [])

    def test_full_run_really_runs_the_step_and_an_empty_update_calls_nothing(self):
        run = self.analyse()
        self.assertEqual(run["state"], "done", run)
        step = next(item for item in run["steps"] if item["key"] == "timeline")
        self.assertEqual(step["state"], "done", step)
        self.assertGreater(step["calls"], 0)
        self.assertGreater(self.runtime.count("timeline"), 0)
        self.assertEqual(self.runtime.count("timeline_review"), 1)
        events = self.events()
        self.assertEqual(set(events), {"一起去爬山", "一起去日本", "吵架后和好", "一起看老照片", "一起看展"})
        self.assertEqual(events["一起去爬山"]["status"], "occurred", "the plan and its result are one event")
        self.assertEqual(events["一起去爬山"]["evidenceCount"], 4)
        japan = events["一起去日本"]
        self.assertEqual((japan["precision"], datetime.datetime.fromtimestamp(japan["startMs"] / 1000).year),
                         ("year", 2022), "去年 is resolved from the date it was said")
        self.assertEqual((japan["importance"], japan["category"]), ("important", "一起出行"))
        self.assertEqual(events["吵架后和好"]["category"], "情感")
        self.assertIsNone(events["一起看老照片"]["startMs"], "a time before 1970 goes to 时间不明")
        page = self.page()
        self.assertEqual(page["categories"][6], "情感")
        self.assertEqual([track["name"] for track in page["tracks"]], ["我们"])
        self.assertEqual(sum(item["count"] for item in page["frequency"]), 12)
        self.assertEqual(page["coverage"]["earliestMs"], BASE)
        calls = len(self.runtime.calls)
        estimate = self.friends.analysis_estimate(ACCOUNT, USER)
        self.assertEqual(next(item for item in estimate["steps"] if item["key"] == "timeline")["tokens"], [0, 0])
        version = page["version"]
        self.friends.analysis_start(ACCOUNT, USER, None, "update")
        self.assertEqual(self.wait_run()["state"], "done")
        self.assertEqual(len(self.runtime.calls), calls, "no new messages: no scan, merge or assign call")
        self.assertEqual(self.page()["version"], version, "an unchanged organize does not bump the version")

    def test_only_shared_events_with_ta_are_kept(self):
        self.analyse()
        events = self.events()
        self.assertNotIn("一起去冲绳", events, "only my proposal, none of ta's lines")
        self.assertNotIn("一起吃饭", events, "ta only asked; nothing shows it happened")
        self.assertNotIn("对方换工作", events, "the review dropped ta's own experience")

    def test_new_messages_scan_only_the_new_segment(self):
        self.analyse()
        before = len([1 for kind, _m in self.runtime.calls if kind == "timeline"])
        later = BASE + 300 * DAY
        self.source.add_messages(USER, [msg(100, "self", "最近呢", later), msg(101, "other", "那个展真不错", later + 60_000)])
        self.friends.analysis_start(ACCOUNT, USER, None, "update")
        self.assertEqual(self.wait_run()["state"], "done")
        prompts = [message for kind, message in self.runtime.calls if kind == "timeline"][before:]
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("爬山", prompts[0])


class EditTests(TimelineCase):
    def test_user_edits_survive_a_rebuild_and_deleted_events_stay_hidden(self):
        self.analyse()
        events = self.events()
        result = self.edit(target="event", action="edit", id=events["一起去爬山"]["id"],
                           fields={"when": "2023.11", "importance": "important", "category": "一起出行"})
        self.assertEqual(result["editLabel"], "修改“一起去爬山”")
        self.edit(target="event", action="delete", id=events["吵架后和好"]["id"])
        added = self.edit(target="event", action="add", fields={"title": "一起吃火锅", "when": "2021"})
        self.runtime.rename = {"吵架后和好": "和好了"}
        self.assertEqual(self.analyse("rebuild")["state"], "done")
        events = self.events()
        hike = events["一起去爬山"]
        self.assertEqual((hike["precision"], hike["importance"], hike["category"], hike["level"]),
                         ("month", "important", "一起出行", "confirmed"))
        self.assertNotIn("和好了", events, "a deleted event stays hidden under a new title")
        self.assertEqual((events["一起吃火锅"]["track"], events["一起吃火锅"]["manual"], events["一起吃火锅"]["id"]),
                         ("我们", True, added["editedId"]))

    def test_version_conflict_bad_fields_and_read_only_rows(self):
        self.analyse()
        page = self.page()
        target = page["events"][0]["id"]
        with self.assertRaises(AdvisorError) as caught:
            self.edit(target="event", action="confirm", id=target, expectedVersion=page["version"] - 1)
        self.assertEqual(caught.exception.code, "conflict")
        for fields in ({"category": "旅行"}, {"track": "工作"}, {"when": "1960"}):
            with self.assertRaises(AdvisorError):
                self.edit(target="event", action="edit", id=target, fields=fields)
        with self.assertRaises(AdvisorError) as caught:
            self.edit(target="event", action="delete", id="memory:abc")
        self.assertEqual(caught.exception.code, "read-only")

    def test_paging_honours_page_bytes(self):
        self.analyse()
        first = self.page(pageBytes=1)
        self.assertEqual((len(first["events"]), first["nextOffset"], first["eventsTotal"]), (1, 1, 5))
        rest = self.page(offset=1)
        self.assertIsNone(rest["nextOffset"])
        self.assertEqual(len(rest["events"]), 4)
        evidence = tl.evidence(self.friends.pages, ACCOUNT, USER, {"id": self.events()["一起去爬山"]["id"]})
        self.assertEqual(evidence["total"], 4)
        self.assertEqual({item["side"] for item in evidence["items"]}, {"self", "other"})
        self.assertTrue(all(item["status"] == "ok" for item in evidence["items"]))


class TrackTests(TimelineCase):
    def test_custom_track_assign_reruns_only_assign_and_delete_returns_to_us(self):
        self.analyse()
        with self.assertRaises(AdvisorError):
            self.edit(target="track", action="add", fields={"title": "全部", "color": COLORS[1]})
        self.edit(target="track", action="add", fields={"title": "爬山", "description": "一起爬山", "color": COLORS[2]})
        self.assertEqual([track["name"] for track in self.page()["tracks"]], ["我们", "爬山"])
        estimate = self.friends.analysis_estimate(ACCOUNT, USER)
        self.assertGreater(next(item for item in estimate["steps"] if item["key"] == "timeline")["tokens"][1], 0)
        calls = len(self.runtime.calls)
        self.friends.analysis_start(ACCOUNT, USER, None, "update")
        self.assertEqual(self.wait_run()["state"], "done")
        self.assertEqual([kind for kind, _m in self.runtime.calls[calls:] if kind.startswith("timeline")],
                         ["timeline_assign"], "changing tracks re-runs only the assignment")
        self.assertEqual(self.events()["一起去爬山"]["track"], "爬山")
        moved = self.edit(target="event", action="edit", id=self.events()["一起去日本"]["id"], fields={"track": "爬山"})
        self.assertEqual({item["title"]: item["track"] for item in moved["events"]}["一起去日本"], "爬山")
        self.edit(target="track", action="delete", id="爬山")
        events = self.events()
        self.assertEqual((events["一起去爬山"]["track"], events["一起去日本"]["track"]), ("我们", "我们"))
        with self.assertRaises(AdvisorError):
            self.edit(target="track", action="delete", id="我们")
        restored = self.undo()
        self.assertEqual(restored["undone"], "删除“爬山”")
        self.assertEqual({item["title"]: item["track"] for item in restored["events"]}["一起去日本"], "爬山")

    def test_tracks_and_undo_are_per_friend(self):
        self.analyse()
        self.edit(target="track", action="add", fields={"title": "工作", "color": COLORS[1]})
        other = TimelineData(self.harness.accounts.account(ACCOUNT), "wxid_other_friend")
        self.assertEqual(other.track_names(), ["我们"])
        self.assertEqual(other.journal().recent("user"), (0, []))
        self.assertIsNone(other.journal().undo(actor="user"))
        self.assertEqual(self.page()["undo"]["user"]["count"], 1)


class UndoTests(TimelineCase):
    def test_each_source_undoes_only_its_own_edits(self):
        self.analyse()
        events = self.events()
        hike, japan = events["一起去爬山"]["id"], events["一起去日本"]["id"]
        self.edit(target="event", action="confirm", id=hike)
        agent = self.edit(AGENT, target="event", action="edit", id=japan, fields={"title": "一起去东京"})
        self.assertEqual(agent["undo"]["agent"]["count"], 1)
        self.assertEqual(agent["undo"]["agent"]["items"][0]["agent"], "合成 Agent")
        self.assertIsNotNone({item["id"]: item for item in agent["events"]}[japan]["agentEditedMs"])
        with self.assertRaises(AdvisorError) as caught:
            self.undo("user", AGENT)
        self.assertEqual(caught.exception.code, "forbidden")
        result = self.undo("user")
        self.assertEqual(result["undone"], "确认“一起去爬山”")
        self.assertIn("一起去东京", {item["title"] for item in result["events"]}, "the Agent's edit stays")
        result = self.undo("agent", AGENT)
        self.assertEqual((result["undone"], result["skipped"]), ("修改“一起去东京”", None))
        self.assertIn("一起去日本", {item["title"] for item in result["events"]})
        self.assertEqual(self.undo("agent", AGENT)["undone"], None)

    def test_a_later_edit_by_the_other_source_makes_undo_skip(self):
        self.analyse()
        japan = self.events()["一起去日本"]["id"]
        self.edit(AGENT, target="event", action="edit", id=japan, fields={"title": "一起去东京"})
        self.edit(target="event", action="edit", id=japan, fields={"title": "一起去大阪"})
        result = self.undo("agent", AGENT)
        self.assertIsNone(result["undone"])
        self.assertEqual(result["skipped"]["label"], "修改“一起去东京”")
        self.assertIn("一起去大阪", {item["title"] for item in result["events"]})
        self.assertEqual(result["undo"]["agent"]["count"], 0, "the stale entry is dropped")
        self.assertEqual(self.undo("user")["undone"], "修改“一起去大阪”")

    def test_actor_comes_from_context_and_agent_writes_do_not_push_out_user_history(self):
        self.analyse()
        added = self.edit(target="event", action="add", fields={"title": "一起吃火锅"}, actor="agent")
        self.assertEqual((added["undo"]["user"]["count"], added["undo"]["agent"]["count"]), (1, 0),
                         "an actor in params is ignored")
        manual = added["editedId"]
        for index in range(205):
            self.edit(AGENT, target="event", action="edit", id=manual,
                      fields={"importance": "important" if index % 2 == 0 else "general"})
        undo = self.page()["undo"]
        self.assertEqual((undo["user"]["count"], undo["agent"]["count"]), (1, 200))
        with self.assertRaises(AdvisorError):
            self.edit(AGENT, target="event", action="unlink", id=manual, memoryId="m1")


class MemoryLinkTests(TimelineCase):
    def test_shared_event_memories_link_once_and_unlinked_ones_show_read_only(self):
        self.runtime.memories = [("爬山爬完腿都软了", {"text": "对方和我一起爬山爬完腿软", "tags": ["共同事件"],
                                                      "eventTime": "2023-11"})]
        self.analyse()
        core = self.harness.service.friends
        calls = len(self.runtime.calls)
        pending = core.memories(ACCOUNT, USER, status="pending")["items"]
        self.assertEqual([item["text"] for item in pending], ["对方和我一起爬山爬完腿软"])
        core.memory_action(ACCOUNT, USER, {"action": "confirm", "id": pending[0]["id"]})
        self.wait_run()
        for text, when in (("我们一起看过那个展", "2023-10"), ("我们一起打过羽毛球", "2022")):
            core.memory_action(ACCOUNT, USER, {"action": "create", "text": text, "tags": ["共同事件"], "eventTime": when})
            self.wait_run()
        core.memory_action(ACCOUNT, USER, {"action": "create", "text": "对方去过冰岛", "tags": ["经历"],
                                           "eventTime": "2021"})
        self.wait_run()
        self.friends.analysis_start(ACCOUNT, USER, None, "update")
        self.assertEqual(self.wait_run()["state"], "done")
        kinds = {kind for kind, _m in self.runtime.calls[calls:] if kind.startswith("timeline")}
        self.assertEqual(kinds, {"timeline_merge"}, "memory anchors join the merge; nothing is scanned again")
        page = self.page()
        events = {item["title"]: item for item in page["events"]}
        self.assertEqual(events["一起去爬山"]["memory"]["text"], "对方和我一起爬山爬完腿软")
        self.assertEqual(events["一起去爬山"]["level"], "confirmed")
        self.assertEqual(events["一起看展"]["memory"]["text"], "我们一起看过那个展", "linked through the merge")
        self.assertEqual([ref["text"] for ref in page["memoryRefs"]], ["我们一起打过羽毛球"],
                         "an unlinked 共同事件 shows read-only; 经历 never does")
        self.assertTrue(page["memoryRefs"][0]["readOnly"])
        self.assertEqual(len([item for item in page["events"] if item["memory"]]), 2)
        before = json.dumps(core.memories(ACCOUNT, USER, status="confirmed")["items"], sort_keys=True, ensure_ascii=False)
        hike = events["一起去爬山"]
        unlinked = self.edit(target="event", action="unlink", id=hike["id"], memoryId=hike["memory"]["id"])
        self.assertIsNone({item["id"]: item for item in unlinked["events"]}[hike["id"]]["memory"])
        self.assertIn("对方和我一起爬山爬完腿软", [ref["text"] for ref in unlinked["memoryRefs"]])
        self.edit(target="event", action="delete", id=events["一起看展"]["id"])
        page = self.page()
        self.assertNotIn("我们一起看过那个展", [ref["text"] for ref in page["memoryRefs"]],
                         "deleting a linked event hides it from the timeline only")
        self.assertEqual(json.dumps(core.memories(ACCOUNT, USER, status="confirmed")["items"], sort_keys=True,
                                    ensure_ascii=False), before, "the timeline never changes 关系记忆")


class ProfileTests(TimelineCase):
    """模拟档案 2b “我们的经历”: which events, the user's first, memories once, tracks, backtests cut."""

    def scope(self):
        _account, _user, _fp, database, store = self.friends.pages._scope(ACCOUNT, USER)
        return database, store

    def layers(self, before_ms=None):
        database, store = self.scope()
        profile = friend_sim_encode.encode(database, store, USER, before_ms=before_ms)
        return profile, {layer["key"]: layer["lines"] for layer in profile["layers"]}

    def lines(self, now):
        database, _store = self.scope()
        return tl.profile(database, USER, now=now)[0]

    def test_recent_important_and_planned_events_and_the_users_first(self):
        self.analyse()
        _profile, layers = self.layers()
        self.assertEqual(layers["shared"], ["2022年 一起去日本（重要）"], "long ago: only the important one")
        self.assertEqual(self.lines(BASE + 300 * DAY), ["2022年 一起去日本（重要）"])
        self.assertEqual(self.lines(BASE + 7 * DAY),
                         ["2023-11-20 提到 一起看老照片", "2023-11-19 提到 吵架后和好", "2023-11-18 提到 一起去爬山",
                          "2023年10月 一起看展", "2022年 一起去日本（重要）"],
                         "the last six months, newest first; an undated event counts from when it was said")
        events = self.events()
        self.edit(target="event", action="edit", id=events["一起看展"]["id"], fields={"title": "一起看画展"})
        self.edit(target="event", action="add", fields={"title": "一起去冲绳", "when": "2099", "status": "planned"})
        self.edit(target="event", action="delete", id=events["吵架后和好"]["id"])
        lines = self.lines(BASE + 7 * DAY)
        self.assertEqual(lines[:2], ["2099年 一起去冲绳（计划）", "2023年10月 一起看画展"],
                         "the user's versions first, newest first; a plan still ahead is kept")
        self.assertNotIn("吵架后和好", "\n".join(lines))

    def test_memory_written_once_and_layer_4_skips_it(self):
        self.runtime.memories = [("爬山爬完腿都软了", {"text": "对方和我一起爬山爬完腿软", "tags": ["共同事件"],
                                                      "eventTime": "2023-11"})]
        self.analyse()
        core = self.harness.service.friends
        pending = core.memories(ACCOUNT, USER, status="pending")["items"]
        core.memory_action(ACCOUNT, USER, {"action": "confirm", "id": pending[0]["id"]})
        self.wait_run()
        _profile, layers = self.layers()
        self.assertIn("2023年11月 一起去爬山（关系记忆：对方和我一起爬山爬完腿软）", layers["shared"],
                      "a linked memory counts as confirmed and is written with its event")
        self.assertNotIn("对方和我一起爬山爬完腿软", "\n".join(layers.get("memory", [])), "layer 4 skips it")

    def test_custom_tracks_make_sections(self):
        self.analyse()
        self.edit(target="track", action="add", fields={"title": "爬山", "description": "一起爬山", "color": COLORS[2]})
        self.friends.analysis_start(ACCOUNT, USER, None, "update")
        self.assertEqual(self.wait_run()["state"], "done")
        lines = self.lines(BASE + 7 * DAY)
        self.assertEqual(lines[0], "主线「我们」：")
        self.assertLess(lines.index("主线「我们」："), lines.index("主线「爬山」："))
        self.assertEqual(lines[lines.index("主线「爬山」："):], ["主线「爬山」：", "2023-11-18 提到 一起去爬山"])

    def test_backtest_keeps_only_events_said_before_the_cut_without_edits(self):
        self.analyse()
        self.edit(target="event", action="edit", id=self.events()["一起去日本"]["id"], fields={"title": "一起去东京"})
        self.edit(target="event", action="add", fields={"title": "一起去冲绳", "when": "2099", "status": "planned"})
        cut = BASE + 2 * DAY + 1  # 日本 was said at line 5; the hike's lines go on until line 7
        profile, layers = self.layers(before_ms=cut)
        self.assertEqual(layers["shared"], ["2022年 一起去日本（重要）"])
        text = friend_sim_encode.prefix(profile)
        for leaked in ("东京", "冲绳", "爬山", "和好"):
            self.assertNotIn(leaked, text)
        _profile, layers = self.layers(before_ms=BASE)
        self.assertNotIn("shared", layers)


if __name__ == "__main__":
    unittest.main()
