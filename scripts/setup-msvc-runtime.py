"""Prepare the pinned app-local CRT from licensed VS2022 release files only.

No installer is run and no system directory is modified. Use --source-dir for
an official extracted CRT directory; otherwise locate the installed VS2022
Community redist. Exact hashes bind the files to the verified official package.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

import msvc_runtime as msvc

ROOT = Path(__file__).resolve().parents[1]


def matches(folder, rows):
    try:
        for row in rows:
            if row["name"] in msvc.CRT_FILES:
                candidates = [folder / row["name"], folder / (row["name"] + "_amd64")]
                source = next((p for p in candidates if p.is_file()), candidates[0])
                msvc.check_file(source, row)
        return True
    except ValueError:
        return False


def prepare(root, source=None):
    root = Path(root).resolve()
    spec = msvc.specification(root)
    parent = root / ".local/tools/msvc-runtime"
    for path in (parent, *parent.parents):
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("linked Microsoft runtime destination")
        if path == root:
            break
    destination = parent / "dist"
    if destination.exists():
        msvc.checked_runtime(root)
        return destination
    if source is None:
        vswhere = Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft Visual Studio/Installer/vswhere.exe"
        result = subprocess.run([str(vswhere), "-products", "Microsoft.VisualStudio.Product.Community",
                                 "-version", "[17.0,18.0)", "-property", "installationPath"],
                                capture_output=True, text=True, check=True, timeout=30)
        folders = [folder for line in result.stdout.splitlines()
                   for folder in Path(line).glob("VC/Redist/MSVC/*/x64/Microsoft.VC143.CRT")]
        source = next((folder for folder in folders if matches(folder, spec["files"])), None)
    if source is None:
        raise ValueError("provide licensed VS2022 x64 release CRT files matching scripts/msvc-runtime.json")
    source = Path(source).resolve()
    if {part.lower() for part in source.parts} & {"system32", "syswow64", "winsxs"}:
        raise ValueError("system DLL directories are not a runtime redistribution source")
    if not matches(source, spec["files"]):
        raise ValueError("CRT source differs from the pinned VS2022 release")
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="prepare-", dir=parent) as temporary:
        staged = Path(temporary) / "dist"
        staged.mkdir()
        for row in spec["files"]:
            if row["name"] in msvc.CRT_FILES:
                original = source / row["name"]
                if not original.is_file():
                    original = source / (row["name"] + "_amd64")
            else:
                original = root / "licenses" / row["name"]
            msvc.check_file(original, row)
            shutil.copy2(original, staged / row["name"])
        (staged / "manifest.json").write_text(json.dumps(spec, indent=2) + "\n", encoding="utf-8")
        if not staged.resolve().is_relative_to(parent.resolve()) or not destination.resolve().is_relative_to(parent.resolve()):
            raise ValueError("CRT destination escaped the local tools directory")
        if destination.exists():
            raise ValueError("CRT destination appeared concurrently; refusing to overwrite")
        staged.rename(destination)
    msvc.checked_runtime(root)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path)
    args = parser.parse_args()
    try:
        prepare(ROOT, args.source_dir)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, "Microsoft runtime preparation rejected: " + str(error) + "\n")
    print("Pinned Microsoft app-local runtime verified; no system installation performed.")


if __name__ == "__main__":
    main()
