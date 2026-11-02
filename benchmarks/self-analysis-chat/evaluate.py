"""Offline, reproducible evaluation of reply-situation retrieval.

This program only reads a synthetic, frozen dataset and precomputed vectors.
It neither downloads a model nor reads any application/account data.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "plugins/kyf.self-analysis/backend"
sys.path[:0] = [str(BACKEND), str(ROOT / "plugin-sdk/python")]
import numpy as np  # noqa: E402
import self_retrieval  # noqa: E402
from self_samples import normalize, scene, terms  # noqa: E402
from self_store import SCHEMA  # noqa: E402
from self_vectors import ensure_schema  # noqa: E402

MODELS = ("bge-small-zh-v1.5", "bge-base-zh-v1.5", "qwen3-embedding-0.6b")
MODEL_DIMENSIONS = {"bge-small-zh-v1.5": 512, "bge-base-zh-v1.5": 768, "qwen3-embedding-0.6b": 1024}
KS = (1, 4, 10)
METRICS = tuple(f"{name}@{k}" for name in ("Recall", "Hit") for k in KS) + (
    "MRR@10", "nDCG@4", "nDCG@10", "StrongHit@1", "StrongHit@4", "StrongRecall@4")
DEFAULT_ACCOUNT, DEFAULT_SOURCE = "synthetic-benchmark-account", "synthetic-benchmark-source"
REPRESENTATIONS = ("raw", "role-explicit")
ROLE_PREFIX = re.compile(r"^[ \t]*(?:本人|对方|我)[ \t]*[:：][ \t]*", re.MULTILINE)


def raw_query_text(text: str) -> str:
    """Remove only line-leading speaker prefixes, retaining body and line order.

    Production suggestion workers join message bodies without speaker labels.
    This transform does not infer or remove roles mentioned inside a message.
    """
    return ROLE_PREFIX.sub("", text)


def prepare_dataset(data: dict, representation: str = "raw") -> dict:
    """Copy frozen source data; only transform query text for the selected arm."""
    if representation not in REPRESENTATIONS:
        raise ValueError("unknown query representation: " + representation)
    prepared = copy.deepcopy(data)
    if representation == "raw":
        for query in prepared["queries"]:
            query["text"] = raw_query_text(query["text"])
    return prepared


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ranking_metrics(ranked_ids, qrels):
    """Recall is fraction of all relevant documents; Hit is any relevant result.

    All positive grades count for binary relevance. nDCG uses gains 2^grade-1
    and an unconstrained ideal ranking of all judged relevant documents.
    """
    if not qrels or any(type(grade) is not int or grade <= 0 for grade in qrels.values()):
        raise ValueError("qrels must contain positive integer grades")
    if len(ranked_ids) != len(set(ranked_ids)):
        raise ValueError("ranked document IDs must be unique")
    relevant = set(qrels)
    output = {}
    for k in KS:
        hits = len(set(ranked_ids[:k]) & relevant)
        output[f"Recall@{k}"] = hits / len(relevant)
        output[f"Hit@{k}"] = float(hits > 0)
    output["MRR@10"] = next((1 / (rank + 1) for rank, identifier in enumerate(ranked_ids[:10]) if identifier in relevant), 0.0)
    strong = {identifier for identifier, grade in qrels.items() if grade >= 2}
    for k in (1, 4):
        output[f"StrongHit@{k}"] = float(bool(set(ranked_ids[:k]) & strong))
    output["StrongRecall@4"] = len(set(ranked_ids[:4]) & strong) / len(strong) if strong else None
    for k in (4, 10):
        dcg = sum((2 ** qrels.get(identifier, 0) - 1) / math.log2(rank + 2)
                  for rank, identifier in enumerate(ranked_ids[:k]))
        ideal = sum((2 ** grade - 1) / math.log2(rank + 2)
                    for rank, grade in enumerate(sorted(qrels.values(), reverse=True)[:k]))
        output[f"nDCG@{k}"] = dcg / ideal
    return output


def eligible(document, query, *, evidence=True):
    if (document.get("accountId", DEFAULT_ACCOUNT) != query.get("accountId", DEFAULT_ACCOUNT) or
            document.get("sourceFingerprint", DEFAULT_SOURCE) != query.get("sourceFingerprint", DEFAULT_SOURCE) or
            document.get("excluded", False) or document["timeMs"] >= query["timeMs"]):
        return False
    if document["contactId"] == query["contactId"]:
        if query.get("beforeSeq") is not None and document.get("toSeq") is not None and document["toSeq"] >= query["beforeSeq"]:
            return False
        excluded = set(query.get("excludeMessageIds", []))
        if query.get("messageId"):
            excluded.add(query["messageId"])
        if evidence and excluded.intersection(document.get("messageIds", [document["id"]])):
            return False
    return True


def feasible_relevant_count(documents):
    """Maximum positive documents after reply dedup and a capacity of two/contact.

    This is a bipartite max flow: one slot per normalized reply, two per
    contact. Labels remain unchanged even when the product constraints cap recall.
    """
    capacity = defaultdict(dict)
    source, sink = ("source",), ("sink",)
    for document in documents:
        reply, contact = ("reply", normalize(document["reply"])), ("contact", document["contactId"])
        for left, right, amount in ((source, reply, 1), (reply, contact, 1), (contact, sink, 2)):
            capacity[left][right] = amount
            capacity[right].setdefault(left, 0)
    flow = 0
    while True:
        parents, queue = {source: None}, deque([source])
        while queue and sink not in parents:
            left = queue.popleft()
            for right, amount in capacity[left].items():
                if amount and right not in parents:
                    parents[right] = left
                    queue.append(right)
        if sink not in parents:
            return flow
        node = sink
        while parents[node] is not None:
            previous = parents[node]
            capacity[previous][node] -= 1
            capacity[node][previous] += 1
            node = previous
        flow += 1


def validate_dataset(data, require_frozen=True):
    if data.get("schemaVersion") != 1 or (require_frozen and data.get("status") != "frozen"):
        raise ValueError("dataset must have schemaVersion=1 and be frozen before evaluation")
    documents = data.get("candidates")
    queries = data.get("queries")
    families = data.get("families")
    if not all(isinstance(value, list) and value for value in (documents, queries, families)):
        raise ValueError("dataset requires candidates, queries and families")
    by_doc = {document["id"]: document for document in documents}
    by_family = {family["id"]: family for family in families}
    if len(by_doc) != len(documents) or len({query["id"] for query in queries}) != len(queries) or len(by_family) != len(families):
        raise ValueError("duplicate dataset identifiers")
    for document in documents:
        if (document.get("text") != document.get("input") or not isinstance(document.get("input"), str) or
                not document["input"].strip() or not isinstance(document.get("reply"), str) or not document["reply"].strip() or
                not isinstance(document.get("contactId"), str) or type(document.get("timeMs")) is not int or
                document["familyId"] not in by_family):
            raise ValueError("invalid candidate or candidate.text differs from input: " + document["id"])
    ceilings = {}
    for query in queries:
        family = by_family.get(query["familyId"])
        if (not family or query.get("split") not in ("dev", "test") or query["split"] != family["split"] or
                not isinstance(query.get("text"), str) or not query["text"].strip() or
                not isinstance(query.get("contactId"), str) or type(query.get("timeMs")) is not int):
            raise ValueError("invalid query/split: " + query["id"])
        qrels = query.get("qrels")
        if not isinstance(qrels, dict) or not qrels or any(type(grade) is not int or grade not in (1, 2) for grade in qrels.values()):
            raise ValueError("query needs graded qrels 1/2: " + query["id"])
        if any(identifier not in by_doc for identifier in qrels):
            raise ValueError("unknown qrel document: " + query["id"])
        invalid = [identifier for identifier in qrels if not eligible(by_doc[identifier], query)]
        if invalid:
            raise ValueError("qrels violate account/source/time/exclusion constraints: " + query["id"] + " " + repr(invalid))
        hard = query.get("hardNegativeIds", [])
        if any(identifier not in by_doc or identifier in qrels for identifier in hard):
            raise ValueError("invalid hard negatives: " + query["id"])
        count = feasible_relevant_count([by_doc[identifier] for identifier in qrels])
        ceilings[query["id"]] = {"relevantCount": len(qrels), "constraintFeasibleRelevantCount": count,
                                  **{f"constraintRecallCeiling@{k}": min(k, count) / len(qrels) for k in KS}}
    return ceilings


class MemoryStore:
    """Production SQLite schema in RAM; there is no application-data directory."""
    def __init__(self, documents, account):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        ensure_schema(self.db)
        self.source = DEFAULT_SOURCE
        contacts = {}
        for document in documents:
            if document.get("accountId", DEFAULT_ACCOUNT) != account:
                continue
            user, identifier = document["contactId"], document["id"]
            contacts[user] = contacts.get(user, 0) + 1
            seq = document.get("toSeq", contacts[user])
            evidence = [{"id": value, "hash": "frozen", "seq": seq} for value in document.get("messageIds", [identifier])]
            self.db.execute("INSERT OR IGNORE INTO conversations(user,name) VALUES(?,?)", (user, user))
            self.db.execute("INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (identifier, user, document.get("sourceFingerprint", DEFAULT_SOURCE), "frozen", seq, seq,
                             document["timeMs"], document["input"], document["reply"], scene(document["input"]),
                             normalize(document["reply"]), json.dumps(evidence), "offline"))
            self.db.executemany("INSERT INTO terms VALUES(?,?)", [(token, identifier) for token in terms(document["input"])])
            if document.get("excluded"):
                self.db.execute("INSERT INTO exclusions VALUES(?,0)", (identifier,))
        self.db.commit()

    def rows(self, sql, values=()):
        return [dict(row) for row in self.db.execute(sql, values)]

    def meta(self, key, default=None):
        return self.source if key == "source" else default

    def close(self):
        self.db.close()


class FrozenRetriever(self_retrieval.Retrieval):
    def _verify_many(self, rows):
        # Original-source network checks have no analogue in a frozen synthetic
        # corpus. Eligibility is checked separately and no live source is used.
        return [True] * len(rows)


class ProductRanker:
    def __init__(self, documents):
        self.documents = {document["id"]: document for document in documents}
        self.stores = {}

    def close(self):
        for store in self.stores.values():
            store.close()

    def semantic_order(self, query, similarities):
        return sorted(((float(similarities[identifier]), identifier) for identifier, document in self.documents.items()
                       if eligible(document, query, evidence=False) and math.isfinite(float(similarities[identifier])) and
                       float(similarities[identifier]) >= 0.2), reverse=True)[:64]

    def rank(self, query, mode, similarities=None):
        if mode not in ("lexical", "semantic", "hybrid"):
            raise ValueError("unknown retrieval mode")
        semantic = self.semantic_order(query, similarities) if mode != "lexical" else []
        if mode == "semantic":
            selected, contacts, seen = [], Counter(), set()
            for score, identifier in semantic[:16]:
                document = self.documents[identifier]
                if not eligible(document, query):
                    continue
                normalized = normalize(document["reply"])
                if contacts[document["contactId"]] >= 2 or normalized in seen:
                    continue
                selected.append({"id": identifier, "score": score, "matchKinds": ["semantic"]})
                contacts[document["contactId"]] += 1
                seen.add(normalized)
                if len(selected) == 10:
                    break
            return selected
        account = query.get("accountId", DEFAULT_ACCOUNT)
        if account not in self.stores:
            self.stores[account] = MemoryStore(list(self.documents.values()), account)
        store = self.stores[account]
        store.source = query.get("sourceFingerprint", DEFAULT_SOURCE)
        params = {"query": query["text"], "beforeTimeMs": query["timeMs"], "limit": 10,
                  "sourceFingerprint": store.source}
        for field in ("beforeSeq", "messageId", "excludeMessageIds"):
            if field in query:
                params[field] = query[field]
        def recall(*_args, **_kwargs):
            return {"ids": [identifier for _, identifier in semantic], "scores": dict((identifier, score) for score, identifier in semantic),
                    "used": mode == "hybrid", "partial": False, "scanned": len(self.documents)}
        # Only the display cap is extended to ten for diagnostic @10 metrics.
        # All candidate ranking/filtering and the top-16 verification window are
        # executed by the shipped product implementation without re-copying it.
        with patch.object(self_retrieval, "recall", recall), patch.object(self_retrieval, "MAX_SOURCES", 10):
            result = FrozenRetriever(None, store, account).search(params, query["contactId"])
        return [{"id": item["id"], "score": item["score"], "matchKinds": item["matchKinds"]} for item in result]


def load_vectors(path, data, dataset_sha, representation="raw"):
    path = Path(path)
    if path.suffix.lower() == ".npz":
        with np.load(path, allow_pickle=False) as archive:
            value = {"candidateIds": archive["candidate_ids"].tolist(), "queryIds": archive["query_ids"].tolist(),
                     "candidateVectors": archive["candidate_vectors"], "queryVectors": archive["query_vectors"],
                     "metadata": json.loads(str(archive["metadata"].item()))}
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
    metadata = value.get("metadata", value)
    if metadata.get("datasetSha256") != dataset_sha:
        raise ValueError("vectors must identify the exact frozen dataset SHA-256: " + str(path))
    if representation not in REPRESENTATIONS or metadata.get("queryRepresentation") != representation:
        raise ValueError("vector queryRepresentation must match evaluation representation: " + str(path))
    document_ids, query_ids = value["candidateIds"], value["queryIds"]
    if (len(document_ids) != len(set(document_ids)) or len(query_ids) != len(set(query_ids)) or
            set(document_ids) != {document["id"] for document in data["candidates"]} or
            set(query_ids) != {query["id"] for query in data["queries"]}):
        raise ValueError("vector IDs must exactly cover the frozen corpus and queries")
    passages, queries = np.asarray(value["candidateVectors"], dtype=np.float32), np.asarray(value["queryVectors"], dtype=np.float32)
    if (passages.ndim != 2 or queries.ndim != 2 or passages.shape[0] != len(document_ids) or
            queries.shape[0] != len(query_ids) or passages.shape[1] != queries.shape[1] or passages.shape[1] == 0):
        raise ValueError("vector dimensions/counts do not match")
    if not np.isfinite(passages).all() or not np.isfinite(queries).all():
        raise ValueError("vectors contain non-finite values")
    pnorm, qnorm = np.linalg.norm(passages, axis=1), np.linalg.norm(queries, axis=1)
    if not np.isfinite(pnorm).all() or not np.isfinite(qnorm).all() or (pnorm < 1e-12).any() or (qnorm < 1e-12).any():
        raise ValueError("vectors contain zero-length vectors")
    passages, queries = passages / pnorm[:, None], queries / qnorm[:, None]
    return {"candidateIds": document_ids, "queryIds": query_ids, "passages": passages, "queries": queries,
            "metadata": metadata, "sha256": sha256(path), "path": str(path)}


def paired_bootstrap(left, right, groups, *, iterations=10000, seed=20261018):
    """Cluster bootstrap of paired query differences, weighted by query count.

    All paraphrases of a query family are resampled together. Unequal family
    sizes keep their query weights within every replicate, rather than treating
    a one-query family as equal to a six-query family.
    """
    left, right = np.asarray(left, dtype=float), np.asarray(right, dtype=float)
    if left.shape != right.shape or left.ndim != 1 or len(groups) != len(left) or not len(left):
        raise ValueError("paired bootstrap requires aligned nonempty vectors and groups")
    if iterations < 100:
        raise ValueError("bootstrap requires at least 100 iterations")
    group_indices = defaultdict(list)
    for index, group in enumerate(groups):
        group_indices[group].append(index)
    keys = sorted(group_indices)
    differences = left - right
    sums = np.asarray([differences[group_indices[key]].sum() for key in keys])
    counts = np.asarray([len(group_indices[key]) for key in keys])
    rng = np.random.default_rng(seed)
    samples = rng.integers(0, len(keys), size=(iterations, len(keys)))
    means = sums[samples].sum(axis=1) / counts[samples].sum(axis=1)
    low, high = np.quantile(means, [0.025, 0.975])
    return {"delta": float(differences.mean()), "low95": float(low), "high95": float(high),
            "groups": len(keys), "queries": len(left), "iterations": iterations,
            "caution": "few independent query families" if len(keys) < 5 else None}


def slices(queries):
    result = []
    for split in ("dev", "test"):
        in_split = [query for query in queries if query["split"] == split]
        if not in_split:
            continue
        result.append((split, "all", in_split))
        categories = sorted({query.get("category", "uncategorized") for query in in_split})
        result.extend((split, category, [query for query in in_split if query.get("category", "uncategorized") == category])
                      for category in categories)
    return result


def mean_defined(values):
    values = [value for value in values if value is not None]
    return float(np.mean(values)) if values else None


def evaluate(data, bundles, *, bootstrap_iterations=10000, seed=20261018):
    ceilings = validate_dataset(data)
    ranker = ProductRanker(data["candidates"])
    runs, per_query, floor_queries = ["lexical"], {}, []
    try:
        for query in data["queries"]:
            ranked = ranker.rank(query, "lexical")
            per_query[("lexical", query["id"])] = {"ranking": ranked, **ranking_metrics([row["id"] for row in ranked], query["qrels"])}
        for model, bundle in bundles.items():
            query_positions = {identifier: index for index, identifier in enumerate(bundle["queryIds"])}
            runs.extend(model + "/" + mode for mode in ("semantic", "hybrid"))
            for query in data["queries"]:
                vector = bundle["queries"][query_positions[query["id"]]]
                scores = bundle["passages"] @ vector
                similarities = dict(zip(bundle["candidateIds"], scores.tolist()))
                allowed = [document for document in data["candidates"] if eligible(document, query, evidence=False)]
                positives = [identifier for identifier in query["qrels"]]
                strong = [identifier for identifier, grade in query["qrels"].items() if grade == 2]
                dropped = lambda identifiers: sum(similarities[identifier] < 0.2 for identifier in identifiers)
                floor_queries.append({"model": model, "queryId": query["id"], "split": query["split"],
                                      "category": query.get("category", "uncategorized"),
                                      "eligibleCount": len(allowed), "eligibleBelowFloor": dropped(document["id"] for document in allowed),
                                      "positiveCount": len(positives), "positiveBelowFloor": dropped(positives),
                                      "strongCount": len(strong), "strongBelowFloor": dropped(strong)})
                for mode in ("semantic", "hybrid"):
                    run = model + "/" + mode
                    ranked = ranker.rank(query, mode, similarities)
                    per_query[(run, query["id"])] = {"ranking": ranked, **ranking_metrics([row["id"] for row in ranked], query["qrels"])}
    finally:
        ranker.close()
    summary, differences, floor_summary = [], [], []
    comparisons = [(run, "lexical") for run in runs[1:]] + [(model + "/hybrid", model + "/semantic") for model in bundles]
    model_keys = list(bundles)
    comparisons += [(left + "/hybrid", right + "/hybrid") for index, left in enumerate(model_keys)
                    for right in model_keys[:index]]
    for split, category, queries in slices(data["queries"]):
        for model in bundles:
            subset = [row for row in floor_queries if row["model"] == model and row["split"] == split and
                      (category == "all" or row["category"] == category)]
            totals = {field: sum(row[field] for row in subset) for field in (
                "eligibleCount", "eligibleBelowFloor", "positiveCount", "positiveBelowFloor", "strongCount", "strongBelowFloor")}
            floor_summary.append({"model": model, "split": split, "category": category, "queries": len(subset), **totals,
                                  **{kind + "BelowFloorRate": totals[kind + "BelowFloor"] / totals[kind + "Count"]
                                     if totals[kind + "Count"] else None for kind in ("eligible", "positive", "strong")}})
        for run in runs:
            summary.append({"split": split, "category": category, "run": run, "queries": len(queries),
                            "groups": len({query["familyId"] for query in queries}),
                            "strongLabeledQueries": sum(any(grade == 2 for grade in query["qrels"].values()) for query in queries),
                            "shortlistBelow4Rate": float(np.mean([len(per_query[(run, query["id"])]["ranking"]) < 4 for query in queries])),
                            **{metric: mean_defined([per_query[(run, query["id"])][metric] for query in queries]) for metric in METRICS},
                            **{f"constraintRecallCeiling@{k}": float(np.mean([ceilings[query["id"]][f"constraintRecallCeiling@{k}"] for query in queries])) for k in KS}})
        for left, right in comparisons:
            for metric in METRICS:
                paired_queries = [query for query in queries if per_query[(left, query["id"])][metric] is not None and
                                  per_query[(right, query["id"])][metric] is not None]
                if not paired_queries:
                    continue
                comparison_seed = seed + int(hashlib.sha256(f"{split}|{category}|{left}|{right}|{metric}".encode()).hexdigest()[:8], 16)
                statistics = paired_bootstrap([per_query[(left, query["id"])][metric] for query in paired_queries],
                                              [per_query[(right, query["id"])][metric] for query in paired_queries],
                                              [query["familyId"] for query in paired_queries], iterations=bootstrap_iterations, seed=comparison_seed)
                differences.append({"split": split, "category": category, "left": left, "right": right, "metric": metric, **statistics})
    details = [{"run": run, "queryId": query["id"], "familyId": query["familyId"], "split": query["split"],
                "category": query.get("category", "uncategorized"), **ceilings[query["id"]], **per_query[(run, query["id"])]}
               for run in runs for query in data["queries"]]
    return {"runs": runs, "summary": summary, "pairedDifferences": differences, "perQuery": details,
            "diagnostics": {"cosineFloor": 0.2, "floorSummary": floor_summary, "floorPerQuery": floor_queries,
                            "interpretation": "Pair-weighted rejection rates; production threshold unchanged. Differences combine encoder geometry and the fixed floor, not intrinsic model capability alone."}}


def csv_file(path, rows):
    if not rows:
        return
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(directory, result, data, manifest):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    report = {"manifest": manifest, **result}
    (directory / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    csv_file(directory / "summary.csv", result["summary"])
    csv_file(directory / "paired-differences.csv", result["pairedDifferences"])
    csv_file(directory / "cosine-floor.csv", result["diagnostics"]["floorSummary"])
    csv_file(directory / "cosine-floor-per-query.csv", result["diagnostics"]["floorPerQuery"])
    with (directory / "per-query.jsonl").open("w", encoding="utf-8") as stream:
        for row in result["perQuery"]:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    csv_file(directory / "per-query.csv", [{**{key: value for key, value in row.items() if key != "ranking"},
                                             "topIds": " ".join(item["id"] for item in row["ranking"])} for row in result["perQuery"]])
    representation = manifest.get("queryRepresentation", "raw")
    lines = ["# 本人回复情境检索基准", "", "查询表示：" + representation + "。raw 为当前线上无角色前缀的主评测；role-explicit 为预注册的输入表示消融，不参与主模型选择。",
             "主指标为 @4；@10 是同一排名规则下的诊断扩展，线上不会展示 10 条。",
             "Recall 是找回全部相关样例的比例；Hit 是是否至少找到一条相关样例。二者不能互换。",
             "StrongHit 仅将 grade=2 视为命中；StrongRecall@4 仅在存在 grade=2 标注的查询上取均值，分母数量另列。shortlistBelow4Rate 保留不足四条结果的实际比例，不补齐。",
             "nDCG 使用 2^grade−1 与未施加联系人/去重限制的理想排名；constraintRecallCeiling 单独报告产品约束造成的上限。",
             "分组 bootstrap 按 query family 配对重采样；置信区间是未作多重比较校正的探索性区间，小分组不宜下强结论。",
             "词项/hybrid 直接调用生产排序，原文网络核对在冻结合成资料中恒为真。语义全量RAM扫描；不测编码器、原文校验或应用端到端耗时。", ""]
    columns = ("run", "Recall@1", "Recall@4", "Recall@10", "Hit@1", "Hit@4", "Hit@10", "MRR@10", "nDCG@4", "nDCG@10",
               "StrongHit@1", "StrongHit@4", "StrongRecall@4", "shortlistBelow4Rate")
    for split, category, _ in slices(data["queries"]):
        lines += [f"## {split} / {category}", "", "| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
        for row in result["summary"]:
            if row["split"] == split and row["category"] == category:
                lines.append("| " + " | ".join(str(row[column]) if column == "run" else
                                               "n/a" if row[column] is None else f"{row[column]:.4f}" for column in columns) + " |")
        lines.append("")
    lines += ["## 配对差异：test / all / nDCG@4", "", "| left | right | delta | 95% CI | families |", "| --- | --- | --- | --- | --- |"]
    for row in result["pairedDifferences"]:
        if row["split"] == "test" and row["category"] == "all" and row["metric"] == "nDCG@4":
            lines.append(f"| {row['left']} | {row['right']} | {row['delta']:.4f} | [{row['low95']:.4f}, {row['high95']:.4f}] | {row['groups']} |")
    lines += ["", "## 固定 cosine ≥ 0.2 的影响", "",
              "这些是被生产阈值过滤的候选/正例配对比例。阈值不调整；不同模型得分几何不同，不能把这些差异全解释成模型本身能力。", "",
              "| split | model | eligible below floor | positive below floor | strong below floor |", "| --- | --- | --- | --- | --- |"]
    for row in result["diagnostics"]["floorSummary"]:
        if row["category"] == "all":
            rates = ["n/a" if row[kind + "BelowFloorRate"] is None else f"{row[kind + 'BelowFloorRate']:.4f}" for kind in ("eligible", "positive", "strong")]
            lines.append("| " + " | ".join([row["split"], row["model"], *rates]) + " |")
    (directory / "results.md").write_text("\n".join(lines), encoding="utf-8")
    by_query = {query["id"]: query for query in data["queries"]}
    by_doc = {document["id"]: document for document in data["candidates"]}
    errors = ["# @4 未命中或未强命中的逐查询审核", "", "这些错误用于解释固定评测，不可用 test 结果调参数后仍把它称为独立测试。", ""]
    for row in result["perQuery"]:
        query = by_query[row["queryId"]]
        has_strong = any(grade == 2 for grade in query["qrels"].values())
        if row["Hit@4"] and (row["StrongHit@4"] or not has_strong):
            continue
        errors += [f"## {row['run']} / {query['id']} ({query['split']})", "",
                   f"Hit@4={row['Hit@4']:.0f}，StrongHit@4={row['StrongHit@4']:.0f}", "", query["text"], "",
                   "标注依据：" + (query.get("annotationRationale", "") if isinstance(query.get("annotationRationale", ""), str)
                                 else json.dumps(query["annotationRationale"], ensure_ascii=False)),
                   "", "相关：" + ", ".join(query["qrels"]), ""]
        for rank, item in enumerate(row["ranking"][:10], 1):
            document = by_doc[item["id"]]
            errors += [f"{rank}. {item['id']} / grade={query['qrels'].get(item['id'], 0)} / {item['matchKinds']}",
                       "   情境：" + document["input"].replace("\n", " / "), "   本人回复：" + document["reply"].replace("\n", " / ")]
        errors.append("")
    (directory / "errors.md").write_text("\n".join(errors), encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path(__file__).with_name("dataset.json"))
    parser.add_argument("--vectors", action="append", required=True, metavar="MODEL=FILE")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261018)
    parser.add_argument("--representation", choices=REPRESENTATIONS, default="raw")
    args = parser.parse_args(argv)
    original = json.loads(args.dataset.read_text(encoding="utf-8"))
    validate_dataset(original)
    data = prepare_dataset(original, args.representation)
    paths = dict(item.split("=", 1) for item in args.vectors)
    if set(paths) != set(MODELS) or len(args.vectors) != 3:
        parser.error("provide exactly one vector file for each of: " + ", ".join(MODELS))
    dataset_sha = sha256(args.dataset)
    bundles = {model: load_vectors(paths[model], data, dataset_sha, args.representation) for model in MODELS}
    for model, bundle in bundles.items():
        if bundle["passages"].shape[1] != MODEL_DIMENSIONS[model]:
            parser.error(f"{model} requires {MODEL_DIMENSIONS[model]} dimensions")
    manifest = {"protocol": "self-analysis-chat-v1", "createdAtUtc": datetime.now(timezone.utc).isoformat(),
                "datasetId": data["datasetId"], "datasetSha256": dataset_sha, "seed": args.seed,
                "queryRepresentation": args.representation, "selectionEligible": args.representation == "raw",
                "bootstrapIterations": args.bootstrap, "primaryMetric": "test nDCG@4", "primaryK": 4, "diagnosticK": 10,
                "parameters": {"lexicalCharWeight": 0.22, "lexicalOtherWeight": 1, "lexicalSqlCandidates": 500,
                               "routeTopK": 64, "semanticMinCosine": 0.2, "rrfConstant": 60,
                               "contactCap": 2, "verificationWindow": 16, "dedup": "production normalized reply"},
                "productionFiles": {name: sha256(BACKEND / name) for name in ("self_samples.py", "self_retrieval.py", "self_semantic.py")},
                "vectors": {model: {"fileSha256": bundle["sha256"], "metadata": bundle["metadata"]} for model, bundle in bundles.items()},
                "rankingEnvironment": "in-memory SQLite lexical + RAM NumPy semantic; no inference/network/fresh verification"}
    result = evaluate(data, bundles, bootstrap_iterations=args.bootstrap, seed=args.seed)
    write_outputs(args.output, result, data, manifest)
    print(json.dumps({"output": str(args.output), "runs": len(result["runs"]), "queries": len(data["queries"]),
                      "datasetSha256": dataset_sha}, ensure_ascii=False))


if __name__ == "__main__":
    main()
