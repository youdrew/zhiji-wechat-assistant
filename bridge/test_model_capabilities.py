"""Unified alias discovery and real local gateway IPC, using only synthetic metadata/providers."""
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_gateway  # noqa: E402
from core_contracts import model_fingerprint  # noqa: E402
from core_gateway import GatewayUnavailable, ModelGatewayHost  # noqa: E402
from core_services import CoreServices  # noqa: E402
from model_capabilities import (CACHE_TTL, FAILURE_TTL, MAX_RESPONSE_BYTES, REQUEST_TIMEOUT,
                                ModelCapabilitiesUnavailable, UnifiedModelCapabilities, _NoRedirect)  # noqa: E402
from plugin_api import ApiError  # noqa: E402
from usage_ledger import UsageLedger  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin-sdk" / "python"))
from kyf_plugin.models import ModelClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_KEY = "fixture-key-not-a-credential"


def model(**overrides):
    return {"id": "local-default", "unified_gateway": True, "ready": True,
            "actual_model": "synthetic-glm", "backend_id": "fixture-a", "route_revision": 1,
            "context_length": 1048576, "max_output_tokens": 32768, "supports_vision": False, **overrides}


class Response(io.BytesIO):
    pass


class Opener:
    def __init__(self):
        self.item = model()
        self.error = None
        self.requests = []
        self.raw = None

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        return Response(self.raw if self.raw is not None else json.dumps({"data": [self.item]}).encode())


class Fixture:
    def __init__(self):
        self.now = 0.0
        self.opener = Opener()
        self.capabilities = UnifiedModelCapabilities(clock=lambda: self.now, opener=self.opener)
        self.api = {"protocol": "chat_completions", "baseUrl": "http://127.0.0.1:1/v1",
                    "model": "local-default", "contextTokens": 1000000, "encryptedKey": "synthetic-ciphertext"}
        self.core = CoreServices.__new__(CoreServices)
        self.core.model_settings = lambda: {"values": {"maxConcurrentTasks": 4}}
        self.core._model_capabilities = self.capabilities
        self.core.backend = SimpleNamespace(model_source_store=SimpleNamespace(
            saved_selection=lambda: {"sourceId": "fixture-service", "api": self.api},
            resolve_key=lambda *_args: SYNTHETIC_KEY))

    def advance(self, seconds=CACHE_TTL + .01):
        self.now += seconds


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def test_only_reserved_chat_completions_alias_is_discovered(self):
        f = self.fixture
        for fields in ({"model": "synthetic-glm"}, {"protocol": "ollama"}, {"protocol": "anthropic"}):
            self.assertIsNone(f.capabilities.lookup({**f.api, **fields}, SYNTHETIC_KEY))
        self.assertEqual(f.opener.requests, [])

    def test_bounded_authenticated_metadata_request_and_credential_sensitive_cache(self):
        f = self.fixture
        first = f.capabilities.lookup(f.api, SYNTHETIC_KEY)
        request, timeout = f.opener.requests[0]
        self.assertEqual(request.full_url, f.api["baseUrl"] + "/models")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SYNTHETIC_KEY)
        self.assertEqual(timeout, REQUEST_TIMEOUT)
        first["actual_model"] = "mutated-by-caller"
        self.assertEqual(f.capabilities.lookup(f.api, SYNTHETIC_KEY)["actual_model"], "synthetic-glm")
        self.assertEqual(len(f.opener.requests), 1)
        f.capabilities.lookup(f.api, "second-fixture-key")
        self.assertEqual(len(f.opener.requests), 2)
        self.assertNotIn(SYNTHETIC_KEY, repr(f.capabilities.cached))

    def test_fixed_alias_switch_clamps_context_and_changes_route_identity(self):
        f = self.fixture
        first = f.core._gateway_services()[0]
        self.assertEqual(first["contextTokens"], 1000000)
        self.assertEqual(first["actualModel"], "synthetic-glm")
        self.assertEqual(first["maxOutputTokens"], 32768)
        self.assertFalse(first["supportsVision"])
        f.opener.item = model(actual_model="synthetic-small", backend_id="fixture-b", route_revision=2,
                              context_length=65536, max_output_tokens=8192, supports_vision=True)
        f.advance()
        second = f.core._gateway_services()[0]
        for name in ("id", "model", "baseUrl"):
            self.assertEqual(first[name], second[name])
        self.assertEqual(second["contextTokens"], 65536)
        self.assertEqual(second["actualModel"], "synthetic-small")
        self.assertEqual(second["maxOutputTokens"], 8192)
        self.assertTrue(second["supportsVision"])
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["stamp"], second["stamp"])
        self.assertEqual(f.api["contextTokens"], 1000000, "saved preference was mutated")

    def test_user_context_is_an_upper_bound_and_route_revision_invalidates_same_model(self):
        f = self.fixture
        f.api["contextTokens"] = 32768
        first = f.core._gateway_services()[0]
        self.assertEqual(first["contextTokens"], 32768)
        f.opener.item["route_revision"] += 1
        f.advance()
        second = f.core._gateway_services()[0]
        self.assertEqual(second["contextTokens"], 32768)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(first["stamp"], second["stamp"])
        f.api["contextTokens"] = None
        self.assertEqual(f.core._gateway_services()[0]["contextTokens"], 1000000)

    def test_cache_endpoint_identity_is_stable_across_routes_but_not_endpoint_or_context_edits(self):
        f = self.fixture
        original = f.core._gateway_services()[0]
        f.opener.item["route_revision"] += 1
        f.advance()
        routed = f.core._gateway_services()[0]
        self.assertEqual(original["cacheFingerprint"], routed["cacheFingerprint"])
        self.assertNotEqual(original["fingerprint"], routed["fingerprint"])
        self.assertNotEqual(original["stamp"], routed["stamp"])
        f.api["baseUrl"] = "http://127.0.0.1:2/v1"
        endpoint = f.core._gateway_services()[0]
        self.assertEqual(endpoint["id"], original["id"])
        self.assertNotEqual(endpoint["cacheFingerprint"], routed["cacheFingerprint"])
        f.api["contextTokens"] = 32768
        context = f.core._gateway_services()[0]
        self.assertNotEqual(context["cacheFingerprint"], endpoint["cacheFingerprint"])
        host = ModelGatewayHost.__new__(ModelGatewayHost)
        host.ensure = lambda: 1
        host._refresh_services = lambda: [context]
        public = host.services()[0]
        self.assertEqual(public["cacheFingerprint"], context["cacheFingerprint"])
        self.assertNotIn("apiKey", public)
        self.assertNotIn("baseUrl", public)

    def test_normal_models_keep_legacy_identity_and_make_no_metadata_request(self):
        f = self.fixture
        f.api["model"] = "synthetic-normal"
        service = f.core._gateway_services()[0]
        self.assertEqual(service["fingerprint"], model_fingerprint("chat_completions", f.api["baseUrl"],
                                                                   "synthetic-normal", 1000000))
        self.assertNotIn("actualModel", service)
        self.assertEqual(f.opener.requests, [])

    def test_unknown_output_limit_remains_null_without_an_invented_cap(self):
        f = self.fixture
        f.opener.item = model(actual_model="synthetic-unknown-output", max_output_tokens=None)
        service = f.core._gateway_services()[0]
        self.assertIsNone(service["maxOutputTokens"])
        self.assertEqual(service["contextTokens"], 1000000)

    def test_draining_and_network_failures_never_reuse_previous_ready_route(self):
        f = self.fixture
        self.assertTrue(f.core._gateway_services())
        f.opener.item["ready"] = False
        f.advance()
        with self.assertRaises(GatewayUnavailable) as caught:
            f.core._gateway_services()
        self.assertEqual(caught.exception.code, "upstream-unavailable")
        count = len(f.opener.requests)
        with self.assertRaises(GatewayUnavailable):
            f.core._gateway_services()
        self.assertEqual(len(f.opener.requests), count)
        f.opener.item["ready"] = True
        f.advance(FAILURE_TTL + .01)
        self.assertTrue(f.core._gateway_services())
        f.opener.error = TimeoutError("fixture failure containing sensitive detail")
        f.advance()
        with self.assertRaises(GatewayUnavailable) as caught:
            f.core._gateway_services()
        self.assertNotIn("sensitive detail", str(caught.exception))

    def test_marker_and_valid_schema_are_required_even_on_first_request(self):
        for change in ({"unified_gateway": False}, {"ready": False}, {"actual_model": None},
                       {"backend_id": ""}, {"route_revision": True}, {"context_length": 4095},
                       {"max_output_tokens": True}, {"max_output_tokens": 1048577}, {"supports_vision": "false"}):
            with self.subTest(change=change):
                f = Fixture()
                f.opener.item.update(change)
                with self.assertRaises(ModelCapabilitiesUnavailable):
                    f.capabilities.lookup(f.api, SYNTHETIC_KEY)
        for raw in (b"{}", b"not-json", json.dumps({"data": []}).encode(),
                    json.dumps({"data": [model(), model()]}).encode(), b"x" * (MAX_RESPONSE_BYTES + 1)):
            with self.subTest(raw_size=len(raw)):
                f = Fixture()
                f.opener.raw = raw
                with self.assertRaises(ModelCapabilitiesUnavailable):
                    f.capabilities.lookup(f.api, SYNTHETIC_KEY)

    def test_proxy_is_bypassed_and_redirects_are_not_followed(self):
        with patch("model_capabilities.urllib.request.build_opener") as build:
            UnifiedModelCapabilities()
        proxy, redirect = build.call_args.args
        self.assertEqual(proxy.proxies, {})
        self.assertIsInstance(redirect, _NoRedirect)
        self.assertIsNone(redirect.redirect_request(None, None, None, None, None, None))

    def test_plugin_receives_clear_503_on_upstream_unavailability(self):
        f = self.fixture
        f.core.gateway = Mock()
        f.core.gateway.services.side_effect = GatewayUnavailable("upstream-unavailable", "正在切换模型")
        with self.assertRaises(ApiError) as caught:
            f.core.model_services("fixture-plugin")
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "upstream-unavailable"))


class ModelsProvider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.server.seen.append((self.path, self.headers.get("Authorization")))
        body = json.dumps({"data": [{"id": self.server.marker}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class SDKDiscoveryTests(unittest.TestCase):
    def test_dynamic_alias_rediscovers_each_use_without_an_extra_cache_delay(self):
        core = Mock()
        core.model_services.side_effect = [
            {"services": [{"model": "local-default", "actualModel": "fixture-a", "stamp": "route-1"}]},
            {"services": [{"model": "local-default", "actualModel": "fixture-b", "stamp": "route-2"}]},
        ]
        client = ModelClient(SimpleNamespace(core=core))
        self.assertEqual(client.service()["actualModel"], "fixture-a")
        self.assertEqual(client.service()["actualModel"], "fixture-b")
        self.assertEqual(core.model_services.call_count, 2)

    def test_normal_services_keep_ten_second_sdk_cache(self):
        core = Mock()
        core.model_services.return_value = {"services": [{"model": "fixture-normal", "stamp": "normal"}]}
        client = ModelClient(SimpleNamespace(core=core))
        with patch("kyf_plugin.models.time.monotonic", return_value=1.0):
            client.service()
        with patch("kyf_plugin.models.time.monotonic", return_value=10.9):
            client.service()
        self.assertEqual(core.model_services.call_count, 1)
        with patch("kyf_plugin.models.time.monotonic", return_value=11.1):
            client.service()
        self.assertEqual(core.model_services.call_count, 2)


class GatewayCapabilityIPCTests(unittest.TestCase):
    """Real Node config.applied messages are required; write completion alone cannot pass."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.fixture = Fixture()
        self.providers = []
        for marker in ("provider-a", "provider-b"):
            server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsProvider)
            server.marker, server.seen = marker, []
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.providers.append(server)
        self.fixture.api["baseUrl"] = self.base(0)
        self.ledger = UsageLedger(Path(self.temp.name) / "usage.sqlite3")
        self.host = ModelGatewayHost(ROOT, services=self.fixture.core._gateway_services, ledger=self.ledger)
        self.host.token("add", hashlib.sha256(b"fixture-execution-token").hexdigest(),
                        {"plugin": "fixture-plugin", "task": "fixture-task", "execution": "fixture-execution"})

    def tearDown(self):
        self.host.close()
        self.ledger.close()
        for server in self.providers:
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    def base(self, index):
        return f"http://127.0.0.1:{self.providers[index].server_port}/v1"

    def get_models(self, url):
        request = urllib.request.Request(url + "/models", headers={"Authorization": "Bearer fixture-execution-token"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=5) as response:
            return json.loads(response.read())["data"][0]["id"]

    def test_actual_route_change_is_acknowledged_before_new_service_is_returned(self):
        first = self.host.services()[0]
        first_configuration = self.host._configuration
        self.assertEqual(self.get_models(first["url"]), "provider-a")
        self.assertNotIn("apiKey", first)
        self.fixture.opener.item = model(actual_model="synthetic-small", backend_id="fixture-b", route_revision=2,
                                         context_length=65536, max_output_tokens=8192)
        self.fixture.advance()
        second = self.host.services()[0]
        self.assertEqual(second["model"], "local-default")
        self.assertEqual(second["contextTokens"], 65536)
        self.assertNotEqual(first_configuration, self.host._configuration)
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(self.host.pending, {})
        # A transport target change additionally proves Node applied the config,
        # rather than merely accepting bytes in its input pipe.
        self.fixture.api["baseUrl"] = self.base(1)
        third = self.host.services()[0]
        self.assertEqual(self.get_models(third["url"]), "provider-b")
        self.assertEqual(self.providers[1].seen[0][1], "Bearer " + SYNTHETIC_KEY)

    def test_draining_clears_node_route_and_recovery_requires_fresh_metadata(self):
        service = self.host.services()[0]
        self.fixture.opener.item["ready"] = False
        self.fixture.advance()
        with self.assertRaises(GatewayUnavailable) as caught:
            self.host.services()
        self.assertEqual(caught.exception.code, "upstream-unavailable")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get_models(service["url"])
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()
        self.fixture.opener.item["ready"] = True
        self.fixture.advance(FAILURE_TTL + .01)
        self.assertEqual(self.get_models(self.host.services()[0]["url"]), "provider-a")

    def test_unchanged_service_does_not_send_repeated_config(self):
        self.host.services()
        with patch.object(self.host, "_send", wraps=self.host._send) as send:
            self.host.services()
        send.assert_not_called()

    def test_missing_apply_ack_is_bounded_and_fails_generation(self):
        self.host.services()
        self.fixture.opener.item["route_revision"] += 1
        self.fixture.advance()
        original = self.host._send

        def drop_config(message, channel=None):
            if message.get("type") == "config":
                sent = threading.Event()
                sent.set()  # The pipe write succeeded, but there was no Node apply acknowledgment.
                return sent
            return original(message, channel)

        with patch.object(self.host, "_send", side_effect=drop_config), patch.object(core_gateway, "CONFIG_TIMEOUT", .05):
            with self.assertRaises(GatewayUnavailable) as caught:
                self.host.services()
        self.assertEqual(caught.exception.code, "gateway-config-timeout")
        self.assertFalse(self.host.ready.is_set())
        self.assertEqual(self.host.pending, {})


if __name__ == "__main__":
    unittest.main()
