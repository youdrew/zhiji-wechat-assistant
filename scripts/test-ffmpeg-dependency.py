"""Synthetic source-delivery and wheel replacement checks; no binary is executed."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import ffmpeg_dependency as ffmpeg

SETUP_SPEC = importlib.util.spec_from_file_location("setup_media_runtimes", Path(__file__).with_name("setup-media-runtimes.py"))
setup = importlib.util.module_from_spec(SETUP_SPEC)
SETUP_SPEC.loader.exec_module(setup)


class FFmpegDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kyf-ffmpeg-distribution-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / ".local/tools/ffmpeg-lgpl/dist"
        self.base.mkdir(parents=True)
        self.spec_path = self.root / "scripts/ffmpeg-runtime.json"
        self.spec_path.parent.mkdir()
        rows = []
        for name in sorted(ffmpeg.SOURCE_FILES):
            path = self.base / name
            path.write_bytes(("synthetic reviewed LGPL file: " + name).encode())
            rows.append({"name": name, "bytes": path.stat().st_size, "sha256": ffmpeg.file_sha256(path)})
        self.spec = {"kind": "zhiji-ffmpeg-lgpl", "version": "7.1", "license": "LGPL-2.1-or-later",
                     "configuration": "--disable-gpl --disable-nonfree --disable-autodetect --disable-network",
                     "sourceArchive": "ffmpeg-7.1.tar.xz", "files": rows}
        self.write_spec()

    def write_spec(self):
        text = json.dumps(self.spec)
        self.spec_path.write_text(text, encoding="utf-8")
        (self.base / "manifest.json").write_text(text, encoding="utf-8")

    def test_source_and_license_closure_is_required_and_every_hash_is_checked(self):
        self.assertEqual(ffmpeg.checked_ffmpeg(self.root)[0], self.base)
        (self.base / "ffmpeg-7.1.tar.xz").write_bytes(b"altered source archive")
        with self.assertRaisesRegex(ValueError, "hash differs"):
            ffmpeg.checked_ffmpeg(self.root)
        (self.base / "ffmpeg-7.1.tar.xz").unlink()
        with self.assertRaisesRegex(ValueError, "unexpected or missing"):
            ffmpeg.checked_ffmpeg(self.root)

    def test_unreviewed_or_gpl_build_and_extra_runtime_data_are_rejected(self):
        self.spec["configuration"] += " --enable-gpl"
        self.write_spec()
        with self.assertRaisesRegex(ValueError, "unreviewed"):
            ffmpeg.checked_ffmpeg(self.root)
        self.spec["configuration"] = "--disable-gpl"
        self.write_spec()
        (self.base / "personal-settings.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "unexpected or missing"):
            ffmpeg.checked_ffmpeg(self.root)

    def test_fresh_checkout_preparation_uses_only_explicit_hashed_inputs_and_small_resources(self):
        fresh = self.root / "fresh-checkout"
        resources = fresh / "scripts/ffmpeg-runtime-resources"
        resources.mkdir(parents=True)
        (fresh / "scripts/ffmpeg-runtime.json").write_text(json.dumps(self.spec))
        for name in ffmpeg.SOURCE_FILES - {"ffmpeg.exe", "ffmpeg-7.1.tar.xz"}:
            shutil.copy2(self.base / name, resources / name)
        inputs = {name: self.base / name for name in ("ffmpeg.exe", "ffmpeg-7.1.tar.xz")}
        target = setup.prepare_bundle(fresh, "ffmpeg-lgpl", "ffmpeg-runtime.json", ffmpeg.SOURCE_FILES,
                                      "ffmpeg-runtime-resources", inputs)
        self.assertEqual(ffmpeg.checked_ffmpeg(fresh)[0], target)
        self.assertEqual(target, fresh / ".local/tools/ffmpeg-lgpl/dist")
        self.assertEqual(setup.prepare_bundle(fresh, "ffmpeg-lgpl", "ffmpeg-runtime.json", ffmpeg.SOURCE_FILES,
                                              "ffmpeg-runtime-resources"), target)

    def test_preparation_refuses_to_overwrite_existing_unreviewed_runtime_data(self):
        extra = self.base / "personal-settings.json"
        extra.write_bytes(b"synthetic local settings")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            setup.prepare_bundle(self.root, "ffmpeg-lgpl", "ffmpeg-runtime.json", ffmpeg.SOURCE_FILES,
                                 "ffmpeg-runtime-resources")
        self.assertEqual(extra.read_bytes(), b"synthetic local settings")

    def test_wheel_gpl_executable_is_excluded_and_new_wrapper_and_sources_are_delivered(self):
        installed = self.root / "site-packages"
        package = installed / "imageio_ffmpeg"
        package.mkdir(parents=True)
        definitions = b'FNAME_PER_PLATFORM = {"windows-x86_64": "ffmpeg-win-x86_64-v7.1.exe"}\n'
        files = {"imageio_ffmpeg/__init__.py": b"# synthetic BSD wrapper\n",
                 "imageio_ffmpeg/_definitions.py": definitions,
                 "imageio_ffmpeg/binaries/__init__.py": b"",
                 "imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe": b"synthetic original GPL executable",
                 "imageio_ffmpeg/binaries/README.md": b"upstream wheel binary readme",
                 "imageio_ffmpeg-0.6.0.dist-info/LICENSE": b"synthetic BSD license",
                 "imageio_ffmpeg-0.6.0.dist-info/METADATA": b"Name: imageio-ffmpeg\n",
                 "imageio_ffmpeg-0.6.0.dist-info/RECORD": b"original unmodified wheel record"}
        for name, data in files.items():
            path = installed / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        dist = SimpleNamespace(version="0.6.0", metadata={"Name": "imageio-ffmpeg"},
                               files=[Path(name) for name in files], locate_file=lambda name: installed / name)
        stage = self.root / "client"
        target = stage / "runtime/python/Lib/site-packages"
        def copy(source, destination, _category):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        with mock.patch.object(ffmpeg, "DEFINITIONS_SHA256", hashlib.sha256(definitions).hexdigest()):
            result = ffmpeg.stage_imageio_distribution(dist, installed, target, stage, self.root, copy)
        self.assertEqual(result["profile"], "reviewed-lgpl-ffmpeg")
        binaries = target / "imageio_ffmpeg/binaries"
        self.assertEqual({path.name for path in binaries.iterdir()}, {"__init__.py", ffmpeg.EXECUTABLE_NAME})
        self.assertEqual((binaries / ffmpeg.EXECUTABLE_NAME).read_bytes(), (self.base / "ffmpeg.exe").read_bytes())
        self.assertIn(ffmpeg.EXECUTABLE_NAME, (target / "imageio_ffmpeg/_definitions.py").read_text())
        self.assertEqual({path.name for path in (stage / "runtime/ffmpeg").iterdir()},
                         (ffmpeg.SOURCE_FILES - {"ffmpeg.exe"}) | {"manifest.json"})
        self.assertTrue((target / "imageio_ffmpeg-0.6.0.dist-info/LICENSE").is_file())
        record = (target / "imageio_ffmpeg-0.6.0.dist-info/RECORD").read_text()
        self.assertIn(ffmpeg.EXECUTABLE_NAME, record)
        self.assertNotIn("ffmpeg-win-x86_64-v7.1.exe", record)
        self.assertEqual((package / "binaries/ffmpeg-win-x86_64-v7.1.exe").read_bytes(),
                         b"synthetic original GPL executable")


if __name__ == "__main__":
    unittest.main()
