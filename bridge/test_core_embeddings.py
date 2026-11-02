"""Local retrieval model boundaries; every asset and text here is synthetic."""
import hashlib
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin-sdk" / "python"))

from core_embeddings import EmbeddingError, EmbeddingService, _Redirects
from core_gateway import GatewayUnavailable, ModelGatewayHost
from plugin_api import ApiError, PREFIX, PluginAPI
from kyf_plugin.core import CoreClient


class Response(io.BytesIO):
    def __init__(self, data, *, status=200, headers=None, url="https://modelscope.cn/synthetic"):
        super().__init__(data)
        self.status, self.headers, self.url = status, headers or {}, url

    def geturl(self):
        return self.url


class EmbeddingAssetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.directory = self.base / "assets"
        self.data = {"model.onnx": b"synthetic-onnx", "tokenizer.json": b'{"test":true}'}
        self.manifest = {"schema": 1, "modelId": "test/model", "revision": "fixed-v1", "dimensions": 2,
            "allowedHosts": ["modelscope.cn"], "files": [{"name": name,
                "url": f"https://modelscope.cn/{name}", "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest()} for name, data in self.data.items()]}
        self.manifest_path = self.base / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.gateway = Mock()
        self.gateway.embedding.side_effect = lambda request, **_kw: {
            **{key: request[key] for key in ("modelId", "revision", "dimensions")},
            "vectors": [[1., 0.] for _ in request["texts"]]}
        self.opener = Mock()
        self.opener.open.side_effect = lambda request, **_kw: Response(self.data[request.full_url.rsplit("/", 1)[1]])
        self.service = EmbeddingService(self.base, self.directory, self.gateway,
            manifest_path=self.manifest_path, opener=self.opener)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def ready(self):
        self.directory.mkdir(exist_ok=True)
        for name, data in self.data.items():
            (self.directory / name).write_bytes(data)

    def finish(self):
        self.service.thread.join(5)
        self.assertFalse(self.service.thread.is_alive())
        return self.service.status()

    def test_status_does_not_download_or_create_directories(self):
        status = self.service.status()
        self.assertEqual((status["state"], status["ready"], status["downloadedBytes"]), ("missing", False, 0))
        self.assertFalse(self.directory.exists())
        self.opener.open.assert_not_called()
        self.gateway.embedding.assert_not_called()
        with self.assertRaises(EmbeddingError) as caught:
            self.service.embed(["合成文本"])
        self.assertEqual(caught.exception.code, "embedding-not-ready")
        self.opener.open.assert_not_called()

    def test_prepare_is_nonblocking_and_promotes_only_verified_assets(self):
        entered, release = threading.Event(), threading.Event()
        original = self.opener.open.side_effect

        def open_asset(request, **kw):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(request, **kw)

        self.opener.open.side_effect = open_asset
        self.assertEqual(self.service.prepare()["state"], "downloading")
        try:
            self.assertTrue(entered.wait(5))
            self.assertFalse((self.directory / "model.onnx").exists())
            worker = self.service.thread
            self.service.prepare()
            self.assertIs(self.service.thread, worker)
        finally:
            release.set()
        status = self.finish()
        self.assertTrue(status["ready"])
        self.assertEqual(status["downloadedBytes"], status["totalBytes"])
        self.assertFalse(list(self.directory.glob("*.part")))
        for name, data in self.data.items():
            self.assertEqual((self.directory / name).read_bytes(), data)
        self.service.prepare()
        self.assertEqual(self.opener.open.call_count, 2)

    def test_hash_mismatch_never_becomes_ready_and_can_retry(self):
        self.opener.open.side_effect = lambda request, **_kw: Response(b"x" * len(self.data[request.full_url.rsplit("/", 1)[1]]))
        self.service.prepare()
        status = self.finish()
        self.assertEqual(status["state"], "error")
        self.assertFalse((self.directory / "model.onnx").exists())
        self.assertFalse((self.directory / "model.onnx.part").exists())
        self.opener.open.side_effect = lambda request, **_kw: Response(self.data[request.full_url.rsplit("/", 1)[1]])
        self.service.prepare()
        status = self.finish()
        self.assertTrue(status["ready"])
        self.assertNotIn("error", status)

    def test_short_download_resumes_from_exact_range(self):
        self.directory.mkdir()
        name, data = next(iter(self.data.items()))
        (self.directory / (name + ".part")).write_bytes(data[:4])
        self.assertEqual(self.service.status()["downloadedBytes"], 4)

        def resume(request, **_kw):
            if request.full_url.endswith(name):
                self.assertEqual(request.get_header("Range"), "bytes=4-")
                return Response(data[4:], status=206, headers={"Content-Range": f"bytes 4-{len(data)-1}/{len(data)}"})
            return Response(self.data[request.full_url.rsplit("/", 1)[1]])

        self.opener.open.side_effect = resume
        self.service.prepare()
        self.assertTrue(self.finish()["ready"])
        self.assertEqual((self.directory / name).read_bytes(), data)

    def test_server_ignoring_range_replaces_partial(self):
        self.directory.mkdir()
        (self.directory / "model.onnx.part").write_bytes(b"synt")
        self.service.prepare()
        self.assertTrue(self.finish()["ready"])
        self.assertEqual((self.directory / "model.onnx").read_bytes(), self.data["model.onnx"])

    def test_remote_redirect_host_and_oversize_are_rejected(self):
        for response in (Response(self.data["model.onnx"], url="https://huggingface.co/model"),
                         Response(self.data["model.onnx"] + b"oversized"),
                         Response(self.data["model.onnx"], status=206, headers={"Content-Range": "bytes 4-5/6"})):
            with self.subTest(response=response):
                self.opener.open.side_effect = lambda *_args, **_kw: response
                self.service.prepare()
                self.assertEqual(self.finish()["state"], "error")
                self.assertFalse((self.directory / "model.onnx").exists())
        import urllib.request
        handler = _Redirects({"modelscope.cn"})
        with self.assertRaises(EmbeddingError):
            handler.redirect_request(urllib.request.Request("https://modelscope.cn/test"), None,
                302, "redirect", {}, "https://huggingface.co/model")

    def test_untrusted_error_text_is_not_exposed(self):
        self.opener.open.side_effect = RuntimeError("PRIVATE_NETWORK_DETAIL")
        self.service.prepare()
        self.assertNotIn("PRIVATE_NETWORK_DETAIL", json.dumps(self.finish()))

    def test_cached_assets_are_checked_after_replacement_and_error_clears(self):
        self.ready()
        self.assertTrue(self.service.status()["ready"])
        (self.directory / "model.onnx").write_bytes(b"corrupt")
        self.assertFalse(self.service.status()["ready"])
        self.service.state, self.service.error = "error", "old failure"
        self.ready()
        self.assertTrue(self.service.status()["ready"])
        self.assertNotIn("error", self.service.status())

    def test_embed_boundaries_priority_deadlines_and_identity(self):
        self.ready()
        result = self.service.embed(["查询"], "query", plugin="kyf.test")
        self.assertEqual(result["vectors"], [[1., 0.]])
        args, kwargs = self.gateway.embedding.call_args
        self.assertEqual((args[0]["plugin"], args[0]["kind"], kwargs["timeout"]), ("kyf.test", "query", 2))
        self.service.embed(["中" * 4000], "query")
        self.service.embed(["😀" * 2400] * 16)
        self.assertEqual(self.gateway.embedding.call_args.kwargs["timeout"], 30)
        for texts, kind in (([], "query"), (["x"] * 17, "passage"), (["中" * 2401], "passage"),
                            (["中" * 4001], "query"), ([" "], "query"), (["x\x00"], "query"),
                            ([False], "query"), (["x"], "remote")):
            with self.subTest(texts=str(texts)[:20], kind=kind), self.assertRaises(EmbeddingError):
                self.service.embed(texts, kind)

    def test_invalid_vector_and_wrong_model_results_are_rejected(self):
        self.ready()
        valid = {"modelId": "test/model", "revision": "fixed-v1", "dimensions": 2, "vectors": [[1., 0.]]}
        for changes in ({"modelId": "wrong"}, {"revision": "wrong"}, {"dimensions": 3},
                        {"vectors": [[0., 0.]]}, {"vectors": [[float("nan"), 1.]]},
                        {"vectors": [[float("inf"), 1.]]}, {"vectors": [[True, 0]]},
                        {"vectors": [[1.]]}, {"vectors": []}, {"vectors": [[10**400, 1]]}):
            self.gateway.embedding.side_effect = None
            self.gateway.embedding.return_value = valid | changes
            with self.subTest(changes=str(changes)[:70]), self.assertRaises(EmbeddingError):
                self.service.embed(["合成文本"])

    def test_account_recheck_discards_late_result_even_on_gateway_error(self):
        self.ready()
        check = Mock(side_effect=[None, RuntimeError("account-changed")])
        with self.assertRaisesRegex(RuntimeError, "account-changed"):
            self.service.embed(["合成文本"], check=check)
        self.gateway.embedding.side_effect = GatewayUnavailable()
        check = Mock(side_effect=[None, RuntimeError("account-changed")])
        with self.assertRaisesRegex(RuntimeError, "account-changed"):
            self.service.embed(["合成文本"], check=check)

    def test_invalid_manifest_cannot_redirect_or_escape_storage(self):
        for change in ({"name": "../escape"}, {"url": "http://modelscope.cn/test"},
                       {"url": "https://huggingface.co/test"}, {"sha256": "not-sha256"}):
            manifest = {**self.manifest, "files": [{**self.manifest["files"][0], **change}]}
            self.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.subTest(change=change), self.assertRaises(EmbeddingError):
                EmbeddingService(self.base, self.directory, self.gateway, manifest_path=self.manifest_path)


class GatewayEmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.channel = SimpleNamespace(failed=threading.Event())
        self.host = ModelGatewayHost(Path(__file__).parent, services=lambda: [], ledger=Mock(), traces=Mock())
        self.host._channel = self.channel
        self.host.ensure = Mock(return_value=123)
        self.host._send = Mock()

    def test_timeout_and_scope_cancellation_do_not_kill_shared_gateway(self):
        for check in (None, Mock(side_effect=[None, RuntimeError("account-changed")])):
            with self.subTest(check=check), self.assertRaises((GatewayUnavailable, RuntimeError)):
                self.host.embedding({"texts": ["synthetic"]}, timeout=.001, check=check)
            self.assertEqual(self.host._send.call_args.args[0]["type"], "embedding.cancel")
            self.assertFalse(self.channel.failed.is_set())
            self.assertFalse(self.host.pending)
        self.host.traces.assert_not_called()
        self.host.ledger.record.assert_not_called()

    def test_result_is_bound_to_its_waiter_and_provider_errors_are_sanitized(self):
        def send(message, channel):
            self.assertIs(channel, self.channel)
            waiter = self.host.pending[message["id"]]
            waiter["result"] = {"vectors": [[1.]]}
            waiter["event"].set()
        self.host._send.side_effect = send
        self.assertEqual(self.host.embedding({"texts": ["synthetic"]}), {"vectors": [[1.]]})
        self.assertFalse(self.host.pending)

        def failure(message, _channel):
            if message["type"] == "embedding":
                waiter = self.host.pending[message["id"]]
                waiter["result"] = {"error": {"code": "RAW_PRIVATE_DETAIL"}}
                waiter["event"].set()
        self.host._send.side_effect = failure
        with self.assertRaises(GatewayUnavailable) as caught:
            self.host.embedding({"texts": ["synthetic"]})
        self.assertNotIn("RAW_PRIVATE_DETAIL", str(caught.exception))

    def test_reader_accepts_only_current_generation_matching_control_kind(self):
        event = threading.Event()
        waiter = {"event": event, "result": None, "channel": self.channel, "kind": "embedding"}
        self.host.pending["e1"] = waiter
        self.host._fail_channel = Mock()
        stale = SimpleNamespace(failed=threading.Event(), process=SimpleNamespace(stdout=io.BytesIO(
            b'{"type":"embedding.result","id":"e1","result":{"stale":true}}\n')))
        self.host._read(stale)
        self.assertFalse(event.is_set())
        self.channel.process = SimpleNamespace(stdout=io.BytesIO(
            b'{"type":"probe.result","id":"e1","result":{"wrongKind":true}}\n'))
        self.host._read(self.channel)
        self.assertFalse(event.is_set())
        self.channel.process = SimpleNamespace(stdout=io.BytesIO(
            b'{"type":"embedding.result","id":"e1","result":{"vectors":[[1.0]]}}\n'))
        self.host._read(self.channel)
        self.assertTrue(event.is_set())
        self.assertEqual(waiter["result"], {"vectors": [[1.0]]})
        self.host.ledger.record.assert_not_called()

    def test_short_query_startup_wait_does_not_terminate_shared_gateway(self):
        self.channel.ready = threading.Event()
        self.host._alive = Mock(return_value=True)
        self.host._fail_channel = Mock()
        with self.assertRaises(GatewayUnavailable):
            ModelGatewayHost.ensure(self.host, timeout=0)
        self.host._fail_channel.assert_not_called()


class EmbeddingAPITests(unittest.TestCase):
    def setUp(self):
        self.core = SimpleNamespace(supervisor=SimpleNamespace(plugin_for_token=Mock(return_value="kyf.test")),
            scope=Mock(return_value={"account": "acct"}), ensure_account=Mock(), require_plugin=Mock(),
            registry=SimpleNamespace(enabled=Mock(return_value=True)), embeddings=Mock(),
            tasks=SimpleNamespace(verify=Mock(return_value={"account": "acct"})))
        self.api = PluginAPI(self.core)
        self.headers = {"Authorization": "Bearer synthetic-token"}

    def request(self, method, path, data):
        return self.api.handle(method, PREFIX + "embeddings/" + path,
            data if method == "GET" else {}, self.headers, data if method != "GET" else {})[1]

    def test_authenticated_account_bound_status_and_no_client_model_or_path(self):
        self.core.embeddings.status.return_value = {"ready": False}
        self.assertEqual(self.request("GET", "status", {"account": "acct"}), {"ready": False})
        self.core.embeddings.prepare.assert_not_called()
        for account in (None, "other"):
            with self.assertRaises(ApiError):
                self.request("POST", "prepare", {"account": account})
        for extra in ("url", "modelDir", "plugin", "modelId"):
            with self.subTest(extra=extra), self.assertRaises(ApiError):
                self.request("POST", "embed", {"account": "acct", "texts": ["合成文本"], extra: "attacker"})

    def test_prepare_and_embed_return_only_under_still_current_account(self):
        def replace():
            self.core.scope.return_value = {"account": "replacement"}
            return {"ready": True}
        self.core.embeddings.prepare.side_effect = replace
        with self.assertRaises(ApiError) as caught:
            self.request("POST", "prepare", {"account": "acct"})
        self.assertEqual(caught.exception.code, "account-changed")

    def test_authenticated_plugin_identity_and_revoked_execution_are_rechecked(self):
        self.headers["X-KYF-Execution"] = "synthetic-execution"
        def embed(texts, kind, *, plugin, check):
            self.assertEqual((texts, kind, plugin), (["合成文本"], "query", "kyf.test"))
            self.core.registry.enabled.return_value = False
            check()
        self.core.embeddings.embed.side_effect = embed
        with self.assertRaises(ApiError) as caught:
            self.request("POST", "embed", {"account": "acct", "texts": ["合成文本"], "kind": "query"})
        self.assertEqual(caught.exception.code, "plugin-disabled")
        self.core.registry.enabled.return_value = True
        self.core.tasks.verify.return_value = {"account": "other"}
        with self.assertRaises(ApiError) as caught:
            self.request("POST", "embed", {"account": "acct", "texts": ["合成文本"]})
        self.assertEqual(caught.exception.code, "scope-mismatch")

    def test_sdk_preserves_execution_bound_transport_and_bounded_routes(self):
        client = CoreClient("http://127.0.0.1/plugin-api/v1", "synthetic-token", "execution")
        client._request = Mock(return_value={"ready": False})
        client.embedding_status("acct")
        client._request.assert_called_with("GET", "embeddings/status", {"account": "acct"})
        client.embedding_prepare("acct")
        client._request.assert_called_with("POST", "embeddings/prepare", {}, {"account": "acct"})
        client.embed("acct", iter(["合成文本"]), kind="query")
        client._request.assert_called_with("POST", "embeddings/embed", {},
            {"account": "acct", "texts": ["合成文本"], "kind": "query"})


if __name__ == "__main__":
    unittest.main()
