"""性格时间线: scoring, the code's overrides of model answers and level reuse, on synthetic data only.

Fake responses test validation and caching, not the model's semantic accuracy.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import collections
import datetime
import json
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend"),
                str(ROOT / "plugins/kyf.self-analysis/tests")]
import self_periods
from self_contracts import SelfError
from self_periods import (FACTORS, INNER, ITEMS, MINI, MONTHS, PLAN, SIGN, Period, PeriodAnswers, PeriodStore, factor_score,
                          find_changes, load_terms, of_length, parse_answer, period_bounds, period_detail, period_ids, period_label,
                          period_of, periods_view, reports_by_period, sample, save_period_edit, settled, type_letters, valid_period)
from self_store import Store
from test_self_analysis import dialogue
from test_unified_analysis import UnifiedModels, UnifiedTests

OUTER = next(item for item in ITEMS if item not in INNER)
FEELING = next(iter(sorted(INNER)))


def ms(year, month, day=10):
    return int(datetime.datetime(year, month, day, 12).timestamp() * 1000)


def line(index, text, when, user="friend-a", name="合成朋友"):
    return {"id": f"m{index}", "user": user, "name": name, "time_ms": when, "text": text}


def answers(values, item_pass=0, users=("a", "b", "c")):
    return [{"item": item, "pass": item_pass, "answer": value, "users": list(users)} for item, value in values.items()]


def factor(score=None, low=None, high=None, chats=5):
    return {"score": score, "low": low, "high": high, "chats": chats}


class Corpus:
    def __init__(self, rows):
        self.lines = rows

    def rows(self, query, values=()):
        times = [row["time_ms"] for row in self.lines]
        if "min(time_ms)" in query:
            return [{"first": min(times), "last": max(times)}]
        return [{"n": sum(1 for value in times if values[0] <= value < values[1])}]

    def messages_between(self, start, end):
        return [dict(row) for row in self.lines if start <= row["time_ms"] < end]

    def around(self, identifier, before=3, after=2):
        return {"before": [{"side": "other", "text": "合成朋友 你平时怎么样", "speaker": "合成朋友", "time": 1}], "after": []}


class Models:
    def __init__(self, reply=None):
        self.requests, self.lock = [], threading.Lock()
        self.reply = reply or json.dumps({"votes": [{"i": 1, "s": 2, "k": "自述"}, {"i": 2, "s": 1, "k": "自述"}],
                                          "answer": 4, "confidence": "中", "why": "合成"})

    def service(self):
        return {"contextTokens": 32768}

    def generate(self, context, request, step=None):
        with self.lock:
            self.requests.append(request)
        return {"text": self.reply}


class TimeAxis(unittest.TestCase):
    def test_half_years_run_newest_first_and_bound_a_period_exactly(self):
        self.assertEqual(period_ids(ms(2024, 12), ms(2026, 1)), ["2026H1", "2025H2", "2025H1", "2024H2"])
        start, end = period_bounds("2025H2")
        self.assertEqual((period_of(start), period_of(end - 1), period_of(end)), ("2025H2", "2025H2", "2026H1"))
        self.assertEqual(period_label("2025H2"), "2025 下半年")


class Instrument(unittest.TestCase):
    def test_every_item_scores_exactly_one_factor_and_has_search_words(self):
        keyed = [abs(item) for keys in FACTORS.values() for item in keys]
        self.assertEqual(sorted(keyed), sorted(ITEMS))
        terms = load_terms()
        self.assertEqual(set(terms), set(ITEMS))
        self.assertTrue(all(sides["yes"] and sides["no"] for sides in terms.values()))

    def test_levels_nest_and_only_deep_repeats(self):
        self.assertEqual({key: (len(items), passes) for key, (items, passes) in PLAN.items()},
                         {"basic": (20, 1), "standard": (50, 1), "deep": (50, 3)})
        self.assertLessEqual(set(MINI), set(ITEMS))
        self.assertEqual(collections.Counter(self_periods.FACTOR[item] for item in MINI), {key: 4 for key in FACTORS})

    def test_the_prompt_cannot_ask_for_activity_or_the_users_answers(self):
        for word in ("发起次数", "活跃度", "自答", "外向性"):
            self.assertNotIn(word, self_periods.SYSTEM)


class Settling(unittest.TestCase):
    def test_an_inner_item_needs_the_users_own_words_whatever_the_model_answered(self):
        behaviour = [{"k": "行为", "s": 2}, {"k": "行为", "s": 2}, {"k": "行为", "s": 1}]
        self.assertIsNone(settled(FEELING, 5, behaviour))
        self.assertEqual(settled(OUTER, 5, behaviour), 5)
        self.assertEqual(settled(FEELING, 2, [{"k": "自述", "s": -1}, {"k": "自述", "s": -2}]), 2)

    def test_lines_driven_by_circumstances_never_hold_up_an_answer(self):
        pushed = [{"k": "处境", "s": 2}, {"k": "处境", "s": 2}, {"k": "处境", "s": 2}, {"k": "行为", "s": 1}]
        self.assertIsNone(settled(OUTER, 5, pushed))

    def test_the_parser_applies_the_override_and_keeps_what_the_model_proposed(self):
        reply = json.dumps({"votes": [{"i": 1, "s": 2, "k": "行为"}, {"i": 2, "s": 1, "k": "行为"}], "answer": 5, "why": "常主动约人"})
        result = parse_answer(FEELING, {1, 2})(reply)
        self.assertEqual((result["answer"], result["proposed"]), (None, 5))

    def test_a_vote_for_an_unshown_line_or_an_unknown_kind_is_dropped(self):
        reply = json.dumps({"votes": [{"i": 9, "s": 2, "k": "自述"}, {"i": 1, "s": 2, "k": "猜测"}, {"i": 1, "s": 3, "k": "自述"},
                                      {"i": 2, "s": 1, "k": "行为"}, {"i": 2, "s": -1, "k": "行为"}], "answer": 3})
        self.assertEqual(parse_answer(OUTER, {1, 2})(reply)["votes"], [{"i": 2, "s": 1, "k": "行为"}])

    def test_split_and_broken_replies_keep_their_answer(self):
        split = '{"votes":[{"i":1,"s":2,"k":"自述"}]}\n{"answer":4,"why":"合成"}'
        self.assertEqual(parse_answer(OUTER, {1})(split)["proposed"], 4)
        broken = '{"votes":[{"i":1,"s":2,"k":"自述"},{"i":2,"s":-1,"k":"行为"}"answer":2,"why":"合成"}'
        result = parse_answer(OUTER, {1, 2})(broken)
        self.assertEqual((result["proposed"], len(result["votes"])), (2, 2))

    def test_a_reply_without_an_answer_is_rejected(self):
        for text in ("没有结论", '{"votes":[]}', '{"answer":7}', '{"answer":1,"why":"} Sorry, I cannot complete this request"}',
                     '{"answer":2,"why":"}<tool_call>用户：根据下面的对话片段"}'):
            with self.assertRaises(SelfError):
                parse_answer(OUTER, {1})(text)

    def test_bad_language_and_role_or_tool_markers_get_distinct_retry_guidance(self):
        for why in ('English explanation', '合成理由 tool_call', '用户：合成理由', '助手：合成理由'):
            with self.subTest(why=why), self.assertRaises(SelfError) as failure:
                parse_answer(OUTER, {1})(json.dumps({'answer': 3, 'why': why}))
            self.assertEqual(failure.exception.code, 'invalid-output')
            if why == 'English explanation':
                self.assertEqual(failure.exception.detail, 'why 用中文写判断理由')
            else:
                self.assertIn(self_periods.RUN_ON_HINT, failure.exception.detail)


class Scoring(unittest.TestCase):
    def test_reverse_keyed_items_are_flipped(self):
        keys = FACTORS["big5_e"]
        forward, reverse = [abs(item) for item in keys if item > 0], [abs(item) for item in keys if item < 0]
        rows = answers({**{item: 5 for item in forward}, **{item: 1 for item in reverse}})
        self.assertEqual(factor_score(rows, "big5_e")["score"], 5)
        self.assertLess(SIGN[reverse[0]], 0)

    def test_too_few_answered_items_is_no_score(self):
        items = [abs(item) for item in FACTORS["big5_o"]]
        rows = answers({item: 4 for item in items[:4]}) + answers({item: None for item in items[4:]})
        result = factor_score(rows, "big5_o")
        self.assertEqual((result["score"], result["items"], result["total"]), (None, 4, 10))
        rows = answers({item: 4 for item in items[:5]}) + answers({item: None for item in items[5:]})
        self.assertIsNotNone(factor_score(rows, "big5_o")["score"])

    def test_only_repeats_produce_a_range_and_it_spans_them(self):
        items = [abs(item) for item in FACTORS["big5_c"]]
        once = answers({item: 3 for item in items})
        self.assertIsNone(factor_score(once, "big5_c")["low"])
        twice = once + [{**row, "pass": 1, "answer": 3 + (SIGN[row["item"]] > 0) - (SIGN[row["item"]] < 0)} for row in once]
        result = factor_score(twice, "big5_c")
        self.assertEqual((result["low"], result["high"]), (3, 4))
        self.assertEqual(result["score"], 3.5)

    def test_the_range_holds_the_score_when_passes_answer_different_items(self):
        items = [abs(item) for item in FACTORS["big5_e"] if item > 0]
        rows = answers({item: 5 for item in items}) + answers({item: 3 for item in items[:3]}, item_pass=1) + \
            answers({item: None for item in items[3:]}, item_pass=1)
        result = factor_score(rows, "big5_e")
        self.assertEqual(result["score"], 4.4)
        self.assertLessEqual(result["low"], result["score"])
        self.assertGreaterEqual(result["high"], result["score"])
        self.assertEqual((result["low"], result["high"]), (3.8, 5))

    def test_a_range_across_the_midpoint_still_gets_the_letter_of_its_score(self):
        factors = {"big5_e": factor(3.05, 2.9, 3.4), "big5_o": factor(2.95, 2.5, 3.2), "big5_a": factor(2.5, 2.2, 2.8), "big5_c": factor(None)}
        self.assertEqual(type_letters(factors), ["E", "S", "T", "?"])

    def test_a_single_pass_names_one_letter_and_a_score_on_the_line_counts_as_high(self):
        factors = {"big5_e": factor(3.1), "big5_o": factor(2.9), "big5_a": factor(2.4), "big5_c": factor(3.0)}
        self.assertEqual(type_letters(factors), ["E", "S", "T", "J"])

    def test_a_change_needs_separate_ranges_enough_size_and_enough_chats(self):
        def timeline(*rows):
            return [{"id": f"p{index}", "factors": {key: (value if key == "big5_e" else factor()) for key in FACTORS}}
                    for index, value in enumerate(rows)]

        apart = timeline(factor(3.0, 2.9, 3.1), factor(3.6, 3.5, 3.7), factor(3.6, 3.5, 3.7))
        self.assertEqual(find_changes(apart), [{"key": "big5_e", "from": "p0", "to": "p1", "delta": .6, "confirmed": True}])
        self.assertFalse(find_changes(timeline(factor(3.0, 2.9, 3.4), factor(3.5, 3.3, 3.7))), "overlapping ranges")
        self.assertFalse(find_changes(timeline(factor(3.0, 2.95, 3.05), factor(3.2, 3.15, 3.25))), "smaller than the noise")
        self.assertFalse(find_changes(timeline(factor(3.0, 2.9, 3.1, 2), factor(3.6, 3.5, 3.7))), "too few chats")

    def test_a_change_the_next_period_undoes_stays_unconfirmed(self):
        rows = [factor(3.0, 2.9, 3.1), factor(3.6, 3.5, 3.7), factor(3.0, 2.9, 3.1)]
        periods = [{"id": f"p{index}", "factors": {key: (value if key == "big5_e" else factor()) for key in FACTORS}}
                   for index, value in enumerate(rows)]
        self.assertEqual([change["confirmed"] for change in find_changes(periods)], [False, False])

    def test_the_most_repeated_claim_stands_for_its_half_year(self):
        rows = [{"value": "ENFP", "time": ms(2025, 2)}, {"value": "ENFP", "time": ms(2025, 3)}, {"value": "INFP", "time": ms(2025, 4)},
                {"value": "INFJ", "time": ms(2026, 9)}, {"value": "INTJ", "time": None}]
        result = reports_by_period(rows)
        self.assertEqual(result["2025H1"], {"value": "ENFP", "count": 2, "others": ["INFP"]})
        self.assertEqual(set(result), {"2025H1", "2026H2"})


class Sampling(unittest.TestCase):
    def setUp(self):
        terms = load_terms()[1]
        word = terms["yes"][2]
        rows = [line(index, f"第{index}句 {word} 其他内容", ms(2025, 1 + index % 6), user=f"chat{index % 40}") for index in range(400)]
        rows += [line(1000, "我是 INFJ 哦", ms(2025, 2)), line(1001, "最近在做 MBTI 性格测试", ms(2025, 2))]
        rows += [line(2000 + index, f"随机的日常闲聊内容{index}", ms(2025, 1 + index % 6), user=f"chat{index % 40}") for index in range(6000)]
        self.period, self.terms = Period("2025H1", rows), terms

    def test_lines_naming_a_type_never_reach_the_model(self):
        self.assertFalse(any("INFJ" in row["text"] or "MBTI" in row["text"] for row in self.period.rows))

    def test_hits_map_back_to_their_rows(self):
        hits = self.period.hits("随机的日常闲聊内容5999")
        self.assertEqual([self.period.rows[index]["text"] for index in hits], ["随机的日常闲聊内容5999"])

    def test_passes_share_no_line_and_a_higher_level_reproduces_the_lower(self):
        three = sample(self.period, 1, self.terms, 3)
        self.assertEqual(sample(self.period, 1, self.terms, 1)[0], three[0])
        sets = [set(picks) for picks, _ in three]
        self.assertFalse(sets[0] & sets[1] or sets[0] & sets[2] or sets[1] & sets[2])
        self.assertTrue(all(sets))

    def test_no_single_chat_fills_a_sample(self):
        picks, _ = sample(self.period, 1, self.terms, 1)[0]
        chats = collections.Counter(self.period.rows[index]["user"] for index in picks)
        self.assertLessEqual(max(chats.values()), 15)


class Answering(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name), "synthetic")
        self.addCleanup(self.store.close)
        patcher = patch("self_periods.MIN_LINES", 5)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.corpus = Corpus([line(index, f"合成朋友 我今天很累 第{index}句", ms(2025, 1 + index % 6), user=f"chat{index % 5}") for index in range(60)])
        self.models = Models()
        self.context = SimpleNamespace(check=lambda: None, cancelled=threading.Event())

    def run_level(self, depth):
        plugin = SimpleNamespace(models=self.models)
        before = len(self.models.requests)
        PeriodAnswers(plugin, self.store, "synthetic", self.corpus, depth).work(self.context)
        return len(self.models.requests) - before

    def test_each_level_only_adds_what_the_last_one_lacks(self):
        self.assertEqual(self.run_level("basic"), 20)
        self.assertEqual(self.run_level("standard"), 30)
        self.assertEqual(self.run_level("deep"), 100)
        self.assertEqual(self.run_level("deep"), 0)

    def test_a_small_growth_keeps_a_period_and_a_large_one_asks_again(self):
        self.run_level("basic")
        self.corpus.lines.extend(line(100 + index, f"新增一句{index}", ms(2025, 3)) for index in range(3))
        self.assertEqual(self.run_level("basic"), 0)
        self.corpus.lines.extend(line(200 + index, f"又新增一句{index}", ms(2025, 3)) for index in range(10))
        self.assertEqual(self.run_level("basic"), 20)

    def test_new_rules_discard_old_answers(self):
        self.run_level("basic")
        with patch("self_periods.PeriodAnswers.rules", "other-rules"):
            self.assertEqual(self.run_level("basic"), 20)

    def test_the_prompt_has_no_dates_names_or_keys(self):
        self.run_level("basic")
        prompt = self.models.requests[0]["prompt"]
        self.assertIn("TA 我今天很累", prompt)
        self.assertIn("TA 你平时怎么样", prompt)
        for leaked in ("合成朋友", "2025", "反向", "big5", "chat1"):
            self.assertNotIn(leaked, prompt)

    def test_formatting_note_leaves_the_instrument_and_completed_cache_alone(self):
        self.run_level('basic')
        for request in self.models.requests:
            self.assertEqual(request['system'], self_periods.SYSTEM)
            self.assertTrue(request['prompt'].endswith(self_periods.OUTPUT_FORMAT_NOTE.format(field='why')))
        before = PeriodStore(self.store).answers()
        with patch('self_periods.OUTPUT_FORMAT_NOTE', '\n\n另一个合成格式提醒：{field}'):
            self.assertEqual(self.run_level('basic'), 0)
        self.assertEqual(PeriodStore(self.store).answers(), before)

    def test_role_marker_rejection_reaches_the_next_request_without_changing_its_samples(self):
        job = PeriodAnswers(SimpleNamespace(models=self.models), self.store, 'synthetic', self.corpus, 'basic')
        period = job._open('2025H1')
        shown = {1: period.rows[0], 2: period.rows[1]}
        requests = []

        def model(context, request, step):
            requests.append(dict(request))
            if len(requests) == 1:
                return {'text': json.dumps({'answer': 3, 'why': '助手：合成续写'})}
            self.assertFalse(PeriodStore(self.store).done(period.id), 'the rejected answer must not be saved')
            return {'text': self.models.reply}

        with patch.object(job, '_model_call', side_effect=model):
            job._answer(self.context, period.id, (MINI[0], 0), shown, '合成规则', '合成题目和片段',
                        'synthetic-answer', parse_answer(MINI[0], set(shown)))
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]['system'], '合成规则')
        self.assertIn(self_periods.RUN_ON_HINT, requests[1]['system'])
        self.assertEqual(requests[0]['prompt'], requests[1]['prompt'])
        self.assertEqual(requests[0]['prompt'].count('输出要求：'), 1)
        self.assertEqual([request['maxOutputTokens'] for request in requests], [2400, 2400])
        self.assertEqual(PeriodStore(self.store).done(period.id), {(MINI[0], 0)})

    def test_an_unreadable_reply_costs_one_answer_and_is_asked_again(self):
        self.models.reply = "不是结论"
        plugin = SimpleNamespace(models=self.models)
        job = PeriodAnswers(plugin, self.store, "synthetic", self.corpus, "basic")
        with self.assertRaises(SelfError) as failure:
            job.work(self.context)
        self.assertEqual(failure.exception.code, "period-incomplete")
        self.assertEqual(job.view()["skipped"], 20)
        self.assertEqual(job.view()["attempted"], 60)
        self.assertEqual(job.view()["retryRound"], 2)
        self.assertEqual(periods_view(self.store)["periods"][0]["status"], "error")
        self.assertEqual(periods_view(self.store)["coverage"]["failed"], 1)
        self.assertFalse(PeriodStore(self.store).done("2025H1"))
        self.models.reply = Models().reply
        self.assertEqual(self.run_level("basic"), 20)

    def test_bounded_retry_only_reasks_uncached_items_in_the_same_run(self):
        self.run_level("basic")
        periods = PeriodStore(self.store)
        with self.store.lock, self.store.db:
            self.store.db.execute('DELETE FROM period_answers WHERE period=? AND item=?', ("2025H1", MINI[0]))
        calls = []
        job = PeriodAnswers(SimpleNamespace(models=self.models), self.store, "synthetic", self.corpus, "basic")

        def answer(context, period, terms, task):
            calls.append(task)
            if len(calls) == 3:
                periods.save(period.id, *task, {"answer": None, "proposed": None, "confidence": None, "why": "依据不足"}, [])

        with patch.object(job, '_ask', side_effect=answer):
            job.work(self.context)
        self.assertEqual(calls, [(MINI[0], 0)] * 3)
        value = job.view()
        self.assertEqual((value["total"], value["done"], value["attempted"], value["skipped"]), (1, 1, 3, 0))
        self.assertEqual((value["plannedAnswers"], value["cachedAnswers"]), (20, 19))
        self.assertEqual(value["progressScope"], "missing-answers-this-run")
        self.assertEqual(len(periods.done("2025H1")), 20)

    def test_proven_truncation_gets_a_bounded_larger_budget_without_accepting_partial_text(self):
        job = PeriodAnswers(SimpleNamespace(models=self.models), self.store, "synthetic", self.corpus, "basic")
        period = job._open("2025H1")
        shown = {1: period.rows[0], 2: period.rows[1]}
        requests = []

        def model(context, request, step):
            requests.append(dict(request))
            if len(requests) < 3:
                error = SelfError('output-truncated', '合成截断')
                error.partial = self.models.reply  # Even parseable partial text is not a complete answer.
                raise error
            return {"text": self.models.reply}

        with patch.object(job, '_model_call', side_effect=model):
            job._answer(self.context, period.id, (MINI[0], 0), shown, '合成规则', '合成题目',
                        'synthetic-answer', parse_answer(MINI[0], set(shown)))
        self.assertEqual([request['maxOutputTokens'] for request in requests], [2400, 4800, 4800])
        self.assertEqual(PeriodStore(self.store).done(period.id), {(MINI[0], 0)})
        self.assertIn('压缩', requests[1]['system'])

    def test_persistently_truncated_answer_stays_uncached_and_never_exceeds_retry_cap(self):
        job = PeriodAnswers(SimpleNamespace(models=self.models), self.store, "synthetic", self.corpus, "basic")
        period = job._open("2025H1")
        shown = {1: period.rows[0], 2: period.rows[1]}
        caps = []

        def model(context, request, step):
            caps.append(request['maxOutputTokens'])
            raise SelfError('output-truncated', '合成截断')

        with patch.object(job, '_model_call', side_effect=model):
            for _ in range(3):
                job._answer(self.context, period.id, (MINI[0], 0), shown, '合成规则', '合成题目',
                            'synthetic-answer', parse_answer(MINI[0], set(shown)))
        self.assertEqual(caps, [2400] + [4800] * 8)
        self.assertFalse(PeriodStore(self.store).done(period.id))

    def test_shrink_requires_a_complete_verified_corpus_and_real_deletions_invalidate(self):
        self.run_level("basic")
        periods = PeriodStore(self.store)
        self.corpus.lines.pop()
        complete = {"catalogComplete": True, "directoryPartial": False, "conversations": 1,
                    "readConversations": 1, "failedConversations": 0, "unreadMessages": 0, "gaps": []}
        self.corpus.counts = lambda: {**complete, "catalogComplete": False}
        job = PeriodAnswers(SimpleNamespace(models=self.models), self.store, "synthetic", self.corpus, "basic")
        with self.assertRaises(SelfError) as failure:
            job._plan()
        self.assertEqual(failure.exception.code, "corpus-incomplete")
        self.assertEqual(len(periods.done("2025H1")), 20)
        self.corpus.counts = lambda: complete
        self.assertEqual(sum(len(pending) for _, pending in job._plan()), 20)
        self.assertFalse(periods.done("2025H1"))

    def test_a_verified_period_removed_entirely_does_not_keep_stale_answers(self):
        self.run_level("basic")
        self.corpus.lines = [line(index, f"合成新时期{index}", ms(2026, 3)) for index in range(10)]
        self.corpus.counts = lambda: {"catalogComplete": True, "directoryPartial": False,
                                     "conversations": 1, "readConversations": 1, "gaps": []}
        job = PeriodAnswers(SimpleNamespace(models=self.models), self.store, "synthetic", self.corpus, "basic")
        job._plan()
        periods = PeriodStore(self.store)
        self.assertFalse(periods.done("2025H1"))
        old = next(row for row in periods_view(self.store)["periods"] if row["id"] == "2025H1")
        self.assertEqual((old["status"], old["lines"]), ("insufficient", 0))

    def test_missing_periods_are_visible_and_partial_answers_keep_the_full_denominator(self):
        job = PeriodAnswers(SimpleNamespace(models=self.models), self.store, "synthetic", self.corpus, "deep")
        job._plan()
        periods = PeriodStore(self.store)
        periods.save("2025H1", 1, 0, {"answer": 4, "proposed": 4, "confidence": "中", "why": "合成"}, [])
        view = periods_view(self.store)
        period = view["periods"][0]
        self.assertEqual((period["status"], period["answered"], period["expectedAnswers"]), ("partial", 1, 150))
        self.assertIsNone(period["factors"]["big5_e"]["score"])
        self.assertEqual(view["coverage"]["pending"], 1)

    def test_detail_pages_expose_every_unique_saved_line(self):
        from self_periods import period_evidence
        periods = PeriodStore(self.store)
        votes = [{"id": f"v{index}", "s": 1, "k": "自述", "user": "friend", "name": "合成朋友", "time": index,
                  "text": f"合成依据{index}"} for index in range(19)]
        periods.save("2025H1", 1, 0, {"answer": 4, "proposed": 4, "confidence": "中", "why": "合成"}, votes)
        item = period_detail(self.store, "2025H1", "big5_e")["items"][0]
        self.assertEqual((len(item["evidence"]), item["evidenceTotal"], item["evidenceNextOffset"]), (6, 19, 6))
        rest = period_evidence(self.store, "2025H1", 1, 6, 100, expected_version=item["evidenceVersion"])
        self.assertEqual((len(rest["items"]), rest["total"], rest["nextOffset"]), (13, 19, None))
        self.assertEqual(rest["version"], item["evidenceVersion"])
        self.assertEqual(len({vote["id"] for vote in item["evidence"] + rest["items"]}), 19)
        periods.save("2025H1", 1, 1, {"answer": 4, "proposed": 4, "confidence": "中", "why": "合成"},
                     [{**votes[0], "id": "new", "time": -1}])
        with self.assertRaises(SelfError) as failure:
            period_evidence(self.store, "2025H1", 1, 6, 100, expected_version=item["evidenceVersion"])
        self.assertEqual(failure.exception.code, "stale-evidence")
        current = period_detail(self.store, "2025H1", "big5_e")["items"][0]
        self.assertNotEqual(current["evidenceVersion"], item["evidenceVersion"])
        self.assertEqual(period_evidence(self.store, "2025H1", 1, expected_version=current["evidenceVersion"])["total"], 20)

    def test_the_view_and_the_detail_show_what_was_answered(self):
        self.run_level("deep")
        view = periods_view(self.store)
        period = view["periods"][0]
        self.assertEqual((period["id"], period["passes"], period["items"], period["lines"]), ("2025H1", 3, 50, 60))
        self.assertEqual(period["factors"]["big5_e"]["score"], 3)  # 4 on every item, half of them reverse keyed
        self.assertEqual(view["answers"], 150)
        detail = period_detail(self.store, "2025H1", "big5_e")
        self.assertEqual(len(detail["items"]), 10)
        first = detail["items"][0]
        self.assertEqual((first["item"], first["answers"], first["reverse"]), (1, [4, 4, 4], False))
        self.assertEqual(first["evidence"][0]["k"], "自述")
        self.assertEqual(first["evidence"][0]["name"], "合成朋友")
        with self.assertRaises(SelfError):
            period_detail(self.store, "2025H1", "not-a-key")


class Editing(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name), "synthetic")
        self.addCleanup(self.store.close)
        periods, forward = PeriodStore(self.store), {abs(item): item > 0 for keys in FACTORS.values() for item in keys}
        votes = [{"user": user, "k": "行为", "s": 1} for user in "abc"]
        for period, value in (("2025H1", 4), ("2025H2", 2)):
            periods.prepare(period, 400)
            for item in ITEMS:
                answer = value if forward[item] else 6 - value
                for number in range(3):
                    periods.save(period, item, number, {"answer": answer, "proposed": answer, "confidence": "中", "why": "合成"}, votes)
        with self.store.lock, self.store.db:
            self.store._meta("periodReports", {"2025H1": {"value": "ENFJ", "count": 2, "others": []}})

    def period(self, identifier="2025H1"):
        return next(period for period in periods_view(self.store)["periods"] if period["id"] == identifier)

    def test_the_users_type_shows_and_the_model_stays_on_record(self):
        before = self.period()
        save_period_edit(self.store, "2025H1", "type", "INFJ")
        after = self.period()
        self.assertEqual(after["letters"], ["INFJ"])
        self.assertEqual(after["ai"]["type"], "".join(before["letters"]))
        self.assertEqual(after["edited"], ["type"])

    def test_a_report_can_be_changed_or_blanked_and_the_model_comes_back(self):
        save_period_edit(self.store, "2025H1", "report", "INFJ")
        self.assertEqual(self.period()["report"]["value"], "INFJ")
        self.assertEqual(self.period()["ai"]["report"], "ENFJ")
        save_period_edit(self.store, "2025H1", "report", "")
        self.assertIsNone(self.period()["report"])
        save_period_edit(self.store, "2025H1", "report", None)
        self.assertEqual(self.period()["report"]["value"], "ENFJ")
        self.assertEqual(self.period()["edited"], [])
        self.assertEqual(self.store.meta("periodEdits", {}), {})

    def test_an_edited_score_moves_the_letter_unless_the_type_was_set_too(self):
        before = self.period()["factors"]["big5_e"]["score"]
        save_period_edit(self.store, "2025H1", "score:big5_e", 1.5)
        period = self.period()
        self.assertEqual(period["factors"]["big5_e"]["score"], 1.5)
        self.assertEqual(period["ai"]["score:big5_e"], before)
        self.assertEqual(period["letters"][0], "I")
        save_period_edit(self.store, "2025H1", "type", "ENFJ")
        self.assertEqual(self.period()["letters"], ["ENFJ"])

    def test_an_answered_question_counts_in_the_score_and_the_detail_keeps_both(self):
        item = abs(FACTORS["big5_e"][0])
        before = self.period()["factors"]["big5_e"]["score"]
        save_period_edit(self.store, "2025H1", f"item:{item}", 1 if FACTORS["big5_e"][0] > 0 else 5)
        period = self.period()
        self.assertLess(period["factors"]["big5_e"]["score"], before)
        self.assertEqual(period["ai"]["score:big5_e"], before)
        row = next(row for row in period_detail(self.store, "2025H1", "big5_e")["items"] if row["item"] == item)
        self.assertEqual(row["answers"], [4, 4, 4])
        self.assertEqual(row["mine"], 1 if FACTORS["big5_e"][0] > 0 else 5)

    def test_edits_never_change_the_stored_answers_or_the_changes_found(self):
        stored = PeriodStore(self.store).answers()
        changes = periods_view(self.store)["changes"]
        self.assertTrue(changes)
        save_period_edit(self.store, "2025H2", "score:big5_e", 5)
        self.assertEqual(PeriodStore(self.store).answers(), stored)
        self.assertEqual(periods_view(self.store)["changes"], changes)

    def test_only_real_fields_and_values_are_saved(self):
        for period, field, value in (("2025H3", "type", "INFJ"), ("2025-2027", "type", "INFJ"), ("2025H1", "type", "E/I"), ("2025H1", "report", 3), ("2025H1", "target", "x"),
                                     ("2025H1", "score:big5_x", 3), ("2025H1", "score:big5_e", 6), ("2025H1", "score:big5_e", "3"),
                                     ("2025H1", "item:999", 3), ("2025H1", "item:1", 2.5), ("2025H1", "item:1", True), ("2025H1", None, 3)):
            with self.assertRaises(SelfError):
                save_period_edit(self.store, period, field, value)


class ScheduledModels(UnifiedModels):
    def generate(self, task, request, step=None, timeout=None):
        if step == "self-periods-answer":
            self.requests.append({**request, "step": step})
            return {"text": json.dumps({"votes": [], "answer": None, "why": ""})}
        return super().generate(task, request, step, timeout)


class Grain(unittest.TestCase):
    def test_a_moment_falls_in_one_period_of_each_length(self):
        moment = int(datetime.datetime(2025, 8, 15, 12).timestamp() * 1000)
        self.assertEqual([period_of(moment, months) for months in MONTHS], ["2025Q3", "2025H2", "2025", "2024-2025", "2025-2029"])

    def test_the_half_year_default_keeps_the_ids_already_stored(self):
        self.assertEqual(period_of(int(datetime.datetime(2026, 7, 1).timestamp() * 1000)), "2026H2")
        self.assertEqual(period_label("2026H2"), "2026 下半年")

    def test_bounds_hold_the_moment_and_meet_the_next_period(self):
        for months in MONTHS:
            first = int(datetime.datetime(2019, 1, 1).timestamp() * 1000)
            last = int(datetime.datetime(2026, 11, 1).timestamp() * 1000)
            ids = period_ids(first, last, months)[::-1]
            spans = [period_bounds(period) for period in ids]
            for before, after in zip(spans, spans[1:]):
                self.assertEqual(before[1], after[0], months)
            self.assertLessEqual(spans[0][0], first)
            self.assertGreater(spans[-1][1], last)
            self.assertTrue(all(valid_period(period) and of_length(period, months) for period in ids))

    def test_labels_name_the_period(self):
        self.assertEqual([period_label(period) for period in ("2026Q2", "2026H1", "2026", "2024-2025", "2025-2029")],
                         ["2026 第二季度", "2026 上半年", "2026 年", "2024–2025 年", "2025–2029 年"])

    def test_only_the_offered_lengths_are_periods(self):
        for period in ("2026H3", "2026Q5", "2025-2027", "26H1", "", None, 2026):
            self.assertFalse(valid_period(period), period)

    def test_a_self_report_is_kept_for_every_length(self):
        records = [{"value": "INFJ", "time": int(datetime.datetime(2026, 8, 1).timestamp() * 1000)}]
        self.assertEqual(list(reports_by_period(records, 12)), ["2026"])
        self.assertEqual(list(reports_by_period(records)), ["2026H2"])


class Scheduling(UnifiedTests):
    def test_the_grain_setting_decides_the_periods_of_a_run(self):
        self.setup_unified(live={"friend-a": dialogue(prefix="足够长的一句本人发言")})
        self.models = ScheduledModels(configured=True)
        self.plugin.models = self.models
        self.core.plugin_settings = {"analysisDepth": "basic", "replyDepth": "standard", "analysisGrain": "12"}
        with patch("self_periods.MIN_LINES", 1), patch("self_periods.WORKERS", 1):
            self.assertEqual(self.run_analysis(outputs=["periods"])["state"], "done")
        ids = [period["id"] for period in self.service.periods(self.account)["periods"]]
        self.assertTrue(ids and all(re.fullmatch(r"\d{4}", identifier) for identifier in ids), ids)
        self.core.plugin_settings = {**self.core.plugin_settings, "analysisGrain": "6"}
        result = self.service.periods(self.account)
        self.assertEqual((result["periods"], result["others"]), ([], True))

    def test_ticking_it_runs_it_without_shared_evidence(self):
        self.setup_unified(live={"friend-a": dialogue(prefix="足够长的一句本人发言")})
        self.models = ScheduledModels(configured=True)
        self.plugin.models = self.models
        with patch("self_periods.MIN_LINES", 1), patch("self_periods.WORKERS", 1):
            result = self.run_analysis(outputs=["periods"])
        self.assertEqual(result["state"], "done", result)
        states = {step["key"]: step["state"] for step in result["steps"]}
        self.assertEqual(states["periods"], "done")
        self.assertEqual(states["evidence"], "skipped")
        names = [item["step"] for item in self.models.requests]
        self.assertGreater(names.count("self-periods-answer"), 0)
        self.assertNotIn("self-evidence-extract", names)

    def test_a_default_selection_leaves_it_off(self):
        self.setup_unified()
        self.assertNotIn("periods", self.service.analysis_settings(self.account, {})["outputs"])


del UnifiedTests

if __name__ == "__main__":
    unittest.main()
