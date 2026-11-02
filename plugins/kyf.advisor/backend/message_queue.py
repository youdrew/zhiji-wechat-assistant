"""Durable FIFO admission and background dispatch for each assistant discussion.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import threading
import time

from contracts import AdvisorError, valid_mentions, valid_request_id, valid_user_message


class MessageQueueMixin:
    def queue_get(self, account, user, agent_id, thread_id):
        with self.condition:
            database, _ = self._session_scope(account, user, agent_id, thread_id)
            return {"queue": database.queue_get(user, agent_id, thread_id)}

    def queue_enqueue(self, account, user, agent_id, thread_id, message, request_id,
                      thinking=True, mentions=None):
        message = valid_user_message(message)
        request_id = valid_request_id(request_id)
        mentions = valid_mentions(mentions)
        if type(thinking) is not bool:
            raise AdvisorError("invalid-request", "思考模式参数不正确")
        with self.condition:
            database, _ = self._session_scope(account, user, agent_id, thread_id)
            # An accepted request stays accepted after edits/removal, or after its
            # configuration changes. A lost acknowledgement must not enqueue it twice.
            existing = database.queue_find_request(user, agent_id, thread_id, request_id,
                                                   message, thinking=thinking, mentions=mentions)
            if existing is not None:
                result = {"item": existing, "queue": database.queue_get(user, agent_id, thread_id)}
                agent = self.config.agent(agent_id)
                dispatch_allowed = bool(agent and agent["enabled"])
            else:
                database, _ = self._session_scope(account, user, agent_id, thread_id, writable=True)
                self._mentioned_friends(account, user, mentions)
                result = database.queue_enqueue(user, agent_id, thread_id, message, request_id,
                                                thinking=thinking, mentions=mentions)
                dispatch_allowed = True
            if dispatch_allowed:
                self._queue_kick_locked(account, user, agent_id, thread_id)
            result = {"item": result["item"], "queue": result["queue"], "threadId": thread_id}
            if result["item"].get("runId"):
                run = database.run(result["item"]["runId"])
                if run:
                    result["run"] = self._public_run(run)
            return result

    def queue_edit(self, account, user, agent_id, thread_id, item_id, version, message):
        message = valid_user_message(message)
        with self.condition:
            database, _ = self._session_scope(account, user, agent_id, thread_id, writable=True)
            item = database.queue_edit(user, agent_id, thread_id, item_id, message, version)
            return {"item": item, "queue": database.queue_get(user, agent_id, thread_id)}

    def queue_remove(self, account, user, agent_id, thread_id, item_id, version):
        with self.condition:
            database, _ = self._session_scope(account, user, agent_id, thread_id, writable=True)
            item = database.queue_remove(user, agent_id, thread_id, item_id, version)
            self.condition.notify_all()
            return {"item": item, "queue": database.queue_get(user, agent_id, thread_id)}

    def queue_pause(self, account, user, agent_id, thread_id):
        with self.condition:
            database, _ = self._session_scope(account, user, agent_id, thread_id, writable=True)
            queue = database.queue_set_paused(user, agent_id, thread_id, True, "manual")
            self.condition.notify_all()
            return {"queue": queue}

    def queue_resume(self, account, user, agent_id, thread_id):
        with self.condition:
            database, _ = self._session_scope(account, user, agent_id, thread_id, writable=True)
            if any(run.account == account and run.thread_id == thread_id and run.worker is not None and
                   run.worker.is_alive() and (run.cancel.is_set() or run.state in {"stopped", "error"})
                   for run in self._owned_runs()):
                raise AdvisorError("run-active", "上一条任务仍在停止，请稍后继续队列")
            operation = database.session_active_operation(thread_id)
            if operation and operation["state"] == "unknown":
                raise AdvisorError("run-active", "压缩状态尚未确认，请先核对后再继续队列")
            queue = database.queue_set_paused(user, agent_id, thread_id, False)
            self._queue_kick_locked(account, user, agent_id, thread_id)
            return {"queue": queue}

    def _queue_pause_for_run_locked(self, run, reason):
        """Only queues already created by the user are affected by old run APIs."""
        self._queue_pause_existing_locked(run.database, run.user, run.agent_id, run.thread_id, reason)

    def _queue_pause_existing_locked(self, database, user, agent_id, thread_id, reason):
        if database is None:
            return
        try:
            queue = database.queue_get(user, agent_id, thread_id)
            if queue["version"] > 0:
                database.queue_set_paused(user, agent_id, thread_id, True, reason)
        except AdvisorError:
            pass  # A removed discussion/account must not be recreated by late workers.

    def _queue_after_run_locked(self, run):
        if run.state != "done":
            self._queue_pause_for_run_locked(run, "stopped" if run.cancel.is_set() or run.state == "stopped" else "failed")
        self.condition.notify_all()

    def _queue_kick_locked(self, account, user, agent_id, thread_id):
        key = (account, user, agent_id, thread_id)
        if key in self._queue_workers or self._closing or account in self._paused:
            return
        database = self.stores.account(account)
        queue = database.queue_get(user, agent_id, thread_id)
        if queue["paused"] or not queue["items"]:
            return
        worker = threading.Thread(target=self._queue_dispatch, args=(key, database), daemon=True,
                                  name="advisor-message-queue")
        self._queue_workers[key] = worker
        worker.start()

    def _queue_retire_locked(self, key):
        if self._queue_workers.get(key) is threading.current_thread():
            self._queue_workers.pop(key, None)
        self.condition.notify_all()

    def _queue_dispatch(self, key, database):
        account, user, agent_id, thread_id = key
        last_scope_check = 0.0
        try:
            while True:
                with self.condition:
                    if self._closing or account in self._paused:
                        self._queue_retire_locked(key)
                        return
                    database.session_thread(user, agent_id, thread_id)
                    # Streaming events wake the condition very often. Re-check
                    # Core identity periodically while waiting, not per token.
                    if time.monotonic() - last_scope_check >= 2:
                        database, _ = self._session_scope(account, user, agent_id, thread_id, writable=True)
                        last_scope_check = time.monotonic()
                    queue = database.queue_get(user, agent_id, thread_id)
                    if queue["paused"] or not queue["items"]:
                        self._queue_retire_locked(key)
                        return
                    operation = database.session_active_operation(thread_id)
                    if operation and operation["state"] == "unknown":
                        database.queue_set_paused(user, agent_id, thread_id, True, "unknown")
                        self._queue_retire_locked(key)
                        return
                    owned = [run for run in self._owned_runs()
                             if run.account == account and run.thread_id == thread_id]
                    # A terminal event can precede worker cleanup; never dispatch
                    # while that previous provider or compaction is still draining.
                    draining = any(run.worker is not None and run.worker.is_alive() for run in owned)
                    active = database.active_run(thread_id)
                    if draining or operation:
                        self.condition.wait(.2)
                        continue
                    if active:
                        database.queue_set_paused(user, agent_id, thread_id, True, "interrupted")
                        self._queue_retire_locked(key)
                        return
                    database, _ = self._session_scope(account, user, agent_id, thread_id, writable=True)
                    item = database.queue_claim(user, agent_id, thread_id)
                    if item is None:
                        self._queue_retire_locked(key)
                        return
                    try:
                        result = self.start(account, user, agent_id, thread_id, item["message"],
                                            item["dispatchRequestId"], thinking=item["thinking"],
                                            mentions=item["mentions"], _queue_item_id=item["id"])
                        database.queue_dispatched(user, agent_id, thread_id, item["id"], result["run"]["id"])
                        if result["run"]["state"] in {"error", "stopped"}:
                            database.queue_set_paused(user, agent_id, thread_id, True, "failed")
                    except Exception as exc:
                        # A Core task could fail after the run was durably created.
                        # Keep that failed question in history; never replay it as
                        # a fresh task when the user continues the queue.
                        run = database.find_run_by_request(thread_id, item["dispatchRequestId"])
                        if run:
                            database.queue_dispatched(user, agent_id, thread_id, item["id"], run["id"])
                            database.queue_set_paused(user, agent_id, thread_id, True, "failed")
                        else:
                            code = exc.code if isinstance(exc, AdvisorError) else None
                            reason = {"api-not-configured": "model-unavailable", "engine-unavailable": "model-unavailable",
                                      "invalid-request": "invalid-mentions", "invalid-settings": "settings-unavailable",
                                      "settings-unavailable": "settings-unavailable", "account-changed": "account-changed",
                                      "agent-disabled": "agent-unavailable", "agent-unknown": "agent-unavailable"}.get(code, "dispatch-failed")
                            database.queue_release(user, agent_id, thread_id, item["id"], reason)
                        self._queue_retire_locked(key)
                        return
        except Exception as exc:
            # Do not expose raw provider/config exceptions (which may contain secrets).
            if database is not None:
                with self.condition:
                    try:
                        reason = "account-changed" if isinstance(exc, AdvisorError) and exc.code in {
                            "account-changed", "account-paused", "account-closed"} else "unavailable"
                        database.queue_set_paused(user, agent_id, thread_id, True, reason)
                    except Exception:
                        pass
        finally:
            with self.condition:
                self._queue_retire_locked(key)

    def _queue_join_workers(self, account=None):
        with self.condition:
            self.condition.notify_all()
            workers = [worker for key, worker in self._queue_workers.items() if account is None or key[0] == account]
        for worker in workers:
            worker.join(5)
            if worker.is_alive():
                raise RuntimeError("advisor message queue did not stop")
