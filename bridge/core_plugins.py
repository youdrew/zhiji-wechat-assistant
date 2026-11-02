"""Plugin discovery, manifest validation and enable state.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Plugins are folders with a ``plugin.json``. Bundled plugins in ``plugins/``
are enabled by default; a manifest with ``distribution: external`` starts disabled
even in that directory. Local plugins in ``.local/plugins`` also start disabled.
An invalid or incompatible plugin is listed with its reason
and never started. Core checks manifests for correctness only: plugins run with
the user's own permissions and Core does not sandbox them.
"""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path

from core_plugin_settings import PluginSettingsStore, SettingsError, definition_digest, validate_schema

CORE_API_VERSION = 1
PROTOCOL_VERSION = 1
MANIFEST_NAME = "plugin.json"
MAX_MANIFEST_BYTES = 64 * 1024
PLUGIN_ID = re.compile(r"[a-z0-9][a-z0-9.-]{1,62}[a-z0-9]\Z")
VERSION = re.compile(r"(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})(?:-[0-9A-Za-z.-]{1,32})?\Z")
LOCAL_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,47}\Z")
RUNTIMES = ("python", "node", "command")
EVENTS = ("message.new", "friend.opened", "human.changed")
HOST_TABS = ("memories",)
SLOT_KINDS = ("schemes", "chatHeader", "messageAnnotations", "chatToolbar", "nav", "settings")


class ManifestError(ValueError):
    pass


class DependencyError(ManifestError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _text(value, name, maximum, minimum=1):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum or any(
            ord(char) < 32 for char in value):
        raise ManifestError(f"{name} 不正确")
    return value.strip()


def _relative_file(base, value, name):
    text = _text(value, name, 256)
    if text.startswith(("/", "\\")) or ":" in text or any(part in ("", ".", "..") for part in re.split(r"[\\/]", text)):
        raise ManifestError(f"{name} 必须是插件目录内的相对路径")
    path = (base / text).resolve()
    if not path.is_relative_to(base.resolve()) or not path.is_file():
        raise ManifestError(f"{name} 指向的文件不存在")
    return text.replace("\\", "/")


def _validate_contributes(base, value):
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - set(SLOT_KINDS):
        raise ManifestError("contributes 不正确")
    result = {}
    schemes = value.get("schemes", [])
    if not isinstance(schemes, list) or len(schemes) > 8:
        raise ManifestError("schemes 不正确")
    seen = set()
    result["schemes"] = []
    for scheme in schemes:
        if not isinstance(scheme, dict):
            raise ManifestError("scheme 不正确")
        scheme_id = _text(scheme.get("id"), "scheme.id", 48)
        if not LOCAL_ID.match(scheme_id) or scheme_id in seen:
            raise ManifestError("scheme.id 不正确或重复")
        seen.add(scheme_id)
        tabs = scheme.get("tabs")
        if not isinstance(tabs, list) or not 1 <= len(tabs) <= 12:
            raise ManifestError("scheme.tabs 不正确")
        parsed_tabs, tab_ids = [], set()
        for tab in tabs:
            if isinstance(tab, dict) and set(tab) == {"host"}:
                if tab["host"] not in HOST_TABS or tab["host"] in tab_ids:
                    raise ManifestError("宿主栏目不正确")
                tab_ids.add(tab["host"])
                parsed_tabs.append({"host": tab["host"]})
                continue
            if not isinstance(tab, dict) or set(tab) - {"id", "title"}:
                raise ManifestError("scheme.tabs 项不正确")
            tab_id = _text(tab.get("id"), "tab.id", 32)
            if not LOCAL_ID.match(tab_id) or tab_id in tab_ids:
                raise ManifestError("tab.id 不正确或重复")
            tab_ids.add(tab_id)
            parsed_tabs.append({"id": tab_id, "title": _text(tab.get("title"), "tab.title", 16)})
        result["schemes"].append({
            "id": scheme_id, "name": _text(scheme.get("name"), "scheme.name", 24),
            "summary": _text(scheme.get("summary", scheme.get("name")), "scheme.summary", 120),
            "page": _relative_file(base, scheme.get("page"), "scheme.page"),
            "tabs": parsed_tabs,
        })
    for kind in ("chatHeader", "chatToolbar", "nav"):
        items = value.get(kind, [])
        if not isinstance(items, list) or len(items) > 4:
            raise ManifestError(f"{kind} 不正确")
        parsed = []
        for item in items:
            if not isinstance(item, dict):
                raise ManifestError(f"{kind} 项不正确")
            entry = {"id": _text(item.get("id"), f"{kind}.id", 48), "label": _text(item.get("label"), f"{kind}.label", 16)}
            if not LOCAL_ID.match(entry["id"]):
                raise ManifestError(f"{kind}.id 不正确")
            for key in ("page", "icon", "kind"):
                if key in item:
                    entry[key] = (_relative_file(base, item[key], f"{kind}.{key}") if key == "page"
                                  else _text(item[key], f"{kind}.{key}", 24))
            parsed.append(entry)
        result[kind] = parsed
    annotations = value.get("messageAnnotations", False)
    if type(annotations) is not bool:
        raise ManifestError("messageAnnotations 不正确")
    result["messageAnnotations"] = annotations
    if "settings" in value:
        try:
            result["settings"] = validate_schema(value["settings"], {item["id"] for item in result["nav"]})
        except ValueError as exc:
            raise ManifestError(str(exc)) from None
    return result


def parse_manifest(directory, source):
    """Validate one plugin folder. Returns the normalized manifest or raises ManifestError."""
    base = Path(directory)
    path = base / MANIFEST_NAME
    try:
        if path.is_symlink() or path.stat().st_size > MAX_MANIFEST_BYTES:
            raise ManifestError("plugin.json 过大或不是普通文件")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError("缺少 plugin.json") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise ManifestError("plugin.json 无法读取") from exc
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise ManifestError("plugin.json schema 不受支持")
    distribution = raw.get("distribution")
    if "distribution" in raw and distribution not in ("bundled", "external"):
        raise ManifestError("distribution 必须是 bundled 或 external")
    plugin_id = raw.get("id")
    if not isinstance(plugin_id, str) or not PLUGIN_ID.match(plugin_id) or ".." in plugin_id:
        raise ManifestError("插件 id 不正确")
    dependencies = raw.get("dependencies", [])
    if not isinstance(dependencies, list) or len(dependencies) > 32:
        raise ManifestError("dependencies 必须是最多 32 个插件 id 的列表")
    dependency_ids = set()
    for dependency in dependencies:
        if not isinstance(dependency, str) or not PLUGIN_ID.match(dependency) or ".." in dependency:
            raise ManifestError("dependencies 中的插件 id 不正确")
        if dependency == plugin_id:
            raise ManifestError("dependencies 不能依赖插件自身")
        if dependency in dependency_ids:
            raise ManifestError("dependencies 中的插件 id 不能重复")
        dependency_ids.add(dependency)
    version = raw.get("version")
    if not isinstance(version, str) or not VERSION.match(version):
        raise ManifestError("插件版本必须是 主.次.修订")
    core = raw.get("core")
    if not isinstance(core, dict) or not isinstance(core.get("api"), list) or not core["api"] or any(
            type(item) is not int for item in core["api"]):
        raise ManifestError("core.api 不正确")
    runtime = raw.get("runtime")
    if not isinstance(runtime, dict) or runtime.get("type") not in RUNTIMES:
        raise ManifestError("runtime 不正确")
    if runtime["type"] == "command":
        command = runtime.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
            raise ManifestError("runtime.command 不正确")
        parsed_runtime = {"type": "command", "command": [_relative_file(base, command[0], "runtime.command"),
                                                         *[_text(item, "runtime.command", 512) for item in command[1:]]]}
    else:
        parsed_runtime = {"type": runtime["type"], "entry": _relative_file(base, runtime.get("entry"), "runtime.entry")}
    events = raw.get("events", [])
    if not isinstance(events, list) or any(item not in EVENTS for item in events) or len(set(events)) != len(events):
        raise ManifestError("events 不正确")
    manifest = {
        "id": plugin_id, "name": _text(raw.get("name"), "name", 32), "version": version,
        "description": _text(raw.get("description", ""), "description", 300, 0),
        "author": _text(raw.get("author", ""), "author", 64, 0),
        "coreApi": sorted(set(core["api"])), "runtime": parsed_runtime, "events": events,
        "dependencies": list(dependencies),
        "contributes": _validate_contributes(base, raw.get("contributes")),
        "directory": str(base.resolve()), "source": "external" if distribution == "external" else source,
    }
    if CORE_API_VERSION not in manifest["coreApi"]:
        manifest["incompatible"] = (f"插件需要 Core 接口版本 {', '.join(map(str, manifest['coreApi']))}，"
                                    f"当前提供 {CORE_API_VERSION}")
    return manifest


class PluginRegistry:
    """Bundled and development plugins with their persisted enable state."""

    def __init__(self, root, *, bundled_dir=None, local_dir=None, state_path=None, plugin_data_dir=None):
        self.root = Path(root)
        self.bundled_dir = Path(bundled_dir) if bundled_dir else self.root / "plugins"
        self.local_dir = Path(local_dir) if local_dir else self.root / ".local" / "plugins"
        self.state_path = Path(state_path) if state_path else self.root / ".local" / "core" / "plugins.json"
        self.lock = threading.RLock()
        self.settings_store = PluginSettingsStore(plugin_data_dir if plugin_data_dir is not None else self.root / ".local" / "plugin-data")
        self.plugins = {}
        self.problems = []
        self.state = self._read_state()
        self.refresh()

    def _read_state(self):
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            enabled = value.get("enabled") if isinstance(value, dict) else None
            if isinstance(enabled, dict) and all(isinstance(key, str) and type(item) is bool
                                                 for key, item in enabled.items()):
                return {"enabled": dict(enabled)}
        except (OSError, ValueError, AttributeError):
            pass
        return {"enabled": {}}

    def _write_state(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(self.state_path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _scan(self, base, source):
        found = []
        try:
            entries = sorted(base.iterdir()) if base.is_dir() else []
        except OSError:
            return found
        for folder in entries:
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            if not (folder / MANIFEST_NAME).exists():
                continue
            try:
                found.append(parse_manifest(folder, source))
            except ManifestError as exc:
                found.append({"id": None, "directory": str(folder), "source": source, "error": str(exc)})
        return found

    def refresh(self):
        with self.lock:
            plugins, problems = {}, []
            for manifest in self._scan(self.bundled_dir, "bundled") + self._scan(self.local_dir, "local"):
                if manifest.get("error"):
                    problems.append({"directory": manifest["directory"], "source": manifest["source"],
                                     "error": manifest["error"]})
                    continue
                if manifest["id"] in plugins:
                    existing = plugins[manifest["id"]]
                    problems.append({"directory": manifest["directory"], "source": manifest["source"],
                                     "error": f"与{'内置' if existing['source'] == 'bundled' else '已发现的'}插件 "
                                              f"{manifest['id']} 重复，未加载"})
                    continue
                plugins[manifest["id"]] = manifest
            self.plugins, self.problems = plugins, problems
            return self.listing()

    def enabled(self, plugin_id):
        with self.lock:
            manifest = self.plugins.get(plugin_id)
            if manifest is None or manifest.get("incompatible"):
                return False
            default = manifest["source"] == "bundled"
            return self.state["enabled"].get(plugin_id, default)

    def set_enabled(self, plugin_id, enabled):
        if type(enabled) is not bool:
            raise ValueError("invalid enabled flag")
        with self.lock:
            manifest = self.plugins.get(plugin_id)
            if manifest is None:
                raise KeyError(plugin_id)
            if enabled and manifest.get("incompatible"):
                raise ManifestError(manifest["incompatible"])
            if enabled:
                self.startup_order(plugin_id, for_enable=True)
            self.state["enabled"][plugin_id] = enabled
            self._write_state()
            return self.public(plugin_id)

    def get(self, plugin_id):
        with self.lock:
            return self.plugins.get(plugin_id)

    def active(self):
        """Enabled, compatible plugins whose complete dependency graph is usable."""
        with self.lock:
            return [manifest for plugin_id, manifest in sorted(self.plugins.items())
                    if self.enabled(plugin_id) and self.dependency_error(plugin_id) is None]

    def startup_order(self, plugin_id, *, for_enable=False):
        """Validate the complete graph before returning dependency-first plugin IDs.

        Enabling may ignore only the root's current toggle. Dependencies must
        already be enabled; validation never changes any persisted choices.
        """
        with self.lock:
            order, completed = [], set()
            pending = [(plugin_id, (), False)]
            while pending:
                current, ancestors, finished = pending.pop()
                if current in completed:
                    continue
                if finished:
                    completed.add(current)
                    order.append(current)
                    continue
                chain = (*ancestors, current)
                chain_text = " → ".join(chain)
                if current in ancestors:
                    raise DependencyError("dependency-cycle", f"插件 {plugin_id} 存在循环依赖（依赖链：{chain_text}）")
                manifest = self.plugins.get(current)
                if manifest is None:
                    code = "dependency-missing" if ancestors else "plugin-missing"
                    raise DependencyError(code, f"插件 {current} 未安装（依赖链：{chain_text}）")
                if manifest.get("incompatible"):
                    raise DependencyError("dependency-incompatible",
                                          f"插件 {current} 与 Core 不兼容：{manifest['incompatible']}（依赖链：{chain_text}）")
                if not self.enabled(current) and not (for_enable and not ancestors):
                    code = "dependency-disabled" if ancestors else "plugin-disabled"
                    raise DependencyError(code, f"插件 {current} 已停用（依赖链：{chain_text}）")
                pending.append((current, ancestors, True))
                pending.extend((dependency, chain, False)
                               for dependency in reversed(manifest.get("dependencies", [])))
            return order

    def dependency_error(self, plugin_id):
        """A dependency diagnostic independent of the root plugin's toggle."""
        try:
            self.startup_order(plugin_id, for_enable=True)
        except DependencyError as exc:
            return {"code": exc.code, "message": exc.message}
        return None

    def dependents(self, plugin_id):
        """All transitive dependents, with downstream consumers first when possible."""
        with self.lock:
            reverse = {}
            for current, manifest in sorted(self.plugins.items()):
                for dependency in manifest.get("dependencies", []):
                    reverse.setdefault(dependency, []).append(current)
            order, seen = [], {plugin_id}
            pending = [(current, False) for current in reversed(reverse.get(plugin_id, []))]
            while pending:
                current, finished = pending.pop()
                if finished:
                    order.append(current)
                elif current not in seen:
                    seen.add(current)
                    pending.append((current, True))
                    pending.extend((dependent, False) for dependent in reversed(reverse.get(current, [])))
            return order

    def _settings_manifest(self, plugin_id):
        manifest = self.plugins.get(plugin_id)
        if manifest is None:
            raise SettingsError(404, "plugin-missing", "插件不存在或已被移除，请重新扫描插件")
        return manifest

    def settings(self, plugin_id):
        with self.lock:
            manifest = self._settings_manifest(plugin_id)
            schema = manifest["contributes"].get("settings")
            if manifest.get("incompatible"):
                # The detail page remains available; an incompatible plugin's values cannot be applied.
                revision, values = 0, {}
            else:
                revision, values = self.settings_store.read(plugin_id, schema)
            return {"plugin": self.public(plugin_id), "schema": schema, "definition": definition_digest(schema),
                    "values": values, "revision": revision,
                    "enabled": self.enabled(plugin_id), "effective": "next-task"}

    def save_settings(self, plugin_id, revision, values, definition):
        with self.lock:
            manifest = self._settings_manifest(plugin_id)
            if manifest.get("incompatible"):
                raise SettingsError(409, "plugin-incompatible", manifest["incompatible"])
            schema = manifest["contributes"].get("settings")
            if schema is not None and (not isinstance(definition, str) or definition != definition_digest(schema)):
                raise SettingsError(409, "settings-schema-changed", "插件设置定义已变化，请重新载入后再保存")
            self.settings_store.save(plugin_id, schema, revision, values)
            return self.settings(plugin_id)

    def public(self, plugin_id):
        manifest = self.plugins[plugin_id]
        dependency_error = self.dependency_error(plugin_id)
        return {key: manifest[key] for key in ("id", "name", "version", "description", "author", "source",
                                               "events", "contributes")} | {
            "dependencies": list(manifest.get("dependencies", [])),
            "enabled": self.enabled(plugin_id),
            **({"dependencyError": dependency_error} if dependency_error else {}),
            **({"incompatible": manifest["incompatible"]} if manifest.get("incompatible") else {})}

    def listing(self):
        with self.lock:
            return {"coreApi": CORE_API_VERSION,
                    "plugins": [self.public(plugin_id) for plugin_id in sorted(self.plugins)],
                    "problems": list(self.problems)}

    def schemes(self):
        """Every analysis scheme of every active plugin, in a stable order."""
        result = []
        for manifest in self.active():
            for scheme in manifest["contributes"].get("schemes", []):
                result.append({"plugin": manifest["id"], "pluginName": manifest["name"],
                               "pluginVersion": manifest["version"], **scheme})
        return result
