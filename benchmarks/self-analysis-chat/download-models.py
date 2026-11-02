"""Download only the benchmark's pinned public ModelScope assets; no chat access.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
This opt-in preparation tool is separate from the offline encoder/benchmark.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


def allowed(url, hosts):
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.hostname not in hosts or
            parts.username or parts.password or parts.port not in (None, 443)):
        raise ValueError("unapproved asset source")


class DomesticRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts):
        self.hosts = hosts

    def redirect_request(self, request, fp, code, message, headers, url):
        allowed(url, self.hosts)
        return super().redirect_request(request, fp, code, message, headers, url)


def verified(path, item):
    if not path.is_file() or path.is_symlink() or path.stat().st_size != item["bytes"]:
        return False
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest() == item["sha256"]


def download(opener, target, item, hosts, model):
    if verified(target, item):
        print(json.dumps({"model": model, "file": target.name, "state": "verified", "bytes": item["bytes"]}), flush=True)
        return
    partial = target.with_name(target.name + ".part")
    if target.is_symlink() or partial.is_symlink():
        raise ValueError("asset file cannot be a link")
    allowed(item["url"], hosts)
    for attempt in range(3):
        try:
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > item["bytes"]:
                partial.unlink()
                offset = 0
            if offset < item["bytes"]:
                headers = {"User-Agent": "KnowYourFriends-BenchmarkAssets/1"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                request = urllib.request.Request(item["url"], headers=headers)
                with opener.open(request, timeout=45) as response:
                    allowed(response.geturl(), hosts)
                    if response.status not in (200, 206):
                        raise ValueError("asset HTTP status")
                    if offset and response.status != 206:
                        offset = 0
                    if response.status == 206 and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                        raise ValueError("asset range mismatch")
                    progress = time.monotonic()
                    with partial.open("ab" if offset else "wb") as stream:
                        while True:
                            block = response.read(1024 * 1024)
                            if not block:
                                break
                            offset += len(block)
                            if offset > item["bytes"]:
                                raise ValueError("asset too large")
                            stream.write(block)
                            if time.monotonic() - progress > 5:
                                progress = time.monotonic()
                                print(json.dumps({"model": model, "file": target.name,
                                    "downloaded": offset, "total": item["bytes"]}), flush=True)
                        stream.flush()
                        os.fsync(stream.fileno())
            if not verified(partial, item):
                if partial.exists() and partial.stat().st_size >= item["bytes"]:
                    partial.unlink()
                raise ValueError("asset size/SHA256 mismatch")
            os.replace(partial, target)
            print(json.dumps({"model": model, "file": target.name, "state": "verified", "bytes": item["bytes"]}), flush=True)
            return
        except Exception:
            if attempt == 2:
                raise RuntimeError(f"pinned asset failed: {model}/{target.name}; retry preserves partial bytes") from None
            time.sleep(attempt + 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="*", help="model keys, default all")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--cache-root", type=Path, help="optional override; files go into <cache-root>/<model-key>")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    manifest = json.loads(Path(__file__).with_name("models.json").read_text(encoding="utf-8"))
    rows = {row["key"]: row for row in manifest["models"]}
    names = args.models if args.models is not None else list(rows)
    if any(name not in rows for name in names):
        parser.error("unknown model key")
    hosts = set(manifest["allowedHosts"])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), DomesticRedirects(hosts))
    for name in names:
        row = rows[name]
        directory = args.cache_root / name if args.cache_root else args.root / row["cacheDir"]
        for item in row["files"]:
            if Path(item["name"]).name != item["name"] or "/" in item["name"] or "\\" in item["name"]:
                raise ValueError("invalid asset filename")
        if args.verify_only:
            missing = [item["name"] for item in row["files"] if not verified(directory / item["name"], item)]
            print(json.dumps({"model": name, "ready": not missing, "invalid": missing}), flush=True)
            if missing:
                return 1
            continue
        directory.mkdir(parents=True, exist_ok=True)
        if directory.is_symlink() or directory.is_junction():
            raise ValueError("asset directory cannot be a link")
        required = sum(item["bytes"] for item in row["files"] if not verified(directory / item["name"], item))
        if shutil.disk_usage(directory).free < required + 64 * 1024 * 1024:
            raise RuntimeError("insufficient model-cache disk space")
        for item in row["files"]:
            download(opener, directory / item["name"], item, hosts, name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
