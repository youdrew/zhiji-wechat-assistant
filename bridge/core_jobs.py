"""Process groups for plugins: a Windows Job Object, or a POSIX session elsewhere.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

A plugin and every helper it starts (model workers, Agent engines) share one
group. Ending the group ends all of them, and Core's exit closes the job handle,
which the kill-on-close limit turns into the same cleanup. This is fault
isolation only: the processes run with the user's own permissions.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _JOB_EXTENDED_LIMITS = 9
    _KILL_ON_JOB_CLOSE = 0x2000
    _JOB_MEMORY = 0x200
    _JOB_ACCOUNTING = 1
    _JOB_PROCESS_IDS = 3
    _SYNCHRONIZE = 0x00100000
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _BasicAccounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in (
            "TotalUserTime", "TotalKernelTime", "ThisPeriodTotalUserTime", "ThisPeriodTotalKernelTime")]
        _fields_ += [(name, wintypes.DWORD) for name in (
            "TotalPageFaultCount", "TotalProcesses", "ActiveProcesses", "TotalTerminatedProcesses")]

    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p)
    _kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL


class ProcessGroup:
    """One plugin's process tree. ``popen_options`` must be used to start the leader."""

    def __init__(self, memory_limit_bytes=None):
        self.memory_limit_bytes = memory_limit_bytes
        self.job = None
        self.leader = None
        self.lock = threading.RLock()
        if os.name == "nt":
            job = _kernel32.CreateJobObjectW(None, None)
            if not job:
                raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
            limits = _ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
            if memory_limit_bytes:
                limits.BasicLimitInformation.LimitFlags |= _JOB_MEMORY
                limits.JobMemoryLimit = int(memory_limit_bytes)
            if not _kernel32.SetInformationJobObject(job, _JOB_EXTENDED_LIMITS, ctypes.byref(limits),
                                                     ctypes.sizeof(limits)):
                error = ctypes.get_last_error()
                _kernel32.CloseHandle(job)
                raise OSError(error, "SetInformationJobObject failed")
            self.job = job

    @staticmethod
    def popen_options():
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    def adopt(self, process):
        """Put a freshly started leader in the group before it is told to start any helper."""
        self.leader = process
        if os.name != "nt":
            return
        handle = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, process.pid)
        if not handle:
            raise OSError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not _kernel32.AssignProcessToJobObject(self.job, handle):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            _kernel32.CloseHandle(handle)

    def terminate(self):
        """End every process in the group. Safe to call more than once."""
        with self.lock:
            self._terminate()

    def _terminate(self):
        deadline = time.monotonic() + 5
        members = []
        if os.name == "nt":
            if self.job:
                members = self._member_handles()
                _kernel32.TerminateJobObject(self.job, 1)
        elif self.leader is not None:
            try:
                os.killpg(self.leader.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if self.leader is not None:
            try:
                self.leader.kill()
            except OSError:
                pass
        try:
            if os.name == "nt" and self.job:
                accounting = _BasicAccounting()
                while time.monotonic() < deadline:
                    if not _kernel32.QueryInformationJobObject(
                            self.job, _JOB_ACCOUNTING, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
                        break
                    if not accounting.ActiveProcesses:
                        break
                    time.sleep(.01)
                # Job ActiveProcesses can reach zero before the process objects
                # signal exit and release file handles. Wait on each pinned member.
                for handle in members:
                    _kernel32.WaitForSingleObject(handle, max(0, int((deadline - time.monotonic()) * 1000)))
            if self.leader is not None:
                try:
                    self.leader.wait(timeout=max(0, deadline - time.monotonic()))
                except (subprocess.TimeoutExpired, OSError):
                    pass
        finally:
            for handle in members:
                _kernel32.CloseHandle(handle)

    def _member_handles(self):
        """Pin member process objects before termination removes them from the job's PID list."""
        for capacity in (64, 256, 1024, 4096, 16384):
            class ProcessIds(ctypes.Structure):
                _fields_ = [("assigned", wintypes.DWORD), ("count", wintypes.DWORD),
                            ("ids", ctypes.c_size_t * capacity)]
            members = ProcessIds()
            ok = _kernel32.QueryInformationJobObject(self.job, _JOB_PROCESS_IDS, ctypes.byref(members),
                                                     ctypes.sizeof(members), None)
            if not ok and ctypes.get_last_error() == 234:  # ERROR_MORE_DATA
                continue
            if not ok:
                return []
            handles = []
            for pid in members.ids[:min(members.count, capacity)]:
                handle = _kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
                if handle:
                    handles.append(handle)
            return handles
        return []

    def close(self):
        """End and reap the group before releasing its handle, including concurrent closes."""
        with self.lock:
            self._terminate()
            if os.name == "nt" and self.job:
                job, self.job = self.job, None
                _kernel32.CloseHandle(job)
