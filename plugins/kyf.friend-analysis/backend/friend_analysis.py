"""知己分析 v2 unified analysis: one queued run per contact, six ordered steps.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

A run prepares the chat history, summarises the recent conversations first, then
extracts memories and persona evidence over the whole readable history, labels the
chat, synthesises the persona and refreshes the overview. Every step is incremental
and skipped when its inputs did not change. Only one contact is analysed at a time;
background calls yield to the user's own assistant requests. Cancelling keeps every
committed batch; only "继续" starts again.

Moved into the kyf.friend-analysis plugin, 2026-10-13 (Apache-2.0): a run is the work of one
Core scheme task. Chat history comes from the plugin's mirror of Core's projection, memories
are submitted to Core as candidates, and model calls go through Core's gateway.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from contracts import (CHAR_PER_TOKEN_DEN, CHAR_PER_TOKEN_NUM, AdvisorError, effective_model_budget, friendly_error,
                       safety_margin)
import friend_chat
import friend_cache
import friend_overview
import friend_persona
import friend_model_batch
from friend_extract import output_tokens, segment_chars, segments, split_segment
from friend_run_store import RunStore
from analysis_store import FriendStore, chain_step, content_hash, now_ms
from kyf_plugin import Cancelled as TaskCancelled, CoreError
from kyf_plugin.models import ModelError
from memory import MemoryJobs

STEPS = (("history", "准备聊天记录"), ("recent", "近期概述"), ("archive", "历史建档"),
         ("chat", "聊天分析"), ("persona", "性格与三观分析"), ("overview", "补齐好友概述"))
STEP_TITLES = dict(STEPS)
RECENT_WINDOW = 400
AUTO_TOKEN_LIMIT = 100_000
TOKENS_PER_CHAR_LOW, TOKENS_PER_CHAR_HIGH = 0.6, 1.2
RETRIES = 2
RETRY_DELAYS = (3.0, 10.0)
POLL_SECONDS = 0.5
VERIFY_LIMIT = 500
JOIN_SECONDS = 200


def _digest(values):
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


# Usage records and developer-mode traces name each kind of call by this step label.
TRACE_SOURCES = {friend_overview.SUMMARY_SYSTEM: "friend-summary", friend_overview.NEXT_SYSTEM: "friend-next",
                 friend_chat.CHAT_SYSTEM: "friend-chat", friend_persona.SYNTH_SYSTEM: "friend-persona",
                 friend_persona.CONDENSE_SYSTEM: "friend-persona", friend_persona.AUDIT_SYSTEM: "friend-persona-audit",
                 **{system: "friend-persona-evidence" for system in friend_persona.EVIDENCE_SYSTEMS}}
# A partial run kept some results and failed others, so it is never reported as fully adopted.
TRACE_OUTCOMES = {"done": "done", "partial": "error", "error": "error", "stopped": "stopped", "interrupted": "stopped"}


def _tokens(chars):
    if chars <= 0:
        return 0, 0
    return int(chars * TOKENS_PER_CHAR_LOW), int(chars * TOKENS_PER_CHAR_HIGH) + 1


def new_steps():
    return [{"key": key, "title": title, "state": "pending", "detail": "", "done": None, "total": None,
             "unit": None, "calls": 0, "inputTokens": 0, "outputTokens": 0, "estimated": False,
             "error": None, "usedOld": False, "startedAtMs": None, "finishedAtMs": None} for key, title in STEPS]


class Cancelled(Exception):
    """The run was cancelled, superseded or its scope changed; nothing more is committed."""


class _Entry:
    __slots__ = ("run_id", "account", "user", "cancel", "reason", "mode", "completed", "resuming", "task")

    def __init__(self, run_id, account, user, mode, completed=(), resuming=False, task=None):
        self.run_id, self.account, self.user, self.mode = run_id, account, user, mode
        self.cancel = threading.Event()
        self.reason = None
        self.completed, self.resuming = set(completed), resuming
        self.task = task


class _Context:
    """Per-run working state passed between steps."""

    def __init__(self, analysis, entry):
        self.analysis = analysis
        self.entry = entry
        self.task = entry.task
        self.account, self.user = entry.account, entry.user
        self.database = analysis.accounts.account(entry.account)
        self.store = FriendStore(self.database)
        self.runs = RunStore(self.store, now_ms)
        self.steps = None
        self.config = None
        self.config_error = None
        self.runtime = None
        self.fingerprint = None
        self.recent_rows = None
        self.recent_from_projection = False
        self.recent_has_more = False
        self.failed = set()

    def rebuild_step(self, key):
        return self.entry.mode == "rebuild" and key not in self.entry.completed

    # The interface the memory module uses.
    def check(self):
        self.analysis._check(self)

    def cancelled(self):
        return self.entry.cancel.is_set()

    def wait(self, seconds):
        self.entry.cancel.wait(seconds)

    def stop_reason(self):
        return self.entry.reason

    def verify_prefix(self, rows):
        self.analysis._guard(self, rows, prefix=True)(None)


class FriendAnalysis:
    def __init__(self, plugin, accounts):
        self.plugin = plugin
        self.accounts = accounts
        self.memory = MemoryJobs()
        self.condition = threading.Condition(threading.RLock())
        self._queue = []
        self._current = None
        self._closing = False
        self._paused = set()
        self._verified = {}
        self._turn = threading.Lock()

    # -- Core views ------------------------------------------------------------------------------------
    def config(self):
        """The model service this plugin uses, in the fields the steps were written against."""
        services = self.plugin.models.services()
        if not services:
            raise AdvisorError("api-not-configured", "尚未配置生成模型，请先在设置中保存模型")
        service = services[0]
        budget = effective_model_budget(service.get("contextTokens"))
        return {**budget, "model": service["model"], "sourceId": service["id"], "configStamp": service.get("stamp"),
                "modelFp": service.get("fingerprint") or _digest([service["id"], service["model"], budget["contextTokens"]]),
                "cacheIdentity": friend_cache.model_identity(service)}

    def config_stamp(self):
        services = self.plugin.models.services()
        return services[0].get("stamp") if services else None

    def source_scope(self, account, user):
        return self.plugin.core.history_status(account, user).get("sourceFingerprint")

    def recent_rows(self, account, user, limit):
        from account import _row as mirror_row
        value = self.plugin.core.get("history/recent", account=account, user=user, limit=limit, full="1")
        rows = [dict(zip(("user", "seq", "msg_id", "sort_key", "group_chat", "side", "sender_id", "sender_name", "kind",
                          "text", "time_ms", "raw"), mirror_row(view, user))) for view in value["messages"]]
        return rows, bool(value.get("hasMoreBefore"))

    # -- public API (scope already checked by FriendService) ----------------------------------------
    def status(self, account, user, store):
        runs = RunStore(store, now_ms)
        run = runs.latest_run(user)
        result = {"run": self._public_run(run, account), "analyzed": runs.last_completed(user) is not None,
                  "configFingerprint": self.config_stamp()}
        return result

    def _public_run(self, run, account):
        if run is None:
            return None
        steps = json.loads(run["steps_json"])
        position = None
        with self.condition:
            if run["state"] == "queued":
                ids = [entry.run_id for entry in self._queue]
                position = ids.index(run["id"]) + (1 if self._current is not None else 0) if run["id"] in ids else None
        used = {"inputTokens": sum(step["inputTokens"] for step in steps),
                "outputTokens": sum(step["outputTokens"] for step in steps),
                "estimated": any(step["estimated"] for step in steps)}
        return {"id": run["id"], "state": run["state"], "trigger": run["trigger"], "steps": steps,
                "error": run["error"], "queuedAtMs": run["queued_at_ms"], "startedAtMs": run["started_at_ms"],
                "updatedAtMs": run["updated_at_ms"], "finishedAtMs": run["finished_at_ms"], "ahead": position,
                "usage": used}

    # -- Core scheme tasks ------------------------------------------------------------------------------
    def run_task(self, task):
        """Run (or continue) this friend's analysis for one Core task; returns the task's final state."""
        params = task.task.get("params") or {}
        mode = params.get("mode") if params.get("mode") in ("update", "rebuild") else "update"
        trigger = "auto" if params.get("trigger") == "auto" else "manual"
        account, user = task.account, task.user
        runs = RunStore(FriendStore(self.accounts.account(account)), now_ms)
        run = runs.run_for_request(user, task.task["id"])
        completed, resuming = (), False
        if run is None:
            run = runs.create_run(user, trigger, task.task["id"], None, new_steps(), mode)
            if run["request_id"] != task.task["id"]:
                # Another run of this friend is still open in this plugin (a lost task): close it first.
                runs.set_run(run["id"], state="interrupted")
                run = runs.create_run(user, trigger, task.task["id"], None, new_steps(), mode)
        elif run["state"] in ("stopped", "interrupted", "error", "partial"):
            completed = [step["key"] for step in json.loads(run["steps_json"]) if step["state"] in ("done", "skipped")]
            if not runs.restart_run(run["id"], new_steps()):
                raise AdvisorError("request-conflict", "任务状态已变化，请刷新")
            resuming = True
        entry = _Entry(run["id"], account, user, run["mode"], completed, resuming, task)
        stop = threading.Event()

        def watch():
            while not stop.wait(0.2):
                if task.cancelled.is_set() and not entry.cancel.is_set():
                    entry.reason = entry.reason or "stopped"
                    entry.cancel.set()
        threading.Thread(target=watch, daemon=True, name="friend-analysis-cancel").start()
        with self.condition:
            self._queue.append(entry)
        try:
            # One friend at a time: other friends' tasks wait here and say so.
            while not self._turn.acquire(timeout=0.5):
                if entry.cancel.is_set():
                    runs.set_run(entry.run_id, state="stopped")
                    return "stopped"
                task.progress(label="排队中 · 等待其他好友的分析完成")
            try:
                with self.condition:
                    self._queue.remove(entry)
                    self._current = entry
                self._execute(entry)
            finally:
                with self.condition:
                    self._current = None
                    self.condition.notify_all()
                self._turn.release()
        finally:
            stop.set()
            with self.condition:
                if entry in self._queue:
                    self._queue.remove(entry)
        state = (runs.run(entry.run_id) or {}).get("state")
        return {"done": "done", "partial": "partial", "stopped": "stopped"}.get(state, "failed")

    def active(self, account, user):
        with self.condition:
            entries = list(self._queue) + ([self._current] if self._current is not None else [])
            return any((entry.account, entry.user) == (account, user) for entry in entries)

    def close(self):
        with self.condition:
            self._closing = True
            for entry in list(self._queue) + ([self._current] if self._current is not None else []):
                entry.reason = "interrupted"
                entry.cancel.set()

    # -- execution --------------------------------------------------------------------------------
    def _check(self, ctx):
        entry = ctx.entry
        with self.condition:
            if self._closing or entry.account in self._paused:
                entry.reason = entry.reason or "interrupted"
                entry.cancel.set()
        if entry.cancel.is_set():
            raise Cancelled()
        try:
            changed = ((ctx.fingerprint is not None and self.source_scope(entry.account, entry.user) != ctx.fingerprint) or
                       (ctx.config is not None and self.config_stamp() != ctx.config["configStamp"]))
        except CoreError:
            changed = True
        if changed:
            entry.reason = "interrupted"
            entry.cancel.set()
            raise Cancelled()

    def _guard(self, ctx, rows=(), *, prefix=False):
        """Re-check the run against Core's projection inside a commit: a late answer never lands.

        A prefix guard (incremental scans) re-checks the source, the import epoch and the batch's
        latest messages; any other guard re-checks every message the result was made from.
        """
        expected = [(row["msg_id"], content_hash(row)) for row in rows]
        if prefix:
            expected = expected[-VERIFY_LIMIT:]
        initial = ctx.database.context(ctx.user)
        epoch = initial["epoch"] if initial else None

        def guard(connection):
            if ctx.config is not None and self.config_stamp() != ctx.config["configStamp"]:
                raise AdvisorError("stopping", "模型配置已变化")
            if connection is not None:
                row = connection.execute("SELECT state FROM friend_runs WHERE id=?", (ctx.entry.run_id,)).fetchone()
                if row is None or row["state"] != "running":
                    raise AdvisorError("stopping", "分析已停止")
            if ctx.entry.cancel.is_set():
                raise AdvisorError("stopping", "分析已停止")
            core = self.plugin.core
            status = core.history_status(ctx.account, ctx.user)
            if ctx.fingerprint is not None and status.get("sourceFingerprint") != ctx.fingerprint:
                raise AdvisorError("stopping", "聊天数据来源已变化")
            if prefix and status.get("epoch") != epoch:
                raise AdvisorError("stopping", "聊天历史已重新读取")
            for start in range(0, len(expected), VERIFY_LIMIT):
                chunk = expected[start:start + VERIFY_LIMIT]
                verdict = core.post("history/verify", {"account": ctx.account, "user": ctx.user,
                                                       "items": [{"id": msg_id, "hash": digest} for msg_id, digest in chunk]})
                for item in verdict["results"]:
                    # On the first import the recent source window may not yet be in the
                    # projection. A completed or reset projection must match it.
                    if (item["state"] == "missing" and not prefix and status.get("state") == "reading" and
                            status.get("epoch") == epoch):
                        continue
                    if item["state"] != "same":
                        raise AdvisorError("stopping", "分析所用聊天原文已变化")
        return guard

    def _step(self, ctx, key, **fields):
        step = next(item for item in ctx.steps if item["key"] == key)
        if fields.get("state") == "running" and step["startedAtMs"] is None:
            step["startedAtMs"] = now_ms()
        if fields.get("state") in ("done", "skipped", "failed", "stopped"):
            step["finishedAtMs"] = now_ms()
        step.update(fields)
        ctx.runs.set_run(ctx.entry.run_id, steps=ctx.steps)
        return step

    def _execute(self, entry):
        ctx = _Context(self, entry)
        try:
            run = ctx.runs.run(entry.run_id)
            if run is None or run["state"] != "queued":
                return
            ctx.steps = json.loads(run["steps_json"])
            try:
                ctx.config = self.config()
            except (AdvisorError, CoreError) as exc:
                ctx.config_error = exc.message
            ctx.runs.set_run(entry.run_id, state="running", started=True,
                             model_fp=ctx.config["modelFp"] if ctx.config else None)
            for key, _title in STEPS:
                self._check(ctx)
                self._step(ctx, key, state="running", detail="")
                try:
                    getattr(self, "_step_" + key)(ctx)
                except Cancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - a failed step is shown, later steps continue
                    if entry.cancel.is_set() or isinstance(exc, AdvisorError) and exc.code == "stopping":
                        raise Cancelled() from exc
                    ctx.failed.add(key)
                    self._step(ctx, key, state="failed", error=friendly_error(exc))
                    continue
                step = next(item for item in ctx.steps if item["key"] == key)
                if step["state"] == "running":
                    self._step(ctx, key, state="done")
            final = "done" if not ctx.failed else "partial" if len(ctx.failed) < len(STEPS) else "error"
            ctx.runs.set_run(entry.run_id, state=final, steps=ctx.steps)
        except Cancelled:
            state = {"stopped": "stopped"}.get(entry.reason, "interrupted")
            if ctx.steps is not None:
                for step in ctx.steps:
                    if step["state"] in ("running", "pending"):
                        step["state"] = "stopped" if step["state"] == "running" else "pending"
            try:
                ctx.runs.set_run(entry.run_id, state=state, steps=ctx.steps)
            except Exception:
                pass
        except Exception as exc:  # noqa: BLE001 - the run state must become visible
            try:
                ctx.runs.set_run(entry.run_id, state="error", error=friendly_error(exc), steps=ctx.steps)
            except Exception:
                pass

    def _need_model(self, ctx):
        if ctx.config is None:
            raise AdvisorError("api-not-configured", ctx.config_error or "需要配置 API 模型")

    def _budget(self, ctx, system):
        window = effective_model_budget(ctx.config.get("contextTokens"))["contextTokens"]
        budget = max(0, (window - output_tokens(window) - 256) * CHAR_PER_TOKEN_NUM // CHAR_PER_TOKEN_DEN)
        return max(1500, budget - safety_margin(budget) - len(system) - 512)

    def _generate(self, ctx, key, system, message, max_output=None, step=None, measure=None):
        """One model call through Core's gateway with this run's execution token; usage goes on the step."""
        self._need_model(ctx)
        self._check(ctx)
        try:
            generated = friend_model_batch.generate(self.plugin.models, ctx.task, {
                "system": system, "prompt": message, "jsonMode": True, "thinking": False,
                "maxOutputTokens": max_output or self._output_cap(ctx)}, step or TRACE_SOURCES.get(system) or key)
        except TaskCancelled:
            ctx.entry.reason = ctx.entry.reason or "stopped"
            ctx.entry.cancel.set()
            raise Cancelled() from None
        return self._record_generation(ctx, key, generated, measure)

    def _record_generation(self, ctx, key, generated, measure=None):
        """Called only by the run thread, including for completed speculative calls."""
        result, chars = generated.result, generated.chars
        text = result.get("text")
        step = next(item for item in ctx.steps if item["key"] == key)
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        estimated = not isinstance(usage.get("inputTokens"), int)
        step["inputTokens"] += usage["inputTokens"] if not estimated else int(chars * TOKENS_PER_CHAR_HIGH)
        step["outputTokens"] += usage.get("outputTokens") if isinstance(usage.get("outputTokens"), int) else int(
            (len(text) if isinstance(text, str) else 0) * TOKENS_PER_CHAR_HIGH)
        step["estimated"] = step["estimated"] or estimated
        step["calls"] += 1
        ctx.runs.set_run(ctx.entry.run_id, steps=ctx.steps)
        ctx.runs.record_measurement(ctx.config["modelFp"], measure or key, generated.seconds, chars)
        if not isinstance(text, str) or not text.strip():
            raise AdvisorError("invalid-output", "模型没有返回内容")
        return text

    def _retrying(self, ctx, key, action, *, split_truncation=False):
        for attempt in range(RETRIES + 1):
            self._check(ctx)
            try:
                return action()
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                if ctx.entry.cancel.is_set() or isinstance(exc, AdvisorError) and exc.code in (
                        "stopping", "api-not-configured", "output-truncated"):
                    raise
                if split_truncation and isinstance(exc, ModelError) and exc.code == "output-truncated":
                    # The SDK signals truncation before returning a generation.
                    # Reuse the scan's existing split path instead of repeating this window.
                    raise AdvisorError(exc.code, friendly_error(exc)) from exc
                if attempt == RETRIES:
                    raise
                self._step(ctx, key, detail="等待重试")
                ctx.entry.cancel.wait(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])

    # -- step 1: history ------------------------------------------------------------------------------
    def _step_history(self, ctx):
        core = self.plugin.core
        status = core.history_status(ctx.account, ctx.user)
        core.post("history/prepare", {"account": ctx.account, "user": ctx.user, "refresh": not status.get("importing")})
        row = ctx.database.sync(ctx.user, lambda: self._check(ctx))
        ctx.fingerprint = (row or {}).get("source_id") or self.source_scope(ctx.account, ctx.user)
        if row is not None and row["state"] == "ready" and row["source_id"] == ctx.fingerprint:
            rows = ctx.database._rows("SELECT * FROM context_messages WHERE user=? ORDER BY seq DESC LIMIT ?",
                                      (ctx.user, RECENT_WINDOW))
            ctx.recent_rows, ctx.recent_from_projection = list(reversed(rows)), True
            ctx.recent_has_more = row["read_count"] > len(rows)
            self._step(ctx, "history", detail="已读取 %d 条" % row["read_count"])
            return
        rows, more = self.recent_rows(ctx.account, ctx.user, RECENT_WINDOW)
        ctx.recent_rows = rows
        ctx.recent_has_more = more
        read = row["read_count"] if row else 0
        self._step(ctx, "history", detail="已取得最近 %d 条，完整历史读取中（已读取 %d 条）" % (len(rows), read))

    # -- step 2 and 6: recent summary and next step -------------------------------------------------
    def _step_recent(self, ctx):
        self._need_model(ctx)
        changed = self._recent(ctx, "recent", ctx.recent_rows or [], ctx.recent_has_more)
        if not changed:
            self._step(ctx, "recent", state="skipped", detail="最近对话没有变化")

    def _step_overview(self, ctx):
        row = ctx.database.sync(ctx.user, lambda: self._check(ctx))
        if row is None or row["state"] != "ready" or ctx.config is None:
            self._step(ctx, "overview", state="skipped", detail="已用现有结果刷新")
            return
        rows = ctx.database._rows("SELECT * FROM context_messages WHERE user=? ORDER BY seq DESC LIMIT ?",
                                  (ctx.user, RECENT_WINDOW))
        changed = self._recent(ctx, "overview", list(reversed(rows)), row["read_count"] > len(rows))
        if not changed:
            self._step(ctx, "overview", state="skipped", detail="已用现有结果刷新")

    def recent_deps(self, ctx_store, user, rows, config, budget, has_more_before=False):
        selected, complete = friend_overview.recent_sessions(rows, budget)
        if selected and has_more_before and selected[0]["msg_id"] == rows[0]["msg_id"]:
            complete = False
        deps = {"v": friend_overview.OVERVIEW_VERSION, "msgs": friend_overview.digest(selected),
                "model": config["modelFp"], "complete": complete}
        return selected, complete, deps

    def next_inputs(self, store, user, summary_payload, selected, config):
        goal = store.goal(user)
        memories = store.memories(user, ("confirmed",))
        texts = [row["text"] for row in memories][-friend_overview.MAX_MEMORIES:]
        latest = selected[-1]["side"] if selected else None
        deps = {"v": friend_overview.OVERVIEW_VERSION, "summary": _digest(summary_payload.get("summary")),
                "goal": _digest(goal["goal"]), "memories": _digest([[row["id"], row["version"]] for row in memories]),
                "latest": latest, "model": config["modelFp"]}
        return goal["goal"], texts, latest, deps

    def _recent(self, ctx, key, rows, has_more_before=False):
        """Regenerate the summary and next step only when their own inputs changed."""
        budget = self._budget(ctx, friend_overview.SUMMARY_SYSTEM)
        selected, complete, deps = self.recent_deps(ctx.store, ctx.user, rows, ctx.config, budget, has_more_before)
        if not selected:
            self._step(ctx, key, state="skipped", detail="还没有可读的聊天")
            return True
        changed = False
        existing = ctx.runs.result(ctx.user, "summary")
        force = key == "recent" and ctx.rebuild_step("recent")
        if force or existing is None or existing["deps"] != deps:
            guard = self._guard(ctx, selected)
            self._step(ctx, key, detail="生成最近对话摘要")
            text = self._retrying(ctx, key, lambda: self._generate(ctx, key, friend_overview.SUMMARY_SYSTEM,
                                                                     friend_overview.prompt(selected), 1024))
            payload = friend_overview.validate_summary(text, selected)
            payload.update({"fromMs": selected[0]["time_ms"], "toMs": selected[-1]["time_ms"],
                            "messages": len(selected), "complete": complete, "lastMsgId": selected[-1]["msg_id"],
                            "model": ctx.config.get("model")})
            self._check_recent_source(ctx, selected)
            ctx.runs.save_result(ctx.user, "summary", payload, deps, ctx.config["modelFp"], guard)
            existing, changed = ctx.runs.result(ctx.user, "summary"), True
        goal, memories, latest, next_deps = self.next_inputs(ctx.store, ctx.user, existing["payload"], selected,
                                                             ctx.config)
        current = ctx.runs.result(ctx.user, "next")
        if force or current is None or current["deps"] != next_deps:
            guard = self._guard(ctx, selected)
            self._step(ctx, key, detail="生成下一步建议")
            prompt = friend_overview.next_prompt(existing["payload"]["summary"], selected, goal, memories)
            text = self._retrying(ctx, key, lambda: self._generate(ctx, key, friend_overview.NEXT_SYSTEM, prompt, 512))
            value = friend_overview.validate_next(text, latest)
            self._check_recent_source(ctx, selected)
            if self.next_inputs(ctx.store, ctx.user, existing["payload"], selected, ctx.config)[3] != next_deps:
                raise AdvisorError("stopping", "建议所用资料已变化")
            ctx.runs.save_result(ctx.user, "next", {"next": value}, next_deps, ctx.config["modelFp"], guard)
            changed = True
        if changed:
            self._step(ctx, key, detail="已更新")
        return changed

    def _check_recent_source(self, ctx, selected):
        if ctx.recent_from_projection:
            return
        current, _more = self.recent_rows(ctx.account, ctx.user, RECENT_WINDOW)
        hashes = {row["msg_id"]: content_hash(row) for row in current}
        if any(hashes.get(row["msg_id"]) != content_hash(row) for row in selected):
            raise AdvisorError("stopping", "近期聊天原文已变化")

    # -- step 3: archive (memories and persona evidence) --------------------------------------------
    def _wait_history(self, ctx, key):
        while True:
            self._check(ctx)
            row = ctx.database.sync(ctx.user, lambda: self._check(ctx))
            if row is not None and row["source_id"] != ctx.fingerprint:
                ctx.entry.reason = "interrupted"
                raise Cancelled()
            if row is not None and row["state"] == "ready":
                return row
            if row is None or row["state"] == "error":
                raise AdvisorError("context-error", (row or {}).get("error") or "聊天记录读取失败，请重试")
            total = row["total_count"]
            self._step(ctx, key, detail="等待完整历史读取 · 已读取 %d%s 条" % (
                row["read_count"], " / %d" % total if isinstance(total, int) and total else ""))
            ctx.entry.cancel.wait(POLL_SECONDS)

    def _step_archive(self, ctx):
        row = self._wait_history(ctx, "archive")
        self._need_model(ctx)
        notes = []
        try:
            memory = self.memory.run_inline(
                ctx, ctx.rebuild_step("archive"), lambda text: self._step(ctx, "archive", detail=text),
                lambda system, message, max_output, step: self._generate(ctx, "archive", system, message, max_output,
                                                                         step=step, measure="memory"))
            notes.append(memory)
        except Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - persona evidence still runs; the step reports both
            if ctx.entry.cancel.is_set() or isinstance(exc, AdvisorError) and exc.code == "stopping":
                raise Cancelled() from exc
            ctx.failed.add("archive")
            notes.append("记忆未完成：" + friendly_error(exc))
        rows = ctx.database.context_messages(ctx.user, row["read_count"])
        persona = self._scan(ctx, "archive", "persona", rows, len(rows), self._persona_batches, self._persona_batch)
        notes.append(persona)
        if "archive" in ctx.failed:
            raise AdvisorError("partial", "；".join(notes))
        self._step(ctx, "archive", detail="；".join(notes))

    def _persona_batches(self, ctx, rows, start, end):
        # Every channel reads the same segments, so the segment limit is the smallest one.
        limit = min(segment_chars(self._budget(ctx, channel.system)) for channel in friend_persona.CHANNELS)
        return [(part, part[0]["seq"] - 1, part[-1]["seq"]) for part in segments(rows[start:end], limit)]

    def _persona_batch(self, ctx, key, part):
        self._need_model(ctx)
        self._check(ctx)
        items = []
        prompt = friend_persona.segment_prompt(part)
        channels = friend_persona.CHANNELS
        calls = [({"system": channel.system, "prompt": prompt, "jsonMode": True, "thinking": False,
                   "maxOutputTokens": min(self._output_cap(ctx), channel.out_tokens * 2)},
                  TRACE_SOURCES.get(channel.system) or key) for channel in channels]
        # Resolve the SDK's lazy client once on the run thread, then submit only
        # independent model calls. Parsing, usage and every write stay here.
        batch = friend_model_batch.ModelBatch(self.plugin.models, ctx.task, ctx.entry.cancel, calls)
        recorded, error = set(), None
        try:
            for index, channel in enumerate(channels):
                generated = batch.result(index)
                recorded.add(index)
                text = self._record_generation(ctx, key, generated)
                self._check(ctx)
                items += friend_persona.validate_evidence(friend_persona.parse_evidence(text), part, channel=channel.key)
        except BaseException as exc:
            error = exc
        finally:
            batch.close()
            # Failed windows publish nothing, but actual successful speculative
            # calls still consumed tokens. Do not hide them from step accounting.
            for index, generated in batch.completed():
                if index in recorded:
                    continue
                try:
                    self._record_generation(ctx, key, generated)
                except BaseException as exc:
                    if error is None:
                        error = exc
        if isinstance(error, TaskCancelled):
            ctx.entry.reason = ctx.entry.reason or "stopped"
            ctx.entry.cancel.set()
            raise Cancelled() from None
        if error is not None:
            raise error
        return items, sum(friend_chat.readable_text(row) is not None and row["side"] == "other" for row in part), \
            sum(row["side"] == "other" and friend_chat.readable_text(row) is None for row in part)

    def _output_cap(self, ctx):
        return output_tokens(effective_model_budget(ctx.config.get("contextTokens"))["contextTokens"])

    # -- step 4: chat analysis ---------------------------------------------------------------------
    def _step_chat(self, ctx):
        row = self._wait_history(ctx, "chat")
        self._need_model(ctx)
        rows = ctx.database.context_messages(ctx.user, row["read_count"])
        note = self._scan(ctx, "chat", "chat", rows, friend_chat.scan_end(rows), self._chat_batches, self._chat_batch)
        self._step(ctx, "chat", detail=note)

    def _chat_batches(self, ctx, rows, start, end):
        limit = segment_chars(self._budget(ctx, friend_chat.CHAT_SYSTEM))
        result = []
        for batch in friend_chat.batches(rows, start, end, limit):
            result.append((batch, batch[0]["start"], batch[-1]["end"]))
        return result

    def _chat_batch(self, ctx, key, batch, rows, answers=None):
        # The caller retains accepted fields across the existing three attempts.
        # An incomplete window never advances the scan's atomic checkpoint.
        answers = {} if answers is None else answers
        first = batch[0]["start"]
        context = [block for block in friend_chat.blocks(rows[:first])][-friend_chat.CONTEXT_BLOCKS:]
        coverage = friend_chat.answer_coverage(answers, batch)
        if coverage["missing"]:
            repairing = any(answers.values())
            prompt, numbers = (friend_chat.repair_prompt(batch, context, answers) if repairing else
                               friend_chat.batch_prompt(batch, context))
            cap = min(self._output_cap(ctx), friend_chat.estimate_output_tokens(len(batch)) * 2)
            system = friend_chat.repair_system(coverage["missing"]) if repairing else friend_chat.CHAT_SYSTEM
            text = self._generate(ctx, key, system, prompt, max(512, cap), step="friend-chat")
            merged = friend_chat.merge_answers(answers, friend_chat.parse(text), batch, numbers,
                                              missing=coverage["missing"] if repairing else None)
            answers.clear()
            answers.update(merged)
        labelled = friend_chat.validate_answers(answers, batch)
        return labelled, sum(1 for block in batch if block["text"]), sum(1 for block in batch if not block["text"])

    # -- shared incremental scan ----------------------------------------------------------------------
    def scan_state(self, account, user, store, step, rows, end, model_fp, fingerprint, config=None):
        """Which generation to continue and from where; ``rebuild`` when the published one is unusable."""
        runs = RunStore(store, now_ms)
        version = friend_chat.CHAT_VERSION if step == "chat" else friend_persona.PERSONA_VERSION
        result = {"published": None, "continue": None, "repair": None, "start": 0, "rebuild": False, "new": end}

        def source_valid(scan, *, legacy=False):
            versions = (version, friend_chat.LEGACY_CHAT_VERSION) if legacy and step == "chat" else (version,)
            if (scan["source_fp"] != fingerprint or scan["version"] not in versions or
                    not 0 <= scan["covered_count"] <= end):
                return False
            if step == "chat" and not friend_cache.chat_prefix_closed(rows, scan["covered_count"]):
                return False
            digest = ""
            for row in rows[:scan["covered_count"]]:
                digest = chain_step(digest, row)
            return digest == scan["chain_digest"]
        scans = runs.scans(user, step)
        published = next((scan for scan in reversed(scans) if scan["published"]), None)
        result["published"] = published
        usable = [scan for scan in scans if scan["model_fp"] == model_fp and source_valid(scan)]
        if step == "chat":
            usable = [scan for scan in usable if friend_cache.chat_payload_valid(
                runs.chat_blocks(user, scan["generation"]), rows, scan["covered_count"])]
        # Exact-current generations take precedence, even when an older equivalent
        # generation covers more messages. Legacy equivalence alone is insufficient.
        if not usable and config and config.get("cacheIdentity"):
            context = store.database.context(user)
            if (context and context["state"] == "ready" and not context.get("error") and
                    context["source_id"] == fingerprint and
                    context["read_count"] == len(rows)):
                for scan in scans:
                    if not source_valid(scan) or not friend_cache.route_equivalent(scan["model_fp"], config):
                        continue
                    if step == "chat":
                        valid = friend_cache.chat_payload_valid(runs.chat_blocks(user, scan["generation"]), rows,
                                                                scan["covered_count"])
                    else:
                        valid = friend_cache.persona_payload_valid(runs.persona_evidence(user, scan["generation"]), rows,
                                                                   scan["covered_count"])
                    if valid:
                        usable.append(scan)
        if step == "chat":
            context = store.database.context(user)
            alias_scope = bool(context and context["state"] == "ready" and not context.get("error") and
                               context["source_id"] == fingerprint and context["read_count"] == len(rows))
            repairable = [scan for scan in scans if source_valid(scan, legacy=True) and
                          (scan["model_fp"] == model_fp or alias_scope and config and
                           friend_cache.route_equivalent(scan["model_fp"], config))]
            result["repair"] = max(repairable, key=lambda scan: (scan["covered_count"], scan["generation"]), default=None)
        if published is not None:
            result["rebuild"] = published not in usable
            if published in usable:
                result.update({"continue": published, "start": published["covered_count"],
                               "new": end - published["covered_count"]})
        building = next((scan for scan in reversed(usable) if not scan["published"]), None)
        if building is not None:
            result.update({"continue": building, "start": building["covered_count"],
                           "new": end - building["covered_count"]})
        return result

    def _scan(self, ctx, key, step, rows, end, plan, run_batch):
        title = "聊天分析" if step == "chat" else "性格与三观证据"
        state = self.scan_state(ctx.account, ctx.user, ctx.store, step, rows, end, ctx.config["modelFp"],
                                ctx.fingerprint, ctx.config)
        scan = state["continue"]
        repair_answers = {}
        if step == "chat" and state.get("repair") and not ctx.rebuild_step(key):
            old = state["repair"]
            repair_answers = friend_cache.chat_repair_answers(
                ctx.runs.chat_blocks(ctx.user, old["generation"]), rows, old["covered_count"],
                legacy=old["version"] == friend_chat.LEGACY_CHAT_VERSION)
        if ctx.rebuild_step(key) and (not ctx.entry.resuming or scan is None or scan["published"]):
            scan = None
        if scan is not None and scan["model_fp"] != ctx.config["modelFp"]:
            ctx.verify_prefix(rows[:scan["covered_count"]])
        if scan is not None and state["start"] >= end and scan["built"]:
            return title + "没有新内容"
        if scan is None:
            version = friend_chat.CHAT_VERSION if step == "chat" else friend_persona.PERSONA_VERSION
            generation = ctx.runs.begin_scan(ctx.user, step, {"source_fp": ctx.fingerprint, "version": version,
                                                              "model_fp": ctx.config["modelFp"]})
            start, digest = 0, ""
        else:
            generation, start, digest = scan["generation"], scan["covered_count"], scan["chain_digest"]
        if state["published"] is not None and (scan is None or not scan["published"]):
            self._step(ctx, key, usedOld=True)
        queue = plan(ctx, rows, start, end)
        total = end - start
        covered, built = start, False
        while queue:
            self._check(ctx)
            batch, batch_start, batch_end = queue.pop(0)
            self._step(ctx, key, detail="%s · %d / %d 条" % (title, covered - start, total), done=covered - start,
                       total=total, unit="条")
            try:
                if step == "chat":
                    answers = {block["id"]: dict(repair_answers[block["id"]]) for block in batch
                               if block["id"] in repair_answers}
                    try:
                        labelled, processed, excluded = self._retrying(
                            ctx, key, lambda: run_batch(ctx, key, batch, rows, answers), split_truncation=True)
                    finally:
                        # Keep accepted fields when an output truncation splits
                        # this window. Nothing is persisted until a full child passes.
                        repair_answers.update(answers)
                else:
                    labelled, processed, excluded = self._retrying(
                        ctx, key, lambda: run_batch(ctx, key, batch), split_truncation=True)
            except AdvisorError as exc:
                halves = self._split(step, batch) if exc.code == "output-truncated" else None
                if not halves:
                    raise
                queue[:0] = halves
                continue
            for row in rows[covered:batch_end]:
                digest = chain_step(digest, row)
            if step == "chat":
                excluded += batch_start - covered  # source barriers have no model target
            times = [row["time_ms"] for row in rows[batch_start:batch_end]
                     if isinstance(row.get("time_ms"), int) and row["time_ms"] > 0]
            ctx.runs.commit_scan(ctx.user, step, generation, labelled, {
                "start": covered, "covered": batch_end, "digest": digest, "processed": processed,
                "excluded": excluded, "from_ms": min(times) if times else None, "to_ms": max(times) if times else None,
                "built": not queue and batch_end >= end}, self._guard(ctx, rows[:batch_end], prefix=True))
            covered, built = batch_end, not queue and batch_end >= end
        if step == "chat" and covered < end:
            # Invalid sender/time rows are source barriers, not model targets.
            # Account for a trailing barrier even when there were no chat blocks.
            if friend_chat.blocks(rows[covered:end]):
                raise AdvisorError("incomplete-chat", "仍有聊天块未完成标注，可以继续")
            for row in rows[covered:end]:
                digest = chain_step(digest, row)
            ctx.runs.commit_scan(ctx.user, step, generation, [], {
                "start": covered, "covered": end, "digest": digest, "excluded": end - covered, "built": True},
                self._guard(ctx, rows[:end], prefix=True))
            covered, built = end, True
        if covered >= end and not built and (scan is None or not scan["published"]):
            ctx.runs.commit_scan(ctx.user, step, generation, [], {"start": covered, "covered": covered,
                                                                   "digest": digest, "built": True},
                                 self._guard(ctx, rows[:covered], prefix=True))
        return "%s · 已处理 %d 条" % (title, total)

    @staticmethod
    def _split(step, batch):
        if step == "chat":
            cuts = [index for index, block in enumerate(batch) if index and block["reply_to"] is None]
            if not cuts:
                return None
            middle = cuts[len(cuts) // 2]
            return [(part, part[0]["start"], part[-1]["end"]) for part in (batch[:middle], batch[middle:])]
        halves = split_segment(batch)
        return [(part, part[0]["seq"] - 1, part[-1]["seq"]) for part in halves] if halves else None

    # -- step 5: persona synthesis ------------------------------------------------------------------
    @staticmethod
    def persona_deps(scan, evidence, model_fp):
        return {"v": friend_persona.PERSONA_VERSION, "gen": scan["generation"],
                "covered": scan["covered_count"], "digest": scan["chain_digest"],
                "evidence": _digest(sorted(item["id"] for item in evidence)), "model": model_fp}

    def _step_persona(self, ctx):
        if ctx.runs.completed_scan(ctx.user, "persona") is None:
            self._step(ctx, "persona", state="skipped", detail="性格与三观证据尚未完成，暂不综合")
            return
        self._need_model(ctx)
        rows = ctx.database.context_messages(ctx.user, None)
        state = self.scan_state(ctx.account, ctx.user, ctx.store, "persona", rows, len(rows), ctx.config["modelFp"],
                                ctx.fingerprint, ctx.config)
        scan = state["continue"]
        if scan is None or not scan["built"]:
            self._step(ctx, "persona", state="skipped", usedOld=True, detail="新的性格与三观证据尚未完成，保留原有结果")
            return
        evidence = ctx.runs.persona_evidence(ctx.user, scan["generation"])
        deps = self.persona_deps(scan, evidence, ctx.config["modelFp"])
        existing = ctx.runs.result(ctx.user, "persona")
        if existing is not None and existing["deps"] == deps:
            self._step(ctx, "persona", state="skipped", detail="性格与三观证据没有变化")
            return
        if "archive" in ctx.failed:
            self._step(ctx, "persona", usedOld=True)
        guard = self._guard(ctx, rows[:scan["covered_count"]], prefix=True)
        digest = ""
        for row in rows[:scan["covered_count"]]:
            digest = chain_step(digest, row)
        if digest != scan["chain_digest"]:
            raise AdvisorError("stopping", "性格与三观证据所用原文已变化")
        payload = self._synthesize(ctx, scan, evidence, existing["payload"] if existing else None)
        state = self.scan_state(ctx.account, ctx.user, ctx.store, "persona", rows, len(rows), ctx.config["modelFp"],
                                ctx.fingerprint, ctx.config)
        payload["historyComplete"] = state["continue"] is not None and state["continue"]["built"] and state["new"] == 0
        ctx.runs.save_result(ctx.user, "persona", payload, deps, ctx.config["modelFp"], guard)
        self._step(ctx, "persona", detail="已综合 %d 条证据" % len(evidence))

    def _synth_call(self, ctx, dims, blocks):
        prompt = "dims: %s\n\n%s" % (", ".join(dims), "\n\n".join(blocks))
        text = self._retrying(ctx, "persona", lambda: self._generate(ctx, "persona", friend_persona.SYNTH_SYSTEM,
                                                                       prompt, self._output_cap(ctx)))
        return friend_persona.parse_synthesis(text)

    def _synthesize(self, ctx, scan, evidence, previous=None):
        by_id = {item["id"]: item for item in evidence}
        pool = [item for item in evidence if item["dim"] in friend_persona.SYNTH_DIMS and
                not (item["dim"] in friend_persona.VALUE_DIMS and item["lean"] == "selfReport")]
        budget = self._budget(ctx, friend_persona.SYNTH_SYSTEM)
        groups, oversized = friend_persona.plan_groups(pool, budget)
        results = {}
        for dims in groups:
            families = {friend_persona.GROUP_OF[dim] for dim in dims}
            detail = "、".join(friend_persona.FAMILY_LABELS[family] for family in sorted(
                families, key=lambda family: list(friend_persona.GROUPS).index(family)))
            self._step(ctx, "persona", detail="综合判断 · " + detail)
            label_map, blocks, number = {}, [], 0
            for dim in dims:
                items = [item for item in pool if item["dim"] == dim]
                lines = []
                for item in items:
                    number += 1
                    label_map["e%d" % number] = item["id"]
                    lines.append(friend_persona.render_item("e%d" % number, item))
                blocks.append(friend_persona.block_for(dim, lines, items))
            results.update(friend_persona.validate_synthesis(self._synth_call(ctx, dims, blocks), dims,
                                                             label_map, by_id))
        for dim in oversized:
            # Every item is read: chunks are condensed first, then the summaries are judged together.
            self._step(ctx, "persona", detail="压缩证据 · " + friend_persona.LABELS.get(dim, dim))
            items = [item for item in pool if item["dim"] == dim]
            label_map, lines, number = {}, [], 0
            for index, chunk in enumerate(friend_persona.chunk_items(items, budget // 2), 1):
                labels, chunk_lines = {}, []
                for item in chunk:
                    number += 1
                    labels["e%d" % number] = item["id"]
                    chunk_lines.append(friend_persona.render_item("e%d" % number, item))
                text = self._retrying(ctx, "persona", lambda chunk_lines=chunk_lines, chunk=chunk: self._generate(
                    ctx, "persona", friend_persona.CONDENSE_SYSTEM,
                    friend_persona.block_for(dim, chunk_lines, chunk), 1024))
                condensed = friend_persona.parse_condensed(text, labels)
                stamps = [item["first_ms"] for item in chunk if item["first_ms"]]
                lines.append("c%d [%s—%s] 小结：%s" % (index, friend_persona._ms_month(min(stamps)) if stamps else "时间未知",
                                                     friend_persona._ms_month(max(stamps)) if stamps else "时间未知",
                                                     condensed["summary"]))
                for label in condensed["keep"]:
                    label_map[label] = labels[label]
                    lines.append("  " + friend_persona.render_item(label, by_id[labels[label]]))
            block = friend_persona.block_for(dim, lines, items)
            results.update(friend_persona.validate_synthesis(self._synth_call(ctx, [dim], [block]), [dim],
                                                             label_map, by_id))
        removed = self._audit(ctx, results, by_id, budget)
        counted = friend_persona.positions(evidence)
        reports = friend_persona.value_reports(evidence)
        cited = {identifier for value in results.values() for identifier in value["support"] + value["counter"]}
        cited.update(identifier for report in reports.values() for identifier in report["evidence"])
        dimensions = []
        for key in friend_persona.CHART_DIMS:
            item = results.get(key) or friend_persona.unknown(key)
            counted_key = counted[key]
            # Only an audited inference gets a point; its place is counted from every lean.
            known = item["confidence"] != "无法判断"
            entry = dict(item, counts=counted_key["counts"], years=counted_key["years"])
            if key in friend_persona.TYPES:
                entry.update({"position": None, "types": counted_key["types"], "dominant": counted_key["dominant"]})
            else:
                entry["position"] = counted_key["position"] if known else None
            dimensions.append(entry)
        moved = friend_persona.drift(previous.get("dimensions") if previous else None, dimensions)
        for entry in dimensions:
            # 低置信折叠：把握较低的维度标为“参考”，前端与高置信分开呈现。
            entry["tier"] = "reference" if entry["confidence"] == "把握较低" else "main"
        values = [{"id": key, "selfReport": reports.get(key),
                   "inference": results.get(key) or friend_persona.unknown(key)}
                  for key in friend_persona.VALUE_DIMS]
        return {
            "dimensions": dimensions, "selfReports": friend_persona.self_reports(evidence), "values": values,
            "drift": moved,
            "coverage": {"fromMs": scan["from_ms"], "toMs": scan["to_ms"], "processed": scan["processed"],
                         "excluded": scan["excluded"], "evidence": len(evidence), "cited": len(cited)},
            "removed": removed, "model": ctx.config.get("model"),
        }

    def _audit(self, ctx, results, by_id, budget):
        drafts = [value for value in results.values() if value["label"] is not None]
        chunks, current, used = [], [], 0
        for draft in drafts:
            size = len(friend_persona.audit_prompt([draft], by_id)) + 1
            if current and used + size > budget:
                chunks.append(current)
                current, used = [], 0
            current.append(draft)
            used += size
        if current:
            chunks.append(current)
        rejected = set()
        for chunk in chunks:
            self._step(ctx, "persona", detail="逐项核查")
            text = self._retrying(ctx, "persona", lambda chunk=chunk: self._generate(
                ctx, "persona", friend_persona.AUDIT_SYSTEM, friend_persona.audit_prompt(chunk, by_id), 2048))
            for identifier, (passed, _reason) in friend_persona.parse_audit(text, chunk).items():
                if not passed:
                    rejected.add(identifier)
        for identifier in rejected:
            results[identifier] = friend_persona.unknown(identifier, "核查未通过，暂不判断")
        return len(rejected)

    # -- estimates and the automatic start -----------------------------------------------------------
    def _seconds(self, runs, model_fp, step, chars):
        measured = runs.measurement(model_fp, step)
        if not measured or measured["input_chars"] <= 0 or chars <= 0:
            return None
        rate = measured["seconds"] / measured["input_chars"]
        return [round(rate * chars * 0.7), round(rate * chars * 1.6) + 1]

    def estimate(self, account, user, store, mode="update"):
        """Token and time ranges per step from local counts only; no model call."""
        database = store.database
        if mode not in ("update", "rebuild"):
            raise AdvisorError("invalid-request", "分析类型不正确")
        rebuild = mode == "rebuild"
        runs = RunStore(store, now_ms)
        try:
            config = self.config()
        except (AdvisorError, CoreError) as exc:
            return {"configured": False, "error": exc.message}
        context = database.sync(user)
        fingerprint = (context or {}).get("source_id")
        ready = bool(context and context["state"] == "ready" and context["source_id"] == fingerprint)
        steps, needs = [], {"rebuild": [], "first": runs.last_completed(user) is None}

        def add(key, chars, out_tokens, measure_key, unit_count=None):
            low, high = _tokens(chars)
            steps.append({"key": key, "title": STEP_TITLES[key], "inputChars": chars,
                          "tokens": [low + out_tokens // 2, high + out_tokens], "count": unit_count,
                          "seconds": self._seconds(runs, config["modelFp"], measure_key, chars) if chars else [0, 0]})

        rows = database.context_messages(user, context["read_count"]) if context else []
        recent_source = rows[-RECENT_WINDOW:]
        budget_probe = _Probe(config)
        selected, _complete, deps = self.recent_deps(store, user, recent_source, config,
                                                     self._budget(budget_probe, friend_overview.SUMMARY_SYSTEM),
                                                     len(rows) > len(recent_source))
        summary = runs.result(user, "summary")
        recent_chars = 0
        if selected and (rebuild or summary is None or summary["deps"] != deps):
            recent_chars = len(friend_overview.SUMMARY_SYSTEM) + len(friend_overview.prompt(selected))
        if selected:
            current = runs.result(user, "next")
            next_deps = self.next_inputs(store, user, summary["payload"], selected, config)[3] if summary else None
            if recent_chars or current is None or current["deps"] != next_deps:
                recent_chars += len(friend_overview.NEXT_SYSTEM) + 1500
        add("recent", recent_chars, 900 if recent_chars else 0, "recent")
        result = {"configured": True, "historyReady": ready, "readCount": context["read_count"] if context else 0,
                  "totalCount": context["total_count"] if context else None, "steps": steps, "needs": needs}
        if not ready:
            result.update({"full": None, "recent": self._sum(steps)})
            return result
        memory = self.memory.plan(database, store, user, config, fingerprint, rebuild=rebuild)
        add("archive", memory["chars"], memory["calls"] * 700, "memory", memory["messages"])
        for key, step, end in (("archive", "persona", len(rows)), ("chat", "chat", friend_chat.scan_end(rows))):
            state = self.scan_state(account, user, store, step, rows, end, config["modelFp"], fingerprint, config)
            if state["rebuild"] and (state["continue"] is None or not state["continue"]["published"]):
                needs["rebuild"].append(step)
            start = state["start"] if state["continue"] is not None and not rebuild else 0
            if step == "persona":
                # Cost guardrail: input and output both scale with the channel count.
                channels = friend_persona.CHANNELS
                budget = min(self._budget(budget_probe, channel.system) for channel in channels)
                parts = segments(rows[start:end], segment_chars(budget))
                out_low = sum(channel.out_tokens for channel in channels) // 2
                out_high = sum(channel.out_tokens for channel in channels)
                chars = sum(len(channel.system) + len(friend_persona.segment_prompt(part))
                            for part in parts for channel in channels)
                low, high = _tokens(chars)
                extra = steps.pop()
                extra["tokens"] = [extra["tokens"][0] + low + len(parts) * out_low,
                                   extra["tokens"][1] + high + len(parts) * out_high]
                extra["count"] = (extra["count"] or 0) + (end - start)
                persona_seconds = self._seconds(runs, config["modelFp"], "archive", chars)
                extra["seconds"] = self._add_seconds(extra["seconds"], persona_seconds) if chars else extra["seconds"]
                steps.append(extra)
                # The channel scan covers the extended chart dimensions and the five narrative values topics.
                persona_evidence = len(parts) * 12
            else:
                system = friend_chat.CHAT_SYSTEM
                found = [block for block in friend_chat.blocks(rows[:end]) if block["start"] >= start]
                chars = sum(len(system) + 40 + sum(min(len(row.get("text") or ""), 1500) + 40 for row in block["rows"])
                            for block in found) if found else 0
                add("chat", chars, friend_chat.estimate_output_tokens(len(found)) if found else 0, "chat",
                    end - start)
        scan = runs.published_scan(user, "persona")
        persona_chars = 0
        persona_result = runs.result(user, "persona")
        if (rebuild or persona_evidence or scan is None or persona_result is None or
                persona_result["model_fp"] != config["modelFp"]):
            persona_chars = (persona_evidence + (len(runs.persona_evidence(user, scan["generation"])) if scan else 0)) * 220
            persona_chars = persona_chars * 2 + len(friend_persona.SYNTH_SYSTEM) + len(friend_persona.AUDIT_SYSTEM)
        add("persona", persona_chars, 4000 if persona_chars else 0, "persona")
        result["full"] = self._sum(steps)
        result["recent"] = self._sum(steps[:1])
        return result

    @staticmethod
    def _add_seconds(left, right):
        if left is None or right is None:
            return None
        return [left[0] + right[0], left[1] + right[1]]

    def _sum(self, steps):
        tokens = [sum(step["tokens"][0] for step in steps), sum(step["tokens"][1] for step in steps)]
        seconds = [0, 0]
        for step in steps:
            if step["tokens"][1] == 0:
                continue
            seconds = self._add_seconds(seconds, step["seconds"])
        return {"tokens": tokens, "seconds": seconds}

    def maybe_auto(self, account, user, store):
        """After the open-time history refresh: whether to continue an analysed contact automatically."""
        runs = RunStore(store, now_ms)
        latest = runs.latest_run(user)
        if (latest is None or latest["state"] != "done" or runs.last_completed(user) is None or
                self.active(account, user)):
            return None
        estimate = self.estimate(account, user, store)
        if not estimate.get("configured") or not estimate.get("historyReady") or estimate["needs"]["rebuild"]:
            return None
        full = estimate["full"]
        if full is None or full["tokens"][1] == 0 or full["tokens"][1] > AUTO_TOKEN_LIMIT:
            return None
        return True


class _Probe:
    """Just enough of a run context for budget arithmetic."""

    def __init__(self, config):
        self.config = config
