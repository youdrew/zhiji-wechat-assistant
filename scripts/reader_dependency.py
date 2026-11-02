"""Produce the reviewed read-only subset of wechatauto-replica at build time.

The installed development dependency is never modified. Its original Apache-2.0
license and package metadata remain with the staged subset.
"""
from __future__ import annotations

import ast
import base64
import csv
import hashlib
import io
from pathlib import Path


READER_VERSION = "1.2.2.6"
READER_DEPENDENCIES = ("cryptography", "colorama", "imageio-ffmpeg", "zstandard")
READER_SOURCE_HASHES = {
    "db.py": "3ac3d38a28208709da1a96dec18934fe4b6c3d64c095cbcc504a7f44ea0d765a",
    "media.py": "9fc5aa59c00e2120a78b615ccffb42519cbc68d886d8743ec0528a0be7797cd4",
    "logger.py": "9eca86a2e3b7a7c5967aa71389b1f552866f0b1276e8f1c6487e3fe001b3ba90",
    "param.py": "5cee864bbe0465339e80ecc681a1cf46f88ce86dd634ce23832cc0ca4d14f683",
}
REMOVED_MEDIA_METHODS = frozenset({"_find_preview_button", "download_image_original"})
READER_INIT = f'''"""Read-only database/media subset of wechatauto-replica.

Modified by Know Your Friends contributors, 2026-11-02 (Apache-2.0).
GUI automation, message sending and Moments modules are excluded.
See KYF_READ_ONLY.md and the original distribution license.
"""
from .db import WeChatDB, GroupMemberWatcher, auto_detect_db_dir, list_accounts
from .media import MediaDownloader

__version__ = {READER_VERSION!r}
__all__ = ["WeChatDB", "GroupMemberWatcher", "auto_detect_db_dir", "list_accounts", "MediaDownloader"]
'''.encode("utf-8")


def reader_media_source(payload: bytes) -> bytes:
    """Remove the two UI-driven original-image methods, preserving other source."""
    text = payload.decode("utf-8")
    tree = ast.parse(text)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "MediaDownloader"]
    if len(classes) != 1:
        raise ValueError("unreviewed MediaDownloader class layout")
    methods = [node for node in classes[0].body
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in REMOVED_MEDIA_METHODS]
    if {node.name for node in methods} != REMOVED_MEDIA_METHODS:
        raise ValueError("unreviewed MediaDownloader UI method layout")
    lines = text.splitlines(keepends=True)
    for node in sorted(methods, key=lambda item: item.lineno, reverse=True):
        first = min([node.lineno, *(item.lineno for item in node.decorator_list)])
        del lines[first - 1:node.end_lineno]
    result = "# Modified by Know Your Friends contributors, 2026-11-02 (Apache-2.0): UI methods removed.\n" + "".join(lines)
    for node in ast.walk(ast.parse(result)):
        if isinstance(node, ast.ImportFrom) and node.module in {"wx", "guia", "uia_driver", "moment", "sender"}:
            raise ValueError("reader media subset still imports GUI automation")
    return result.encode("utf-8")


def reader_metadata_headers(payload: bytes) -> bytes:
    """Keep package identity/licensing headers without the upstream README body."""
    headers = []
    for line in payload.splitlines():
        if not line.strip():
            break
        headers.append(line)
    return b"\n".join(headers) + b"\n\n"


def stage_reader_distribution(dist, source_site: Path, target_site: Path, copy_file) -> dict:
    """Stage only pinned reader modules, original licensing and fresh RECORD."""
    if dist.version != READER_VERSION:
        raise ValueError("unreviewed wechatauto-replica version; review the read-only subset before building")
    source_site = source_site.resolve()
    records = {}
    for record in dist.files or []:
        original = Path(dist.locate_file(record))
        if original.is_symlink():
            raise ValueError("linked wechatauto reader input")
        resolved = original.resolve()
        if resolved.is_file() and resolved.is_relative_to(source_site):
            records[resolved.relative_to(source_site).as_posix()] = resolved
    payloads = {}
    for name, expected in READER_SOURCE_HASHES.items():
        original = records.get("wechatauto/" + name)
        if original is None:
            raise ValueError("missing reviewed wechatauto reader source")
        data = original.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError("wechatauto reader source differs from the reviewed distribution")
        payloads[name] = data
    metadata_roots = {name.split("/")[0] for name in records
                      if name.split("/")[0].lower().startswith("wechatauto_replica-") and
                      name.split("/")[0].endswith(".dist-info")}
    if len(metadata_roots) != 1:
        raise ValueError("missing wechatauto reader distribution metadata")
    metadata_root = metadata_roots.pop()
    selected = {name: original for name, original in records.items()
                if name.startswith(metadata_root + "/") and
                (name.split("/", 1)[1] in {"METADATA", "WHEEL", "top_level.txt"} or
                 name.split("/", 1)[1].startswith("licenses/"))}
    if not any(name.lower().endswith("/license") or name.lower().endswith("/license.txt") for name in selected):
        raise ValueError("original wechatauto-replica license missing")
    names = []
    for name, original in selected.items():
        if name == metadata_root + "/METADATA":
            destination = target_site / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(reader_metadata_headers(original.read_bytes()))
        else:
            copy_file(original, target_site / name, "python_packages")
        names.append(name)
    generated = {"__init__.py": READER_INIT, "media.py": reader_media_source(payloads["media.py"])}
    for name in ("db.py", "logger.py", "param.py"):
        relative = "wechatauto/" + name
        copy_file(records[relative], target_site / relative, "python_packages")
        names.append(relative)
    hashes = "\n".join(f"- `{name}`: `{checksum}`" for name, checksum in READER_SOURCE_HASHES.items())
    generated["KYF_READ_ONLY.md"] = f'''# wechatauto-replica read-only subset

Upstream: https://github.com/fanyuantaier/wechatauto-replica, version {READER_VERSION}.
Original Apache-2.0 license and distribution metadata headers are retained in `{metadata_root}`.

Modified by Know Your Friends contributors, 2026-11-02:
- Replaced the package initializer with database/media exports only.
- Retained `db.py`, `logger.py`, `param.py` and local media decoding in `media.py`.
- Removed `MediaDownloader.download_image_original` and its UI preview helper.
- Excluded GUI, sender, WeChat client, Moments, observers, recall, message UI and demo modules.
- Removed METADATA's upstream README long description and its account examples; package identity and licensing headers remain.
- Rebuilt RECORD to describe this subset. The original dependency installation is unchanged.

These modules read WeChat data; they may create local decrypted snapshots/media caches.
No GUI operation that triggers a WeChat download or sends a message is included.

Reviewed original source SHA-256:
{hashes}
'''.encode("utf-8")
    for name, data in generated.items():
        relative = "wechatauto/" + name
        destination = target_site / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        names.append(relative)
    record_name = metadata_root + "/RECORD"
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted(names):
        data = (target_site / name).read_bytes()
        checksum = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        writer.writerow((name, "sha256=" + checksum, len(data)))
    writer.writerow((record_name, "", ""))
    (target_site / record_name).write_text(output.getvalue(), encoding="utf-8", newline="")
    names.append(record_name)
    return {"name": dist.metadata["Name"], "version": dist.version, "profile": "database-media-only",
            "recordedFiles": len(names), "recordedBytes": sum((target_site / name).stat().st_size for name in names)}
