"""One resumable account task over one verified, incremental chat snapshot.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import sqlite3
import threading
import time

from kyf_plugin import Cancelled
from self_contracts import SelfError, now_ms
from self_library import ReplyLibrary
from self_model import Job, failure, retry_unavailable, transient
from self_profile_corpus import ProfileCorpus, Reader
from self_progress import progress_step, timed_view

OUTPUTS = ("profile", "tags", "timeline", "persona", "periods", "worldview", "values", "library")
# 既有账号未保存过选择时保持原默认五项；时间线和两条时期时间线只在被勾选后运行。
DEFAULT_OUTPUTS = ("profile", "tags", "persona", "values", "library")
AI_OUTPUTS = ("profile", "tags", "persona", "values")
TIMELINE_KEYS = ("timeline-extract", "timeline")
STEPS = (("history", "同步聊天记录"), ("library", "整理表达知识库"), ("evidence", "提取分析依据"), ("profile", "本人资料"),
         ("tags", "标签分析"), ("timeline-extract", "时间线提取"), ("timeline", "整理时间线"),
         ("persona", "性格分析"), ("periods", "性格时间线"),
         ("worldview", "三观时间线"), ("values", "三观分析"),
         ("semantic", "完善语义索引"))
# A stage hit by a Core blip or a busy model service resumes from its saved
# progress after a pause instead of stopping the whole overnight run.
STAGE_RETRIES, STAGE_RETRY_SECONDS = 6, (20, 40, 60, 120, 180, 300)
SOURCE_ERRORS = {"account-changed", "account-unavailable", "source-changed", "core-unavailable", "stale-corpus",
                 "history-unverified", "verification-unavailable"}


def selected_outputs(value, allow_empty=False):
    if (not isinstance(value, list) or any(not isinstance(key, str) or key not in OUTPUTS for key in value) or
            len(set(value)) != len(value) or not value and not allow_empty):
        raise SelfError("invalid-request", "请选择要更新的分析项目")
    return [key for key in OUTPUTS if key in value]


def saved_outputs(store):
    value = store.meta("selfAnalysisOutputs", list(DEFAULT_OUTPUTS))
    return [key for key in OUTPUTS if key in value] if isinstance(value, list) else list(DEFAULT_OUTPUTS)


def saved_view(store):
    saved = store.meta("selfAnalysisRun", {})
    if not saved:
        return {"state": "idle", "steps": [], "depth": store.meta("profileDraftDepth", "deep"),
                "outputs": saved_outputs(store)}
    saved = {**saved, "outputs": [key for key in saved.get("outputs", DEFAULT_OUTPUTS) if key in OUTPUTS],
             "steps": [step for step in saved.get("steps", []) if step.get("key") in dict(STEPS)]}
    if saved.get("state") == "running":
        saved = {**saved, "state": "interrupted", "error": "上次分析已中断，进度已保留，可以继续"}
        saved["steps"] = [{**step, "state": "pending" if step["state"] == "running" else step["state"]}
                          for step in saved.get("steps", [])]
    return saved


class StageCancellation:
    """Cancel an individual request without cancelling the shared Core task."""
    def __init__(self, stage):
        self.stage = stage

    def is_set(self):
        return self.stage.context.cancelled.is_set() or self.stage.parent._stage_paused(self.stage.key)

    def set(self):
        if self.stage.key in OUTPUTS or self.stage.key in TIMELINE_KEYS:
            self.stage.parent.pause_output("timeline" if self.stage.key in TIMELINE_KEYS else self.stage.key)
        else:
            self.stage.parent.cancel()

    def wait(self, timeout=None):
        start = time.monotonic()
        while not self.is_set():
            remaining = None if timeout is None else timeout - (time.monotonic() - start)
            if remaining is not None and remaining <= 0:
                return False
            self.stage.context.cancelled.wait(min(.05, remaining) if remaining is not None else .05)
        return True


class StageContext:
    """Keep the Core execution token while routing child progress to one step."""
    defer_judge_shadow = True
    def __init__(self, parent, context, key):
        self.parent, self.context, self.key = parent, context, key
        self.cancelled = StageCancellation(self)

    def __getattr__(self, name):
        return getattr(self.context, name)

    def check(self):
        self.context.check()
        self.parent._check_scope()
        if self.cancelled.is_set():
            raise Cancelled()

    def progress(self, done=None, total=None, label=None, **extra):
        self.parent._step(self.key, done=done, total=total, detail=label or "处理中", **extra)
        callback = getattr(self.context, "progress", None)
        if callable(callback):
            callback(done=done, total=total, label=label or dict(STEPS)[self.key], **extra)


class AnalysisRun(Job):
    title, kind, fallback = "自我剖析", "self-analysis", "分析未完成，已有结果和进度已保留，可以继续"
    task_kind, needs_samples = "background", False

    def __init__(self, plugin, store, account, profile, selected_depth, tags, embedding, track, outputs=None, months=6,
                 refresh_history=False):
        self.profile, self.depth, self.tags, self.months = profile, selected_depth, tags, months
        self.refresh_history = refresh_history
        self.embedding, self.track = embedding, track
        self.outputs = selected_outputs(list(OUTPUTS) if outputs is None else outputs)
        self.paused_outputs = set()
        self.control_lock = threading.RLock()
        self.children = {}
        self.embedding_started = False
        self.scope_checked_at = 0
        self.corpus = ProfileCorpus(store)
        enabled = {"history", *self.outputs}
        if any(key in self.outputs for key in AI_OUTPUTS):
            enabled.add("evidence")
        if "timeline" in self.outputs:
            enabled.update(TIMELINE_KEYS)
        if "library" in self.outputs:
            enabled.add("semantic")
        self.steps = [{"key": key, "title": title, "state": "pending" if key in enabled else "skipped", "detail": ""}
                      for key, title in STEPS]
        super().__init__(plugin, store, account, depth=selected_depth, outputs=self.outputs, pausedOutputs=[],
                         months=months, refreshHistory=refresh_history, phase="history", steps=self.steps, coverage={})
        with store.lock, store.db:
            if not self.corpus.meta("sharedReplies", False):
                self.corpus._meta("sharedReplies", True)
                store.db.execute("UPDATE profile_corpus_conversations SET proofs_dirty=1 WHERE is_group=0")
        self._set()

    def _set(self, **changes):
        super()._set(**changes)
        try:
            with self.store.lock, self.store.db:
                self.store._meta("selfAnalysisRun", self.view())
        except sqlite3.ProgrammingError:
            pass

    def _step(self, key, **changes):
        with self.control_lock:
            stamp = now_ms()
            self.steps = [progress_step(step, changes, stamp) if step["key"] == key else step for step in self.steps]
            self._set(phase=key, steps=self.steps, pausedOutputs=sorted(self.paused_outputs))

    def _state(self, key):
        return next(step["state"] for step in self.steps if step["key"] == key)

    def _stage_paused(self, key):
        paused = self.paused_outputs
        if key == "evidence":
            return not any(output in self.outputs and output not in paused for output in AI_OUTPUTS)
        if key == "history":
            return all(output in paused for output in self.outputs)
        if key in TIMELINE_KEYS:
            return "timeline" in paused
        return ("library" if key == "semantic" else key) in paused

    def _settle_pause(self, key):
        linked = AI_OUTPUTS if key == "evidence" else ("library",) if key == "semantic" else ()
        for output in linked:
            if output in self.outputs:
                self._step(output, stopping=False)

    def pause_output(self, key):
        if key not in self.outputs:
            raise SelfError("invalid-request", "本次没有选择这个分析项目")
        with self.control_lock:
            if not self.running or self._state(key) == "done" and (key != "library" or self._state("semantic") == "done"):
                return
            self.paused_outputs.add(key)
            active_phase = self.data["phase"]
            self._step(key, state="running" if self._state(key) == "running" else "paused",
                       stopping=self._state(key) == "running", detail="进度已保留")
            if key == "library":
                self.embedding.pause()
                if self._state("semantic") not in ("done", "skipped"):
                    active = active_phase == "semantic" and self._state("semantic") == "running"
                    self._step("semantic", state="running" if active else "paused", stopping=active, detail="进度已保留")
                    self._step("library", stopping=active or self._state("library") == "running")
            if key in AI_OUTPUTS and self._stage_paused("evidence") and self._state("evidence") == "running":
                self._step("evidence", stopping=True)
                for output in self.outputs:
                    if output in AI_OUTPUTS:
                        self._step(output, stopping=True)
            self._set(phase=active_phase)

    def resume_output(self, key):
        if key not in self.outputs:
            raise SelfError("invalid-request", "本次没有选择这个分析项目")
        with self.control_lock:
            if not self.running:
                return False
            if key not in self.paused_outputs:
                return True
            if any(step.get("stopping") for step in self.steps if step["key"] == key or key == "library" and step["key"] == "semantic"):
                raise SelfError("analysis-busy", "这一项正在暂停，进度保存后即可继续")
            self.paused_outputs.remove(key)
            active_phase = self.data["phase"]
            if self._state(key) == "paused":
                self._step(key, state="pending", stopping=False, detail="")
            if key in AI_OUTPUTS and self._state("evidence") == "paused":
                self._step("evidence", state="pending", detail="")
            if key == "library" and self._state("semantic") == "paused":
                self._step("semantic", state="pending", detail="")
            self._set(phase=active_phase, pausedOutputs=sorted(self.paused_outputs))
            return True

    def _has_work(self):
        return any(self._state(key) in ("pending", "paused") and not self._stage_paused(key) and
                   not (key == "timeline" and self._state("timeline-extract") != "done")
                   for key in (*self.outputs, "timeline-extract", "semantic"))

    def view(self):
        result = super().view()
        if self.embedding_started:
            embedding = self.store.meta("embeddingRun", {})
            states = {"waiting": "running", "indexing": "running", "ready": "done", "error": "error", "paused": "pending"}
            result["steps"] = [{**step, "state": step["state"] if step["state"] in ("error", "paused", "skipped") else states.get(embedding.get("state"), step["state"]),
                                "done": embedding.get("done"), "total": embedding.get("total"), "unit": "条样本",
                                "detail": "", "error": step.get("error") or embedding.get("error")}
                               if step["key"] == "semantic" else step for step in result["steps"]]
        return timed_view(result, now_ms())

    def _child_update(self, key, value, offsets=None):
        names = ("done", "total", "unit", "phase", "batchesDone", "batchesSkipped", "skippedParts", "droppedProofs", "analyzedMessages",
                 "ownMessages", "modelCalls", "reusedMessages", "newlyAnalyzedMessages", "completedCallCount",
                 "successfulCallCount", "cumulativeCallMs", "activeCallStartedAt", "activeCallStep",
                 "progressScope", "plannedAnswers", "cachedAnswers", "attempted", "retryRound", "skipped", "failedAttempts")
        changes = {name: value[name] for name in names if name in value}
        for name, offset in (offsets or {}).items():
            changes[name] = offset + value.get(name, 0)
        if value.get("state") == "done" and type(value.get("ownMessages")) is int:
            changes.update(done=value.get("analyzedMessages", 0), total=value["ownMessages"], unit="条")
        self._step(key, **changes)

    def _coverage(self):
        result = self.corpus.counts()
        if "profile" in self.children:
            result["profile"] = self.children["profile"].view().get("coverage", {})
        if "persona" in self.children:
            child = self.children["persona"].view()
            result["persona"] = child.get("coverage") or child.get("result", {}).get("coverage", {})
        if "values" in self.children:
            child = self.children["values"].view()
            result["values"] = child.get("coverage") or child.get("result", {}).get("coverage", {})
        result["tags"] = self.tags.counts()
        if "evidence" in self.children:
            child = self.children["evidence"].view()
            result["evidence"] = child.get("coverage") or child.get("result", {}).get("coverage") or {
                key: child[key] for key in ("ownMessages", "analyzedMessages", "totalParts", "analyzedParts",
                                            "reusedMessages", "newlyAnalyzedMessages", "depth", "gaps") if key in child}
        return result

    def _check_scope(self, force=False):
        # Local per-message checks must remain cheap on large histories. Every
        # stage boundary also performs an unconditional account/source check.
        current = time.monotonic()
        if force or current - self.scope_checked_at >= 1:
            scope = retry_unavailable(lambda: self.plugin.core.scope())
            if scope.get("account") != self.account:
                raise SelfError("account-changed", "微信账号已变化，请重新打开页面")
            self.scope_checked_at = current

    def _guard(self, context):
        # One status call carries the account-wide source fingerprint. Checking
        # every chat's epoch here cost one Core round trip per chat (3479 calls,
        # 20+ minutes on a large account) after each stage; a rebuilt chat is
        # found by the next sync instead.
        self._check_scope(force=True)
        context.check()
        for row in self.corpus.rows("SELECT user,epoch FROM profile_corpus_conversations WHERE state='done' AND external=0 "
                                    "ORDER BY user LIMIT 1"):
            status = retry_unavailable(lambda: self.plugin.core.history_status(self.account, row["user"]))
            context.check()
            source = self.corpus.meta("source")
            if (status.get("sourceFingerprint") != source or status.get("currentSourceFingerprint", source) != source or
                    status.get("epoch") != row["epoch"]):
                raise SelfError("source-changed", "聊天来源或版本已变化，已保留结果，请重新同步分析")

    def _child(self, context, key, job):
        step = next(step for step in self.steps if step["key"] == key)
        counters = ("modelCalls", "completedCallCount", "successfulCallCount", "cumulativeCallMs")
        offsets = {name: step.get(name, 0) for name in counters}
        self.children[key] = job
        self.track(key, job)
        job.context = context
        job.on_update = lambda value: self._child_update(key, value, offsets)
        try:
            result = job.work(context)
            context.check()
            job._set(state="done", phase="done", result=result, finishedAt=now_ms())
            if key == "tags":
                self.tags.update(state="complete", error=None, completedAt=now_ms())
            return result
        except Exception as exc:
            cancelled = isinstance(exc, Cancelled) or context.cancelled.is_set()
            job._set(state="cancelled" if cancelled else "error", error=None if cancelled else failure(exc), finishedAt=now_ms())
            if key == "tags":
                self.tags.update(state="paused" if cancelled else "failed", error=None if cancelled else failure(exc))
            raise
        finally:
            close = getattr(job, "close_models", None)
            if callable(close):
                close()

    def _execute(self, context, key, action):
        self._check_scope(force=True)
        stage = StageContext(self, context, key)
        context.check()
        if self._stage_paused(key):
            self._step(key, state="paused", detail="进度已保留")
            return False
        self._step(key, state="running", error=None, startedAt=now_ms(),
                   stopping=False, detail="", unit="个聊天" if key in ("history", "library") else "条样本" if key == "semantic" else "条")
        try:
            attempt = 0
            while True:
                try:
                    action(stage)
                    break
                except Cancelled:
                    raise
                except Exception as exc:
                    if (attempt >= STAGE_RETRIES or not transient(exc) or context.cancelled.is_set() or
                            self._stage_paused(key)):
                        raise
                    self._report(exc)
                    wait = STAGE_RETRY_SECONDS[attempt]
                    attempt += 1
                    self._step(key, state="running", detail=f"连接暂时中断，{wait} 秒后自动继续（第 {attempt} 次）")
                    if stage.cancelled.wait(wait):
                        raise Cancelled() from None
                    self._step(key, detail="")
            stage.check()
            self._guard(stage)
            self._step(key, state="done", detail="", error=None, stopping=False, finishedAt=now_ms())
            self._set(coverage=self._coverage())
            return True
        except Cancelled:
            if context.cancelled.is_set():
                raise
            if key == "library":
                self.store.update_build(state="paused")
            self._settle_pause(key)
            self._step(key, state="paused" if self._stage_paused(key) else "pending", stopping=False, detail="进度已保留")
            return False
        except Exception as exc:
            context.check()
            self._report(exc)
            if getattr(exc, "code", None) in SOURCE_ERRORS:
                self._step(key, state="error", detail="进度已保留", stopping=False, error=failure(exc, self.fallback))
                raise
            if self._stage_paused(key):
                if key == "library":
                    self.store.update_build(state="paused")
                self._settle_pause(key)
                self._step(key, state="paused", stopping=False, detail="进度已保留")
                return False
            self._step(key, state="error", detail="进度已保留", error=failure(exc, self.fallback))
            if key == "library":
                self.store.update_build(state="paused")
            if key == "history" or getattr(exc, "code", None) in SOURCE_ERRORS:
                raise
            return False

    def work(self, context):
        from self_evidence import EvidenceStore, SharedEvidence
        from self_evidence_summaries import ProfileSummary, TagSummary, PersonaSummary, ValuesSummary
        from self_periods import PeriodAnswers
        from self_timeline import TimelineExtract, TimelineOrganize
        from self_worldview import WorldviewAnswers

        def history(stage):
            return Reader(self.plugin.core, self.account, self.corpus, stage,
                          lambda counts: self._set(coverage=counts), reuse_recent=not self.refresh_history).run()

        while not self._execute(context, "history", history):
            if self._stage_paused("history"):
                return {}
        counts = self.corpus.counts()
        if counts["failedConversations"]:
            self._step("history", detail=f"{counts['failedConversations']} 个聊天读取失败，已保留原有资料，下次运行会重试")
        self._set(coverage=counts)
        evidence = EvidenceStore(self.store)

        def library(stage):
            result = ReplyLibrary(self.store, self.corpus, self.account).work(stage, lambda done, total: stage.progress(
                done=done, total=total, unit="个聊天", label="整理历史回复"))
            # A per-output resume may arrive while its previous vector request is
            # still unwinding. Wait for it rather than losing the new start.
            while self.embedding.thread and self.embedding.thread.is_alive() and self.embedding.cancel.is_set():
                stage.check()
                self.embedding.thread.join(.05)
            stage.check()
            self.embedding.start()
            self.embedding_started = True
            return result

        def semantic(stage):
            while self.embedding.thread and self.embedding.thread.is_alive():
                stage.check()
                value = self.store.meta("embeddingRun", {})
                stage.progress(label="建立语义索引", unit="条样本", done=value.get("done"), total=value.get("total"))
                stage.cancelled.wait(.25)
            stage.check()
            value = self.store.meta("embeddingRun", {})
            if value.get("state") != "ready":
                raise SelfError("embedding-unavailable", value.get("error") or "语义索引尚未完成，词项检索仍可使用")

        def eligible(key):
            if key == "timeline" and self._state("timeline-extract") != "done":
                return False
            return self._state(key) in ("pending", "paused") and not self._stage_paused(key)

        while True:
            context.check()
            if eligible("library"):
                self._execute(context, "library", library)
            # 两条时期时间线只读本人发言，不依赖共同证据，所以排在耗时更长的共同提取之前。
            for key, answers in (("periods", PeriodAnswers), ("worldview", WorldviewAnswers)):
                if eligible(key):
                    self._execute(context, key, lambda stage, key=key, answers=answers: self._child(stage, key, answers(
                        self.plugin, self.store, self.account, self.corpus, self.depth, self.months)))
            ai = [key for key in AI_OUTPUTS if eligible(key)]
            if ai and self._state("evidence") != "done":
                if eligible("evidence"):
                    self._execute(context, "evidence", lambda stage: self._child(stage, "evidence", SharedEvidence(
                        self.plugin, self.store, self.account, self.corpus, self.depth)))
                if self._state("evidence") == "error":
                    for key in ai:
                        self._step(key, state="error", error="分析依据尚未完成，进度已保留", detail="")
            if self._state("evidence") == "done":
                for key in AI_OUTPUTS:
                    if not eligible(key):
                        continue
                    factories = {
                        "profile": lambda: ProfileSummary(self.plugin, self.store, self.account, self.corpus,
                                                           evidence, self.profile, self.depth),
                        "tags": lambda: TagSummary(self.plugin, self.store, self.account, self.corpus,
                                                   evidence, self.tags, self.depth),
                        "persona": lambda: PersonaSummary(self.plugin, self.store, self.account, self.corpus,
                                                           evidence, self.depth),
                        "values": lambda: ValuesSummary(self.plugin, self.store, self.account, self.corpus,
                                                        evidence, self.depth),
                    }
                    self._execute(context, key, lambda stage, key=key: self._child(stage, key, factories[key]()))
            # 人生事件提取耗时较长，先交付性格、三观及详细依据；仅选时间线仍可独立运行。
            if "timeline" in self.outputs and eligible("timeline-extract"):
                profile = self.profile if isinstance(self.profile, dict) else {}
                self._execute(context, "timeline-extract", lambda stage: self._child(
                    stage, "timeline-extract", TimelineExtract(self.plugin, self.store, self.account,
                                                               self.corpus, profile, self.depth)))
            if "timeline" in self.outputs and eligible("timeline") and self._state("timeline-extract") == "done" \
                    and self._state("timeline") != "done":
                profile = self.profile if isinstance(self.profile, dict) else {}
                self._execute(context, "timeline", lambda stage: self._child(
                    stage, "timeline", TimelineOrganize(self.plugin, self.store, self.account,
                                                        self.corpus, profile, self.depth)))
            if self._state("library") == "done" and eligible("semantic"):
                self._execute(context, "semantic", semantic)
            if self._state("library") == "error" and self._state("semantic") == "pending":
                self._step("semantic", state="error", error="表达样本尚未完成，可稍后继续", detail="")
            if not any(eligible(key) for key in (*self.outputs, "timeline-extract", "semantic")):
                break
        from self_judge_shadow import drain_shadow
        drain_shadow(self.plugin, self.store, self.corpus, context)
        return {}

    def run(self, context):
        self.context = context
        try:
            while True:
                self.work(context)
                context.check()
                # A resume racing the end of the scheduler either becomes work
                # in this task or observes the terminal state and starts a new
                # task. Never acknowledge it and then discard a pending output.
                with self.control_lock:
                    if self._has_work():
                        continue
                    partial = any(step["state"] == "error" for step in self.steps) or bool(self.corpus.counts()["gaps"])
                    self._set(state="paused" if self.paused_outputs else "partial" if partial else "done",
                              finishedAt=now_ms(), coverage=self._coverage())
                    return "partial" if partial or self.paused_outputs else "done"
        except Cancelled:
            if "library" in self.outputs:
                self.embedding.pause()
                self.store.update_build(state="paused")
            self.steps = [{**step, "stopping": False, **({"state": "pending", "detail": "进度已保留"} if step["state"] == "running" else {})}
                          for step in self.steps]
            self._set(state="paused", steps=self.steps, finishedAt=now_ms(), coverage=self._coverage())
            raise
        except Exception as exc:
            if "library" in self.outputs:
                self.embedding.pause()
                self.store.update_build(state="paused")
            self._report(exc)
            message = failure(exc, self.fallback)
            self.steps = [{**step, "state": "error", "error": message} if step["state"] == "running" else step
                          for step in self.steps]
            self._set(state="partial", steps=self.steps, error=message, finishedAt=now_ms(), coverage=self._coverage())
            return "partial" if any(step["state"] == "done" for step in self.steps) else "failed"
