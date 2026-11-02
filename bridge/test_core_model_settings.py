"""Core model policy contracts using synthetic settings and no real providers."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_http
from core_gateway import GatewayUnavailable
from core_model_settings import DEFAULTS, ModelSettingsStore
from core_plugin_settings import SettingsError
from core_services import CoreServices
from plugin_api import ApiError, PluginAPI


class ModelSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.local = Path(self.temp.name)
        self.store = ModelSettingsStore(self.local)
        self.path = self.local / "core" / "model" / "settings.json"

    def legacy(self, plugin, values):
        path = self.local / "plugin-data" / plugin / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "revision": 2, "values": values}), encoding="utf-8")
        return path

    def test_defaults_are_persisted_without_a_model_profile_or_running_plugin(self):
        first = self.store.read()
        self.assertEqual(first, {"revision": 1, "values": DEFAULTS})
        first["values"]["searchSource"] = "searxng"
        self.assertEqual(ModelSettingsStore(self.local).read()["values"], DEFAULTS)
        self.assertFalse((self.local / "plugin-data").exists())

    def test_migration_preserves_search_and_old_total_capacity_once(self):
        advisor = self.legacy("kyf.advisor", {"includeMemories": False, "searchSource": "searxng",
            "searxngUrl": "http://127.0.0.1:1234/search-instance", "searchOpenPages": False})
        insights = self.legacy("kyf.message-insights", {"maxConcurrentTasks": 6, "replyStyle": "polite"})
        external = self.legacy("kyf.external-chat-import", {"maxConcurrentTasks": 8})
        originals = {path: path.read_bytes() for path in (advisor, insights, external)}
        saved = self.store.read()
        self.assertEqual(saved["values"], {"searchSource": "searxng",
            "searxngUrl": "http://127.0.0.1:1234/search-instance", "searchOpenPages": False})
        self.assertTrue(all(path.read_bytes() == data for path, data in originals.items()))
        # 旧文档残留的统一调度总额在读取和保存时都被忽略，不再写回。
        updated = self.store.save(saved["revision"], {**saved["values"], "maxConcurrentTasks": 1,
                                                      "searchSource": "none"})
        self.assertEqual(ModelSettingsStore(self.local).read(), updated)

    def test_legacy_defaults_keep_total_four_and_bad_files_are_preserved(self):
        self.legacy("kyf.message-insights", {"maxConcurrentTasks": 2})
        broken = self.legacy("kyf.advisor", {})
        broken.write_text("synthetic broken document", encoding="utf-8")
        self.assertEqual(self.store.read()["values"], DEFAULTS)
        self.assertEqual(broken.read_text(), "synthetic broken document")

    def test_invalid_values_and_conflicts_preserve_saved_settings(self):
        first = self.store.read()
        before = self.path.read_bytes()
        invalid = [{"searchSource": "other"}, {"searchOpenPages": 1}, {"searxngUrl": "x" * 513}]
        invalid += [{"searchSource": "searxng", "searxngUrl": url} for url in (
            "", "file:///tmp/search", "http://user:pass@localhost", "https://example.org/?q=x",
            "http://localhost:99999", "http://localhost:0", "http://local host", "https://example.org/#fragment")]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(SettingsError):
                self.store.save(first["revision"], {**DEFAULTS, **change})
            self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaises(SettingsError):
            self.store.save(first["revision"], {"maxConcurrentTasks": 2})
        # 旧客户端多带的 maxConcurrentTasks 被静默丢弃，不再作为可保存字段。
        updated = self.store.save(first["revision"], {**DEFAULTS, "maxConcurrentTasks": 2})
        self.assertEqual(updated["values"], DEFAULTS)
        with self.assertRaises(SettingsError) as caught:
            self.store.save(first["revision"], DEFAULTS)
        self.assertEqual(caught.exception.code, "settings-conflict")

    def test_parallel_windows_do_not_lose_a_saved_update(self):
        first = self.store.read()
        barrier, results = threading.Barrier(2), []
        def save(value):
            barrier.wait()
            try:
                self.store.save(first["revision"], {**DEFAULTS, "maxConcurrentTasks": value})
                results.append("saved")
            except SettingsError as exc:
                results.append(exc.code)
        threads = [threading.Thread(target=save, args=(value,)) for value in (2, 8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual(results, ["saved", "settings-conflict"])

    def test_gateway_uses_only_core_policy_and_keeps_model_identity(self):
        core = CoreServices.__new__(CoreServices)
        core.model_settings_store = self.store
        core.gateway = SimpleNamespace(reconfigure=Mock())
        core.registry = Mock()
        core._model_capabilities = SimpleNamespace(lookup=lambda *_args: None)
        listing = {"models": [{"id": "synthetic-service", "concurrency": 4}],
                   "roles": {"main": "synthetic-service", "judge": None, "image": None}}
        core.backend = SimpleNamespace(model_source_store=SimpleNamespace(
            saved_selection=lambda: {"sourceId": "synthetic-service", "api": {
                "protocol": "ollama", "baseUrl": "http://127.0.0.1:1", "model": "synthetic-model",
                "contextTokens": 32768}},
            resolve_key=lambda *args, **kwargs: None,
            models=lambda: listing))
        before = core._gateway_services()[0]
        self.assertEqual((before["concurrency"], before["roles"]), (4, ["main"]))
        saved = core.model_settings()
        # 统一调度设置不再决定并发；改设置不影响模型的 stamp 和 fingerprint。
        core.save_model_settings(saved["revision"], {**saved["values"], "searchOpenPages": False})
        after = core._gateway_services()[0]
        self.assertEqual(before["concurrency"], after["concurrency"])
        self.assertNotIn("pluginConcurrency", after)
        self.assertEqual((before["stamp"], before["fingerprint"]), (after["stamp"], after["fingerprint"]))
        self.assertEqual(core.registry.mock_calls, [])
        core.gateway.reconfigure.assert_called_once()
        self.path.write_text("synthetic corrupt core settings", encoding="utf-8")
        with self.assertRaises(GatewayUnavailable) as caught:
            core._gateway_services()
        self.assertEqual(caught.exception.code, "settings-corrupt")

    def test_host_can_save_offline_but_plugins_can_only_read(self):
        core = CoreServices.__new__(CoreServices)
        core.model_settings_store = self.store
        core.gateway = SimpleNamespace(reconfigure=Mock(side_effect=GatewayUnavailable()))
        core.supervisor = SimpleNamespace(plugin_for_token=lambda value: "synthetic.plugin" if value == "synthetic" else None)
        core.require_plugin = Mock()
        first = core_http.get(core, "/api/model-settings", {})
        saved = core_http.post(core, "/api/model-settings", {"revision": first["revision"],
            "values": {**first["values"], "maxConcurrentTasks": 3}})
        api = PluginAPI(core)
        headers = {"Authorization": "Bearer synthetic"}
        status, visible = api.handle("GET", "/plugin-api/v1/models/settings", {}, headers, None)
        self.assertEqual((status, visible), (200, saved))
        core.require_plugin.assert_not_called()
        with self.assertRaises(ApiError) as caught:
            api.handle("POST", "/plugin-api/v1/models/settings", {}, headers, saved)
        self.assertEqual(caught.exception.status, 404)
        with self.assertRaises(ApiError):
            api.handle("GET", "/plugin-api/v1/models/settings", {}, {}, None)
        with self.assertRaises(ApiError):
            api.handle("GET", "/plugin-api/v1/models/settings", {"plugin": "other"}, headers, None)
        self.assertEqual(self.store.read(), saved)


    def test_gateway_routes_assigned_roles_and_falls_back_silently(self):
        core = CoreServices.__new__(CoreServices)
        core.model_settings_store = self.store
        core.registry = Mock()
        core._model_capabilities = SimpleNamespace(lookup=lambda *_args: None)
        main_id, judge_id, image_id = "1" * 32, "2" * 32, "3" * 32

        keys = {}

        def make_listing(judge, image, judge_key=True, image_key=True):
            models = [
                {"id": main_id, "protocol": "ollama", "baseUrl": "http://127.0.0.1:1",
                 "model": "main-model", "contextTokens": 32768, "timeoutMs": 3600000,
                 "concurrency": 4, "hasKey": False},
                {"id": judge_id, "protocol": "anthropic", "baseUrl": "https://judge.test/v1",
                 "model": "judge-model", "contextTokens": 16384, "timeoutMs": 3600000,
                 "concurrency": 2, "hasKey": judge_key},
                {"id": image_id, "protocol": "anthropic", "baseUrl": "https://image.test/v1",
                 "model": "image-model", "contextTokens": 16384, "timeoutMs": 3600000,
                 "concurrency": 3, "hasKey": image_key},
            ]
            keys.clear()
            if judge == judge_id and judge_key:
                keys[judge_id] = "judge-key"
            if image == image_id and image_key:
                keys[image_id] = "image-key"
            return {"models": models, "roles": {"main": main_id, "judge": judge, "image": image}}

        store = SimpleNamespace(
            saved_selection=lambda: {"sourceId": main_id, "api": {
                "protocol": "ollama", "baseUrl": "http://127.0.0.1:1", "model": "main-model",
                "contextTokens": 32768}},
            resolve_key=lambda protocol, base_url, key, model_id=None: keys.get(model_id),
            models=lambda: make_listing(judge_id, image_id))
        core.backend = SimpleNamespace(model_source_store=store)
        services = core._gateway_services()
        self.assertEqual([item["id"] for item in services], [main_id, judge_id, image_id],
                         "主模型永远排第一")
        self.assertEqual(services[0]["roles"], ["main"])
        self.assertEqual(services[1]["roles"], ["judge"])
        self.assertEqual(services[1]["apiKey"], "judge-key")
        self.assertEqual(services[2]["roles"], ["image"])
        self.assertEqual([item["concurrency"] for item in services], [4, 2, 3])
        # 主模型身份不随用途改变；roles 不进 stamp。
        self.assertEqual(len({item["stamp"] for item in services}), 3)

        # 判断模型未分配：只下发主模型；判断模型缺密钥：静默退回，不下发该服务。
        store.models = lambda: make_listing(None, image_id, judge_key=False, image_key=False)
        services = core._gateway_services()
        self.assertEqual([item["id"] for item in services], [main_id])
        self.assertEqual(services[0]["roles"], ["main"])

        # 判断模型与主模型同一台：一条服务、roles 写全。
        store.models = lambda: make_listing(main_id, None)
        services = core._gateway_services()
        self.assertEqual([item["id"] for item in services], [main_id])
        self.assertEqual(services[0]["roles"], ["main", "judge"])

        # Two optional purposes assigned to one model share one queue/service.
        store.models = lambda: make_listing(judge_id, judge_id)
        services = core._gateway_services()
        self.assertEqual([item["id"] for item in services], [main_id, judge_id])
        self.assertEqual(services[1]["roles"], ["judge", "image"])

    def test_main_concurrency_reads_the_role_model(self):
        core = CoreServices.__new__(CoreServices)
        listing = {"models": [{"id": "m", "concurrency": 6}], "roles": {"main": "m", "judge": None, "image": None}}
        core.backend = SimpleNamespace(model_source_store=SimpleNamespace(models=lambda: listing))
        self.assertEqual(core.main_concurrency(), 6)
        core.backend = SimpleNamespace(model_source_store=SimpleNamespace(models=lambda: (_ for _ in ()).throw(RuntimeError())))
        self.assertEqual(core.main_concurrency(), 4)

    def test_model_stamp_is_the_same_in_main_and_optional_roles_and_tracks_key_changes(self):
        from model_source import ModelSourceStore
        store = ModelSourceStore(self.local / "model" / "source.json", root=self.local,
                                 protect=lambda key: key.encode(), unprotect=lambda key: key.decode())
        first = store.save_api("responses", "https://example.test/v1", "first", "first-key")
        second = store.save_model(protocol="responses", base_url="https://example.test/v1",
                                  model="second", key="second-key")
        store.set_roles(image=second)
        core = CoreServices.__new__(CoreServices)
        core.model_settings_store = self.store
        core._model_capabilities = SimpleNamespace(lookup=lambda *_args: None)
        core.backend = SimpleNamespace(model_source_store=store)
        optional = core._gateway_services()[1]
        store.set_roles(main=second, image=first)
        main = core._gateway_services()[0]
        self.assertEqual(optional["stamp"], main["stamp"])
        self.assertEqual(optional["fingerprint"], main["fingerprint"])
        store.save_model(second, protocol="responses", base_url="https://example.test/v1",
                         model="second", key="replacement-key")
        changed = core._gateway_services()[0]
        self.assertNotEqual(main["stamp"], changed["stamp"])
        self.assertEqual(main["fingerprint"], changed["fingerprint"])


if __name__ == "__main__":
    unittest.main()
