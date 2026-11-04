"""Agent-opened windows: the UI host process, restricted window sessions and their guard (B2/B3).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

外部 Agent 经 MCP 请求"打开某个知己窗口"时，Core 不把任何 URL、令牌或数据交给
Agent；它自己拉起一个常驻的 Electron UI 宿主（``scripts/ui-host.cjs``），经
stdin/stdout JSONL 下发打开指令，并给每个窗口铸一枚只在内存里的受限会话令牌。
窗口里的每个请求都带 ``X-KYF-UI-Session``，由 :meth:`UiWindowService.guard`
在路由之前放行或拒绝（只读、只限本窗口的账号与联系人、只限模块插件）。

协议（每行一个 JSON 对象）：

Core → 宿主::

    {"op":"open","key","reqId","surface","url","hash","title","tab","token","theme"}
    {"op":"close","key","token"?}             聊天记录窗用令牌区分先后两次打开
    {"op":"select-tab","key","tab"}
    {"op":"quit"}

宿主 → Core::

    {"event":"ready"}                         启动完成，可以接收 open
    {"event":"opened"|"focused","key","reqId"} 对某次 open 的回答
    {"event":"error","reqId"?,"key"?,"code"}   reqId 存在时是对 open 的失败回答；
                                              启动期间的 code "no-display" 表示无显示环境
    {"event":"closed","key","token"?}        用户关掉了窗口

宿主缺运行时、启动失败、报告 no-display、15 s 内没有 ready → ``unavailable``
（进程被杀）；open 10 s 内没有回答 → ``failed``。宿主存活期间持有 ``("ui","host")``
租约；stdout 读到 EOF 时丢弃全部窗口会话并释放租约。

主窗口模块（app.main / app.settings）不经宿主：按需拉起主窗外壳
``real-client-shell.cjs --client-url <url> [--open-module <m>]``（单实例锁会把已在
运行的主窗唤到前台），以 GUI 租约是否出现判断结果。联系人聊天记录走受限宿主，
不需要主窗运行。
"""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from core_runtime import electron_sidecar_command, packaged_electron_executable
from core_ui_modules import CONTACT_SUBJECT_TYPE, UiError, resolve

UI_SESSION_HEADER = "X-KYF-UI-Session"
TOKEN_PREFIX = "zus_"
TOKEN_PATTERN = re.compile(r"zus_[0-9a-f]{64}")
HOST_LEASE = ("ui", "host")
MAX_WINDOWS_PER_CLIENT = 8
MIN_OPEN_INTERVAL_SECONDS = 2.0
READY_TIMEOUT_SECONDS = 15.0
REPLY_TIMEOUT_SECONDS = 10.0
SHELL_TIMEOUT_SECONDS = 15.0
SCOPE_CACHE_SECONDS = 2.0
LINE_LIMIT = 64 * 1024

# -- guard policy --------------------------------------------------------------------------------
# /api/friend/settings 不在此列：它带回账号级的 accountProfile（背景/风格），MCP 只把它当
# 账号级汇总开放，按联系人授权的窗口不能读；受限好友页也从不调用它（“更多”菜单被隐藏）。
FRIEND_READ_PATHS = frozenset({"/api/friend/overview", "/api/friend/memories", "/api/friend/memory",
                               "/api/friend/evidence", "/api/friend/observations", "/api/friend/message"})
SCOPED_READ_PATHS = FRIEND_READ_PATHS | {"/api/schemes", "/api/legacy-portrait"}
# kyf.friend-analysis 的 page.* 全部只读（overview/persona/chat/personaEvidence/message/round/trend）；
# scheme.status 是好友窗头部运行状态行的只读查询。scheme.estimate 可能触达模型，不放行。
READ_RPC_EXTRA = frozenset({"scheme.status"})
# 受限好友窗实际加载的 chatui 静态文件（chatui/friend.html 及其 <link>/<script>），别的一律拒绝。
FRIEND_STATIC_FILES = frozenset({"/friend.html", "/friend.css", "/friend-core.js", "/friend-memory.js",
                                 "/friend-run.js", "/plugin-frame.js", "/friend-scheme.js", "/friend.js"})
CHAT_STATIC_FILES = frozenset({"/history.html", "/history-panel.js", "/history-panel.css",
                               "/style.css", "/media.js", "/media.css"})
CHAT_READ_PATHS = frozenset({"/api/history/search", "/api/media"})
SDK_WEB_PREFIX = "/plugin-sdk/web/"
SDK_WEB_FILE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*\.(?:js|css)")


class UiGuardError(Exception):
    """A refused window-session request; always HTTP 403 with ``{"error": code}``."""

    def __init__(self, code="ui-session-forbidden"):
        super().__init__(code)
        self.code = code
        self.status = 403


# -- desktop / process plumbing ------------------------------------------------------------------
def interactive_desktop():
    """Whether this process can reach the interactive input desktop (Windows); True elsewhere.

    A Core started from session 0 / a service / a locked workstation has no input
    desktop: ``OpenInputDesktop`` returns NULL and no window would ever be seen.
    """
    if os.name != "nt":
        return True
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.OpenInputDesktop.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        user32.CloseDesktop.argtypes = [ctypes.c_void_p]
        handle = user32.OpenInputDesktop(0, False, 0x0001)  # DESKTOP_READOBJECTS
    except (OSError, AttributeError):
        return False
    if not handle:
        return False
    user32.CloseDesktop(handle)
    return True


class _HostProcess:
    """The default launcher's handle: one Electron UI host inside a killable job object."""

    def __init__(self, command, cwd=None):
        from core_jobs import ProcessGroup
        self.group = ProcessGroup()
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, cwd=cwd, **ProcessGroup.popen_options())
            self.group.adopt(self.proc)
        except BaseException:
            self.group.terminate()
            self.group.close()
            raise
        self.stdin, self.stdout, self.pid = self.proc.stdin, self.proc.stdout, self.proc.pid

    def poll(self):
        return self.proc.poll()

    def kill(self):
        self.group.terminate()
        self.group.close()
        for stream in (self.stdin, self.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            self.proc.wait(5)
        except (subprocess.TimeoutExpired, OSError):
            pass


def default_launcher(root):
    def launch(command):
        return _HostProcess(command, cwd=str(root) if root else None)
    return launch


def default_shell_launcher(root):
    """Start the main-window shell like ``start-real-client.open_client`` (outlives this call)."""
    def launch(command):
        from instance_identity import instance_id
        environment = {**os.environ, "KYF_INSTANCE_ID": instance_id(root)}
        return subprocess.Popen(command, cwd=str(root), env=environment, close_fds=True,
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    return launch


class _UiHost:
    """One running UI host: write lock, reply waiters and the stdout reader."""

    def __init__(self, handle):
        self.handle = handle
        self.ready = threading.Event()
        self.ready_error = None
        self.exited = False
        self.pending = {}  # reqId -> {"done": Event, "reply": dict | None}
        self.write_lock = threading.Lock()

    def start(self, on_event, on_exit):
        def pump():
            try:
                for raw in iter(self.handle.stdout.readline, b""):
                    if len(raw) > LINE_LIMIT:
                        continue
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(event, dict) and isinstance(event.get("event"), str):
                        on_event(self, event)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    self.handle.stdout.close()
                except (OSError, ValueError, AttributeError):
                    pass
                on_exit(self)
        threading.Thread(target=pump, name="zhiji-ui-host-read", daemon=True).start()

    def send(self, message):
        line = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
        with self.write_lock:
            try:
                self.handle.stdin.write(line)
                self.handle.stdin.flush()
                return True
            except (OSError, ValueError, AttributeError):
                return False

    def alive(self):
        if self.exited:
            return False
        try:
            return self.handle.poll() is None
        except Exception:
            return False

    def kill(self):
        try:
            self.handle.kill()
        except Exception:
            pass


def window_key(client_id, surface, account_id, subject_id):
    raw = f"{client_id}|{surface}|{account_id or ''}|{subject_id or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class UiWindowService:
    """Opens agentOpenable modules as real windows and guards their restricted sessions.

    All collaborators are injected (no import of the MCP proxy):

    - ``launcher(command) -> handle`` starts the UI host; the handle has binary
      ``stdin``/``stdout``, ``poll()``, ``kill()`` and optional ``pid``.
    - ``desktop_probe() -> bool``: an interactive desktop exists (default :func:`interactive_desktop`).
    - ``registry_lookup(accountId) -> {"account": internal, ...} | None`` (``registry_records``).
    - ``scope_for(principal_info) -> Scope`` with ``accounts()`` and
      ``allows(accountId, subject_type, subject_id)``; may raise when the principal is stale.
    - ``plugin_enabled(plugin_id, account) -> bool``; ``live_account() -> str | None``.
    - ``leases``: :class:`core_leases.LeaseService`; ``clock``: monotonic seconds.
    - ``current_auth(clientId) -> {"authVersion", "fingerprint"} | None``: the guard's
      re-check. Without it every guarded request is refused (fail closed).
    - ``task_lookup(taskId) -> {"account", "user", ...} | None`` for ``GET /api/task``.
    - ``display_name(account, subjectId) -> str`` for the window title.
    """

    def __init__(self, *, launcher=None, desktop_probe=None, registry_lookup, scope_for, plugin_enabled,
                 live_account, leases=None, clock=time.monotonic, current_auth=None, task_lookup=None,
                 display_name=None, client_url="http://127.0.0.1/", root=None, electron=None,
                 shell_launcher=None, theme=None, ready_timeout=READY_TIMEOUT_SECONDS,
                 reply_timeout=REPLY_TIMEOUT_SECONDS, shell_timeout=SHELL_TIMEOUT_SECONDS,
                 scope_cache_seconds=SCOPE_CACHE_SECONDS, max_windows=MAX_WINDOWS_PER_CLIENT,
                 min_interval=MIN_OPEN_INTERVAL_SECONDS):
        self.root = Path(root) if root else None
        self._launcher = launcher or default_launcher(self.root)
        self._shell_launcher = shell_launcher or (default_shell_launcher(self.root) if self.root else None)
        self._desktop_probe = desktop_probe or interactive_desktop
        self._registry_lookup = registry_lookup
        self._scope_for = scope_for
        self._plugin_enabled = plugin_enabled
        self._live_account = live_account
        self._leases = leases
        self._clock = clock
        self._current_auth = current_auth
        self._task_lookup = task_lookup
        self._display_name = display_name
        self._theme = theme
        if electron is None:
            def electron():
                if self.root is None:
                    return None
                from core_runtime import electron_executable
                return electron_executable(self.root)
        self._electron = electron
        self.client_url = client_url if client_url.endswith("/") else client_url + "/"
        self.ready_timeout, self.reply_timeout = ready_timeout, reply_timeout
        self.shell_timeout, self.scope_cache_seconds = shell_timeout, scope_cache_seconds
        self.max_windows, self.min_interval = max_windows, min_interval
        self._lock = threading.RLock()
        self._host_lock = threading.Lock()  # one host startup at a time
        self._key_locks = {}  # window key -> [Lock, users]
        self._host = None
        self._sessions = {}  # token -> binding
        self._windows = {}  # key -> token
        self._last_open = {}  # clientId -> clock()
        self._checked = {}  # token -> (expires, ok)

    # -- public: open --------------------------------------------------------------------------
    def open(self, principal_info, scope, module, account_id=None, subject_id=None):
        """Open or focus one module window for one MCP principal.

        Returns ``{"status": "opened"|"focused"|"unavailable"|"failed", "module", "windowKey", "reason"?}``;
        raises :class:`UiError` for request errors (module-unknown, module-not-openable,
        context-invalid, account-not-authorized, contact-not-authorized, account-unknown).
        """
        context = {key: value for key, value in (("accountId", account_id), ("subjectId", subject_id))
                   if value is not None}
        spec = resolve(module, context)
        client_id = principal_info["clientId"]
        account = None
        if spec["context"] == "contact":
            readable = scope.accounts()
            if readable is not None and account_id not in readable:
                raise UiError("account-not-authorized", "该账号未获授权")
            if not scope.allows(account_id, CONTACT_SUBJECT_TYPE, subject_id):
                raise UiError("contact-not-authorized", "该联系人未获授权")
            record = self._registry_lookup(account_id)
            if not isinstance(record, dict) or not isinstance(record.get("account"), str) or not record["account"]:
                raise UiError("account-unknown", "该账号不是本机已保存账号")
            account = record["account"]
        key = window_key(client_id, spec["surface"], account_id, subject_id) if spec["context"] == "contact" else "main"
        result = {"module": module, "windowKey": key}
        with self._lock:
            now = self._clock()
            last = self._last_open.get(client_id)
            if last is not None and now - last < self.min_interval:
                return {**result, "status": "failed", "reason": "rate-limited"}
            self._last_open[client_id] = now
        # 下面的等待（宿主就绪最多 15 s、open 回答最多 10 s、主窗租约最多 15 s）都不持有
        # 全局锁：宿主启动由 _host_lock 串行（不会起两个宿主），同一窗口由按 key 的锁串行
        # （不会铸两枚令牌、开两个窗口），不同客户端/窗口互不阻塞。
        for plugin in spec["plugins"]:
            if not self._plugin_enabled(plugin, account):
                return {**result, "status": "unavailable", "reason": "plugin-disabled"}
        if spec["context"] == "contact" and self._live_account() != account:
            return {**result, "status": "unavailable", "reason": "account-not-live"}
        if not self._desktop_probe():
            return {**result, "status": "unavailable", "reason": "no-interactive-desktop"}
        if spec["surface"] == "main":
            with self._key_lock(key):
                return {**result, **self._open_main(spec)}
        return {**result, **self._open_restricted(principal_info, spec, account, key)}

    # -- public: lifecycle -----------------------------------------------------------------------
    def on_version_change(self, client_id):
        """McpStore version listener: revoke this client's windows and close them."""
        self._revoke_windows(lambda binding: binding["clientId"] == client_id)

    def close_client(self, client_id):
        self.on_version_change(client_id)

    def close_all(self):
        """MCP as a whole went away (kyf.mcp disabled/uninstalled or the master switch turned
        off): revoke every Agent-opened window session and close the windows at once. The
        host process stays up for the next open; it idles out on its own lease."""
        return self._revoke_windows(lambda binding: True)

    def on_plugin_disabled(self, plugin_id):
        """A module plugin was disabled: revoke every window session that shows it and close the windows.

        The guard calls this itself when its recheck finds the plugin disabled; Core may
        also call it directly from the plugin-toggle path so idle windows close at once.
        """
        self._revoke_windows(lambda binding: plugin_id in binding["plugins"])

    @staticmethod
    def _close_command(key, token, surface):
        return {"op": "close", "key": key, **({"token": token} if surface == "chat" else {})}

    def _revoke_windows(self, matches):
        with self._lock:
            windows = [(token, binding) for token, binding in self._sessions.items() if matches(binding)]
            host = self._host
            for _, binding in windows:
                self._drop_key(binding["key"])
            if host is not None:
                revoked = {token for token, _ in windows}
                for waiter in host.pending.values():
                    if waiter["token"] in revoked:
                        waiter["revoked"] = True
                        waiter["done"].set()
        if host is not None:
            for token, binding in windows:
                if not host.send(self._close_command(binding["key"], token, binding["surface"])):
                    host.kill()
                    break
        return len(windows)

    def windows(self, client_id):
        with self._lock:
            return [{"windowKey": binding["key"], "module": binding["module"]}
                    for binding in self._sessions.values() if binding["clientId"] == client_id]

    def shutdown(self):
        with self._lock:
            host, self._host = self._host, None
            self._sessions.clear()
            self._windows.clear()
            self._checked.clear()
        if host is not None:
            host.send({"op": "quit"})
            host.kill()
            self._release_lease()

    # -- public: guard -------------------------------------------------------------------------
    def guard(self, token, method, path, query, body):
        """Allow one HTTP request of a restricted window session, or raise :class:`UiGuardError`."""
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise UiGuardError()
        with self._lock:
            binding = self._sessions.get(token)
        if binding is None or not self._session_current(token, binding):
            raise UiGuardError()
        query = query if isinstance(query, dict) else {}
        if method == "GET":
            return self._guard_get(binding, path, query)
        if method == "POST" and path == "/api/plugins/rpc" and binding["surface"] == "friend":
            return self._guard_rpc(binding, body)
        if method in ("POST", "DELETE", "PUT", "PATCH"):
            raise UiGuardError("ui-session-read-only")
        raise UiGuardError()

    # -- restricted windows --------------------------------------------------------------------
    @contextlib.contextmanager
    def _key_lock(self, key):
        """Serialize opens of one window key without blocking other keys."""
        with self._lock:
            entry = self._key_locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._lock:
                entry[1] -= 1
                if entry[1] == 0 and self._key_locks.get(key) is entry:
                    del self._key_locks[key]

    def _open_restricted(self, principal_info, spec, account, key):
        host, reason = self._ensure_host()
        if host is None:
            return {"status": "unavailable", "reason": reason}
        with self._key_lock(key):
            return self._open_restricted_window(host, principal_info, spec, account, key)

    def _open_restricted_window(self, host, principal_info, spec, account, key):
        client_id = principal_info["clientId"]
        chat = spec["surface"] == "chat"
        with self._lock:
            token = self._windows.get(key)
            existing = token is not None and token in self._sessions
            if not existing:
                if sum(1 for binding in self._sessions.values() if binding["clientId"] == client_id) \
                        >= self.max_windows:
                    return {"status": "failed", "reason": "window-limit"}
                token = TOKEN_PREFIX + secrets.token_hex(32)
                self._sessions[token] = {
                    "clientId": client_id, "authVersion": principal_info.get("authVersion"),
                    "fingerprint": principal_info.get("fingerprint"), "module": spec["module"],
                    "surface": spec["surface"], "accountId": spec["accountId"], "account": account,
                    "subjectId": spec["subjectId"], "plugins": frozenset(spec["plugins"]), "key": key,
                    "tab": spec["tab"]}
                self._windows[key] = token
            binding = self._sessions[token]
            previous = binding["module"]
            request_id = secrets.token_hex(8)
            waiter = {"done": threading.Event(), "reply": None, "key": key, "token": token, "revoked": False}
            host.pending[request_id] = waiter
        name = spec["subjectId"]
        if callable(self._display_name):
            try:
                name = self._display_name(account, spec["subjectId"]) or name
            except Exception:
                pass
        if chat:
            name = "".join(char for char in str(name) if ord(char) >= 32 and ord(char) != 127)[:48] or "联系人"
        page, title = ("history.html", "聊天记录") if chat else ("friend.html", "知己分析")
        message = {"op": "open", "key": key, "reqId": request_id, "surface": spec["surface"],
                   "url": self.client_url + page,
                   "hash": urlencode({"account": account, "user": spec["subjectId"], "name": name}),
                   "title": f"{name} · {title}", "tab": spec["tab"], "token": token,
                   "theme": self._current_theme()}
        # A version change during desktop startup may predate window registration. Recheck
        # after registration, then order the send against the listener that drops the binding.
        allowed = not chat or self._recheck(binding)
        with self._lock:
            if not allowed or waiter["revoked"]:
                host.pending.pop(request_id, None)
                if self._windows.get(key) == token:
                    self._drop_key(key)
                denied = True
            else:
                denied = False
                sent = host.send(message)
        if denied:
            if existing and not host.send(self._close_command(key, token, spec["surface"])):
                host.kill()
            return {"status": "failed", "reason": "authorization-changed"}
        replied = sent and waiter["done"].wait(self.reply_timeout)
        with self._lock:
            host.pending.pop(request_id, None)
            if waiter["revoked"]:
                return {"status": "failed", "reason": "authorization-changed"}
        reply = waiter["reply"] if replied else None
        if reply is None or reply.get("event") not in ("opened", "focused"):
            if not existing:
                with self._lock:
                    if self._windows.get(key) == token:
                        self._drop_key(key)
                if not host.send(self._close_command(key, token, spec["surface"])):
                    host.kill()
            if reply is None:
                return {"status": "failed", "reason": "host-exited" if host.exited else "host-timeout"}
            code = str(reply.get("code") or "host-error")[:64]
            return {"status": "unavailable" if code == "no-display" else "failed", "reason": code}
        status = reply["event"]
        if existing:
            # 同一窗口换模块：聚焦后切到该模块的页签（重新打开的窗口已按 open 里的 tab 加载）。
            if previous != spec["module"] and status == "focused":
                host.send({"op": "select-tab", "key": key, "tab": spec["tab"]})
            with self._lock:
                binding = self._sessions.get(token)
                if binding is not None:
                    binding.update(module=spec["module"], tab=spec["tab"],
                                   plugins=binding["plugins"] | frozenset(spec["plugins"]))
        return {"status": status}

    def _ensure_host(self):
        with self._lock:
            host = self._host
        if host is not None and host.alive() and host.ready.is_set() and not host.ready_error:
            return host, None
        with self._host_lock:
            return self._start_host()

    def _start_host(self):
        # Under _host_lock self._host is either None or a host whose startup already succeeded.
        with self._lock:
            host = self._host
        if host is not None and host.alive():
            return host, None
        if host is not None:
            self._on_exit(host)
            host.kill()
        command = electron_sidecar_command(self.root, "ui-host", electron=self._electron())
        if command is None:
            return None, "runtime-missing"
        command.extend(("--client-url", self.client_url))
        try:
            handle = self._launcher(command)
        except Exception:
            return None, "spawn-failed"
        host = _UiHost(handle)
        with self._lock:
            self._host = host
        host.start(self._on_event, self._on_exit)
        ready = host.ready.wait(self.ready_timeout)
        with self._lock:
            ok = ready and not host.ready_error and not host.exited and self._host is host
            if ok and self._leases is not None:
                self._leases.acquire(*HOST_LEASE, pid=getattr(handle, "pid", None))
            if not ok and self._host is host:
                self._host = None
        if not ok:
            # The reason is read before kill(): killing closes stdout and the reader marks the host exited.
            reason = host.ready_error or ("host-exited" if host.exited else "host-not-ready")
            host.kill()
            return None, reason
        return host, None

    def _on_event(self, host, event):
        name = event.get("event")
        with self._lock:
            if name == "ready":
                host.ready.set()
                return
            request_id = event.get("reqId")
            if name in ("opened", "focused", "error") and isinstance(request_id, str) and \
                    request_id in host.pending:
                waiter = host.pending[request_id]
                if event.get("key") != waiter["key"]:
                    return
                waiter["reply"] = event
                waiter["done"].set()
                return
            if name == "error" and not host.ready.is_set():
                host.ready_error = str(event.get("code") or "host-error")[:64]
                host.ready.set()
                return
            if name == "closed" and host is self._host and isinstance(event.get("key"), str):
                key = event["key"]
                token = self._windows.get(key)
                binding = self._sessions.get(token)
                if binding and (binding["surface"] != "chat" or event.get("token") == token):
                    self._drop_key(key)

    def _on_exit(self, host):
        with self._lock:
            if host.exited:
                return
            host.exited = True
            host.ready.set()
            for waiter in host.pending.values():
                waiter["done"].set()
            current = self._host is host
            if current:
                self._host = None
                self._sessions.clear()
                self._windows.clear()
                self._checked.clear()
        if current:
            self._release_lease()

    def _release_lease(self):
        if self._leases is not None:
            self._leases.release(*HOST_LEASE)

    def _drop_key(self, key):
        token = self._windows.pop(key, None)
        if token is not None:
            self._sessions.pop(token, None)
            self._checked.pop(token, None)

    def _current_theme(self):
        if callable(self._theme):
            try:
                value = self._theme()
            except Exception:
                value = None
            if value in ("light", "dark", "system"):
                return value
        return "system"

    # -- main window ---------------------------------------------------------------------------
    def _open_main(self, spec):
        running = self._leases is not None and self._leases.count("gui") > 0
        electron = self._electron()
        if electron:
            script = str(self.root / "scripts" / "real-client-shell.cjs") if self.root else "scripts/real-client-shell.cjs"
            command = [electron, script, "--client-url", self.client_url]
        else:
            packaged = packaged_electron_executable(self.root)
            if not packaged:
                command = None
            elif running:
                # The GUI already holds the instance lock and this bridge is up: start the shell
                # directly so it hands --open-module over at once, skipping the desktop launcher.
                command = [packaged, "--kyf-shell", "--client-url", self.client_url]
            else:
                command = [packaged]
        if command is None or self._shell_launcher is None:
            return {"status": "unavailable", "reason": "runtime-missing"}
        if spec.get("openModule"):
            command += ["--open-module", spec["openModule"]]
        try:
            self._shell_launcher(command)
        except Exception:
            return {"status": "unavailable", "reason": "spawn-failed"}
        if running:
            return {"status": "focused"}
        deadline = time.monotonic() + self.shell_timeout
        while time.monotonic() < deadline:
            if self._leases is not None and self._leases.count("gui") > 0:
                return {"status": "opened"}
            time.sleep(0.05)
        return {"status": "failed", "reason": "shell-timeout"}

    # -- guard internals -----------------------------------------------------------------------
    def _session_current(self, token, binding):
        now = self._clock()
        with self._lock:
            cached = self._checked.get(token)
        if cached is not None and cached[0] > now:
            return cached[1]
        disabled = self._disabled_plugin(binding)
        if disabled is not None:
            if disabled is not False:
                self.on_plugin_disabled(disabled)
            return False
        ok = self._recheck(binding)
        with self._lock:
            if token in self._sessions:
                self._checked[token] = (now + self.scope_cache_seconds, ok)
        return ok

    def _disabled_plugin(self, binding):
        """The first module plugin no longer enabled, ``False`` when the check itself failed, else None.

        Checked on the same cadence as scope (each guard outside the cache window), so a
        disabled plugin stops an open window within ``scope_cache_seconds``.
        """
        for plugin in sorted(binding["plugins"]):
            try:
                enabled = self._plugin_enabled(plugin, binding["account"])
            except Exception:
                return False
            if not enabled:
                return plugin
        return None

    def _recheck(self, binding):
        if not callable(self._current_auth):
            return False
        try:
            auth = self._current_auth(binding["clientId"])
            if not isinstance(auth, dict) or auth.get("authVersion") != binding["authVersion"] or \
                    auth.get("fingerprint") != binding["fingerprint"]:
                return False
            scope = self._scope_for({"clientId": binding["clientId"], "authVersion": binding["authVersion"],
                                     "fingerprint": binding["fingerprint"]})
            readable = scope.accounts()
            if readable is not None and binding["accountId"] not in readable:
                return False
            return bool(scope.allows(binding["accountId"], CONTACT_SUBJECT_TYPE, binding["subjectId"]))
        except Exception:
            return False

    @staticmethod
    def _match(binding, values, required=()):
        if not isinstance(values, dict):
            raise UiGuardError()
        expected = {"account": binding["account"], "user": binding["subjectId"]}
        for name in ("account", "user"):
            if name in values:
                if values[name] != expected[name]:
                    raise UiGuardError()
            elif name in required:
                raise UiGuardError()

    def _guard_get(self, binding, path, query):
        if not isinstance(path, str):
            raise UiGuardError()
        if binding["surface"] == "chat":
            if path in CHAT_STATIC_FILES:
                return None
            if path in CHAT_READ_PATHS:
                return self._match(binding, query, ("account", "user"))
            raise UiGuardError()
        if path.startswith("/plugin-pages/"):
            plugin = path[len("/plugin-pages/"):].partition("/")[0]
            if plugin not in binding["plugins"]:
                raise UiGuardError()
            return None
        if path.startswith(SDK_WEB_PREFIX):
            if not SDK_WEB_FILE.fullmatch(path[len(SDK_WEB_PREFIX):]):
                raise UiGuardError()
            return None
        if path in FRIEND_STATIC_FILES:
            return None
        if path in SCOPED_READ_PATHS:
            return self._match(binding, query, ("account", "user"))
        if path == "/api/tasks":
            # 列表必须按本窗口模块的插件过滤；省略 plugin 会返回该联系人所有插件的任务。
            self._match(binding, query, ("account", "user"))
            if query.get("plugin") not in binding["plugins"]:
                raise UiGuardError()
            return None
        if path == "/api/task":
            task = None
            if callable(self._task_lookup) and isinstance(query.get("id"), str):
                try:
                    task = self._task_lookup(query["id"])
                except Exception:
                    task = None
            if not isinstance(task, dict) or task.get("plugin") not in binding["plugins"]:
                raise UiGuardError()
            return self._match(binding, {"account": task.get("account"), "user": task.get("user")},
                               ("account", "user"))
        if path == "/api/media":
            self._match(binding, query, ("user",))
            if "account" not in query:
                try:
                    live = self._live_account()
                except Exception:
                    live = None
                if live != binding["account"]:
                    raise UiGuardError()
            return None
        raise UiGuardError()

    def _guard_rpc(self, binding, body):
        if not isinstance(body, dict) or body.get("plugin") not in binding["plugins"]:
            raise UiGuardError()
        method = body.get("method")
        if not isinstance(method, str) or not (method.startswith("page.") or method in READ_RPC_EXTRA):
            raise UiGuardError("ui-session-read-only")
        self._match(binding, body, ("account", "user"))
        return None
