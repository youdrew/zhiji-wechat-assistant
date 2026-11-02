"""Measured ranking costs and deployment summaries for the frozen experiment.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations
import argparse
import importlib.util
import json
from pathlib import Path
import random
import time

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location("chat_benchmark_evaluate", HERE / "evaluate.py")
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def describe(values):
    values = np.asarray(values, dtype=float)
    return {"count": len(values), "p50": float(np.quantile(values, .5)), "p95": float(np.quantile(values, .95)),
            "mean": float(values.mean()), "min": float(values.min()), "max": float(values.max())}


def ranking_times(data, bundles, seed):
    ranker = evaluation.ProductRanker(data["candidates"])
    rows = []
    try:
        prepared = {}
        for model, bundle in bundles.items():
            positions = {identifier: i for i, identifier in enumerate(bundle["queryIds"])}
            prepared[model] = positions
        # Warm the production SQLite schema and Python code outside timings.
        ranker.rank(data["queries"][0], "lexical")
        for repeat in range(3):
            indices = list(range(len(data["queries"])))
            random.Random(seed + repeat).shuffle(indices)
            for index in indices:
                query = data["queries"][index]
                started = time.perf_counter()
                ranker.rank(query, "lexical")
                rows.append({"run": "lexical", "queryId": query["id"], "repeat": repeat,
                             "rankingMs": (time.perf_counter() - started) * 1000})
                for model, bundle in bundles.items():
                    vector = bundle["queries"][prepared[model][query["id"]]]
                    for mode in ("semantic", "hybrid"):
                        started = time.perf_counter()
                        cosine = bundle["passages"] @ vector
                        similarities = dict(zip(bundle["candidateIds"], cosine))
                        ranker.rank(query, mode, similarities)
                        rows.append({"run": model + "/" + mode, "queryId": query["id"], "repeat": repeat,
                                     "rankingMs": (time.perf_counter() - started) * 1000})
        return rows
    finally:
        ranker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / ".local/benchmarks/self-analysis/2026-10-18-v1")
    args = parser.parse_args()
    directory = args.directory.resolve()
    data = evaluation.prepare_dataset(json.loads((HERE / "dataset.json").read_text(encoding="utf-8")), "raw")
    evaluation.validate_dataset(data)
    data_sha = evaluation.sha256(HERE / "dataset.json")
    performance = json.loads((directory / "performance.json").read_text(encoding="utf-8"))
    if performance["datasetSha256"] != data_sha:
        raise ValueError("performance belongs to a different dataset")
    protocol = json.loads((HERE / "protocol.json").read_text(encoding="utf-8"))
    bundles = {model: evaluation.load_vectors(directory / f"{model}.npz", data, data_sha) for model in evaluation.MODELS}
    rows = ranking_times(data, bundles, protocol["seed"])
    timings = {(row["model"], row["run"], query["id"]): query for row in performance["runs"] for query in row["queries"]}
    by_query = {query["id"]: query for query in data["queries"]}
    for row in rows:
        row["split"] = by_query[row["queryId"]]["split"]
        row["category"] = by_query[row["queryId"]].get("category")
        if row["run"] == "lexical":
            row["encoderIpcMs"], row["encoderInternalMs"] = 0., 0.
        else:
            model = row["run"].split("/")[0]
            encoded = timings[(model, row["repeat"], row["queryId"])]
            row["encoderIpcMs"], row["encoderInternalMs"] = encoded["wallMs"], encoded["encoderMs"]
        row["combinedMs"] = row["encoderIpcMs"] + row["rankingMs"]
    summary = []
    for split in ("all", "dev", "test"):
        for name in sorted({row["run"] for row in rows}):
            chosen = [row for row in rows if row["run"] == name and (split == "all" or row["split"] == split)]
            summary.append({"split": split, "run": name,
                **{key: describe([row[key] for row in chosen]) for key in ("encoderIpcMs", "encoderInternalMs", "rankingMs", "combinedMs")}})
    resource_rows = []
    manifest = json.loads((HERE / "models.json").read_text(encoding="utf-8"))
    for model in evaluation.MODELS:
        selected = [row for row in performance["runs"] if row["model"] == model]
        definition = next(row for row in manifest["models"] if row["key"] == model)
        resource_rows.append({"model": model, "dimensions": definition["dimensions"],
            "assetBytes": sum(row["bytes"] for row in definition["files"]),
            "processColdLoadMs": describe([row["loadWallMs"] for row in selected]),
            "corpusEncodeMs": describe([row["corpusWallMs"] for row in selected]),
            "corpusSamplesPerSecond": describe([len(data["candidates"]) * 1000 / row["corpusWallMs"] for row in selected]),
            "peakRssBytes": max(row["peakRssBytes"] for row in selected),
            "sampledPeakPrivateBytes": max(row["peakPrivateBytes"] for row in selected),
            "peakRssBytesByRun": [row["peakRssBytes"] for row in selected],
            "corpusTruncatedPerRun": [sum(batch["truncatedCount"] for batch in row["corpusBatches"]) for row in selected],
            "queryTruncatedPerRun": [sum(query["truncatedCount"] for query in row["queries"]) for row in selected],
            "queryTokens": describe([query["tokenCount"] for row in selected for query in row["queries"]]),
            "vectorDriftAcrossRuns": max(row.get("maxAbsVectorDrift", 0) for row in selected)})
    result = {"datasetSha256": data_sha, "hardware": performance["hardware"],
              "combinedLatencyDefinition": "Per-query sum of independently measured encoder IPC and ranking cost; an estimate of those components, not an observed end-to-end application request.",
              "latencyScope": protocol["performanceScope"], "summary": summary, "resources": resource_rows, "perQuery": rows}
    path = directory / "latency-and-resources.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(path), "latencyRows": len(rows), "resources": resource_rows}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
