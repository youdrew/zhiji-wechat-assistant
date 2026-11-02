"""Warm retrieval working sets in owned encoder + ranking subprocesses.

This is a diagnostic of the benchmark pipeline, not full application RAM.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import random
import subprocess
import sys
import threading
import time

import numpy as np
import psutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def ranking_worker(args):
    evaluation = module("memory_evaluation", "evaluate.py")
    data = evaluation.prepare_dataset(json.loads((HERE / "dataset.json").read_text(encoding="utf-8")), "raw")
    evaluation.validate_dataset(data)
    ranker = evaluation.ProductRanker(data["candidates"])
    bundle = None
    if args.model:
        bundle = evaluation.load_vectors(args.directory / f"{args.model}.npz", data, evaluation.sha256(HERE / "dataset.json"))
    queries = {row["id"]: row for row in data["queries"]}
    try:
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("op") == "close":
                print(json.dumps({"closed": True}), flush=True)
                break
            query = queries[request["id"]]
            similarities = None
            if bundle is not None:
                vector = np.asarray(request["vector"], dtype=np.float32)
                similarities = dict(zip(bundle["candidateIds"], bundle["passages"] @ vector))
            ranked = ranker.rank(query, args.mode, similarities)
            print(json.dumps({"id": query["id"], "count": len(ranked)}), flush=True)
    finally:
        ranker.close()


class CombinedMemory:
    def __init__(self, pids):
        self.processes = [psutil.Process(pid) for pid in pids]
        self.stop = threading.Event()
        self.peak_rss = self.peak_private = 0
        self.samples = 0
        self.process_counts = []
        self.thread = threading.Thread(target=self.monitor, daemon=True)

    def sample(self):
        # Windows venv python.exe can be a launcher, not the actual worker.
        # Count each owned descendant once, including launcher/console overhead.
        tree = {}
        for root in self.processes:
            for process in [root, *root.children(recursive=True)]:
                tree[process.pid] = process
        values = [process.memory_info() for process in tree.values()]
        rss = sum(value.rss for value in values)
        private = sum(getattr(value, "private", value.vms) for value in values)
        self.peak_rss, self.peak_private = max(self.peak_rss, rss), max(self.peak_private, private)
        self.samples += 1
        self.process_counts.append(len(values))
        return {"rssBytes": rss, "privateBytes": private}

    def monitor(self):
        while not self.stop.wait(.01):
            try:
                self.sample()
            except psutil.Error:
                break

    def __enter__(self):
        self.sample()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(1)


def measure(args, model, mode):
    runner = module("memory_runner", "run.py")
    evaluation = module("memory_query_format", "evaluate.py")
    data = evaluation.prepare_dataset(json.loads((HERE / "dataset.json").read_text(encoding="utf-8")), "raw")
    indices = random.Random(20261018).sample(range(len(data["queries"])), min(16, len(data["queries"])))
    encoder = None
    worker = None
    try:
        if model:
            encoder = runner.EncoderProcess(runner.node_executable(ROOT))
            encoder.request("load", model=model, options={"root": str(ROOT), "threads": 2, "batchSize": 8, "maxTokens": 512})
            # Exercise the configured corpus batch before measuring warm queries.
            encoder.request("encode", texts=[row["text"] for row in data["candidates"][:8]], kind="passage", batchSize=8)
        command = [sys.executable, str(HERE / "memory.py"), "--worker", "--mode", mode, "--directory", str(args.directory)]
        if model:
            command.extend(["--model", model])
        worker = subprocess.Popen(command, cwd=ROOT, env=runner.base_environment(OMP_NUM_THREADS=2, MKL_NUM_THREADS=2, OPENBLAS_NUM_THREADS=2),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        def query(index):
            row = data["queries"][index]
            request = {"id": row["id"]}
            if encoder:
                value, _ = encoder.request("encode", texts=[row["text"]], kind="query", batchSize=1)
                request["vector"] = value["vectors"][0]
            worker.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            worker.stdin.flush()
            response = worker.stdout.readline()
            if not response:
                raise RuntimeError("ranking worker failed: " + worker.stderr.read()[-1200:])
            result = json.loads(response)
            if result.get("id") != row["id"]:
                raise RuntimeError("ranking worker protocol mismatch")

        query(indices[0])
        pids = [worker.pid] + ([encoder.process.pid] if encoder else [])
        with CombinedMemory(pids) as memory:
            start = memory.sample()
            started = time.perf_counter()
            for index in indices:
                query(index)
            end = memory.sample()
            elapsed = (time.perf_counter() - started) * 1000
        return {"run": f"{model}/{mode}" if model else mode, "queries": len(indices),
            "queryIds": [data["queries"][index]["id"] for index in indices],
            "warmStart": start, "warmEnd": end, "sampledPeakRssBytes": memory.peak_rss,
            "sampledPeakPrivateBytes": memory.peak_private, "memorySamples": memory.samples, "wallMs": elapsed,
            "processCountMin": min(memory.process_counts), "processCountMax": max(memory.process_counts),
            "processes": "owned Python ranking process tree, including Windows launcher/console helpers" + (" plus coexisting Node encoder process tree" if encoder else "")}
    finally:
        if worker:
            if worker.poll() is None:
                worker.stdin.write('{"op":"close"}\n')
                worker.stdin.flush()
                try:
                    worker.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait(timeout=10)
            for stream in (worker.stdin, worker.stdout, worker.stderr):
                stream.close()
        if encoder:
            encoder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / ".local/benchmarks/self-analysis/2026-10-18-v1")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--mode", choices=("lexical", "semantic", "hybrid"))
    args = parser.parse_args()
    args.directory = args.directory.resolve()
    if not args.directory.is_relative_to((ROOT / ".local").resolve()):
        raise ValueError("raw outputs must remain under .local")
    if args.worker:
        ranking_worker(args)
        return
    rows = []
    for model, mode in [(None, "lexical")] + [(model, mode) for model in (
            "bge-small-zh-v1.5", "bge-base-zh-v1.5", "qwen3-embedding-0.6b") for mode in ("semantic", "hybrid")]:
        rows.append(measure(args, model, mode))
        result = {"scope": "Sum of coexisting owned process trees' RSS/private at 10ms intervals during 16 warm queries; includes Windows launchers/console helpers, Python/NumPy, frozen synthetic data, vectors and ranking schema. Excludes coordinator, app/Core and other processes. Sampled peak is not an OS exact peak; shared pages can be counted in multiple processes.", "rows": rows}
        (args.directory / "method-memory.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"phase": "memory-complete", "run": rows[-1]["run"]}), flush=True)


if __name__ == "__main__":
    main()
