"""Pinned Microsoft release CRT deployment and native-load verification."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from pe_imports import imported_dlls

VERSION = "14.44.35211.0"
CRT_FILES = frozenset({"msvcp140.dll", "msvcp140_1.dll", "msvcp140_atomic_wait.dll",
                       "vcruntime140.dll", "vcruntime140_1.dll"})
LICENSE = "Microsoft-Visual-Cpp-Runtime-2015-2022.rtf"
NOTICE = "Microsoft-Visual-Cpp-Runtime-NOTICE.md"
FILES = CRT_FILES | {LICENSE, NOTICE}
NATIVE_SUFFIXES = {".exe", ".dll", ".pyd", ".node"}
CRT_NAME = re.compile(r"(?:msvcp|vcruntime|concrt|vcomp)[0-9].*\.dll\Z")
VENDORED_NAME = re.compile(r"msvcp140-[a-f0-9]{32}\.dll\Z")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def specification(root):
    value = json.loads((Path(root) / "scripts/msvc-runtime.json").read_text(encoding="utf-8"))
    if (value.get("kind") != "zhiji-msvc-app-local" or value.get("schema") != 1 or
            value.get("version") != VERSION or value.get("platform") != "win-x64-release" or
            value.get("origin", {}).get("sha256") != "cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b" or
            value.get("origin", {}).get("authenticodeStatus") != "Valid"):
        raise ValueError("unreviewed Microsoft app-local runtime specification")
    rows = value.get("files")
    if not isinstance(rows, list) or len(rows) != len(FILES) or {row.get("name") for row in rows} != FILES:
        raise ValueError("Microsoft runtime inventory differs")
    for row in rows:
        if (type(row.get("bytes")) is not int or row["bytes"] <= 0 or
                not re.fullmatch(r"[a-f0-9]{64}", row.get("sha256", "")) or
                (row["name"] in CRT_FILES and row.get("version") != VERSION)):
            raise ValueError("invalid Microsoft runtime metadata")
    return value


def check_file(path, row):
    if (Path(path).is_symlink() or not Path(path).is_file() or
            Path(path).stat().st_size != row["bytes"] or digest(path) != row["sha256"]):
        raise ValueError("Microsoft runtime hash differs: " + row["name"])


def checked_runtime(root):
    root = Path(root)
    spec = specification(root)
    base = root / ".local/tools/msvc-runtime/dist"
    for path in (base, *base.parents):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("linked Microsoft runtime input")
        if path == root:
            break
    if not base.is_dir() or {p.name for p in base.iterdir()} != FILES | {"manifest.json"}:
        raise ValueError("Microsoft app-local runtime missing; run scripts/setup-msvc-runtime.py")
    if json.loads((base / "manifest.json").read_text(encoding="utf-8")) != spec:
        raise ValueError("Microsoft runtime provenance manifest differs")
    for row in spec["files"]:
        check_file(base / row["name"], row)
        if row["name"] in CRT_FILES:
            required_crt(imported_dlls(base / row["name"]), set())
    return base, spec


def required_crt(imports, vendor_names):
    result = set()
    for name in imports:
        if not CRT_NAME.fullmatch(name):
            continue
        if name in CRT_FILES:
            result.add(name)
        elif not (VENDORED_NAME.fullmatch(name) and name in vendor_names):
            raise ValueError("unreviewed Microsoft runtime dependency: " + name)
    return result


def native_files(folder):
    return sorted(p for p in Path(folder).rglob("*") if p.is_file() and p.suffix.lower() in NATIVE_SUFFIXES)


def deployment_targets(client):
    client = Path(client)
    targets = {"runtime/node", "runtime/python", "runtime/judge"}
    files = native_files(client / "node_modules")
    vendor = {p.name.lower() for p in files}
    for path in files:
        if required_crt(imported_dlls(path), vendor):
            # Node's addon loader can prefer its native module folder over the
            # executable directory. Co-location prevents a System32 fallback.
            targets.add(path.parent.relative_to(client).as_posix())
    return sorted(targets)


def stage_runtime(root, client, copy_file):
    base, spec = checked_runtime(root)
    client = Path(client)
    targets = deployment_targets(client)
    for row in spec["files"]:
        copy_file(base / row["name"], client / "runtime/msvc" / row["name"], "msvc_runtime")
        if row["name"] in CRT_FILES:
            for target in targets:
                copy_file(base / row["name"], client / target / row["name"], "msvc_runtime")
    manifest = {**spec, "packagedDirs": targets}
    (client / "runtime/msvc/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    verify_deployment(client, spec, include_judge=False, probes=False)
    return {"version": VERSION, "packagedDirs": targets, "dllsPerDirectory": len(CRT_FILES)}


def verify_deployment(client, spec, *, include_judge=True, probes=True):
    client = Path(client).resolve()
    manifest = json.loads((client / "runtime/msvc/manifest.json").read_text(encoding="utf-8"))
    targets = deployment_targets(client)
    if manifest != {**spec, "packagedDirs": targets}:
        raise ValueError("packaged Microsoft runtime manifest differs")
    for row in spec["files"]:
        check_file(client / "runtime/msvc" / row["name"], row)
        if row["name"] in CRT_FILES:
            for target in targets:
                check_file(client / target / row["name"], row)
    scanned = 0
    for folder in (client / "node_modules", client / "runtime/python", *([client / "runtime/judge"] if include_judge else [])):
        files = native_files(folder)
        vendor = {p.name.lower() for p in files}
        for path in files:
            required_crt(imported_dlls(path), vendor)
            scanned += 1
    result = {"version": VERSION, "nativeImagesChecked": scanned, "packagedDirs": targets}
    if probes:
        result["loads"] = load_probes(client, spec, include_judge=include_judge)
    return result


NODE_PROBE = r"""
const ort = require('onnxruntime-node');
if (typeof ort.InferenceSession?.create !== 'function') throw Error('native ONNX binding unavailable');
const dlls = process.report.getReport().sharedObjects.filter(p => /[\\/](msvcp140[^\\/]*|vcruntime140[^\\/]*)\.dll$/i.test(p));
process.stdout.write(JSON.stringify({backends:ort.listSupportedBackends(),dlls}));
"""
PYTHON_PROBE = r"""
import ctypes,json,sys
from pathlib import Path
import numpy,cv2,sqlite3,ssl,ctypes
names = ('msvcp140.dll','msvcp140_1.dll','msvcp140_atomic_wait.dll','vcruntime140.dll','vcruntime140_1.dll')
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.GetModuleHandleW.argtypes=[ctypes.c_wchar_p]; kernel.GetModuleHandleW.restype=ctypes.c_void_p
kernel.GetModuleFileNameW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_uint32]
paths=[]
for name in names:
    handle=kernel.GetModuleHandleW(name)
    if not handle:
        handle=ctypes.WinDLL(str(Path(sys.executable).parent / name))._handle
    buffer=ctypes.create_unicode_buffer(32768)
    if not kernel.GetModuleFileNameW(handle,buffer,len(buffer)): raise RuntimeError('module path unavailable')
    paths.append(buffer.value)
print(json.dumps({'dlls':paths}))
"""


def load_probes(client, spec, *, include_judge=True):
    environment = {key: os.environ[key] for key in
                   ("SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP") if key in os.environ}
    result = {}
    rows = {row["name"]: row for row in spec["files"] if row["name"] in CRT_FILES}
    for label, command in (
        ("node", [str(client / "runtime/node/node.exe"), "-e", NODE_PROBE]),
        ("python", [str(client / "runtime/python/python.exe"), "-I", "-B", "-c", PYTHON_PROBE]),
    ):
        process = subprocess.run(command, cwd=client, env=environment, capture_output=True, text=True,
                                 encoding="utf-8", timeout=45, creationflags=0x08000000 if os.name == "nt" else 0)
        if process.returncode:
            raise ValueError("packaged Microsoft runtime native import failed: " + label)
        loaded = json.loads(process.stdout)
        paths = {}
        for value in loaded["dlls"]:
            path = Path(value[4:] if value.startswith("\\\\?\\") else value).resolve()
            if path.name.lower() not in CRT_FILES:
                continue
            if not path.is_relative_to(client):
                raise ValueError("Microsoft runtime loaded from outside the package: " + label + ":" + path.name)
            check_file(path, rows[path.name.lower()])
            paths[path.name.lower()] = path.relative_to(client).as_posix()
        if set(paths) != CRT_FILES:
            raise ValueError("native probe did not observe the full local Microsoft runtime: " + label)
        result[label] = paths
    if include_judge:
        process = subprocess.run([str(client / "runtime/judge/llama-server.exe"), "--version"], cwd=client,
                                 env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=30, creationflags=0x08000000 if os.name == "nt" else 0)
        if process.returncode:
            raise ValueError("packaged judge runtime cannot load its local dependencies")
        result["judge"] = "PE closure hashes and --version passed; no model or account opened"
    return result
