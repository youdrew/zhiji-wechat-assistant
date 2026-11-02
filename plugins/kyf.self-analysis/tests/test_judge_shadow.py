"""Shadow judging does not change extraction, cache hits or cancellation; synthetic data only."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend"),
                str(Path(__file__).parent)]

from kyf_plugin import Cancelled, ModelError
from self_evidence import EvidenceStore, SharedEvidence
from self_judge_shadow import JudgeShadow, drain_shadow, record_extraction
from self_store import Store
from test_evidence import Models
from test_profile_corpus import Context
from test_tag_corpus import Corpus, NoHistory, own
from test_timeline import (_CorpusStub, _FakeModels, _JobContext, _corpus_row, TIMELINE_WINDOW,
                           TimelineExtract, TimelineStore, model_event, parse_reply)


class JudgeModels(Models):
    def __init__(self):
        super().__init__()
        self.judgments, self.judge_hook = [], None
        self.judge_service = {"roles": ["judge"], "fingerprint": "judge-fp", "stamp": "judge-stamp"}
        self.engine, self.probabilities = "systemone", [.1]

    def service_for(self, role, refresh=False):
        return self.judge_service

    def judge(self, context, state, questions, step=None):
        self.judgments.append({"state": state, "questions": copy.deepcopy(questions), "step": step})
        if self.judge_hook:
            self.judge_hook(context)
        return {"answers": {"has_content": {"noul": self.probabilities.pop(0) if len(self.probabilities) > 1
                                             else self.probabilities[0]}},
                "engine": self.engine, "fingerprint": self.judge_service["fingerprint"]}


class ShadowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name), "synthetic-shadow")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.models = JudgeModels()
        self.plugin = SimpleNamespace(core=NoHistory(), models=self.models)
        self.corpus = Corpus([own(1, "我喜欢篮球"), own(2, "我喜欢先独处整理思路")])
        self.context = Context()
        self.shadow = JudgeShadow(self.store)

    def summary(self):
        return self.shadow.summary(self.corpus.meta("source"), self.corpus.meta("revision", 0))

    def extract(self):
        return SharedEvidence(self.plugin, self.store, "synthetic-shadow", self.corpus).work(self.context)

    def enqueue(self, state="合成窗口", positive=True, kind="evidence", identifier="window-1"):
        self.shadow.enqueue(source=self.corpus.meta("source"), revision=self.corpus.meta("revision", 0),
                            kind=kind, fingerprints=[identifier], state=state, main_positive=positive,
                            service=self.models.judge_service)

    def test_successful_extraction_finishes_before_shadow_and_all_cache_identities_survive(self):
        self.context.defer_judge_shadow = True
        first = self.extract()
        records = list(EvidenceStore(self.store).records())
        self.assertEqual(len(records), 2)
        self.assertTrue(all(row["profile"] for row in records))
        self.assertEqual(self.models.judgments, [])
        self.assertEqual(self.summary()["counts"]["pending"], 1)
        self.models.judge_hook = lambda _: self.assertEqual(list(EvidenceStore(self.store).records()), records)
        drain_shadow(self.plugin, self.store, self.corpus, self.context)
        self.assertEqual(list(EvidenceStore(self.store).records()), records)
        summary = self.summary()
        self.assertEqual(summary["counts"], {"total": 1, "done": 1, "pending": 0, "error": 0})
        self.assertIsNone(summary["threshold"])
        self.assertEqual(summary["engines"][0]["falseNegativeWindows"], 1)
        self.assertEqual(summary["engines"][0]["positiveWindows"], 1)
        calls = len(self.models.requests)
        second = self.extract()
        self.assertEqual(first["identity"], second["identity"])
        self.assertEqual(len(self.models.requests), calls)
        self.assertEqual(second["coverage"]["newlyAnalyzedMessages"], 0)
        self.assertEqual(self.summary()["counts"]["total"], 1)

    def test_judge_following_main_skips_optional_judgments(self):
        self.models.judge_service["roles"] = ["main", "judge"]
        self.extract()
        self.assertTrue(list(EvidenceStore(self.store).records()))
        self.assertEqual(self.models.judgments, [])
        self.assertEqual(self.summary()["counts"]["total"], 0)

    def test_judge_failure_is_safe_and_extraction_remains_successful(self):
        def fail(_):
            raise ModelError("judge-unavailable", "PRIVATE_PROVIDER_BODY")
        self.models.judge_hook = fail
        result = self.extract()
        self.assertEqual(result["coverage"]["analyzedMessages"], 2)
        self.assertEqual(self.summary()["counts"]["error"], 1)
        row = self.store.rows("SELECT * FROM judge_shadow")[0]
        self.assertEqual(row["error"], "judge-unavailable")
        self.assertNotIn("PRIVATE_PROVIDER_BODY", str(row))

    def test_cancellation_propagates_without_committing_a_late_judgment(self):
        self.enqueue()
        def cancel(context):
            context.cancelled.set()
        self.models.judge_hook = cancel
        with self.assertRaises(Cancelled):
            drain_shadow(self.plugin, self.store, self.corpus, self.context)
        self.assertEqual(self.summary()["counts"]["pending"], 1)

    def test_source_and_model_changes_discard_late_results(self):
        for change in (lambda: self.corpus.metadata.update(source="different-source"),
                       lambda: self.corpus.metadata.update(revision=2),
                       lambda: self.models.judge_service.update(fingerprint="different-judge"),
                       lambda: self.models.judge_service.update(stamp="different-stamp")):
            with self.subTest(change=change):
                self.corpus.metadata.update(source="shared-synthetic-source", revision=1)
                self.models.judge_service.update(fingerprint="judge-fp", stamp="judge-stamp")
                self.enqueue()
                self.models.judge_hook = lambda context: change()
                drain_shadow(self.plugin, self.store, self.corpus, self.context)
                self.assertEqual(self.store.rows("SELECT status FROM judge_shadow")[0]["status"], "pending")

    def test_long_state_is_split_and_only_whole_windows_enter_the_denominator(self):
        self.enqueue("合" * 12001)
        self.models.probabilities = [.1, .8, .2]
        drain_shadow(self.plugin, self.store, self.corpus, self.context)
        self.assertEqual([len(call["state"]) for call in self.models.judgments], [6000, 6000, 1])
        row = self.store.rows("SELECT chunks,probability FROM judge_shadow")[0]
        self.assertEqual(row, {"chunks": 3, "probability": .8})
        group = self.summary()["engines"][0]
        self.assertEqual((group["windows"], group["positiveWindows"], group["falseNegativeWindows"]), (1, 1, 0))

    def test_different_engines_and_kinds_are_never_pooled(self):
        self.enqueue(identifier="systemone-positive")
        drain_shadow(self.plugin, self.store, self.corpus, self.context)
        self.models.engine = "generate"
        self.enqueue(identifier="generate-positive")
        self.enqueue(identifier="generate-empty", positive=False)
        self.enqueue(identifier="timeline-positive", kind="timeline")
        drain_shadow(self.plugin, self.store, self.corpus, self.context)
        groups = self.summary()["engines"]
        self.assertEqual(len(groups), 3)
        group = next(row for row in groups if row["engine"] == "generate" and row["kind"] == "evidence")
        self.assertEqual((group["windows"], group["positiveWindows"], group["falseNegativeWindows"]), (2, 1, 1))

    def test_context_rejection_halves_chunks_without_dropping_any_state(self):
        self.enqueue("合" * 3000)
        def context_limit(_):
            if len(self.models.judgments[-1]["state"]) > 1500:
                raise ModelError("judge-state-too-long", "synthetic context limit")
        self.models.judge_hook = context_limit
        drain_shadow(self.plugin, self.store, self.corpus, self.context)
        self.assertEqual([len(call["state"]) for call in self.models.judgments], [3000, 1500, 1500])
        self.assertEqual(self.store.rows("SELECT chunks,status FROM judge_shadow")[0], {"chunks": 2, "status": "done"})

    def test_invalid_probability_is_not_counted_as_a_false_negative(self):
        self.models.probabilities = [float("nan")]
        self.enqueue()
        drain_shadow(self.plugin, self.store, self.corpus, self.context)
        self.assertEqual(self.summary()["counts"]["error"], 1)
        self.assertEqual(self.summary()["engines"], [])

    def test_key_rotation_does_not_count_the_same_window_twice(self):
        self.enqueue()
        drain_shadow(self.plugin, self.store, self.corpus, self.context)
        self.models.judge_service["stamp"] = "rotated-key-stamp"
        self.models.probabilities = [.9]
        self.enqueue()
        drain_shadow(self.plugin, self.store, self.corpus, self.context)
        self.assertEqual(len(self.store.rows("SELECT * FROM judge_shadow")), 2)
        summary = self.summary()
        self.assertEqual(summary["counts"]["total"], 1)
        self.assertEqual(summary["engines"][0]["falseNegativeWindows"], 0)

    def test_temporary_judge_failure_can_be_retried_without_reextracting(self):
        self.models.probabilities = [float("nan")]
        self.extract()
        self.assertEqual(self.summary()["counts"]["error"], 1)
        calls = len(self.models.requests)
        self.models.probabilities = [.9]
        self.extract()
        self.assertEqual(self.summary()["counts"]["done"], 1)
        self.assertEqual(len(self.models.requests), calls)

    def test_reset_clears_shadow_data_and_stale_extraction_cannot_enqueue(self):
        self.enqueue()
        self.store.reset()
        self.assertEqual(self.store.rows("SELECT * FROM judge_shadow"), [])
        record_extraction(self.plugin, self.store, self.corpus, self.context, "evidence", [], True,
                          "old-source", 0)
        self.assertEqual(self.store.rows("SELECT * FROM judge_shadow"), [])

    def test_standalone_timeline_queues_its_final_events_after_saving_them(self):
        corpus = _CorpusStub([_corpus_row()])
        models = _FakeModels({TIMELINE_WINDOW: parse_reply(model_event(0))})
        models.service_for = self.models.service_for
        calls = []
        def judge(context, state, questions, step=None):
            self.assertEqual(TimelineStore(self.store).active_counts()["done"], 1)
            calls.append(questions)
            return {"answers": {"has_content": {"noul": .9}}, "engine": "systemone", "fingerprint": "judge-fp"}
        models.judge = judge
        result = TimelineExtract(SimpleNamespace(models=models), self.store, "synthetic-shadow", corpus, {}, "basic").work(_JobContext())
        self.assertEqual(result["windows"], 1)
        self.assertEqual(len(calls), 1)
        summary = self.shadow.summary(corpus.meta("source"), 0)
        self.assertEqual(summary["engines"][0]["kind"], "timeline")
        self.assertEqual(summary["engines"][0]["positiveWindows"], 1)


if __name__ == "__main__":
    unittest.main()
