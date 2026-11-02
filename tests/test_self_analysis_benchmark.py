"""Hand-checkable metrics and product-faithful offline ranking; no model/network.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import copy
import importlib.util
import json
import io
import math
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("self_analysis_benchmark", ROOT / "benchmarks/self-analysis-chat/evaluate.py")
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)
np = benchmark.np


def document(identifier, text, *, reply=None, contact="past-friend", **extra):
    return {"id": identifier, "familyId": "family-a", "contactId": contact, "timeMs": 1000,
            "input": text, "text": text, "reply": reply or "合成本人回复" + identifier, "tags": [], **extra}


def query(identifier="query-a", text="新消息", **extra):
    return {"id": identifier, "familyId": "family-a", "split": "test", "category": "toy", "text": text,
            "contactId": "current-friend", "timeMs": 100000, "qrels": {"doc-a": 2}, "hardNegativeIds": [],
            "annotationRationale": "Synthetic hand-labelled fixture", **extra}


def dataset(documents=None, queries=None):
    return {"schemaVersion": 1, "datasetId": "metric-toy", "status": "frozen", "seed": 20261018,
            "families": [{"id": "family-a", "title": "toy", "split": "test"}],
            "candidates": documents or [document("doc-a", "历史输入")], "queries": queries or [query()]}


class MetricTests(unittest.TestCase):
    def test_multi_relevant_recall_is_not_hit(self):
        result = benchmark.ranking_metrics(["a"], {"a": 2, "b": 1})
        self.assertEqual(result["Recall@1"], 0.5)
        self.assertEqual(result["Hit@1"], 1)
        self.assertEqual(result["MRR@10"], 1)
        self.assertEqual(result["StrongHit@1"], 1)
        weak = benchmark.ranking_metrics(["b"], {"a": 2, "b": 1})
        self.assertEqual(weak["Hit@1"], 1)
        self.assertEqual(weak["StrongHit@1"], 0)
        self.assertEqual(weak["StrongRecall@4"], 0)
        self.assertIsNone(benchmark.ranking_metrics(["b"], {"b": 1})["StrongRecall@4"])

    def test_graded_dcg_and_reciprocal_rank_match_hand_computation(self):
        result = benchmark.ranking_metrics(["x", "a", "b"], {"a": 2, "b": 1})
        expected = (3 / math.log2(3) + 1 / math.log2(4)) / (3 + 1 / math.log2(3))
        self.assertAlmostEqual(result["nDCG@4"], expected)
        self.assertAlmostEqual(result["nDCG@10"], expected)
        self.assertEqual(result["MRR@10"], 0.5)
        self.assertEqual(result["Recall@4"], 1)
        self.assertEqual(result["Hit@1"], 0)

    def test_empty_ranking_and_invalid_qrels(self):
        self.assertTrue(all(value == 0 for value in benchmark.ranking_metrics([], {"a": 2}).values()))
        with self.assertRaises(ValueError):
            benchmark.ranking_metrics(["a", "a"], {"a": 2})
        with self.assertRaises(ValueError):
            benchmark.ranking_metrics([], {})

    def test_clustered_paired_bootstrap_has_known_constant_and_unequal_group_weights(self):
        fixed = benchmark.paired_bootstrap([0.75] * 4, [0.5] * 4, ["a", "a", "b", "b"], iterations=1000, seed=8)
        self.assertEqual((fixed["delta"], fixed["low95"], fixed["high95"]), (0.25, 0.25, 0.25))
        left, right, groups = [1, 1, 1, 0], [0] * 4, ["a", "a", "a", "b"]
        value = benchmark.paired_bootstrap(left, right, groups, iterations=1000, seed=9)
        self.assertEqual(value["delta"], 0.75)
        self.assertEqual((value["low95"], value["high95"]), (0, 1))
        self.assertEqual(value, benchmark.paired_bootstrap(left, right, groups, iterations=1000, seed=9))


class CorpusTests(unittest.TestCase):
    def test_query_representation_is_exact_shared_transform_without_mutating_frozen_data(self):
        explicit = "本人：我周末有事\n  对方:  那下周呢？\n我：可以问问。\n说明：本人：是正文\n我想：保留这一句"
        expected = "我周末有事\n那下周呢？\n可以问问。\n说明：本人：是正文\n我想：保留这一句"
        value = dataset(queries=[query(text=explicit)])
        raw = benchmark.prepare_dataset(value)
        self.assertEqual(benchmark.raw_query_text(explicit), expected)
        self.assertEqual(raw["queries"][0]["text"], expected)
        self.assertEqual(value["queries"][0]["text"], explicit)
        self.assertEqual(raw["candidates"], value["candidates"])
        self.assertIsNot(raw["candidates"], value["candidates"])
        self.assertEqual(benchmark.prepare_dataset(value, "role-explicit"), value)
        with self.assertRaises(ValueError):
            benchmark.prepare_dataset(value, "unknown")

    def test_invalid_future_qrel_is_a_hard_error(self):
        value = dataset([document("doc-a", "历史输入", timeMs=100000)])
        with self.assertRaisesRegex(ValueError, "constraints"):
            benchmark.validate_dataset(value)
        value = dataset()
        value["candidates"][0]["text"] += " 本人回复泄漏"
        with self.assertRaisesRegex(ValueError, "differs"):
            benchmark.validate_dataset(value)

    def test_reply_dedup_contact_cap_ceiling_uses_matching_not_naive_count(self):
        documents = [document("a", "甲", reply="同一句", contact="a"), document("b", "乙", reply="同一句", contact="b"),
                     document("c", "丙", reply="另一句", contact="a"), document("d", "丁", reply="第三句", contact="a")]
        self.assertEqual(benchmark.feasible_relevant_count(documents), 3)
        value = dataset(documents, [query(qrels={row["id"]: 1 for row in documents})])
        ceiling = benchmark.validate_dataset(value)["query-a"]
        self.assertEqual(ceiling["constraintRecallCeiling@4"], 0.75)

    def test_vector_identity_shape_zero_and_nan_are_rejected(self):
        value = dataset()
        good = {"candidateIds": ["doc-a"], "queryIds": ["query-a"], "candidateVectors": [[1.0, 0]],
                "queryVectors": [[1.0, 0]], "metadata": {"datasetSha256": "fixture", "queryRepresentation": "raw"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vectors.json"
            path.write_text(json.dumps(good), encoding="utf-8")
            loaded = benchmark.load_vectors(path, value, "fixture")
            self.assertEqual(loaded["passages"].shape, (1, 2))
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                benchmark.load_vectors(path, value, "different")
            with self.assertRaisesRegex(ValueError, "queryRepresentation"):
                benchmark.load_vectors(path, value, "fixture", "role-explicit")
            for vectors in ([[0, 0]], [[float("nan"), 0]], [[1, 0, 0]]):
                broken = {**good, "candidateVectors": vectors}
                path.write_text(json.dumps(broken), encoding="utf-8")
                with self.assertRaises(ValueError):
                    benchmark.load_vectors(path, value, "fixture")

    def test_cli_produces_seven_runs_from_precomputed_npz_without_encoder(self):
        value = dataset()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / "dataset.json"
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            digest = benchmark.sha256(path)
            arguments = ["--dataset", str(path), "--output", str(base / "results"), "--bootstrap", "100"]
            for model, dimensions in benchmark.MODEL_DIMENSIONS.items():
                vector = np.zeros((1, dimensions), dtype=np.float32)
                vector[0, 0] = 1
                vector_path = base / (model + ".npz")
                np.savez(vector_path, candidate_ids=np.asarray(["doc-a"]), query_ids=np.asarray(["query-a"]),
                         candidate_vectors=vector, query_vectors=vector,
                         metadata=np.asarray(json.dumps({"datasetSha256": digest, "modelId": model, "queryRepresentation": "raw"})))
                arguments.extend(("--vectors", model + "=" + str(vector_path)))
            with redirect_stdout(io.StringIO()):
                benchmark.main(arguments)
            report = json.loads((base / "results/results.json").read_text(encoding="utf-8"))
            self.assertEqual(len(report["runs"]), 7)
            self.assertEqual(report["manifest"]["datasetSha256"], digest)
            self.assertEqual(report["manifest"]["primaryMetric"], "test nDCG@4")
            self.assertEqual(report["manifest"]["queryRepresentation"], "raw")
            self.assertTrue(report["manifest"]["selectionEligible"])
            self.assertTrue(any(row["left"] == "bge-base-zh-v1.5/hybrid" and row["right"] == "bge-small-zh-v1.5/hybrid"
                                and row["delta"] == 0 for row in report["pairedDifferences"]))
            for model, dimensions in benchmark.MODEL_DIMENSIONS.items():
                vector = np.zeros((1, dimensions), dtype=np.float32)
                vector[0, 0] = 1
                np.savez(base / (model + ".npz"), candidate_ids=np.asarray(["doc-a"]), query_ids=np.asarray(["query-a"]),
                         candidate_vectors=vector, query_vectors=vector,
                         metadata=np.asarray(json.dumps({"datasetSha256": digest, "modelId": model, "queryRepresentation": "role-explicit"})))
            with redirect_stdout(io.StringIO()):
                benchmark.main([*arguments, "--representation", "role-explicit"])
            ablation = json.loads((base / "results/results.json").read_text(encoding="utf-8"))
            self.assertEqual(ablation["manifest"]["queryRepresentation"], "role-explicit")
            self.assertFalse(ablation["manifest"]["selectionEligible"])


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.rankers = []

    def tearDown(self):
        for ranker in self.rankers:
            ranker.close()

    def ranker(self, documents):
        ranker = benchmark.ProductRanker(documents)
        self.rankers.append(ranker)
        return ranker

    def test_production_lexical_weights_affinity_and_scene(self):
        ranker = self.ranker([document("doc-a", "对方：吃饭", contact="current-friend")])
        result = ranker.rank(query(text="吃饭"), "lexical")
        age = (100000 - 1000) / 86400000
        expected = 1 + 0.12 + 0.1 + 0.04 / (1 + math.log1p(age))
        self.assertAlmostEqual(result[0]["score"], expected, places=6)

    def test_semantic_recall_has_no_lexical_overlap_gate_and_is_not_limited_to_500(self):
        documents = [document(f"noise-{n:04}", "腹部词项" + str(n), contact="n" + str(n)) for n in range(600)]
        documents += [document("doc-a", "对方：共进晚餐")]
        ranker = self.ranker(documents)
        similarities = {row["id"]: 0.0 for row in documents}
        similarities["doc-a"] = 0.99
        value = query(text="腹中空空")
        pure = ranker.rank(value, "semantic", similarities)
        hybrid = ranker.rank(value, "hybrid", similarities)
        self.assertEqual(pure[0]["id"], "doc-a")
        self.assertTrue(any(row["id"] == "doc-a" and row["matchKinds"] == ["semantic"] for row in hybrid))

    def test_filtering_contact_cap_duplicate_reply_and_top16_window(self):
        documents = [document(f"d{n:02}", "匹配", contact="same", reply="重复" if n < 2 else "回复" + str(n)) for n in range(16)]
        documents += [document("z17", "匹配", contact="other")]
        ranker = self.ranker(documents)
        similarities = {row["id"]: 1 - n / 100 for n, row in enumerate(documents)}
        pure = ranker.rank(query(text="匹配"), "semantic", similarities)
        self.assertEqual(len(pure), 2)
        self.assertNotIn("z17", [row["id"] for row in pure])
        filtered = query(text="匹配", contactId="same", excludeMessageIds=["d00"], beforeSeq=1)
        self.assertEqual([row["id"] for row in ranker.rank(filtered, "hybrid", similarities)], ["z17"])

    def test_labels_tags_and_family_are_not_ranking_inputs(self):
        documents = [document("doc-a", "对方：吃饭"), document("doc-b", "对方：吃饭", contact="b")]
        first = self.ranker(documents).rank(query(text="吃饭"), "lexical")
        documents[0]["familyId"], documents[0]["tags"] = "leaky-label", ["gold-answer"]
        changed_query = query(text="吃饭", familyId="other-family", qrels={"doc-b": 2}, annotationRationale="must not affect search")
        self.assertEqual(first, self.ranker(documents).rank(changed_query, "lexical"))

    def test_evaluation_and_audit_artifacts_use_one_fixed_corpus(self):
        value = dataset([document("doc-a", "共进晚餐"), document("doc-b", "电脑黑屏", contact="b")], [query(text="腹中空空")])
        bundle = {"candidateIds": ["doc-a", "doc-b"], "queryIds": ["query-a"],
                  "passages": np.asarray([[1, 0], [0, 1]], dtype=np.float32), "queries": np.asarray([[1, 0]], dtype=np.float32)}
        result = benchmark.evaluate(value, {"fixture-model": bundle}, bootstrap_iterations=100)
        self.assertEqual(len(result["runs"]), 3)
        lexical = next(row for row in result["perQuery"] if row["run"] == "lexical")
        semantic = next(row for row in result["perQuery"] if row["run"] == "fixture-model/semantic")
        self.assertEqual(lexical["Hit@4"], 0)
        self.assertEqual(semantic["Hit@4"], 1)
        diagnostic = next(row for row in result["diagnostics"]["floorSummary"] if row["category"] == "all")
        self.assertEqual(diagnostic["positiveBelowFloorRate"], 0)
        self.assertEqual(diagnostic["eligibleBelowFloorRate"], 0.5)
        low = {**bundle, "queries": np.asarray([[0.1, math.sqrt(0.99)]], dtype=np.float32)}
        low_result = benchmark.evaluate(value, {"fixture-model": low}, bootstrap_iterations=100)
        floor = next(row for row in low_result["diagnostics"]["floorSummary"] if row["category"] == "all")
        self.assertEqual(floor["positiveBelowFloorRate"], 1)
        with tempfile.TemporaryDirectory() as directory:
            benchmark.write_outputs(directory, result, value, {"datasetSha256": "toy"})
            for name in ("results.json", "results.md", "summary.csv", "paired-differences.csv", "per-query.jsonl", "per-query.csv", "errors.md"):
                self.assertTrue((Path(directory) / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
