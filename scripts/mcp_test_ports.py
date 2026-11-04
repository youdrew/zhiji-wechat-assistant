"""Test helper: a temporary installation root whose derived Core port can be bound.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The launcher derives an installation's loopback port from its path
(``default_port(root)``). Windows reserves some port ranges (Hyper-V/WSL exclusions,
``netsh int ipv4 show excludedportrange``), and binding one fails with WinError 10013,
so a test that serves a fake Core on that port first probes the port and retries with
a fresh temporary root. Not shipped; used by the scripts/test-*.py suites only.
"""
from __future__ import annotations

import socket
import tempfile
from pathlib import Path

ATTEMPTS = 40


def port_bindable(port):
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def bindable_install_dir(default_port, **tempdir_options):
    """A ``tempfile.TemporaryDirectory`` whose ``default_port(Path(name))`` binds right now."""
    for _ in range(ATTEMPTS):
        directory = tempfile.TemporaryDirectory(**tempdir_options)
        if port_bindable(default_port(Path(directory.name))):
            return directory
        directory.cleanup()
    raise RuntimeError(f"{ATTEMPTS} 个临时安装目录的派生端口都无法绑定")
