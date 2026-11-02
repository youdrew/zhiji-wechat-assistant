"""Start a Python plugin entry with the plugin SDK importable.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Core runs ``python -B launch.py <entry>``, adding ``-s`` for the bundled Python so
it never loads the user's own packages. The bundled Python ignores PYTHONPATH,
so the entry's folder and the SDK are put on ``sys.path`` here.
"""
import runpy
import sys
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        raise SystemExit("usage: launch.py <plugin entry> [args...]")
    entry = Path(sys.argv[1]).resolve()
    sdk = Path(__file__).resolve().parent
    for path in (str(sdk), str(entry.parent)):
        if path not in sys.path:
            sys.path.insert(0, path)
    sys.argv = [str(entry), *sys.argv[2:]]
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
