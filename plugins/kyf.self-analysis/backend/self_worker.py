"""One resumable, cancellable local indexing worker per account.

Core owns all source access. No model, source database paths or credentials are
used by this plugin. A stopped/late page is discarded before its transaction.
Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import threading
import time

from self_contracts import PAGE_SIZE, SelfError, Stopped, now_ms
from self_samples import extract

PREPARE_TIMEOUT = 180
# Core's history/verify-many bounds: one source read checks up to this many
# conversations and references, so tails are verified in groups.
VERIFY_GROUP = 16
VERIFY_ITEMS = 500
SOURCE_FAILURES = ("account-changed", "account-unavailable", "source-changed", "core-unavailable")


class Worker:
    def __init__(self, core, store, account):
        self.core, self.store, self.account = core, store, account
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.thread = None
        self.closed = False

    def start(self, mode="incremental"):
        if mode not in ("full", "incremental", "retry"):
            raise SelfError("invalid-request", "不支持的建库方式")
        with self.lock:
            if self.closed:
                raise SelfError("stopping", "插件正在关闭")
            if self.thread and self.thread.is_alive():
                return False
            self.cancel.clear()
            self.store.begin(mode)
            self.thread = threading.Thread(target=self.run, daemon=True, name="self-analysis-index")
            self.thread.start()
            return True

    def pause(self):
        with self.lock:
            self.cancel.set()
            state = self.store.meta("build", {}).get("state")
            if state == "running":
                self.store.update_build(state="paused", error=None)

    def close(self):
        with self.lock:
            self.closed = True
            self.pause()
        if self.thread:
            self.thread.join(3)
        # A blocked Core request may still be finishing. Service closes the
        # database only after the worker has exited, so no late writes race it.
        return not self.thread or not self.thread.is_alive()

    def check(self):
        if self.cancel.is_set() or self.closed:
            raise Stopped()

    def run(self):
        try:
            self._discover()
            with self.lock:
                self.check()
                self.store.skip_unchanged()
            while True:
                self.check()
                batch = self.store.pending(VERIFY_GROUP)
                if not batch:
                    break
                # Prepare a group, verify all their stored tails in one source read, then read each.
                prepared = []
                for row in batch:
                    status = self._guarded(row["user"], self._prepare, row["user"])
                    if status is not None:
                        prepared.append((row["user"], status))
                tails = self._verify_tails([user for user, _ in prepared])
                for user, status in prepared:
                    if tails[user] == "failed":
                        self._fail(user)
                        continue
                    if tails[user] == "changed":
                        with self.lock:
                            self.check()
                            self.store.invalidate(user)
                        status = self._guarded(user, self._prepare, user)
                    if status is not None:
                        self._guarded(user, self._read, user, status)
            self.check()
            counts = self.store.counts()
            coverage = self.store.meta("coverage", {})
            partial = counts["failed"] or coverage.get("state", "complete") != "complete"
            with self.lock:
                self.check()
                self.store.update_build(state="partial" if partial else "complete", phase="done", currentUser=None,
                                        completedAt=now_ms(), error=None)
        except Stopped:
            pass
        except Exception as exc:
            with self.lock:
                if not self.cancel.is_set():
                    error = ("聊天来源已变化，未自动清空知识库。请恢复原来源后继续；如需替换，请先确认清空后重新建立。"
                             if getattr(exc, "code", None) == "source-changed" else
                             "聊天来源读取中断，已保存进度，可以继续")
                    self.store.update_build(state="failed", error=error)

    def _discover(self):
        build = self.store.meta("build", {})
        if build.get("phase") != "discovering":
            return
        cursor = build.get("cursor")
        seen = set()
        restarted = False
        while True:
            self.check()
            try:
                page = self.core.history_conversations(self.account, after=cursor, limit=PAGE_SIZE)
            except Exception as exc:
                if cursor and not restarted and getattr(exc, "code", None) in ("directory-expired", "invalid-cursor", "source-changed"):
                    with self.lock:
                        self.check()
                        self.store.restart_catalog()
                    cursor, restarted, seen = None, True, set()
                    continue
                raise
            self.check()
            source = self.store.meta("source")
            if cursor and source is not None and source != page.get("sourceFingerprint"):
                with self.lock:
                    self.check()
                    self.store.restart_catalog()
                cursor, seen = None, set()
                continue
            if not isinstance(page.get("sourceFingerprint"), str) or not page["sourceFingerprint"]:
                raise SelfError("source-unavailable", "无法确认聊天来源")
            cursor = page.get("next")
            if cursor is not None and cursor in seen:
                raise SelfError("invalid-page", "会话目录分页没有前进")
            seen.add(cursor)
            with self.lock:
                self.check()
                self.store.catalog_page(page)
            if cursor is None:
                return

    def _ready(self, user, rebuild=False):
        self.check()
        status = self.core.history_prepare(self.account, user, refresh=True, **({"rebuild": True} if rebuild else {}))
        started = time.monotonic()
        while status.get("state") != "ready" or status.get("importing") is True:
            self.check()
            if status.get("state") == "error" or time.monotonic() - started > PREPARE_TIMEOUT:
                raise SelfError("history-error", "会话读取未完成")
            self.cancel.wait(0.1)
            self.check()
            status = self.core.history_status(self.account, user)
        self.check()
        if (status.get("sourceFingerprint") != self.store.meta("source") or
                status.get("currentSourceFingerprint", status.get("sourceFingerprint")) != self.store.meta("source")):
            raise SelfError("source-changed", "聊天来源已变化")
        return status

    def _guarded(self, user, action, *args):
        """Run one conversation step; only a source-wide failure stops the build."""
        try:
            return action(*args)
        except Stopped:
            raise
        except Exception as exc:
            # Do not persist arbitrary exception text: it could contain
            # a message, path or HTTP diagnostic with private material.
            self._source_failure(exc)
            self._fail(user)
            return None

    def _source_failure(self, exc):
        code = getattr(exc, "code", "history-error")
        if code in SOURCE_FAILURES:
            with self.lock:
                self.check()
                self.store.update_build(phase="discovering", cursor=None, catalogComplete=False)
            raise SelfError(code, "聊天来源不可用或已切换，恢复连接后可继续") from None

    def _fail(self, user):
        with self.lock:
            self.check()
            self.store.finish_conversation(user, "这个会话读取失败，可以重试")

    def _prepare(self, user):
        local = self.store.conversation(user)
        # A full build rebuilds each projection once. After binding it, resume
        # must use its saved page checkpoint rather than create another epoch.
        status = self._ready(user, rebuild=bool(local["needs_rebuild"] or
                                               (self.store.meta("build", {}).get("mode") == "full" and
                                                local["source"] is None)))
        with self.lock:
            self.check()
            self.store.bind(user, status)
        return status

    def _verify_tails(self, users):
        """Check the stored tails of several conversations in as few source reads as possible.

        Incremental updates verify only a bounded tail. Every returned search
        sample is separately checked against its original source, so an old
        deleted/edited message never becomes usable merely because it is
        outside this tail. A full rebuild remains available for a full audit.
        Returns ``{user: "same" | "changed" | "failed"}``.
        """
        outcome, groups, group, size = {}, [], [], 0
        for user in users:
            self.check()
            local = self.store.conversation(user)
            after = max(local["verified_seq"], local["after_seq"] - PAGE_SIZE)
            proofs = self.store.rows("SELECT seq,msg_id,hash FROM proofs WHERE user=? AND seq>? ORDER BY seq LIMIT ?",
                                     (user, after, PAGE_SIZE))
            if not proofs:
                outcome[user] = "same"
                continue
            if group and (len(group) == VERIFY_GROUP or size + len(proofs) > VERIFY_ITEMS):
                groups.append(group)
                group, size = [], 0
            group.append((user, proofs))
            size += len(proofs)
        if group:
            groups.append(group)
        for group in groups:
            outcome.update(self._verify_group(group))
        return outcome

    def _verify_group(self, group):
        requests = [{"user": user, "items": [{"id": row["msg_id"], "hash": row["hash"]} for row in proofs]}
                    for user, proofs in group]
        verify_many = getattr(self.core, "history_verify_many", None)
        checks = {}
        if callable(verify_many):
            self.check()
            try:
                response = verify_many(self.account, requests)
            except Exception as exc:
                self._source_failure(exc)
                return {user: "failed" for user, _ in group}
            results = response.get("results") if isinstance(response, dict) else None
            for check in results if isinstance(results, list) else []:
                if isinstance(check, dict) and isinstance(check.get("user"), str):
                    checks[check["user"]] = check
        else:
            for request in requests:
                self.check()
                try:
                    checks[request["user"]] = self.core.post("history/verify", {
                        "account": self.account, "fresh": True, **request})
                except Exception as exc:
                    self._source_failure(exc)
        self.check()
        outcome = {}
        for user, proofs in group:
            check = checks.get(user)
            results = check.get("results") if isinstance(check, dict) else None
            if (not isinstance(results, list) or len(results) != len(proofs) or
                    any(not isinstance(item, dict) or item.get("id") != row["msg_id"] or item.get("state") == "unready"
                        for item, row in zip(results, proofs))):
                outcome[user] = "failed"  # the original messages cannot be checked right now
            elif any(item.get("state") != "same" for item in results):
                outcome[user] = "changed"
            else:
                outcome[user] = "same"
                with self.lock:
                    self.check()
                    self.store.verified(user, proofs[-1]["seq"])
        return outcome

    def _read(self, user, status):
        local = self.store.conversation(user)
        after, carry = local["after_seq"], json.loads(local["carry"])
        ceiling = (status.get("watermark") or {}).get("seq", status.get("readCount", 0))
        while after < ceiling:
            self.check()
            page = self.core.history_messages(self.account, user, after=after, limit=PAGE_SIZE)
            self.check()
            fresh = self.core.history_status(self.account, user)
            self.check()
            if (fresh.get("sourceFingerprint") != status.get("sourceFingerprint") or
                    fresh.get("currentSourceFingerprint", fresh.get("sourceFingerprint")) != self.store.meta("source") or
                    fresh.get("epoch") != status.get("epoch") or
                    page.get("epoch", status.get("epoch")) != status.get("epoch")):
                with self.lock:
                    self.check()
                    self.store.invalidate(user)
                raise SelfError("source-changed", "读取期间聊天来源已变化")
            rows = [row for row in page.get("messages", []) if row["seq"] <= ceiling]
            if not rows or rows[0]["seq"] <= after or any(a["seq"] >= b["seq"] for a, b in zip(rows, rows[1:])):
                raise SelfError("invalid-page", "消息分页没有前进")
            samples, carry, skipped = extract(rows, carry, user, status["sourceFingerprint"], status.get("epoch"), self.account)
            self.check()
            # pause/reset obtains this same lock. A late network response cannot
            # write after cancellation has been acknowledged.
            with self.lock:
                self.check()
                self.store.page(user, rows, samples, carry, skipped, status, expected_after=after)
            after = rows[-1]["seq"]
        self.check()
        # The source position this read reached; the next build skips the chat while
        # the directory still reports it as the newest row.
        highwater = status.get("highwater")
        with self.lock:
            self.check()
            self.store.finish_conversation(user, indexed=highwater if isinstance(highwater, str) else None)
