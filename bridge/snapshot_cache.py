"""Reuse account-specific SQLite snapshots; refresh changed sources in the background.

Refreshing into a separate work directory preserves the last usable database while a new
snapshot is built. When WeChat only appended to a database's WAL, the refresh decrypts just
the newly committed frames into the previous snapshot; the installed reader rebuilds the whole
snapshot only on first use, after a checkpoint rewrote the main file, or when an incremental
merge cannot be verified.
"""
from __future__ import annotations

import copy
import logging
import os
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path

from wechatauto import db as upstream

_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="wechat-snapshot")
_states_lock = threading.Lock()
_states = {}
_log = logging.getLogger(__name__)
CONTACT_REFRESH_WAIT = 1.5


class ContactSnapshotStaleError(RuntimeError):
    """The current contact source is newer than every safe readable snapshot."""


class _State:
    def __init__(self):
        self.lock = threading.RLock()
        self.future = None
        self.retry_at = 0.0
        self.last_error = None


def _state(reader, rel):
    key = (os.path.normcase(os.path.realpath(reader.account_dir)),
           os.path.normcase(os.path.realpath(reader.workdir)), rel)
    with _states_lock:
        return _states.setdefault(key, _State())


def _destination(reader, rel):
    root = Path(reader.workdir).resolve()
    path = (root / rel.replace(os.sep, "__")).resolve()
    if path.parent != root:
        raise RuntimeError("invalid snapshot path")
    return path


def _source_signature(reader, rel):
    source = os.stat(reader._db_path(rel))
    wal_path = reader._wal_path(rel)
    try:
        wal = os.stat(wal_path) if wal_path else None
    except FileNotFoundError:
        # A checkpoint may remove the WAL between discovery and stat.
        wal = None
    return (source.st_mtime, source.st_size,
            wal.st_mtime if wal else 0.0, wal.st_size if wal else 0)


def _stamp_details(destination):
    try:
        parts = Path(str(destination) + ".stamp").read_text(encoding="ascii").split(",")
        if len(parts) != 6 or int(parts[0]) != upstream.STAMP_VERSION:
            return None
        return ((float(parts[1]), int(parts[2]), float(parts[3]), int(parts[4])), int(parts[5]))
    except (OSError, ValueError):
        return None


def _saved_signature(destination):
    details = _stamp_details(destination)
    return details[0] if details else None


def _merged_contact_signature(destination):
    details = _stamp_details(destination)
    return details[0] if details and details[1] >= 0 else None


def _write_ascii(path, text):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(text, encoding="ascii")
    os.replace(temporary, path)


def _write_stamp(built, signature, applied):
    _write_ascii(str(built) + ".stamp", "%d,%r,%d,%r,%d,%d" % (
        upstream.STAMP_VERSION, signature[0], signature[1], signature[2], signature[3], applied))


def _wal_state(built):
    """The WAL generation (salt) and frame cursor the snapshot already contains."""
    try:
        salt, cursor = Path(str(built) + ".walstate").read_text(encoding="ascii").split(",")
        return bytes.fromhex(salt), int(cursor)
    except (OSError, ValueError):
        return None


def _drop_wal_state(built):
    Path(str(built) + ".walstate").unlink(missing_ok=True)


def _committed_frames(wal_path, salt, start, *, end=None, pages=True):
    """Frames from ``start`` through the last commit of the current WAL generation.

    Returns (generation salt, cursor after that commit, [(pgno, page)]), or None when the
    WAL was restarted since ``salt``. Frames after the last commit are left for a later pass:
    an open transaction may still be rewritten or rolled back.
    """
    header_size, frame_size = upstream.WeChatDB.WAL_HEADER_SZ, upstream.WeChatDB.WAL_FRAME_SZ
    with open(wal_path, "rb") as wal:
        header = wal.read(header_size)
        if len(header) < header_size:
            return (salt, start, []) if salt or start == 0 else None
        current = header[16:24]
        if salt and current != salt:
            return None
        size = os.fstat(wal.fileno()).st_size if end is None else min(end, os.fstat(wal.fileno()).st_size)
        cursor, committed, pending = start, [], []
        wal.seek(header_size + start * frame_size)
        for index in range(start, max(start, (size - header_size) // frame_size)):
            frame = wal.read(frame_size)
            if len(frame) < frame_size or frame[8:16] != current:
                break  # The end of this generation; later bytes are an older one.
            pending.append((int.from_bytes(frame[:4], "big"), frame[24:] if pages else None))
            if int.from_bytes(frame[4:8], "big"):  # A commit frame records the database size.
                committed.extend(pending)
                pending, cursor = [], index + 1
        return current, cursor, committed


def _merge_new_wal_frames(reader, rel, built):
    """Apply only WAL frames committed since the previous snapshot. False means rebuild."""
    details, state = _stamp_details(built), _wal_state(built)
    key = reader._keys.get(rel)
    if (details is None or details[1] < 0 or state is None or not built.is_file()
            or not isinstance(key, bytes) or len(key) != 32):
        return False
    signature = _source_signature(reader, rel)
    if signature == details[0]:
        return True
    wal_path = reader._wal_path(rel)
    if signature[:2] != details[0][:2] or not wal_path:
        return False  # A checkpoint rewrote the main file in place.
    try:
        found = _committed_frames(wal_path, state[0], state[1])
        if found is None:
            return False
        salt, cursor, frames = found
        if frames:
            with open(built, "r+b") as output:
                highest = 0
                for pgno, page in frames:
                    plain = upstream._decrypt_page(key, page, pgno)
                    if pgno < 1 or pgno == 1 and int.from_bytes(plain[16:18], "big") != upstream.PAGE_SZ:
                        raise ValueError("unexpected WAL page")
                    output.seek((pgno - 1) * upstream.PAGE_SZ)
                    output.write(plain)
                    highest = max(highest, pgno)
                output.seek(0)
                first = output.read(upstream.PAGE_SZ)
                pages = max(int.from_bytes(first[28:32], "big"), highest,
                            (os.fstat(output.fileno()).st_size + upstream.PAGE_SZ - 1) // upstream.PAGE_SZ)
                output.seek(28)
                output.write(pages.to_bytes(4, "big"))
            if not upstream.WeChatDB._check_merged(str(built)):
                raise ValueError("merged snapshot failed its integrity check")
        _write_stamp(built, signature, cursor)
        _write_ascii(str(built) + ".walstate", "%s,%d" % (salt.hex(), cursor))
        return True
    except (OSError, ValueError, sqlite3.Error):
        # A partial merge must never look current; the next build starts from the source.
        Path(str(built) + ".stamp").unlink(missing_ok=True)
        _drop_wal_state(built)
        return False


def _record_wal_state(reader, rel, built):
    """After a full build, remember the WAL position it covers for later incremental merges."""
    _drop_wal_state(built)
    details = _stamp_details(built)
    key = reader._keys.get(rel)
    if details is None or details[1] < 0 or not isinstance(key, bytes) or len(key) != 32:
        return
    saved = details[0]
    if _source_signature(reader, rel)[:2] != saved[:2]:
        return
    if saved[3] == 0:
        # The build saw no WAL, so every frame a new WAL receives is newer than the snapshot.
        _write_ascii(str(built) + ".walstate", ",0")
        return
    wal_path = reader._wal_path(rel)
    if not wal_path:
        return
    found = _committed_frames(wal_path, b"", 0, end=saved[3], pages=False)
    # The installed reader merged at least every frame inside the size it stamped.
    if found is not None and details[1] >= found[1]:
        _write_ascii(str(built) + ".walstate", "%s,%d" % (found[0].hex(), found[1]))


def _connect(destination):
    connection = sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA schema_version").fetchone()
        connection.row_factory = sqlite3.Row
        connection.text_factory = upstream._sqlite_text_factory
        return connection
    except Exception:
        connection.close()
        raise


def _refresh(reader, rel, destination, state):
    try:
        staged = copy.copy(reader)
        staged._keys = dict(reader._keys)
        staged.workdir = str(Path(reader.workdir) / ".snapshot-refresh")
        Path(staged.workdir).mkdir(parents=True, exist_ok=True)
        built = _destination(staged, rel)
        if Path(rel).name.lower() == "contact.db":
            details = _stamp_details(built)
            if details is not None and details[1] < 0:
                Path(str(built) + ".stamp").unlink(missing_ok=True)
        if not _merge_new_wal_frames(staged, rel, built):
            connection = upstream.WeChatDB._open(staged, rel)
            connection.close()
            _record_wal_state(staged, rel, built)
        if _saved_signature(built) is None:
            raise RuntimeError("refreshed snapshot is unverified")
        if Path(rel).name.lower() == "contact.db" and _merged_contact_signature(built) is None:
            Path(str(built) + ".stamp").unlink(missing_ok=True)
            raise RuntimeError("contact snapshot WAL is not merged")
        # Existing readers may briefly hold the Windows file open. In that case keep the
        # current snapshot and retry on a later read; never interrupt its reader.
        pending = Path(str(destination) + ".refresh-ready")
        pending_stamp = Path(str(destination) + ".stamp.refresh-ready")
        shutil.copyfile(built, pending)
        shutil.copyfile(str(built) + ".stamp", pending_stamp)
        with state.lock:
            os.replace(pending, destination)
            os.replace(pending_stamp, str(destination) + ".stamp")
            state.retry_at = 0.0
            state.last_error = None
    except Exception as exc:
        # Do not log paths, keys or message contents. The valid old snapshot remains usable.
        with state.lock:
            state.retry_at = time.monotonic() + (10 if Path(rel).name.lower() == "contact.db" else 2)
            kind = type(exc).__name__
            if state.last_error != kind:
                _log.warning("snapshot refresh deferred (%s); retaining previous data", kind)
            state.last_error = kind


def try_forget_account(workdir):
    """Release only idle refresh state before deleting an inactive account's caches.

    The caller must hold the source lock and recheck the selected live account.
    This function never removes files or cancels an in-progress writer.
    """
    selected = os.path.normcase(os.path.realpath(workdir))
    with _states_lock:
        matches = [(key, state) for key, state in _states.items() if key[1] == selected]
        if any(state.future is not None and not state.future.done() for _, state in matches):
            return False
        for key, _ in matches:
            del _states[key]
    return True


def wait_forget_account(workdir, timeout=180):
    """Wait for this account's already-running snapshot writer before deleting its files."""
    selected = os.path.normcase(os.path.realpath(workdir))
    deadline = time.monotonic() + timeout
    with _states_lock:
        futures = [state.future for key, state in _states.items()
                   if key[1] == selected and state.future is not None and not state.future.done()]
    for future in futures:
        try:
            future.result(timeout=max(0, deadline - time.monotonic()))
        except FutureTimeoutError:
            return False
        except Exception:
            # A failed refresh is already finished; its partial staged files are still scoped.
            pass
    return try_forget_account(workdir)


class SnapshotCacheMixin:
    def _open(self, rel):
        state = _state(self, rel)
        destination = _destination(self, rel)
        contact = Path(rel).name.lower() == "contact.db"
        future = None
        with state.lock:
            signature = _source_signature(self, rel)
            saved = _saved_signature(destination)
            if destination.is_file() and saved is not None:
                if contact and _merged_contact_signature(destination) != signature:
                    if (time.monotonic() >= state.retry_at and
                            (state.future is None or state.future.done())):
                        state.future = _workers.submit(_refresh, self, rel, destination, state)
                    future = state.future
                else:
                    try:
                        connection = _connect(destination)
                    except sqlite3.DatabaseError:
                        # Force the existing reader to rebuild a corrupt cached SQLite file.
                        Path(str(destination) + ".stamp").unlink(missing_ok=True)
                        connection = upstream.WeChatDB._open(self, rel)
                        if contact and _merged_contact_signature(destination) != _source_signature(self, rel):
                            connection.close()
                            raise ContactSnapshotStaleError("联系人资料正在更新，请稍后重试")
                        return connection
                    if contact and _merged_contact_signature(destination) != _source_signature(self, rel):
                        connection.close()
                        raise ContactSnapshotStaleError("联系人资料正在更新，请稍后重试")
                    if (signature != saved and time.monotonic() >= state.retry_at
                            and (state.future is None or state.future.done())):
                        state.future = _workers.submit(_refresh, self, rel, destination, state)
                    return connection
            else:
                # Only a database without a usable snapshot blocks for its first preparation.
                connection = upstream.WeChatDB._open(self, rel)
                if contact and _merged_contact_signature(destination) != _source_signature(self, rel):
                    connection.close()
                    raise ContactSnapshotStaleError("联系人资料正在更新，请稍后重试")
                return connection
        if future is not None:
            try:
                future.result(timeout=CONTACT_REFRESH_WAIT)
            except FutureTimeoutError:
                pass
        with state.lock:
            if _merged_contact_signature(destination) == _source_signature(self, rel):
                try:
                    return _connect(destination)
                except sqlite3.DatabaseError:
                    pass
        raise ContactSnapshotStaleError("联系人资料正在更新，请稍后重试")
