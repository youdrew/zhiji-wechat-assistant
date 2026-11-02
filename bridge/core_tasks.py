"""Task lifecycle: executions, tokens, scheme conflicts, queueing and the stop sequence.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from functools import wraps
import secrets
import threading
import time
import uuid

from core_plugin_process import PluginUnavailable
from core_store import ACTIVE, KINDS, TERMINAL, now_ms

STOP_GRACE = 10.0
FINISH_STATES = ("done", "partial", "failed")


class TaskError(Exception):
    def __init__(self, code, message, status=400, detail=None):
        super().__init__(message)
        self.code, self.message, self.status, self.detail = code, message, status, detail


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def serialized(method):
    """Keep execution validation and the state change it authorizes indivisible."""
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return call


class TaskManager:
    def __init__(self, store, supervisor, registry):
        self.store = store
        self.supervisor = supervisor
        self.registry = registry
        self.lock = threading.RLock()
        self.live = {}          # token hash -> execution binding
        self.deadlines = {}     # task id -> (monotonic deadline, plugin id)
        self.owners = {}        # task id -> (execution id, process token); never exposed to plugins
        self.blocked_accounts = set()  # Account cleanup remains closed until the next Core session.
        self.token_listeners = []
        self.change_listeners = []
        # Wired by Core (core_services): one "task" lease per running task so the
        # shared Core does not idle-exit under continuing work. Optional for tests.
        self.leases = None
        store.interrupt_unfinished()
        supervisor.on("task.ended", self._ended)
        supervisor.on_exit(self._plugin_exit)
        supervisor.add_ticker(self.tick)

    def on_token(self, listener):
        """``listener("add", hash, binding)`` / ``listener("revoke", hash, binding)`` for the model gateway."""
        self.token_listeners.append(listener)

    def on_change(self, listener):
        self.change_listeners.append(listener)

    def _notify(self, task):
        for listener in list(self.change_listeners):
            try:
                listener(task)
            except Exception:
                pass

    # Creation -------------------------------------------------------------------------------------------------------
    def create(self, plugin, *, kind, account, user=None, scheme=None, title, params=None, conflict=None,
               from_plugin=False):
        manifest = self.registry.get(plugin)
        if manifest is None or not self.registry.enabled(plugin):
            raise TaskError("plugin-unavailable", "插件不存在或已停用", 404)
        if kind not in KINDS:
            raise TaskError("invalid-request", "任务类型不正确")
        if kind == "scheme" and not any(item["id"] == scheme for item in manifest["contributes"].get("schemes", [])):
            raise TaskError("invalid-request", "分析方案不存在")
        with self.lock:
            self._require_account(account)
            after = None
            if kind == "scheme":
                running = self.store.tasks(account=account, user=user, kind="scheme", states=ACTIVE, limit=500)
                if running:
                    blocking = next((item for item in running if item["state"] in ("running", "stopping")), running[0])
                    if conflict == "stop-other":
                        self.stop(blocking["id"])
                    elif conflict == "queue":
                        after = running[0]["id"]
                    else:
                        raise TaskError("scheme-busy", "该好友已有分析方案正在运行", 409, blocking)
            task = self.store.insert_task({
                "id": uuid.uuid4().hex, "plugin": plugin, "plugin_version": manifest["version"], "kind": kind,
                "account": account, "user": user, "scheme": scheme, "title": title[:80], "params": params or {},
                "state": "queued", "after_task": after})
            if after is not None:
                self._notify(task)
                return {"task": task}
            if after is None and kind == "scheme" and self._blocked_by_stopping(account, user, task["id"]):
                self._notify(task)
                return {"task": task}
            return self._start(task, deliver=not from_plugin)

    def _blocked_by_stopping(self, account, user, task_id):
        stopping = [item for item in self.store.tasks(account=account, user=user, kind="scheme",
                                                      states=("running", "stopping"), limit=500) if item["id"] != task_id]
        if stopping:
            self.store.update_task(task_id, after_task=stopping[0]["id"])
            return True
        return False

    def _start(self, task, deliver=True):
        self._require_account(task["account"])
        if task["kind"] == "scheme" and self._blocked_by_stopping(task["account"], task["user"], task["id"]):
            return {"task": self.store.task(task["id"])}
        try:
            process = self.supervisor.ensure(task["plugin"])
        except PluginUnavailable as exc:
            return {"task": self._close(task["id"], "interrupted", {"code": exc.code, "message": exc.message})}
        token = secrets.token_urlsafe(32)
        digest = token_hash(token)
        execution_id = uuid.uuid4().hex
        generation = self.store.insert_execution(execution_id, task["id"], digest)
        binding = {"execution": execution_id, "generation": generation, "task": task["id"], "plugin": task["plugin"],
                   "pluginVersion": task["pluginVersion"], "account": task["account"], "user": task["user"],
                   "kind": task["kind"], "scheme": task["scheme"]}
        task = self.store.update_task(task["id"], state="running", started_at_ms=now_ms(), error=None,
                                      finished_at_ms=None, after_task=None)
        with self.lock:
            self.live[digest] = binding
            self.owners[task["id"]] = (execution_id, process.token)
        if self.leases is not None:
            self.leases.acquire("task", task["id"])
        for listener in list(self.token_listeners):
            listener("add", digest, binding)
        result = {"task": task, "execution": {"id": execution_id, "generation": generation, "token": token}}
        if deliver:
            try:
                process.send({"type": "task.start", "task": self._task_message(task),
                              "execution": result["execution"]})
            except PluginUnavailable as exc:
                self._close(task["id"], "interrupted", {"code": exc.code, "message": exc.message})
                result["task"] = self.store.task(task["id"])
            result = {"task": result["task"]}
        self._notify(result["task"])
        return result

    @staticmethod
    def _task_message(task):
        return {key: task[key] for key in ("id", "kind", "account", "user", "scheme", "title", "params")}

    # Tokens ---------------------------------------------------------------------------------------------------------
    def verify(self, token, plugin, task_id=None):
        """The live execution of ``token`` for ``plugin``; raises 409 execution-revoked otherwise."""
        if not isinstance(token, str) or not token:
            raise TaskError("execution-required", "缺少执行令牌", 401)
        with self.lock:
            binding = self.live.get(token_hash(token))
        if binding is None or binding["plugin"] != plugin or (task_id is not None and binding["task"] != task_id):
            raise TaskError("execution-revoked", "该任务执行已结束或已被停止", 409)
        return binding

    def _revoke(self, task_id):
        with self.lock:
            revoked = [(digest, binding) for digest, binding in self.live.items() if binding["task"] == task_id]
            for digest, _binding in revoked:
                self.live.pop(digest, None)
        for digest, binding in revoked:
            self.store.end_execution(binding["execution"])
            for listener in list(self.token_listeners):
                listener("revoke", digest, binding)
        return revoked

    @contextmanager
    def submission(self, token, plugin, task_id=None):
        """A bounded Core write commits before cancellation, or sees the revoked execution."""
        with self.lock:
            yield self.verify(token, plugin, task_id)

    # Progress and terminal states ------------------------------------------------------------------------------------
    @serialized
    def progress(self, token, plugin, task_id, progress):
        self.verify(token, plugin, task_id)
        task = self.store.update_task(task_id, expected_states=("running",), progress=progress)
        if task is None:
            raise TaskError("execution-revoked", "该任务执行已结束或已被停止", 409)
        self._notify(task)
        return task

    @serialized
    def finish(self, token, plugin, task_id, state, error=None):
        if state not in FINISH_STATES:
            raise TaskError("invalid-request", "任务终态不正确")
        self.verify(token, plugin, task_id)
        return self._close(task_id, state, error)

    @serialized
    def _close(self, task_id, state, error=None):
        self._revoke(task_id)
        if self.leases is not None:
            self.leases.release("task", task_id)
        with self.lock:
            self.deadlines.pop(task_id, None)
            self.owners.pop(task_id, None)
        task = self.store.update_task(task_id, expected_states=ACTIVE, state=state, error=error,
                                      finished_at_ms=now_ms())
        task = task or self.store.task(task_id)
        self._notify(task)
        self._release_queue(task_id)
        return task

    def _release_queue(self, task_id):
        for waiting in reversed(self.store.tasks(states=("queued",), limit=500)):
            if waiting["afterTask"] == task_id and waiting["account"] not in self.blocked_accounts:
                try:
                    self._start(waiting)
                except Exception:
                    self._close(waiting["id"], "interrupted", {"code": "start-failed", "message": "排队任务未能开始"})

    @serialized
    def stop(self, task_id):
        task = self.store.task(task_id)
        if task is None:
            raise TaskError("not-found", "任务不存在", 404)
        if task["state"] == "queued":
            return self._close(task_id, "stopped")
        if task["state"] != "running":
            return task
        revoked = self._revoke(task_id)
        task = self.store.update_task(task_id, expected_states=("running",), state="stopping") or self.store.task(task_id)
        with self.lock:
            self.deadlines[task_id] = (time.monotonic() + STOP_GRACE, task["plugin"])
        for _digest, binding in revoked:
            try:
                self.supervisor.send(task["plugin"], {"type": "task.cancel", "task": task_id,
                                                      "execution": binding["execution"]}, start=False)
            except PluginUnavailable:
                pass
        self._notify(task)
        if self.supervisor.running(task["plugin"]) is None:
            return self._close(task_id, "stopped")
        return task

    def stop_owned(self, plugin, task_id):
        """``stop`` for a plugin's own task; another plugin's task is reported as missing."""
        task = self.store.task(task_id) if isinstance(task_id, str) else None
        if task is None or task["plugin"] != plugin:
            raise TaskError("not-found", "任务不存在", 404)
        return self.stop(task_id)

    @serialized
    def continue_task(self, task_id, conflict=None):
        task = self.store.task(task_id)
        if task is None:
            raise TaskError("not-found", "任务不存在", 404)
        self._require_account(task["account"])
        if task["state"] not in ("interrupted", "stopped", "failed", "partial"):
            raise TaskError("invalid-state", "任务当前不能继续", 409)
        if task["kind"] == "scheme":
            running = [item for item in self.store.tasks(account=task["account"], user=task["user"], kind="scheme",
                                                         states=ACTIVE, limit=500) if item["id"] != task_id]
            blocking = next((item for item in running if item["state"] in ("running", "stopping")),
                            running[0] if running else None)
            if running and conflict == "stop-other":
                self.stop(blocking["id"])
            elif running and conflict == "queue":
                task = self.store.update_task(task_id, state="queued", after_task=running[0]["id"])
                self._notify(task)
                return {"task": task}
            elif running:
                raise TaskError("scheme-busy", "该好友已有分析方案正在运行", 409, blocking)
        task = self.store.update_task(task_id, state="queued")
        if task["kind"] == "scheme" and self._blocked_by_stopping(task["account"], task["user"], task_id):
            return {"task": self.store.task(task_id)}
        return self._start(task)

    def _require_account(self, account):
        if account in self.blocked_accounts:
            raise TaskError("account-cleared", "账号资料正在清理或已清除，请重新打开应用", 409)

    @serialized
    def account_blocked(self, account):
        return account in self.blocked_accounts

    @serialized
    def quiesce_account(self, account):
        """Freeze starts and revoke every execution before the host drains plugin processes."""
        self.blocked_accounts.add(account)
        while pending := self.store.tasks(account=account, states=ACTIVE, limit=500):
            for task in pending:
                stopped = self.stop(task["id"])
                if stopped["state"] == "stopping":
                    self._close(task["id"], "stopped")

    # Plugin signals -------------------------------------------------------------------------------------------------
    @serialized
    def _ended(self, plugin, message):
        execution = self.store.execution(message.get("execution")) if isinstance(message.get("execution"), str) else None
        if execution is None:
            return
        task = self.store.task(execution["task"])
        if task is None or task["plugin"] != plugin:
            return
        owner = self.owners.get(task["id"])
        if owner is None or owner[0] != execution["id"]:
            return  # An old generation cannot finish a newly resumed/stopping task.
        if task["state"] == "stopping":
            self._close(task["id"], "stopped")
        elif task["state"] == "running" and execution["state"] == "active":
            self._close(task["id"], "failed", {"code": "no-result", "message": "插件结束了任务但未报告结果"})

    @serialized
    def _plugin_exit(self, plugin, expected, process_token):
        for task in self.store.tasks(plugin=plugin, states=("running", "stopping"), limit=500):
            owner = self.owners.get(task["id"])
            if owner is None or owner[1] != process_token:
                continue
            if task["state"] == "stopping":
                self._close(task["id"], "stopped")
            else:
                self._close(task["id"], "interrupted",
                            {"code": "plugin-exited", "message": "插件进程已结束，任务已中断，可继续"})

    @serialized
    def tick(self):
        now = time.monotonic()
        with self.lock:
            expired = [(task_id, plugin) for task_id, (deadline, plugin) in self.deadlines.items() if deadline <= now]
            for task_id, _plugin in expired:
                self.deadlines.pop(task_id, None)
        for task_id, plugin in expired:
            task = self.store.task(task_id)
            if task is not None and task["state"] == "stopping":
                self.supervisor.kill(plugin, "停止任务时插件未及时结束，已结束插件进程")
                self._close(task_id, "stopped")

    # Views ----------------------------------------------------------------------------------------------------------
    def tasks(self, **filters):
        return self.store.tasks(**filters)

    def task(self, task_id):
        return self.store.task(task_id)

    def binding(self, digest):
        with self.lock:
            return self.live.get(digest)
