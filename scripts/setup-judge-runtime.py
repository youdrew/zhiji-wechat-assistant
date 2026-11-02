"""Prepare the pinned llama.cpp CPU runtime for development and packaging only.

End users receive this runtime in the installer. This helper is not shipped and
is never called by the application; --archive reuses a previously downloaded zip.
"""
# Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "b12032"
ARCHIVE = "llama-b12032-bin-win-cpu-x64.zip"
ARCHIVE_BYTES = 24_582_754
ARCHIVE_SHA256 = "5296f5d18bb7d422c7c26f4bb9bc796d9d4f41e1312a8c296cff475bdc60d0d4"
URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{VERSION}/{ARCHIVE}"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def linked(path):
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def specification(root=ROOT):
    value = json.loads((Path(root) / "scripts/judge-runtime.json").read_text(encoding="utf-8"))
    expected = {"schema": 1, "version": VERSION, "platform": "win-cpu-x64", "archiveName": ARCHIVE,
                "archiveBytes": ARCHIVE_BYTES, "archiveSha256": ARCHIVE_SHA256, "url": URL}
    if not isinstance(value, dict) or any(value.get(key) != item for key, item in expected.items()):
        raise ValueError("unreviewed judge runtime release")
    rows = value.get("files")
    names = set()
    if not isinstance(rows, list) or not 1 <= len(rows) <= 64:
        raise ValueError("invalid judge runtime inventory")
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        if (not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", name) or name.endswith(".") or
                name.casefold() in names or type(row.get("bytes")) is not int or not 0 < row["bytes"] < 128 * 1024**2 or
                not isinstance(row.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", row["sha256"])):
            raise ValueError("invalid judge runtime file")
        names.add(name.casefold())
    needed = {"llama-server.exe", "llama-server-impl.dll", "llama-common.dll", "llama.dll", "mtmd.dll",
              "ggml-base.dll", "ggml.dll", "ggml-rpc.dll", "ggml-cpu-x64.dll", "libomp.dll", "license-llvm-openmp"}
    if not needed <= names:
        raise ValueError("judge runtime dependency closure is incomplete")
    return value


def checked_runtime(folder, spec, *, additional_files=()):
    folder = Path(folder)
    if not folder.is_dir() or linked(folder):
        raise ValueError("judge runtime directory is missing or linked")
    expected = {row["name"]: row for row in spec["files"]}
    for row in additional_files:
        if (not isinstance(row, dict) or row.get("name") in expected or
                not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", row.get("name", "")) or
                type(row.get("bytes")) is not int or row["bytes"] <= 0 or
                not re.fullmatch(r"[a-f0-9]{64}", row.get("sha256", ""))):
            raise ValueError("invalid additional judge runtime dependency")
        expected[row["name"]] = row
    found = {item.name: item for item in folder.iterdir()}
    if set(found) != set(expected) | {"manifest.json"} or any(not item.is_file() or linked(item) for item in found.values()):
        raise ValueError("judge runtime inventory differs")
    manifest = found["manifest.json"]
    if manifest.stat().st_size > 64 * 1024 or json.loads(manifest.read_text(encoding="utf-8")) != spec:
        raise ValueError("judge runtime manifest differs from the pinned release")
    for name, row in expected.items():
        if found[name].stat().st_size != row["bytes"] or digest(found[name]) != row["sha256"]:
            raise ValueError(f"judge runtime hash differs: {name}")
    return spec


def install(root=ROOT, archive=None):
    root = Path(root).resolve()
    spec = specification(root)
    parent = root / ".local/tools/judge-runtime"
    current = root
    for part in parent.relative_to(root).parts:
        current = current / part
        if linked(current) or current.exists() and not current.is_dir():
            raise ValueError("judge runtime install directory is unsafe")
        current.mkdir(exist_ok=True)
    destination = parent / VERSION
    if destination.exists() or linked(destination):
        return checked_runtime(destination, spec)
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=parent) as temporary:
        work = Path(temporary)
        package = Path(archive).absolute() if archive is not None else work / ARCHIVE
        if archive is None:
            request = urllib.request.Request(URL, headers={"User-Agent": "Zhiji-Judge-Build/1"})
            with urllib.request.urlopen(request, timeout=120) as response, package.open("wb") as target:
                copied = 0
                while block := response.read(256 * 1024):
                    copied += len(block)
                    if copied > ARCHIVE_BYTES:
                        raise ValueError("judge runtime archive exceeds the pinned size")
                    target.write(block)
        if (not package.is_file() or linked(package) or package.stat().st_size != spec["archiveBytes"] or
                digest(package) != spec["archiveSha256"]):
            raise ValueError("judge runtime archive checksum differs")
        output = work / "runtime"
        output.mkdir()
        with zipfile.ZipFile(package) as content:
            entries = {}
            for entry in content.infolist():
                name = entry.filename
                if (entry.is_dir() or "/" in name or "\\" in name or ":" in name or
                        name in ("", ".", "..") or name.casefold() in entries or
                        stat.S_IFMT(entry.external_attr >> 16) == stat.S_IFLNK or entry.flag_bits & 1):
                    raise ValueError("unsafe judge runtime archive layout")
                entries[name.casefold()] = entry
            for row in spec["files"]:
                entry = entries.get(row["name"].casefold())
                if entry is None or entry.filename != row["name"] or entry.file_size != row["bytes"]:
                    raise ValueError("judge runtime archive dependency differs")
                with content.open(entry) as source, (output / row["name"]).open("wb") as target:
                    shutil.copyfileobj(source, target)
        (output / "manifest.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        checked_runtime(output, spec)
        os.rename(output, destination)
    return checked_runtime(destination, spec)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="reuse the fixed b12032 zip without downloading")
    args = parser.parse_args(argv)
    try:
        result = install(archive=args.archive)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        parser.exit(1, f"Judge runtime rejected: {error}\n")
    print(json.dumps({"version": result["version"], "files": len(result["files"]), "verified": True}))


if __name__ == "__main__":
    main()
