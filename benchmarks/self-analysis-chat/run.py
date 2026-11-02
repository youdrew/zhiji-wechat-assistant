"""Sequential, isolated-process CPU measurements on frozen synthetic chat data.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
No model download or private configuration is performed by this runner.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import numpy as np
import psutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from core_runtime import base_environment, node_executable


def query_representations():
    spec = importlib.util.spec_from_file_location("chat_benchmark_query_format", HERE / "evaluate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.raw_query_text


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def hardware():
    import platform
    name = platform.processor()
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            name = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    return {"cpu": name, "physicalCores": psutil.cpu_count(logical=False),
            "logicalCpus": psutil.cpu_count(), "ramBytes": psutil.virtual_memory().total,
            "availableRamBytes": psutil.virtual_memory().available, "os": platform.platform(),
            "python": platform.python_version(), "numpy": np.__version__, "psutil": psutil.__version__,
            "architecture": platform.machine(), "cpuPercentAtStart": psutil.cpu_percent(interval=1)}


class EncoderProcess:
    def __init__(self, node):
        environment = base_environment(OMP_NUM_THREADS=2, MKL_NUM_THREADS=2, OPENBLAS_NUM_THREADS=2,
                                       ORT_DISABLE_TELEMETRY=1)
        self.process = subprocess.Popen([node, str(HERE / "encoder.cjs")], cwd=ROOT, env=environment,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.responses = queue.Queue()
        self.pid = psutil.Process(self.process.pid)
        self.sequence = 0
        self.peak_rss = 0
        self.peak_private = 0
        self.stop = threading.Event()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.monitor = threading.Thread(target=self._monitor, daemon=True)
        self.reader.start()
        self.monitor.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.responses.put(json.loads(line))
                except ValueError:
                    self.responses.put({"protocolError": "non-json stdout"})
        finally:
            self.responses.put({"protocolError": "encoder exited"})

    def sample(self):
        try:
            info = self.pid.memory_info()
            peak = max(info.rss, getattr(info, "peak_wset", 0))
            self.peak_rss = max(self.peak_rss, peak)
            self.peak_private = max(self.peak_private, getattr(info, "private", info.vms))
            return {"rssBytes": info.rss, "osPeakRssBytes": peak,
                    "privateBytes": getattr(info, "private", info.vms)}
        except psutil.Error:
            return {}

    def _monitor(self):
        while not self.stop.wait(.01):
            self.sample()

    def request(self, op, timeout=300, **fields):
        self.sequence += 1
        request = {"id": str(self.sequence), "op": op, **fields}
        started = time.perf_counter()
        self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
        self.process.stdin.flush()
        try:
            value = self.responses.get(timeout=timeout)
        except queue.Empty:
            raise RuntimeError(f"encoder {op} timed out") from None
        elapsed = (time.perf_counter() - started) * 1000
        if value.get("id") != request["id"] or value.get("error") or "result" not in value:
            raise RuntimeError(f"encoder {op} failed: {str(value)[:500]}")
        result = value["result"]
        if isinstance(result, dict) and result.get("error"):
            raise RuntimeError(f"encoder {op} failed: {str(result['error'])[:500]}")
        return result, elapsed

    def close(self):
        if self.process.poll() is None:
            try:
                self.request("close", timeout=10)
                self.process.wait(timeout=10)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=10)
        self.sample()
        self.stop.set()
        self.monitor.join(1)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()


def check_vectors(value, count, dimensions):
    vectors = np.asarray(value["vectors"], dtype=np.float32)
    if vectors.shape != (count, dimensions) or not np.isfinite(vectors).all():
        raise ValueError("invalid vectors")
    if not np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-4):
        raise ValueError("vectors must be L2-normalized")
    return vectors


def measure_model(node, model, run, data, protocol, output, data_hash):
    cpu_before = psutil.cpu_percent(interval=.5)
    worker = EncoderProcess(node)
    record = {"model": model, "run": run, "cpuPercentBefore": cpu_before, "queries": [], "corpusBatches": []}
    raw_query_text = query_representations()
    try:
        started = time.perf_counter()
        loaded, load_ms = worker.request("load", model=model, options={"root": str(ROOT),
            "maxTokens": protocol["maxTokens"], "batchSize": protocol["corpusBatchSize"], "threads": protocol["threads"]})
        dimensions = int(loaded["dimensions"])
        record.update(load=loaded, loadWallMs=load_ms, memoryAfterLoad=worker.sample())
        worker.request("encode", texts=["对方：最近连续加班，脑子像一团浆糊。\n本人：你想聊聊吗？\n对方：嗯，先听我说就好。"], kind="query", batchSize=protocol["queryBatchSize"])
        worker.request("encode", texts=["对方：这周有点忙，咱们换个时间再约。"], kind="passage")
        record["memoryAfterWarmup"] = worker.sample()
        corpus = data["candidates"]
        encoded = []
        batch_size = protocol["corpusBatchSize"]
        corpus_started = time.perf_counter()
        for begin in range(0, len(corpus), batch_size):
            rows = corpus[begin:begin + batch_size]
            value, wall_ms = worker.request("encode", texts=[row["text"] for row in rows], kind="passage", batchSize=batch_size)
            encoded.append(check_vectors(value, len(rows), dimensions))
            record["corpusBatches"].append({"start": begin, "count": len(rows), "wallMs": wall_ms,
                "encoderMs": value.get("elapsedMs"), "tokenCounts": value.get("tokenCounts"),
                "truncatedCount": value.get("truncatedCount", 0)})
            if begin % (batch_size * 12) == 0:
                print(json.dumps({"model": model, "run": run, "phase": "corpus", "done": begin + len(rows), "total": len(corpus)}), flush=True)
        record["corpusWallMs"] = (time.perf_counter() - corpus_started) * 1000
        record["memoryAfterCorpus"] = worker.sample()
        candidates = np.concatenate(encoded, axis=0)
        indices = list(range(len(data["queries"])))
        random.Random(protocol["seed"] + run).shuffle(indices)
        queries = np.zeros((len(indices), dimensions), dtype=np.float32)
        for number, index in enumerate(indices):
            row = data["queries"][index]
            value, wall_ms = worker.request("encode", texts=[raw_query_text(row["text"])], kind="query", batchSize=protocol["queryBatchSize"], timeout=120)
            queries[index] = check_vectors(value, 1, dimensions)[0]
            record["queries"].append({"id": row["id"], "wallMs": wall_ms, "encoderMs": value.get("elapsedMs"),
                "tokenCount": value.get("tokenCounts", [None])[0], "truncatedCount": value.get("truncatedCount", 0)})
            if number % 20 == 0:
                print(json.dumps({"model": model, "run": run, "phase": "queries", "done": number + 1, "total": len(indices)}), flush=True)
        record.update(memoryAfterQueries=worker.sample(), peakRssBytes=worker.peak_rss,
                      peakPrivateBytes=worker.peak_private, totalWallMs=(time.perf_counter() - started) * 1000)
        if run == 0:
            metadata = {"modelId": loaded.get("modelId", model), "revision": loaded.get("revision"),
                "dimensions": dimensions, "datasetSha256": data_hash, "precision": "FP32", "runtime": loaded,
                "queryRepresentation": "raw"}
            np.savez_compressed(output / f"{model}.npz", candidate_ids=np.array([row["id"] for row in corpus]),
                query_ids=np.array([row["id"] for row in data["queries"]]), candidate_vectors=candidates,
                query_vectors=queries, metadata=np.array(json.dumps(metadata, ensure_ascii=False)))
            role_vectors = np.zeros_like(queries)
            role_measurements = []
            for number, index in enumerate(indices):
                row = data["queries"][index]
                value, wall_ms = worker.request("encode", texts=[row["text"]], kind="query",
                    batchSize=protocol["queryBatchSize"], timeout=120)
                role_vectors[index] = check_vectors(value, 1, dimensions)[0]
                role_measurements.append({"id": row["id"], "wallMs": wall_ms, "encoderMs": value.get("elapsedMs"),
                    "tokenCount": value.get("tokenCounts", [None])[0], "truncatedCount": value.get("truncatedCount", 0)})
                if number % 40 == 0:
                    print(json.dumps({"model": model, "run": run, "phase": "queries-role-explicit", "done": number + 1,
                                      "total": len(indices)}), flush=True)
            record["roleExplicitAblationQueries"] = role_measurements
            np.savez_compressed(output / f"{model}-role-explicit.npz", candidate_ids=np.array([row["id"] for row in corpus]),
                query_ids=np.array([row["id"] for row in data["queries"]]), candidate_vectors=candidates,
                query_vectors=role_vectors, metadata=np.array(json.dumps({**metadata, "queryRepresentation": "role-explicit"}, ensure_ascii=False)))
        else:
            with np.load(output / f"{model}.npz", allow_pickle=False) as previous:
                record["maxAbsVectorDrift"] = max(float(np.max(np.abs(previous["candidate_vectors"] - candidates))),
                                                  float(np.max(np.abs(previous["query_vectors"] - queries))))
        return record
    finally:
        worker.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=HERE / "dataset.json")
    parser.add_argument("--lock", type=Path, default=HERE / "dataset-lock.json")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/benchmarks/self-analysis/2026-10-18-v1")
    parser.add_argument("--node", default=node_executable(ROOT))
    args = parser.parse_args()
    protocol_path = HERE / "protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    data = json.loads(args.dataset.read_text(encoding="utf-8"))
    lock = json.loads(args.lock.read_text(encoding="utf-8"))
    data_hash = sha256(args.dataset)
    if lock["datasetSha256"] != data_hash or lock["protocolSha256"] != sha256(protocol_path):
        raise ValueError("dataset/protocol differs from the pre-results freeze")
    for key, filename in (("encoderSha256", "encoder.cjs"), ("modelsManifestSha256", "models.json"),
                          ("evaluatorSha256", "evaluate.py"), ("runnerSha256", "run.py")):
        if lock.get(key) and lock[key] != sha256(HERE / filename):
            raise ValueError(f"{filename} differs from the pre-results freeze")
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / ".local").resolve()):
        raise ValueError("raw benchmark outputs must remain in workspace .local")
    output.mkdir(parents=True, exist_ok=True)
    result_path = output / "performance.json"
    if result_path.exists():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if (result["datasetSha256"] != data_hash or result["protocolSha256"] != sha256(protocol_path) or
                result["modelsManifestSha256"] != sha256(HERE / "models.json")):
            raise ValueError("cannot resume a different frozen experiment")
    else:
        result = {"benchmarkId": protocol["benchmarkId"], "datasetSha256": data_hash,
            "protocolSha256": sha256(protocol_path), "modelsManifestSha256": sha256(HERE / "models.json"),
            "startedAtUtc": datetime.now(timezone.utc).isoformat(), "hardware": hardware(), "runs": []}
        save_json(result_path, result)
    completed = {(row["model"], row["run"]) for row in result["runs"]}
    for run, order in enumerate(protocol["modelRunOrder"]):
        for model in order:
            if (model, run) in completed:
                continue
            record = measure_model(args.node, model, run, data, protocol, output, data_hash)
            result["runs"].append(record)
            save_json(result_path, result)
            print(json.dumps({"model": model, "run": run, "phase": "complete", "peakRssMiB": round(record["peakRssBytes"] / 2**20, 1)}), flush=True)
    result["completedAtUtc"] = datetime.now(timezone.utc).isoformat()
    save_json(result_path, result)
    print("BENCHMARK_ENCODING_COMPLETE", flush=True)


if __name__ == "__main__":
    main()
