"""Production presenter for MCP pairing: the on-demand Electron pairing window (G12).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Same on-demand pattern as the deletion confirm window (core_mcp_confirm_window):
Core spawns ``electron scripts/pairing-window.cjs`` with one JSON request line on
stdin and reads protocol lines from its stdout:

- ``{"event":"shown"}``                              the window is visible
- ``{"event":"decided","approved":bool,"accounts":[...],"capabilities":{...}}``
- ``{"event":"error","message":"..."}``              could not show (exit 2)

The window has no network ability and no token: the user's selection reaches Core
only through this process's stdout, which only Core can read. A window that cannot
be shown (no interactive desktop, no Electron runtime) raises
:class:`core_mcp_pairing.PairingUnavailable`; one that is closed, crashes or times
out after showing yields ``None`` (no approval). A ``confirm`` lease (keyed by the pairing id) is held while
the window is up so Core's idle countdown does not stop it.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from pathlib import Path

from core_mcp_confirm_window import _WindowProcess, _parse
from core_mcp_pairing import PairingUnavailable
from core_runtime import electron_sidecar_command

# The pairing window is the same class of on-demand local confirmation as the deletion
# confirm window, so it shares that lease kind (keyed by its own pairing id); core_leases
# needs no new kind.
PAIRING_LEASE_KIND = "confirm"
SHOW_MARGIN_SECONDS = 30.0
POLL_SECONDS = 0.25
REAP_SECONDS = 15.0
REQUEST_LINE_LIMIT = 64 * 1024


class ElectronPairingPresenter:
    """``presenter(payload, on_shown) -> {"approved", "accounts", "capabilities"} | None``."""

    def __init__(self, root, *, leases=None, electron_path=None, script_path=None, launcher=None,
                 margin=SHOW_MARGIN_SECONDS, clock=time.time):
        self.root = Path(root)
        self.leases = leases
        self._electron = electron_path
        self._script = script_path
        self._launcher = launcher
        self._margin = margin
        self._clock = clock

    def __call__(self, payload, on_shown=None):
        if not isinstance(payload, dict) or not isinstance(payload.get("pairingId"), str) or \
                not isinstance(payload.get("expiresAtMs"), int):
            raise PairingUnavailable("配对载荷不正确")
        deadline = payload["expiresAtMs"] / 1000 + self._margin
        if self.leases is not None:
            self.leases.acquire(PAIRING_LEASE_KIND, payload["pairingId"], pid=os.getpid())
        try:
            handle = self._spawn(payload)
            try:
                decision = self._read(handle, deadline, on_shown)
            except BaseException:
                handle.kill()
                handle.close()
                raise
            if decision is None:
                handle.kill()
                handle.close()
                return None
            threading.Thread(target=self._reap, args=(handle,), name="zhiji-mcp-pairing-reap",
                             daemon=True).start()
            return decision
        finally:
            if self.leases is not None:
                self.leases.release(PAIRING_LEASE_KIND, payload["pairingId"])

    def _spawn(self, payload):
        command = electron_sidecar_command(self.root, "pairing-window", electron=self._electron, script=self._script)
        if command is None:
            raise PairingUnavailable("本机没有可用的配对窗口运行时")
        line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(line) > REQUEST_LINE_LIMIT:
            raise PairingUnavailable("配对载荷超出可传递范围")
        if self._launcher is not None:
            return self._launcher(command, line)
        try:
            return _WindowProcess(command, line)
        except OSError as exc:
            raise PairingUnavailable(f"配对窗口无法启动：{exc}") from None

    def _read(self, handle, deadline, on_shown):
        lines, done = queue.Queue(), [False]
        threading.Thread(target=self._pump, args=(handle, lines, done),
                         name="zhiji-mcp-pairing-read", daemon=True).start()
        shown = False
        while True:
            remaining = deadline - self._clock()
            try:
                raw = lines.get(timeout=POLL_SECONDS if remaining > POLL_SECONDS else max(0.0, remaining))
            except queue.Empty:
                raw = None
            if raw is not None:
                event = _parse(raw)
                name = event.get("event") if event else None
                if name == "shown" and not shown:
                    shown = True
                    if callable(on_shown):
                        try:
                            on_shown()
                        except Exception:
                            pass
                elif name == "decided":
                    return _decision(event)
                elif name == "error":
                    if not shown:
                        raise PairingUnavailable("配对窗口未能显示：" + str(event.get("message", ""))[:200])
                    return None
            if done[0] and lines.empty() and handle.poll() is not None:
                if not shown:
                    raise PairingUnavailable("配对窗口未能显示（进程已退出）")
                return None
            if remaining <= 0:
                if not shown:
                    raise PairingUnavailable("配对窗口未能在时限内显示")
                return None

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


def _decision(event):
    """Only an explicit ``approved: true`` counts; anything malformed is a refusal."""
    if event.get("approved") is not True:
        return {"approved": False}
    accounts = event.get("accounts")
    capabilities = event.get("capabilities")
    if not isinstance(accounts, list) or not isinstance(capabilities, dict):
        return {"approved": False}
    return {"approved": True, "accounts": accounts, "capabilities": capabilities}
