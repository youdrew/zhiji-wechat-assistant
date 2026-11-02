"""Replace the imageio wheel's bundled executable with the reviewed LGPL build."""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from pathlib import Path, PurePosixPath


IMAGEIO_VERSION = "0.6.0"
DEFINITIONS_SHA256 = "5206f96cb45bc27c4e314d39c03e4d3b45df17deaff3d6881e9f45702d169e32"
EXECUTABLE_NAME = "ffmpeg-zhiji-lgpl.exe"
SOURCE_FILES = {
    "ffmpeg.exe", "ffmpeg-7.1.tar.xz", "ffmpeg-7.1.tar.xz.asc", "ffmpeg-devel.asc",
    "build-ffmpeg.sh", "FFmpeg-LGPL-2.1.txt", "FFmpeg-LICENSE.md",
    "MinGW-w64-runtime-COPYING.txt", "GCC-RUNTIME-LIBRARY-EXCEPTION.txt",
    "BUILD-INFO.md", "ffmpeg-version.txt", "ffmpeg-buildconf.txt", "ffmpeg-license.txt", "GCC-GPL-3.0.txt",
}


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_ffmpeg(root: Path):
    """A checked-in manifest anchors the binary and its complete source delivery."""
    spec_path = root / "scripts" / "ffmpeg-runtime.json"
    if not spec_path.is_file():
        raise ValueError("reviewed LGPL FFmpeg specification is missing")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    configuration = spec.get("configuration", "")
    if (spec.get("kind") != "zhiji-ffmpeg-lgpl" or spec.get("version") != "7.1" or
            spec.get("license") != "LGPL-2.1-or-later" or spec.get("sourceArchive") != "ffmpeg-7.1.tar.xz" or
            not isinstance(configuration, str) or
            any(flag in configuration for flag in ("--enable-gpl", "--enable-version3", "--enable-nonfree", "--enable-lib"))):
        raise ValueError("unreviewed FFmpeg build configuration")
    rows = spec.get("files")
    if not isinstance(rows, list):
        raise ValueError("FFmpeg specification lacks its exact source/binary closure")
    expected = {}
    for row in rows:
        name = row.get("name") if isinstance(row, dict) else None
        if not isinstance(name, str) or PurePosixPath(name).name != name or "\\" in name or name in expected:
            raise ValueError("unsafe or duplicate FFmpeg distribution path")
        if (type(row.get("bytes")) is not int or row["bytes"] <= 0 or
                not isinstance(row.get("sha256"), str) or len(row["sha256"]) != 64 or
                any(char not in "0123456789abcdef" for char in row["sha256"])):
            raise ValueError("invalid FFmpeg distribution metadata")
        expected[name] = row
    if set(expected) != SOURCE_FILES:
        raise ValueError("FFmpeg source/binary/license closure differs")
    base = root / ".local" / "tools" / "ffmpeg-lgpl" / "dist"
    for directory in (base, *base.parents):
        if directory.is_symlink() or getattr(directory, "is_junction", lambda: False)():
            raise ValueError("linked FFmpeg distribution directory")
        if directory == root:
            break
    if not base.is_dir():
        raise ValueError("reviewed LGPL FFmpeg distribution is missing")
    found = list(base.iterdir())
    if any(not path.is_file() or path.is_symlink() for path in found) or {path.name for path in found} != SOURCE_FILES | {"manifest.json"}:
        raise ValueError("FFmpeg distribution has unexpected or missing content")
    if json.loads((base / "manifest.json").read_text(encoding="utf-8")) != spec:
        raise ValueError("FFmpeg build manifest differs from the reviewed specification")
    for name, row in expected.items():
        path = base / name
        if path.stat().st_size != row["bytes"] or file_sha256(path) != row["sha256"]:
            raise ValueError("FFmpeg distribution hash differs")
    return base, spec


def stage_imageio_distribution(dist, source_site: Path, target_site: Path, stage: Path, root: Path, copy_file):
    if dist.version != IMAGEIO_VERSION:
        raise ValueError("unreviewed imageio-ffmpeg wrapper version")
    base, spec = checked_ffmpeg(root)
    source_site = source_site.resolve()
    records = {}
    for record in dist.files or []:
        original = Path(dist.locate_file(record))
        if original.is_symlink():
            raise ValueError("linked imageio-ffmpeg input")
        resolved = original.resolve()
        if resolved.is_file() and resolved.is_relative_to(source_site):
            records[resolved.relative_to(source_site).as_posix()] = resolved
    metadata_root = f"imageio_ffmpeg-{IMAGEIO_VERSION}.dist-info"
    definitions = records.get("imageio_ffmpeg/_definitions.py")
    if definitions is None or file_sha256(definitions) != DEFINITIONS_SHA256:
        raise ValueError("imageio-ffmpeg platform mapping differs from the reviewed wrapper")
    mapping = '"windows-x86_64": "ffmpeg-win-x86_64-v7.1.exe"'
    text = definitions.read_text(encoding="utf-8")
    if text.count(mapping) != 1:
        raise ValueError("unreviewed imageio-ffmpeg Windows platform mapping")
    names = []
    for name, original in records.items():
        # Never copy the wheel's GPL executable or its binary README. Python
        # wrapper code and its BSD license are retained separately.
        package_source = name.startswith("imageio_ffmpeg/") and name.endswith(".py") and "__pycache__" not in name
        package_metadata = name.startswith(metadata_root + "/") and name.split("/", 1)[1] in {
            "METADATA", "WHEEL", "top_level.txt", "LICENSE", "LICENSE.txt"}
        if (package_source or package_metadata) and name != "imageio_ffmpeg/_definitions.py":
            copy_file(original, target_site / name, "python_packages")
            names.append(name)
    if not any(name.endswith(("/LICENSE", "/LICENSE.txt")) for name in names):
        raise ValueError("imageio-ffmpeg BSD license missing")
    generated = {
        "imageio_ffmpeg/_definitions.py": (
            "# Modified by Know Your Friends contributors, 2026-11-02: use the source-complete LGPL build.\n" +
            text.replace(mapping, f'"windows-x86_64": "{EXECUTABLE_NAME}"')).encode("utf-8"),
        "imageio_ffmpeg/KYF_FFMPEG.md": b"# Modified imageio-ffmpeg distribution\n\n"
            b"The original Python wrapper remains BSD-licensed. Its wheel executable is excluded.\n"
            b"The Windows mapping now selects ffmpeg-zhiji-lgpl.exe, built from FFmpeg 7.1 source\n"
            b"with no GPL, nonfree or external codec libraries. Exact source archive, build script,\n"
            b"licenses, toolchain notices and hashes are delivered in runtime/ffmpeg.\n",
    }
    for name, data in generated.items():
        destination = target_site / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        names.append(name)
    executable = "imageio_ffmpeg/binaries/" + EXECUTABLE_NAME
    copy_file(base / "ffmpeg.exe", target_site / executable, "python_packages")
    names.append(executable)
    for name in sorted(SOURCE_FILES - {"ffmpeg.exe"}):
        copy_file(base / name, stage / "runtime" / "ffmpeg" / name, "python_packages")
    manifest = {**spec, "packagedExecutable": "runtime/python/Lib/site-packages/" + executable}
    (stage / "runtime" / "ffmpeg" / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted(names):
        data = (target_site / name).read_bytes()
        checksum = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
        writer.writerow((name, "sha256=" + checksum, len(data)))
    record_name = metadata_root + "/RECORD"
    writer.writerow((record_name, "", ""))
    (target_site / record_name).write_text(output.getvalue(), encoding="utf-8", newline="")
    names.append(record_name)
    return {"name": dist.metadata["Name"], "version": dist.version, "profile": "reviewed-lgpl-ffmpeg",
            "recordedFiles": len(names), "recordedBytes": sum((target_site / name).stat().st_size for name in names)}
