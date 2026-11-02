"""Read-only, same-query comparison using the shipped retrieval implementation.

The keyword branch hides only the optional embedding capability. Both branches
keep the production ranking, eligibility limits and fresh source verification.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import time
import unicodedata

from self_contracts import MAX_QUERY_CHARS, MAX_SOURCES, SelfError, integer, now_ms
from self_embedding import status as embedding_status
from self_retrieval import Retrieval
from self_semantic import numerical_runtime, runtime_status
from self_vectors import VectorStore, model_info


class LexicalOnlyCore:
    """Per-request capability adapter; never changes shared Core or module state."""
    def __init__(self, core):
        self.core = core

    def embedding_status(self, account):
        return {"ready": False}

    def __getattr__(self, name):
        return getattr(self.core, name)


def parameters(params):
    if not isinstance(params, dict):
        raise SelfError("invalid-request", "对比参数不正确")
    query = params.get("query")
    if (not isinstance(query, str) or len(query) > MAX_QUERY_CHARS or
            not any(unicodedata.category(char)[0] in ("L", "N") for char in query)):
        raise SelfError("invalid-request", "请输入 4000 字以内的聊天文字或场景")
    return {**params, "query": query, "limit": integer(params.get("limit"), 4, 1, MAX_SOURCES),
            "beforeTimeMs": integer(params.get("beforeTimeMs"), now_ms(), 1, 2**53)}


class Comparison:
    def __init__(self, core, store, account):
        self.core, self.store, self.account = core, store, account

    def _stamp(self):
        with self.store.lock:
            return {"sourceFingerprint": self.store.meta("source"), "generation": self.store.meta("generation", 0),
                    "revision": self.store.meta("revision", 0)}

    def _semantic_status(self, retrieval):
        embedding = embedding_status(self.core, self.account)
        info = model_info(embedding)
        counts = VectorStore(self.store).counts(info)
        ready = bool(info and counts["indexedSamples"] and numerical_runtime() is not None)
        used = retrieval.mode == "hybrid"
        if used:
            state = "partial" if retrieval.semantic_partial or counts["pendingSamples"] else "ready"
            message = ("本次语义扫描达到时限，仅覆盖已扫描的样本" if retrieval.semantic_partial else
                       "本次已结合语义匹配；仍有样本等待建立语义索引" if counts["pendingSamples"] else
                       "本次已结合语义匹配")
        elif ready:
            state, message = "fallback", "本次语义查询未完成，混合列已降级为关键词检索，可稍后重试"
        elif not counts["totalSamples"]:
            state, message = "empty", "知识库暂无可用样本，请先建立本人回复知识库"
        elif not info:
            state, message = embedding.get("state") or "unavailable", "语义服务尚未就绪，混合列当前使用关键词检索"
        elif not counts["indexedSamples"]:
            state, message = "indexing", "样本尚未建立语义索引，混合列当前使用关键词检索"
        else:
            runtime = runtime_status()
            state, message = runtime["state"], runtime["message"]
        return {"ready": ready, "used": used, "partial": retrieval.semantic_partial,
                "scanned": retrieval.semantic_scanned, **counts, "state": state, "message": message,
                "modelId": embedding.get("modelId"), "modelRevision": embedding.get("revision")}

    def run(self, params, user=None):
        params = parameters(params)
        started = time.perf_counter()
        initial = self._stamp()
        lexical = Retrieval(LexicalOnlyCore(self.core), self.store, self.account)
        hybrid = Retrieval(self.core, self.store, self.account)
        branch_start = time.perf_counter()
        lexical_sources = lexical.search(params, user)
        lexical_ms = (time.perf_counter() - branch_start) * 1000
        branch_start = time.perf_counter()
        hybrid_sources = hybrid.search(params, user)
        hybrid_ms = (time.perf_counter() - branch_start) * 1000
        semantic = self._semantic_status(hybrid)
        refs = list({row["id"]: {"id": row["id"], "hash": row["hash"]}
                     for row in [*lexical_sources, *hybrid_sources]}.values())
        consistent = (params.get("sourceFingerprint") in (None, initial["sourceFingerprint"]) and
                      self._stamp() == initial)
        # A first-branch source can change while the second branch is running.
        # Recheck the union using the existing fresh verifier, in bounded chunks.
        if consistent:
            consistent = all(hybrid.validate(refs[start:start + MAX_SOURCES])
                             for start in range(0, len(refs), MAX_SOURCES))
        with self.store.lock:
            consistent = bool(consistent and self._stamp() == initial and hybrid.references_current(refs))
        if not consistent:
            lexical_sources, hybrid_sources = [], []
        lexical_ids, hybrid_ids = [row["id"] for row in lexical_sources], [row["id"] for row in hybrid_sources]
        result = {"query": params["query"], "beforeTimeMs": params["beforeTimeMs"], "limit": params["limit"],
                  "available": bool(lexical_sources or hybrid_sources), "consistent": consistent, **initial,
                  "semantic": semantic,
                  "results": {"lexical": {"available": bool(lexical_sources), "sources": lexical_sources},
                              "hybrid": {"available": bool(hybrid_sources), "sources": hybrid_sources,
                                         "mode": hybrid.mode}},
                  "overlap": {"sharedIds": [identifier for identifier in lexical_ids if identifier in hybrid_ids],
                              "lexicalOnlyIds": [identifier for identifier in lexical_ids if identifier not in hybrid_ids],
                              "hybridOnlyIds": [identifier for identifier in hybrid_ids if identifier not in lexical_ids]},
                  "timings": {"lexicalMs": round(lexical_ms, 3), "hybridMs": round(hybrid_ms, 3),
                              "totalMs": round((time.perf_counter() - started) * 1000, 3)}}
        if not consistent:
            result["reason"] = ("source-changed" if params.get("sourceFingerprint") not in (None, initial["sourceFingerprint"]) else
                                "library-changed")
        return result
