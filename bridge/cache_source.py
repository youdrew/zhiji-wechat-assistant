"""Cached-key initialization and reusable snapshots with upstream database/WAL decoding."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from wechatauto.db import WeChatDB
from snapshot_cache import SnapshotCacheMixin


ANCHOR_DATABASES = frozenset(("session/session.db", "contact/contact.db"))
MESSAGE_DATABASE = re.compile(r"message/message_\d+\.db\Z")
MEDIA_KEY_SUFFIX = ".media.json"


def stable_key_path(account, suffix=".json"):
    """The upstream reader's stable key cache for one account (same rule as ``WeChatDB``).

    Validated keys persist here so a restart reuses them instead of reading WeChat's
    memory again; every use still validates each key against the current file.
    """
    if not account or os.path.basename(account) != account or account in (".", ".."):
        return None
    root = os.environ.get("WECHATAUTO_KEYS_DIR")
    if not root:
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("USERPROFILE")
        root = os.path.join(base, "wechatauto_keys") if base else None
    return Path(os.path.abspath(root)) / (account + suffix) if root else None


def _plain_file(path):
    return not path.is_symlink() and not getattr(path.lstat(), "st_file_attributes", 0) & 0x400


def read_key_file(path):
    """A JSON key file's object, or {} when it is missing, unsafe or malformed."""
    try:
        if path is None or not os.path.lexists(path) or not _plain_file(path):
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_key_file(path, data):
    """Atomically replace one key file; a failed write keeps the previous file."""
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not _plain_file(path.parent) or (os.path.lexists(path) and not _plain_file(path)):
            return False
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
        return True
    except OSError:
        return False


def persist_database_keys(account, keys):
    """Merge validated per-database keys into the account's stable cache."""
    path = stable_key_path(account)
    saved = read_key_file(path)
    merged = dict(saved)
    merged.update({rel: raw.hex() for rel, raw in keys.items()
                   if isinstance(raw, bytes) and len(raw) in (32, 48)})
    return merged == saved or write_key_file(path, merged)


def database_groups(rels):
    """Find role candidates inside the already selected account, plus message shards."""
    rels = tuple(rels)
    anchors = {rel for rel in rels if os.path.basename(rel.replace("\\", "/")).casefold()
               in ("session.db", "contact.db")}
    # Match the installed reader's case-sensitive message inventory exactly.
    messages = {rel for rel in rels if MESSAGE_DATABASE.fullmatch(rel.replace("\\", "/"))}
    return anchors, messages


def resolve_anchor_rels(rels, validated):
    """Prefer a verified canonical role; otherwise accept one verified fallback."""
    candidates, _messages = database_groups(rels)
    selected = {}
    for role in ("session", "contact"):
        valid = [rel for rel in candidates if os.path.basename(rel.replace("\\", "/")).casefold()
                 == role + ".db" and rel in validated]
        canonical = next((rel for rel in valid if rel.replace("\\", "/").casefold()
                          == role + "/" + role + ".db"), None)
        if canonical is not None:
            selected[role] = canonical
        elif len(valid) == 1:
            selected[role] = valid[0]
        else:
            return None
    return selected


class IncompleteKeyCache(RuntimeError):
    """A selected account's individually validated cache keys, without a usable reader."""

    def __init__(self, keys):
        super().__init__("local WeChat key cache unavailable for selected account")
        self.validated_keys = dict(keys)


class CacheOnlyWeChatDB(SnapshotCacheMixin, WeChatDB):
    def get_self_info(self):
        for rel, path, _ in self._db_files:
            if rel != self.anchor_rels["contact"]:
                continue
            conn = self._open(rel)
            try:
                row = conn.execute(
                    "SELECT username, nick_name, remark FROM contact WHERE username=? LIMIT 1",
                    (self.wxid,),
                ).fetchone()
            finally:
                conn.close()
            if row:
                return {"username": row[0], "nick_name": row[1], "remark": row[2]}
        return {"username": self.wxid, "nick_name": "", "remark": ""}

    def _load_or_extract_keys(self, master_key=None):
        if master_key is not None:
            raise RuntimeError("cache-only mode does not accept a master key")
        self.master_key = None
        self.cfg_dword = None
        self._keys = {}
        stable = self._stable_key_file()
        cache_paths = [stable, self.keys_file, self.keys_file + ".bak"]
        files = {rel for rel, _, _ in self._db_files}
        _anchors, messages = database_groups(files)
        for path in cache_paths:
            if not path or not os.path.isfile(path):
                continue
            try:
                candidates = self._load_key_cache(path)
            except (AttributeError, TypeError, ValueError):
                continue
            for rel in files - self._keys.keys():
                key = candidates.get(rel)
                if not isinstance(key, bytes) or len(key) not in (32, 48):
                    continue
                self._keys[rel] = key
                try:
                    valid = self._key_works(rel)
                except Exception:
                    valid = False
                if not valid:
                    del self._keys[rel]
        selected = resolve_anchor_rels(files, self._keys)
        if selected is None or not messages or (set(selected.values()) | messages) - self._keys.keys():
            validated = dict(self._keys)
            self._keys = {}
            raise IncompleteKeyCache(validated)
        self.anchor_rels = selected
        self.messages_ready = True

    def _auto_diagnose_key_failure(self, _rel):
        raise RuntimeError("local WeChat key cache unavailable for selected account")

    def extract_keys(self):
        raise RuntimeError("process key extraction disabled in cache-only mode")

    def extract_master_key(self):
        raise RuntimeError("process key extraction disabled in cache-only mode")

    def _all_key_candidates(self):
        raise RuntimeError("process key extraction disabled in cache-only mode")
