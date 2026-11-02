"""Bind the exact OpenCV FFmpeg DLL to its delivered upstream source archives."""
from __future__ import annotations

import importlib.metadata as metadata
import json
from pathlib import Path
import shutil
import sysconfig
import tempfile
import unittest

from ffmpeg_dependency import file_sha256
import opencv_dependency as opencv


class OpenCVSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kyf-opencv-sources-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / ".local/tools/opencv-ffmpeg-source/dist"
        self.base.mkdir(parents=True)
        self.site = self.root / "site-packages"
        self.dll = self.site / "cv2/opencv_videoio_ffmpeg4130_64.dll"
        self.dll.parent.mkdir(parents=True)
        self.dll.write_bytes(b"MZ synthetic reviewed DLL")
        self.payload = self.dll.read_bytes()
        rows = []
        for name in sorted(opencv.SOURCE_FILES):
            path = self.base / name
            path.write_bytes(("synthetic official source material: " + name).encode())
            rows.append({"name": name, "bytes": path.stat().st_size, "sha256": file_sha256(path)})
        self.spec = {"kind": "zhiji-opencv-ffmpeg-sources", "packageVersion": "4.13.0.90",
                     "binary": {"name": self.dll.name, "bytes": self.dll.stat().st_size, "sha256": file_sha256(self.dll)},
                     "binaryCommit": "d82ad9a54a7b42a1648a9cae8fed5c2f20ea396c",
                     "sourceCommit": "62bd53c24feadd92af0146462317fcd4d64fd842", "files": rows}
        (self.root / "scripts").mkdir()
        (self.root / "scripts/opencv-ffmpeg-runtime.json").write_text(json.dumps(self.spec))
        (self.base / "manifest.json").write_text(json.dumps(self.spec))

    @staticmethod
    def copy(source, destination, _category):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    def stage(self):
        return opencv.stage_opencv_sources(self.root, self.site, self.root / "client", "4.13.0.90", self.copy)

    def test_exact_source_closure_is_delivered_for_the_pinned_binary(self):
        result = self.stage()
        self.assertEqual(result["binarySha256"], file_sha256(self.dll))
        delivered = self.root / "client/runtime/ffmpeg-opencv"
        self.assertEqual({path.name for path in delivered.iterdir()}, opencv.SOURCE_FILES | {"manifest.json"})

    def test_binary_or_source_drift_stops_before_a_source_bundle_is_recorded(self):
        self.dll.write_bytes(b"changed DLL")
        with self.assertRaisesRegex(ValueError, "DLL differs"):
            self.stage()
        self.dll.write_bytes(self.payload)
        (self.base / "ffmpeg-src-n4.4.6.tar.xz").write_bytes(b"changed source archive")
        with self.assertRaisesRegex(ValueError, "source hash differs"):
            self.stage()
        self.assertFalse((self.root / "client").exists())

    def test_installed_wheel_ffmpeg_dll_matches_the_reviewed_real_source_bundle(self):
        try:
            dist = metadata.distribution("opencv-python")
        except metadata.PackageNotFoundError:
            self.skipTest("opencv-python is not installed in this test interpreter")
        root = Path(__file__).resolve().parents[1]
        spec = json.loads((root / "scripts/opencv-ffmpeg-runtime.json").read_text())
        self.assertEqual(dist.version, spec["packageVersion"])
        dll = Path(sysconfig.get_path("purelib")) / "cv2" / spec["binary"]["name"]
        self.assertEqual(dll.stat().st_size, spec["binary"]["bytes"])
        self.assertEqual(file_sha256(dll), spec["binary"]["sha256"])


if __name__ == "__main__":
    unittest.main()
