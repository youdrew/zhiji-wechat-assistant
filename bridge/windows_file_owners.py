"""Read-only Windows file ownership query; never closes or restarts applications."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from functools import lru_cache

INITIAL_PROCESS_CAPACITY = 64
MAX_PROCESS_CAPACITY = 4096


class _UniqueProcess(ctypes.Structure):
    _fields_ = [("pid", wintypes.DWORD), ("started", wintypes.FILETIME)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("process", _UniqueProcess), ("app", wintypes.WCHAR * 256),
                ("service", wintypes.WCHAR * 64), ("app_type", wintypes.DWORD),
                ("status", wintypes.DWORD), ("session", wintypes.DWORD),
                ("restartable", wintypes.BOOL)]


@lru_cache(maxsize=1)
def _api():
    if os.name != "nt":
        raise NotImplementedError("Windows file ownership unavailable")
    api = ctypes.WinDLL("rstrtmgr", use_last_error=True)
    api.RmStartSession.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR]
    api.RmRegisterResources.argtypes = [wintypes.DWORD, wintypes.UINT,
        ctypes.POINTER(wintypes.LPCWSTR), wintypes.UINT, ctypes.c_void_p,
        wintypes.UINT, ctypes.c_void_p]
    api.RmGetList.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT), ctypes.POINTER(_ProcessInfo), ctypes.POINTER(wintypes.DWORD)]
    api.RmEndSession.argtypes = [wintypes.DWORD]
    for name in ("RmStartSession", "RmRegisterResources", "RmGetList", "RmEndSession"):
        getattr(api, name).restype = wintypes.DWORD
    return api


def file_owners(paths):
    """Return (PID, creation time) pairs without requiring process handle duplication."""
    paths = tuple(dict.fromkeys(str(path) for path in paths))
    if not paths:
        return ()
    api = _api()
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(33)
    status = api.RmStartSession(ctypes.byref(session), 0, key)
    if status:
        raise OSError(status, "file ownership query could not start")
    try:
        resources = (wintypes.LPCWSTR * len(paths))(*paths)
        status = api.RmRegisterResources(session, len(paths), resources, 0, None, 0, None)
        if status:
            raise OSError(status, "file ownership resources could not be registered")
        needed, count, reasons = wintypes.UINT(), wintypes.UINT(INITIAL_PROCESS_CAPACITY), wintypes.DWORD()
        entries = (_ProcessInfo * INITIAL_PROCESS_CAPACITY)()
        # RmGetList refreshes ownership on every call, including a size-only
        # query. Start with a bounded buffer to avoid an extra complete scan.
        # A growing owner population still requires a new query, never reuse.
        for _attempt in range(4):
            status = api.RmGetList(session, ctypes.byref(needed), ctypes.byref(count), entries,
                                   ctypes.byref(reasons))
            if status == 0:
                if count.value > len(entries) or needed.value > len(entries):
                    raise OSError(234, "file ownership result exceeded capacity")
                return tuple((entry.process.pid,
                              ((entry.process.started.dwHighDateTime << 32) |
                               entry.process.started.dwLowDateTime) / 10_000_000 - 11_644_473_600)
                             for entry in entries[:count.value])
            if status != 234 or not len(entries) < needed.value <= MAX_PROCESS_CAPACITY:
                raise OSError(status, "file ownership query failed")
            count = wintypes.UINT(min(MAX_PROCESS_CAPACITY, max(len(entries) * 2, needed.value)))
            entries = (_ProcessInfo * count.value)()
        raise OSError(234, "file ownership changed repeatedly")
    finally:
        api.RmEndSession(session)
