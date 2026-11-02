"""Shared helpers for the reply-evaluation scripts (docs/loop/reply-eval protocol).

Every SQLite connection here is opened read-only (``mode=ro``); nothing in this
package writes to any business database, and no real chat text may appear in
anything committed to git — sample data lives only under ``.local/reply-eval/``.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import hashlib
import json
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ACCOUNTS_JSON = REPO_ROOT / ".local" / "real-client-data" / "accounts.json"
PORT_JSON = REPO_ROOT / ".local" / "real-client-runtime" / "selected-port.json"
DATA_DIR = REPO_ROOT / ".local" / "reply-eval"
SELF_DB_NAME = "self-analysis.sqlite3"
CORE_DB_NAME = "advisor.sqlite3"

# Trial bounds from plugins/kyf.self-analysis/backend/self_trial.py; the generated
# context must satisfy the same limits as an interactive trial.
TURN_CHARS = 1000
MAX_TURNS = 20


def load_account(account=None):
    """The Core account name (not the MCP accountId) and its workdir from accounts.json."""
    entries = json.loads(ACCOUNTS_JSON.read_text(encoding="utf-8")).get("accounts") or []
    if account:
        for entry in entries:
            if entry.get("account") == account:
                return entry
        raise SystemExit(f"accounts.json 中没有账号 {account}")
    if len(entries) != 1:
        raise SystemExit("accounts.json 应恰好有一个账号，或用 --account 指定")
    return entries[0]


def _account_digest(entry):
    return hashlib.sha256(entry["account"].encode("utf-8")).hexdigest()


def discover_self_db(entry):
    """self-analysis.sqlite3: the plugin data dir keyed by sha256(account) first, then
    the account workdir, then a bounded search under .local (paths containing the digest)."""
    digest = _account_digest(entry)
    candidates = [REPO_ROOT / ".local" / "plugin-data" / "kyf.self-analysis" / "accounts" / digest / SELF_DB_NAME,
                  Path(entry.get("workdir", ".")) / SELF_DB_NAME]
    for path in candidates:
        if path.is_file():
            return path
    found = sorted(path for path in (REPO_ROOT / ".local").rglob(SELF_DB_NAME) if digest in str(path))
    if len(found) == 1:
        return found[0]
    raise SystemExit(f"找不到 {entry['account']} 的 {SELF_DB_NAME}，请用 --db 指定（候选：{found}）")


def discover_core_db(entry):
    """Core 的消息库：advisor.sqlite3（context_messages 表），同样按账号摘要定位。"""
    digest = _account_digest(entry)
    candidates = [REPO_ROOT / ".local" / "advisor-data" / digest / CORE_DB_NAME]
    for path in candidates:
        if path.is_file():
            return path
    found = sorted(path for path in (REPO_ROOT / ".local").rglob(CORE_DB_NAME) if digest in str(path))
    if len(found) == 1:
        return found[0]
    raise SystemExit(f"找不到 {entry['account']} 的 {CORE_DB_NAME}，请用 --core-db 指定（候选：{found}）")


def open_ro(path):
    """Read-only connection; live WAL databases stay safe to read while analysis runs."""
    return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)


def parse_context(text):
    """Turn a sample input into trial messages: 本人：/对方： lines open a turn; a line
    without a prefix continues the previous message (multi-line message text); a cut-off
    first line (inputs are tail-truncated at 2400 chars) is dropped. Returns up to
    MAX_TURNS turns within TURN_CHARS each, the last of which is the friend's."""
    turns = []
    for line in text.splitlines():
        if line.startswith("对方："):
            turns.append({"from": "friend", "text": line[3:]})
        elif line.startswith("本人："):
            turns.append({"from": "me", "text": line[3:]})
        elif turns:
            turns[-1]["text"] += "\n" + line
        # a prefixless first line is a truncation remnant and carries no prefix
    turns = [turn for turn in turns if turn["text"].strip()]
    if not turns or turns[-1]["from"] != "friend":
        return None
    return [{"from": turn["from"], "text": turn["text"].strip()[:TURN_CHARS]} for turn in turns[-MAX_TURNS:]]
