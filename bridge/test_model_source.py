"""Model-source security and HTTP contract checks without provider or chat access."""

import base64
import hashlib
import http.client
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from model_source import (DEFAULT_MODEL_TIMEOUT_MS, LOCAL_SOURCE_ID, ModelSourceStore,
                          ModelSourceUnavailable, connection_values)
from core_contracts import DEFAULT_CONTEXT_TOKENS
from backend_service import Backend
from real_http import make_handler


class FakeProbe:
    def __init__(self):
        self.calls = []

    def model_list(self, protocol, base_url, api_key):
        self.calls.append(("list", protocol, base_url, api_key))
        return {"models": [{"id": "test-model", "name": "Test Model"}], "supported": True}

    def model_test(self, protocol, base_url, api_key, model):
        self.calls.append(("test", protocol, base_url, api_key, model))
        return {"ok": True, "latencyMs": 12.5}


class ModelSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        runtime = root / ".local" / "real-client-runtime"
        self.path = runtime / "api-model-source.json"
        self.legacy = runtime / "model-source.json"
        self.store = ModelSourceStore(
            self.path, root=root, legacy_path=self.legacy,
            protect=lambda key: b"protected:" + key[::-1].encode("utf-8"),
            unprotect=lambda data: data.removeprefix(b"protected:").decode("utf-8")[::-1],
        )
        self.probe = FakeProbe()
        self.backend = Backend(SimpleNamespace(identity=lambda: ("account-a", root)),
                               model_source_store=self.store, model_probe=self.probe)

    def server(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def test_returning_to_the_same_saved_api_source_skips_a_redundant_probe(self):
        request = {"mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
                   "model": "test-model", "apiKey": "test-only-key", "contextTokens": 128000}
        first = self.backend.model_source_activate(request)["sourceId"]
        self.assertEqual(len(self.probe.calls), 1)
        with self.assertRaises(ValueError):
            self.backend.model_source_activate({"mode": "local"})
        second = self.backend.model_source_activate({**request, "apiKey": ""})["sourceId"]
        self.assertEqual(second, first)
        self.assertEqual(len(self.probe.calls), 1)
        self.backend.model_source_activate({**request, "model": "different-model", "apiKey": ""})
        self.assertEqual(len(self.probe.calls), 2)

    @staticmethod
    def request(server, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            connection.request(method, path, body=encoded,
                               headers={"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_encrypted_profile_masks_key_and_reuse_is_endpoint_scoped(self):
        source_id = self.store.save_api("responses", "https://example.test/v1", "test-model",
                                        "test-only-key", context_tokens=128000)
        self.assertEqual(len(source_id), 32)
        self.assertNotIn("test-only-key", self.path.read_text(encoding="utf-8"))
        self.assertEqual(self.store.saved_selection()["selectedMode"], "api")
        self.assertEqual(self.store.public()["api"], {
            "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "test-model", "contextTokens": 128000, "timeoutMs": DEFAULT_MODEL_TIMEOUT_MS, "hasKey": True})
        self.assertEqual(self.store.public()["mode"], "local")
        self.assertEqual(self.store.resolve_key("responses", "https://example.test/v1", None), "test-only-key")
        self.assertIsNone(self.store.resolve_key("responses", "https://other.test/v1", None))
        self.assertIsNone(self.store.resolve_key("anthropic", "https://example.test/v1", None))
        self.store.save_local()
        self.assertEqual(self.store.saved_selection()["selectedMode"], "local")
        self.assertTrue(self.store.public()["api"]["hasKey"])
        self.store.clear_key()
        self.assertFalse(self.store.public()["api"]["hasKey"])

    def test_fetched_catalog_survives_restart_without_saving_or_activating_a_profile(self):
        settings = {"protocol": "responses", "baseUrl": "https://example.test/v1/",
                    "apiKey": "catalog-only-synthetic-key"}
        reply = self.backend.model_source_list(settings)
        self.assertFalse(self.path.exists())
        self.assertEqual(self.backend.model_source()["mode"], "none")
        reopened = ModelSourceStore(self.path, root=self.root)
        self.assertEqual(reopened.public()["modelList"], {
            "protocol": "responses", "baseUrl": "https://example.test/v1", **reply})
        self.assertIsNone(reopened.public()["api"])
        self.assertEqual(reopened.saved_selection()["selectedMode"], "local")
        self.assertIsNone(reopened.resolve_key("responses", "https://example.test/v1", None))
        cached = self.store.catalog_path.read_text(encoding="utf-8")
        self.assertNotIn("catalog-only-synthetic-key", cached)
        self.assertNotIn("apiKey", cached)
        self.assertNotIn("encryptedKey", cached)

    def test_catalog_preserves_saved_model_key_and_profile_when_another_endpoint_is_fetched(self):
        settings = {"protocol": "responses", "baseUrl": "https://example.test/v1"}
        catalog = self.backend.model_source_list({**settings, "apiKey": "test-only-key"})
        self.backend.model_source_activate({"mode": "api", **settings, "apiKey": "test-only-key",
                                            "model": "test-model"})
        saved_bytes = self.path.read_bytes()
        reopened = ModelSourceStore(self.path, root=self.root, unprotect=self.store.unprotect)
        self.assertEqual(reopened.public()["api"]["model"], "test-model")
        self.assertEqual(reopened.public()["modelList"], {**settings, **catalog})
        other = {"protocol": "ollama", "baseUrl": "http://other.test:11434"}
        self.backend.model_source_list(other)
        self.assertEqual(self.path.read_bytes(), saved_bytes)
        self.assertEqual(reopened.public()["modelList"], {**other, **catalog})
        self.assertEqual(reopened.resolve_key("responses", "https://example.test/v1", None), "test-only-key")
        self.store.clear_key()
        self.assertEqual(reopened.public()["modelList"], {**other, **catalog})

    def test_failed_or_invalid_discovery_does_not_replace_last_catalog(self):
        settings = {"protocol": "responses", "baseUrl": "https://example.test/v1"}
        self.backend.model_source_list(settings)
        saved_bytes = self.store.catalog_path.read_bytes()
        with mock.patch.object(self.probe, "model_list", side_effect=RuntimeError("network")):
            with self.assertRaises(ModelSourceUnavailable):
                self.backend.model_source_list(settings)
        for reply in ({"supported": True, "models": [{"id": "bad\nmodel"}]},
                      {"supported": "yes", "models": []},
                      {"supported": True, "models": [{"id": "a", "contextTokens": True}]}):
            with self.subTest(reply=reply), mock.patch.object(self.probe, "model_list", return_value=reply):
                with self.assertRaises(ModelSourceUnavailable):
                    self.backend.model_source_list(settings)
        self.assertEqual(self.store.catalog_path.read_bytes(), saved_bytes)

    def test_catalog_retains_only_valid_bounded_metadata(self):
        settings = {"protocol": "responses", "baseUrl": "https://example.test/v1"}
        reply = {"supported": True, "apiKey": "provider-extra-synthetic-key", "models": [
            {"id": "a", "name": "Model A", "contextTokens": 128000, "private": "discard"},
            {"id": "a", "name": "Duplicate"},
            *[{"id": f"model-{index}"} for index in range(501)]]}
        with mock.patch.object(self.probe, "model_list", return_value=reply):
            result = self.backend.model_source_list(settings)
        self.assertEqual(len(result["models"]), 499)
        self.assertEqual(result["models"][0], {"id": "a", "name": "Model A", "contextTokens": 128000})
        self.assertEqual(self.store.model_list(), {**settings, **result})
        self.assertNotIn("provider-extra-synthetic-key", self.store.catalog_path.read_text(encoding="utf-8"))
        with mock.patch.object(self.probe, "model_list", return_value={"supported": False, "models": []}):
            self.backend.model_source_list(settings)
        self.assertEqual(self.store.model_list(), {**settings, "supported": False, "models": []})

    def test_older_discovery_cannot_overwrite_a_newer_request_catalog(self):
        settings = {"protocol": "responses", "baseUrl": "https://example.test/v1"}
        for same_endpoint in (False, True):
            for newer_fails in (False, True):
                with self.subTest(same_endpoint=same_endpoint, newer_fails=newer_fails):
                    self.store.save_model_list(settings["protocol"], settings["baseUrl"],
                                               {"supported": True, "models": [{"id": "previous"}]})
                    started, release = threading.Event(), threading.Event()
                    errors = []

                    def probe(_protocol, _url, key):
                        if key == "older-synthetic-key":
                            started.set()
                            if not release.wait(3):
                                raise RuntimeError("test discovery not released")
                            return {"supported": True, "models": [{"id": "older"}]}
                        if newer_fails:
                            raise RuntimeError("network")
                        return {"supported": True, "models": [{"id": "newer"}]}

                    def older_request():
                        try:
                            self.backend.model_source_list({**settings, "apiKey": "older-synthetic-key"})
                        except Exception as exc:
                            errors.append(exc)

                    with mock.patch.object(self.probe, "model_list", side_effect=probe):
                        thread = threading.Thread(target=older_request)
                        thread.start()
                        try:
                            self.assertTrue(started.wait(3))
                            newer = {**settings, "apiKey": "newer-synthetic-key"}
                            if not same_endpoint:
                                newer["baseUrl"] = "https://other.test/v1"
                            if newer_fails:
                                with self.assertRaises(ModelSourceUnavailable):
                                    self.backend.model_source_list(newer)
                            else:
                                self.backend.model_source_list(newer)
                            expected = self.store.model_list()
                        finally:
                            release.set()
                            thread.join(3)
                    self.assertFalse(thread.is_alive())
                    self.assertEqual(errors, [])
                    self.assertEqual(self.store.model_list(), expected)
                    self.assertEqual(expected["models"], [{"id": "previous" if newer_fails else "newer"}])

    def test_bad_catalog_is_a_cache_miss_and_does_not_disable_a_saved_profile(self):
        self.store.save_api("responses", "https://example.test/v1", "test-model", "test-only-key")
        saved_bytes = self.path.read_bytes()
        for content in ("not json", "[]", '{"version":2}',
                        json.dumps({"version": 1, "protocol": "responses", "baseUrl": "invalid",
                                    "supported": True, "models": []})):
            with self.subTest(content=content):
                self.store.catalog_path.write_text(content, encoding="utf-8")
                public = self.backend.model_source()
                self.assertIsNone(public["modelList"])
                self.assertEqual(public["api"]["model"], "test-model")
                self.assertTrue(public["api"]["hasKey"])
                self.assertEqual(self.path.read_bytes(), saved_bytes)

    def test_catalog_path_rejects_reparse_directory(self):
        runtime = self.path.parent
        runtime.mkdir(parents=True)
        real_lstat = Path.lstat

        def marked_lstat(path):
            state = real_lstat(path)
            if path == runtime:
                return SimpleNamespace(st_mode=state.st_mode, st_file_attributes=0x400)
            return state

        with mock.patch.object(Path, "lstat", marked_lstat):
            self.assertIsNone(self.store.model_list())
            with self.assertRaises(ModelSourceUnavailable):
                self.store.save_model_list("responses", "https://example.test/v1",
                                           {"supported": True, "models": []})
        self.assertFalse(self.store.catalog_path.exists())

    def test_url_and_input_validation(self):
        good = connection_values({"protocol": "ollama", "baseUrl": "http://127.0.0.1:11434/"})
        self.assertEqual(good["baseUrl"], "http://127.0.0.1:11434")
        for bad in ("file:///tmp/model", "https://key@example.test/v1", "https://example.test/v1?key=x",
                    "https://example.test/v1#fragment", "https://example.test/../v1",
                    "https://example.test\\@evil.test", "https://example.test\n.evil.test"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                connection_values({"protocol": "responses", "baseUrl": bad})
        with self.assertRaises(ValueError):
            connection_values({"protocol": ["responses"], "baseUrl": "https://example.test/v1"})

    def test_context_size_validation_and_legacy_saved_profile(self):
        request = {"protocol": "responses", "baseUrl": "https://example.test/v1",
                   "model": "test-model"}
        for value in (4096, 1000000):
            with self.subTest(value=value):
                self.assertEqual(connection_values({**request, "contextTokens": value},
                                                   require_model=True)["contextTokens"], value)
        for value in (4095, 1000001, True, 8192.5, "8192"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                connection_values({**request, "contextTokens": value}, require_model=True)
        self.store.save_api("responses", "https://example.test/v1", "test-model",
                            "test-only-key")
        self.assertIsNone(self.store.public()["api"]["contextTokens"])

    def test_api_source_identity_survives_switches_restart_and_key_clear(self):
        def activate(model, key, context_tokens=8192, base_url="https://example.test/v1"):
            return self.backend.model_source_activate({
                "mode": "api", "protocol": "responses", "baseUrl": base_url,
                "model": model, "apiKey": key, "contextTokens": context_tokens})["sourceId"]

        first = activate("model-a", "synthetic-key-a")
        second = activate("model-b", "synthetic-key-b")
        self.assertNotEqual(first, second)
        self.assertEqual(activate("model-a", "synthetic-key-a", 16384), first)
        changed_key = activate("model-a", "synthetic-key-c")
        self.assertEqual(changed_key, first)
        changed_endpoint = activate("model-a", "synthetic-key-a",
                                    base_url="https://other.test/v1")
        self.assertNotIn(changed_endpoint, (first, second))

        reopened = ModelSourceStore(
            self.path, root=self.root,
            protect=lambda key: b"protected:" + key[::-1].encode("utf-8"),
            unprotect=lambda data: data.removeprefix(b"protected:").decode("utf-8")[::-1])
        self.backend.model_source_store = reopened
        self.assertEqual(activate("model-b", "synthetic-key-b"), second)
        self.backend.model_source_clear_key({})
        keyless = activate("model-b", "")
        self.assertEqual(keyless, second)
        self.assertEqual(activate("model-a", "synthetic-key-a"), first)
        public = self.backend.model_source()
        self.assertNotIn("sourceIds", public)
        stored = self.path.read_text(encoding="utf-8")
        self.assertNotIn("synthetic-key-a", stored)
        self.assertNotIn("synthetic-key-b", stored)
        self.assertNotIn("synthetic-key-c", stored)

    def test_timeout_validation_and_legacy_default_do_not_rewrite_old_settings(self):
        request = {"protocol": "responses", "baseUrl": "https://example.test/v1", "model": "test-model"}
        self.assertEqual(connection_values(request, require_model=True)["timeoutMs"], DEFAULT_MODEL_TIMEOUT_MS)
        for value in (60_000, 3_600_000, 10_800_000):
            self.assertEqual(connection_values({**request, "timeoutMs": value}, require_model=True)["timeoutMs"], value)
        for value in (None, True, 59_999, 10_800_001, 60_000.0, "60000"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                connection_values({**request, "timeoutMs": value}, require_model=True)
        self.store.save_api("responses", "https://example.test/v1", "test-model", "test-only-key")
        saved = json.loads(self.path.read_text(encoding="utf-8"))
        next(item for item in saved["models"] if item["id"] == saved["roles"]["main"]).pop("timeoutMs")
        self.path.write_text(json.dumps(saved), encoding="utf-8")
        old_bytes = self.path.read_bytes()
        self.assertEqual(self.store.public()["api"]["timeoutMs"], DEFAULT_MODEL_TIMEOUT_MS)
        self.assertEqual(self.path.read_bytes(), old_bytes)

    def test_timeout_update_keeps_source_key_context_and_survives_restart(self):
        request = {"mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
                   "model": "test-model", "contextTokens": 128000}
        first = self.backend.model_source_activate({**request, "apiKey": "test-only-key"})
        original_ciphertext = self.store.saved_selection()["api"]["encryptedKey"]
        # A real encryption operation uses a fresh nonce; re-encrypting even the
        # same key would invalidate the service stamp of a running conversation.
        self.store.protect = mock.Mock(side_effect=AssertionError("must retain existing ciphertext"))
        changed = self.backend.model_source_activate({**request, "timeoutMs": 7_200_000})
        self.assertEqual(changed["sourceId"], first["sourceId"])
        self.assertEqual(changed["api"]["contextTokens"], 128000)
        self.assertEqual(changed["api"]["timeoutMs"], 7_200_000)
        self.assertEqual(self.store.saved_selection()["api"]["encryptedKey"], original_ciphertext)
        self.assertEqual(len(self.probe.calls), 1)
        reopened = ModelSourceStore(self.path, root=self.root, unprotect=self.store.unprotect)
        self.assertEqual(reopened.public()["api"]["timeoutMs"], 7_200_000)
        self.assertEqual(reopened.resolve_key("responses", "https://example.test/v1", None), "test-only-key")
        # An older settings client must not reset the chosen timeout.
        again = self.backend.model_source_activate(request)
        self.assertEqual(again["api"]["timeoutMs"], 7_200_000)

    def test_unspecified_context_saves_effective_default_and_survives_reload(self):
        request = {"mode": "api", "protocol": "chat_completions",
                   "baseUrl": "https://example.test/v1", "model": "test-model",
                   "apiKey": "test-only-key"}
        for context in ({}, {"contextTokens": None}):
            with self.subTest(context=context):
                # A different model must not inherit the previously saved window.
                self.backend.model_source_activate({**request, "model": "previous-model",
                                                    "contextTokens": 128000})
                active = self.backend.model_source_activate({**request, **context})
                self.assertEqual(active["api"]["contextTokens"], DEFAULT_CONTEXT_TOKENS)
                self.assertEqual(type(active["api"]["contextTokens"]), int)
                reopened = ModelSourceStore(self.path, root=self.root,
                                            protect=self.store.protect, unprotect=self.store.unprotect)
                self.assertEqual(reopened.saved_selection()["api"]["model"], "test-model")
                self.assertEqual(reopened.public()["api"]["contextTokens"], DEFAULT_CONTEXT_TOKENS)
                self.assertEqual(reopened.resolve_key("chat_completions", "https://example.test/v1", None),
                                 "test-only-key")

    def test_legacy_api_source_id_migrates_before_switch(self):
        first = self.store.save_api("responses", "https://example.test/v1", "model-a",
                                    "synthetic-key-a", context_tokens=8192)
        # A genuine version 1 document: one profile, no fingerprint table.
        main_entry = json.loads(self.path.read_text(encoding="utf-8"))["models"][0]
        old = {"version": 1, "sourceId": main_entry["id"], "selectedMode": "api",
               "api": {key: value for key, value in main_entry.items() if key != "id"}}
        self.legacy.write_text(json.dumps(old), encoding="utf-8")
        self.path.unlink()
        legacy_bytes = self.legacy.read_bytes()
        self.backend.model_source_activate({
            "mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "model-b", "apiKey": "synthetic-key-b", "contextTokens": 8192})
        restored = self.backend.model_source_activate({
            "mode": "api", "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "model-a", "apiKey": "synthetic-key-a", "contextTokens": 16384})
        self.assertEqual(restored["sourceId"], first)
        self.assertEqual(self.legacy.read_bytes(), legacy_bytes)
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["sourceIds"]), 2)

    def test_config_paths_reject_escape(self):
        outside = self.root.parent / "outside-model-source.json"
        unsafe = ModelSourceStore(outside, root=self.root, legacy_path=self.legacy)
        with self.assertRaises(ModelSourceUnavailable):
            unsafe.saved_selection()
        with self.assertRaises(ModelSourceUnavailable):
            unsafe.save_api("responses", "https://example.test/v1", "test-model", None)

    def test_config_paths_reject_reparse_directory(self):
        runtime = self.path.parent
        runtime.mkdir(parents=True)
        real_lstat = Path.lstat
        def marked_lstat(path):
            state = real_lstat(path)
            if path == runtime:
                return SimpleNamespace(st_mode=state.st_mode, st_file_attributes=0x400)
            return state
        with mock.patch.object(Path, "lstat", marked_lstat):
            with self.assertRaises(ModelSourceUnavailable):
                self.store.saved_selection()
        self.assertFalse(self.path.exists())

    def test_http_contract_probes_and_activation(self):
        server = self.server()
        status, initial = self.request(server, "GET", "/api/model-source")
        self.assertEqual(status, 200)
        self.assertEqual(initial, {"mode": "none", "api": None,
                                   "sourceId": LOCAL_SOURCE_ID, "status": "active",
                                   "models": [], "roles": {"main": None, "judge": None, "image": None},
                                   "localJudge": initial["localJudge"], "modelList": None})
        self.assertEqual(initial["localJudge"]["state"], "missing")
        self.assertFalse(initial["localJudge"]["qualityVerified"])
        self.assertIsNone(self.backend._core)
        settings = {"protocol": "responses", "baseUrl": "https://example.test/v1",
                    "apiKey": "test-only-key"}
        status, models = self.request(server, "POST", "/api/model-source/list", settings)
        self.assertEqual((status, models), (200, {"models": [{"id": "test-model", "name": "Test Model"}],
                                                  "supported": True}))
        status, tested = self.request(server, "POST", "/api/model-source/test",
                                      {**settings, "model": "test-model"})
        self.assertEqual((status, tested), (200, {"ok": True, "latencyMs": 12.5}))
        self.assertEqual(self.probe.calls[1][-2], "test-only-key")
        status, missing = self.request(server, "POST", "/api/model-source/activate",
                                       {"mode": "api", **settings, "model": "test-model"})
        self.assertEqual(status, 200)
        self.assertEqual(missing["api"]["contextTokens"], DEFAULT_CONTEXT_TOKENS)
        self.assertEqual(self.backend.model_source()["mode"], "api")
        status, active = self.request(server, "POST", "/api/model-source/activate",
                                      {"mode": "api", **settings, "model": "test-model",
                                       "contextTokens": 128000})
        self.assertEqual(status, 200)
        self.assertEqual(active["mode"], "api")
        self.assertEqual(active["api"]["contextTokens"], 128000)
        self.assertEqual(active["api"]["timeoutMs"], DEFAULT_MODEL_TIMEOUT_MS)
        self.assertEqual(len(active["sourceId"]), 32)
        self.assertNotIn("test-only-key", self.path.read_text(encoding="utf-8"))
        saved_bytes = self.path.read_bytes()
        probes = len(self.probe.calls)
        for timeout_ms in (None, True, 0, 59_999, 10_800_001, 60_000.0, "60000"):
            with self.subTest(timeout_ms=timeout_ms):
                status, _error = self.request(server, "POST", "/api/model-source/activate",
                                             {"mode": "api", **settings, "model": "test-model",
                                              "timeoutMs": timeout_ms})
                self.assertEqual(status, 400)
                self.assertEqual(self.path.read_bytes(), saved_bytes)
                self.assertEqual(len(self.probe.calls), probes)
        status, updated = self.request(server, "POST", "/api/model-source/activate",
                                       {"mode": "api", **settings, "model": "test-model",
                                        "contextTokens": 128000, "timeoutMs": 10_800_000})
        self.assertEqual(status, 200)
        self.assertEqual(updated["api"]["timeoutMs"], 10_800_000)
        self.assertNotIn("test-only-key", json.dumps(updated))
        self.assertNotIn("encryptedKey", json.dumps(updated))
        active, saved_bytes, probes = updated, self.path.read_bytes(), len(self.probe.calls)
        for context_tokens in (4095, 1000001, True, 8192.5, "8192", ""):
            with self.subTest(context_tokens=context_tokens):
                status, _error = self.request(server, "POST", "/api/model-source/activate",
                                             {"mode": "api", **settings, "model": "different-model",
                                              "contextTokens": context_tokens})
                self.assertEqual(status, 400)
                self.assertEqual(self.backend.model_source(), active)
                self.assertEqual(self.path.read_bytes(), saved_bytes)
                self.assertEqual(len(self.probe.calls), probes)
        status, cleared = self.request(server, "POST", "/api/model-source/clear-key", {})
        self.assertEqual((status, cleared["mode"], cleared["api"]["hasKey"]), (200, "api", False))
        self.assertEqual(self.request(server, "POST", "/api/model-source/list",
                                      {**settings, "protocol": "unknown"})[0], 400)
        self.assertEqual(self.request(server, "POST", "/api/model-source/list", settings,
                                      {"Origin": "https://evil.test"})[0], 403)

    def test_saved_key_requires_explicit_endpoint_matched_reveal_with_origin_guard(self):
        server = self.server()
        settings = {"protocol": "responses", "baseUrl": "https://example.test/v1"}
        self.store.save_api("responses", "https://example.test/v1", "test-model", "test-only-key")
        saved_bytes = self.path.read_bytes()
        status, public = self.request(server, "GET", "/api/model-source")
        self.assertEqual(status, 200)
        self.assertTrue(public["api"]["hasKey"])
        self.assertNotIn("test-only-key", json.dumps(public))
        self.assertNotIn("encryptedKey", json.dumps(public))
        self.assertEqual(self.request(server, "POST", "/api/model-source/reveal-key", settings),
                         (200, {"apiKey": "test-only-key"}))
        self.assertEqual(self.request(server, "POST", "/api/model-source/reveal-key", settings,
                                      {"Origin": "https://evil.test"}), (403, {"error": "forbidden"}))
        for request in ({**settings, "baseUrl": "https://other.test/v1"},
                        {**settings, "protocol": "anthropic"}):
            with self.subTest(request=request):
                self.assertEqual(self.request(server, "POST", "/api/model-source/reveal-key", request),
                                 (503, {"error": "saved API key unavailable"}))
        self.assertEqual(self.request(server, "POST", "/api/model-source/reveal-key",
                                      {**settings, "apiKey": "test-only-key"})[0], 400)
        self.assertEqual(self.request(server, "GET", "/api/model-source/reveal-key")[0], 404)
        self.assertEqual(self.path.read_bytes(), saved_bytes)
        self.assertEqual(self.probe.calls, [])
        self.store.clear_key()
        self.assertEqual(self.request(server, "POST", "/api/model-source/reveal-key", settings),
                         (503, {"error": "saved API key unavailable"}))

    def test_revealed_key_response_is_never_http_cached(self):
        server = self.server()
        self.store.save_api("responses", "https://example.test/v1", "test-model", "test-only-key")
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("POST", "/api/model-source/reveal-key",
                           body=json.dumps({"protocol": "responses", "baseUrl": "https://example.test/v1"}),
                           headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(json.loads(response.read()), {"apiKey": "test-only-key"})

    def test_connector_auth_error_is_reported_without_provider_body(self):
        server = self.server()
        def denied(*_args):
            raise RuntimeError("auth")
        self.probe.model_test = denied
        status, body = self.request(server, "POST", "/api/model-source/test", {
            "protocol": "responses", "baseUrl": "https://example.test/v1",
            "model": "test-model", "apiKey": "test-only-key"})
        self.assertEqual((status, body), (503, {"error": "auth"}))
        self.assertEqual(self.backend.model_source()["mode"], "none")


    def test_model_save_http_adds_first_model_and_rotates_its_key(self):
        server = self.server()
        request = {"protocol": "responses", "baseUrl": "https://first.test/v1",
                   "model": "first-model", "apiKey": "synthetic-first-key", "concurrency": 2}
        status, added = self.request(server, "POST", "/api/model-source/models/save", request)
        self.assertEqual(status, 200)
        main_id = added["roles"]["main"]
        self.assertEqual(added["sourceId"], main_id)
        self.assertTrue(added["models"][0]["hasKey"])
        self.assertEqual(self.probe.calls[-1][3], "synthetic-first-key")
        status, edited = self.request(server, "POST", "/api/model-source/models/save",
                                      {**request, "id": main_id, "apiKey": "synthetic-next-key"})
        self.assertEqual(status, 200)
        self.assertEqual(edited["sourceId"], main_id)
        self.assertEqual(self.probe.calls[-1][3], "synthetic-next-key")
        self.assertEqual(self.store.resolve_key(request["protocol"], request["baseUrl"], None, main_id),
                         "synthetic-next-key")

    def test_id_scoped_probe_and_discovery_do_not_send_keys_to_another_endpoint(self):
        main_id = self.store.save_api("responses", "https://saved.test/v1", "model-a", "synthetic-key")
        for endpoint in ({"protocol": "responses", "baseUrl": "https://other.test/v1"},
                         {"protocol": "anthropic", "baseUrl": "https://saved.test/v1"}):
            with self.subTest(endpoint=endpoint):
                self.backend.model_source_list({"id": main_id, **endpoint})
                self.assertIsNone(self.probe.calls[-1][3])
                self.backend.model_source_test({"id": main_id, "model": "model-a", **endpoint})
                self.assertIsNone(self.probe.calls[-1][3])
                with self.assertRaises(ModelSourceUnavailable):
                    self.backend.model_source_reveal_key({"id": main_id, **endpoint})


class MultiModelStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        runtime = root / ".local" / "real-client-runtime"
        self.path = runtime / "api-model-source.json"
        self.store = ModelSourceStore(
            self.path, root=root,
            protect=lambda key: b"protected:" + key[::-1].encode("utf-8"),
            unprotect=lambda data: data.removeprefix(b"protected:").decode("utf-8")[::-1],
        )

    def activate(self, model, key="synthetic-key"):
        return self.store.save_api("responses", "https://example.test/v1", model, key,
                                   context_tokens=8192)

    def test_main_stamp_fields_survive_v1_upgrade_and_first_write_archives_v1(self):
        def fingerprint(model):
            identity = json.dumps(["api-source-v1", "responses", "https://example.test/v1", model],
                                  ensure_ascii=False, separators=(",", ":"))
            return hashlib.sha256(identity.encode("utf-8")).hexdigest()

        # A genuine version 1 document, as the previous release wrote it.
        first = "a" * 32
        encrypted = base64.b64encode(self.store.protect("synthetic-key-a")).decode("ascii")
        v1_doc = {"version": 1, "sourceId": first, "selectedMode": "api",
                  "api": {"protocol": "responses", "baseUrl": "https://example.test/v1",
                          "model": "model-a", "contextTokens": 8192,
                          "timeoutMs": DEFAULT_MODEL_TIMEOUT_MS, "encryptedKey": encrypted}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(v1_doc), encoding="utf-8")
        v1_bytes = self.path.read_bytes()
        reopened = ModelSourceStore(
            self.path, root=self.root, initial_concurrency=6,
            protect=self.store.protect, unprotect=self.store.unprotect)
        self.assertEqual(reopened.saved_selection()["sourceId"], first)
        self.assertEqual(reopened.saved_selection()["api"]["encryptedKey"], encrypted)
        second = reopened.save_api("responses", "https://example.test/v1", "model-b",
                                   "synthetic-key-b")
        backup = json.loads((self.path.parent / "api-model-source.v1.json").read_text(encoding="utf-8"))
        self.assertEqual(backup, v1_doc)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8"))["version"], 2)
        listing = reopened.models()
        self.assertEqual([item["model"] for item in listing["models"]], ["model-a", "model-b"])
        self.assertEqual(listing["roles"], {"main": second, "judge": None, "image": None})
        self.assertEqual(listing["models"][0]["concurrency"], 6)
        # The upgraded main keeps the original cache identity.
        self.assertEqual(reopened.saved_selection()["sourceId"], second)
        self.assertEqual(reopened.saved_selection()["sourceIds"],
                         {fingerprint("model-a"): first, fingerprint("model-b"): second})
        # Re-activating model-a restores its original id from the fingerprint table.
        self.assertEqual(reopened.save_api("responses", "https://example.test/v1", "model-a",
                                           "synthetic-key-a"), first)

    def test_role_assignment_fallback_and_main_protection(self):
        main_id = self.activate("model-a", "key-a")
        judge_id = self.store.save_model(name="备用", protocol="anthropic",
                                         base_url="https://judge.test/v1", model="judge-model",
                                         key="judge-key", context_tokens=16384)
        self.store.set_roles(judge=judge_id)
        self.assertEqual(self.store.models()["roles"]["judge"], judge_id)
        self.assertEqual(self.store.resolve_key("anthropic", "https://judge.test/v1", None), "judge-key")
        with self.assertRaises(ValueError):
            self.store.delete_model(main_id)
        self.store.delete_model(judge_id)
        self.assertEqual(self.store.models()["roles"]["judge"], None)
        self.assertEqual([item["model"] for item in self.store.models()["models"]], ["model-a"])
        with self.assertRaises(ValueError):
            self.store.delete_model(judge_id)
        with self.assertRaises(ValueError):
            self.store.set_roles(judge=main_id, image="0" * 32)

    def test_blank_key_reuses_same_endpoint_ciphertext_without_reencryption(self):
        main_id = self.activate("model-a", "key-a")
        original = self.store.models()["models"][0]
        self.assertTrue(original["hasKey"])
        # Editing name/concurrency of the main entry keeps identity and ciphertext.
        edited = self.store.save_model(main_id, name="主力", concurrency=6,
                                       protocol="responses", base_url="https://example.test/v1",
                                       model="model-a")
        self.assertEqual(edited, main_id)
        entry = self.store.models()["models"][0]
        self.assertEqual(entry["name"], "主力")
        self.assertEqual(entry["concurrency"], 6)
        self.assertEqual(self.store.resolve_key("responses", "https://example.test/v1", None), "key-a")

    def test_builtin_judge_registration_and_public_masking(self):
        main_id = self.activate("model-a", "key-a")
        builtin_id = self.store.ensure_builtin("julia-1", "本地判断模型", "julia-1")
        self.assertEqual(builtin_id, self.store.ensure_builtin("julia-1", "本地判断模型", "julia-1"))
        self.store.set_roles(judge=builtin_id)
        public = json.dumps(self.store.public())
        self.assertNotIn("key-a", public)
        self.assertNotIn("encryptedKey", public)
        entries = {item["model"]: item for item in self.store.models()["models"]}
        self.assertEqual(entries["julia-1"]["concurrency"], 1)
        self.assertNotIn("hasKey", entries["julia-1"])
        with self.assertRaises(ValueError):
            self.store.set_roles(main=builtin_id)

    def test_save_model_edits_require_known_id_and_main_keeps_role(self):
        main_id = self.activate("model-a", "key-a")
        with self.assertRaises(ValueError):
            self.store.save_model("1" * 32, protocol="responses",
                                  base_url="https://example.test/v1", model="model-a")
        self.store.save_model(name="改名", protocol="responses",
                              base_url="https://example.test/v1", model="model-a")
        self.assertEqual(self.store.models()["roles"]["main"], main_id)
        self.assertEqual(self.store.models()["models"][0]["name"], "改名")

    def test_first_saved_api_model_becomes_main_and_reopens(self):
        main_id = self.store.save_model(protocol="responses", base_url="https://first.test/v1",
                                         model="first-model", key="synthetic-key")
        reopened = ModelSourceStore(self.path, root=self.root,
                                    protect=self.store.protect, unprotect=self.store.unprotect)
        self.assertEqual(reopened.models()["roles"]["main"], main_id)
        self.assertEqual(reopened.saved_selection()["sourceId"], main_id)

    def test_v1_empty_identity_table_does_not_lose_the_current_source_id(self):
        main_id = self.activate("model-a")
        current = self.store.saved_selection()["api"]
        original = {"version": 1, "selectedMode": "api", "sourceId": main_id,
                    "api": current, "sourceIds": {}}
        self.path.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(self.store.save_api("responses", "https://example.test/v1", "model-a", None), main_id)
        self.assertEqual(self.store.saved_selection()["sourceId"], main_id)

    def test_builtin_only_store_can_be_emptied_and_then_configured(self):
        builtin_id = self.store.ensure_builtin("julia-1", "本地判断模型", "julia-1")
        self.store.set_roles(judge=builtin_id)
        self.store.delete_model(builtin_id)
        self.assertEqual(self.store.models(), {
            "models": [], "roles": {"main": None, "judge": None, "image": None}})
        main_id = self.store.save_model(protocol="responses", base_url="https://first.test/v1",
                                         model="first-model", key="synthetic-key")
        self.assertEqual(self.store.models()["roles"]["main"], main_id)

    def test_api_model_can_be_added_after_a_builtin_is_registered(self):
        self.store.ensure_builtin("julia-1", "本地判断模型", "julia-1")
        main_id = self.store.save_model(protocol="responses", base_url="https://first.test/v1",
                                         model="first-model")
        self.assertEqual(self.store.models()["roles"]["main"], main_id)

    def test_unconfigured_v1_profile_migrates_without_a_fake_api_model(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        original = {"version": 1, "selectedMode": "local", "api": None}
        self.path.write_text(json.dumps(original), encoding="utf-8")
        self.assertEqual(self.store.models(), {
            "models": [], "roles": {"main": None, "judge": None, "image": None}})
        self.store.ensure_builtin("julia-1", "本地判断模型", "julia-1")
        self.assertEqual(json.loads(self.path.with_name("api-model-source.v1.json").read_text()), original)

    def test_editing_into_an_existing_identity_does_not_corrupt_ids(self):
        first = self.activate("first-model")
        second = self.store.save_model(protocol="responses", base_url="https://example.test/v1",
                                       model="second-model")
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.save_model(second, protocol="responses", base_url="https://example.test/v1",
                                   model="first-model")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.store.models()["roles"]["main"], first)

    def test_model_limit_rejects_additions_without_breaking_existing_settings(self):
        self.activate("model-0")
        for index in range(1, 16):
            self.store.save_model(protocol="responses", base_url="https://example.test/v1",
                                   model=f"model-{index}")
        original = self.path.read_bytes()
        for add in (lambda: self.store.save_model(protocol="responses", base_url="https://example.test/v1",
                                                  model="model-16"),
                    lambda: self.store.ensure_builtin("julia-1", "本地判断模型", "julia-1")):
            with self.assertRaises(ValueError):
                add()
            self.assertEqual(self.path.read_bytes(), original)
            self.assertEqual(len(self.store.models()["models"]), 16)


if __name__ == "__main__":
    unittest.main()
