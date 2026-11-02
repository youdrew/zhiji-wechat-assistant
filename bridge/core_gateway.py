"""Core side of the model gateway: start and supervise it, feed it credentials and tokens.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The gateway is a Node process in its own process group. Credentials and token
bindings travel only on its standard input. Its in-flight attempts and usage records go to the ledger;
developer-mode diagnostics reuse the model-trace service per task execution.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import queue
import subprocess
import threading
import time
from pathlib import Path
from collections import deque

from core_jobs import ProcessGroup
from core_runtime import base_environment, node_executable
from usage_ledger import usage_scope

READY_TIMEOUT = 20.0
PROBE_TIMEOUT = 45.0
RESTART_WINDOW = 600.0
RESTART_LIMIT = 5
MAX_OUTBOUND_MESSAGES = 256
MAX_OUTBOUND_BYTES = 8 * 1024 * 1024
WRITE_TIMEOUT = 3.0
CONFIG_TIMEOUT = 3.0
CLEANUP_TIMEOUT = 7.0
LOGGER = logging.getLogger(__name__)


class _ControlChannel:
    """Control transport owned by one gateway generation, never reused after restart."""
    def __init__(self, process, group):
        self.process, self.group = process, group
        self.queue = queue.Queue(MAX_OUTBOUND_MESSAGES)
        self.lock = threading.Lock()
        self.bytes = 0
        self.writing_since = None
        self.failed = threading.Event()
        self.cleaned = threading.Event()
        self.writer_done = threading.Event()
        self.ready = threading.Event()
        self.port = None
        self.failure = None


class GatewayUnavailable(RuntimeError):
    def __init__(self, code="gateway-unavailable", message="模型网关暂时不可用"):
        super().__init__(message)
        self.code, self.message = code, message


class ModelGatewayHost:
    def __init__(self, root, *, services, ledger, traces=None):
        """``services()`` returns the configured services with their credentials."""
        self.root = Path(root)
        self.services_source = services
        self.ledger = ledger
        self.traces = traces
        self.lock = threading.RLock()
        self.process = None
        self.group = None
        self.port = None
        self.ready = threading.Event()
        self.bindings = {}          # token hash -> binding (sent to the gateway)
        self.trace_contexts = {}    # execution id -> trace context
        self.scopes = {}            # execution id -> ledger scope
        self.pending = {}
        self.sequence = itertools.count(1)
        self.starts = []
        self.closed = False
        self._channel = None
        self._channels = set()
        self._configuration_lock = threading.RLock()
        self._configuration = None
        self.failures = deque(maxlen=20)

    # Process ----------------------------------------------------------------------------------------------------------
    def _alive(self):
        return (self.process is not None and self.process.poll() is None and
                self._channel is not None and not self._channel.failed.is_set())

    def ensure(self, timeout=READY_TIMEOUT):
        with self.lock:
            if self.closed:
                raise GatewayUnavailable("core-closing", "应用正在退出")
            if self._alive() and self.ready.is_set():
                return self.port
            if not self._alive():
                now = time.monotonic()
                self.starts = [stamp for stamp in self.starts if now - stamp < RESTART_WINDOW] + [now]
                if len(self.starts) > RESTART_LIMIT:
                    raise GatewayUnavailable("gateway-crashed", "模型网关连续异常退出，请重新启动应用")
                self._start()
            channel = self._channel
        if not channel.ready.wait(timeout):
            # A short local-query deadline may expire while the shared gateway
            # is still starting. Leave it alive for other waiting requests.
            if timeout >= READY_TIMEOUT:
                self._fail_channel(channel, "gateway-start")
            raise GatewayUnavailable("gateway-start", "模型网关未能启动")
        with self.lock:
            if channel is not self._channel or channel.failed.is_set() or not self._alive():
                raise GatewayUnavailable("gateway-start", "模型网关未能启动")
            return channel.port

    def _start(self):
        node = node_executable(self.root)
        if not node:
            raise GatewayUnavailable("gateway-start", "找不到 Node 运行时")
        if self._channel is not None:
            self._fail_channel(self._channel)
        loader = (self.root / "node_modules" / "tsx" / "dist" / "loader.mjs").as_uri()
        self.ready.clear()
        self.port = None
        group = ProcessGroup()
        try:
            process = subprocess.Popen(
                [node, "--import", loader, str(self.root / "scripts" / "model-gateway.ts")], cwd=self.root,
                env=base_environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                bufsize=0, **ProcessGroup.popen_options())
            group.adopt(process)
        except BaseException:
            group.close()
            raise
        channel = _ControlChannel(process, group)
        self.process, self.group, self._channel = process, group, channel
        self._channels.add(channel)
        threading.Thread(target=self._write, args=(channel,), daemon=True, name="model-gateway-write").start()
        threading.Thread(target=self._watch_write, args=(channel,), daemon=True, name="model-gateway-watch").start()
        threading.Thread(target=self._read, args=(channel,), daemon=True, name="model-gateway-read").start()

    def _send(self, message, channel=None):
        """Enqueue one bounded control message; never write to an OS pipe on a Core thread."""
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with self.lock:
            channel = channel or self._channel
            if (channel is None or channel is not self._channel or not self._alive() or
                    (self.closed and message.get("type") != "shutdown")):
                raise GatewayUnavailable()
        sent = threading.Event()
        overflow = False
        with channel.lock:
            if channel.failed.is_set():
                raise GatewayUnavailable()
            if len(data) + channel.bytes > MAX_OUTBOUND_BYTES:
                overflow = True
            else:
                try:
                    channel.queue.put_nowait((data, sent))
                    channel.bytes += len(data)
                except queue.Full:
                    overflow = True
        if overflow:
            # Dropping token.revoke could authorize a stopped task indefinitely.
            # Fail the complete transport and terminate that gateway generation.
            self._fail_channel(channel, "gateway-backpressure")
            raise GatewayUnavailable("gateway-backpressure", "模型网关控制通道拥塞，已停止该网关")
        return sent

    def _write(self, channel):
        try:
            while not channel.failed.is_set():
                try:
                    data, sent = channel.queue.get(timeout=.1)
                except queue.Empty:
                    continue
                with channel.lock:
                    channel.writing_since = time.monotonic()
                try:
                    view = memoryview(data)
                    while view:
                        count = channel.process.stdin.write(view)
                        if not count:
                            raise OSError("gateway pipe closed")
                        view = view[count:]
                    channel.process.stdin.flush()
                except (OSError, ValueError):
                    self._fail_channel(channel)
                    return
                finally:
                    with channel.lock:
                        channel.bytes -= len(data)
                        channel.writing_since = None
                    sent.set()
                    channel.queue.task_done()
        finally:
            channel.writer_done.set()

    def _watch_write(self, channel):
        while not channel.failed.wait(.05):
            with channel.lock:
                started = channel.writing_since
            if started is not None and time.monotonic() - started >= WRITE_TIMEOUT:
                self._fail_channel(channel, "gateway-write-timeout")
                return

    def _fail_channel(self, channel, code="gateway-unavailable", *, stage="control", exception=None):
        if channel is None:
            return
        with self.lock:
            with channel.lock:
                if channel.failed.is_set():
                    return
                channel.failed.set()
                channel.ready.set()  # Wake ensure() so failure is not a startup timeout.
                # Normal close is not a failure. Publish genuine failures only after cleanup.
                if not self.closed:
                    channel.failure = {"at": int(time.time() * 1000), "code": code if code in (
                        "gateway-unavailable", "gateway-start", "gateway-write-timeout", "gateway-backpressure",
                        "gateway-config-timeout") else "gateway-unavailable",
                        "stage": stage if stage in ("control", "stdout-eof", "read", "ledger-record", "ledger-track") else "control",
                        "exception": exception if exception in ("OSError", "ValueError", "GatewayUnavailable") else None,
                        "exitCode": channel.process.poll(), "closing": False}
            if channel is self._channel:
                self.ready.clear()
                self.port = None
            for waiter in self.pending.values():
                if waiter["channel"] is channel:
                    waiter["error"] = GatewayUnavailable(code)
                    waiter["event"].set()
        threading.Thread(target=self._dispose_channel, args=(channel,), daemon=True,
                         name="model-gateway-cleanup").start()

    def _dispose_channel(self, channel):
        try:
            channel.group.close()
            try:
                channel.process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            channel.writer_done.wait(1)
            for stream in (channel.process.stdin, channel.process.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            while True:
                try:
                    data, sent = channel.queue.get_nowait()
                except queue.Empty:
                    break
                with channel.lock:
                    channel.bytes -= len(data)
                sent.set()
                channel.queue.task_done()
        finally:
            with channel.lock:
                failure = channel.failure
                if failure is not None:
                    # Keep an already-observed exit code if bounded cleanup cannot obtain another.
                    exit_code = channel.process.poll()
                    failure = {**failure, "exitCode": exit_code if exit_code is not None else failure["exitCode"]}
                    channel.failure = failure
            with self.lock:
                if failure is not None:
                    self.failures.append(failure)
                    LOGGER.warning("model-gateway failure %s", json.dumps(failure, separators=(",", ":")))
                self._channels.discard(channel)
            channel.cleaned.set()

    def _read(self, channel):
        process = channel.process
        stage = "read"
        try:
            for line in iter(lambda: process.stdout.readline(4 * 1024 * 1024), b""):
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                with self.lock:
                    kind = message.get("type")
                    # Attempt records remain valid after token revocation or
                    # restart; the gateway carries their immutable task scope.
                    if kind == "usage" and isinstance(message.get("record"), dict):
                        stage = "ledger-record"
                        record = message["record"]
                        self.ledger.record(record, record.get("scope") or self.scopes.get(record.get("execution")))
                        stage = "read"
                        continue
                    if kind == "attempt" and isinstance(message.get("attempt"), dict):
                        stage = "ledger-track"
                        attempt = message["attempt"]
                        self.ledger.track(attempt, attempt.get("scope") or self.scopes.get(attempt.get("execution")),
                                          channel)
                        stage = "read"
                        continue
                    if channel is not self._channel or channel.failed.is_set():
                        continue
                    if kind == "ready" and type(message.get("port")) is int:
                        sent = self._replay(channel)
                        channel.port = message["port"]
                    elif kind in ("probe.result", "embedding.result", "local-judge.result"):
                        waiter = self.pending.get(message.get("id"))
                        if (waiter is not None and waiter["channel"] is channel and
                                kind == {"embedding": "embedding.result", "local-judge": "local-judge.result"}.get(
                                    waiter.get("kind"), "probe.result")):
                            waiter["result"] = message.get("result")
                            waiter["event"].set()
                        continue
                    elif kind == "config.applied":
                        waiter = self.pending.get(message.get("id"))
                        if waiter is not None and waiter["channel"] is channel and waiter.get("kind") == "config":
                            waiter["result"] = True
                            waiter["event"].set()
                        continue
                    else:
                        continue
                # Services and token replay must have reached the pipe before
                # callers are given this generation's address.
                if not sent.wait(WRITE_TIMEOUT):
                    self._fail_channel(channel, "gateway-write-timeout")
                    continue
                with self.lock:
                    if channel is self._channel and not channel.failed.is_set():
                        self.port = channel.port
                        self.ready.set()
                        channel.ready.set()
        except (OSError, ValueError, GatewayUnavailable) as exc:
            self._fail_channel(channel, stage=stage, exception=type(exc).__name__)
        finally:
            # Nothing more can arrive from this generation; its unfinished attempts ended with it.
            self.ledger.forget(channel)
            self._fail_channel(channel, stage="stdout-eof")

    def _replay(self, channel):
        """Send services and live tokens to a freshly started gateway."""
        try:
            services = self.services_source()
        except GatewayUnavailable:
            services = []  # A draining upstream must not prevent the local control transport from starting.
        sent = self._send({"type": "config", "services": services}, channel)
        for digest, binding in self.bindings.items():
            sent = self._send({"type": "token.add", "hash": digest, "binding": binding}, channel)
        return sent

    # Configuration and tokens ---------------------------------------------------------------------------------------
    def reconfigure(self):
        if self._alive() and self.ready.is_set():
            self._refresh_services()

    def _refresh_services(self):
        with self._configuration_lock:
            try:
                services = self.services_source()
            except GatewayUnavailable:
                self._apply_services([])  # Remove the old route before reporting upstream unavailability.
                raise
            self._apply_services(services)
            return services

    def _apply_services(self, services):
        digest = hashlib.sha256(json.dumps(services, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":")).encode("utf-8")).digest()
        with self._configuration_lock:
            with self.lock:
                channel = self._channel
                if channel is None or not self._alive() or not self.ready.is_set():
                    raise GatewayUnavailable()
                if self._configuration == (channel, digest):
                    return
                config_id = f"c{next(self.sequence)}"
                waiter = {"event": threading.Event(), "result": None, "channel": channel,
                          "error": None, "kind": "config"}
                self.pending[config_id] = waiter
            try:
                self._send({"type": "config", "id": config_id, "services": services}, channel)
                if not waiter["event"].wait(CONFIG_TIMEOUT):
                    self._fail_channel(channel, "gateway-config-timeout")
                    raise GatewayUnavailable("gateway-config-timeout", "模型网关配置更新未完成，请重试")
                if waiter["error"] is not None:
                    raise waiter["error"]
                with self.lock:
                    if channel is not self._channel or channel.failed.is_set() or waiter["result"] is not True:
                        raise GatewayUnavailable()
                    self._configuration = (channel, digest)
            finally:
                with self.lock:
                    self.pending.pop(config_id, None)

    def services(self):
        port = self.ensure()
        services = self._refresh_services()
        return [{"id": item["id"], "protocol": item["protocol"], "model": item["model"],
                 "contextTokens": item.get("contextTokens"), "timeoutMs": item.get("timeoutMs", 3_600_000),
                 "concurrency": item.get("concurrency", 4),
                 "stamp": item.get("stamp"),
                 "fingerprint": item.get("fingerprint"),
                 **({"roles": item["roles"]} if item.get("roles") else {}),
                 **{key: item[key] for key in ("cacheFingerprint", "actualModel", "backendId", "routeRevision", "maxOutputTokens",
                                              "supportsVision") if key in item},
                 "url": f"http://127.0.0.1:{port}/s/{item['id']}"}
                for item in services]

    def token(self, action, digest, binding):
        """Task-manager listener: register or revoke one execution token."""
        if action == "add":
            trace = self._trace_begin(binding)
            scope = usage_scope(binding.get("plugin"), binding.get("user"), multi=binding.get("multi"))
            message_binding = {key: binding.get(key) for key in ("plugin", "pluginVersion", "task", "execution",
                                                                 "generation", "account", "user", "kind")}
            message_binding["scope"] = scope
            if trace is not None:
                message_binding["trace"] = trace
            with self.lock:
                self.bindings[digest] = message_binding
                self.scopes[binding["execution"]] = scope
            try:
                self.ensure()
                self._send({"type": "token.add", "hash": digest, "binding": message_binding})
            except GatewayUnavailable:
                pass
        else:
            with self.lock:
                self.bindings.pop(digest, None)
            try:
                if self._alive():
                    self._send({"type": "token.revoke", "hash": digest})
            except GatewayUnavailable:
                pass
            self._trace_end(binding)

    def _trace_begin(self, binding):
        if self.traces is None:
            return None
        try:
            context = self.traces.register_execution({
                "source": f"plugin:{binding['plugin']}", "trigger": binding.get("kind") or "background",
                "accountId": binding.get("account"), "contactId": binding.get("user"),
                "businessJobId": binding["task"]})
            context["endpoint"] = getattr(self.traces, "transport_url", None)
            if self._alive():
                context.update(self.traces.bind_transport(context["producerId"], self.process.pid))
        except Exception:
            return None
        with self.lock:
            self.trace_contexts[binding["execution"]] = context
        return context

    def _trace_end(self, binding):
        with self.lock:
            context = self.trace_contexts.pop(binding.get("execution"), None)
            self.scopes.pop(binding.get("execution"), None)
        if context is not None and self.traces is not None:
            try:
                self.traces.unregister(context["producerId"])
            except Exception:
                pass

    def attempt_outcome(self, plugin, attempt_id, body):
        """Record how a plugin used one attempt's result (diagnostics only)."""
        status = body.get("status") if body.get("status") in ("success", "failed", "discarded") else "success"
        adoption = body.get("adoption") if body.get("adoption") in ("adopted", "discarded", "partial") else "adopted"
        changed, (record_id, task) = self.ledger.set_outcome(attempt_id, plugin, f"{status}:{adoption}")
        if record_id and self.traces is not None:
            with self.lock:
                context = next((item for item in self.trace_contexts.values() if item.get("businessJobId") == task),
                               None)
            if context is not None:
                try:
                    self.traces.record_outcome(context, record_id, status, adoption)
                except Exception:
                    pass
        return {"ok": changed}

    def probe(self, request, timeout=PROBE_TIMEOUT, trace=None):
        """List models or test a connection; ``trace(pid)`` may bind a diagnostic context."""
        self.ensure()
        with self.lock:
            channel = self._channel
            if channel is None or channel.failed.is_set():
                raise GatewayUnavailable()
        if trace is not None:
            try:
                request = {**request, "traceContext": trace(channel.process.pid)}
            except Exception:
                pass
        probe_id = f"p{next(self.sequence)}"
        waiter = {"event": threading.Event(), "result": None, "channel": channel, "error": None}
        with self.lock:
            self.pending[probe_id] = waiter
        try:
            self._send({"type": "probe", "id": probe_id, "request": request}, channel)
            if not waiter["event"].wait(timeout):
                raise RuntimeError("timeout")
            if waiter["error"] is not None:
                raise waiter["error"]
            if waiter["result"] is None:
                raise GatewayUnavailable()
            return waiter["result"]
        finally:
            with self.lock:
                self.pending.pop(probe_id, None)

    def embedding(self, request, timeout=30, check=None):
        """One local CPU request; no text, usage or diagnostic trace is retained."""
        deadline = time.monotonic() + timeout
        self.ensure(timeout=min(timeout, READY_TIMEOUT))
        if check:
            check()
        with self.lock:
            channel = self._channel
            if channel is None or channel.failed.is_set():
                raise GatewayUnavailable()
            request_id = f"e{next(self.sequence)}"
            waiter = {"event": threading.Event(), "result": None, "channel": channel,
                      "error": None, "kind": "embedding"}
            self.pending[request_id] = waiter
        try:
            if time.monotonic() >= deadline:
                raise GatewayUnavailable("embedding-timeout", "本机语义检索超时，已回退关键词检索")
            self._send({"type": "embedding", "id": request_id, "request": request}, channel)
            while not waiter["event"].wait(max(0, min(.1, deadline - time.monotonic()))):
                if check:
                    check()
                if time.monotonic() >= deadline:
                    raise GatewayUnavailable("embedding-timeout", "本机语义检索超时，已回退关键词检索")
            if check:
                check()
            if time.monotonic() >= deadline:
                raise GatewayUnavailable("embedding-timeout", "本机语义检索超时，已回退关键词检索")
            if waiter["error"] is not None:
                raise waiter["error"]
            result = waiter["result"]
            if not isinstance(result, dict) or result.get("error"):
                raise GatewayUnavailable("embedding-unavailable", "本机语义检索暂不可用")
            return result
        except BaseException:
            try:
                self._send({"type": "embedding.cancel", "id": request_id}, channel)
            except GatewayUnavailable:
                pass
            raise
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def local_judge(self, operation, local=None, timeout=45, check=None):
        """Trusted local-model lifecycle control; no user text or credentials."""
        if operation not in ("selftest", "stop"):
            raise ValueError("invalid judge operation")
        self.ensure(timeout=min(timeout, READY_TIMEOUT))
        with self.lock:
            channel = self._channel
            if channel is None or channel.failed.is_set():
                raise GatewayUnavailable()
            request_id = f"j{next(self.sequence)}"
            waiter = {"event": threading.Event(), "result": None, "channel": channel,
                      "error": None, "kind": "local-judge"}
            self.pending[request_id] = waiter
        deadline = time.monotonic() + timeout
        try:
            if check:
                check()
            self._send({"type": "local-judge." + operation, "id": request_id,
                        "request": {"local": local}}, channel)
            while not waiter["event"].wait(max(0, min(.1, deadline - time.monotonic()))):
                if check:
                    check()
                if time.monotonic() >= deadline:
                    raise GatewayUnavailable("judge-unavailable", "本地判断模型自测超时")
            if check:
                check()
            if waiter["error"] is not None:
                raise waiter["error"]
            result = waiter["result"]
            if not isinstance(result, dict) or result.get("error"):
                raise GatewayUnavailable("judge-unavailable", "本地判断模型暂不可用")
            return result
        except BaseException:
            try:
                self._send({"type": "local-judge.cancel", "id": request_id}, channel)
            except GatewayUnavailable:
                pass
            raise
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def close(self):
        with self.lock:
            self.closed = True
            channel = self._channel
        if channel is not None and not channel.failed.is_set() and channel.process.poll() is None:
            try:
                self._send({"type": "shutdown"}, channel)
                channel.process.wait(timeout=3)
            except (GatewayUnavailable, subprocess.TimeoutExpired):
                pass
        with self.lock:
            channels = list(self._channels)
        for owned in channels:
            self._fail_channel(owned)
        deadline = time.monotonic() + CLEANUP_TIMEOUT
        for owned in channels:
            owned.cleaned.wait(max(0, deadline - time.monotonic()))
