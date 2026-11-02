"""Analysis-only support for one opaque, single-picture local WXGF profile.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
The container layout was cross-checked against Sarv's format investigation:
https://sarv.blog/posts/wxam/ . Unknown headers, alpha and animations are rejected;
no partition scanning, timing guesses, proprietary decoder or third-party code.
"""
from __future__ import annotations

import hashlib
import io
import itertools
import os
import re
import subprocess
import threading
from pathlib import Path

from media_assets import MAX_IMAGE, MediaUnavailable, analysis_image, check_owned_path

_STATIC_ARGS = bytes.fromhex("00000000400f22")
_MAX_PIXELS = 2048 * 2048
_TIMEOUT = 12


def _picture(data):
    """Accept a complete known profile, never locate payloads by scanning magic."""
    if (not 22 < len(data) <= MAX_IMAGE or data[:7] != b"wxgf\x12\x00\x00" or
            data[11:18] != _STATIC_ARGS):
        raise MediaUnavailable("wxgf-profile-unsupported")
    width, height = int.from_bytes(data[7:9], "big"), int.from_bytes(data[9:11], "big")
    if not (0 < width <= 2048 and 0 < height <= 2048 and width * height <= _MAX_PIXELS):
        raise MediaUnavailable("image-too-large")
    size = int.from_bytes(data[18:22], "big")
    if size != len(data) - 22:
        raise MediaUnavailable("wxgf-profile-unsupported")
    payload = data[22:]
    starts = list(itertools.islice(re.finditer(b"\x00\x00\x00?\x01", payload), 65))
    if not starts or starts[0].start() or len(starts) > 64:
        raise MediaUnavailable("wxgf-profile-unsupported")
    types = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(payload)
        nalu = payload[start.end():end]
        if len(nalu) < 3:
            raise MediaUnavailable("wxgf-profile-unsupported")
        kind = (nalu[0] >> 1) & 63
        layer = ((nalu[0] & 1) << 5) | (nalu[1] >> 3)
        # Only an independent IDR picture with one VPS/SPS/PPS and optional SEI.
        # In particular, no auxiliary/alpha layers or other VCL pictures.
        if nalu[0] & 128 or layer or not nalu[1] & 7 or kind not in (19, 20, 32, 33, 34, 39, 40):
            raise MediaUnavailable("wxgf-profile-unsupported")
        types.append(kind)
    if (any(types.count(kind) != 1 for kind in (32, 33, 34)) or
            types[:3] != [32, 33, 34] or sum(kind in (19, 20) for kind in types) != 1):
        raise MediaUnavailable("wxgf-profile-unsupported")
    return payload, width, height


def _bounded_process(command, data, output_limit, timeout=_TIMEOUT):
    """Bound both pipes while running, including when a decoder never exits."""
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=0x08000000 if os.name == "nt" else 0)
    except OSError:
        raise MediaUnavailable("media-conversion-failed") from None
    outputs = [bytearray(), bytearray()]
    failed = threading.Event()

    def stop():
        failed.set()
        try:
            process.kill()
        except OSError:
            pass

    def read(stream, output, limit):
        try:
            while chunk := stream.read(min(65536, limit + 1 - len(output))):
                output.extend(chunk)
                if len(output) > limit:
                    stop()
                    return
        except OSError:
            stop()

    def write():
        try:
            process.stdin.write(data)
            process.stdin.close()
        except (OSError, ValueError):
            stop()

    threads = [threading.Thread(target=read, args=(process.stdout, outputs[0], output_limit), daemon=True),
               threading.Thread(target=read, args=(process.stderr, outputs[1], 65536), daemon=True),
               threading.Thread(target=write, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop()
            process.wait(timeout=2)
        for thread in threads:
            thread.join(timeout=2)
        if failed.is_set() or process.returncode or any(thread.is_alive() for thread in threads):
            raise MediaUnavailable("media-conversion-failed")
        return bytes(outputs[0]), bytes(outputs[1])
    finally:
        if process.poll() is None:
            stop()
            process.wait(timeout=2)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def decode_still(data, executable):
    """Return a provider-safe JPEG only after decoding the complete single picture."""
    payload, width, height = _picture(data)
    command = [executable, "-nostdin", "-hide_banner", "-loglevel", "level+info", "-xerror",
               "-max_alloc", "16777216", "-protocol_whitelist", "pipe", "-threads", "1",
               "-max_pixels", str(_MAX_PIXELS), "-err_detect", "explode", "-f", "hevc", "-i", "pipe:0",
               "-an", "-sn", "-dn", "-vf", "showinfo,format=rgb24", "-threads", "1",
               "-fps_mode", "passthrough", "-frames:v", "2", "-f", "rawvideo", "pipe:1"]
    output, diagnostics = _bounded_process(command, payload, width * height * 3 * 2)
    log = diagnostics.decode("utf-8", errors="replace")
    seen = re.findall(r"\bn:\s*\d+.*?\bfmt:(\S+).*?\bs:(\d+)x(\d+)", log)
    if ("[error]" in log or "[warning]" in log or "[fatal]" in log or
            seen != [("yuv420p", str(width), str(height))] or len(output) != width * height * 3):
        raise MediaUnavailable("wxgf-profile-unsupported")
    from PIL import Image
    with Image.frombytes("RGB", (width, height), output) as image:
        encoded = io.BytesIO()
        image.save(encoded, "PNG")
    return analysis_image(encoded.getvalue())[0]


def _external_hash(db, account, identifier):
    from media_database import prepare_emoticon_databases
    if hasattr(db, "_keys"):
        prepare_emoticon_databases(db)
    matches = []
    for rel, raw_path, _size in getattr(db, "_db_files", ()):
        if str(rel).replace("\\", "/") != "emoticon/emoticon.db":
            continue
        path = Path(raw_path)
        check_owned_path(path)
        if path.resolve() != account / "db_storage" / "emoticon" / "emoticon.db":
            raise MediaUnavailable("emoticon-database-unavailable")
        if hasattr(db, "_keys") and rel not in db._keys:
            continue
        connection = db._open(rel)
        try:
            matches.extend(connection.execute("SELECT extern_md5 FROM kNonStoreEmoticonTable "
                "WHERE lower(md5)=? LIMIT 2", (identifier,)).fetchall())
        finally:
            connection.close()
        if len(matches) > 1:
            break
    if len(matches) != 1 or not isinstance(matches[0][0], str) or not re.fullmatch(r"[a-fA-F0-9]{32}", matches[0][0]):
        raise MediaUnavailable("wxgf-mapping-unavailable")
    return matches[0][0].lower()


def local_still(db, identifier, key_for_path, executable):
    """Verify the account's local alias and CBC plaintext before any conversion.

    The returned JPEG is deliberately not archived/cached as the original sticker:
    its MD5 is neither the original identity nor the verified WXGF external hash.
    """
    if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise MediaUnavailable("metadata-unavailable")
    account = Path(db.account_dir).absolute()
    check_owned_path(account)
    account = account.resolve()
    external = _external_hash(db, account, identifier)
    candidates = [account / "business" / "emoticon" / "Persist" / identifier[:2] / identifier]
    cache = account / "cache"
    check_owned_path(cache)
    if cache.is_dir():
        for month in sorted(cache.iterdir(), reverse=True):
            if re.fullmatch(r"\d{4}-\d{2}", month.name):
                candidates.append(month / "Emoticon" / identifier[:2] / identifier)
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.padding import PKCS7
    for path in candidates:
        try:
            check_owned_path(path)
            if not path.is_file() or not path.resolve().is_relative_to(account):
                continue
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if not 0 < before.st_size <= MAX_IMAGE + 16 or before.st_size % 16:
                    continue
                encrypted = stream.read(MAX_IMAGE + 17)
                after = os.fstat(stream.fileno())
            check_owned_path(path)
            current = path.stat()
            # Windows path stat and handle fstat can expose different ctime
            # semantics after a rename; compare ctime only across the same handle.
            signature = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            if (not path.resolve().is_relative_to(account) or signature(before) != signature(after) or
                    before.st_ctime_ns != after.st_ctime_ns or signature(before) != signature(current) or
                    len(encrypted) != before.st_size):
                continue
            key = key_for_path(path)
            if not isinstance(key, bytes) or len(key) != 16:
                continue
            cipher = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
            unpad = PKCS7(128).unpadder()
            raw = cipher.update(encrypted) + cipher.finalize()
            plain = unpad.update(raw) + unpad.finalize()
            if hashlib.md5(plain).hexdigest() != external:
                continue
            return decode_still(plain, executable())
        except (OSError, ValueError, MediaUnavailable):
            continue
    raise MediaUnavailable("sticker-unavailable")
