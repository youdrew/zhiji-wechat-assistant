"""Plugin-owned storage helpers.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Each plugin keeps its own data under the directory Core assigns
(``.local/plugin-data/<plugin>``). Per-account data lives in
``accounts/<sha256(account)>`` so Core can delete it when the user clears an
account. Schema and upgrades belong to the plugin.
"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path


def account_key(account):
    return hashlib.sha256(account.encode("utf-8")).hexdigest()


def account_dir(data_dir, account):
    path = Path(data_dir) / "accounts" / account_key(account)
    path.mkdir(parents=True, exist_ok=True)
    return path


def connect(path):
    """A WAL SQLite connection for one plugin database file."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), check_same_thread=False, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=15000")
    return connection
