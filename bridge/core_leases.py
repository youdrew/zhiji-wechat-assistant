"""Core's shared run leases: the GUI, every MCP connection and running tasks.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The bridge process stops only after the last lease has ended and a bounded idle
period has passed; a GUI exit or an MCP disconnect never stops a Core that the
other still uses. Every lease records its holder's process id when one exists
(the Electron shell, an MCP stdio adapter), and a reaper reclaims leases whose
holder process died without releasing. Tasks run inside Core itself, so their
leases carry no pid and end with the task. The idle clock advances on every
acquire, refresh and release; a Core that never held a lease keeps the previous
behavior of running until someone stops it explicitly.

The idle limit defaults to 300 s and is configurable through the
``KYF_CORE_IDLE_EXIT_SECONDS`` environment variable (0 or less disables the
idle exit entirely).
"""
from __future__ import annotations

import ctypes
import os
import threading
import time

DEFAULT_IDLE_EXIT_SECONDS = 300
REAP_INTERVAL_SECONDS = 15
# "confirm" 是阶段 5 的按需确认窗：一次高影响删除的本机确认展示期间持有租约。
KINDS = ("gui", "mcp", "task", "confirm")


def idle_exit_seconds(env=None):
    raw = (env if env is not None else os.environ).get("KYF_CORE_IDLE_EXIT_SECONDS")
    if raw is None:
        return DEFAULT_IDLE_EXIT_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_IDLE_EXIT_SECONDS
    return value if value > 0 else 0


def pid_alive(pid):
    """Whether a process id currently names a live process (cheap ownership probe)."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False  # A gone PID leaves no accessible object behind.
        try:
            code = ctypes.c_uint32()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class LeaseService:
    """Reference-counted holders of one Core, with process-death reclaim and idle bookkeeping."""

    def __init__(self, *, idle_seconds=None, monotonic=time.monotonic, alive=pid_alive):
        self.idle_seconds = idle_exit_seconds() if idle_seconds is None else idle_seconds
        self.monotonic = monotonic
        self.alive = alive
        self.lock = threading.Lock()
        self.leases = {}  # (kind, key) -> {"pid": int|None, "acquired": float, "activity": float}
        self._has_held = False
        self._last_activity = None

    def acquire(self, kind, key, *, pid=None):
        """Take or refresh one lease; the same holder re-acquiring just refreshes it."""
        if kind not in KINDS:
            raise ValueError("unknown lease kind")
        if not isinstance(key, str) or not 1 <= len(key) <= 256:
            raise ValueError("invalid lease key")
        now = self.monotonic()
        with self.lock:
            self.leases[(kind, key)] = {"pid": pid, "acquired": now, "activity": now}
            self._has_held = True
            self._last_activity = now

    def refresh(self, kind, key):
        now = self.monotonic()
        with self.lock:
            lease = self.leases.get((kind, key))
            if lease is not None:
                lease["activity"] = now
                self._last_activity = now

    def release(self, kind, key):
        now = self.monotonic()
        with self.lock:
            if self.leases.pop((kind, key), None) is not None:
                self._last_activity = now

    def count(self, kind=None):
        with self.lock:
            if kind is None:
                return len(self.leases)
            return sum(1 for (lease_kind, _key) in self.leases if lease_kind == kind)

    def holders(self, kind=None):
        with self.lock:
            return [{"kind": lease_kind, "key": key, "pid": lease["pid"]}
                    for (lease_kind, key), lease in self.leases.items()
                    if kind is None or lease_kind == kind]

    def reap_dead(self):
        """Drop leases whose holder process is gone; returns the reclaimed keys."""
        with self.lock:
            dead = [key for key, lease in self.leases.items()
                    if lease["pid"] is not None and not self.alive(lease["pid"])]
        for key in dead:
            self.release(*key)
        return dead

    def should_exit(self):
        """True only after at least one lease was held, none remains and the idle period passed.

        A Core that never served a lease holder keeps running until an explicit stop.
        """
        now = self.monotonic()
        with self.lock:
            if self.leases or not self._has_held or self.idle_seconds <= 0:
                return False
            if self._last_activity is None:
                return False
            return now - self._last_activity >= self.idle_seconds

    def idle_deadline(self):
        """When the idle exit would fire, or None while leases are held or none was ever held."""
        with self.lock:
            if self.leases or not self._has_held or self._last_activity is None or self.idle_seconds <= 0:
                return None
            return self._last_activity + self.idle_seconds
