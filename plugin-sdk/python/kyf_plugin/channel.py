"""Control-channel loop for Python plugins (plugin contract v1).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Core writes JSON lines to standard input; the plugin answers on standard
output. Nothing else may be printed to standard output: use ``plugin.log`` or
standard error for diagnostics.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .core import CoreClient, ExecutionRevoked

PROTOCOL = 1
HEARTBEAT_SECONDS = 5
MAX_LINE_BYTES = 4 * 1024 * 1024  # Core drops longer output lines, so the caller would only see a timeout.


class Cancelled(Exception):
    """Raised by ``TaskContext.check`` after Core cancelled the execution."""


class RpcError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


class TaskContext:
    def __init__(self, plugin, task, execution):
        self.plugin = plugin
        self.task = task
        self.execution = execution
        self.token = execution["token"]
        self.cancelled = threading.Event()
        self.core = plugin.core.for_execution(self.token)
        self.finished = False

    @property
    def account(self):
        return self.task["account"]

    @property
    def user(self):
        return self.task.get("user")

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def progress(self, done=None, total=None, label=None, **extra):
        self.check()
        body = {key: value for key, value in {"done": done, "total": total, "label": label, **extra}.items()
                if value is not None}
        try:
            self.core.post(f"tasks/{self.task['id']}/progress", body)
        except ExecutionRevoked:
            self.cancelled.set()
            raise Cancelled() from None

    def stop(self):
        """Stop this task from inside the plugin; Core revokes the token and closes it as ``stopped``.

        Core is told first so the handler's exit (``task.ended``) finds the task already stopping.
        """
        try:
            return self.plugin.core.stop_task(self.task["id"])
        finally:
            self.cancelled.set()

    def finish(self, state="done", error=None):
        if self.finished:
            return
        self.finished = True
        try:
            self.core.post(f"tasks/{self.task['id']}/finish", {"state": state, "error": error})
        except ExecutionRevoked:
            pass


class Plugin:
    def __init__(self, workers=8):
        self.methods = {}
        self.task_handlers = {}
        self.event_handlers = {}
        self.core = None
        self.data_dir = None
        self.info = {}
        self.contexts = {}
        self.write_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="plugin")
        self.stopping = threading.Event()
        self.on_shutdown = []
        self._models = None

    @property
    def models(self):
        """Model calls through Core's gateway (``ModelClient``), started on first use."""
        if self._models is None:
            from .models import ModelClient
            self._models = ModelClient(self)
            self.on_shutdown.append(self._models.close)
        return self._models

    # Registration ---------------------------------------------------------------------------------------------------
    def method(self, name):
        def register(function):
            self.methods[name] = function
            return function
        return register

    def task(self, kind="*"):
        def register(function):
            self.task_handlers[kind] = function
            return function
        return register

    def event(self, name):
        def register(function):
            self.event_handlers[name] = function
            return function
        return register

    # Output ---------------------------------------------------------------------------------------------------------
    def send(self, message):
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
        if len(data) * 3 > MAX_LINE_BYTES and len(data.encode("utf-8")) > MAX_LINE_BYTES:
            raise RpcError("response-too-large", "插件返回的内容过大")
        with self.write_lock:
            sys.stdout.write(data)
            sys.stdout.flush()

    def log(self, message, level="info"):
        self.send({"type": "log", "level": level, "message": str(message)[:500]})

    # Loop -----------------------------------------------------------------------------------------------------------
    def run(self):
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stdout.reconfigure(encoding="utf-8")
        hello = json.loads(sys.stdin.readline() or "{}")
        if hello.get("type") != "hello":
            raise SystemExit("expected hello")
        self.info = hello
        self.core = CoreClient(hello["core"]["url"], hello["core"]["token"])
        self.data_dir = Path(hello["dataDir"])
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.send({"type": "ready", "protocols": [PROTOCOL]})
        threading.Thread(target=self._heartbeat, daemon=True, name="plugin-heartbeat").start()
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            kind = message.get("type")
            if kind == "shutdown":
                break
            if kind == "rpc":
                self.pool.submit(self._rpc, message)
            elif kind == "task.start":
                threading.Thread(target=self._task, args=(message,), daemon=True, name="plugin-task").start()
            elif kind == "task.cancel":
                context = self.contexts.get(message.get("execution"))
                if context is not None:
                    context.cancelled.set()
            elif kind == "event":
                self.pool.submit(self._event, message)
        self.stopping.set()
        for context in list(self.contexts.values()):
            context.cancelled.set()
        for callback in self.on_shutdown:
            try:
                callback()
            except Exception:
                pass
        self.pool.shutdown(wait=False, cancel_futures=True)
        os._exit(0)

    def _heartbeat(self):
        while not self.stopping.wait(HEARTBEAT_SECONDS):
            try:
                self.send({"type": "heartbeat"})
            except (OSError, ValueError):
                return

    def _rpc(self, message):
        handler = self.methods.get(message.get("method"))
        try:
            if handler is None:
                raise RpcError("unknown-method", "插件不支持该操作")
            result = handler(message.get("params") or {}, message.get("context") or {})
            self.send({"type": "rpc.result", "id": message.get("id"), "result": result})
        except RpcError as exc:
            self.send({"type": "rpc.result", "id": message.get("id"),
                       "error": {"code": exc.code, "message": exc.message}})
        except Exception as exc:
            self.log("".join(traceback.format_exception_only(type(exc), exc)).strip(), "error")
            self.send({"type": "rpc.result", "id": message.get("id"),
                       "error": {"code": getattr(exc, "code", "plugin-error"),
                                 "message": getattr(exc, "message", "插件处理失败")}})

    def start_task(self, account, user, *, kind="interactive", title, params=None, handler):
        """Create a task of this plugin and run ``handler(context)`` on a daemon thread.

        Returns ``(task, context)`` at once; ``context.stop()`` stops it, ``context.token`` is the
        execution token for model calls.
        """
        created = self.core.post("tasks", {"kind": kind, "account": account, "user": user, "title": title,
                                           "params": params or {}})
        task, execution = created["task"], created["execution"]
        context = TaskContext(self, task, execution)
        self.contexts[execution["id"]] = context
        threading.Thread(target=self.run_task, args=(task, execution, handler, context), daemon=True,
                         name="plugin-task").start()
        return task, context

    def run_task(self, task, execution, handler=None, context=None):
        """Run a task handler with an execution token, e.g. one the plugin created itself."""
        context = context or TaskContext(self, task, execution)
        self.contexts[execution["id"]] = context
        handler = handler or self.task_handlers.get(task.get("kind")) or self.task_handlers.get("*")
        try:
            if handler is None:
                context.finish("failed", {"code": "unsupported", "message": "插件不支持该任务"})
                return
            outcome = handler(context)
            if not context.finished and not context.cancelled.is_set():
                context.finish(outcome if outcome in ("done", "partial", "failed") else "done")
        except (Cancelled, ExecutionRevoked):
            pass
        except Exception as exc:
            self.log("".join(traceback.format_exception_only(type(exc), exc)).strip(), "error")
            if not context.cancelled.is_set():
                context.finish("failed", {"code": getattr(exc, "code", "plugin-error"),
                                          "message": getattr(exc, "message", "分析失败")})
        finally:
            self.contexts.pop(execution["id"], None)
            self.send({"type": "task.ended", "execution": execution["id"]})

    def _task(self, message):
        self.run_task(message["task"], message["execution"])

    def _event(self, message):
        handler = self.event_handlers.get(message.get("event"))
        try:
            if handler is not None:
                handler(message)
        except Exception as exc:
            self.log("".join(traceback.format_exception_only(type(exc), exc)).strip(), "error")
        finally:
            self.send({"type": "event.ack", "id": message.get("id")})
