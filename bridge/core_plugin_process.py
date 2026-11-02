"""One running plugin: its process group and the JSON-line control channel.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import itertools
import json
import queue
import secrets
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from core_jobs import ProcessGroup
from core_runtime import base_environment, node_executable, python_environment, python_executable, python_flags

PROTOCOL_VERSION = 1
MAX_LINE_BYTES = 4 * 1024 * 1024
READY_TIMEOUT = 10.0
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_MISSES = 3
RPC_TIMEOUT = 30.0
LOG_LINES = 200
LOG_LINE_CHARS = 500
MAX_OUTBOUND_MESSAGES = 32
MAX_OUTBOUND_BYTES = 8 * 1024 * 1024


class PluginUnavailable(RuntimeError):
    """The plugin cannot answer: not started, crashed, restarted or timed out."""

    def __init__(self, code="plugin-unavailable", message="插件暂时不可用"):
        super().__init__(message)
        self.code, self.message = code, message


class PluginError(RuntimeError):
    """The plugin answered with an error of its own."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def launch_command(manifest, root):
    directory = Path(manifest["directory"])
    runtime = manifest["runtime"]
    if runtime["type"] == "python":
        launcher = Path(root) / "plugin-sdk" / "python" / "launch.py"
        python = python_executable(root)
        return [python, *python_flags(python), "-B", str(launcher), str(directory / runtime["entry"])]
    if runtime["type"] == "node":
        node = node_executable(root)
        if not node:
            raise PluginUnavailable("plugin-start", "找不到 Node 运行时")
        loader = (Path(root) / "node_modules" / "tsx" / "dist" / "loader.mjs").as_uri()
        return [node, "--import", loader, str(directory / runtime["entry"])]
    return [str(directory / runtime["command"][0]), *runtime["command"][1:]]


class PluginProcess:
    def __init__(self, manifest, *, root, data_dir, core_url, dispatch, on_exit):
        self.manifest = manifest
        self.id = manifest["id"]
        self.root = Path(root)
        self.data_dir = Path(data_dir)
        self.core_url = core_url
        self.dispatch = dispatch
        self.on_exit = on_exit
        self.token = secrets.token_urlsafe(32)
        self.state = "starting"
        self.reason = None
        self.group = None
        self.process = None
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.ready = threading.Event()
        self.pending = {}
        self.sequence = itertools.count(1)
        self.log = deque(maxlen=LOG_LINES)
        self.started = time.monotonic()
        self.last_heartbeat = self.started
        self.exit_code = None
        self.stopping = False
        self.outbound = queue.Queue(MAX_OUTBOUND_MESSAGES)
        self.outbound_bytes = 0
        self.outbound_lock = threading.Lock()
        self.writer_done = threading.Event()

    # Lifecycle -------------------------------------------------------------------------------------------------------
    def start(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        command = launch_command(self.manifest, self.root)
        node = node_executable(self.root)
        python = python_executable(self.root)
        environment = base_environment(
            KYF_PLUGIN_ID=self.id, KYF_PLUGIN_DIR=self.manifest["directory"],
            KYF_PLUGIN_SDK=str(self.root / "plugin-sdk"), KYF_APP_ROOT=str(self.root),
            PYTHONUTF8="1", PYTHONIOENCODING="utf-8", KYF_PYTHON=python, **python_environment(python),
            KYF_TSX_LOADER=(self.root / "node_modules" / "tsx" / "dist" / "loader.mjs").as_uri(),
            **({"KYF_NODE": node} if node else {}))
        self.group = ProcessGroup()
        try:
            self.process = subprocess.Popen(
                command, cwd=self.manifest["directory"], env=environment, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, **ProcessGroup.popen_options())
            self.group.adopt(self.process)
        except BaseException:
            self.group.terminate()
            self.group.close()
            raise
        threading.Thread(target=self._read, daemon=True, name=f"plugin-{self.id}-out").start()
        threading.Thread(target=self._read_log, daemon=True, name=f"plugin-{self.id}-err").start()
        threading.Thread(target=self._write, daemon=True, name=f"plugin-{self.id}-in").start()
        self.send({"type": "hello", "protocol": PROTOCOL_VERSION,
                   "plugin": {"id": self.id, "version": self.manifest["version"],
                              "directory": self.manifest["directory"]},
                   "dataDir": str(self.data_dir),
                   "core": {"url": self.core_url.rstrip("/") + "/plugin-api/v1", "token": self.token}})

    def alive(self):
        return self.process is not None and self.process.poll() is None and self.state not in ("exited", "stopped")

    def wait_ready(self, timeout=READY_TIMEOUT):
        if not self.ready.wait(timeout):
            self.kill("插件启动超时")
            raise PluginUnavailable("plugin-start", "插件启动超时")
        if not self.alive():
            raise PluginUnavailable("plugin-start", self.reason or "插件启动失败")

    def shutdown(self, grace=3.0):
        """Ask the plugin to exit; end the group if it does not within ``grace`` seconds."""
        with self.lock:
            self.stopping = True
        try:
            self.send({"type": "shutdown"})
        except PluginUnavailable:
            pass
        try:
            if self.process is not None:
                self.process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        self.kill("已停止", state="stopped")

    def kill(self, reason, state="exited"):
        with self.lock:
            if state == "stopped":
                self.stopping = True
            self.reason = self.reason or reason
        if self.group is not None:
            self.group.terminate()
            self.group.close()
        self._fail_pending(PluginUnavailable("plugin-unavailable", reason))

    # Control channel -----------------------------------------------------------------------------------------------
    def send(self, message):
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) > MAX_LINE_BYTES:
            raise PluginUnavailable("message-too-large", "发送给插件的内容过大")
        if not self.alive():
            raise PluginUnavailable("plugin-unavailable", self.reason or "插件已结束")
        with self.outbound_lock:
            if self.outbound_bytes + len(data) > MAX_OUTBOUND_BYTES:
                raise PluginUnavailable("plugin-busy", "插件通信队列已满，请稍后重试")
            try:
                self.outbound.put_nowait(data)
            except queue.Full:
                raise PluginUnavailable("plugin-busy", "插件通信队列已满，请稍后重试") from None
            self.outbound_bytes += len(data)

    def _write(self):
        """Only this per-plugin thread may wait on its pipe; host and supervisor never do."""
        try:
            while self.alive():
                try:
                    data = self.outbound.get(timeout=.25)
                except queue.Empty:
                    continue
                try:
                    with self.write_lock:
                        view = memoryview(data)
                        while view:
                            written = self.process.stdin.write(view)
                            if not written:
                                raise OSError("plugin pipe closed")
                            view = view[written:]
                        self.process.stdin.flush()
                finally:
                    with self.outbound_lock:
                        self.outbound_bytes -= len(data)
                    self.outbound.task_done()
        except (OSError, ValueError):
            if self.alive():
                self.kill("插件通信管道已关闭")
        finally:
            self.writer_done.set()

    def call(self, method, params=None, context=None, timeout=RPC_TIMEOUT):
        request_id = f"r{next(self.sequence)}"
        waiter = {"event": threading.Event(), "result": None, "error": None}
        with self.lock:
            if not self.alive():
                raise PluginUnavailable("plugin-unavailable", self.reason or "插件未运行")
            self.pending[request_id] = waiter
        try:
            self.send({"type": "rpc", "id": request_id, "method": method, "params": params or {},
                       "context": context or {}})
            if not waiter["event"].wait(timeout):
                raise PluginUnavailable("plugin-timeout", "插件响应超时")
        finally:
            with self.lock:
                self.pending.pop(request_id, None)
        if waiter["error"] is not None:
            raise waiter["error"]
        return waiter["result"]

    def _fail_pending(self, error):
        with self.lock:
            waiters, self.pending = list(self.pending.values()), {}
        for waiter in waiters:
            waiter["error"] = error
            waiter["event"].set()

    def _read(self):
        stream = self.process.stdout
        try:
            while True:
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_LINE_BYTES and not line.endswith(b"\n"):
                    self._note("warn", "插件输出的一行超过 4 MiB，已丢弃")
                    while line and not line.endswith(b"\n"):
                        line = stream.readline(MAX_LINE_BYTES + 1)
                    continue
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, ValueError):
                    self._note("warn", "插件标准输出包含非协议内容")
                    continue
                if isinstance(message, dict) and isinstance(message.get("type"), str):
                    self._handle(message)
        except (OSError, ValueError):
            pass
        finally:
            self._exited()

    def _read_log(self):
        try:
            for line in iter(lambda: self.process.stderr.readline(8192), b""):
                self._note("stderr", line.decode("utf-8", "replace").rstrip())
        except (OSError, ValueError):
            pass

    def _note(self, level, text):
        self.log.append({"at": int(time.time() * 1000), "level": level, "text": str(text)[:LOG_LINE_CHARS]})

    def _handle(self, message):
        kind = message["type"]
        if kind == "ready":
            protocols = message.get("protocols", [message.get("protocol")])
            if not isinstance(protocols, list) or PROTOCOL_VERSION not in protocols:
                self.reason = "插件不支持当前控制协议"
                self.kill(self.reason)
                return
            with self.lock:
                self.state = "ready"
                self.last_heartbeat = time.monotonic()
            self.ready.set()
        elif kind == "heartbeat":
            with self.lock:
                self.last_heartbeat = time.monotonic()
                if self.state == "unresponsive":
                    self.state = "ready"
        elif kind == "rpc.result":
            with self.lock:
                waiter = self.pending.get(message.get("id"))
            if waiter is None:
                return
            error = message.get("error")
            if error is not None:
                code = error.get("code") if isinstance(error, dict) else None
                text = error.get("message") if isinstance(error, dict) else None
                waiter["error"] = PluginError(str(code or "plugin-error")[:64], str(text or "插件处理失败")[:300])
            else:
                waiter["result"] = message.get("result")
            waiter["event"].set()
        elif kind == "log":
            self._note(str(message.get("level") or "info")[:8], message.get("message") or "")
        else:
            self.dispatch(self, message)

    def _exited(self):
        code = None
        try:
            code = self.process.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        with self.lock:
            self.exit_code = code
            expected = self.stopping
            self.state = "stopped" if expected else "exited"
            if not expected and not self.reason:
                self.reason = f"插件进程已退出（代码 {code}）" if code is not None else "插件进程已退出"
        self.ready.set()
        if self.group is not None:
            self.group.close()
        with self.write_lock:
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except (OSError, ValueError):
                    pass
        self._fail_pending(PluginUnavailable("plugin-unavailable", self.reason or "插件已停止"))
        self.on_exit(self, expected)

    def check(self, now=None):
        """Heartbeat supervision, called about once a second."""
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.state == "starting" and now - self.started > READY_TIMEOUT:
                expired = True
            else:
                expired = False
                if self.state == "ready" and now - self.last_heartbeat > HEARTBEAT_INTERVAL * HEARTBEAT_MISSES:
                    self.state = "unresponsive"
        if expired:
            self.kill("插件启动超时")

    def public(self):
        with self.lock:
            return {"state": self.state, "reason": self.reason, "pid": self.process.pid if self.process else None,
                    "exitCode": self.exit_code, "log": list(self.log)[-20:]}
