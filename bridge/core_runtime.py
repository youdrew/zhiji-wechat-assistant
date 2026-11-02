"""Locate the bundled Node and Python runtimes Core starts helper processes with.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import os
import shutil
import site
import sys
from pathlib import Path

# Only the variables a helper process needs to start; credentials never travel in the environment.
BASE_ENVIRONMENT = ("SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "TEMP", "TMP",
                    "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")


def node_executable(root):
    root = Path(root)
    configured = os.environ.get("KYF_NODE")
    if configured and Path(configured).is_file():
        return configured
    bundled = root / "runtime" / "node" / "node.exe"
    if bundled.is_file():
        return str(bundled)
    return shutil.which("node")


def electron_executable(root):
    """The dedicated Electron shell Core raises on-demand windows with, or None.

    与 scripts/start-real-client.py 打开的独立外壳同一个二进制；缺失（未安装
    依赖或打包布局不含外壳）时返回 None，调用方按"无法显示窗口"失败关闭。
    """
    root = Path(root)
    configured = os.environ.get("KYF_ELECTRON")
    if configured and Path(configured).is_file():
        return configured
    bundled = root / "node_modules" / "electron" / "dist" / "electron.exe"
    if bundled.is_file():
        return str(bundled)
    return None


def python_executable(root):
    root = Path(root)
    configured = os.environ.get("KYF_PYTHON")
    if configured and Path(configured).is_file():
        return configured
    bundled = root / "runtime" / "python" / "python.exe"
    if bundled.is_file():
        return str(bundled)
    return sys.executable


def self_contained(python):
    """True for the bundled runtime: a ._pth file beside python.exe fixes its paths."""
    return any(Path(python).parent.glob("*._pth"))


def python_flags(python):
    """Flags that keep the bundled runtime from importing the user's own Python packages.

    A ._pth file isolates the interpreter yet leaves the user site on, found through
    APPDATA or PYTHONUSERBASE, so its folder and .pth files would load; -s turns it off.
    """
    return ["-s"] if self_contained(python) else []


def python_environment(python):
    """Let a system Python helper import the per-user packages Core imports.

    Windows derives the user site from APPDATA, which helpers do not receive, so
    packages installed with ``pip --user`` (NumPy for semantic search) would be
    missing in plugins. The bundled runtime must stay self-contained, so it gets nothing.
    """
    if not site.ENABLE_USER_SITE or self_contained(python):
        return {}
    base = site.getuserbase()
    return {"PYTHONUSERBASE": base} if base else {}


def base_environment(**extra):
    environment = {key: os.environ[key] for key in BASE_ENVIRONMENT if key in os.environ}
    environment.update({key: str(value) for key, value in extra.items()})
    return environment
