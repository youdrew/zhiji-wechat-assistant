"""Volatile media-key initialization for the currently owned WeChat account.

Adapted from julibeian/wechat-local-exporter media.py's bounded cfg-pointer approach,
https://github.com/julibeian/wechat-local-exporter (local source version 1.6.0).
Emoticon derivation and full-UIN layout verified against upstream commit
50b79147a16f3d3510c07bc54da1bba5387152a9 (1.6.1).
Copyright (c) 2026 julibeian, MIT; see licenses/wechat-exporter-MIT.txt.
Know Your Friends modifications (2026-10-10) are Apache-2.0: require current
file ownership, match the account, validate the requested V2 file, and never
log a key. No database master-key extraction or general RAM scan.
Modified 2026-10-24: a validated key is saved in the account's stable key directory
and tried before WeChat's memory is read again; it is still checked against every file.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import struct
import threading
import time
from pathlib import Path

V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
LANDMARK = b"global_config"
MAX_MODULE_BYTES = 512 * 1024 * 1024
RETRY_SECONDS = 30


def derive_image_keys(value, account):
    return hashlib.md5(f"{value}{account}".encode("utf-8")).hexdigest()[:16], value & 255


def derive_emoticon_key(value, account):
    return hashlib.md5(f"{value}{account}EMOTICON".encode("utf-8")).digest()


def _image_header(head):
    return (head.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM", b"wxgf")) or
            head.startswith(b"RIFF") and head[8:12] == b"WEBP")


def validates(probe, key):
    if len(probe) != 16 or not isinstance(key, str) or len(key) != 16:
        return False
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        decryptor = Cipher(algorithms.AES(key.encode("ascii")), modes.ECB()).decryptor()
        head = decryptor.update(probe) + decryptor.finalize()
        return _image_header(head)
    except (ValueError, UnicodeError):
        return False


def validates_emoticon(probe, key):
    if len(probe) != 16 or not isinstance(key, bytes) or len(key) != 16:
        return False
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    return _image_header(decryptor.update(probe) + decryptor.finalize())


def _remote_account(read, address):
    length = read(address + 16, 8)
    if len(length) != 8:
        return ""
    count = struct.unpack("<Q", length)[0]
    if not 1 <= count <= 256:
        return ""
    if count > 15:
        pointer = read(address, 8)
        if len(pointer) != 8:
            return ""
        address = struct.unpack("<Q", pointer)[0]
    raw = read(address, count)
    return raw.decode("utf-8", errors="replace") if len(raw) == count else ""


def _cfg_candidates(image, base, read, *, full_uin=False):
    """Read only the bounded cfg chain behind Weixin.dll's SSO landmark."""
    cursor, seen = len(image), set()
    for _ in range(128):
        cursor = image.rfind(LANDMARK, 0, cursor)
        if cursor < 0:
            return
        if cursor + 28 > len(image):
            continue
        length, capacity = struct.unpack_from("<I", image, cursor + 16)[0], struct.unpack_from("<I", image, cursor + 24)[0]
        if length != len(LANDMARK) or capacity != 15:
            continue
        for offset in (0x138, 0x130):
            pointer = read(base + cursor + 16 - offset, 8)
            if len(pointer) != 8:
                continue
            owner = struct.unpack("<Q", pointer)[0]
            pointer = read(owner + 0x68, 8)
            if len(pointer) != 8:
                continue
            cfg = struct.unpack("<Q", pointer)[0]
            if not 0x10000 <= cfg < 0x800000000000:
                continue
            count = 8 if full_uin else 4
            raw = read(cfg + 0x40, count)
            if len(raw) != count:
                continue
            value = struct.unpack("<Q" if full_uin else "<I", raw)[0]
            account = _remote_account(read, cfg + 0x48)
            if value and account and (value, account) not in seen:
                seen.add((value, account))
                yield value, account


def _read_cfg(pid, created, *, full_uin=False):
    """Only a known current WeChat owner may expose a module and cfg chain."""
    if os.name != "nt" or not created:
        return []
    from live_source import discovery
    from wechatauto.db import _find_weixin_module
    process = discovery.psutil.Process(pid)
    if process.name().casefold() not in ("weixin.exe", "wechat.exe") or abs(process.create_time() - created) >= .001:
        return []
    module = _find_weixin_module(pid)
    if not module or not 0 < module[1] < MAX_MODULE_BYTES:
        return []
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x0010 | 0x0400, False, pid)
    if not handle:
        return []
    try:
        def read(address, count):
            if not 0x10000 <= address < 0x800000000000 or not 0 < count < MAX_MODULE_BYTES:
                return b""
            buffer = ctypes.create_string_buffer(count)
            received = ctypes.c_size_t()
            ok = kernel.ReadProcessMemory(handle, ctypes.c_void_p(address), buffer, count, ctypes.byref(received))
            return buffer.raw if ok and received.value == count else b""
        image = read(module[0], module[1])
        if len(image) != module[1]:
            return []
        return list(_cfg_candidates(image, module[0], read, full_uin=full_uin))
    finally:
        kernel.CloseHandle(handle)


def _account(db):
    return Path(db.account_dir).name


def _saved_media(db):
    from cache_source import MEDIA_KEY_SUFFIX, read_key_file, stable_key_path
    saved = read_key_file(stable_key_path(_account(db), MEDIA_KEY_SUFFIX))
    return saved if saved.get("wxid") == db.wxid else {"wxid": db.wxid}


def _save_media(db, name, value):
    from cache_source import MEDIA_KEY_SUFFIX, stable_key_path, write_key_file
    saved = _saved_media(db)
    if saved.get(name) != value:
        write_key_file(stable_key_path(_account(db), MEDIA_KEY_SUFFIX), {**saved, name: value})


def _selection():
    from live_source import active_account_snapshot
    return active_account_snapshot(fresh=False)


class ImageKeyResolver:
    """Account-scoped keys; cache misses briefly, verify every used key."""
    SAVED_NAME = "image"

    def __init__(self, db):
        self.db = db
        self.lock = threading.Lock()
        self.identity = None
        self.keys = None
        self.retry_after = 0
        self.recent = ()

    @staticmethod
    def _probe(path):
        with Path(path).open("rb") as stream:
            header = stream.read(31)
        return header[15:31] if header.startswith(V2_MAGIC) and len(header) == 31 else None

    @staticmethod
    def _candidates(pid, created):
        return ((account, derive_image_keys(value, account)) for value, account in _read_cfg(pid, created))

    @staticmethod
    def _validates(probe, keys):
        return validates(probe, keys[0])

    @staticmethod
    def _dump(keys):
        return [keys[0], keys[1]]

    @staticmethod
    def _load(value):
        if (isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and len(value[0]) == 16
                and type(value[1]) is int and 0 <= value[1] <= 255):
            return value[0], value[1]
        return None

    def resolve(self, path):
        with self.lock:
            try:
                root = Path(self.db.account_dir).resolve()
                if not Path(path).resolve().is_relative_to(root):
                    return None
                probe = self._probe(path)
                if probe is None:
                    return None
                selected = _selection()
                if selected is None or selected.account_dir.resolve() != root:
                    self.keys = None
                    return None
                identity = (root, selected.processes, self.db.wxid)
                if identity != self.identity:
                    self.identity, self.keys, self.retry_after, self.recent = identity, None, 0, ()
                if self.keys and self._validates(probe, self.keys):
                    return self.keys
                self.keys = None
                saved = self._load(_saved_media(self.db).get(self.SAVED_NAME))
                if saved is not None and self._validates(probe, saved) and _selection() == selected:
                    self.keys = saved
                    return saved
                # Modified 2026-10-14: read process memory at most once per window; other
                # files (a bad newer cache, another sticker) reuse that read's candidates.
                if time.monotonic() >= self.retry_after:
                    self.retry_after, self.recent = time.monotonic() + RETRY_SECONDS, ()
                    self.recent = tuple(keys for pid, created in selected.processes
                                        for account, keys in self._candidates(pid, created)
                                        if account == self.db.wxid)
                for keys in self.recent:
                    if self._validates(probe, keys) and _selection() == selected:
                        self.keys = keys
                        _save_media(self.db, self.SAVED_NAME, self._dump(keys))
                        return keys
                return None
            except Exception:
                # No raw exception can expose paths, IDs, process data or a key.
                self.keys = None
                return None


class EmoticonKeyResolver(ImageKeyResolver):
    """Local CBC caches use a distinct full-UIN key."""
    SAVED_NAME = "emoticon"

    @staticmethod
    def _probe(path):
        with Path(path).open("rb") as stream:
            probe = stream.read(16)
        return probe if len(probe) == 16 else None

    @staticmethod
    def _candidates(pid, created):
        return ((account, derive_emoticon_key(value, account))
                for value, account in _read_cfg(pid, created, full_uin=True))

    @staticmethod
    def _validates(probe, key):
        return validates_emoticon(probe, key)

    @staticmethod
    def _dump(key):
        return key.hex()

    @staticmethod
    def _load(value):
        try:
            key = bytes.fromhex(value) if isinstance(value, str) else None
        except ValueError:
            return None
        return key if key is not None and len(key) == 16 else None
