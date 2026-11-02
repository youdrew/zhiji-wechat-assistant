"""Synthetic PE/CRT closure regressions; never installs a runtime or executes fixtures."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import msvc_runtime as msvc
from pe_imports import imported_dlls


def pe_image(imports=(), delayed=()):
    image = bytearray(0x1000)
    image[:2] = b"MZ"
    struct.pack_into("<I", image, 0x3c, 0x80)
    image[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HH", image, 0x84, 0x8664, 1)
    struct.pack_into("<H", image, 0x94, 240)
    optional = 0x98
    struct.pack_into("<H", image, optional, 0x20b)
    struct.pack_into("<Q", image, optional + 24, 0x140000000)
    struct.pack_into("<I", image, optional + 60, 0x200)
    struct.pack_into("<I", image, optional + 108, 16)
    section = optional + 240
    struct.pack_into("<IIII", image, section + 8, 0xe00, 0x1000, 0xe00, 0x200)
    cursor = 0x600
    for directory_index, names, table, stride in ((1, imports, 0x200, 20), (13, delayed, 0x400, 32)):
        if not names:
            continue
        struct.pack_into("<II", image, optional + 112 + directory_index * 8,
                         table - 0x200 + 0x1000, (len(names) + 1) * stride)
        for index, name in enumerate(names):
            payload = name.encode("ascii") + b"\0"
            image[cursor:cursor + len(payload)] = payload
            rva = cursor - 0x200 + 0x1000
            if directory_index == 1:
                struct.pack_into("<I", image, table + index * stride + 12, rva)
            else:
                struct.pack_into("<II", image, table + index * stride, 1, rva)
            cursor += len(payload)
    return bytes(image)


class RuntimeClosureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="zhiji-crt-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = self.root / "client"
        self.dist = self.root / ".local/tools/msvc-runtime/dist"
        self.dist.mkdir(parents=True)
        self.put(self.client / "runtime/node/node.exe", pe_image())
        self.put(self.client / "runtime/python/python.exe", pe_image(("VCRUNTIME140.dll",)))
        self.put(self.client / "runtime/judge/llama-server.exe", pe_image(("VCRUNTIME140.dll",)))
        self.addon = self.client / "node_modules/synthetic/bin/onnxruntime_binding.node"
        self.put(self.addon, pe_image(("MSVCP140.dll",), ("MSVCP140_ATOMIC_WAIT.dll",)))
        rows = []
        for name in sorted(msvc.FILES):
            self.put(self.dist / name, pe_image() if name in msvc.CRT_FILES else b"synthetic legal text")
            row = {"name": name, "bytes": (self.dist / name).stat().st_size, "sha256": msvc.digest(self.dist / name)}
            if name in msvc.CRT_FILES:
                row["version"] = msvc.VERSION
            rows.append(row)
        self.spec = {"schema": 1, "kind": "zhiji-msvc-app-local", "version": msvc.VERSION,
                     "platform": "win-x64-release", "files": rows,
                     "origin": {"sha256": "cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b",
                                "authenticodeStatus": "Valid"}}
        self.put(self.root / "scripts/msvc-runtime.json", json.dumps(self.spec).encode())
        self.put(self.dist / "manifest.json", json.dumps(self.spec).encode())

    @staticmethod
    def put(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def copy(source, destination, _category):
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.read_bytes() != source.read_bytes():
            raise ValueError("refusing overwrite")
        shutil.copy2(source, destination)

    def test_pe_reader_handles_normal_and_delayed_imports_without_executing(self):
        self.assertEqual(imported_dlls(self.addon), {"msvcp140.dll", "msvcp140_atomic_wait.dll"})
        self.put(self.addon, pe_image(("KERNEL32.dll",), ("NODE.EXE",)))
        self.assertEqual(imported_dlls(self.addon), {"kernel32.dll", "node.exe"})
        self.put(self.addon, b"MZ truncated")
        with self.assertRaises(ValueError):
            imported_dlls(self.addon)

    def test_stage_colocates_crt_with_node_addon_and_each_host_executable(self):
        report = msvc.stage_runtime(self.root, self.client, self.copy)
        self.assertEqual(set(report["packagedDirs"]),
                         {"runtime/node", "runtime/python", "runtime/judge", "node_modules/synthetic/bin"})
        msvc.verify_deployment(self.client, self.spec, probes=False)
        (self.addon.parent / "msvcp140.dll").unlink()
        with self.assertRaisesRegex(ValueError, "hash differs"):
            msvc.verify_deployment(self.client, self.spec, probes=False)

    def test_pe_module_names_cover_observed_host_extensions_but_reject_paths_and_controls(self):
        for name in ("MSVCP140.dll", "NODE.EXE", "win32ui.pyd", "WINSPOOL.DRV"):
            for delayed in (False, True):
                with self.subTest(name=name, delayed=delayed):
                    self.put(self.addon, pe_image(() if delayed else (name,), (name,) if delayed else ()))
                    self.assertEqual(imported_dlls(self.addon), {name.lower()})
        for name in ("../outside.dll", "C:\\outside.exe", "\\\\server\\file.dll", "name\n.dll", "trailing.dll."):
            with self.subTest(name=name):
                self.put(self.addon, pe_image((name,)))
                with self.assertRaises(ValueError):
                    imported_dlls(self.addon)

    def test_new_unreviewed_crt_dependency_is_not_satisfied_by_the_developer_machine(self):
        self.put(self.addon, pe_image(("VCOMP140.dll",)))
        with self.assertRaisesRegex(ValueError, "unreviewed Microsoft runtime dependency"):
            msvc.stage_runtime(self.root, self.client, self.copy)

    def test_import_success_with_system32_crt_does_not_pass_local_load_gate(self):
        system = {"dlls": ["C:/Windows/System32/" + name for name in sorted(msvc.CRT_FILES)]}
        with mock.patch.object(msvc.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(system))):
            with self.assertRaisesRegex(ValueError, "outside the package"):
                msvc.load_probes(self.client, self.spec, include_judge=False)

    def test_extended_windows_module_paths_are_accepted_only_inside_the_package(self):
        if __import__('os').name != 'nt':
            self.skipTest("Windows path syntax")
        msvc.stage_runtime(self.root, self.client, self.copy)
        rows = {"dlls": ["\\\\?\\" + str((self.client / "runtime/node" / name).resolve()) for name in sorted(msvc.CRT_FILES)]}
        with mock.patch.object(msvc.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(rows))):
            result = msvc.load_probes(self.client, self.spec, include_judge=False)
        self.assertEqual(set(result["node"]), msvc.CRT_FILES)

    def test_same_name_old_crt_inside_package_cannot_pass_the_actual_load_gate(self):
        msvc.stage_runtime(self.root, self.client, self.copy)
        old = self.client / "unexpected/msvcp140.dll"
        self.put(old, b"synthetic old runtime version")
        paths = [str(old if name == old.name else self.client / "runtime/node" / name) for name in sorted(msvc.CRT_FILES)]
        with mock.patch.object(msvc.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps({"dlls": paths}))):
            with self.assertRaisesRegex(ValueError, "hash differs"):
                msvc.load_probes(self.client, self.spec, include_judge=False)

    def test_source_distribution_hash_drift_is_rejected_before_copy(self):
        (self.dist / "msvcp140.dll").write_bytes(pe_image(("kernel32.dll",)))
        with self.assertRaisesRegex(ValueError, "hash differs"):
            msvc.stage_runtime(self.root, self.client, self.copy)


if __name__ == "__main__":
    unittest.main()
