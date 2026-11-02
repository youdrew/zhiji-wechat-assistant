"""Bounded SQLite vector-scan scaling, using frozen vectors repeated as load data.

This measures scanner cost, not retrieval quality on 50,000 unique chats.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations
import argparse
import heapq
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import time

import numpy as np
import psutil

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(ROOT / "plugins/kyf.self-analysis/backend"), str(ROOT / "plugin-sdk/python"), str(ROOT / "bridge")]
from core_runtime import base_environment
from self_store import Store
from self_vectors import VectorStore


class DimensionedVectorStore(VectorStore):
    """Benchmark-only adaptation of the production scanner's fixed 512D guard.

    Every other join, paging and eligibility condition remains inherited.
    Product storage currently supports only 512D and is not changed here.
    """
    def __init__(self, store, dimensions):
        super().__init__(store)
        self.dimensions = dimensions

    def _valid_join(self):
        return super()._valid_join().replace("length(e.vector)=2048", f"length(e.vector)={self.dimensions * 4}")


def worker(args):
    with np.load(args.vectors, allow_pickle=False) as archive:
        passages = archive["candidate_vectors"]
        queries = archive["query_vectors"]
        metadata = json.loads(str(archive["metadata"].item()))
    dimension = passages.shape[1]
    info = {"modelId": metadata["modelId"], "revision": metadata["revision"], "dimensions": dimension}
    cache_root = (ROOT / ".local/tmp").resolve()
    temporary = tempfile.TemporaryDirectory(prefix="kyf-embedding-scan-", dir=cache_root)
    directory = Path(temporary.name).resolve()
    if not directory.is_relative_to(cache_root):
        raise ValueError("invalid temporary benchmark directory")
    store = Store(directory, "synthetic-scan-only")
    try:
        started = time.perf_counter()
        blobs = [row.astype("<f4").tobytes() for row in passages]
        with store.lock, store.db:
            store._meta("source", "synthetic-scan-source")
            for begin in range(0, args.size, 512):
                ids = list(range(begin, min(args.size, begin + 512)))
                store.db.executemany("INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                    (f"s{i:09}", f"contact{i % 100}", "synthetic-scan-source", "frozen", i + 1, i + 1,
                     1700000000000 + i, "合成负载前文", f"合成负载回复{i}", "闲聊", str(i), "[]", str(i)) for i in ids])
                store.db.executemany("INSERT INTO embeddings VALUES(?,?,?,?,?,?)", [
                    (f"s{i:09}", info["modelId"], info["revision"], dimension, str(i), blobs[i % len(blobs)]) for i in ids])
        store.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        build_ms = (time.perf_counter() - started) * 1000
        database_bytes = sum(file.stat().st_size for file in store.path.parent.glob("self-analysis.sqlite3*"))
        index = DimensionedVectorStore(store, dimension)
        choice = random.Random(20261018).sample(range(len(queries)), min(16, len(queries)))
        measurements = []
        for query_index in choice:
            vector = queries[query_index]
            after, heap, scanned = "", [], 0
            started = time.perf_counter()
            partial = False
            while True:
                rows = index.scan_page(info, "synthetic-scan-source", 1800000000000, after=after)
                if not rows:
                    break
                after = rows[-1]["id"]
                matrix = np.frombuffer(b"".join(row["vector"] for row in rows), dtype="<f4").reshape(-1, dimension)
                similarities = (matrix @ vector) / np.linalg.norm(matrix, axis=1)
                for row, score in zip(rows, similarities):
                    score = float(score)
                    if not np.isfinite(score) or score < .2:
                        continue
                    item = (min(1., score), row["id"])
                    if len(heap) < 64:
                        heapq.heappush(heap, item)
                    elif item > heap[0]:
                        heapq.heapreplace(heap, item)
                scanned += len(rows)
                if time.perf_counter() - started >= 2:
                    partial = bool(index.scan_page(info, "synthetic-scan-source", 1800000000000, after=after, size=1))
                    break
            measurements.append({"queryIndex": query_index, "elapsedMs": (time.perf_counter() - started) * 1000,
                                 "scanned": scanned, "partial": partial, "candidatesKept": len(heap)})
            if not partial and scanned != args.size:
                raise AssertionError(f"scanner silently skipped vectors: {scanned} != {args.size}")
        memory = psutil.Process().memory_info()
        print(json.dumps({"modelId": info["modelId"], "dimensions": dimension, "size": args.size,
            "sqlitePopulateMs": build_ms, "databaseBytes": database_bytes,
            "peakRssBytes": max(memory.rss, getattr(memory, "peak_wset", 0)),
            "memoryDefinition": "whole isolated Python scanner working set; includes NumPy and SQLite, excludes encoder",
            "measurements": measurements}, ensure_ascii=False), flush=True)
    finally:
        store.close()
        temporary.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--vectors", type=Path)
    parser.add_argument("--size", type=int)
    parser.add_argument("--directory", type=Path, default=ROOT / ".local/benchmarks/self-analysis/2026-10-18-v1")
    args = parser.parse_args()
    if args.worker:
        worker(args)
        return
    output = args.directory.resolve()
    if not output.is_relative_to((ROOT / ".local").resolve()):
        raise ValueError("raw benchmark outputs must remain under .local")
    rows = []
    environment = base_environment(OMP_NUM_THREADS=2, MKL_NUM_THREADS=2, OPENBLAS_NUM_THREADS=2)
    for model in ("bge-small-zh-v1.5", "bge-base-zh-v1.5", "qwen3-embedding-0.6b"):
        for size in (480, 5000, 50000):
            completed = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", "--vectors",
                str(output / f"{model}.npz"), "--size", str(size)], cwd=ROOT, env=environment,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if completed.returncode:
                raise RuntimeError(completed.stderr[-2000:])
            row = json.loads(completed.stdout)
            rows.append(row)
            (output / "scan-scaling.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"model": model, "size": size, "phase": "scan-complete"}), flush=True)


if __name__ == "__main__":
    main()
