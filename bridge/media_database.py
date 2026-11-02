"""Lazy read-only credentials for media databases excluded from chat startup.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
Validated keys remain in memory; this module never persists or prints them.
"""
from __future__ import annotations

import os
import re
import threading
import time
from pathlib import Path

from media_assets import MediaUnavailable, check_owned_path

_lock = threading.Lock()


def _media_pages(reader, kind):
    from live_source import PAGE_SIZE, MAX_DB_FILES
    root = (Path(reader.account_dir) / "db_storage").resolve()
    pages = {}
    for rel, raw_path, _size in reader._db_files:
        if kind == "voice" and not re.fullmatch(r"media_\d+\.db", Path(raw_path).name):
            continue
        if kind == "emoticon" and str(rel).replace("\\", "/") != "emoticon/emoticon.db":
            continue
        path = Path(raw_path)
        check_owned_path(path)
        if not path.resolve().is_relative_to(root):
            raise MediaUnavailable(kind + "-database-unavailable")
        with path.open("rb") as stream:
            page = stream.read(PAGE_SIZE)
        if len(page) != PAGE_SIZE or page.startswith(b"SQLite format 3\x00"):
            continue
        pages[rel] = (path, page[:16], page)
        if len(pages) > MAX_DB_FILES:
            raise MediaUnavailable(kind + "-database-unavailable")
    return pages


def _prepare_media_databases(reader, kind):
    """On first access only, supplement the selected reader's optional keys.

    Startup readiness remains anchors + messages. Media cannot delay ordinary chat.
    The bounded native-reader scanner receives only the requested database pages.
    """
    from live_source import active_account_snapshot, _valid_page_key, _scan_config_cipher_keys
    with _lock:
        pages = _media_pages(reader, kind)
        if not pages:
            return
        keys = {rel: reader._keys[rel] for rel in pages if rel in reader._keys and
                _valid_page_key(reader._keys[rel], pages[rel][2])}
        if len(keys) == len(pages):
            return
        # Existing cached keys and currently verified keys may already cover media.
        for key in tuple(reader._keys.values()):
            for rel in pages.keys() - keys.keys():
                if _valid_page_key(key, pages[rel][2]):
                    keys[rel] = key
        cache_paths = [reader._stable_key_file(), reader.keys_file, reader.keys_file + ".bak"]
        for path in cache_paths:
            if not path or not os.path.isfile(path):
                continue
            try:
                candidates = reader._load_key_cache(path)
            except (OSError, ValueError, TypeError):
                continue
            for rel in pages.keys() - keys.keys():
                raw = candidates.get(rel)
                if _valid_page_key(raw, pages[rel][2]):
                    keys[rel] = raw
        if len(keys) != len(pages):
            retry_attribute = "_" + kind + "_key_retry_at"
            if time.monotonic() - getattr(reader, retry_attribute, 0) < 30:
                reader._keys.update(keys)
                return
            setattr(reader, retry_attribute, time.monotonic())
            selection = active_account_snapshot()
            if selection is None or selection.account_dir.resolve() != Path(reader.account_dir).resolve():
                raise MediaUnavailable(kind + "-account-changed")
            for pid, created in selection.processes:
                if created is None or active_account_snapshot() != selection:
                    raise MediaUnavailable(kind + "-account-changed")
                try:
                    found = _scan_config_cipher_keys(pid, {rel: pages[rel] for rel in pages.keys() - keys.keys()}, {})
                except Exception:
                    found = {}
                for rel, raw in found.items():
                    if rel in pages and _valid_page_key(raw, pages[rel][2]):
                        keys[rel] = raw
                if len(keys) == len(pages):
                    break
            if active_account_snapshot() != selection:
                raise MediaUnavailable(kind + "-account-changed")
        # Revalidate against current bytes after scanning before installing any key.
        current = _media_pages(reader, kind)
        reader._keys.update({rel: raw for rel, raw in keys.items()
            if rel in current and current[rel][1] == pages[rel][1] and _valid_page_key(raw, current[rel][2])})


def prepare_voice_databases(reader):
    _prepare_media_databases(reader, "voice")


def prepare_emoticon_databases(reader):
    _prepare_media_databases(reader, "emoticon")
