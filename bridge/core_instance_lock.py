"""One Core per installation: an exclusive lock on the installation's runtime directory.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The launcher and the MCP adapter already serialize their starts and attach to a
running Core, but they pick ports first and their launch mutex is per port. This
lock is the Core's own backstop: every store, model gateway and background run
lives under the one installation, so a second Core over the same data refuses to
start instead of serving it in parallel on another port. The operating system
drops the lock when the holding process exits, however it exits; the handle is
not inherited by plugin or gateway children.
"""
from __future__ import annotations

import os
from pathlib import Path

LOCK_NAME = "core.lock"
ALREADY_RUNNING_EXIT = 3


def acquire(runtime_dir):
    """Hold the installation's Core lock for this process; None when another Core holds it."""
    runtime_dir = Path(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    handle = (runtime_dir / LOCK_NAME).open("a+b")
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle
