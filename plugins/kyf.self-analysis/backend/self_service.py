"""Local-library application service; account checks precede all stored-data access.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import threading

from self_contracts import DEFAULT_CONTEXT_CHARS, MAX_CONTEXT_CHARS, SelfError, integer
from self_retrieval import Retrieval, bounded_context
from self_store import Store
from self_worker import Worker
from self_embedding import EmbeddingWorker, status as embedding_status
from self_semantic import numerical_runtime, runtime_status
from self_vectors import VectorStore, model_info
from self_comparison import Comparison, parameters as comparison_parameters
from kyf_plugin.settings import read_settings

from self_draft import DEPTHS, Draft, existing as draft_profile, depth as draft_depth, saved_view as draft_saved_view
from self_model import failure, service_ready
from self_tag_store import CATEGORIES, TagStore
from self_tags import TagJob
from self_trial import REPLY_DEPTH_SETTINGS, Trial, conversation, eval_guard, depth as reply_depth
from self_analysis_run import AnalysisRun, saved_outputs, selected_outputs, saved_view as analysis_saved_view
from self_progress import timed_view
from self_contracts import now_ms

SETTINGS_FIELDS = {"analysisDepth": DEPTHS, "replyDepth": REPLY_DEPTH_SETTINGS, "analysisGrain": ("3", "6", "12", "24", "60")}


class Service:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = threading.RLock()
        self.accounts = {}
        self.embeddings = {}
        self.drafts = {}
        self.trials = {}
        self.tag_stores = {}
        self.tag_jobs = {}
        self.analyses = {}

    def _ensure_no_analysis(self, account):
        job = self.analyses.get(account)
        if job is not None and job.running:
            raise SelfError("analysis-busy", "统一分析正在进行，请使用顶部暂停按钮后再操作")

    def analysis_status(self, account):
        store, _ = self._account(account)
        with self.lock:
            job = self.analyses.get(account)
            return ({**job.view(), "stopping": bool(job.running and job.context is not None and job.context.cancelled.is_set())}
                    if job is not None else timed_view(analysis_saved_view(store), now_ms()))

    def judge_shadow(self, account):
        from self_judge_shadow import JudgeShadow
        from self_profile_corpus import ProfileCorpus
        store, _ = self._account(account)
        corpus = ProfileCorpus(store)
        result = JudgeShadow(store).summary(corpus.meta("source"), corpus.meta("revision", 0))
        self._scope(account)
        return result

    def analyze(self, account, params, remember=True):
        store, worker = self._account(account)
        selected_depth, months = self._depth(params), self._months()
        refresh_history = params.get("refreshHistory", False)
        if type(refresh_history) is not bool:
            raise SelfError("invalid-request", "同步聊天记录的参数不正确")
        outputs = selected_outputs(params.get("outputs", saved_outputs(store)))
        current = self.plugin.core.self_profile(account)
        profile = draft_profile({key: params.get(key, current.get(key) or "") for key in ("background", "style")})
        _, tags = self._tags(account)
        with self.lock:
            existing = self.analyses.get(account)
            if existing is not None and existing.running:
                if (not set(outputs) <= set(existing.outputs) or selected_depth != existing.depth or
                        months != existing.months or refresh_history and not existing.refresh_history):
                    raise SelfError("analysis-busy", "已有不同范围的分析正在进行，请等待完成或暂停后重试")
                return self.analysis_status(account)
            busy = any(jobs.get(account) is not None and jobs[account].running for jobs in (self.drafts, self.tag_jobs))
            if busy or worker.thread and worker.thread.is_alive() or self.embeddings[account].thread and self.embeddings[account].thread.is_alive():
                raise SelfError("analysis-busy", "已有分析或建库正在结束，请等待完成或暂停后重试")
            with store.lock, store.db:
                store._meta("profileDraftDepth", selected_depth)
                if remember:
                    store._meta("selfAnalysisOutputs", outputs)

            def track(key, child):
                with self.lock:
                    if key == "profile":
                        self.drafts[account] = child
                    elif key == "tags":
                        self.tag_jobs[account] = child

            job = self.analyses[account] = AnalysisRun(self.plugin, store, account, profile, selected_depth,
                                                       tags, self.embeddings[account], track, outputs, months,
                                                       refresh_history=refresh_history)
            try:
                job.start()
            except Exception as exc:
                job._set(state="interrupted", error=failure(exc, "分析暂时无法启动，请重试"))
                raise SelfError(getattr(exc, "code", None) or "analysis-failed", failure(exc)) from None
        return self.analysis_status(account)

    def analysis_settings(self, account, params):
        store, _ = self._account(account)
        if "outputs" in params:
            outputs = selected_outputs(params["outputs"], allow_empty=True)
            with self.lock, store.lock, store.db:
                self._ensure_no_analysis(account)
                store._meta("selfAnalysisOutputs", outputs)
        result = {"outputs": saved_outputs(store)}
        try:
            result["depth"] = self._settings()["analysisDepth"]
        except Exception:
            pass  # The page shows a plain settings link when the saved depth cannot be read.
        return result

    def analysis_pause(self, account, params=None):
        self._scope(account)
        params = params or {}
        output = selected_outputs([params["output"]])[0] if "output" in params else None
        with self.lock:
            job = self.analyses.get(account)
            if job is not None and job.running:
                if output is None:
                    job.cancel()
                    if "library" in job.outputs:
                        self.embeddings[account].pause()
                else:
                    job.pause_output(output)
        return self.analysis_status(account)

    def analysis_resume(self, account, params):
        self._scope(account)
        output = selected_outputs([params.get("output")])[0]
        with self.lock:
            job = self.analyses.get(account)
            if job is not None and job.running:
                if job.resume_output(output):
                    return self.analysis_status(account)
        # Continuing one item runs only that item; the saved selection stays
        # what the user ticked, so the next full run still covers the rest.
        return self.analyze(account, {**params, "outputs": [output]}, remember=False)

    def persona(self, account):
        from self_persona import PersonaStore
        store, _ = self._account(account)
        result = PersonaStore(store).view()
        self._scope(account)
        return result

    def persona_edit(self, account, params):
        from self_persona import PersonaStore
        store, _ = self._account(account)
        PersonaStore(store).edit(params.get("key"), params.get("text"), params.get("expectedVersion"))
        return self.persona(account)

    def opinion_evidence(self, account, params, kind="persona"):
        from self_persona import PersonaStore
        from self_values import ValuesStore
        store, _ = self._account(account)
        opinion = (PersonaStore if kind == "persona" else ValuesStore)(store)
        result = opinion.evidence_page(params.get("key"), params.get("kind", "inference"),
                                       params.get("side", "evidence"), params.get("offset", 0),
                                       params.get("limit", 50), expected_version=params.get("expectedVersion"))
        self._scope(account)
        return result

    def values(self, account):
        from self_values import ValuesStore
        store, _ = self._account(account)
        result = ValuesStore(store).view()
        self._scope(account)
        return result

    def values_edit(self, account, params):
        from self_values import ValuesStore
        store, _ = self._account(account)
        ValuesStore(store).edit(params.get("key"), params.get("text"), params.get("expectedVersion"))
        return self.values(account)

    def periods(self, account):
        from self_periods import periods_view
        store, _ = self._account(account)
        result = periods_view(store, self._months())
        self._scope(account)
        return result

    def period_detail(self, account, params):
        from self_periods import period_detail
        store, _ = self._account(account)
        result = period_detail(store, params.get("period"), params.get("key"))
        self._scope(account)
        return result

    def period_evidence(self, account, params, table="period"):
        from self_periods import period_evidence
        store, _ = self._account(account)
        result = period_evidence(store, params.get("period"), params.get("item"),
                                 params.get("offset", 0), params.get("limit", 50), table=table,
                                 expected_version=params.get("expectedVersion"))
        self._scope(account)
        return result

    def period_edit(self, account, params):
        from self_periods import periods_view, save_period_edit
        store, _ = self._account(account)
        save_period_edit(store, params.get("period"), params.get("field"), params.get("value"))
        result = periods_view(store, self._months())
        self._scope(account)
        return result

    def worldview(self, account):
        from self_worldview import worldview_view
        store, _ = self._account(account)
        result = worldview_view(store, self._months())
        self._scope(account)
        return result

    def worldview_detail(self, account, params):
        from self_worldview import worldview_detail
        store, _ = self._account(account)
        result = worldview_detail(store, params.get("period"), params.get("key"))
        self._scope(account)
        return result

    def worldview_edit(self, account, params):
        from self_worldview import save_worldview_edit, worldview_view
        store, _ = self._account(account)
        save_worldview_edit(store, params.get("period"), params.get("field"), params.get("value"))
        result = worldview_view(store, self._months())
        self._scope(account)
        return result

    def timeline(self, account, params=None):
        from self_profile_corpus import ProfileCorpus
        from self_timeline import timeline_view
        offset, page_bytes = (params or {}).get("offset", 0), (params or {}).get("pageBytes")
        if type(offset) is not int or offset < 0:
            raise SelfError("invalid-request", "时间线读取位置不正确")
        if page_bytes is not None and (type(page_bytes) is not int or page_bytes < 1):
            raise SelfError("invalid-request", "时间线读取范围不正确")
        store, _ = self._account(account)
        result = {**timeline_view(store, ProfileCorpus(store), offset, page_bytes), "months": self._months()}
        self._scope(account)
        return result

    def timeline_evidence(self, account, params):
        from self_profile_corpus import ProfileCorpus
        from self_timeline import timeline_evidence
        store, _ = self._account(account)
        if not isinstance(params.get("id"), str) or not params["id"]:
            raise SelfError("invalid-request", "事件不正确")
        result = timeline_evidence(store, params["id"], ProfileCorpus(store))
        self._scope(account)
        return result

    @staticmethod
    def _timeline_expect(timeline, params):
        expected = params.get("expectedVersion")
        if expected is not None and expected != timeline.version():
            raise SelfError("conflict", "时间线已在其他页面修改，请读取最新后重试")

    def timeline_edit(self, account, params):
        from self_timeline import TimelineStore, apply_timeline_edit, edit_label
        store, _ = self._account(account)
        timeline = TimelineStore(store)
        with self.lock, store.lock, store.db:
            self._timeline_expect(timeline, params)
            label, before = edit_label(timeline, params), timeline.user_state()
            edited = apply_timeline_edit(store, params)
            timeline.push_undo(label, before)
        return {**self.timeline(account), "editedId": edited, "editLabel": label}

    def timeline_undo(self, account, params):
        from self_timeline import TimelineStore
        store, _ = self._account(account)
        timeline = TimelineStore(store)
        with self.lock, store.lock, store.db:
            self._timeline_expect(timeline, params)
            undone = timeline.undo()
        return {**self.timeline(account), "undone": undone}

    def _scope(self, account):
        scope = self.plugin.core.scope()
        if scope.get("account") != account:
            raise SelfError("account-changed", "微信账号已变化，请重新打开页面")
        return scope

    def _account(self, account):
        self._scope(account)
        with self.lock:
            if account not in self.accounts:
                store = Store(self.plugin.data_dir, account)
                self.accounts[account] = (store, Worker(self.plugin.core, store, account))
                self.embeddings[account] = EmbeddingWorker(self.plugin.core, store, account, self.accounts[account][1])
            return self.accounts[account]

    def _profile(self, account):
        profile = self.plugin.core.self_profile(account)
        return {"style": profile.get("style") or "", "version": profile.get("version") or 0,
                "updatedAt": profile.get("updated_at_ms")}

    def status(self, account):
        store, worker = self._account(account)
        scope = self._scope(account)
        counts = store.counts()
        embedding = embedding_status(self.plugin.core, account)
        info = model_info(embedding)
        vector_counts = VectorStore(store).counts(info)
        semantic_ready = bool(info and vector_counts["indexedSamples"] > 0 and numerical_runtime() is not None)
        runtime = runtime_status()
        embedding_run = store.meta("embeddingRun", {})
        index_state = embedding_run.get("state", "idle")
        if index_state == "ready" and vector_counts["pendingSamples"]:
            index_state = "idle"
        if info and runtime["state"] in ("loading", "error") and index_state in ("ready", "idle"):
            index_state = "waiting" if runtime["state"] == "loading" else "error"
        embedding_worker = self.embeddings[account]
        embedding.update(indexState=index_state, indexError=embedding_run.get("error") or (
                             runtime["message"] if runtime["state"] == "error" else None),
                         stopping=bool(embedding_worker.thread and embedding_worker.thread.is_alive() and embedding_worker.cancel.is_set()))
        build = store.meta("build", {"state": "idle", "phase": "discovering"})
        coverage = store.meta("coverage", {})
        complete = (build.get("state") == "complete" and build.get("catalogComplete") is True and
                    counts["failed"] == 0 and coverage.get("state", "complete") == "complete" and not counts["stale"])
        return {"account": account, "source": {"ready": scope.get("ready"), "canBuild": True, "fingerprint": store.meta("source"),
                "message": "Core 已确认账号；完整可读范围以建库结果为准"},
                "build": {**build, "processedConversations": counts["done"], "totalConversations": counts["total"],
                          "messageCount": counts["messages"], "sampleCount": counts["samples"],
                          "failedCount": counts["failed"], "stopping": bool(worker.thread and worker.thread.is_alive() and worker.cancel.is_set())},
                "coverage": {**coverage, "scope": "all-private", "complete": complete,
                             "catalogComplete": build.get("catalogComplete", False),
                             "discoveredConversations": counts["total"], "indexedConversations": counts["done"],
                             "failedConversations": counts["failed"], "sampleCount": counts["samples"],
                             "messageCount": counts["messages"], "skippedMessages": counts["skipped"],
                             "storedSamples": counts["storedSamples"], "excludedSamples": counts["excludedSamples"],
                             "newMessagesAvailable": counts["stale"], "asOf": build.get("completedAt")},
                "profile": self._profile(account), "embedding": embedding,
                "retrieval": {"ready": counts["samples"] > 0, "mode": "hybrid" if semantic_ready else "lexical",
                              "semanticReady": semantic_ready, **vector_counts,
                              "runtimeState": runtime["state"],
                              "message": "结合本机语义与词项检索" if semantic_ready else (
                                  runtime["message"] if runtime["state"] in ("loading", "error") else
                                  "词项检索可用，语义索引就绪后自动结合")},
                "model": {"ready": None, "message": "回复模型在核心模型设置中管理"},
                "capabilities": {"lexicalReady": counts["samples"] > 0, "pause": True, "resume": True, "incremental": True},
                "revision": store.meta("revision", 0), "generation": store.meta("generation", 0)}

    def exclude(self, account, identifier, excluded=True):
        store, _ = self._account(account)
        if not isinstance(identifier, str) or len(identifier) != 64 or type(excluded) is not bool:
            raise SelfError("invalid-request", "样例标识或排除设置不正确")
        changed = store.exclude(identifier, excluded)
        return {**self.status(account), "changed": changed, "excluded": excluded, "id": identifier}

    def exclusions(self, account):
        store, _ = self._account(account)
        rows = store.rows("SELECT s.*,c.name FROM samples s JOIN exclusions e ON e.sample_id=s.id "
                          "JOIN conversations c ON c.user=s.user ORDER BY e.updated_ms DESC LIMIT 200")
        return {"sources": [{"id": row["id"], "user": row["user"], "name": row["name"], "scene": row["scene"],
                             "input": row["input"], "reply": row["reply"], "timeMs": row["time_ms"]} for row in rows]}

    def build(self, account, mode="incremental"):
        store, worker = self._account(account)
        with self.lock, worker.lock:
            self._ensure_no_analysis(account)
            # Semantic completion only backfills missing vectors; it must not
            # rediscover chats or restart their history progress.
            if mode != "semantic":
                worker.start(mode)
            self.embeddings[account].start()
        return self.status(account)

    def resume(self, account):
        store, worker = self._account(account)
        with self.lock, worker.lock:
            mode = "semantic" if store.meta("build", {}).get("state") == "complete" else "incremental"
            return self.build(account, mode)

    def pause(self, account):
        _, worker = self._account(account)
        with self.lock, worker.lock:
            worker.pause()
            self.embeddings[account].pause()
        return self.status(account)

    def reset(self, account):
        store, worker = self._account(account)
        with self.lock, worker.lock:
            self._ensure_no_analysis(account)
            worker.pause()
            self.embeddings[account].pause()
            store.reset()
        return self.status(account)

    def search(self, account, params, user=None):
        store, _ = self._account(account)
        retrieval = Retrieval(self.plugin.core, store, account)
        sources = retrieval.search(params, user)
        self._scope(account)
        return {"available": bool(sources), "sources": sources, "mode": retrieval.mode,
                "semanticPartial": retrieval.semantic_partial, "semanticScanned": retrieval.semantic_scanned,
                "revision": store.meta("revision", 0), "generation": store.meta("generation", 0)}

    def compare(self, account, params, user=None):
        if not isinstance(account, str) or not 1 <= len(account) <= 256:
            raise SelfError("invalid-request", "请先选择微信账号")
        params = comparison_parameters(params)
        store, _ = self._account(account)
        result = Comparison(self.plugin.core, store, account).run(params, user)
        self._scope(account)
        return result

    def draft(self, account, params):
        """Start one AI first draft of 本人资料; the running job is returned instead of a second one."""
        store, _ = self._account(account)
        self._ensure_no_analysis(account)
        profile = draft_profile(params)
        selected_depth = self._depth(params)
        with self.lock, store.lock, store.db:
            self._ensure_no_analysis(account)
            store._meta("profileDraftDepth", selected_depth)
        return self._start(account, store, self.drafts, Draft, profile, selected_depth)

    def draft_status(self, account):
        store, _ = self._account(account)
        with self.lock:
            job = self.drafts.get(account)
        if job is not None:
            return {**job.view(), "stopping": bool(job.running and job.context is not None and job.context.cancelled.is_set())}
        return draft_saved_view(store)

    def draft_pause(self, account):
        self._scope(account)
        with self.lock:
            job = self.drafts.get(account)
        if job is not None and job.running:
            job.cancel()
        return self.draft_status(account)

    def _settings(self):
        """The plugin settings page owns the depths and the grain; a saved value applies to the next run."""
        return read_settings(self.plugin.core, SETTINGS_FIELDS)["values"]

    def _months(self):
        """How long one period is on every timeline; an analysis keeps the value it started with."""
        return int(self._settings()["analysisGrain"])

    def _depth(self, params):
        return draft_depth(params["depth"] if "depth" in params else self._settings()["analysisDepth"])

    def reply(self, account, params):
        """Start one 聊天试用 reply to the friend's last message; a running reply is returned instead.

        Optional ``beforeTimeMs``/``excludeSampleIds``/``excludeMessageIds`` are evaluation-only
        leak guards (protocol 3.3); absent keys keep the production behaviour unchanged.
        """
        turns = conversation(params)
        level = reply_depth(params["depth"] if "depth" in params else self._settings()["replyDepth"])
        guard = eval_guard(params)
        store, tags = self._tags(account)
        return self._start(account, store, self.trials, Trial, turns, tags, level, guard.get("beforeTimeMs"),
                           guard.get("excludeSampleIds"), guard.get("excludeMessageIds"))

    def reply_status(self, account):
        return self._job_status(account, self.trials)

    def _tags(self, account):
        store, _ = self._account(account)
        with self.lock:
            if account not in self.tag_stores:
                self.tag_stores[account] = TagStore(store)
            return store, self.tag_stores[account]

    def tags(self, account):
        _, tags = self._tags(account)
        with self.lock:
            job = self.tag_jobs.get(account)
        run = tags.state()
        if job is not None and job.running:
            view = job.view()
            run = {**run, "state": "running", "phase": view["phase"], "done": view["done"], "total": view["total"],
                   "stopping": bool(job.context is not None and job.context.cancelled.is_set())}
        result = {"account": account, "run": run, "counts": tags.counts(), "tags": tags.tags(),
                  "categories": list(CATEGORIES)}
        self._scope(account)
        return result

    def tags_analyze(self, account):
        """Read every chat and extract tags; a paused run continues and a finished one reads only new messages."""
        store, tags = self._tags(account)
        self._ensure_no_analysis(account)
        self._start(account, store, self.tag_jobs, TagJob, tags)
        return self.tags(account)

    def tags_pause(self, account):
        self._tags(account)
        with self.lock:
            job = self.tag_jobs.get(account)
        if job is not None and job.running:
            job.cancel()
        return self.tags(account)

    def tag_evidence(self, account, params):
        _, tags = self._tags(account)
        if not isinstance(params.get("name"), str):
            raise SelfError("invalid-request", "标签不正确")
        result = tags.evidence(params["name"])
        self._scope(account)
        return result

    def tag_edit(self, account, params):
        _, tags = self._tags(account)
        tags.edit(params.get("action"), params.get("name"), params.get("category"))
        return self.tags(account)

    def _start(self, account, store, jobs, kind, *args):
        with self.lock:
            if kind in (Draft, TagJob):
                self._ensure_no_analysis(account)
            job = jobs.get(account)
            if job is not None and job.running:
                return job.view()
        if kind.needs_samples and store.counts()["samples"] == 0:
            raise SelfError("no-samples", "还没有可用的本人回复，请先建立知识库")
        try:
            service_ready(self.plugin)
        except Exception as exc:
            raise SelfError(getattr(exc, "code", None) or "model-unavailable", failure(exc, kind.fallback)) from None
        with self.lock:
            if kind in (Draft, TagJob):
                self._ensure_no_analysis(account)
            job = jobs.get(account)
            if job is not None and job.running:
                return job.view()
            job = jobs[account] = kind(self.plugin, store, account, *args)
        try:
            job.start()
        except Exception as exc:
            with self.lock:
                if jobs.get(account) is job:
                    del jobs[account]
            raise SelfError(getattr(exc, "code", None) or kind.kind + "-failed", failure(exc, kind.fallback)) from None
        return job.view()

    def _job_status(self, account, jobs):
        self._scope(account)
        with self.lock:
            job = jobs.get(account)
        return job.view() if job is not None else {"state": "idle"}

    def context(self, account, params, user=None):
        store, _ = self._account(account)
        profile = self._profile(account)
        common = {"revision": store.meta("revision", 0), "generation": store.meta("generation", 0),
                  "profileVersion": profile["version"], "sourceFingerprint": store.meta("source"), "mode": "lexical"}
        if params.get("validateOnly"):
            retrieval = Retrieval(self.plugin.core, store, account)
            valid = (params.get("generation") == common["generation"] and
                     params.get("profileVersion") == profile["version"] and
                     params.get("sourceFingerprint") == common["sourceFingerprint"] and
                     retrieval.validate(params.get("sources", [])))
            # Never hold SQLite locks across source calls. Re-read the stamps
            # after verification in case reset/profile editing happened in flight.
            profile = self._profile(account)
            common.update(generation=store.meta("generation", 0), revision=store.meta("revision", 0),
                          sourceFingerprint=store.meta("source"), profileVersion=profile["version"])
            valid = bool(valid and all(params.get(key) == common[key] for key in (
                "generation", "sourceFingerprint", "profileVersion")) and
                         retrieval.references_current(params.get("sources", [])))
            self._scope(account)
            return {**common, "available": valid, "valid": valid}
        budget = integer(params.get("budgetChars"), DEFAULT_CONTEXT_CHARS, 256, MAX_CONTEXT_CHARS)
        retrieval = Retrieval(self.plugin.core, store, account)
        search_params = {**params, "limit": min(4, integer(params.get("limit"), 4, 1, 8))}
        sources = retrieval.search(search_params, user)
        bounded = bounded_context(sources, profile["style"], max(256, budget - 350))
        self._scope(account)
        result = {**common, **bounded, "mode": retrieval.mode, "semanticPartial": retrieval.semantic_partial,
                  "available": bool(bounded["sources"] or bounded["profile"]["style"]),
                  "revision": store.meta("revision", 0), "generation": store.meta("generation", 0)}
        while len(json.dumps(result, ensure_ascii=False)) > budget and result["sources"]:
            result["sources"].pop()
        if len(json.dumps(result, ensure_ascii=False)) > budget:
            result["text"] = ""
            result["profile"]["style"] = ""
        result["available"] = bool(result["sources"] or result["profile"]["style"])
        return result

    def changed(self, account, user):
        with self.lock:
            entry = self.accounts.get(account)
            if entry:
                entry[0].mark_stale(user)

    def close(self):
        with self.lock:
            entries = [(account, *value) for account, value in self.accounts.items()]
            self.accounts.clear()
            for job in [*self.drafts.values(), *self.trials.values(), *self.tag_jobs.values(), *self.analyses.values()]:
                job.cancel()
        for account, store, worker in entries:
            embeddings_stopped = self.embeddings[account].close()
            if worker.close() and embeddings_stopped and not (self.analyses.get(account) and self.analyses[account].running):
                store.close()
