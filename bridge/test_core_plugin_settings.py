"""Plugin settings contracts using temporary synthetic plugins and configuration only."""
import copy
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_http
from core_plugin_settings import MAX_SETTINGS_BYTES, PluginSettingsStore, SettingsError, validate_schema
from core_plugins import ManifestError, PluginRegistry, parse_manifest
from plugin_api import ApiError, PluginAPI


SCHEMA = {
    "schema": 1, "description": "插件默认行为", "groups": [{"id": "behavior", "title": "行为", "fields": [
        {"key": "autoUpdate", "title": "自动更新", "type": "boolean", "default": True},
        {"key": "replyStyle", "title": "回复风格", "type": "select", "default": "natural",
         "options": [{"value": "natural", "label": "自然"}, {"value": "brief", "label": "简短"}]},
        {"key": "count", "title": "数量", "type": "integer", "default": 3, "minimum": 1, "maximum": 10},
        {"key": "prefix", "title": "前缀", "type": "string", "default": "", "maxLength": 30},
    ]}], "links": [{"label": "管理仓库", "nav": "repository"}],
}


def write_plugin(root, plugin_id="dev.test", *, schema=SCHEMA, incompatible=False):
    folder = root / ".local" / "plugins" / plugin_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.py").write_text("raise AssertionError('settings must not start a plugin')\n", encoding="utf-8")
    (folder / "repository.html").write_text("<!doctype html>", encoding="utf-8")
    contributes = {"nav": [{"id": "repository", "label": "仓库", "page": "repository.html"}]}
    if schema is not None:
        contributes["settings"] = copy.deepcopy(schema)
    manifest = {"schema": 1, "id": plugin_id, "name": "测试插件", "version": "0.1.0",
                "core": {"api": [2 if incompatible else 1]},
                "runtime": {"type": "python", "entry": "main.py"}, "contributes": contributes}
    (folder / "plugin.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return folder


class SchemaTests(unittest.TestCase):
    def test_plugins_cannot_declare_model_scheduling_authority(self):
        schema = copy.deepcopy(SCHEMA)
        schema["modelConcurrencyKey"] = "count"
        with self.assertRaises(ValueError):
            validate_schema(schema, {"repository"})

    def test_manifest_normalizes_supported_fields_and_own_nav_links(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = parse_manifest(write_plugin(Path(temp)), "local")
        schema = manifest["contributes"]["settings"]
        self.assertEqual(schema["groups"][0]["fields"][0]["key"], "autoUpdate")
        self.assertEqual(schema["links"], SCHEMA["links"])

    def test_rejects_invalid_defaults_duplicates_types_and_foreign_nav(self):
        mutations = [
            lambda s: s.update(schema=True),
            lambda s: s["links"][0].update(nav="another-plugin-nav"),
            lambda s: s["groups"][0]["fields"][0].update(default=1),
            lambda s: s["groups"][0]["fields"][0].update(type="secret"),
            lambda s: s["groups"][0]["fields"][1].update(default="missing"),
            lambda s: s["groups"][0]["fields"][1]["options"][0].update(value=True),
            lambda s: s["groups"][0]["fields"][1]["options"][0].update(value=1),
            lambda s: s["groups"][0]["fields"][2].update(minimum=True),
            lambda s: s["groups"][0]["fields"][2].update(minimum=20),
            lambda s: s["groups"][0]["fields"][3].update(maxLength=5000),
            lambda s: s["groups"][0]["fields"][3].update(key="constructor"),
            lambda s: s["groups"][0]["fields"].append(s["groups"][0]["fields"][0]),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                schema = copy.deepcopy(SCHEMA)
                mutate(schema)
                with tempfile.TemporaryDirectory() as temp:
                    with self.assertRaises(ManifestError):
                        parse_manifest(write_plugin(Path(temp), schema=schema), "local")


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        write_plugin(self.root)
        write_plugin(self.root, "dev.other")
        write_plugin(self.root, "dev.plain", schema=None)
        self.registry = PluginRegistry(self.root)
        self.core = SimpleNamespace(registry=self.registry)

    def save(self, plugin_id, revision, values):
        return self.registry.save_settings(plugin_id, revision, values, self.registry.settings(plugin_id)["definition"])

    def test_disabled_plugin_defaults_save_reload_and_data_isolation_without_starting(self):
        result = core_http.get(self.core, "/api/plugins/settings", {"id": "dev.test"})
        self.assertFalse(result["enabled"])
        self.assertEqual(result["values"], {"autoUpdate": True, "replyStyle": "natural", "count": 3, "prefix": ""})
        self.assertEqual(result["revision"], 0)
        self.assertFalse((self.root / ".local" / "plugin-data").exists())
        saved = core_http.post(self.core, "/api/plugins/settings", {"id": "dev.test", "revision": 0,
                                                                    "definition": result["definition"],
                                                                    "values": {"autoUpdate": False, "replyStyle": "brief"}})
        self.assertEqual((saved["revision"], saved["values"]["replyStyle"], saved["effective"]), (1, "brief", "next-task"))
        self.assertFalse(self.registry.enabled("dev.test"))
        restored = PluginRegistry(self.root).settings("dev.test")
        self.assertEqual(restored["values"], saved["values"])
        self.assertEqual(self.registry.settings("dev.other")["values"]["replyStyle"], "natural")
        document = json.loads((self.root / ".local" / "plugin-data" / "dev.test" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(set(document), {"schema", "revision", "values"})

    def test_conflict_and_validation_preserve_saved_document(self):
        self.save("dev.test", 0, {"autoUpdate": False})
        path = self.root / ".local" / "plugin-data" / "dev.test" / "settings.json"
        before = path.read_bytes()
        for revision, values, expected in [
            (0, {}, "settings-conflict"), (True, {}, "settings-invalid-revision"),
            (1, {"unknown": 1}, "settings-unknown-field"), (1, {"count": True}, "settings-field-invalid"),
            (1, {"count": 99}, "settings-field-invalid"), (1, {"replyStyle": "invalid"}, "settings-field-invalid"),
            (1, {"prefix": "x" * 31}, "settings-field-invalid"), (1, {"autoUpdate": "yes"}, "settings-field-invalid"),
        ]:
            with self.subTest(expected=expected, values=values):
                with self.assertRaises(SettingsError) as caught:
                    self.save("dev.test", revision, values)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(path.read_bytes(), before)

    def test_parallel_forms_cannot_lose_an_update(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def save(style):
            barrier.wait()
            try:
                outcomes.append(self.save("dev.test", 0, {"replyStyle": style})["revision"])
            except SettingsError as exc:
                outcomes.append(exc.code)

        workers = [threading.Thread(target=save, args=(style,)) for style in ("natural", "brief")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertCountEqual(outcomes, [1, "settings-conflict"])

    def test_corrupt_oversized_or_changed_saved_config_is_never_overwritten(self):
        definition = self.registry.settings("dev.test")["definition"]
        path = self.root / ".local" / "plugin-data" / "dev.test" / "settings.json"
        path.parent.mkdir(parents=True)
        for content, expected in [("not json", "settings-corrupt"), ("x" * (MAX_SETTINGS_BYTES + 1), "settings-corrupt"),
                                  (json.dumps({"schema": 1, "revision": 1, "values": {"autoUpdate": "invalid"}}), "settings-schema-changed")]:
            path.write_text(content, encoding="utf-8")
            before = path.read_bytes()
            with self.subTest(expected=expected):
                with self.assertRaises(SettingsError) as caught:
                    self.registry.save_settings("dev.test", 1, {}, definition)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(path.read_bytes(), before)

    def test_manifest_changes_reject_a_form_even_before_the_first_save(self):
        original = self.registry.settings("dev.test")
        changed = copy.deepcopy(SCHEMA)
        changed["groups"][0]["fields"][0]["default"] = False
        write_plugin(self.root, schema=changed)
        self.registry.refresh()
        current = self.registry.settings("dev.test")
        self.assertEqual((original["revision"], current["revision"]), (0, 0))
        self.assertNotEqual(original["definition"], current["definition"])
        with self.assertRaises(SettingsError) as stale:
            self.registry.save_settings("dev.test", 0, original["values"], original["definition"])
        self.assertEqual(stale.exception.code, "settings-schema-changed")
        changed["links"][0]["label"] = "新的仓库入口"
        write_plugin(self.root, schema=changed)
        self.registry.refresh()
        self.assertNotEqual(current["definition"], self.registry.settings("dev.test")["definition"])

    def test_plugin_update_projects_saved_fields_without_writing_until_explicit_save(self):
        old_schema = {"schema": 1, "groups": [{"id": "behavior", "title": "行为", "fields": [
            {"key": "foo", "title": "旧字段", "type": "boolean", "default": True},
            {"key": "kept", "title": "保留字段", "type": "string", "default": "default"},
        ]}]}
        write_plugin(self.root, schema=old_schema)
        self.registry.refresh()
        old = self.save("dev.test", 0, {"foo": False, "kept": "saved"})
        path = self.root / ".local" / "plugin-data" / "dev.test" / "settings.json"
        original_bytes = path.read_bytes()
        new_schema = copy.deepcopy(old_schema)
        new_schema["groups"][0]["fields"][0] = {"key": "bar", "title": "新字段", "type": "boolean", "default": True}
        write_plugin(self.root, schema=new_schema)
        self.registry.refresh()
        current = self.registry.settings("dev.test")
        self.assertEqual((current["revision"], current["values"]), (1, {"bar": True, "kept": "saved"}))
        self.assertEqual(path.read_bytes(), original_bytes)
        with self.assertRaises(SettingsError) as stale:
            self.registry.save_settings("dev.test", old["revision"], old["values"], old["definition"])
        self.assertEqual(stale.exception.code, "settings-schema-changed")
        self.assertEqual(path.read_bytes(), original_bytes)
        with self.assertRaises(SettingsError) as unknown:
            self.registry.save_settings("dev.test", current["revision"], {"foo": False}, current["definition"])
        self.assertEqual(unknown.exception.code, "settings-unknown-field")
        saved = self.registry.save_settings("dev.test", current["revision"], current["values"], current["definition"])
        self.assertEqual((saved["revision"], saved["values"]), (2, current["values"]))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["values"], {"bar": True, "kept": "saved"})

    def test_atomic_write_failure_keeps_old_settings_and_cleans_temporary_file(self):
        self.save("dev.test", 0, {"autoUpdate": False})
        directory = self.root / ".local" / "plugin-data" / "dev.test"
        before = (directory / "settings.json").read_bytes()
        with mock.patch("core_plugin_settings.os.replace", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(SettingsError) as caught:
                self.save("dev.test", 1, {"autoUpdate": True})
        self.assertEqual(caught.exception.code, "settings-write-failed")
        self.assertEqual((directory / "settings.json").read_bytes(), before)
        self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_plain_incompatible_and_removed_plugins_have_explicit_behavior(self):
        result = self.registry.settings("dev.plain")
        self.assertEqual((result["schema"], result["values"], result["revision"]), (None, {}, 0))
        with self.assertRaises(SettingsError) as plain:
            self.save("dev.plain", 0, {})
        self.assertEqual(plain.exception.code, "settings-unavailable")
        write_plugin(self.root, "dev.incompatible", incompatible=True)
        self.registry.refresh()
        self.assertIn("incompatible", self.registry.settings("dev.incompatible")["plugin"])
        with self.assertRaises(SettingsError) as incompatible:
            self.save("dev.incompatible", 0, {})
        self.assertEqual(incompatible.exception.code, "plugin-incompatible")
        del self.registry.plugins["dev.test"]
        with self.assertRaises(SettingsError) as missing:
            self.registry.settings("dev.test")
        self.assertEqual(missing.exception.code, "plugin-missing")

    def test_http_field_error_shape_and_plugin_identity_are_enforced(self):
        with self.assertRaises(core_http.HostError) as caught:
            core_http.post(self.core, "/api/plugins/settings", {"id": "dev.test", "revision": 0, "values": {"count": 0},
                                                               "definition": self.registry.settings("dev.test")["definition"]})
        self.assertEqual(caught.exception.status, 422)
        self.assertIn("count", caught.exception.detail["fields"])
        api = PluginAPI(SimpleNamespace(registry=self.registry,
            supervisor=SimpleNamespace(plugin_for_token=lambda token: "dev.test" if token == "synthetic-token" else None)))
        status, result = api.handle("GET", "/plugin-api/v1/settings", {}, {"Authorization": "Bearer synthetic-token"}, None)
        self.assertEqual((status, result["plugin"]["id"]), (200, "dev.test"))
        with self.assertRaises(ApiError) as unauthorized:
            api.handle("GET", "/plugin-api/v1/settings", {}, {}, None)
        self.assertEqual(unauthorized.exception.status, 401)
        with self.assertRaises(ApiError) as foreign:
            api.handle("GET", "/plugin-api/v1/settings", {"id": "dev.other"}, {"Authorization": "Bearer synthetic-token"}, None)
        self.assertEqual(foreign.exception.code, "invalid-request")

    def test_path_traversal_and_reparse_paths_are_rejected(self):
        store = PluginSettingsStore(self.root / "data")
        schema = validate_schema(SCHEMA, {"repository"})
        with self.assertRaises(SettingsError):
            store.read("../outside", schema)
        with mock.patch("core_plugin_settings._reparse", return_value=True):
            with self.assertRaises(SettingsError) as caught:
                store.read("dev.test", schema)
        self.assertEqual(caught.exception.code, "settings-unsafe-path")

    def test_plugin_api_maps_settings_errors_to_status_code_and_message(self):
        for code, message in [("settings-corrupt", "插件设置无法读取；原文件已保留"),
                              ("settings-schema-changed", "已保存设置与当前插件声明不一致")]:
            with self.subTest(code=code):
                registry = SimpleNamespace(settings=mock.Mock(side_effect=SettingsError(409, code, message)))
                api = PluginAPI(SimpleNamespace(registry=registry,
                    supervisor=SimpleNamespace(plugin_for_token=lambda token: "dev.test")))
                with self.assertRaises(ApiError) as caught:
                    api.handle("GET", "/plugin-api/v1/settings", {}, {"Authorization": "Bearer synthetic-token"}, None)
                self.assertEqual((caught.exception.status, caught.exception.code, caught.exception.message),
                                 (409, code, message))
                registry.settings.assert_called_once_with("dev.test")


if __name__ == "__main__":
    unittest.main()
