"""Core-owned model scheduling and opt-in search settings.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import copy
from pathlib import Path
from urllib.parse import urlsplit

from core_plugin_settings import PluginSettingsStore, SettingsError, validate_values


FIELDS = [
    {"key": "searchSource", "title": "外部搜索源", "type": "select", "default": "none",
     "options": [{"value": "none", "label": "不联网"}, {"value": "searxng", "label": "SearXNG"}]},
    {"key": "searxngUrl", "title": "SearXNG 地址", "type": "string",
     "default": "http://127.0.0.1:8080", "maxLength": 512},
    {"key": "searchOpenPages", "title": "允许打开搜索结果网页", "type": "boolean", "default": True},
]
SCHEMA = {"schema": 1, "groups": [{"id": "model", "title": "模型设置", "fields": FIELDS}]}
DEFAULTS = {field["key"]: field["default"] for field in FIELDS}


def validate_model_settings(values):
    if not isinstance(values, dict):
        raise SettingsError(400, "model-settings-invalid", "请提供完整的模型调度与联网设置")
    # 旧文档的 maxConcurrentTasks 已改为每模型并发；读取和保存都忽略这个字段。
    cleaned = {key: value for key, value in values.items() if key in DEFAULTS}
    if set(cleaned) != set(DEFAULTS):
        raise SettingsError(400, "model-settings-invalid", "请提供完整的模型调度与联网设置")
    result = validate_values(SCHEMA, cleaned)
    result["searxngUrl"] = result["searxngUrl"].strip()
    if result["searchSource"] == "searxng":
        url = result["searxngUrl"]
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in ("http", "https") and parsed.hostname and
                     parsed.username is None and parsed.password is None and
                     not parsed.query and not parsed.fragment and parsed.port != 0 and
                     not any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char == "\\" for char in url))
        except ValueError:
            valid = False
        if not valid:
            message = "请在模型设置中填写有效的 SearXNG http(s) 网址（不含用户名、密码、查询参数或片段）"
            raise SettingsError(422, "model-settings-invalid-url", message,
                                {"fields": {"searxngUrl": message}})
    return result


class ModelSettingsStore:
    """One versioned local document, independent of model credentials and plugins.

    Reuse Core's atomic settings persistence and path checks. Legacy plugin files
    are projected once before being retired; they are never modified here.
    """

    def __init__(self, local):
        self.local = Path(local)
        self.store = PluginSettingsStore(self.local / "core")
        self.legacy = PluginSettingsStore(self.local / "plugin-data")

    def _legacy_values(self):
        values = copy.deepcopy(DEFAULTS)
        # 旧的统一调度总额（maxConcurrentTasks）不再在这里消费：迁移主模型时
        # 由 backend_service 直接读旧文档折算成该模型的并发。
        schema = {"schema": 1, "groups": [{"fields": FIELDS}]}
        try:
            revision, saved = self.legacy.read("kyf.advisor", schema)
        except SettingsError:
            # A corrupt old plugin document never changes core defaults or
            # gets overwritten. Its own settings page still reports it.
            return values
        if revision:
            values.update(saved)
        return values

    def _read(self):
        revision, values = self.store.read("model", SCHEMA)
        if revision == 0:
            revision, values = self.store.save("model", SCHEMA, 0, self._legacy_values())
        return {"revision": revision, "values": values}

    @staticmethod
    def _error(exc):
        return SettingsError(exc.status, exc.code, exc.message.replace("插件设置", "模型设置"), exc.detail)

    def read(self):
        with self.store.lock:
            try:
                return self._read()
            except SettingsError as exc:
                raise self._error(exc) from None

    def save(self, revision, values):
        validated = validate_model_settings(values)
        with self.store.lock:
            try:
                self._read()
                revision, saved = self.store.save("model", SCHEMA, revision, validated)
                return {"revision": revision, "values": saved}
            except SettingsError as exc:
                raise self._error(exc) from None
