"""自我剖析·时间线: time math, validation, merge, edits and version conflicts on synthetic data only.

Fake responses test validation and caching, not the model's semantic accuracy.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import datetime
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend"),
                str(ROOT / "plugins/kyf.self-analysis/tests")]
from self_contracts import SelfError
from self_service import Service
from self_store import Store
from self_timeline import (TIMELINE_ASSIGN, TIMELINE_MERGE, TIMELINE_REVIEW, TIMELINE_WINDOW, TimelineExtract,
                           TimelineOrganize, _apply_edits, _merge_candidates, birth_year_of, build_periods,
                           display_when, event_id, parse_events, parse_review, parse_when, resolve_relative)
from self_timeline_store import BUILTIN_TRACKS, TimelineStore

DAY = 86_400_000
BASE = 1_700_000_000_000  # 2023-11-14 late evening UTC; local dates come from time.localtime


class _FakeModels:
    """Replies keyed by system prompt; records every system prompt it was asked."""

    def __init__(self, replies=None, service=None):
        self.replies, self.systems, self.calls = dict(replies or {}), [], []
        self.service_value = service

    def service(self):
        return dict(self.service_value) if self.service_value is not None else {"contextTokens": 32768}

    def generate(self, context, request, step=None):
        self.systems.append(request.get("system"))
        self.calls.append((request.get("system"), request.get("prompt")))
        return {"text": self.replies.get(request.get("system"), json.dumps({"merge": [], "assign": []}))}


class _JobContext:
    def check(self):
        return None

    def progress(self, **values):
        pass


def _corpus_row(rowid=1, text="我上个月去了日本玩", identifier="m1"):
    return {"rowid": rowid, "id": identifier, "name": "合成朋友", "is_group": False, "time_ms": BASE,
            "text": text, "user": "我：", "context_before": [], "context_after": [], "context_info": {}}


class _CorpusStub:
    def __init__(self, rows):
        self.rows = rows

    def meta(self, key, default=None):
        return "synthetic-source" if key == "source" else default

    def counts(self):
        return {}

    def iter_messages(self, after_id=0, limit=100):
        return [row for row in self.rows if row["rowid"] > after_id][:limit]


def window_item(index, text, said=BASE, identifier=None):
    return {"i": index, "id": identifier or f"m{index}", "chat": "合成朋友", "group": False, "time": said,
            "part": 1, "parts": 1, "text": text, "context": {"before": [], "after": [], "truncated": False}}


def model_event(index, **overrides):
    event = {"i": index, "title": "去日本旅行", "track": "旅行出行", "kind": "point", "status": "occurred",
             "importance": "general", "phrase": "", "when": "", "precision": "unknown", "place": "日本",
             "people": "", "reason": "本人明确说过这趟行程"}
    event.update(overrides)
    return event


def parse_reply(*events):
    return json.dumps({"events": list(events)}, ensure_ascii=False)


def plugin_stub(data_dir):
    # Only data_dir is used by store-level tests; jobs get their own stubs below.
    return SimpleNamespace(data_dir=Path(data_dir), log=lambda *args: None)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = plugin_stub(self.temp.name)
        self.store = Store(self.plugin.data_dir, "synthetic-account")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_creates_timeline_tables_and_seeds_builtin_tracks(self):
        timeline = TimelineStore(self.store)
        tables = {row["name"] for row in self.store.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("timeline_meta", "timeline_cache", "timeline_active", "timeline_staging",
                      "timeline_events", "timeline_periods", "timeline_edits", "timeline_deleted", "timeline_tracks"):
            self.assertIn(table, tables)
        names = [track["name"] for track in timeline.tracks()]
        self.assertEqual([name for name in names if name in BUILTIN_TRACKS], list(BUILTIN_TRACKS))

    def test_activation_and_prepared_marker_rollback_together(self):
        timeline = TimelineStore(self.store)
        timeline.stage([(0, "synthetic-fingerprint", {"id": "synthetic-message"})])
        with patch.object(timeline, "_meta", side_effect=RuntimeError("synthetic interruption")):
            with self.assertRaises(RuntimeError):
                timeline.activate()
        self.assertFalse(timeline.meta("prepared", False))
        self.assertEqual(timeline.active_counts()["total"], 0)
        self.assertEqual(len(timeline.rows("SELECT position FROM timeline_staging")), 1)
        timeline.activate()
        self.assertTrue(timeline.meta("prepared"))
        self.assertEqual(timeline.active_counts()["total"], 1)
        self.assertEqual(timeline.rows("SELECT position FROM timeline_staging"), [])

    def test_reset_keeps_timeline_and_manual_content(self):
        timeline = TimelineStore(self.store)
        timeline.replace_events([{"id": "e1", "title": "毕业", "track": "学业", "startMs": 1,
                                  "evidence": [{"messageId": "m1"}]}])
        timeline.add_edit("event", "confirm", "e1", anchors=("m1",))
        timeline.add_edit("event", "add", "manual-1", fields={"title": "手动事件", "track": "其他"})
        timeline.set_track("创业", color="#07c160", description="和小店经营有关的事")
        self.store.reset()
        timeline = TimelineStore(self.store)
        self.assertEqual([event["title"] for event in timeline.events()], ["毕业"])
        self.assertEqual(timeline.edits("event")[0]["id"], "e1")
        self.assertEqual([edit["id"] for edit in timeline.edits("event") if edit["state"] == "manual"], ["manual-1"])
        self.assertIn("创业", timeline.track_names(custom_only=True))

    def test_version_bumps_and_conflicts_come_from_the_service(self):
        timeline = TimelineStore(self.store)
        before = timeline.version()
        timeline.add_edit("event", "confirm", "e1", anchors=("m1",))
        self.assertEqual(timeline.version(), before + 1)

    def test_track_visibility_and_custom_tracks(self):
        timeline = TimelineStore(self.store)
        timeline.set_track("健康", visible=False)
        self.assertFalse(next(track for track in timeline.tracks() if track["name"] == "健康")["visible"])
        with self.assertRaises(SelfError):
            timeline.delete_track("健康")
        with self.assertRaises(SelfError):
            timeline.set_track("旅行出行", color="#123456")
        timeline.set_track("带娃", color="#576b95", description="和带孩子有关的事")
        timeline.set_track("带娃", description="改过的说明")
        timeline.delete_track("带娃")
        self.assertNotIn("带娃", timeline.track_names())

    def test_confirm_keeps_user_fields_and_unconfirm_demotes(self):
        timeline = TimelineStore(self.store)
        timeline.add_edit("event", "edit", "e1", anchors=("m1",), fields={"title": "硕士毕业", "startMs": 150})
        timeline.add_edit("event", "confirm", "e1")
        row = timeline.edits("event")[0]
        self.assertEqual((row["state"], row["fields"]), ("confirmed", {"title": "硕士毕业", "startMs": 150}),
                         "confirming must not wipe the user's edited fields")
        timeline.add_edit("event", "unconfirm", "e1")
        row = timeline.edits("event")[0]
        self.assertEqual((row["state"], row["fields"]), ("edited", {"title": "硕士毕业", "startMs": 150}),
                         "unconfirm keeps the fields, it only drops the confirmed state")
        timeline.add_edit("event", "confirm", "e1")
        timeline.add_edit("event", "unconfirm", "e1")
        self.assertEqual(timeline.edits("event")[0]["state"], "edited", "unconfirm is idempotent while fields exist")
        timeline.add_edit("event", "confirm", "e2")
        timeline.add_edit("event", "unconfirm", "e2")
        self.assertEqual([row["id"] for row in timeline.edits("event")], ["e1"],
                         "a fieldless confirm is removed by unconfirm")

    def test_delete_track_with_unknown_name_is_a_noop(self):
        timeline = TimelineStore(self.store)
        timeline.delete_track("不存在的主线")  # must not raise IndexError
        self.assertNotIn("不存在的主线", timeline.track_names())

    def test_restore_clears_the_deleted_anchor_set(self):
        timeline = TimelineStore(self.store)
        timeline.add_deleted("e1", ("m1", "m2"), track="学业")
        timeline.add_edit("event", "restore", "e1")
        self.assertEqual(timeline.deleted_sets(), [], "a restored event must be recoverable, not re-hidden")

    def test_events_are_ordered_newest_first(self):
        timeline = TimelineStore(self.store)
        timeline.replace_events([
            {"id": "old", "title": "旧事", "track": "学业", "startMs": 100, "evidence": []},
            {"id": "new", "title": "新事", "track": "学业", "startMs": 900, "evidence": []},
            {"id": "unknown", "title": "时间不明", "track": "学业", "startMs": None, "evidence": []}])
        self.assertEqual([event["id"] for event in timeline.events()], ["new", "old", "unknown"])

    def test_deleted_anchor_sets_and_edit_removal(self):
        timeline = TimelineStore(self.store)
        timeline.add_deleted("e1", ("m1", "m2"), track="学业")
        sets = timeline.deleted_sets()
        self.assertEqual(sets[0]["anchors"], {"m1", "m2"})
        timeline.drop_deleted("e1")
        self.assertEqual(timeline.deleted_sets(), [])
        timeline.add_edit("event", "confirm", "e1", anchors=("m1",))
        timeline.add_edit("event", "unconfirm", "e1")
        self.assertEqual(timeline.edits("event"), [])


class TimeMathTests(unittest.TestCase):
    def test_parse_when_covers_year_month_day_season_and_ranges(self):
        start, end, precision = parse_when("2023")
        self.assertEqual(precision, "year")
        self.assertLess(end - start, 367 * DAY)
        self.assertEqual(parse_when("2024-02-30"), None)
        self.assertEqual(parse_when("2024-13"), None)
        start, end, precision = parse_when("2022-夏")
        self.assertEqual(precision, "season")
        self.assertEqual(parse_when("2024-05-01-2024-05-05")[2], "day")
        self.assertEqual(parse_when("2024-05-01-2024-05-05")[1] - parse_when("2024-05-01-2024-05-05")[0],
                         4 * DAY + DAY - 1)

    def test_relative_phrases_are_computed_from_the_said_date(self):
        # BASE is 2023-11-14/15 local; the program, not the model, owns the arithmetic.
        resolved = resolve_relative("去年", BASE)
        self.assertEqual(resolved[2], "year")
        self.assertEqual(display_when(*resolved).startswith("2022"), True)
        resolved = resolve_relative("上个月", BASE)
        self.assertEqual(resolved[2], "month")
        self.assertEqual(resolve_relative("大三那年", BASE), None, "only common relative phrases are overridden")

    def test_birth_year_is_read_from_the_profile(self):
        self.assertEqual(birth_year_of("我1995年出生，现在在杭州"), 1995)
        self.assertIsNone(birth_year_of("住在杭州"))


class ExtractionTests(unittest.TestCase):
    def test_quotes_time_phrases_and_relative_overrides(self):
        items = [window_item(0, "我上个月去了日本玩", identifier="m1")]
        payload = parse_events(items)(parse_reply(model_event(0, phrase="上个月", when="2019-01-01", precision="day")))
        event = payload["events"][0]
        self.assertEqual((event["messageId"], event["phrase"]), ("m1", "上个月"))
        self.assertEqual(event["precision"], "month", "the program overrides the model's relative time")
        self.assertEqual(event["startMs"], resolve_relative("上个月", BASE)[0], "a wrong model date must lose")

    def test_relative_matching_takes_the_longest_expression(self):
        # 上周五是那一天，不是整个上周；上上周三差两周；去年底落在 12 月。
        resolved = resolve_relative("上周五", BASE)
        self.assertEqual(resolved[2], "day")
        self.assertEqual(datetime.date.fromtimestamp(resolved[0] / 1000).weekday(), 4)
        self.assertLess(resolve_relative("上上周三", BASE)[0], resolve_relative("上周三", BASE)[0] - 6 * DAY)
        end = resolve_relative("去年底", BASE)
        self.assertEqual((end[2], datetime.date.fromtimestamp(end[1] / 1000).month), ("month", 12))
        last = resolve_relative("上个月底", BASE)
        self.assertEqual(last[2], "day")
        self.assertEqual(datetime.date.fromtimestamp((last[1] + 1) / 1000).day, 1,
                         "上个月底 ends on the last day of the previous month")
        self.assertEqual(resolve_relative("下下周", BASE)[0], resolve_relative("下周", BASE)[0] + 7 * DAY)

    def test_model_about_precision_survives_and_non_relative_infers(self):
        # “约某年”不再被抹掉（doc 2.1）。
        about = parse_events([window_item(0, "我2018年前后搬去了成都", identifier="m1")])(
            parse_reply(model_event(0, title="搬家", track="居住迁移", phrase="", when="2018", precision="about")))
        self.assertEqual(about["events"][0]["precision"], "about")
        # 非相对表达由模型推断：精度降一级并标 inferred（doc 2.2）。
        inferred = parse_events([window_item(0, "我大三那年去了成都", identifier="m1")])(
            parse_reply(model_event(0, title="上学", track="学业", phrase="", when="2020-06-15", precision="day")))
        event = inferred["events"][0]
        self.assertEqual((event["precision"], event["inferred"]), ("month", True))
        # 程序解析出来的相对时间不算推断。
        relative = parse_events([window_item(0, "我上个月去了日本", identifier="m1")])(
            parse_reply(model_event(0, phrase="上个月")))
        self.assertFalse(relative["events"][0]["inferred"])

    def test_range_straddling_said_downgrades_to_uncertain(self):
        straddle = parse_events([window_item(0, "我那段时间住院了", said=BASE, identifier="m1")])(
            parse_reply(model_event(0, title="住院", track="健康", when="2024-01-01—2024-06-01",
                                    precision="day", importance="important")))
        self.assertEqual(straddle["events"][0]["status"], "uncertain",
                         "an explicit range running past the speaking time contradicts occurred")

    def test_occurred_after_said_downgrades_to_uncertain_and_birth_year_drops(self):
        future = parse_events([window_item(0, "我毕业了", said=BASE, identifier="m1")])(
            parse_reply(model_event(0, title="毕业", track="学业", when="2024-06", precision="month",
                                    importance="important")))
        self.assertEqual(future["events"][0]["status"], "uncertain")
        dropped = parse_events([window_item(0, "我出生在绍兴", said=BASE, identifier="m1")], birth_year=1995)(
            parse_reply(model_event(0, title="出生", track="其他", when="1990", precision="year")))
        self.assertEqual(dropped["events"], [])

    def test_invalid_rows_are_dropped_without_hurting_the_rest(self):
        items = [window_item(0, "我上个月去了日本玩", identifier="m1"), window_item(1, "我上周跑了半马", identifier="m2")]
        payload = parse_events(items)(parse_reply(
            model_event(0, phrase="上个月"),
            model_event(1, title="跑半马", track="健康", phrase="上周", kind="wrong"),
            {"i": 9, "title": "越界", "track": "其他", "kind": "point", "status": "occurred",
             "importance": "general", "phrase": "", "when": "", "precision": "unknown"}))
        self.assertEqual([event["title"] for event in payload["events"]], ["去日本旅行"])

    def test_review_only_deletes_or_demotes_unlisted_kept(self):
        proposed = [{"n": 0, **model_event(0, importance="important")},
                    {"n": 1, **model_event(1, title="入职", track="工作事业", importance="important", phrase="上个月")}]
        parsed = parse_review([window_item(0, "我上个月入职了")], proposed)(parse_reply({"n": 0, "importance": "general"}))
        self.assertEqual(parsed["events"], [{"n": 0, "importance": "general"}], "listed with importance = demote")
        parsed = parse_review([window_item(0, "我上个月入职了")], proposed)(parse_reply({"n": 1}))
        self.assertEqual(parsed["events"], [{"n": 1}], "listed bare = delete; unlisted candidates are kept by default")
        with self.assertRaises(SelfError):
            parse_review([window_item(0, "我上个月入职了")], proposed)(parse_reply({"n": 0, "importance": "important", "title": "改名"}))
        with self.assertRaises(SelfError):
            parse_review([window_item(0, "我上个月入职了")], proposed)(parse_reply({"n": 5}))


def candidate(index, title, start, precision="day", status="occurred", track="学业", identifier=None, kind="point"):
    return {"title": title, "track": track, "kind": kind, "status": status, "importance": "important",
            "startMs": start, "endMs": None, "precision": precision, "phrase": "", "place": "", "people": "",
            "reason": "合成依据", "saidMs": start + DAY, "messageId": identifier or f"m{index}",
            "chat": f"聊天{index}", "isGroup": False, "text": f"原话{index}", "time": start + DAY,
            "truncated": False, "evidence": [{"messageId": identifier or f"m{index}", "chat": f"聊天{index}",
                                              "isGroup": False, "time": start + DAY, "text": f"原话{index}",
                                              "phrase": "", "truncated": False}]}


def merged(*candidates):
    """One merged event with its id assigned, exactly the shape TimelineOrganize feeds build_periods."""
    event = _merge_candidates(list(candidates))
    event["id"] = event_id([row["messageId"] for row in event["evidence"]])
    return event


class OrganizeTests(unittest.TestCase):
    def test_merge_keeps_all_evidence_and_picks_the_most_precise_time(self):
        merged = _merge_candidates([candidate(0, "毕业", 100), candidate(1, "硕士毕业", 200, precision="year")])
        self.assertEqual(merged["title"], "毕业")
        self.assertEqual(merged["precision"], "day", "the most precise statement wins, no averaging")
        self.assertEqual([row["messageId"] for row in merged["evidence"]], ["m1", "m0"])
        self.assertEqual(merged["conflicts"], [{"when": display_when(200, None, "year"), "chat": "聊天1"}])

    def test_merge_prefers_occurred_over_planned(self):
        merged = _merge_candidates([candidate(0, "搬家", 100, status="planned"), candidate(1, "搬完了", 200, status="occurred")])
        self.assertEqual(merged["status"], "occurred", "a later result settles a plan (doc 3.5)")

    def test_periods_pair_start_and_end_and_states_stay_open(self):
        # 真实输入是归并事件（没有顶层 messageId）：时期锚定必须走事件自己的 id。
        periods = build_periods([merged(candidate(0, "入职A", 100, track="工作", kind="start")),
                                 merged(candidate(1, "离职A", 200, track="工作", kind="end")),
                                 merged(candidate(2, "在杭州上班", 300, track="工作", kind="state"))])
        self.assertEqual([(period["title"], period["startMs"], period["endMs"], period["ongoing"]) for period in periods],
                         [("入职A", 100, 200, False), ("在杭州上班", 300, None, True)])

    def test_a_later_period_closes_an_ongoing_state_on_the_same_track(self):
        periods = build_periods([merged(candidate(0, "在杭州上班", 100, track="工作", kind="state")),
                                 merged(candidate(1, "入职B", 200, track="工作", kind="start"))])
        self.assertEqual([(period["title"], period["startMs"], period["endMs"], period["ongoing"]) for period in periods],
                         [("在杭州上班", 100, 200, False), ("入职B", 200, None, True)],
                         "an open-ended state must not overlap the next period on the same track")

    def test_repeated_mentions_of_one_state_form_one_period_until_a_start_intervenes(self):
        periods = build_periods([
            merged({**candidate(0, "人在南昌", 100 * DAY, track="居住", kind="state"), "place": "南昌"}),
            merged({**candidate(1, "仍在南昌", 130 * DAY, track="居住", kind="state"), "place": "南昌"}),
            merged(candidate(2, "搬去北京", 200 * DAY, track="居住", kind="start")),
            merged({**candidate(3, "本人在南昌", 260 * DAY, track="居住", kind="state"), "place": "南昌"})])
        self.assertEqual([(period["title"], period["startMs"], period["endMs"], len(period["evidence"]))
                          for period in periods],
                         [("人在南昌", 100 * DAY, 200 * DAY, 2), ("搬去北京", 200 * DAY, 260 * DAY, 1),
                          ("本人在南昌", 260 * DAY, None, 1)])

    def test_same_title_candidates_join_without_the_model_and_chunks_stay_small(self):
        from self_timeline import MERGE_CHUNK, _merge_chunks, _same_title_units
        units = _same_title_units([candidate(0, "从腾讯离职", BASE, kind="end"),
                                   candidate(1, "从腾讯离职", BASE + 60 * DAY, kind="end"),
                                   candidate(2, "从腾讯离职", BASE + 900 * DAY, kind="end"),
                                   {**candidate(3, "从腾讯离职", BASE, kind="end"), "startMs": None},
                                   candidate(4, "入职腾讯", BASE + 10 * DAY, kind="start")])
        self.assertEqual(sorted(sorted(event["messageId"] for event in unit) for unit in units),
                         [["m0", "m1"], ["m2"], ["m3"], ["m4"]],
                         "close same-title mentions join; a far one or an ambiguous undated one stays apart")
        many = [[candidate(index, f"事件{index}", (index + 1) * DAY)] for index in range(MERGE_CHUNK * 2 + 5)]
        first, second = _merge_chunks(many), _merge_chunks(many, MERGE_CHUNK // 2)
        self.assertTrue(all(len(chunk) <= MERGE_CHUNK for chunk in first + second))
        self.assertEqual(sum(len(chunk) for chunk in first), len(many))
        self.assertNotEqual(len(first[0]), len(second[0]), "the second round shifts the chunk borders")

    def test_edits_overlay_confirms_edits_hides_and_adds(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp), "synthetic-account")
            try:
                timeline = TimelineStore(store)
                events = [candidate(0, "毕业", 100), candidate(1, "旅行", 300)]
                timeline.add_edit("event", "confirm", "e0", anchors=("m0",))
                timeline.add_edit("event", "edit", "e1", anchors=("m1",), fields={"title": "去大理", "startMs": 400})
                timeline.add_deleted("e9", ("m9",), track="学业")
                view = _apply_edits(timeline, events + [candidate(9, "旧事", 500)])
                by_title = {event["title"]: event for event, _ in view}
                self.assertEqual(by_title["毕业"]["level"], "confirmed")
                self.assertEqual((by_title["去大理"]["title"], by_title["去大理"]["startMs"]), ("去大理", 400))
                self.assertNotIn("旧事", by_title, "a deleted event's evidence set must not resurface")
                timeline.add_edit("event", "add", "manual-1", fields={"title": "手动", "track": "其他",
                                                                       "status": "occurred", "importance": "general",
                                                                       "startMs": 600, "endMs": None,
                                                                       "precision": "day", "phrase": "", "place": "",
                                                                       "people": ""})
                view = _apply_edits(timeline, events)
                self.assertEqual([event["title"] for event, _ in view if event.get("manual")], ["手动"])
            finally:
                store.close()

    def test_two_events_from_one_message_do_not_collide_on_the_primary_key(self):
        # 一条消息说两件事：依据集合相同 → 折叠为一行，而不是 UNIQUE constraint 整轮失败。
        with tempfile.TemporaryDirectory() as temp:
            plugin = SimpleNamespace(models=_FakeModels({}), log=lambda *args: None)
            store = Store(Path(temp), "synthetic-account")
            try:
                timeline = TimelineStore(store)
                item = {"id": "m0", "chat": "合成朋友", "group": False, "time": BASE, "part": 1, "parts": 1,
                        "text": "我毕业了还搬了家", "context": {"before": [], "after": [], "truncated": False}}
                timeline.stage([(0, "fp-a", item)])
                timeline.activate()
                timeline.save(timeline.pending(10), [{"events": [dict(candidate(0, "毕业", BASE, track="学业", identifier="m0")),
                                                                 dict(candidate(1, "搬家", BASE, track="居住迁移", identifier="m0"))]}])
                TimelineOrganize(plugin, store, "synthetic-account", None, {}).work(_JobContext())
                events = timeline.events()
                self.assertEqual(len(events), 1, "same evidence set is one event (doc 4.2)")
                self.assertEqual(events[0]["title"], "毕业")
            finally:
                store.close()

    def test_confirmed_events_are_not_merged_and_keep_user_fields_after_reruns(self):
        merge_them = {TIMELINE_MERGE: json.dumps({"merge": [{"into": 0, "from": [1]}]}, ensure_ascii=False)}
        with tempfile.TemporaryDirectory() as temp:
            models = _FakeModels(merge_them)
            plugin = SimpleNamespace(models=models, log=lambda *args: None)
            store = Store(Path(temp), "synthetic-account")
            try:
                timeline = TimelineStore(store)
                item = {"id": "m0", "chat": "合成朋友", "group": False, "time": BASE, "part": 1, "parts": 1,
                        "text": "我毕业了", "context": {"before": [], "after": [], "truncated": False}}
                timeline.stage([(0, "fp-a", item), (1, "fp-b", {**item, "id": "m1"})])
                timeline.activate()
                timeline.save(timeline.pending(10), [{"events": [dict(candidate(0, "毕业", BASE, identifier="m0")),
                                                                  dict(candidate(1, "硕士毕业", BASE, identifier="m1"))]}])
                timeline.add_edit("event", "confirm", "e0", anchors=("m0",))
                job = TimelineOrganize(plugin, store, "synthetic-account", None, {})
                job.work(_JobContext())
                self.assertNotIn(TIMELINE_MERGE, models.systems, "a protected candidate never reaches the merge model")
                self.assertEqual(len(timeline.events()), 2, "a user-confirmed event must not be merged away")
                # 换一批标题不同、依据相同的候选重跑，用户改过的字段仍在。
                timeline.add_edit("event", "edit", "user-title", anchors=("m0",), fields={"title": "本科毕业"},
                                  track="学业")
                timeline.stage([(2, "fp-c", item)])
                timeline.activate()
                timeline.save(timeline.pending(10), [{"events": [dict(candidate(0, "拿到了学位", BASE, identifier="m0"))]}])
                job.work(_JobContext())
                view = _apply_edits(timeline, timeline.events())
                titled = {event["title"]: event for event, _ in view}
                self.assertIn("本科毕业", titled, "the user's words survive re-analysis (doc 4.2)")
                self.assertEqual(titled["本科毕业"]["level"], "confirmed")
            finally:
                store.close()

    def test_assign_uses_a_dense_index_and_never_touches_protected_events(self):
        replies = {TIMELINE_MERGE: json.dumps({"merge": []}, ensure_ascii=False),
                   TIMELINE_ASSIGN: json.dumps({"assign": [{"n": 0, "track": "创业"}]}, ensure_ascii=False)}
        with tempfile.TemporaryDirectory() as temp:
            plugin = SimpleNamespace(models=_FakeModels(replies), log=lambda *args: None)
            store = Store(Path(temp), "synthetic-account")
            try:
                timeline = TimelineStore(store)
                timeline.set_track("创业", color="#07c160", description="和小店经营有关的事")
                item = {"id": "m0", "chat": "合成朋友", "group": False, "time": BASE, "part": 1, "parts": 1,
                        "text": "我毕业了", "context": {"before": [], "after": [], "truncated": False}}
                timeline.stage([(0, "fp-a", item), (1, "fp-b", {**item, "id": "m1"})])
                timeline.activate()
                timeline.save(timeline.pending(10), [{"events": [dict(candidate(0, "毕业", BASE, track="学业", identifier="m0")),
                                                                  dict(candidate(1, "开店", BASE + 10 * DAY, track="工作事业", identifier="m1"))]}])
                # 用户手动把 m0 的事件改到自定义主线：它不再参与改归。
                timeline.add_edit("event", "edit", "user", anchors=("m0",), fields={"track": "创业"}, track="学业")
                TimelineOrganize(plugin, store, "synthetic-account", None, {}).work(_JobContext())
                view = _apply_edits(timeline, timeline.events())
                by_message = {event["evidence"][0]["messageId"]: event for event, _ in view}
                self.assertEqual(by_message["m0"]["track"], "创业", "the user's own assignment wins")
                self.assertEqual(by_message["m1"]["track"], "创业",
                                 "the model's dense n=0 maps to the only assignable event, not to merged[0]")
            finally:
                store.close()

    def test_organize_builds_periods_from_start_end_state_and_manual_events(self):
        # 回归：归并事件与手工事件都没有顶层 messageId，时期构建不得索引它（曾以 KeyError 整轮失败）。
        with tempfile.TemporaryDirectory() as temp:
            plugin = SimpleNamespace(models=_FakeModels({}), log=lambda *args: None)
            store = Store(Path(temp), "synthetic-account")
            try:
                timeline = TimelineStore(store)
                item = {"id": "m0", "chat": "合成朋友", "group": False, "time": BASE, "part": 1, "parts": 1,
                        "text": "我毕业了", "context": {"before": [], "after": [], "truncated": False}}
                timeline.stage([(0, "fp-a", item), (1, "fp-b", {**item, "id": "m1"}),
                                (2, "fp-c", {**item, "id": "m2"}), (3, "fp-d", {**item, "id": "m3"})])
                timeline.activate()
                timeline.save(timeline.pending(10), [{"events": [dict(candidate(0, "入职", BASE, track="工作事业",
                                                                                  identifier="m0", kind="start"))]},
                                                     {"events": [dict(candidate(1, "离职", BASE + DAY, track="工作事业",
                                                                                identifier="m1", kind="end"))]},
                                                     {"events": [dict(candidate(2, "在杭州上班", BASE + 2 * DAY,
                                                                                track="工作事业", identifier="m2",
                                                                                kind="state"))]},
                                                     {"events": [dict(candidate(3, "毕业", BASE + 3 * DAY,
                                                                                track="学业", identifier="m3",
                                                                                kind="state"))]}])
                timeline.add_edit("event", "add", "manual-1",
                                  fields={"title": "手动读研", "track": "学业", "kind": "state", "status": "occurred",
                                          "importance": "general", "startMs": BASE + 4 * DAY, "endMs": None,
                                          "precision": "day", "phrase": "", "place": "", "people": ""})
                TimelineOrganize(plugin, store, "synthetic-account", None, {}).work(_JobContext())
                self.assertEqual(len(timeline.events()), 4, "start/end/state candidates all become events")
                periods = {period["title"]: period for period in timeline.periods()}
                self.assertEqual(set(periods), {"入职", "在杭州上班", "毕业", "手动读研"})
                self.assertEqual((periods["入职"]["endMs"], periods["入职"]["ongoing"]),
                                 (BASE + DAY, False), "a paired end closes its start period")
                self.assertEqual((periods["手动读研"]["startMs"], periods["手动读研"]["ongoing"]),
                                 (BASE + 4 * DAY, True), "a manual state event opens its own period")
            finally:
                store.close()

    def test_organize_merges_a_large_track_in_two_rounds_of_small_requests(self):
        from self_timeline import MERGE_CHUNK
        count = MERGE_CHUNK * 2 + 5
        with tempfile.TemporaryDirectory() as temp:
            models = _FakeModels({TIMELINE_MERGE: json.dumps({"merge": [{"into": 0, "from": [1]}]}, ensure_ascii=False)})
            plugin = SimpleNamespace(models=models, log=lambda *args: None)
            store = Store(Path(temp), "synthetic-account")
            try:
                timeline = TimelineStore(store)
                item = {"id": "m0", "chat": "合成朋友", "group": False, "time": BASE, "part": 1, "parts": 1,
                        "text": "我毕业了", "context": {"before": [], "after": [], "truncated": False}}
                timeline.stage([(index, f"fp-{index}", {**item, "id": f"m{index}"}) for index in range(count)])
                timeline.activate()
                timeline.save(timeline.pending(count), [{"events": [candidate(index, f"事件{index}", BASE + index * DAY,
                                                                              identifier=f"m{index}")]}
                                                        for index in range(count)])
                TimelineOrganize(plugin, store, "synthetic-account", None, {}).work(_JobContext())
                sizes = [len(json.loads(prompt)["groups"][0]) for system, prompt in models.calls if system == TIMELINE_MERGE]
                self.assertTrue(sizes and max(sizes) <= MERGE_CHUNK, "no request carries more than one chunk")
                self.assertGreater(len(sizes), len(range(0, count, MERGE_CHUNK)), "a second, shifted round runs")
                self.assertLess(len(timeline.events()), count)
                calls = len(models.calls)
                TimelineOrganize(plugin, store, "synthetic-account", None, {}).work(_JobContext())
                self.assertEqual(len(models.calls), calls, "both rounds are cached by content")
            finally:
                store.close()

    def test_organize_runs_merge_assign_and_periods_with_cache(self):
        class Models:
            def __init__(self, merge, assign):
                self.merge, self.assign = merge, assign
                self.calls = []

            def generate(self, context, request, step=None):
                self.calls.append(step)
                if step == "self-timeline-merge":
                    return {"text": json.dumps({"merge": [{"into": 0, "from": [1]}]}, ensure_ascii=False)}
                return {"text": json.dumps({"assign": []}, ensure_ascii=False)}

        class Context:
            def __init__(self):
                self.reports = []

            def check(self):
                return None

            def progress(self, **values):
                self.reports.append(values)

        with tempfile.TemporaryDirectory() as temp:
            plugin = SimpleNamespace(models=Models([[[0, 1]], []], []), log=lambda *args: None)
            store = Store(Path(temp), "synthetic-account")
            try:
                timeline = TimelineStore(store)
                item = {"id": "m0", "chat": "合成朋友", "group": False, "time": BASE, "part": 1, "parts": 1,
                        "text": "我毕业了", "context": {"before": [], "after": [], "truncated": False}}
                timeline.stage([(0, "fp-a", item), (1, "fp-b", {**item, "id": "m1"})])
                timeline.activate()
                rows = timeline.pending(10)
                timeline.save(rows, [{"events": [dict(candidate(0, "毕业", BASE, identifier="m0")),
                                                  dict(candidate(1, "硕士毕业", BASE, identifier="m1"))]},
                                    {"events": [candidate(2, "入职", BASE + 30 * DAY, track="工作事业", identifier="m1")]}])
                job = TimelineOrganize(plugin, store, "synthetic-account", None, {})
                job.work(Context())
                self.assertEqual(sorted(event["title"] for event in timeline.events()), ["入职", "毕业"])
                self.assertEqual(sorted(event["track"] for event in timeline.events()), ["学业", "工作事业"])
                calls = plugin.models.calls
                plugin.models.calls = []
                job.work(Context())
                self.assertEqual(plugin.models.calls, [], "organize results are cached by content fingerprint")
            finally:
                store.close()


class ExtractionRunTests(unittest.TestCase):
    """Full TimelineExtract runs on synthetic corpus rows with a scripted model."""

    def run_extract(self, store, depth, models, rows=None):
        corpus = _CorpusStub(rows or [_corpus_row()])
        job = TimelineExtract(SimpleNamespace(models=models, log=lambda *args: None), store, "synthetic-account",
                              corpus, {}, selected_depth=depth)
        job.work(_JobContext())
        return job

    @staticmethod
    def shared_counts(store):
        names = {row["name"] for row in store.rows("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: len(store.rows(f"SELECT * FROM {name}")) for name in
                ("self_evidence_cache", "self_evidence_meta", "self_evidence_active") if name in names}

    def test_failed_single_window_is_partial_then_retried_and_valid_empty_result_is_cached(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp), "synthetic-account")
            try:
                models = _FakeModels({TIMELINE_WINDOW: "not JSON"})
                with self.assertRaises(SelfError) as caught:
                    self.run_extract(store, "basic", models)
                self.assertEqual(caught.exception.code, "timeline-incomplete")
                timeline = TimelineStore(store)
                self.assertEqual(timeline.active_counts(), {"total": 1, "done": 0})
                self.assertEqual(len(models.calls), 3, "one pass must stop after bounded model retries")
                models.replies[TIMELINE_WINDOW] = parse_reply()
                self.run_extract(store, "basic", models)
                self.assertEqual(timeline.active_counts(), {"total": 1, "done": 1})
                self.assertEqual(len(models.calls), 4)
                self.run_extract(store, "basic", models)
                self.assertEqual(len(models.calls), 4, "a verified no-event answer remains cached")
                # Older releases left failures as done without a cache row.
                with store.lock, store.db:
                    store.db.execute("DELETE FROM timeline_cache")
                self.run_extract(store, "basic", models)
                self.assertEqual(len(models.calls), 5)
            finally:
                store.close()

    def test_interrupted_activation_cannot_make_an_empty_snapshot_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp), "synthetic-account")
            try:
                models = _FakeModels({TIMELINE_WINDOW: parse_reply()})
                with patch.object(TimelineStore, "activate", side_effect=RuntimeError("synthetic interruption")):
                    with self.assertRaises(RuntimeError):
                        self.run_extract(store, "basic", models)
                timeline = TimelineStore(store)
                self.assertFalse(timeline.meta("prepared", False))
                self.assertEqual(timeline.active_counts()["total"], 0)
                # Recover the prior release's exact interruption state as well.
                with store.lock, store.db:
                    timeline._meta("prepared", True)
                self.run_extract(store, "basic", models)
                self.assertEqual(timeline.active_counts(), {"total": 1, "done": 1})
                self.assertEqual(len(models.calls), 1)
            finally:
                store.close()

    def test_review_happens_when_depth_changes_and_stays_cached_otherwise(self):
        replies = {TIMELINE_WINDOW: parse_reply(model_event(0, phrase="上个月", importance="important")),
                   TIMELINE_REVIEW: parse_reply()}
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp), "synthetic-account")
            try:
                models = _FakeModels(replies)
                self.run_extract(store, "basic", models)
                self.assertEqual(models.systems, [TIMELINE_WINDOW], "basic runs no review")
                self.run_extract(store, "standard", models)
                self.assertEqual(models.systems, [TIMELINE_WINDOW, TIMELINE_WINDOW, TIMELINE_REVIEW],
                                 "changing depth must invalidate the extraction cache so the review really happens")
                self.run_extract(store, "standard", models)
                self.assertEqual(len(models.systems), 3, "a same-depth rerun is fully cached, review included")
            finally:
                store.close()

    def test_review_scope_widens_with_depth(self):
        window_reply = parse_reply(model_event(0, title="毕业", track="学业", importance="important", phrase=""),
                                   model_event(1, title="旅行", track="旅行出行", importance="general", phrase=""))
        replies = {TIMELINE_WINDOW: window_reply, TIMELINE_REVIEW: parse_reply()}
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp), "synthetic-account")
            try:
                models = _FakeModels(replies)
                rows = [_corpus_row(rowid=1, text="我毕业了", identifier="m1"),
                        _corpus_row(rowid=2, text="我上个月去了日本玩", identifier="m2")]
                self.run_extract(store, "standard", models, rows=rows)
                self.run_extract(store, "deep", models, rows=rows)
                reviews = [json.loads(prompt) for system, prompt in models.calls if system == TIMELINE_REVIEW]
                self.assertEqual(len(reviews), 2)
                self.assertEqual([row["title"] for row in reviews[0]["proposed"]["events"]], ["毕业"],
                                 "standard reviews only the important candidates")
                self.assertEqual(len(reviews[1]["proposed"]["events"]), 2, "deep reviews every candidate")
            finally:
                store.close()

    def test_route_revision_change_keeps_the_extraction_cache(self):
        """网关换路顶高 route_revision 与联动 service fingerprint：不属内容身份，不得作废已提取窗口。"""
        def service(actual_model="GLM-5.3-Flash-EXL3", route_revision=25, fingerprint="route-fp-25"):
            return {"id": "synthetic-source", "model": "local-default", "contextTokens": 1000000,
                    "actualModel": actual_model, "backendId": "glm",
                    "routeRevision": route_revision, "fingerprint": fingerprint}
        replies = {TIMELINE_WINDOW: parse_reply(model_event(0, phrase="上个月", importance="important")),
                   TIMELINE_REVIEW: parse_reply()}
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp), "synthetic-account")
            try:
                models = _FakeModels(replies, service=service())
                self.run_extract(store, "deep", models)
                first_run_calls = len(models.systems)
                self.assertGreater(first_run_calls, 0)
                # 上游网关换路：route_revision 与 service fingerprint 变化，actualModel 不变。
                models.service_value = service(route_revision=39, fingerprint="route-fp-39")
                self.run_extract(store, "deep", models)
                self.assertEqual(len(models.systems), first_run_calls,
                                 "路由细节变化必须命中缓存，绝不能重提取")
                # settings 里也不应再出现路由细节键。
                settings = TimelineExtract._settings(SimpleNamespace(depth="deep"), models.service())
                self.assertNotIn("routeRevision", settings)
                self.assertNotIn("fingerprint", settings)
                # 内容身份（actualModel）变了仍须作废旧缓存。
                models.service_value = service(actual_model="other-model")
                self.run_extract(store, "deep", models)
                self.assertGreater(len(models.systems), first_run_calls,
                                   "actualModel 变化必须重新提取")
            finally:
                store.close()

    def test_timeline_never_touches_the_shared_evidence_tables(self):
        replies = {TIMELINE_WINDOW: parse_reply(model_event(0, phrase="上个月")), TIMELINE_REVIEW: parse_reply()}
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp), "synthetic-account")
            try:
                models = _FakeModels(replies)
                before = self.shared_counts(store)
                self.run_extract(store, "standard", models)
                TimelineOrganize(SimpleNamespace(models=_FakeModels({}), log=lambda *args: None), store,
                                 "synthetic-account", None, {}).work(_JobContext())
                self.assertEqual(self.shared_counts(store), before,
                                 "plan B: the timeline pipeline must not write the shared evidence tables")
            finally:
                store.close()


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plugin = SimpleNamespace(data_dir=Path(self.temp.name), log=lambda *args: None,
                                      core=SimpleNamespace(scope=lambda: {"account": "synthetic-account"}, get=lambda path: {"revision": 0, "values": {
                                          "analysisDepth": "deep", "replyDepth": "standard", "analysisGrain": "6"}}))
        self.service = Service(self.plugin)
        self.account = "synthetic-account"

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def seed(self):
        store, _ = self.service._account(self.account)
        timeline = TimelineStore(store)
        timeline.replace_events([{"id": "e1", "title": "毕业", "track": "学业", "kind": "point", "status": "occurred",
                                  "importance": "important", "startMs": 100, "precision": "day",
                                  "evidence": [{"messageId": "m1", "chat": "A", "isGroup": False, "time": 5,
                                                "text": "我毕业了", "phrase": "", "truncated": False}]}])
        timeline.replace_periods([{"id": "p1", "title": "读研", "track": "学业", "startMs": 100, "endMs": 200,
                                   "precision": "day", "ongoing": False,
                                   "evidence": [{"messageId": "m1", "time": 5}]}])
        return timeline

    def test_view_returns_version_events_periods_tracks_and_coverage(self):
        self.seed()
        view = self.service.timeline(self.account)
        self.assertEqual(view["version"], 0)
        self.assertEqual([event["title"] for event in view["events"]], ["毕业"])
        self.assertEqual(view["events"][0]["whenText"], display_when(100, None, "day"))
        self.assertEqual([period["title"] for period in view["periods"]], ["读研"])
        self.assertEqual([track["name"] for track in view["tracks"]][:2], ["学业", "工作事业"])
        self.assertIn("run", view)

    def test_view_coverage_ignores_messages_without_a_real_time(self):
        from self_profile_corpus import ProfileCorpus
        store, _ = self.service._account(self.account)
        ProfileCorpus(store)
        store.db.executemany("INSERT INTO profile_corpus_messages(id,user,seq,time_ms,text,context) VALUES(?,?,?,?,?,?)",
                             [("z", "A", 1, 0, "坏时间", "[]"), ("r", "A", 2, BASE, "正常", "[]")])
        coverage = self.service.timeline(self.account)["coverage"]
        self.assertEqual(coverage["earliestMs"], BASE)

    def test_view_pages_events_within_the_reply_budget(self):
        import self_timeline
        store, _ = self.service._account(self.account)
        TimelineStore(store).replace_events([
            {"id": f"e{index:03d}", "title": f"事件{index}", "track": "学业", "startMs": BASE + index * DAY,
             "precision": "day", "reason": "原因" * 30, "evidence": [{"messageId": f"m{index}"}]}
            for index in range(120)])
        budget = self_timeline.EVENT_PAGE_BYTES
        self_timeline.EVENT_PAGE_BYTES = 8_000
        try:
            pages, offset = [], 0
            while offset is not None:
                view = self.service.timeline(self.account, {"offset": offset})
                self.assertEqual(view["eventsTotal"], 120)
                self.assertLessEqual(len(json.dumps(view["events"], ensure_ascii=False).encode()), 8_000)
                pages.append(view["events"])
                offset = view["nextOffset"]
        finally:
            self_timeline.EVENT_PAGE_BYTES = budget
        self.assertGreater(len(pages), 1)
        ids = [event["id"] for page in pages for event in page]
        self.assertEqual(ids, [f"e{index:03d}" for index in reversed(range(120))])
        whole = self.service.timeline(self.account)
        self.assertEqual(([event["id"] for event in whole["events"]], whole["nextOffset"]), (ids, None))
        with self.assertRaises(SelfError):
            self.service.timeline(self.account, {"offset": -1})

    def test_edit_conflict_keeps_input_and_returns_conflict(self):
        self.seed()
        view = self.service.timeline(self.account)
        with self.assertRaises(SelfError) as raised:
            self.service.timeline_edit(self.account, {"target": "event", "action": "confirm", "id": "e1",
                                                      "expectedVersion": view["version"] + 5})
        self.assertEqual(raised.exception.code, "conflict")
        self.assertEqual(self.service.timeline(self.account)["version"], view["version"])
        result = self.service.timeline_edit(self.account, {"target": "event", "action": "confirm", "id": "e1",
                                                           "expectedVersion": view["version"]})
        self.assertEqual(result["events"][0]["level"], "confirmed")
        self.assertEqual(result["version"], view["version"] + 1)

    def test_edit_fields_are_validated_and_anchored(self):
        timeline = self.seed()
        with self.assertRaises(SelfError):
            self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                      "fields": {"nonsense": 1}})
        result = self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                           "fields": {"title": "硕士毕业", "startMs": 150}})
        event = result["events"][0]
        self.assertEqual((event["title"], event["startMs"], event["level"]), ("硕士毕业", 150, "confirmed"))
        self.assertEqual(event["edited"], ["startMs", "title"])
        self.assertEqual(timeline.version(), result["version"])

    def test_partial_edits_keep_earlier_changes(self):
        self.seed()
        self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                  "fields": {"title": "硕士毕业", "place": "杭州"}})
        result = self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                           "fields": {"importance": "general"}})
        event = result["events"][0]
        self.assertEqual((event["title"], event["place"], event["importance"]), ("硕士毕业", "杭州", "general"))

    def test_partial_edit_lands_on_the_row_that_is_winning(self):
        timeline = self.seed()
        self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                  "fields": {"title": "硕士毕业"}})
        # 重新分析后同一依据换了事件 id，再改一次也要生效，而不是被旧行盖住。
        timeline.replace_events([{"id": "e9", "title": "毕业", "track": "学业", "startMs": 100, "precision": "day",
                                  "evidence": [{"messageId": "m1", "time": 100}]}])
        result = self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e9",
                                                           "fields": {"importance": "general"}})
        event = result["events"][0]
        self.assertEqual((event["id"], event["title"], event["importance"]), ("e9", "硕士毕业", "general"))
        self.assertEqual(len(timeline.edits("event")), 1)

    def test_partial_edit_of_a_manual_event_keeps_its_other_fields(self):
        self.seed()
        added = self.service.timeline_edit(self.account, {"target": "event", "action": "add",
                                                          "fields": {"title": "入职", "track": "工作事业", "when": "2024.05"}})
        result = self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": added["editedId"],
                                                           "fields": {"importance": "important"}})
        event = next(item for item in result["events"] if item["id"] == added["editedId"])
        self.assertEqual((event["title"], event["track"], event["precision"], event["importance"], event["manual"]),
                         ("入职", "工作事业", "month", "important", True))
        with self.assertRaises(SelfError) as raised:
            self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "manual-missing",
                                                      "fields": {"title": "不存在"}})
        self.assertEqual(raised.exception.code, "not-found")

    def test_new_items_need_a_title_and_a_known_track(self):
        self.seed()
        for target, fields in (("event", {"track": "其他"}), ("period", {"track": "其他"}),
                               ("event", {"title": "x", "track": "不存在"}), ("period", {"title": "x", "track": "不存在"})):
            with self.assertRaises(SelfError):
                self.service.timeline_edit(self.account, {"target": target, "action": "add", "fields": fields})
        event = self.service.timeline_edit(self.account, {"target": "event", "action": "add", "fields": {"title": "只有标题"}})
        added = next(item for item in event["events"] if item["manual"])
        self.assertEqual((added["track"], added["status"], added["importance"], added["whenText"]),
                         ("其他", "occurred", "general", "时间不明"))

    def test_manual_period_survives_a_partial_edit(self):
        self.seed()
        added = self.service.timeline_edit(self.account, {"target": "period", "action": "add",
                                                          "fields": {"title": "读研", "track": "学业", "when": "2020.09-2023.06"}})
        result = self.service.timeline_edit(self.account, {"target": "period", "action": "edit", "id": added["editedId"],
                                                           "fields": {"title": "硕士"}})
        period = next(item for item in result["periods"] if item["id"] == added["editedId"])
        self.assertEqual((period["title"], period["track"], period["precision"], period["manual"]),
                         ("硕士", "学业", "month", True))

    def test_when_is_parsed_as_people_type_it(self):
        self.seed()
        for text, expected in (("2024.05", ("month", "2024年5月")), ("2024-05", ("month", "2024年5月")),
                               ("2024/5", ("month", "2024年5月")), ("2024年5月", ("month", "2024年5月")),
                               ("202405", ("month", "2024年5月")), ("2024.5.3", ("day", "2024年5月3日")),
                               ("2024年5月3日", ("day", "2024年5月3日")), ("20240503", ("day", "2024年5月3日")),
                               ("2024", ("year", "2024年")), ("2024春", ("season", "2024年3月")),
                               ("2024.03-2024.06", ("month", "2024年3月–2024年6月"))):
            result = self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                               "fields": {"when": text}})
            event = result["events"][0]
            self.assertEqual((event["precision"], event["whenText"]), expected, text)
        cleared = self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                            "fields": {"when": ""}})["events"][0]
        self.assertEqual((cleared["startMs"], cleared["endMs"], cleared["precision"], cleared["whenText"]),
                         (None, None, "unknown", "时间不明"))
        for bad in ("2024.13", "2024.02.30", "去年", "24.5", "24.5.3", "1965", "0024.05", "20245", "2024.03-1965", "2024.03-9999"):
            with self.assertRaises(SelfError):
                self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                          "fields": {"when": bad}})
        with self.assertRaises(SelfError):
            self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                      "fields": {"when": "2024", "startMs": 1}})

    def test_edit_that_flips_the_range_is_refused(self):
        timeline = self.seed()
        timeline.replace_events([{"id": "e1", "title": "毕业", "track": "学业", "startMs": 100, "endMs": 200, "precision": "day",
                                  "evidence": [{"messageId": "m1", "time": 100}]}])
        with self.assertRaises(SelfError):
            self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                      "fields": {"startMs": 300}})

    def test_undo_walks_back_edits_in_order(self):
        self.seed()
        version = self.service.timeline(self.account)["version"]
        self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                  "fields": {"title": "硕士毕业"}})
        self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                  "fields": {"importance": "general"}})
        result = self.service.timeline_undo(self.account, {})
        self.assertEqual(result["undone"], "修改“硕士毕业”")
        self.assertEqual((result["events"][0]["title"], result["events"][0]["importance"]), ("硕士毕业", "important"))
        result = self.service.timeline_undo(self.account, {})
        event = result["events"][0]
        # 回到完全没修改过的样子：字段、确认状态都还原，不只是把值改回去。
        self.assertEqual((event["title"], event["level"], event["edited"]), ("毕业", "possible", []))
        self.assertEqual(result["version"], version + 4)
        self.assertIsNone(self.service.timeline_undo(self.account, {})["undone"])

    def test_undo_covers_delete_add_confirm_and_tracks(self):
        self.seed()
        view = self.service.timeline_edit(self.account, {"target": "event", "action": "delete", "id": "e1"})
        self.assertEqual((view["events"], view["editLabel"]), ([], "删除“毕业”"))
        self.assertEqual(self.service.timeline_undo(self.account, {})["events"][0]["title"], "毕业")
        added = self.service.timeline_edit(self.account, {"target": "event", "action": "add", "fields": {"title": "手动"}})
        self.assertEqual(len(added["events"]), 2)
        self.assertEqual(len(self.service.timeline_undo(self.account, {})["events"]), 1)
        self.service.timeline_edit(self.account, {"target": "event", "action": "confirm", "id": "e1"})
        self.assertEqual(self.service.timeline_undo(self.account, {})["events"][0]["level"], "possible")
        self.service.timeline_edit(self.account, {"target": "track", "action": "add",
                                                  "fields": {"title": "创业", "color": "#07c160"}})
        self.service.timeline_edit(self.account, {"target": "track", "action": "visible", "id": "健康",
                                                  "fields": {"visible": False}})
        result = self.service.timeline_undo(self.account, {})
        self.assertTrue(next(track for track in result["tracks"] if track["name"] == "健康")["visible"])
        result = self.service.timeline_undo(self.account, {})
        self.assertNotIn("创业", [track["name"] for track in result["tracks"]])

    def test_undo_restores_an_edit_made_on_a_period(self):
        self.seed()
        added = self.service.timeline_edit(self.account, {"target": "period", "action": "add",
                                                          "fields": {"title": "读研", "track": "学业", "when": "2020"}})
        self.service.timeline_edit(self.account, {"target": "period", "action": "delete", "id": added["editedId"]})
        result = self.service.timeline_undo(self.account, {})
        self.assertIn(added["editedId"], [period["id"] for period in result["periods"]])

    def test_undo_keeps_the_version_guard_and_a_failed_edit_records_nothing(self):
        self.seed()
        version = self.service.timeline(self.account)["version"]
        self.service.timeline_edit(self.account, {"target": "event", "action": "confirm", "id": "e1"})
        with self.assertRaises(SelfError) as raised:
            self.service.timeline_undo(self.account, {"expectedVersion": version})
        self.assertEqual(raised.exception.code, "conflict")
        with self.assertRaises(SelfError):
            self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1", "fields": {"title": ""}})
        self.assertEqual(self.service.timeline_undo(self.account, {})["undone"], "确认“毕业”")
        self.assertIsNone(self.service.timeline_undo(self.account, {})["undone"])

    def test_undo_journal_is_bounded(self):
        import self_timeline_store
        timeline = self.seed()
        limit = self_timeline_store.UNDO_LIMIT
        self_timeline_store.UNDO_LIMIT = 3
        try:
            for index in range(5):
                self.service.timeline_edit(self.account, {"target": "event", "action": "edit", "id": "e1",
                                                          "fields": {"title": f"第{index}次"}})
        finally:
            self_timeline_store.UNDO_LIMIT = limit
        self.assertEqual(timeline.rows("SELECT count(*) total FROM timeline_undo")[0]["total"], 3)

    def test_view_honors_a_tighter_page_budget(self):
        store, _ = self.service._account(self.account)
        TimelineStore(store).replace_events([
            {"id": f"e{index:03d}", "title": f"事件{index}", "track": "学业", "startMs": BASE + index * DAY,
             "precision": "day", "reason": "原因" * 30, "evidence": [{"messageId": f"m{index}"}]}
            for index in range(120)])
        view = self.service.timeline(self.account, {"pageBytes": 8_000})
        self.assertLessEqual(len(json.dumps(view["events"], ensure_ascii=False).encode()), 8_000)
        self.assertIsNotNone(view["nextOffset"])
        with self.assertRaises(SelfError):
            self.service.timeline(self.account, {"pageBytes": 0})

    def test_evidence_returns_rows_and_resolved_time(self):
        self.seed()
        result = self.service.timeline_evidence(self.account, {"id": "e1"})
        self.assertEqual((result["total"], len(result["items"])), (1, 1))
        self.assertEqual(result["items"][0]["messageId"], "m1")
        self.assertEqual(result["resolvedWhen"], display_when(100, None, "day"))
        self.assertEqual(self.service.timeline_evidence(self.account, {"id": "missing"})["total"], 0)

    def test_evidence_carries_the_nearby_conversation(self):
        from self_profile_corpus import ProfileCorpus
        self.seed()
        store, _ = self.service._account(self.account)
        ProfileCorpus(store)
        minute = 60_000
        store.db.execute("INSERT INTO profile_corpus_conversations(user,name,is_group) VALUES('A','阿杰',0)")
        store.db.execute("INSERT INTO profile_corpus_messages(id,user,seq,time_ms,text,context) "
                         "VALUES('m1','A',5,?,'我毕业了','')", (BASE,))
        store.db.executemany("INSERT INTO profile_corpus_context(id,user,seq,time_ms,side,speaker,text,truncated) "
                             "VALUES(?,?,?,?,?,?,?,0)",
                             [("c1", "A", 1, BASE - 600 * minute, "other", "阿杰", "太早了不算"),
                              ("c2", "A", 2, BASE - 3 * minute, "other", "阿杰", "最近忙啥"),
                              ("c3", "A", 3, BASE - 2 * minute, "self", "我", "还行"),
                              ("c4", "A", 4, BASE - minute, "other", "阿杰", "论文呢"),
                              ("c5", "A", 6, BASE + minute, "other", "阿杰", "恭喜！")])
        item = self.service.timeline_evidence(self.account, {"id": "e1"})["items"][0]
        self.assertEqual([row["text"] for row in item["before"]], ["最近忙啥", "还行", "论文呢"])
        self.assertEqual([(row["side"], row["text"]) for row in item["after"]], [("other", "恭喜！")])

    def test_delete_and_restore_via_deleted_anchor_sets(self):
        self.seed()
        result = self.service.timeline_edit(self.account, {"target": "event", "action": "delete", "id": "e1"})
        self.assertEqual(result["events"], [])
        result = self.service.timeline_edit(self.account, {"target": "event", "action": "restore", "id": "e1"})
        self.assertEqual([event["title"] for event in result["events"]], ["毕业"])

    def test_track_visibility_and_custom_tracks_through_the_service(self):
        self.seed()
        result = self.service.timeline_edit(self.account, {"target": "track", "action": "visible", "id": "健康",
                                                           "fields": {"visible": False}})
        self.assertFalse(next(track for track in result["tracks"] if track["name"] == "健康")["visible"])
        result = self.service.timeline_edit(self.account, {"target": "track", "action": "add",
                                                           "fields": {"title": "创业", "color": "#07c160",
                                                                      "description": "和小店经营有关的事"}})
        self.assertIn("创业", [track["name"] for track in result["tracks"] if not track["builtin"]])


if __name__ == "__main__":
    unittest.main()
