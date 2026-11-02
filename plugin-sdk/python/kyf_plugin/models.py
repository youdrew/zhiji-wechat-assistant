"""Model calls for Python plugins through the SDK's Node model worker and Core's gateway.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The worker is a child of the plugin process, so it shares the plugin's process
group. Every call carries the task's execution token; after the task is stopped
the gateway rejects the token and aborts the request.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import threading
import time
from pathlib import Path

from .channel import Cancelled

DEFAULT_MODEL_TIMEOUT_MS = 3_600_000
MIN_MODEL_TIMEOUT_MS = 60_000
MAX_MODEL_TIMEOUT_MS = 10_800_000
DEFAULT_TIMEOUT = DEFAULT_MODEL_TIMEOUT_MS / 1000
# A truncated reply may carry the text it delivered before the cut.
MAX_PARTIAL_CHARS = 1_000_000


def model_timeout_ms(value=None):
    """Validate the shared saved inference limit; no credentials or payloads here."""
    if value is None:
        return DEFAULT_MODEL_TIMEOUT_MS
    if type(value) is not int or not MIN_MODEL_TIMEOUT_MS <= value <= MAX_MODEL_TIMEOUT_MS:
        raise ValueError("invalid-model-timeout")
    return value


class ModelError(RuntimeError):
    def __init__(self, code, message=None, *, http_status=None, partial=None):
        super().__init__(message or code)
        self.code, self.message = code, message or "模型调用失败"
        # Numeric metadata permits bounded retry policies without exposing the
        # provider's response body, request headers or credential-bearing text.
        self.http_status = http_status if type(http_status) is int and 400 <= http_status <= 599 else None
        # The text a reply delivered before the provider cut it at its output cap.
        # It is the model's own answer, so the caller may salvage it.
        self.partial = partial if type(partial) is str and 0 < len(partial) <= MAX_PARTIAL_CHARS else None


class ModelClient:
    def __init__(self, plugin):
        self.plugin = plugin
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.process = None
        self.pending = {}
        self.sequence = itertools.count(1)
        self._services = (0.0, None)

    # Services ---------------------------------------------------------------------------------------------------------
    def services(self, refresh=False):
        stamp, cached = self._services
        dynamic_alias = any(isinstance(item, dict) and item.get("model") == "local-default" for item in (cached or []))
        # Core already has a short metadata cache for this switchable alias.
        # Do not add the ordinary SDK cache's ten seconds to route discovery.
        if refresh or dynamic_alias or cached is None or time.monotonic() - stamp > 10:
            cached = self.plugin.core.model_services().get("services", [])
            self._services = (time.monotonic(), cached)
        return cached

    def service(self, refresh=False):
        services = self.services(refresh=refresh)
        if not services:
            raise ModelError("api-not-configured", "尚未配置生成模型，请先在设置中保存模型")
        return services[0]

    def service_for(self, role, refresh=False):
        """The first service carrying this purpose; the main model otherwise.

        An unassigned or silently failed purpose (no service carries the role)
        falls back to the main model, so callers can branch on
        ``"main" in service.get("roles", [])`` to detect that fallback.
        """
        if role is None:
            return self.service(refresh=refresh)
        for service in self.services(refresh=refresh):
            if role in (service.get("roles") or []):
                return service
        return self.service(refresh=refresh)

    # Worker -----------------------------------------------------------------------------------------------------------
    def _start(self):
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return self.process
            node = os.environ.get("KYF_NODE")
            loader = os.environ.get("KYF_TSX_LOADER")
            sdk = Path(os.environ.get("KYF_PLUGIN_SDK", Path(__file__).resolve().parents[2]))
            if not node or not loader:
                raise ModelError("worker-missing", "插件运行环境缺少 Node")
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self.process = subprocess.Popen(
                [node, "--import", loader, str(sdk / "node" / "model-worker.ts")], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=str(sdk), bufsize=0, creationflags=flags)
            threading.Thread(target=self._read, args=(self.process,), daemon=True, name="model-worker").start()
            return self.process

    def _read(self, process):
        try:
            for line in iter(lambda: process.stdout.readline(32 * 1024 * 1024), b""):
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                waiter = self.pending.get(message.get("id")) if isinstance(message, dict) else None
                if waiter is not None:
                    waiter["message"] = message
                    waiter["event"].set()
        except (OSError, ValueError):
            pass
        finally:
            for waiter in list(self.pending.values()):
                waiter["event"].set()

    def _send(self, process, message):
        # JSON permits these characters inside strings, but Node's readline
        # splits them into separate records. Escape only the wire representation;
        # JSON.parse restores the exact original text in the worker.
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        line = line.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        data = line.encode("utf-8") + b"\n"
        with self.write_lock:
            try:
                process.stdin.write(data)
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise ModelError("worker-unavailable", "模型工作进程已结束") from exc

    def generate(self, task, request, step=None, timeout=DEFAULT_TIMEOUT, role=None):
        """Run one generation for ``task`` (a TaskContext); returns ``{"text", "usage"?}``.

        ``role`` routes the call to the model assigned to that purpose
        ("image", "judge"); unassigned purposes use the main model.
        """
        task.check()
        # Re-read on each new call so a settings save applies immediately. ``timeout`` is retained
        # for old plugin callers and ignored: the gateway applies Core's model timeout once the
        # request leaves its queue, so waiting for a turn never fails a call. The wait ends with
        # the answer, the task's stop or the worker's exit.
        service = self.service_for(role, refresh=True)
        return self._call(task, "generate", {"service": service, "token": task.token,
                                              "step": step, "request": request})

    def judge(self, task, state, questions, step=None):
        """Answer typed questions with the assigned judge, using the shared worker.

        Judge failures are optional-optimization errors. Callers may continue
        their main work on ModelError, but must preserve task cancellation.
        """
        task.check()
        try:
            service = self.service_for("judge", refresh=True)
        except ModelError as exc:
            if exc.code == "api-not-configured":
                raise ModelError("judge-not-configured", "尚未配置判断模型") from exc
            raise
        try:
            return self._call(task, "judge", {"service": service, "token": task.token,
                                              "step": step, "state": state, "questions": questions})
        except ModelError as exc:
            if exc.code == "cancelled":
                raise Cancelled() from exc
            if exc.code in {"worker-missing", "worker-unavailable"}:
                raise ModelError("judge-unavailable", "判断模型暂时不可用") from exc
            raise

    def _call(self, task, command, payload):
        process = self._start()
        request_id = next(self.sequence)
        waiter = {"event": threading.Event(), "message": None}
        self.pending[request_id] = waiter
        try:
            self._send(process, {"id": request_id, "cmd": command, "payload": payload})
            while not waiter["event"].wait(0.2):
                if task.cancelled.is_set():
                    self._send(process, {"id": f"c{request_id}", "cmd": "cancel", "payload": {"targetId": request_id}})
                    raise Cancelled()
        finally:
            self.pending.pop(request_id, None)
        task.check()
        message = waiter["message"]
        if message is None:
            raise ModelError("worker-unavailable", "模型工作进程已结束")
        if message.get("error"):
            error = message["error"]
            if error.get("code") == "cancelled" and task.cancelled.is_set():
                raise Cancelled()
            raise ModelError(error.get("code") or "provider-error", error.get("message"),
                             http_status=error.get("httpStatus"), partial=error.get("partial"))
        return message.get("result") or {}

    def close(self):
        with self.lock:
            process, self.process = self.process, None
        if process is not None and process.poll() is None:
            try:
                process.stdin.close()
                process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
