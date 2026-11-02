"""Start, watch and stop plugin processes on demand.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

A plugin starts the first time something needs it: an interface call, a task or
an event. A crash, hang or timeout affects only that plugin's work; the
supervisor never blocks WeChat data maintenance or other plugins.
"""
from __future__ import annotations

import hmac
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from core_plugin_process import RPC_TIMEOUT, PluginProcess, PluginUnavailable
from core_plugins import DependencyError

CRASH_WINDOW = 600.0
CRASH_LIMIT = 3


class PluginSupervisor:
    def __init__(self, registry, *, root, data_dir, core_url):
        self.registry = registry
        self.root = Path(root)
        self.data_dir = Path(data_dir)
        self.core_url = core_url
        self.lock = threading.RLock()
        self.processes = {}
        self.start_locks = defaultdict(threading.RLock)
        self.dependency_bindings = {}
        self.dependency_failures = {}
        self.crashes = defaultdict(deque)
        self.blocked = {}
        self.handlers = defaultdict(list)
        self.exit_listeners = []
        self.tickers = []
        self.closed = False
        self.wake = threading.Event()
        self.monitor = threading.Thread(target=self._monitor, daemon=True, name="plugin-supervisor")
        self.monitor.start()

    # Wiring ---------------------------------------------------------------------------------------------------------
    def on(self, message_type, handler):
        """Register ``handler(plugin_id, message)`` for a plugin-to-Core message type."""
        self.handlers[message_type].append(handler)

    def on_exit(self, listener):
        """Register ``listener(plugin_id, expected, process_token)`` for process ends."""
        self.exit_listeners.append(listener)

    def add_ticker(self, ticker):
        self.tickers.append(ticker)

    def data_directory(self, plugin_id):
        return self.data_dir / plugin_id

    # Processes ------------------------------------------------------------------------------------------------------
    def running(self, plugin_id):
        with self.lock:
            process = self.processes.get(plugin_id)
            return process if process is not None and process.alive() else None

    def ensure(self, plugin_id, wait=True):
        """Validate the complete graph, then wait for prerequisites before starting their users."""
        try:
            order = self.registry.startup_order(plugin_id)
        except DependencyError as exc:
            raise PluginUnavailable(exc.code, exc.message) from None
        with self.lock:
            for dependency in order[:-1]:
                if dependency in self.blocked:
                    reason = f"依赖插件 {dependency} 无法启动：{self.blocked[dependency]}"
                    self.dependency_failures[plugin_id] = {"plugin": dependency, "message": reason}
                    raise PluginUnavailable("dependency-start-failed", reason)
        for dependency in order[:-1]:
            try:
                self._ensure_one(dependency, wait=True)
            except PluginUnavailable as exc:
                reason = f"依赖插件 {dependency} 启动失败：{exc.message}"
                with self.lock:
                    self.dependency_failures[plugin_id] = {"plugin": dependency, "message": reason}
                raise PluginUnavailable("dependency-start-failed", reason) from None
        return self._ensure_one(plugin_id, wait=wait)

    def _bindings(self, manifest):
        """Called under the supervisor lock: only ready, live prerequisite generations qualify."""
        bindings = {}
        for dependency in manifest.get("dependencies", []):
            process = self.processes.get(dependency)
            if (process is None or not process.alive() or process.stopping or
                    not process.ready.is_set() or process.state != "ready"):
                raise PluginUnavailable("dependency-unavailable", f"依赖插件 {dependency} 尚未就绪或已经退出")
            bindings[dependency] = process.token
        return bindings

    def _ensure_one(self, plugin_id, wait=True):
        # Per-plugin locks deduplicate concurrent starts without holding the global lock while waiting for ready.
        with self.start_locks[plugin_id]:
            return self._start_one(plugin_id, wait)

    def _start_one(self, plugin_id, wait):
        # A newly started dependency cannot be attached to an older dependent process.
        with self.lock, self.registry.lock:
            try:
                self.registry.startup_order(plugin_id)
            except DependencyError as exc:
                raise PluginUnavailable(exc.code, exc.message) from None
            manifest = self.registry.get(plugin_id)
            bindings = self._bindings(manifest)
            previous = self.processes.get(plugin_id)
            stale = previous is not None and previous.alive() and (
                previous.stopping or self.dependency_bindings.get(plugin_id, {}) != bindings)
        if stale:
            previous.reason = "依赖插件进程已变化，正在重新启动"
            previous.shutdown(grace=.1)
        with self.lock, self.registry.lock:
            if self.closed:
                raise PluginUnavailable("core-closing", "应用正在退出")
            # A rescan or disable during dependency startup must be checked again before process creation.
            try:
                self.registry.startup_order(plugin_id)
            except DependencyError as exc:
                raise PluginUnavailable(exc.code, exc.message) from None
            manifest = self.registry.get(plugin_id)
            bindings = self._bindings(manifest)
            process = self.processes.get(plugin_id)
            if process is not None and process.alive():
                pass
            else:
                if plugin_id in self.blocked:
                    raise PluginUnavailable("plugin-crashed", self.blocked[plugin_id])
                process = PluginProcess(manifest, root=self.root, data_dir=self.data_directory(plugin_id),
                                        core_url=self.core_url, dispatch=self._dispatch, on_exit=self._exited)
                self.processes[plugin_id] = process
                self.dependency_bindings[plugin_id] = bindings
                try:
                    process.start()
                except PluginUnavailable:
                    raise
                except OSError as exc:
                    process.state, process.reason = "exited", f"插件无法启动：{exc.strerror or exc}"
                    raise PluginUnavailable("plugin-start", process.reason) from exc
        if wait:
            process.wait_ready()
        with self.lock, self.registry.lock:
            if self.closed:
                raise PluginUnavailable("core-closing", "应用正在退出")
            try:
                order = self.registry.startup_order(plugin_id)
            except DependencyError as exc:
                raise PluginUnavailable(exc.code, exc.message) from None
            for current in order:
                running = self.processes.get(current)
                if (running is None or not running.alive() or running.stopping or
                        ((current != plugin_id or wait) and running.state != "ready")):
                    raise PluginUnavailable("dependency-unavailable" if current != plugin_id else "plugin-unavailable",
                                            f"插件 {current} 在启动期间已停止或尚未就绪")
                if self.dependency_bindings.get(current, {}) != self._bindings(self.registry.get(current)):
                    raise PluginUnavailable("dependency-changed", f"插件 {current} 的依赖进程在启动期间已变化")
            if self.processes.get(plugin_id) is not process:
                raise PluginUnavailable("plugin-unavailable", "插件进程在启动期间已变化")
            self.dependency_failures.pop(plugin_id, None)
        return process

    def reconcile_dependencies(self):
        """Suspend running dependents whose graph or prerequisite generation is no longer usable."""
        with self.lock:
            if self.closed:
                return
            processes = list(self.processes.values())
        # Stop dependents before their prerequisites; manifest removal still preserves old bindings.
        affected = []
        for process in processes:
            if not process.alive() or process.stopping:
                continue
            try:
                with self.lock, self.registry.lock:
                    for plugin_id in self.registry.startup_order(process.id):
                        bindings = self._bindings(self.registry.get(plugin_id))
                        if self.dependency_bindings.get(plugin_id, {}) != bindings:
                            raise PluginUnavailable("dependency-changed", f"依赖插件 {plugin_id} 的进程或依赖声明已变化")
            except (DependencyError, PluginUnavailable) as exc:
                affected.append((process, exc.message))
        targets = {process.id: (process, reason) for process, reason in affected}
        ordered = []
        for plugin_id in targets:
            for dependent in self.registry.dependents(plugin_id):
                if dependent in targets and dependent not in ordered:
                    ordered.append(dependent)
            if plugin_id not in ordered:
                ordered.append(plugin_id)
        for plugin_id in ordered:
            process, reason = targets[plugin_id]
            with self.lock:
                if self.processes.get(plugin_id) is not process or not process.alive() or process.stopping:
                    continue
                if self.registry.enabled(plugin_id):
                    self.dependency_failures[plugin_id] = {"plugin": plugin_id, "message": reason}
                process.reason = reason
                process.stopping = True
            process.shutdown(grace=.1)

    def plugin_for_token(self, token):
        """The plugin whose current process token this is, or None."""
        if not isinstance(token, str) or not token:
            return None
        with self.lock:
            for plugin_id, process in self.processes.items():
                if process.alive() and hmac.compare_digest(process.token, token):
                    return plugin_id
        return None

    def token_for_plugin(self, plugin_id):
        """The current process token of one running plugin, or None.

        唯一的外发用途：本机高影响删除确认窗（core_mcp_confirm_window）代表
        kyf.mcp 的设置通道回传决定，属于本机设置界面的一部分；令牌只交给
        Core 自己拉起的确认窗进程，不给任何插件或 MCP 客户端。插件未运行
        时返回 None，确认窗仍可展示并以 presenter 返回值收尾。
        """
        if not isinstance(plugin_id, str) or not plugin_id:
            return None
        with self.lock:
            process = self.processes.get(plugin_id)
            if process is not None and process.alive():
                return process.token
        return None

    def rpc(self, plugin_id, method, params=None, context=None, timeout=RPC_TIMEOUT):
        return self.ensure(plugin_id).call(method, params, context, timeout)

    def send(self, plugin_id, message, start=True):
        process = self.ensure(plugin_id) if start else self.running(plugin_id)
        if process is None:
            raise PluginUnavailable("plugin-unavailable", "插件未运行")
        process.send(message)

    def stop(self, plugin_id, reason="已停止", grace=3.0):
        with self.lock:
            self.dependency_failures.pop(plugin_id, None)
            process = self.processes.get(plugin_id)
        if process is not None and process.alive():
            process.reason = reason
            process.shutdown(grace)

    def kill(self, plugin_id, reason):
        with self.lock:
            process = self.processes.get(plugin_id)
        if process is not None and process.alive():
            process.kill(reason)

    def reset(self, plugin_id):
        """Allow a plugin blocked after repeated crashes to start again."""
        with self.lock:
            self.blocked.pop(plugin_id, None)
            self.crashes.pop(plugin_id, None)
            self.dependency_failures.pop(plugin_id, None)
            for parent, failure in list(self.dependency_failures.items()):
                if failure["plugin"] == plugin_id:
                    self.dependency_failures.pop(parent, None)

    def _dispatch(self, process, message):
        with self.lock:
            if self.processes.get(process.id) is not process:
                return
        for handler in list(self.handlers.get(message["type"], ())):
            try:
                handler(process.id, message)
            except Exception as exc:  # A faulty message affects only the plugin that sent it.
                process._note("warn", f"Core 未能处理插件消息 {message['type']}：{type(exc).__name__}")

    def _exited(self, process, expected):
        with self.lock:
            if not expected and self.processes.get(process.id) is process:
                now = time.monotonic()
                history = self.crashes[process.id]
                history.append(now)
                while history and now - history[0] > CRASH_WINDOW:
                    history.popleft()
                if len(history) >= CRASH_LIMIT:
                    self.blocked[process.id] = "插件在 10 分钟内连续异常退出，已暂停自动启动；可在设置中关闭后重新开启插件"
        for listener in list(self.exit_listeners):
            try:
                listener(process.id, expected, process.token)
            except Exception:
                pass
        self.wake.set()

    def _monitor(self):
        while not self.closed:
            self.wake.wait(1.0)
            self.wake.clear()
            if self.closed:
                break
            with self.lock:
                processes = list(self.processes.values())
            now = time.monotonic()
            for process in processes:
                if process.alive():
                    process.check(now)
            self.reconcile_dependencies()
            for ticker in list(self.tickers):
                try:
                    ticker()
                except Exception:
                    pass

    # Views ----------------------------------------------------------------------------------------------------------
    def status(self, plugin_id):
        dependency_error = self.registry.dependency_error(plugin_id)
        with self.lock:
            process = self.processes.get(plugin_id)
            blocked = self.blocked.get(plugin_id)
            failure = self.dependency_failures.get(plugin_id)
            if failure:
                dependency = self.processes.get(failure["plugin"])
                if dependency is not None and dependency.alive() and dependency.state == "ready" and not dependency.stopping:
                    self.dependency_failures.pop(plugin_id, None)
                    failure = None
        if dependency_error or failure:
            return {"state": "dependency-blocked", "reason": (dependency_error or failure)["message"],
                    "log": process.public()["log"] if process else []}
        if blocked:
            return {"state": "blocked", "reason": blocked, "log": process.public()["log"] if process else []}
        if process is None:
            return {"state": "idle", "reason": None, "log": []}
        return process.public()

    def close(self, grace=3.0):
        with self.lock:
            self.closed = True
            processes = list(self.processes.values())
        self.wake.set()
        threads = [threading.Thread(target=process.shutdown, args=(grace,), daemon=True)
                   for process in processes]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(grace + 6)
        if self.monitor is not threading.current_thread():
            self.monitor.join(6)
