"""Bounded import previews and native-picker grants; no model or chat access."""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

from advisor_contracts import AdvisorError
from advisor_packages import parse_package, validate_package
from advisor_store import _checked_root, _ensure_directory, _reparse, _write_json_atomic

TOKEN = re.compile(r"imp_[a-f0-9]{32}\Z")
TTL_SECONDS = 900
MAX_PREVIEWS = 16
MAX_GRANT_BYTES = 4 * 1024 * 1024


def issue_local_grant(root, selected_path):
    root = _checked_root(root)
    package = parse_package({"kind": "local", "path": str(selected_path)})
    folder = _ensure_directory(root, root / ".local/advisor/import-grants")
    for item in folder.glob("imp_*.json"):
        if _reparse(item) or not item.is_file():
            raise AdvisorError("invalid-request", "导入目录不安全")
        if time.time() - item.stat().st_mtime > TTL_SECONDS:
            item.unlink()
    if len(list(folder.glob("imp_*.json"))) >= MAX_PREVIEWS:
        raise AdvisorError("config-full", "导入任务过多，请稍后重试")
    token = "imp_" + secrets.token_hex(16)
    _write_json_atomic(folder / (token + ".json"), {"created": time.time(), "package": package})
    return {"token": token, "label": Path(selected_path).name}


class PackagePreviews:
    def __init__(self, root):
        self.root = _checked_root(root)
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
                token = source.get("token")
                if set(source) != {"kind", "token"} or not isinstance(token, str) or not TOKEN.fullmatch(token):
                    raise AdvisorError("invalid-request", "请通过文件选择器选择技能包")
                path = self.root / ".local/advisor/import-grants" / (token + ".json")
                _ensure_directory(self.root, path, leaf_file=True)
                if not path.is_file() or _reparse(path) or path.stat().st_size > MAX_GRANT_BYTES:
                    raise AdvisorError("invalid-request", "导入凭证无效，请重新选择文件")
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    if (not isinstance(value, dict) or type(value.get("created")) not in (int, float) or
                            not 0 <= time.time() - value["created"] <= TTL_SECONDS):
                        raise ValueError("expired")
                    package = validate_package(value["package"])
                except (KeyError, ValueError, OSError) as exc:
                    raise AdvisorError("invalid-request", "导入凭证无效，请重新选择文件") from exc
                label = package.get("source", {}).get("label", "本地技能包")
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
