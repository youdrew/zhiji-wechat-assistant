"""Relationship-memory extraction, run as the memory part of 历史建档.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Moved from Core (friend_service jobs) into kyf.friend-analysis, 2026-10-13. Candidates go to
Core as pending memories with their evidence; the user confirms them in 关系记忆.
"""
from __future__ import annotations

import time

from contracts import CHAR_PER_TOKEN_DEN, CHAR_PER_TOKEN_NUM, AdvisorError, effective_model_budget, safety_margin
from friend_extract import (DEDUP_BATCH, DEDUP_SYSTEM, EXTRACT_SYSTEM, EXTRACT_VERSION, MAX_SPLIT_DEPTH,
                            dedup_prompt, existing_records, neighbours, output_tokens, parse_dedup, parse_json,
                            segment_chars, segment_prompt, segments, split_segment, validate_candidates)
from analysis_store import chain_step, content_hash, normalize_text
from friend_cache import route_equivalent

SEGMENT_RETRIES = 2
RETRY_DELAYS = (3.0, 10.0)


def output_cap(config):
    return output_tokens(effective_model_budget(config.get("contextTokens"))["contextTokens"])


def segment_limit(config):
    window = effective_model_budget(config.get("contextTokens"))["contextTokens"]
    budget = max(0, (window - output_tokens(window) - 256) * CHAR_PER_TOKEN_NUM // CHAR_PER_TOKEN_DEN)
    return segment_chars(budget - safety_margin(budget) - len(EXTRACT_SYSTEM) - 512)


class MemoryJobs:
    def __init__(self):
        self.verified = {}

    def compatible(self, database, store, user, config, fingerprint, latest, coverage):
        if latest is None:
            return False
        if latest["model_fp"] == config["modelFp"]:
            return True
        if not coverage["valid"] or not route_equivalent(latest["model_fp"], config):
            return False
        context, point = database.context(user), store.checkpoint(user)
        rows = database.context_messages(user, None)
        clear_epoch = store.state(user)["clear_epoch"]
        if (not context or context["state"] != "ready" or context.get("error") or context["source_id"] != fingerprint or
                context["read_count"] != len(rows) or point["source_fp"] != fingerprint or
                point["extract_version"] != EXTRACT_VERSION or not 0 <= point["covered_count"] <= len(rows) or
                latest["source_fp"] != fingerprint or latest["extract_version"] != EXTRACT_VERSION or
                latest["clear_epoch"] != clear_epoch or point.get("clear_epoch") != clear_epoch):
            return False
        # The ordinary coverage memo predates this compatibility path and is keyed
        # by epoch. Prove the actual prefix again when adopting an older identity.
        digest = ""
        for row in rows[:point["covered_count"]]:
            digest = chain_step(digest, row)
        if digest != point["chain_digest"]:
            return False
        # An older full job may be behind the current checkpoint. Its digest must
        # not be attached to a shorter positive prefix; start a fresh full job then.
        return not (latest["kind"] == "full" and latest["state"] in ("stopped", "error", "interrupted") and
                    0 < latest["done_messages"] < point["covered_count"])

    def reconcile_clear(self, store, user):
        """Start over when the user cleared this friend's memories since the last batch."""
        checkpoint = store.checkpoint(user)
        current = store.state(user)["clear_epoch"]
        if checkpoint is not None and checkpoint.get("clear_epoch") not in (None, current):
            store.reset_memory_progress(user)

    def coverage(self, database, store, user, fingerprint, context=None):
        """How much of the current projection the saved checkpoint provably covers."""
        context = context if context is not None else database.context(user)
        checkpoint = store.checkpoint(user)
        read = context["read_count"] if context else 0
        if checkpoint is None:
            return {"valid": False, "covered": 0, "new": read}
        if (context is None or checkpoint["source_fp"] != fingerprint or context["source_id"] != fingerprint or
                checkpoint["extract_version"] != EXTRACT_VERSION or checkpoint["covered_count"] > read):
            return {"valid": False, "covered": 0, "new": read}
        key = (database.account, user, context["epoch"], checkpoint["covered_count"], checkpoint["chain_digest"])
        if self.verified.get((database.account, user)) != key:
            digest = ""
            for row in database.context_messages(user, checkpoint["covered_count"]):
                digest = chain_step(digest, row)
            if digest != checkpoint["chain_digest"]:
                return {"valid": False, "covered": 0, "new": read}
            self.verified[(database.account, user)] = key
        return {"valid": True, "covered": checkpoint["covered_count"], "new": read - checkpoint["covered_count"]}

    def plan(self, database, store, user, config, fingerprint, rebuild=False):
        """Pending extraction for an estimate: characters, calls and messages."""
        context = database.context(user)
        if context is None or context["state"] != "ready":
            return {"chars": 0, "calls": 0, "messages": 0}
        coverage = self.coverage(database, store, user, fingerprint, context)
        latest = store.latest_job(user)
        compatible = self.compatible(database, store, user, config, fingerprint, latest, coverage)
        start = coverage["covered"] if coverage["valid"] and compatible and not rebuild else 0
        if (start and latest["model_fp"] != config["modelFp"] and latest["kind"] == "full" and
                latest["state"] in ("stopped", "error", "interrupted")):
            start = min(start, latest["done_messages"])
        rows = database.context_messages(user, context["read_count"])[start:]
        parts = segments(rows, segment_limit(config))
        chars = sum(len(EXTRACT_SYSTEM) + len(segment_prompt(part)) for part in parts)
        return {"chars": chars + chars // 5, "calls": len(parts), "messages": len(rows)}

    # -- the memory part of a run -------------------------------------------------------------------------------
    def run_inline(self, ctx, rebuild, progress, generate):
        """Extract over the projection not yet covered; ``generate(system, message, max_output)`` calls the model."""
        database, store, user, fingerprint = ctx.database, ctx.store, ctx.user, ctx.fingerprint
        self.reconcile_clear(store, user)
        coverage = self.coverage(database, store, user, fingerprint)
        checkpoint = store.checkpoint(user)
        built = bool(checkpoint and checkpoint["built"])
        latest = store.latest_job(user)
        compatible = self.compatible(database, store, user, ctx.config, fingerprint, latest, coverage)
        if compatible and latest["model_fp"] != ctx.config["modelFp"] and not rebuild:
            ctx.verify_prefix(database.context_messages(user, coverage["covered"]))
        if built and not coverage["valid"] and not rebuild:
            raise AdvisorError("rebuild-required", "记忆检查点无法核对当前历史，请在“更多”中重新分析全部")
        if built and coverage["valid"] and coverage["new"] <= 0 and compatible and not rebuild:
            return "记忆没有新内容"
        resume = latest if (latest is not None and latest["state"] in ("stopped", "error", "interrupted") and
                            compatible and (not rebuild or ctx.entry.resuming) and
                            (latest["kind"] == "full" or coverage["valid"])) else None
        kind = resume["kind"] if resume is not None else (
            "incremental" if built and coverage["valid"] and compatible and not rebuild else "full")
        job = self._start(ctx, kind, resume)
        state = self._work(ctx, job, progress, generate)
        job = store.job(job["id"])
        if state == "done":
            return "记忆 · 已处理 %d 条%s" % (job["done_messages"], "，新增 %d 条待确认" % job["candidates"]
                                            if job["candidates"] else "")
        if state in ("stopped", "interrupted", "stale"):
            raise AdvisorError("stopping", job["error"] or "已停止")
        raise AdvisorError("memory-failed", job["error"] or "记忆提取失败")

    def _fields(self, ctx, kind):
        context = ctx.database.context(ctx.user)
        coverage = self.coverage(ctx.database, ctx.store, ctx.user, ctx.fingerprint, context)
        start = coverage["covered"] if coverage["valid"] and kind == "incremental" else 0
        return {"source_fp": ctx.fingerprint, "context_epoch": context["epoch"], "extract_version": EXTRACT_VERSION,
                "model_fp": ctx.config["modelFp"], "clear_epoch": ctx.store.state(ctx.user)["clear_epoch"],
                "upto_seq": context["read_count"], "start_count": start,
                "total_messages": context["read_count"] - start, "done_messages": 0, "total_segments": None}

    def _start(self, ctx, kind, resume):
        store = ctx.store
        fields = self._fields(ctx, kind)
        if resume is None:
            return store.create_job(ctx.user, kind, None, fields)
        coverage = self.coverage(ctx.database, store, ctx.user, ctx.fingerprint)
        covered = coverage["covered"] if coverage["valid"] else 0
        if resume["kind"] == "full":
            covered = min(covered, resume["done_messages"])
            fields["total_messages"], fields["done_messages"] = fields["upto_seq"], covered
        else:
            if not coverage["valid"]:
                raise AdvisorError("rebuild-required", "记忆检查点无法核对当前历史，请重新建档")
            fields["total_messages"] = fields["upto_seq"] - resume["start_count"]
            fields["done_messages"] = max(0, covered - resume["start_count"])
        if resume["model_fp"] != ctx.config["modelFp"]:
            # A proven equivalent old execution keeps its fingerprint and progress.
            # Only the new formal job uses this run's current opaque identity.
            fields["start_count"] = resume["start_count"]
            return store.create_job(ctx.user, kind, None, fields)
        if not store.restart_job(resume["id"], fields):
            raise AdvisorError("request-conflict", "任务状态已变化，请刷新")
        return store.job(resume["id"])

    def _check(self, ctx, job):
        ctx.check()
        context = ctx.database.context(ctx.user)
        if context is None or context["epoch"] != job["context_epoch"] or context["source_id"] != job["source_fp"]:
            raise AdvisorError("stopping", "聊天历史已重新读取，请继续任务")

    def _dedup(self, ctx, job, candidates, generate):
        store = ctx.store
        records = existing_records(store.memories(ctx.user, ("pending", "confirmed")), store.exclusions(ctx.user))
        links, accepted, needs_model = [], [], []

        def link(candidate, target, relation):
            links.append({"proposal_key": candidate["proposal_key"], "text": candidate["text"],
                          "target_kind": target["kind"], "target_id": target["target"], "relation": relation})

        for candidate in candidates:
            exact = next((record for record in records
                          if normalize_text(record["text"]) == normalize_text(candidate["text"])), None)
            if exact is not None:
                link(candidate, exact, "duplicate")
                continue
            near = neighbours(candidate, records)
            if near:
                needs_model.append((candidate, near))
            else:
                accepted.append(candidate)
        for start in range(0, len(needs_model), DEDUP_BATCH):
            chunk = needs_model[start:start + DEDUP_BATCH]
            batch, union, seen = [item for item, _near in chunk], [], set()
            for _item, near in chunk:
                for record in near:
                    if record["id"] not in seen:
                        seen.add(record["id"])
                        union.append(record)
            store.update_job(job["id"], phase="checking")
            decisions = parse_dedup(generate(DEDUP_SYSTEM, dedup_prompt(batch, union), output_cap(ctx.config),
                                             "memory-dedup"), batch, union)
            for candidate, decision in zip(batch, decisions):
                relation, target = decision["relation"], decision["target"]
                if relation == "new" or (relation == "conflict" and target["kind"] != "memory"):
                    accepted.append(candidate)
                elif relation == "duplicate" or (relation == "supplement" and target["kind"] != "memory"):
                    link(candidate, target, "duplicate")
                elif relation == "supplement":
                    accepted.append({**candidate, "text": decision["merged"], "relation": "supplement",
                                     "related_id": target["target"]})
                elif relation == "conflict":
                    accepted.append({**candidate, "relation": "conflict", "related_id": target["target"]})
                else:
                    accepted.append({**candidate, "relation": "uncertain",
                                     "related_id": target["target"] if target and target["kind"] == "memory" else None})
        return links, accepted

    def _work(self, ctx, job, progress, generate):
        database, store, user = ctx.database, ctx.store, ctx.user
        try:
            self._check(ctx, job)
            rows = database.context_messages(user, job["upto_seq"])
            coverage = self.coverage(database, store, user, job["source_fp"])
            start = coverage["covered"] if coverage["valid"] else 0
            if job["kind"] == "full":
                start = min(start, job["done_messages"])
            if job["kind"] == "incremental" and not coverage["valid"]:
                raise AdvisorError("rebuild-required", "记忆检查点无法核对当前历史，请重新建档")
            checkpoint = store.checkpoint(user)
            digest = checkpoint["chain_digest"] if start and coverage["valid"] and checkpoint else ""
            first_ms = checkpoint["covered_from_ms"] if start and coverage["valid"] and checkpoint else None
            queue = [(part, 0) for part in segments(rows[start:], segment_limit(ctx.config))]
            total, empty = job["done_segments"] + len(queue), not queue
            store.update_job(job["id"], total_segments=total, phase="reading")
            covered = start
            while queue:
                part, depth = queue.pop(0)
                self._check(ctx, job)
                current = store.job(job["id"])
                progress("记忆 · %d / %d 条" % (current["done_messages"], current["total_messages"] or 0))
                began = time.monotonic()
                halves = None
                for attempt in range(SEGMENT_RETRIES + 1):
                    extracting = True
                    try:
                        store.update_job(job["id"], phase="splitting" if depth else "extracting")
                        text = generate(EXTRACT_SYSTEM, segment_prompt(part), output_cap(ctx.config), "memory")
                        extracting = False
                        store.update_job(job["id"], phase="checking")
                        candidates = validate_candidates(parse_json(text, "memories"), part, user, job["source_fp"])
                        links, accepted = self._dedup(ctx, job, candidates, generate)
                        break
                    except Exception as exc:
                        if isinstance(exc, AdvisorError) and exc.code == "stopping" or ctx.cancelled():
                            raise
                        if extracting and getattr(exc, "code", None) == "output-truncated" and depth < MAX_SPLIT_DEPTH:
                            halves = split_segment(part)
                            if halves:
                                break
                        if attempt == SEGMENT_RETRIES:
                            raise
                    store.update_job(job["id"], phase="retrying", attempts=attempt + 1)
                    ctx.wait(RETRY_DELAYS[min(attempt, len(RETRY_DELAYS) - 1)])
                    self._check(ctx, job)
                if halves:
                    queue[:0] = [(half, depth + 1) for half in halves]
                    total += 1
                    store.update_job(job["id"], total_segments=total, phase="splitting")
                    continue
                for row in part:
                    digest = chain_step(digest, row)
                covered += len(part)
                times = [row["time_ms"] for row in part if isinstance(row["time_ms"], int) and row["time_ms"] > 0]
                first_ms = first_ms or (min(times) if times else None)
                last = part[-1]
                store.update_job(job["id"], phase="saving")
                built = job["kind"] == "incremental" or not queue
                store.commit_batch(job, accepted, links, {
                    "source_fp": job["source_fp"], "extract_version": EXTRACT_VERSION, "covered_count": covered,
                    "chain_digest": digest, "last_msg_id": last["msg_id"], "last_hash": content_hash(last),
                    "covered_from_ms": first_ms, "covered_to_ms": max(times) if times else None, "built": built,
                }, {"messages": len(part), "from_ms": min(times) if times else None,
                    "to_ms": max(times) if times else None,
                    "label": "第 %d—%d 条" % (covered - len(part) + 1, covered)},
                    lambda _connection: self._check(ctx, job), ctx.task.core)
                store.record_measurement(ctx.config["modelFp"], 1, time.monotonic() - began)
            if empty and job["kind"] == "full":
                store.commit_batch(job, [], [], {
                    "source_fp": job["source_fp"], "extract_version": EXTRACT_VERSION, "covered_count": covered,
                    "chain_digest": digest, "last_msg_id": rows[-1]["msg_id"] if rows else None,
                    "last_hash": content_hash(rows[-1]) if rows else None, "covered_from_ms": first_ms,
                    "covered_to_ms": None, "built": True}, {"messages": 0, "label": None},
                    lambda _connection: None, ctx.task.core)
            store.finish_job(job["id"], "done")
            return "done"
        except Exception as exc:
            if isinstance(exc, AdvisorError) and exc.code == "stopping" or ctx.cancelled():
                state = "stopped" if ctx.stop_reason() == "stopped" else "interrupted"
                error = None if state == "stopped" else getattr(exc, "message", None)
            else:
                from contracts import friendly_error
                state, error = "error", friendly_error(exc)
            try:
                store.finish_job(job["id"], state, error)
            except Exception:
                pass
            return state
