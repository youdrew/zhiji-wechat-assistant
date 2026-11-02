"""Independent, time-bounded semantic recall over all eligible SQLite vectors.

The lexical candidate set never limits this route. NumPy uses bounded blocks;
missing numerical dependencies or encoder failures leave lexical search usable.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import heapq
import threading
import time

from self_embedding import status
from self_vectors import DIMENSIONS, VectorStore, model_info, vectors

SCAN_SECONDS = 2.0
TOP_K = 64
_NP = None
_TRIED = False
_RUNTIME_LOCK = threading.Lock()
_RUNTIME_READY = threading.Event()


def numerical_runtime():
    """Never load DLLs synchronously in a status/search RPC.

    On Windows a first NumPy import can be delayed by cold DLL loading or virus
    scanning. The local library must keep answering while that happens.
    """
    global _TRIED
    with _RUNTIME_LOCK:
        if not _TRIED:
            _TRIED = True
            threading.Thread(target=_load_runtime, daemon=True, name="self-analysis-numeric-runtime").start()
    return _NP


def _load_runtime():
    global _NP
    try:
        import numpy
        _NP = numpy
    except (ImportError, OSError):
        pass
    finally:
        _RUNTIME_READY.set()


def wait_numerical_runtime(check):
    """Only background indexing waits for the optional numerical runtime."""
    numerical_runtime()
    while not _RUNTIME_READY.wait(0.1):
        check()
    check()
    return _NP


def runtime_status():
    if _NP is not None:
        return {"state": "ready", "message": "本机数值运行时已就绪"}
    if _RUNTIME_READY.is_set():
        return {"state": "error", "message": "本机数值运行时不可用，仍可使用词项检索；重启后可重试"}
    if _TRIED:
        return {"state": "loading", "message": "正在后台准备本机数值运行时，暂用词项检索"}
    return {"state": "idle", "message": "语义运行时将在后台准备，暂用词项检索"}


def recall(core, store, account, query, source, before, user=None, before_seq=None):
    result = {"ids": [], "scores": {}, "used": False, "partial": False, "scanned": 0}
    np = numerical_runtime()
    if not query.strip() or np is None:
        return result
    try:
        info = model_info(status(core, account))
        index = VectorStore(store)
        if not info or index.counts(info)["indexedSamples"] == 0:
            return result
        response = core.embed(account, [query[-2400:]], kind="query")
        encoded = vectors(response, info, 1)[0]
        if model_info(status(core, account)) != info:
            return result
        query_vector = np.asarray(encoded, dtype=np.float32)
        heap, after = [], ""
        started = time.monotonic()
        while True:
            batch = index.scan_page(info, source, before, user, before_seq, after)
            if not batch:
                break
            after = batch[-1]["id"]
            valid = [row for row in batch if len(row["vector"]) == DIMENSIONS * 4]
            if valid:
                matrix = np.frombuffer(b"".join(row["vector"] for row in valid), dtype="<f4").reshape(-1, DIMENSIONS)
                with np.errstate(invalid="ignore", over="ignore", divide="ignore"):
                    norms = np.linalg.norm(matrix, axis=1)
                    similarities = (matrix @ query_vector) / norms
                for row, similarity, norm in zip(valid, similarities, norms):
                    score = float(similarity)
                    if not np.isfinite(norm) or norm < 1e-12 or not np.isfinite(score) or score < 0.2:
                        continue
                    entry = (min(1.0, score), row["id"])
                    if len(heap) < TOP_K:
                        heapq.heappush(heap, entry)
                    elif entry > heap[0]:
                        heapq.heapreplace(heap, entry)
            result["scanned"] += len(batch)
            if time.monotonic() - started >= SCAN_SECONDS:
                # Do not silently call a truncated scan full semantic coverage.
                result["partial"] = bool(index.scan_page(info, source, before, user, before_seq, after, size=1))
                break
        ranked = sorted(heap, reverse=True)
        result.update(ids=[identifier for _, identifier in ranked], scores={identifier: score for score, identifier in ranked}, used=True)
    except Exception:
        # No source text, paths, provider errors or vectors belong in UI errors.
        return {**result, "used": False, "ids": [], "scores": {}}
    return result
