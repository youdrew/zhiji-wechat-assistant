"""三观时间线: the statements, the sentences per period, edits and the period length."""
import json
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend")]
from self_contracts import SelfError
from self_periods import OUTPUT_FORMAT_NOTE, RUN_ON_HINT, Period, PeriodStore, factor_score, load_terms, parse_answer, period_evidence, sample
from self_worldview import (INNER, ITEMS, SCALES, TABLE, VIEWS, POLITICAL_ITEMS, POLITICAL_WEIGHTS, WorldviewAnswers,
                            parse_view, save_worldview_edit, worldview_detail, worldview_score,
                            worldview_letters, worldview_style, worldview_view, matched_count)
from period_fixtures import Corpus, fresh_store, ms


def settled(score):
    return {"score": score, "low": score - .1, "high": score + .1}


def own(index, text, when):
    return {"id": f"m{index}", "user": f"chat{index % 3}", "name": "合成朋友", "time_ms": when, "text": text}


class Lines(Corpus):
    """Counts a period's lines across all chats, as 性格时间线 and 三观时间线 do."""

    def rows(self, query, values=()):
        if "count(*) n" in query:
            return [{"n": sum(1 for row in self.lines if values[0] <= row["time_ms"] < values[1])}]
        return super().rows(query, values)


def corpus():
    talk = [own(index, f"以后的打算是存钱，我觉得{POLITICAL_ITEMS[1002]['yes'][0]}很重要 {index}", ms(2026, 2) + index * 60000) for index in range(40)]
    return Lines(talk + [own(1000 + index, f"今天吃了第 {index} 顿饭", ms(2026, 3) + index * 60000) for index in range(900)], {})


class Models:
    def __init__(self):
        self.requests, self.lock = [], threading.Lock()

    def service(self):
        return {"contextTokens": 32768}

    def generate(self, context, request, step=None):
        with self.lock:
            self.requests.append({**request, "step": step})
        if step == "self-worldview-view":
            return {"text": json.dumps({"text": "这段时期想先把工作定下来", "lines": [1, 2], "confidence": "中"}, ensure_ascii=False)}
        return {"text": json.dumps({"votes": [{"i": 1, "s": 2, "k": "自述"}, {"i": 2, "s": 1, "k": "自述"}], "answer": 4,
                                    "confidence": "中", "why": "合成"}, ensure_ascii=False)}


class Context:
    def check(self):
        pass

    def progress(self, **values):
        pass


def run(store, corpus, months=6, depth="basic"):
    models = Models()
    job = WorldviewAnswers(SimpleNamespace(models=models), store, "synthetic", corpus, depth, months)
    job.generate = lambda context, request, step, parse, **kwargs: parse(models.generate(context, request, step)["text"])
    job.close_models = lambda: None
    with patch("self_periods.WORKERS", 1):
        job.work(Context())
    return models


class Instrument(unittest.TestCase):
    def test_weighted_scores_match_the_pinned_upstream_formula(self):
        # Reference vector: original question number modulo 5 + 1; upstream quiz.html
        # gives equality=47.435897%, liberty=52.34375%. Convert to our 1..5 axes.
        rows = [{"item": item, "pass": 0, "answer": row["number"] % 5 + 1, "users": ["chat"]}
                for item, row in POLITICAL_ITEMS.items()]
        self.assertEqual(worldview_score(rows, "pol_econ")["score"], 3.1)
        self.assertEqual(worldview_score(rows, "pol_lib")["score"], 2.91)
        self.assertEqual((len(POLITICAL_ITEMS), len(POLITICAL_WEIGHTS["pol_econ"]), len(POLITICAL_WEIGHTS["pol_lib"])), (54, 22, 37))

    def test_partial_runs_and_missing_answers_do_not_become_confident_scores(self):
        rows = [{"item": 1001, "pass": 0, "answer": 5, "users": ["chat"]}]
        self.assertIsNone(worldview_score(rows, "pol_econ")["score"])
        weights = POLITICAL_WEIGHTS["pol_econ"]
        # Half the questions with the smallest weights are less than half the weight.
        ids = sorted(weights, key=lambda item: abs(weights[item]))[:11]
        rows = [{"item": item, "pass": 0, "answer": 3, "users": ["chat"]} for item in ids]
        self.assertIsNone(worldview_score(rows, "pol_econ")["score"])
        rows = [{"item": item, "pass": 0, "answer": None, "users": []} for item in weights]
        self.assertIsNone(worldview_score(rows, "pol_econ")["score"])
        for row in rows:
            row["answer"] = 3
        self.assertEqual(worldview_score(rows, "pol_econ")["score"], 3)

    def test_partial_value_axes_use_the_whole_instrument_as_the_denominator(self):
        for factor in ("val_change", "val_self"):
            items = [abs(item) for item in SCALES[factor]]
            rows = [{"item": item, "pass": 0, "answer": 4, "users": ["chat"]} for item in items[:2]]
            result = worldview_score(rows, factor)
            self.assertEqual(result["total"], 6)
            self.assertIsNone(result["score"])
            rows.append({"item": items[2], "pass": 0, "answer": 4, "users": ["chat"]})
            self.assertIsNotNone(worldview_score(rows, factor)["score"])

    def test_repeated_passes_keep_the_weighted_score_inside_the_range(self):
        rows = [{"item": item, "pass": number, "answer": (None if number == 1 and item % 2 else number + 2), "users": ["chat"]}
                for number in range(3) for item in POLITICAL_ITEMS]
        for key in POLITICAL_WEIGHTS:
            result = worldview_score(rows, key)
            self.assertLessEqual(result["low"], result["score"])
            self.assertLessEqual(result["score"], result["high"])

    def test_every_statement_scores_one_axis_and_can_be_searched(self):
        self.assertEqual({abs(item) for keys in SCALES.values() for item in keys}, set(ITEMS))
        self.assertEqual(set(load_terms("worldview-terms.json")) | set(POLITICAL_ITEMS), {*ITEMS, *VIEWS})

    def test_each_axis_asks_from_both_ends(self):
        for key, items in SCALES.items():
            self.assertTrue(any(item > 0 for item in items) and any(item < 0 for item in items), key)

    def test_a_low_answer_on_a_reverse_statement_counts_toward_the_high_end(self):
        rows = [{"item": abs(item), "pass": 0, "answer": 1 if item < 0 else 5, "users": ["chat"]} for item in SCALES["pol_econ"]]
        self.assertEqual(worldview_score(rows, "pol_econ")["score"], 5)

    def test_an_opinion_needs_the_users_own_words(self):
        behaviour = json.dumps({"votes": [{"i": 1, "s": 2, "k": "行为"}, {"i": 2, "s": 2, "k": "行为"}], "answer": 5})
        self.assertIsNone(parse_answer(1001, {1, 2}, INNER)(behaviour)["answer"])
        self.assertEqual(parse_answer(14, {1, 2}, INNER)(behaviour)["answer"], 5)

    def test_every_scored_period_lands_in_one_quadrant(self):
        factors = {key: settled(2) for key in SCALES}
        self.assertEqual(worldview_letters({**factors, "pol_lib": settled(4)}), ["社会主义"])
        self.assertEqual(worldview_letters({**factors, "pol_econ": settled(4), "pol_lib": settled(4)}), ["保守主义"])
        self.assertEqual(worldview_letters(factors), ["社会民主主义"])
        self.assertEqual(worldview_letters({**factors, "pol_econ": settled(3.01)}), ["保守自由主义"])
        self.assertEqual(worldview_style({**factors, "val_self": settled(4)}), "理想派")
        self.assertEqual(worldview_style({**factors, "val_change": settled(4), "val_self": settled(4)}), "守护派")
        self.assertEqual(worldview_style(factors), "进取派")
        self.assertEqual(worldview_style({**factors, "val_change": settled(4)}), "务实派")
        self.assertEqual(worldview_letters({**factors, "pol_lib": {"score": None}}), [])
        self.assertEqual(worldview_style({**factors, "val_self": {"score": None}}), "")

    def test_a_sentence_without_a_real_line_is_dropped(self):
        parse = parse_view({1, 2})
        self.assertEqual(parse(json.dumps({"text": "想先定下工作", "lines": [9]}, ensure_ascii=False))["why"], "")
        result = parse(json.dumps({"text": "想先定下工作", "lines": [2, 2, 1, "x"], "confidence": "高"}, ensure_ascii=False))
        self.assertEqual((result["why"], [vote["i"] for vote in result["votes"]]), ("想先定下工作", [2, 1]))
        with self.assertRaises(SelfError):
            parse("not json")
        with self.assertRaises(SelfError):
            parse(json.dumps({"text": "no chinese here", "lines": [1]}))

    def test_sentence_role_markers_are_rejected_with_the_matching_format_guidance(self):
        for summary in ('English sentence', '合成看法 tool_call', '用户：合成看法', '助手：合成看法'):
            with self.subTest(summary=summary), self.assertRaises(SelfError) as failure:
                parse_view({1})(json.dumps({'text': summary, 'lines': [1]}))
            self.assertEqual(failure.exception.code, 'invalid-output')
            if summary == 'English sentence':
                self.assertEqual(failure.exception.detail, 'text 用中文写一句话')
            else:
                self.assertIn(RUN_ON_HINT, failure.exception.detail)


class Answering(unittest.TestCase):
    def setUp(self):
        self.store, self.corpus = fresh_store(self), corpus()

    def test_a_statement_nobody_talks_about_costs_no_call(self):
        models = run(self.store, self.corpus)
        asked = [request["prompt"].split("\n")[0] for request in models.requests]
        self.assertIn("题目：" + ITEMS[1002], asked)
        self.assertNotIn("题目：" + ITEMS[1060], asked)
        self.assertEqual(len(PeriodStore(self.store, TABLE).done("2026H1")), len(ITEMS) + len(VIEWS))

    def test_rare_but_sufficient_views_are_not_lost_by_rounding_a_long_periods_rate(self):
        lines = [own(index, f"我觉得存钱很重要 {index}", ms(2026, 2) + index * 60000) for index in range(5)]
        lines.extend(own(index, f"今天吃了第 {index} 顿饭", ms(2026, 3) + index * 60000) for index in range(5, 20005))
        period = Period("2026H1", lines)
        terms = {"yes": ["存钱"], "no": ["负债"]}
        self.assertEqual(sample(period, 103, terms, 1)[0][1], 0.0)
        self.assertEqual(matched_count(period, terms), 5)
        job = WorldviewAnswers(SimpleNamespace(models=Models()), self.store, "synthetic", Lines(lines, {}), "basic")
        with patch.object(job, "_answer") as answer:
            job._ask(Context(), period, {103: terms}, (103, 0))
        self.assertTrue(answer.call_args.args[3], "sufficient exact matches must still reach the model")

    def test_overlapping_search_terms_do_not_inflate_a_sparse_views_evidence(self):
        period = Period("2026H1", [own(index, "存钱和投资" if index < 3 else f"今天吃了第 {index} 顿饭",
                                      ms(2026, 2) + index * 60000) for index in range(1000)])
        terms = {"yes": ["存钱", "投资"], "no": ["存钱"]}
        self.assertEqual(matched_count(period, terms), 3)
        job = WorldviewAnswers(SimpleNamespace(models=Models()), self.store, "synthetic", self.corpus, "basic")
        with patch.object(job, "_answer") as answer:
            job._ask(Context(), period, {103: terms}, (103, 0))
        self.assertFalse(answer.call_args.args[3], "three lines do not become enough through repeated matching")

    def test_the_prompt_has_no_dates_names_or_scales(self):
        for request in run(self.store, self.corpus).requests:
            for leaked in ("2026", "合成朋友", "pol_econ", "chat1"):
                self.assertNotIn(leaked, request["prompt"])
            field = 'text' if request['step'] == 'self-worldview-view' else 'why'
            self.assertTrue(request['prompt'].endswith(OUTPUT_FORMAT_NOTE.format(field=field)))
            self.assertNotIn('输出要求：', request['system'])

    def test_the_view_carries_the_sentence_scores_and_nothing_for_other_lengths(self):
        run(self.store, self.corpus)
        result = worldview_view(self.store)
        self.assertEqual([period["id"] for period in result["periods"]], ["2026H1"])
        entry = result["periods"][0]
        self.assertEqual(entry["views"]["plan"]["text"], "这段时期想先把工作定下来")
        self.assertEqual(entry["ai"]["view:plan"], "这段时期想先把工作定下来")
        self.assertEqual(entry["items"], len({row["item"] for row in PeriodStore(self.store, TABLE).answers() if row["item"] in ITEMS}))
        self.assertTrue(entry["views"]["plan"]["evidence"][0]["text"])
        self.assertEqual((result["months"], entry["status"], entry["complete"]), (6, "complete", True))
        self.assertEqual(entry["views"]["plan"]["status"], "complete")
        for key, observed in entry['views'].items():
            self.assertEqual(VIEWS[observed['item']], key)
            self.assertEqual(observed['evidenceTotal'], len(observed['evidence']))
            self.assertIsNone(observed['evidenceNextOffset'], 'normal narrative citations are already returned in full')
            page = period_evidence(self.store, entry['id'], observed['item'], table=TABLE,
                                   expected_version=observed['evidenceVersion'])
            self.assertEqual((page['items'], page['version']), (observed['evidence'], observed['evidenceVersion']))
        self.assertEqual(entry["views"]["love"]["status"], "insufficient")
        self.assertEqual(result["coverage"]["completed"], 1)
        year = worldview_view(self.store, 12)
        self.assertEqual((year["periods"], year["others"]), ([], True))

    def test_planned_but_unanswered_and_sparse_periods_remain_visible(self):
        answers = PeriodStore(self.store, TABLE)
        total = len(ITEMS) + len(VIEWS)
        answers.set_plan({"2025H1": {"lines": 400, "total": total, "reason": ""},
                          "2025H2": {"lines": 12, "total": 0, "reason": "insufficient"}}, 6, "basic")
        result = worldview_view(self.store)
        self.assertEqual([(row["id"], row["status"]) for row in result["periods"]],
                         [("2025H1", "pending"), ("2025H2", "insufficient")])
        self.assertEqual(result["coverage"], {"total": 2, "completed": 0, "failed": 0, "pending": 1, "insufficient": 1,
                                             "noTarget": 0, "answers": 0, "expectedAnswers": total})
        self.assertEqual(result["periods"][0]["views"]["plan"]["status"], "pending")
        self.assertEqual(result["periods"][1]["views"]["plan"]["status"], "insufficient")
        self.assertEqual(worldview_view(self.store, 12)["periods"], [])

    def test_a_completed_sentence_does_not_mark_an_unfinished_period_complete(self):
        answers = PeriodStore(self.store, TABLE)
        answers.prepare("2026H1", 1000)
        answers.set_plan({"2026H1": {"lines": 1000, "total": len(ITEMS) + len(VIEWS), "reason": ""}}, 6, "basic")
        answers.save("2026H1", 104, 0, {"answer": None, "proposed": None, "confidence": "中", "why": "想先定下工作"}, [])
        entry = worldview_view(self.store)["periods"][0]
        self.assertEqual((entry["status"], entry["complete"], entry["answered"]), ("partial", False, 1))

    def test_each_length_keeps_its_own_answers(self):
        run(self.store, self.corpus)
        run(self.store, self.corpus, 12)
        self.assertEqual([period["id"] for period in worldview_view(self.store, 12)["periods"]], ["2026"])
        self.assertEqual([period["id"] for period in worldview_view(self.store, 6)["periods"]], ["2026H1"])
        self.assertEqual(worldview_view(self.store, 6)["others"], True)

    def test_a_finished_period_is_not_asked_again(self):
        run(self.store, self.corpus)
        self.assertEqual(run(self.store, self.corpus).requests, [])

    def test_deep_repeats_the_statements_but_writes_each_sentence_once(self):
        models = run(self.store, self.corpus, depth="deep")
        self.assertEqual([request["step"] for request in models.requests].count("self-worldview-view"), 2, "only 金钱观 and 人生规划 have lines")
        done = PeriodStore(self.store, TABLE).done("2026H1")
        self.assertEqual({number for item, number in done if item in VIEWS}, {0})
        self.assertEqual({number for item, number in done if item in ITEMS}, {0, 1, 2})

    def test_the_detail_lists_the_statements_of_one_axis(self):
        run(self.store, self.corpus)
        detail = worldview_detail(self.store, "2026H1", "pol_econ")
        self.assertEqual([item["item"] for item in detail["items"]], sorted(POLITICAL_WEIGHTS["pol_econ"]))
        self.assertTrue(detail["items"][0]["reverse"])
        self.assertEqual(detail["items"][0]["source"], "8values 第 1 题")
        self.assertEqual(detail["items"][0]["weight"], 10)

    def test_narrative_evidence_pages_share_the_visible_snapshot_and_reject_changes(self):
        answers = PeriodStore(self.store, TABLE)
        answers.prepare('2026H1', 1000)
        value = {'answer': None, 'proposed': None, 'confidence': '中', 'why': '合成时期看法'}
        votes = [{'id': f'synthetic-{index}', 'user': 'synthetic-chat', 's': 1, 'k': '自述',
                  'text': f'合成依据{index}', 'time': index} for index in range(8)]
        # An imported/legacy collection can exceed today's four-citation generation limit.
        answers.save('2026H1', 101, 0, value, votes)
        observed = worldview_view(self.store)['periods'][0]['views'][VIEWS[101]]
        self.assertEqual((len(observed['evidence']), observed['evidenceTotal']), (8, 8))
        self.assertIsNone(observed['evidenceNextOffset'], 'do not hide rows just to display a paging button')
        first = period_evidence(self.store, '2026H1', 101, 0, 3, TABLE, observed['evidenceVersion'])
        rest = period_evidence(self.store, '2026H1', 101, first['nextOffset'], 100, TABLE, first['version'])
        self.assertEqual(first['items'] + rest['items'], observed['evidence'])
        self.assertEqual((first['total'], rest['nextOffset']), (8, None))
        votes[0]['text'] = '合成依据已更正'
        answers.save('2026H1', 101, 0, value, votes)
        with self.assertRaises(SelfError) as changed:
            period_evidence(self.store, '2026H1', 101, 3, 3, TABLE, first['version'])
        self.assertEqual(changed.exception.code, 'stale-evidence')

    def test_narrative_evidence_accepts_only_known_integer_items(self):
        for item in VIEWS:
            self.assertEqual(period_evidence(self.store, '2026H1', item, table=TABLE)['total'], 0)
        for item in (100, 106, 0, True, '101', 101.0, 10000):
            with self.subTest(item=item), self.assertRaises(SelfError) as caught:
                period_evidence(self.store, '2026H1', item, table=TABLE)
            self.assertEqual(caught.exception.code, 'invalid-request')


class Editing(unittest.TestCase):
    def setUp(self):
        self.store = fresh_store(self)
        run(self.store, corpus())

    def entry(self):
        return worldview_view(self.store)["periods"][0]

    def test_a_shared_political_answer_recomputes_both_axes_with_original_weights(self):
        answers = PeriodStore(self.store, TABLE)
        for item in POLITICAL_ITEMS:
            answers.save("2026H1", item, 0, {"answer": 3, "proposed": 3, "confidence": "中", "why": "合成"}, [])
        save_worldview_edit(self.store, "2026H1", "item:1001", 5)
        entry = self.entry()
        self.assertEqual(entry["factors"]["pol_econ"]["score"], 2.9)
        self.assertEqual(entry["factors"]["pol_lib"]["score"], 3.03)
        self.assertEqual(entry["ai"]["score:pol_econ"], 3)

    def test_legacy_questionnaire_and_edits_stay_separate(self):
        legacy = PeriodStore(self.store, "worldview")
        legacy.prepare("2025H1", 500)
        legacy.save("2025H1", 1, 0, {"answer": 5, "proposed": 5, "confidence": "中", "why": "旧题"}, [])
        with self.store.lock, self.store.db:
            self.store._meta("worldviewEdits", {"2026H1": {"score:pol_econ": 1, "item:1": 1, "score:moral_bind": 5}})
        result = worldview_view(self.store)
        self.assertNotIn("2025H1", [period["id"] for period in result["periods"]])
        self.assertEqual(self.entry()["edited"], [])
        self.assertEqual(legacy.answers()[0]["answer"], 5)
        self.assertEqual(self.store.meta("worldviewEdits", {})["2026H1"]["score:pol_econ"], 1)
        self.assertNotIn("moral_bind", self.entry()["factors"])

    def test_the_users_text_shows_and_the_models_stays(self):
        save_worldview_edit(self.store, "2026H1", "view:plan", "我其实还没想好")
        entry = self.entry()
        self.assertEqual(entry["views"]["plan"]["text"], "我其实还没想好")
        self.assertEqual(entry["ai"]["view:plan"], "这段时期想先把工作定下来")
        self.assertEqual(entry["edited"], ["view:plan"])
        save_worldview_edit(self.store, "2026H1", "view:plan", None)
        self.assertEqual(self.entry()["views"]["plan"]["text"], "这段时期想先把工作定下来")

    def test_a_changed_score_moves_the_quadrant_unless_the_user_set_it(self):
        save_worldview_edit(self.store, "2026H1", "score:pol_econ", 5)
        save_worldview_edit(self.store, "2026H1", "score:pol_lib", 5)
        self.assertEqual(self.entry()["letters"], ["保守主义"])
        save_worldview_edit(self.store, "2026H1", "type", "社会主义")
        save_worldview_edit(self.store, "2026H1", "score:val_change", 5)
        save_worldview_edit(self.store, "2026H1", "score:val_self", 5)
        entry = self.entry()
        self.assertEqual(entry["letters"], ["社会主义"])
        self.assertEqual(entry["style"], "守护派")
        save_worldview_edit(self.store, "2026H1", "style", "务实派")
        entry = self.entry()
        self.assertEqual(entry["style"], "务实派")
        self.assertEqual(entry["ai"]["style"], "", "the model had too little to place it")

    def test_only_real_fields_and_values_are_saved(self):
        for period, field, value in (("2026H3", "type", "保守主义"), ("2026H1", "type", "左派"), ("2026H1", "style", "ENFJ"),
                                     ("2026H1", "view:other", "x"), ("2026H1", "view:plan", "长" * 121), ("2026H1", "report", "INFJ"),
                                     ("2026H1", "score:big5_e", 3), ("2026H1", "item:101", 3),
                                     ("2026H1", "score:moral_bind", 3), ("2026H1", "item:25", 3), ("2026H1", "item:1", 3)):
            with self.assertRaises(SelfError):
                save_worldview_edit(self.store, period, field, value)


if __name__ == "__main__":
    unittest.main()
