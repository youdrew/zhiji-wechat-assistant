"""Synthetic packaging and setup checks for the fixed local-judge runtime."""
# Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    loader = importlib.util.spec_from_file_location(name.replace("-", "_"), ROOT / "scripts" / (name + ".py"))
    result = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(result)
    return result


stage, setup = load("stage-real-client"), load("setup-judge-runtime")


class JudgeStageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kyf-judge-package-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        (self.source / "scripts").mkdir(parents=True)
        (self.source / "package.json").write_text('{"version":"0.0.0-synthetic"}', encoding="utf-8")
        self.spec = copy.deepcopy(setup.specification(ROOT))
        self.base = self.source / ".local/tools/judge-runtime/b12032"
        self.base.mkdir(parents=True)
        for row in self.spec["files"]:
            data = b"MZ synthetic public runtime: " + row["name"].encode()
            (self.base / row["name"]).write_bytes(data)
            row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.write_manifests()
        for name in ("judge-model.json", "judge-selftest.json"):
            (self.source / "scripts" / name).write_text("{}", encoding="utf-8")
        self.output = self.root / "build/client"

    def write_manifests(self):
        (self.source / "scripts/judge-runtime.json").write_text(json.dumps(self.spec), encoding="utf-8")
        (self.base / "manifest.json").write_text(json.dumps(self.spec), encoding="utf-8")

    def minimal_stage(self):
        with (patch.object(stage, "SCRIPTS", ("judge-runtime.json", "judge-model.json", "judge-selftest.json")),
              patch.object(stage, "BRIDGE", ()), patch.object(stage, "NATIVE_READER", ()),
              patch.object(stage, "PUBLIC_FILES", ())):
            return stage.stage_public(self.source, self.output)

    def test_stage_copies_only_the_verified_runtime_and_rechecks_the_copied_closure(self):
        (self.source / ".local/private-account.json").write_text("PRIVATE_SYNTHETIC_SENTINEL", encoding="utf-8")
        (self.source / ".local/tools/unrelated-tool.exe").write_bytes(b"not part of the release")
        result = self.minimal_stage()
        names = {row["file"] for row in result["files"]}
        expected = {"runtime/judge/" + row["name"] for row in self.spec["files"]} | {"runtime/judge/manifest.json"}
        self.assertTrue(expected <= names)
        self.assertFalse(any(".local" in name or "private" in name for name in names))
        self.assertEqual(len(stage.judge_runtime_mappings(self.output)), len(self.spec["files"]) + 1)
        setup.checked_runtime(self.output / "runtime/judge", self.spec)
        self.assertFalse((self.output / "scripts/setup-judge-runtime.py").exists())
        (self.output / "runtime/judge/llama-server-impl.dll").write_bytes(b"tampered after copy")
        with self.assertRaises(ValueError):
            stage.judge_runtime_mappings(self.output)

    def test_missing_extra_or_modified_runtime_is_rejected_before_output_is_created(self):
        extra = self.base / "private-account.json"
        extra.write_text("PRIVATE_SYNTHETIC_SENTINEL", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing or altered"):
            self.minimal_stage()
        self.assertFalse(self.output.exists())
        extra.unlink()
        file = self.base / "llama-server.exe"
        saved = file.read_bytes()
        file.write_bytes(b"modified")
        with self.assertRaisesRegex(ValueError, "missing or altered"):
            self.minimal_stage()
        self.assertFalse(self.output.exists())
        file.write_bytes(saved)
        (self.base / "manifest.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing or altered"):
            self.minimal_stage()

    def test_unreviewed_release_unsafe_names_and_forged_local_manifest_are_rejected(self):
        self.spec["version"] = "unreviewed"
        self.write_manifests()
        with self.assertRaisesRegex(ValueError, "unreviewed"):
            stage.judge_runtime_mappings(self.source)
        self.spec["version"] = setup.VERSION
        self.spec["files"][0]["name"] = "../outside.dll"
        self.write_manifests()
        with self.assertRaisesRegex(ValueError, "invalid"):
            stage.judge_runtime_mappings(self.source)
        self.spec = copy.deepcopy(setup.specification(ROOT))
        self.write_manifests()
        forged = copy.deepcopy(self.spec)
        forged["files"][0]["sha256"] = "0" * 64
        (self.base / "manifest.json").write_text(json.dumps(forged), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing or altered"):
            stage.judge_runtime_mappings(self.source)

    def test_runtime_code_manifests_and_licenses_ship_but_build_download_helper_does_not(self):
        mapped = {relative.as_posix(): source for source, relative, _root in stage.public_mappings(ROOT)}
        for name in ("bridge/core_model_assets.py", "bridge/core_judge.py", "electron/model-gateway-local.ts",
                     "scripts/judge-model.json", "scripts/judge-runtime.json", "scripts/judge-selftest.json",
                     "licenses/llama.cpp-MIT.txt", "licenses/Julia-1-Apache-2.0.txt", "licenses/LLVM-OpenMP-Apache-2.0.txt"):
            with self.subTest(name=name):
                self.assertIn(name, mapped)
                self.assertTrue(mapped[name].is_file())
        self.assertNotIn("setup-judge-runtime.py", stage.SCRIPTS)
        self.assertTrue(all(row["name"] == "llama-server.exe" or not row["name"].endswith(".exe")
                            for row in setup.specification(ROOT)["files"]))


class JudgeSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kyf-judge-setup-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.spec = copy.deepcopy(setup.specification(ROOT))
        self.archive = self.root / setup.ARCHIVE

    def archive_fixture(self, extra=None):
        with zipfile.ZipFile(self.archive, "w") as content:
            for row in self.spec["files"]:
                data = b"synthetic zip dependency " + row["name"].encode()
                row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                content.writestr(row["name"], data)
            if extra:
                content.writestr(extra, b"synthetic unused archive entry")
        self.spec.update(archiveBytes=self.archive.stat().st_size, archiveSha256=setup.digest(self.archive))

    def test_existing_archive_install_is_exact_verified_and_idempotent_without_network(self):
        self.archive_fixture(extra="llama-cli.exe")
        with patch.object(setup, "specification", return_value=self.spec), patch.object(setup.urllib.request, "urlopen", side_effect=AssertionError("network forbidden")):
            setup.install(self.root, self.archive)
            base = self.root / ".local/tools/judge-runtime/b12032"
            setup.checked_runtime(base, self.spec)
            self.assertFalse((base / "llama-cli.exe").exists())
            setup.install(self.root, self.root / "no-longer-present.zip")
            (base / "llama.dll").write_bytes(b"modified runtime")
            with self.assertRaisesRegex(ValueError, "hash differs"):
                setup.install(self.root, self.archive)
            self.assertEqual((base / "llama.dll").read_bytes(), b"modified runtime")

    def test_archive_integrity_and_zip_paths_are_checked_before_installation(self):
        self.archive_fixture()
        self.spec["archiveSha256"] = "0" * 64
        with patch.object(setup, "specification", return_value=self.spec), self.assertRaisesRegex(ValueError, "checksum"):
            setup.install(self.root, self.archive)
        self.assertFalse((self.root / ".local/tools/judge-runtime/b12032").exists())
        self.archive_fixture(extra="../escaped.exe")
        with patch.object(setup, "specification", return_value=self.spec), self.assertRaisesRegex(ValueError, "unsafe"):
            setup.install(self.root, self.archive)
        self.assertFalse((self.root / ".local/tools/judge-runtime/b12032").exists())
        self.assertFalse((self.root / ".local/tools/judge-runtime/escaped.exe").exists())


if __name__ == "__main__":
    unittest.main()
