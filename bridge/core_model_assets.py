"""Reviewed domestic model assets, resumable downloads and hash verification.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import threading
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

MODELSCOPE_HOSTS = frozenset({"modelscope.cn", "www.modelscope.cn", "cdn-lfs-cn-1.modelscope.cn", "cdn-lfs-cn-2.modelscope.cn"})

class ModelAssetError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message

class _Redirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts, *, error_type=ModelAssetError, code_prefix="model"):
        self.hosts, self.error_type, self.code_prefix = hosts, error_type, code_prefix

    def redirect_request(self, request, fp, code, message, headers, new_url):
        parsed = urlsplit(new_url)
        if parsed.scheme != "https" or parsed.hostname not in self.hosts or parsed.username or parsed.password:
            raise self.error_type(self.code_prefix + "-download", "模型下载跳转超出已审核的国内来源")
        return super().redirect_request(request, fp, code, message, headers, new_url)

class ModelAssets:
    def __init__(self, root, directory, gateway=None, *, manifest_path=None, opener=None,
                 manifest_name="model.json", label="模型", code_prefix="model", error_type=ModelAssetError):
        self.label, self.code_prefix, self.error_type = label, code_prefix, error_type
        self.root, self.directory, self.gateway = Path(root), Path(directory), gateway
        self.manifest_path = Path(manifest_path) if manifest_path else self.root / "scripts" / manifest_name
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.thread = None
        self.response = None
        self.closed = False
        self.state, self.error, self.downloaded = "missing", None, 0
        self._verified = {}
        self.manifest = self._manifest()
        self.opener = opener or urllib.request.build_opener(_Redirects(set(self.manifest["allowedHosts"]), error_type=self.error_type, code_prefix=self.code_prefix))

    def _manifest(self):
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if (value.get("schema") != 1 or not isinstance(value.get("modelId"), str) or
                    not re.fullmatch(r"[a-zA-Z0-9._/-]{1,128}", value["modelId"]) or
                    not isinstance(value.get("revision"), str) or not 1 <= len(value["revision"]) <= 128):
                raise ValueError()
            hosts = value["allowedHosts"]
            if not isinstance(hosts, list) or not hosts or len(hosts) > 16 or any(
                    not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]{1,253}", host) or host not in MODELSCOPE_HOSTS for host in hosts):
                raise ValueError()
            files = value["files"]
            if not isinstance(files, list) or not 1 <= len(files) <= 16:
                raise ValueError()
            names = set()
            for item in files:
                name = item["name"]
                parsed = urlsplit(item["url"])
                if (not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}", name) or
                        name in names or type(item["bytes"]) is not int or not 1 <= item["bytes"] <= 1024**3 or
                        not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) or
                        parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password):
                    raise ValueError()
                names.add(name)
            if sum(item["bytes"] for item in files) > 2 * 1024**3:
                raise ValueError()
            return value
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise self.error_type(self.code_prefix + "-manifest", f"内置{self.label}清单不可用，请更新软件") from None

    def _path(self, name):
        path = self.directory / name
        for current in [self.directory, *self.directory.parents[:2], path]:
            if current.is_symlink() or current.is_junction():
                raise self.error_type(self.code_prefix + "-storage", f"{self.label}目录不能是链接")
        return path

    def _valid(self, item):
        path = self._path(item["name"])
        try:
            before = path.stat()
            signature = (before.st_size, before.st_mtime_ns, before.st_ino)
            if signature[0] != item["bytes"]:
                return False
            cached = self._verified.get(item["name"])
            if cached and cached[0] == signature:
                return cached[1]
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            after = path.stat()
            valid = digest == item["sha256"] and (after.st_size, after.st_mtime_ns, after.st_ino) == signature
            self._verified[item["name"]] = (signature, valid)
            return valid
        except FileNotFoundError:
            return False

    def status(self):
        with self.lock:
            if self.state not in ("downloading", "verifying"):
                try:
                    verified = [item for item in self.manifest["files"] if self._valid(item)]
                    self.downloaded = sum(item["bytes"] for item in verified)
                    verified_names = {item["name"] for item in verified}
                    for item in self.manifest["files"]:
                        partial = self._path(item["name"] + ".part")
                        if item["name"] not in verified_names and partial.exists():
                            self.downloaded += min(item["bytes"], partial.stat().st_size)
                    if len(verified) == len(self.manifest["files"]):
                        self.state, self.error = "ready", None
                    elif self.state != "error":
                        self.state = "missing"
                except (OSError, ModelAssetError):
                    self.state, self.error = "error", f"{self.label}文件暂不可用"
            result = {"state": self.state, "ready": self.state == "ready" and not self.closed,
                "modelId": self.manifest["modelId"], "revision": self.manifest["revision"],
                "totalBytes": sum(item["bytes"] for item in self.manifest["files"]),
                "downloadedBytes": self.downloaded}
            if self.error:
                result["error"] = self.error
            return result

    def prepare(self):
        with self.lock:
            if self.closed:
                raise self.error_type(self.code_prefix + "-closed", "应用正在关闭")
            current = self.status()
            if current["ready"] or self.thread and self.thread.is_alive():
                return current
            self.cancel.clear()
            self.state, self.error = "downloading", None
            self.thread = threading.Thread(target=self._download, name=f"{self.code_prefix}-assets", daemon=True)
            self.thread.start()
            return self.status()

    def _download(self):
        try:
            self._path("check")
            self.directory.mkdir(parents=True, exist_ok=True)
            completed = 0
            for item in self.manifest["files"]:
                if self.cancel.is_set():
                    return
                if not self._valid(item):
                    self._download_file(item, completed)
                completed += item["bytes"]
                with self.lock:
                    self.downloaded = completed
            with self.lock:
                if not self.cancel.is_set():
                    self.state, self.error = "ready", None
        except Exception:
            with self.lock:
                if not self.closed:
                    self.state, self.error = "error", f"国内{self.label}资源准备失败，可重试；已保留下载进度"
        finally:
            with self.lock:
                self.response = None
                if self.cancel.is_set() and self.state in ("downloading", "verifying", "error"):
                    self.state, self.error = "missing", None

    def _download_file(self, item, completed):
        target = self._path(item["name"])
        partial = self._path(item["name"] + ".part")
        offset = partial.stat().st_size if partial.exists() else 0
        if offset > item["bytes"]:
            partial.unlink()
            offset = 0
        if offset < item["bytes"]:
            request = urllib.request.Request(item["url"], headers={"User-Agent": "KnowYourFriends-ModelAssets/1",
                **({"Range": f"bytes={offset}-"} if offset else {})})
            with self.opener.open(request, timeout=30) as response:
                if self.cancel.is_set():
                    return
                with self.lock:
                    self.response = response
                final_url = urlsplit(response.geturl())
                if final_url.scheme != "https" or final_url.hostname not in self.manifest["allowedHosts"] or final_url.username or final_url.password:
                    raise self.error_type(self.code_prefix + "-download", "下载来源不匹配")
                if offset and response.status != 206:
                    offset = 0  # Server ignored Range: replace the partial, never append a full file.
                if response.status == 206 and not response.headers.get("Content-Range", "").startswith(f"bytes {offset}-"):
                    raise self.error_type(self.code_prefix + "-download", "下载范围不匹配")
                if self.cancel.is_set():
                    return
                with partial.open("ab" if offset else "wb") as stream:
                    while not self.cancel.is_set():
                        block = response.read(256 * 1024)
                        if not block:
                            break
                        offset += len(block)
                        if offset > item["bytes"]:
                            raise self.error_type(self.code_prefix + "-integrity", "模型资源大小不匹配")
                        stream.write(block)
                        with self.lock:
                            self.downloaded = completed + offset
                    stream.flush()
                    os.fsync(stream.fileno())
        if self.cancel.is_set():
            return
        with self.lock:
            self.state = "verifying"
        with partial.open("rb") as stream:
            valid = partial.stat().st_size == item["bytes"] and hashlib.file_digest(stream, "sha256").hexdigest() == item["sha256"]
        if not valid:
            if partial.stat().st_size >= item["bytes"]:
                partial.unlink()
            raise self.error_type(self.code_prefix + "-integrity", "模型资源校验失败")
        if self.cancel.is_set():
            return
        os.replace(partial, target)
        self._verified.pop(item["name"], None)
        with self.lock:
            self.state = "downloading"

    def cancel_download(self):
        with self.lock:
            self.cancel.set()
            response, thread = self.response, self.thread
        if response is not None:
            # HTTPResponse.close can wait for another thread's buffered read.
            # Keep application shutdown bounded even on a stalled connection.
            def close_response():
                try:
                    response.close()
                except (OSError, ValueError):
                    pass
            threading.Thread(target=close_response, name=f"{self.code_prefix}-download-close", daemon=True).start()
        if thread is not None:
            thread.join(2)

    def close(self):
        with self.lock:
            self.closed = True
        self.cancel_download()
