"""Bounded import previews of Skill packages; no model or chat access.

Moved from bridge/advisor_imports.py into the kyf.advisor plugin, 2026-10-13 (Apache-2.0): a local package is
the file or folder the user picked in the host's dialog, handed over as a one-time Core file grant
(``POST /plugin-api/v1/files/<grant>/claim``) instead of a grant file under ``.local/advisor``.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import OrderedDict

from contracts import AdvisorError
from packages import parse_package

TOKEN = re.compile(r"imp_[a-f0-9]{32}\Z")
GRANT = re.compile(r"fg_[a-f0-9]{32}\Z")
TTL_SECONDS = 900
MAX_PREVIEWS = 16


class PackagePreviews:
    def __init__(self, claim):
        """``claim(grant)`` returns ``{path, label, kind}`` of a host-picked file once, or raises."""
        self.claim = claim
        self.lock = threading.RLock()
        self.items = OrderedDict()
        self.active = 0
        self.closed = False

    def _prune(self):
        now = time.monotonic()
        for key, item in list(self.items.items()):
            if now - item["created"] > TTL_SECONDS:
                item["cancel"].set()
                self.items.pop(key, None)

    def _local(self, grant):
        try:
            picked = self.claim(grant)
        except Exception as exc:
            raise AdvisorError("invalid-request", "导入凭证无效，请重新选择文件") from exc
        path = picked.get("path") if isinstance(picked, dict) else None
        if not isinstance(path, str) or not path:
            raise AdvisorError("invalid-request", "导入凭证无效，请重新选择文件")
        return parse_package({"kind": "local", "path": path}), picked.get("label") or "本地技能包"

    def preview(self, source):
        if not isinstance(source, dict):
            raise AdvisorError("invalid-request", "导入来源不正确")
        with self.lock:
            self._prune()
            if self.closed or self.active + len(self.items) >= MAX_PREVIEWS:
                raise AdvisorError("config-full", "导入任务过多，请稍后重试")
            self.active += 1
        try:
            if source.get("kind") == "local":
                grant = source.get("grant")
                if set(source) != {"kind", "grant"} or not isinstance(grant, str) or not GRANT.fullmatch(grant):
                    raise AdvisorError("invalid-request", "请通过文件选择器选择技能包")
                package, label = self._local(grant)
                label = package.get("source", {}).get("label") or label
            elif source.get("kind") == "github" and set(source) == {"kind", "url"}:
                package = parse_package(source)
                label = source["url"]
            else:
                raise AdvisorError("invalid-request", "导入来源不正确")
            identifier = "imp_" + secrets.token_hex(16)
            with self.lock:
                if self.closed:
                    raise AdvisorError("account-closed", "导入已取消")
                self.items[identifier] = {"created": time.monotonic(), "package": package, "cancel": threading.Event()}
            entry = package["entry"]
            return {"id": identifier, "name": entry["name"], "description": entry["description"],
                    "defaultPrompt": entry["defaultPrompt"], "welcome": "嗨，我是你的" + entry["name"],
                    "sourceLabel": label, "resourceCount": len(package["resources"]),
                    "compatibility": package["compatibility"]}
        finally:
            with self.lock:
                self.active -= 1

    def get(self, identifier):
        if not isinstance(identifier, str) or not TOKEN.fullmatch(identifier):
            raise AdvisorError("invalid-request", "导入预览编号不正确")
        with self.lock:
            self._prune()
            item = self.items.get(identifier)
            if self.closed or item is None:
                raise AdvisorError("invalid-request", "导入预览已过期，请重新读取")
            return item

    def cancel(self, identifier):
        if not isinstance(identifier, str) or not TOKEN.fullmatch(identifier):
            raise AdvisorError("invalid-request", "导入预览编号不正确")
        with self.lock:
            item = self.items.pop(identifier, None)
            if item is not None:
                item["cancel"].set()
        return {"cancelled": True}

    def close(self):
        with self.lock:
            self.closed = True
            for item in self.items.values():
                item["cancel"].set()
            self.items.clear()
