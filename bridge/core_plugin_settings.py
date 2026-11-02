"""Declarative plugin settings and bounded, versioned local persistence.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from pathlib import Path

MAX_SETTINGS_BYTES = 64 * 1024
MAX_REVISION = 2**53 - 1
KEY = re.compile(r"[a-zA-Z][a-zA-Z0-9_]{0,63}\Z")
PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9.-]{1,62}[a-z0-9]\Z")
RESERVED_KEYS = {"__proto__", "constructor", "prototype"}


def definition_digest(schema):
    if schema is None:
        return None
    return hashlib.sha256(json.dumps(schema, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


class SettingsError(Exception):
    def __init__(self, status, code, message, detail=None):
        super().__init__(message)
        self.status, self.code, self.message, self.detail = status, code, message, detail


def _shape(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or \
            not value.keys() <= set(required) | set(optional):
        raise ValueError("插件设置声明结构不正确")


def _text(value, maximum, *, empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or \
            any(ord(char) < 32 for char in value):
        raise ValueError("插件设置文字不正确")
    return value.strip()


def _identifier(value):
    if not isinstance(value, str) or not KEY.fullmatch(value) or value in RESERVED_KEYS:
        raise ValueError("插件设置标识不正确")
    return value


def _scalar(value):
    return isinstance(value, str) and len(value) <= 128 and not any(ord(char) < 32 for char in value)


def _field_value(field, value):
    kind = field["type"]
    valid = False
    if kind == "boolean":
        valid = type(value) is bool
    elif kind == "integer":
        valid = type(value) is int and field.get("minimum", -MAX_REVISION) <= value <= field.get("maximum", MAX_REVISION)
    elif kind == "string":
        valid = isinstance(value, str) and len(value) <= field["maxLength"] and not any(
            ord(char) < 32 and char not in "\t\n\r" for char in value)
    elif kind == "select":
        valid = any(type(value) is type(option["value"]) and value == option["value"] for option in field["options"])
    if not valid:
        message = f"“{field['title']}”的值不正确"
        raise SettingsError(422, "settings-field-invalid", message, {"fields": {field["key"]: message}})
    return value


def validate_schema(value, nav_ids=()):
    """Normalize manifest-owned fields; raising ValueError invalidates the manifest."""
    _shape(value, ("schema", "groups"), ("description", "links"))
    if type(value["schema"]) is not int or value["schema"] != 1:
        raise ValueError("插件设置 schema 不受支持")
    groups = value["groups"]
    if not isinstance(groups, list) or len(groups) > 8:
        raise ValueError("插件设置分组不正确")
    result = {"schema": 1, "groups": []}
    if "description" in value:
        result["description"] = _text(value["description"], 500, empty=True)
    group_ids, keys = set(), set()
    for group in groups:
        _shape(group, ("id", "title", "fields"), ("description",))
        group_id = _identifier(group["id"])
        if group_id in group_ids or not isinstance(group["fields"], list) or not 1 <= len(group["fields"]) <= 32:
            raise ValueError("插件设置分组重复或字段不正确")
        group_ids.add(group_id)
        parsed = {"id": group_id, "title": _text(group["title"], 64), "fields": []}
        if "description" in group:
            parsed["description"] = _text(group["description"], 500, empty=True)
        for field in group["fields"]:
            _shape(field, ("key", "title", "type", "default"),
                   ("description", "options", "minimum", "maximum", "maxLength"))
            key, kind = _identifier(field["key"]), field["type"]
            if key in keys or len(keys) >= 64 or kind not in ("boolean", "select", "integer", "string"):
                raise ValueError("插件设置字段重复、过多或类型不正确")
            keys.add(key)
            allowed = {"select": {"options"}, "integer": {"minimum", "maximum"}, "string": {"maxLength"}}.get(kind, set())
            if (set(field) & {"options", "minimum", "maximum", "maxLength"}) - allowed:
                raise ValueError("插件设置字段参数与类型不一致")
            item = {"key": key, "title": _text(field["title"], 64), "type": kind, "default": field["default"]}
            if "description" in field:
                item["description"] = _text(field["description"], 500, empty=True)
            if kind == "integer":
                for bound in ("minimum", "maximum"):
                    if bound in field:
                        if type(field[bound]) is not int or not -MAX_REVISION <= field[bound] <= MAX_REVISION:
                            raise ValueError("插件设置数字范围不正确")
                        item[bound] = field[bound]
                if item.get("minimum", -MAX_REVISION) > item.get("maximum", MAX_REVISION):
                    raise ValueError("插件设置最小值大于最大值")
            if kind == "string":
                maximum = field.get("maxLength", 1000)
                if type(maximum) is not int or not 1 <= maximum <= 4000:
                    raise ValueError("插件设置文字长度不正确")
                item["maxLength"] = maximum
            if kind == "select":
                options = field.get("options")
                if not isinstance(options, list) or not 1 <= len(options) <= 32:
                    raise ValueError("插件设置选项不正确")
                item["options"] = []
                for option in options:
                    _shape(option, ("value", "label"))
                    if not _scalar(option["value"]) or any(type(option["value"]) is type(prior["value"]) and
                            option["value"] == prior["value"] for prior in item["options"]):
                        raise ValueError("插件设置选项值不正确或重复")
                    item["options"].append({"value": option["value"], "label": _text(option["label"], 64)})
            try:
                _field_value(item, item["default"])
            except SettingsError as exc:
                raise ValueError("插件设置默认值不符合字段约束") from exc
            parsed["fields"].append(item)
        result["groups"].append(parsed)
    links = value.get("links", [])
    if not isinstance(links, list) or len(links) > 8:
        raise ValueError("插件设置链接不正确")
    result["links"] = []
    seen_links = set()
    for link in links:
        _shape(link, ("label", "nav"))
        nav = link["nav"]
        if not isinstance(nav, str) or nav not in nav_ids or nav in seen_links:
            raise ValueError("插件设置链接必须指向本插件的导航页面且不能重复")
        seen_links.add(nav)
        result["links"].append({"label": _text(link["label"], 64), "nav": nav})
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_SETTINGS_BYTES:
        raise ValueError("插件设置声明过大")
    return result


def validate_values(schema, values):
    if not isinstance(values, dict) or any(not isinstance(key, str) for key in values):
        raise SettingsError(400, "settings-invalid", "设置值必须是对象")
    fields = {field["key"]: field for group in schema["groups"] for field in group["fields"]}
    unknown = set(values) - fields.keys()
    if unknown:
        raise SettingsError(422, "settings-unknown-field", "设置中包含插件未声明的字段",
                            {"fields": {key: "插件未声明此字段" for key in sorted(unknown)}})
    return {key: _field_value(field, values.get(key, field["default"])) for key, field in fields.items()}


def _reparse(path):
    try:
        attributes = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(attributes.st_mode) or bool(getattr(attributes, "st_file_attributes", 0) &
                                                   getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


class PluginSettingsStore:
    """Core owns one settings document per plugin; no plugin process is needed."""

    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.RLock()

    def _path(self, plugin_id):
        if not isinstance(plugin_id, str) or not PLUGIN_ID.fullmatch(plugin_id) or ".." in plugin_id:
            raise SettingsError(400, "invalid-plugin", "插件标识不正确")
        path = self.root / plugin_id / "settings.json"
        if any(_reparse(item) for item in (self.root, path.parent, path)):
            raise SettingsError(409, "settings-unsafe-path", "插件设置路径不是普通本机目录或文件")
        return path

    def _read(self, plugin_id, schema):
        path = self._path(plugin_id)
        try:
            if not path.exists():
                return 0, validate_values(schema, {})
            if not path.is_file() or path.stat().st_size > MAX_SETTINGS_BYTES:
                raise ValueError("invalid file")
            document = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(document, dict) or set(document) != {"schema", "revision", "values"} or \
                    type(document["schema"]) is not int or document["schema"] != 1 or \
                    type(document["revision"]) is not int or not 1 <= document["revision"] <= MAX_REVISION or \
                    not isinstance(document["values"], dict):
                raise ValueError("invalid document")
            # Plugin updates may remove or rename fields. Project saved values onto the current
            # declaration without changing the file; only an explicit save drops retired fields.
            keys = {field["key"] for group in schema["groups"] for field in group["fields"]}
            current_values = {key: value for key, value in document["values"].items() if key in keys}
            return document["revision"], validate_values(schema, current_values)
        except SettingsError as exc:
            raise SettingsError(409, "settings-schema-changed", "已保存设置与当前插件声明不一致，请检查插件版本", exc.detail) from None
        except (OSError, UnicodeError, ValueError):
            raise SettingsError(409, "settings-corrupt", "插件设置无法读取；原文件已保留") from None

    def read(self, plugin_id, schema):
        with self.lock:
            if schema is None:
                return 0, {}
            revision, values = self._read(plugin_id, schema)
            return revision, copy.deepcopy(values)

    def save(self, plugin_id, schema, revision, values):
        if schema is None:
            raise SettingsError(409, "settings-unavailable", "该插件没有声明可编辑设置")
        if type(revision) is not int or not 0 <= revision < MAX_REVISION:
            raise SettingsError(400, "settings-invalid-revision", "设置版本不正确")
        validated = validate_values(schema, values)
        with self.lock:
            current, _ = self._read(plugin_id, schema)
            if revision != current:
                raise SettingsError(409, "settings-conflict", "设置已在其他窗口更新，请重新载入后再保存", {"revision": current})
            document = {"schema": 1, "revision": current + 1, "values": validated}
            data = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
            if len(data.encode("utf-8")) > MAX_SETTINGS_BYTES:
                raise SettingsError(413, "settings-too-large", "插件设置超过大小限制")
            path = self._path(plugin_id)
            temporary = path.with_name("settings." + uuid.uuid4().hex + ".tmp")
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                self._path(plugin_id)
                with temporary.open("x", encoding="utf-8", newline="\n") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                self._path(plugin_id)
                os.replace(temporary, path)
            except OSError:
                raise SettingsError(500, "settings-write-failed", "插件设置保存失败；原设置已保留") from None
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            return current + 1, copy.deepcopy(validated)
