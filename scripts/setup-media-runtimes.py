"""Prepare source-complete media runtimes without copying development caches.

Build FFmpeg with scripts/build-ffmpeg-lgpl.sh and the pinned source/toolchain
first, then pass its executable and original source archive. All other FFmpeg
materials are small reviewed repository resources. OpenCV sources download from
immutable upstream URLs recorded in the checked-in specification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlparse
from urllib.request import urlopen

from ffmpeg_dependency import SOURCE_FILES as FFMPEG_FILES, file_sha256
from opencv_dependency import SOURCE_FILES as OPENCV_FILES


ROOT = Path(__file__).resolve().parents[1]


def verify_file(path, row):
    if (not path.is_file() or path.is_symlink() or path.stat().st_size != row["bytes"] or
            file_sha256(path) != row["sha256"]):
        raise ValueError("media runtime input does not match its reviewed hash")
    if row.get("gitBlobSha"):
        digest = hashlib.sha1(f"blob {row['bytes']}\0".encode())
        with path.open("rb") as stream:
            for data in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(data)
        if digest.hexdigest() != row["gitBlobSha"]:
            raise ValueError("media source Git object does not match")


def checked_existing(target, spec, names):
    if not target.exists():
        return False
    if target.is_symlink() or getattr(target, "is_junction", lambda: False)() or not target.is_dir():
        raise ValueError("linked or invalid media runtime directory")
    found = list(target.iterdir())
    if (target.is_symlink() or any(not path.is_file() or path.is_symlink() for path in found) or
            {path.name for path in found} != set(names) | {"manifest.json"} or
            json.loads((target / "manifest.json").read_text(encoding="utf-8")) != spec):
        raise ValueError("existing media runtime differs; refusing to overwrite it")
    for row in spec["files"]:
        verify_file(target / row["name"], row)
    return True


def prepare_bundle(root, directory, specification, names, resource_directory, inputs=None):
    spec = json.loads((root / "scripts" / specification).read_text(encoding="utf-8"))
    rows = spec.get("files", [])
    if len(rows) != len(names) or {row.get("name") for row in rows} != set(names):
        raise ValueError("unreviewed media runtime file list")
    parent = root / ".local" / "tools" / directory
    for path in (parent, *parent.parents):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("linked media runtime directory")
        if path == root:
            break
    target = parent / "dist"
    if checked_existing(target, spec, names):
        return target
    parent.mkdir(parents=True, exist_ok=True)
    inputs = inputs or {}
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=parent) as temporary:
        staged = Path(temporary) / "dist"
        staged.mkdir()
        for row in rows:
            name = row["name"]
            source = inputs.get(name) or root / "scripts" / resource_directory / name
            destination = staged / name
            if source.is_file():
                verify_file(source, row)
                shutil.copy2(source, destination)
            else:
                url = row.get("url", "")
                parsed = urlparse(url)
                if (parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com" or
                        not parsed.path.startswith("/opencv/opencv_3rdparty/" + spec.get("sourceCommit", "") + "/")):
                    raise ValueError("provide the pinned compiled FFmpeg executable and source archive")
                total = 0
                with urlopen(url, timeout=90) as response, destination.open("xb") as output:
                    while data := response.read(1024 * 1024):
                        total += len(data)
                        if total > row["bytes"]:
                            raise ValueError("media source download exceeds its reviewed size")
                        output.write(data)
                verify_file(destination, row)
        (staged / "manifest.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        # Both paths are freshly created inside this checkout's tools directory.
        tools = (root / ".local" / "tools").resolve()
        if not staged.resolve().is_relative_to(tools) or not target.resolve().is_relative_to(tools):
            raise ValueError("media runtime destination escaped the checkout")
        if target.exists():
            raise ValueError("media runtime appeared concurrently; refusing to replace it")
        staged.rename(target)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ffmpeg-exe", type=Path, help="FFmpeg executable built with the pinned recipe/toolchain")
    parser.add_argument("--ffmpeg-source", type=Path, help="original ffmpeg-7.1.tar.xz archive")
    args = parser.parse_args(argv)
    inputs = {}
    if args.ffmpeg_exe:
        inputs["ffmpeg.exe"] = args.ffmpeg_exe
    if args.ffmpeg_source:
        inputs["ffmpeg-7.1.tar.xz"] = args.ffmpeg_source
    try:
        prepare_bundle(ROOT, "ffmpeg-lgpl", "ffmpeg-runtime.json", FFMPEG_FILES,
                       "ffmpeg-runtime-resources", inputs)
        prepare_bundle(ROOT, "opencv-ffmpeg-source", "opencv-ffmpeg-runtime.json", OPENCV_FILES,
                       "opencv-ffmpeg-runtime-resources")
    except (OSError, ValueError) as error:
        parser.exit(1, f"Media runtime preparation rejected: {error}\n")
    print("Reviewed FFmpeg binary/source closure and OpenCV corresponding sources are ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
