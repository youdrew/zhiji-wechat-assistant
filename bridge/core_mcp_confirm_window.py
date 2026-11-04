"""Production presenter for high-impact deletion confirmations: the Electron window (阶段 5, §7).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

呈现层适配器：把确认服务的 presenter 契约（阻塞调用、返回明确决定）接到按需
拉起的独立 Electron 确认窗。窗口自己经 MCP 设置通道（POST mcp/confirmations）
回传一次性令牌；本适配器只读窗口进程的 stdout 协议行并转成 presenter 返回值：

- 进程报告 ``decided`` 行 → 按行内 ``granted`` 返回（与设置通道回传的结论
  汇合在 core_mcp_confirm._apply，迟到/重复决定被幂等拒绝）；
- 窗口展示后崩溃、被杀、静默或超时 → 拒绝（``False``），绝不猜成确认；
- 无法启动 Electron 或窗口始终无法显示（无可交互桌面、二进制缺失）→ 抛出
  :class:`ConfirmUnavailable`，由确认服务按"确认界面不可用"失败关闭。

凭证语义（600s/300s/一次性/绑定）完全在 core_mcp_confirm；本模块不做任何
凭证判断，也不保存确认状态。展示期间持有 ``confirm`` 租约，计入 Core 的
"最后一个租约"判定；窗口进程按阶段 2 的按需组件模式随用随起、用完即收。
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from core_jobs import ProcessGroup
from core_mcp_confirm import ConfirmUnavailable
from core_runtime import electron_sidecar_command

CONFIRM_LEASE_KIND = "confirm"
# 确认到期后仍留给窗口收尾（打印结论行、退出）的宽限；确认服务在这之后本就
# 一律拒绝，宽限只影响我们等窗口自述多久。
SHOW_MARGIN_SECONDS = 30.0
POLL_SECONDS = 0.25
REAP_SECONDS = 15.0
REQUEST_LINE_LIMIT = 64 * 1024


class _WindowProcess:
    """One spawned confirm-window process inside a killable job object."""

    def __init__(self, command, request_line):
        self.group = ProcessGroup()
        try:
            self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, **ProcessGroup.popen_options())
            self.group.adopt(self.proc)
        except BaseException:
            self.group.terminate()
            self.group.close()
            raise
        self.stdout = self.proc.stdout
        try:
            self.proc.stdin.write(request_line)
            self.proc.stdin.close()
        except OSError:
            pass  # 进程已消失：读取循环随即观察到退出，按协议处理。

    def poll(self):
        return self.proc.poll()

    def kill(self):
        self.group.terminate()

    def wait(self, timeout=None):
        try:
            return self.proc.wait(timeout)
        except (subprocess.TimeoutExpired, OSError):
            return None

    def close(self):
        self.group.close()


class ElectronConfirmPresenter:
    """Raise ``scripts/confirm-window.cjs`` per confirmation and read its decision."""

    def __init__(self, root, core_url, admin_token=None, *, leases=None,
                 electron_path=None, script_path=None, launcher=None,
                 margin=SHOW_MARGIN_SECONDS, clock=time.time):
        self.root = Path(root)
        self.core_url = core_url
        self.admin_token = admin_token  # callable -> str | None（kyf.mcp 设置通道令牌）
        self.leases = leases
        self._electron = electron_path
        self._script = script_path
        self._launcher = launcher
        self._margin = margin
        self._clock = clock

    def __call__(self, payload):
        request = self._request(payload)
        deadline = request["expiresAtMs"] / 1000 + self._margin
        if self.leases is not None:
            self.leases.acquire(CONFIRM_LEASE_KIND, request["confirmId"], pid=os.getpid())
        try:
            return self._present(request, deadline)
        finally:
            if self.leases is not None:
                self.leases.release(CONFIRM_LEASE_KIND, request["confirmId"])

    # -- protocol --------------------------------------------------------------
    def _present(self, request, deadline):
        handle = self._spawn(request)
        try:
            shown, decided = self._read(handle, deadline)
        except BaseException:
            handle.kill()
            handle.close()
            raise
        if decided is None:
            # 展示后没有等到明确决定：窗口崩溃、静默或超时，一律按拒绝收尾。
            handle.kill()
            handle.close()
            return False
        # 已有决定：窗口自行退出；留一个守护线程兜底回收僵死窗口。
        threading.Thread(target=self._reap, args=(handle,), name="zhiji-mcp-confirm-reap",
                         daemon=True).start()
        return decided

    def _spawn(self, request):
        command = electron_sidecar_command(self.root, "confirm-window", electron=self._electron, script=self._script)
        if command is None:
            raise ConfirmUnavailable("本机没有可用的确认窗口运行时，不能发起高影响删除")
        line = json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(line) > REQUEST_LINE_LIMIT:
            raise ConfirmUnavailable("确认载荷超出可传递范围")
        if self._launcher is not None:
            return self._launcher(command, line)
        try:
            return _WindowProcess(command, line)
        except OSError as exc:
            raise ConfirmUnavailable(f"确认窗口无法启动：{exc}") from None

    def _read(self, handle, deadline):
        """Drain the protocol lines until a decision, exit or deadline."""
        lines, done = queue.Queue(), [False]
        threading.Thread(target=self._pump, args=(handle, lines, done),
                         name="zhiji-mcp-confirm-read", daemon=True).start()
        shown = False
        while True:
            remaining = deadline - self._clock()
            try:
                raw = lines.get(timeout=POLL_SECONDS if remaining > POLL_SECONDS else max(0.0, remaining))
            except queue.Empty:
                raw = None
            if raw is not None:
                event = _parse(raw)
                if event is not None:
                    name = event.get("event")
                    if name == "shown":
                        shown = True
                    elif name == "decided":
                        return shown, bool(event.get("granted"))
                    elif name == "error":
                        message = str(event.get("message", ""))[:200]
                        if not shown:
                            raise ConfirmUnavailable(f"确认窗口未能显示：{message}")
                        return True, None  # 展示后出错：按拒绝收尾。
            if done[0] and lines.empty() and handle.poll() is not None:
                if not shown:
                    raise ConfirmUnavailable("确认窗口未能显示（进程已退出）")
                return True, None  # 展示后退出但没给结论：拒绝。
            if remaining <= 0:
                if not shown:
                    raise ConfirmUnavailable("确认窗口未能在时限内显示")
                return True, None

    @staticmethod
    def _pump(handle, lines, done):
        try:
            for raw in iter(handle.stdout.readline, b""):
                lines.put(raw)
        except (OSError, ValueError):
            pass
        finally:
            done[0] = True

    def _reap(self, handle):
        handle.wait(REAP_SECONDS)
        if handle.poll() is None:
            handle.kill()
        handle.close()

    # -- payload ---------------------------------------------------------------
    def _request(self, payload):
        """The flat stdin request: the confirm payload plus the settings-channel fields."""
        if not isinstance(payload, dict) or not isinstance(payload.get("confirmId"), str) or \
                not payload["confirmId"] or not isinstance(payload.get("expiresAtMs"), int):
            raise ConfirmUnavailable("确认载荷不正确")
        request = dict(payload)
        token = ""
        if callable(self.admin_token):
            try:
                token = self.admin_token()
            except Exception:
                token = ""  # 令牌不可用只影响设置通道回传，决定仍经 presenter 返回值生效。
        request["core"] = self.core_url
        request["adminToken"] = token if isinstance(token, str) else ""
        return request


def _parse(raw):
    try:
        event = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    return event if isinstance(event, dict) else None
