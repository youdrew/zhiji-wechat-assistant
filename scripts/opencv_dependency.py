"""Bind OpenCV's FFmpeg DLL to its exact upstream corresponding-source bundle."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ffmpeg_dependency import file_sha256


SOURCE_FILES = frozenset({
    "SOURCES.md", "aom-src-v3.13.1.tar.xz", "ffmpeg-src-n4.4.6.tar.xz", "libvpx-src-v1.15.2.tar.xz",
    "license.txt", "opencv-videoio-ffmpeg.tar.xz", "opencv_ffmpeg.tar.xz",
    "openh264-api-headers-v1.8.0.tar.xz", "readme.txt",
})


def stage_opencv_sources(root: Path, target_site: Path, stage: Path, version: str, copy_file):
    spec_path = root / "scripts" / "opencv-ffmpeg-runtime.json"
    if not spec_path.is_file():
        raise ValueError("reviewed OpenCV FFmpeg source specification is missing")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    binary = spec.get("binary") or {}
    if (spec.get("kind") != "zhiji-opencv-ffmpeg-sources" or version != "4.13.0.90" or
            spec.get("packageVersion") != version or binary.get("name") != "opencv_videoio_ffmpeg4130_64.dll" or
            spec.get("binaryCommit") != "d82ad9a54a7b42a1648a9cae8fed5c2f20ea396c" or
            spec.get("sourceCommit") != "62bd53c24feadd92af0146462317fcd4d64fd842"):
        raise ValueError("unreviewed OpenCV FFmpeg binary/source version")
    dll = target_site / "cv2" / binary["name"]
    if (not dll.is_file() or dll.is_symlink() or dll.stat().st_size != binary.get("bytes") or
            file_sha256(dll) != binary.get("sha256")):
        raise ValueError("OpenCV FFmpeg DLL differs from its reviewed source bundle")
    rows = spec.get("files")
    if not isinstance(rows, list) or len(rows) != len(SOURCE_FILES):
        raise ValueError("OpenCV FFmpeg source closure differs")
    expected = {}
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        if name not in SOURCE_FILES or name in expected:
            raise ValueError("unsafe or duplicate OpenCV FFmpeg source path")
        if (type(row.get("bytes")) is not int or row["bytes"] <= 0 or
                not isinstance(row.get("sha256"), str) or len(row["sha256"]) != 64):
            raise ValueError("invalid OpenCV FFmpeg source metadata")
        expected[name] = row
    base = root / ".local" / "tools" / "opencv-ffmpeg-source" / "dist"
    for directory in (base, *base.parents):
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            raise ValueError("linked OpenCV FFmpeg source directory")
        if directory == root:
            break
    if not base.is_dir():
        raise ValueError("reviewed OpenCV FFmpeg sources are missing")
    found = list(base.iterdir())
    if (any(not path.is_file() or path.is_symlink() for path in found) or
            {path.name for path in found} != SOURCE_FILES | {"manifest.json"}):
        raise ValueError("OpenCV FFmpeg sources have unexpected or missing content")
    if json.loads((base / "manifest.json").read_text(encoding="utf-8")) != spec:
        raise ValueError("OpenCV FFmpeg source manifest differs from the reviewed specification")
    for name, row in expected.items():
        path = base / name
        if path.stat().st_size != row["bytes"] or file_sha256(path) != row["sha256"]:
            raise ValueError("OpenCV FFmpeg source hash differs")
        if row.get("gitBlobSha"):
            digest = hashlib.sha1(f"blob {row['bytes']}\0".encode())
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != row["gitBlobSha"]:
                raise ValueError("OpenCV FFmpeg source Git object differs")
    for name in sorted(SOURCE_FILES | {"manifest.json"}):
        copy_file(base / name, stage / "runtime" / "ffmpeg-opencv" / name, "python_packages")
    return {"binarySha256": binary["sha256"], "binaryCommit": spec["binaryCommit"],
            "sourceCommit": spec["sourceCommit"], "sourceFiles": len(SOURCE_FILES)}
