"""Background vector backfill, independently paused from Core's shared download.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import threading

from self_contracts import Stopped, now_ms
from self_vectors import BATCH_SIZE, VectorStore, model_info

# One pending scan reads the whole sample library (about a second at 70k samples),
# so it fetches this many rows and embeds them in BATCH_SIZE requests.
PENDING_SCAN = 512
RETRY_DELAYS = (1, 2, 4)
TRANSIENT_ERRORS = frozenset(("core-unavailable", "gateway-unavailable", "embedding-timeout", "embedding-unavailable"))
ERROR_CODES = TRANSIENT_ERRORS | frozenset(("embedding-not-ready", "embedding-invalid", "embedding-output",
                                           "embedding-manifest", "embedding-closed", "execution-revoked"))

UNAVAILABLE = {"state": "error", "ready": False, "modelId": None, "revision": None, "dimensions": 512,
               "totalBytes": 0, "downloadedBytes": 0, "error": "语义服务暂不可用，仍可使用词项检索"}


def status(core, account):
    try:
        if not hasattr(core, "embedding_status"):
            return dict(UNAVAILABLE)
        value = core.embedding_status(account)
        if not isinstance(value, dict):
            return dict(UNAVAILABLE)
        # Stable, bounded public metadata only; never propagate internal paths or
        # exception strings which may contain machine-specific information.
        return {key: value.get(key) for key in ("state", "ready", "modelId", "revision", "dimensions",
                                               "totalBytes", "downloadedBytes", "error")}
    except Exception:
        return dict(UNAVAILABLE)


class EmbeddingWorker:
    def __init__(self, core, store, account, history_worker):
        self.core, self.store, self.account = core, store, account
        self.history = history_worker
        self.lock = history_worker.lock
        self.cancel = threading.Event()
        self.thread = None
        self.closed = False
        if store.meta("embeddingRun", {}).get("state") in ("waiting", "indexing"):
            self._state("paused")

    def _state(self, state, error=None, **progress):
        with self.store.lock, self.store.db:
            self.store._meta("embeddingRun", {**self.store.meta("embeddingRun", {}), "state": state,
                                               "error": error, "errorCode": None, "updatedAt": now_ms(), **progress})

    def start(self):
        with self.lock:
            if self.closed or self.thread and self.thread.is_alive():
                return False
            self.cancel.clear()
            self._state("waiting", done=None, total=None)
            self.thread = threading.Thread(target=self.run, daemon=True, name="self-analysis-embedding")
            self.thread.start()
            return True

    def pause(self):
        with self.lock:
            self.cancel.set()
            self.store.samples_changed.set()  # wake a waiting backfill so it sees the pause now
            if self.store.meta("embeddingRun", {}).get("state") in ("waiting", "indexing"):
                self._state("paused")

    def check(self):
        if self.closed or self.cancel.is_set():
            raise Stopped()

    def _retry(self, action):
        """Bounded, cancellable retries for transport/runtime blips, never invalid input."""
        for attempt in range(len(RETRY_DELAYS) + 1):
            self.check()
            try:
                value = action()
                self.check()
                return value
            except Exception as exc:
                self.check()
                code = getattr(exc, "code", None)
                if not isinstance(code, str) or code not in TRANSIENT_ERRORS or attempt == len(RETRY_DELAYS):
                    raise
                if self.cancel.wait(RETRY_DELAYS[attempt]):
                    raise Stopped() from None

    def _status(self):
        # The public status helper deliberately hides transport errors. The
        # worker must preserve their codes long enough to retry safely.
        return self._retry(lambda: self.core.embedding_status(self.account))

    def close(self):
        with self.lock:
            self.closed = True
            self.pause()
        if self.thread:
            self.thread.join(3)
        return not self.thread or not self.thread.is_alive()

    def _coverage(self, vectors, info):
        """Refresh one coherent library snapshot while the caller holds store.lock."""
        self.store.samples_changed.clear()
        counts = vectors.counts(info)
        return (counts["indexedSamples"], counts["totalSamples"],
                self.store.meta("generation", 0), self.store.meta("source"))

    def _batch_indexed(self, vectors, rows, info, source):
        """Count only the current request's bounded IDs, including existing vectors."""
        identifiers = [row["id"] for row in rows]
        marks = ",".join("?" for _ in identifiers)
        values = (info["modelId"], info["revision"], info["dimensions"], source, *identifiers)
        return self.store.rows("SELECT count(*) n FROM samples s JOIN embeddings e ON " + vectors._valid_join() +
                               " WHERE s.source=? AND s.id IN (" + marks + ") AND NOT EXISTS "
                               "(SELECT 1 FROM exclusions WHERE sample_id=s.id)", values)[0]["n"]

    def run(self):
        try:
            self.check()
            if not hasattr(self.core, "embedding_prepare"):
                raise ValueError("embedding unavailable")
            self._retry(lambda: self.core.embedding_prepare(self.account))
            self.check()
            while True:
                value = self._status()
                self.check()
                info = model_info(value)
                if info:
                    break
                if value.get("state") in ("error", "missing"):
                    raise ValueError("embedding unavailable")
                self.cancel.wait(0.5)
                self.check()
            from self_semantic import wait_numerical_runtime
            if wait_numerical_runtime(self.check) is None:
                raise ValueError("numerical runtime unavailable")
            vectors = VectorStore(self.store)
            indexed, total, count_generation, count_source = 0, 0, None, None
            queue, dirty = [], True
            while True:
                self.check()
                if not queue and not dirty:
                    # While chat indexing is producing new pages, keep waiting so one
                    # click also embeds later contacts and later pages; rescan only
                    # after the indexer actually stored samples.
                    indexing = bool(self.history.thread and self.history.thread.is_alive() and
                                    not self.history.cancel.is_set())
                    dirty = self.store.samples_changed.wait(1.0 if indexing else 0)
                    if dirty or indexing:
                        continue
                    with self.lock, self.store.lock:
                        self.check()
                        counts = vectors.counts(info)
                        self._state("ready", done=counts["indexedSamples"], total=counts["totalSamples"])
                    return
                if not queue:
                    with self.store.lock:
                        indexed, total, count_generation, count_source = self._coverage(vectors, info)
                        queue = vectors.pending(info, PENDING_SCAN)
                        dirty = len(queue) == PENDING_SCAN
                        self._state("indexing", done=indexed, total=total)
                    continue
                value = self._status()
                self.check()
                current = model_info(value)
                if current != info:
                    raise ValueError("embedding model changed")
                rows, queue = queue[:BATCH_SIZE], queue[BATCH_SIZE:]
                generation, source = self.store.meta("generation", 0), self.store.meta("source")
                with self.lock:
                    self.check()
                    self._state("indexing")
                response = self._retry(lambda: self.core.embed(
                    self.account, [row["input"][-2400:] for row in rows], kind="passage"))
                self.check()
                if model_info(self._status()) != info:
                    raise ValueError("embedding model changed")
                with self.lock, self.store.lock:
                    self.check()
                    if (self.store.samples_changed.is_set() or self.store.meta("generation", 0) != count_generation or
                            self.store.meta("source") != count_source):
                        indexed, total, count_generation, count_source = self._coverage(vectors, info)
                        dirty = True
                    before = self._batch_indexed(vectors, rows, info, count_source)
                    saved = vectors.save(rows, response, info, generation, source)
                    indexed += self._batch_indexed(vectors, rows, info, count_source) - before
                    self._state("indexing", done=indexed, total=total)
                    if saved < len(rows):
                        dirty = True  # rows that changed while queued are found by the next scan
        except Stopped:
            pass
        except Exception as exc:
            with self.lock:
                if not self.cancel.is_set():
                    code = getattr(exc, "code", None)
                    self._state("error", "语义索引暂未完成，可继续使用词项检索；可从顶部继续分析",
                                errorCode=code if isinstance(code, str) and code in ERROR_CODES else "embedding-failed")
