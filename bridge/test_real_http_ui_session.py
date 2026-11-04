"""The X-KYF-UI-Session guard on the loopback server (B3), with a synthetic backend."""

import http.client
import json
import sys
import threading
import types
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from real_http import UI_SESSION_HEADER, make_handler
from test_core_ui_host import ACCOUNT, FRIEND, OTHER, FakeScope, Harness, ACCOUNT_ID


class StubFriends:
    def __init__(self):
        self.calls = []

    def overview(self, account, user):
        self.calls.append(("overview", account, user))
        return {"account": account, "user": user, "overview": True}

    def drafts(self, account, user):
        self.calls.append(("drafts", account, user))
        return {"account": account, "user": user, "drafts": []}

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, *args))
            return {"ok": True}
        return record


class StubCore:
    def __init__(self, ui):
        self.ui = ui
        self.rpc_calls = []
        self.supervisor = types.SimpleNamespace(rpc=self._rpc, plugin_for_token=lambda token: None)

    def require_plugin(self, plugin, account):
        return None

    def _rpc(self, plugin, method, params, context):
        self.rpc_calls.append((plugin, method, params, context))
        return {"echo": params}


class StubBackend:
    def __init__(self, ui):
        self.friends = StubFriends()
        self._core = StubCore(ui)
        self._data = types.SimpleNamespace(friends=self.friends)
        self.chat_calls = []
        self.source = types.SimpleNamespace(media_asset=self.media_asset)

    def core_services(self):
        return self._core

    def account_data(self):
        return self._data

    def history_search(self, account, user, **query):
        self.chat_calls.append(("history", account, user, query))
        return {"account": account, "user": user, "messages": [{"id": "media"}]}

    def media_asset(self, user, message_id, variant, *, expected_account):
        self.chat_calls.append(("media", expected_account, user, (message_id, variant)))
        return types.SimpleNamespace(data=b"synthetic-media", mime="video/mp4")


class GuardRouteTests(unittest.TestCase):
    def setUp(self):
        self.h = Harness(self, scope=FakeScope(accounts=frozenset({ACCOUNT_ID}), contacts={(ACCOUNT_ID, FRIEND)}))
        self.assertEqual(self.h.open()["status"], "opened")
        self.token = self.h.token()
        self.backend = StubBackend(self.h.service)
        self.start(self.backend)

    def start(self, backend):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(backend))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server = server

    def request(self, method, path, body=None, token=None, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            payload = json.dumps(body).encode("utf-8") if body is not None else None
            headers = {"Content-Type": "application/json; charset=utf-8"}
            headers.update(extra_headers or {})
            if token is not None:
                headers[UI_SESSION_HEADER] = token
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                return response.status, json.loads(raw)
            except ValueError:
                return response.status, raw
        finally:
            connection.close()

    def scoped(self, path, user=FRIEND):
        return f"{path}?account={ACCOUNT}&user={user}"

    def test_requests_without_header_are_unchanged(self):
        status, body = self.request("GET", self.scoped("/api/friend/drafts", OTHER))
        self.assertEqual((status, body["drafts"]), (200, []))
        status, body = self.request("POST", "/api/plugins/rpc", {"plugin": "kyf.advisor", "method": "ask",
                                                                 "params": {"q": 1}})
        self.assertEqual((status, body["result"]["echo"]), (200, {"q": 1}))
        status, _ = self.request("GET", "/api/mcp/nothing")
        self.assertNotEqual(status, 403)

    def test_matching_read_passes(self):
        status, body = self.request("GET", self.scoped("/api/friend/overview"), token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(body["overview"])

    def test_other_contact_is_forbidden(self):
        status, body = self.request("GET", self.scoped("/api/friend/overview", OTHER), token=self.token)
        self.assertEqual((status, body), (403, {"error": "ui-session-forbidden"}))
        self.assertEqual(self.backend.friends.calls, [])

    def test_account_level_settings_are_forbidden_for_a_per_contact_window(self):
        # R2-03: accountProfile is account-wide; the per-contact window gets 403, no handler call.
        status, body = self.request("GET", self.scoped("/api/friend/settings"), token=self.token)
        self.assertEqual((status, body), (403, {"error": "ui-session-forbidden"}))
        self.assertEqual(self.backend.friends.calls, [])
        status, _ = self.request("GET", self.scoped("/api/friend/settings"))
        self.assertEqual(status, 200)  # the user's own window is unchanged

    def test_plugin_listing_and_main_client_code_are_forbidden(self):
        for path in ("/api/plugins", "/index.html", "/app.js"):
            status, body = self.request("GET", path, token=self.token)
            self.assertEqual((status, body), (403, {"error": "ui-session-forbidden"}), path)

    def test_memory_write_and_friend_open_are_read_only(self):
        for path in ("/api/friend/memory", "/api/friend/open", "/api/tasks"):
            status, body = self.request("POST", path, {"account": ACCOUNT, "user": FRIEND, "id": "m"},
                                        token=self.token)
            self.assertEqual((status, body), (403, {"error": "ui-session-read-only"}), path)
        self.assertEqual(self.backend.friends.calls, [])

    def test_rpc_to_other_plugin_or_non_page_method_is_refused(self):
        status, body = self.request("POST", "/api/plugins/rpc", {"plugin": "kyf.advisor", "method": "page.x",
                                                                 "account": ACCOUNT, "user": FRIEND},
                                    token=self.token)
        self.assertEqual((status, body["error"]), (403, "ui-session-forbidden"))
        status, body = self.request("POST", "/api/plugins/rpc", {"plugin": "kyf.friend-analysis",
                                                                 "method": "scheme.estimate",
                                                                 "account": ACCOUNT, "user": FRIEND},
                                    token=self.token)
        self.assertEqual((status, body["error"]), (403, "ui-session-read-only"))
        self.assertEqual(self.backend._core.rpc_calls, [])

    def test_post_body_is_still_readable_after_guard(self):
        params = {"ids": ["a", "b"], "text": "中文" * 50}
        status, body = self.request("POST", "/api/plugins/rpc", {"plugin": "kyf.friend-analysis",
                                                                 "method": "page.personaEvidence", "params": params,
                                                                 "account": ACCOUNT, "user": FRIEND},
                                    token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["result"]["echo"], params)
        self.assertEqual(self.backend._core.rpc_calls[0][3], {"account": ACCOUNT, "user": FRIEND})

    def test_other_plugin_page_is_refused(self):
        status, body = self.request("GET", "/plugin-pages/kyf.advisor/ui/index.html", token=self.token)
        self.assertEqual((status, body), (403, {"error": "ui-session-forbidden"}))

    def test_static_chatui_file_is_served(self):
        status, body = self.request("GET", "/friend.html", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn(b"<", body)

    def test_chat_window_can_read_only_its_history_and_media_ranges(self):
        self.assertEqual(self.h.open("contact.conversation")["status"], "opened")
        token = self.h.token()
        for asset in ("history.html", "style.css", "media.css", "history-panel.css", "media.js", "history-panel.js"):
            self.assertEqual(self.request("GET", "/" + asset, token=token)[0], 200, asset)
        history = self.scoped("/api/history/search") + "&q=synthetic&before=cursor"
        status, body = self.request("GET", history, token=token)
        self.assertEqual((status, body["messages"]), (200, [{"id": "media"}]))
        self.assertEqual(self.backend.chat_calls[0][3]["before"], "cursor")
        media = self.scoped("/api/media") + "&id=media&variant=playback"
        self.assertEqual(self.request("GET", media, token=token, extra_headers={"Range": "bytes=2-5"}),
                         (206, b"nthe"))
        self.assertEqual(self.backend.chat_calls[1], ("media", ACCOUNT, FRIEND, ("media", "playback")))

        for path in (history.replace(ACCOUNT, "another_account"), history.replace(FRIEND, OTHER),
                     media.replace(FRIEND, OTHER), "/api/media?user=" + FRIEND,
                     "/api/accounts", "/api/sessions", "/index.html", "/friend.html", "/api/model-traces/state",
                     "/plugin-sdk/web/plugin-sdk.js", "/plugin-pages/kyf.friend-analysis/ui/index.html"):
            self.assertEqual(self.request("GET", path, token=token), (403, {"error": "ui-session-forbidden"}), path)
        for path in ("/api/plugins/rpc", "/api/usage/clear", "/api/wechat-send/prepare"):
            self.assertEqual(self.request("POST", path, {}, token), (403, {"error": "ui-session-read-only"}), path)
        self.assertEqual(len(self.backend.chat_calls), 2)
        self.assertEqual(self.h.service.close_all(), 2)
        self.assertEqual(self.request("GET", history, token=token)[0], 403)

    def test_unknown_or_revoked_token_and_missing_ui_service(self):
        status, body = self.request("GET", self.scoped("/api/friend/overview"), token="zus_" + "1" * 64)
        self.assertEqual((status, body), (403, {"error": "ui-session-forbidden"}))
        self.h.service.on_version_change("c1")
        status, _ = self.request("GET", self.scoped("/api/friend/overview"), token=self.token)
        self.assertEqual(status, 403)
        self.backend._core.ui = None
        status, body = self.request("GET", "/friend.html", token=self.token)
        self.assertEqual((status, body), (403, {"error": "ui-session-forbidden"}))
        status, _ = self.request("GET", "/friend.html")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
