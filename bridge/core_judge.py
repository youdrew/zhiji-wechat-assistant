"""Core-owned local judge resources and conservative synthetic self-test.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import shutil
import sys
import threading
from pathlib import Path

from core_model_assets import ModelAssets, ModelAssetError

BUILTIN = "julia-1"


class JudgeService:
    def __init__(self, root, directory, gateway, store, changed=lambda: None, *,
                 manifest_path=None, opener=None, supported=None):
        self.root, self.directory = Path(root), Path(directory)
        self.gateway, self.store, self.changed = gateway, store, changed
        self.supported = (sys.platform == "win32" and platform.machine().lower() in ("amd64", "x86_64")) \
            if supported is None else supported
        self.assets = ModelAssets(root, directory, gateway, manifest_path=manifest_path, opener=opener,
            manifest_name="judge-model.json", label="判断模型", code_prefix="judge")
        self.lock = threading.RLock()
        self.cancelled = threading.Event()
        self.worker = None
        self.closed = False
        self.deleting = False
        self.phase, self.error = None, None
        self.result = self._read_result()

    @staticmethod
    def unprepared_status(root, store):
        """A settings read must not instantiate the plugin host just to show a download button."""
        # A read-only resource view restores ready/progress from verified local
        # bytes and the synthetic self-test record without constructing Core or
        # starting a process. The gateway is only needed by explicit actions.
        return JudgeService(root, Path(root) / ".local" / "core" / "judge" / "julia-1", None, store).status()

    def _read_result(self):
        try:
            value = json.loads(self.assets._path("selftest-result.json").read_text(encoding="utf-8"))
            latency = value.get("latencyMs")
            if (value.get("revision") != self.assets.manifest["revision"] or
                    value.get("sha256") != self.assets.manifest["files"][0]["sha256"] or
                    value.get("formatValid") is not True or type(latency) not in (float, int) or
                    not math.isfinite(latency) or latency < 0):
                return None
            return value
        except (OSError, ValueError, TypeError, AttributeError, ModelAssetError):
            return None

    def _entry_id(self):
        return next((entry["id"] for entry in self.store.models()["models"]
                     if entry.get("builtin") == BUILTIN), None)

    def status(self):
        with self.lock:
            asset = self.assets.status()
            entry_id = self._entry_id()
            ready = bool(self.supported and asset["ready"] and self.result and entry_id and not self.closed)
            latency = self.result["latencyMs"] if ready else None
            state = self.phase or ("error" if self.error else "slow" if ready and latency > 2000 else "ready" if ready else
                                  asset["state"] if asset["state"] in ("downloading", "verifying") else
                                  "error" if asset["state"] == "error" or self.error else "missing")
            ready = ready and state in ("ready", "slow")
            result = {"supported": self.supported, "state": state, "ready": ready,
                "modelId": asset["modelId"], "modelEntryId": entry_id,
                "totalBytes": asset["totalBytes"], "downloadedBytes": asset["downloadedBytes"],
                "latencyMs": latency, "qualityVerified": False,
                "backgroundOnly": bool(latency is not None and latency > 500)}
            error = self.error or asset.get("error")
            if error:
                result["error"] = error
            return result

    def local_config(self):
        import psutil
        packaged = self.root / "runtime" / "judge" / "llama-server.exe"
        runtime = packaged if packaged.is_file() else self.root / ".local" / "tools" / "judge-runtime" / "b12032" / "llama-server.exe"
        return {"modelPath": str(self.assets._path(self.assets.manifest["files"][0]["name"]).absolute()),
            "runtimePath": str(runtime.absolute()), "contextTokens": 8192,
            "threads": max(1, (psutil.cpu_count(logical=False) or 1) // 2), "idleMs": 300_000}

    def service(self, entry, roles):
        if entry.get("builtin") != BUILTIN or roles != ["judge"] or not self.status()["ready"]:
            return None
        manifest = self.assets.manifest
        fingerprint = hashlib.sha256(json.dumps(["local-judge-v1", manifest["modelId"],
            manifest["revision"], manifest["files"][0]["sha256"]], separators=(",", ":")).encode()).hexdigest()
        return {"id": entry["id"], "protocol": "systemone", "baseUrl": "http://127.0.0.1",
            "model": BUILTIN, "contextTokens": 8192, "concurrency": 1, "timeoutMs": 60_000,
            "roles": ["judge"], "stamp": fingerprint, "fingerprint": fingerprint,
            "local": self.local_config()}

    def _check(self):
        if self.cancelled.is_set() or self.closed:
            raise ModelAssetError("judge-cancelled", "已取消")

    def _check_resources(self):
        import psutil
        if not self.supported:
            raise ModelAssetError("judge-platform", "本地判断模型仅支持 Windows x64")
        if not Path(self.local_config()["runtimePath"]).is_file():
            raise ModelAssetError("judge-runtime", "本地判断运行时未随软件提供")
        if psutil.virtual_memory().available < 2 * 1024**3:
            raise ModelAssetError("judge-memory", "可用内存不足 2 GB")
        base = self.directory
        while not base.exists():
            base = base.parent
        if shutil.disk_usage(base).free < 500 * 1024**2:
            raise ModelAssetError("judge-disk", "可用磁盘空间不足 500 MB")

    def prepare(self):
        with self.lock:
            if self.closed:
                raise ModelAssetError("judge-closed", "应用正在关闭")
            if self.deleting:
                raise ModelAssetError("judge-busy", "判断模型正在删除")
            if self.assets.thread and self.assets.thread.is_alive() and self.cancelled.is_set():
                raise ModelAssetError("judge-busy", "判断模型正在停止，请稍后重试")
            if self.worker and self.worker.is_alive():
                return self.status()
            if self.status()["ready"]:
                return self.status()
            self.cancelled.clear()
            self.phase, self.error = "checking", None
            self.worker = threading.Thread(target=self._prepare, daemon=True, name="judge-prepare")
            self.worker.start()
            return self.status()

    def _prepare(self):
        try:
            self._check_resources()
            self._check()
            with self.lock:
                self._check()
                self.phase = None
                self.assets.prepare()
            while self.assets.thread and self.assets.thread.is_alive():
                self._check()
                self.assets.thread.join(.1)
            self._check()
            if not self.assets.status()["ready"]:
                raise ModelAssetError("judge-download", "国内判断模型资源准备失败，可重试；已保留下载进度")
            with self.lock:
                self.phase = "selftest"
            result = self.gateway.local_judge("selftest", self.local_config(), timeout=120, check=self._check)
            self._check()
            latency = result.get("latencyMs")
            if (result.get("formatValid") is not True or type(latency) not in (int, float) or
                    not math.isfinite(latency) or latency < 0):
                raise ModelAssetError("judge-selftest", "判断模型自测失败")
            record = {"revision": self.assets.manifest["revision"],
                "sha256": self.assets.manifest["files"][0]["sha256"], "formatValid": True,
                "latencyMs": latency, "qualityVerified": False}
            target = self.assets._path("selftest-result.json")
            temporary = self.assets._path("selftest-result.json.tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            self._check()
            os.replace(temporary, target)
            with self.lock:
                self._check()
                self.store.ensure_builtin(BUILTIN, "本地判断模型", BUILTIN)
                self.result, self.phase, self.error = record, None, None
            # No automatic assignment: the candidate has not passed quality validation.
            self.changed()
        except Exception as exc:
            with self.lock:
                self.phase = None
                self.error = None if self.cancelled.is_set() or self.closed else (
                    exc.message if isinstance(exc, ModelAssetError) else "本地判断模型准备失败，可重试")

    def cancel(self):
        with self.lock:
            self.cancelled.set()
        self.assets.cancel_download()
        worker = self.worker
        if worker and worker is not threading.current_thread():
            worker.join(2)
        return self.status()

    def delete(self):
        with self.lock:
            if self.deleting:
                raise ModelAssetError("judge-busy", "判断模型正在删除")
            self.deleting = True
            self.phase = "checking"
        try:
            self.cancel()
            if (self.worker and self.worker.is_alive()) or (self.assets.thread and self.assets.thread.is_alive()):
                raise ModelAssetError("judge-busy", "判断模型正在停止，请稍后重试")
            # Remove the route before killing its process, then cancel admitted
            # local calls. Main-model requests and tokens are unaffected.
            entry_id = self._entry_id()
            if entry_id:
                self.store.delete_model(entry_id)
            self.changed()
            self.gateway.local_judge("stop", timeout=10)
            for item in self.assets.manifest["files"]:
                for suffix in ("", ".part"):
                    self.assets._path(item["name"] + suffix).unlink(missing_ok=True)
            for name in ("selftest-result.json", "selftest-result.json.tmp"):
                self.assets._path(name).unlink(missing_ok=True)
            self.assets._verified.clear()
            self.result, self.phase, self.error = None, None, None
        except Exception:
            with self.lock:
                self.phase, self.error = None, "判断模型删除失败，可重试"
        finally:
            with self.lock:
                self.deleting = False
        return self.status()

    def close(self):
        self.closed = True
        self.cancel()
        self.assets.close()
