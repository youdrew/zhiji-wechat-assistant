"""Core backend: account scope, chat browsing, model profile and retired-analysis reads.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import hashlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

from backend_contracts import AccountChangedError
from backend_service import Backend
from instance_identity import instance_id
from legacy_portrait import read_legacy_portrait, result_database
from model_source import ModelSourceStore
from real_http import ROOT, make_handler
from wechat_source import WeChatSource, avatar_candidates, message_id


class SyntheticSource:
    def __init__(self, workdir, account="account-a"):
        self.workdir = workdir
        self.account = account
        self.db = object()
        self.rows = {}

    def identity(self):
        return self.account, self.workdir

    def messages(self, user, limit, offset=0):
        rows = self.rows.get(user, [])
        return rows[-(limit + offset):len(rows) - offset if offset else None][-limit:]

    def stats(self, user, member=None):
        rows = self.rows.get(user, [])
        return len(rows), len(rows), []

    def media(self, user, stable_id):
        return None

    def add(self, user, count, start=0, sender=None, text=None):
        rows = self.rows.setdefault(user, [])
        sender = sender or ("member" if user.endswith("@chatroom") else user)
        for number in range(start, start + count):
            row = {"local_id": number + 1, "sort_seq": number + 1, "server_id": number + 1}
            rows.append({"id": message_id(self.account, user, "message__message_0.db", row),
                         "side": "self" if sender == "me" else "other", "kind": "text",
                         "text": text or f"synthetic {number}", "senderId": sender,
                         "_sort": [number + 1, "message__message_0.db", number + 1]})


class FakeProbe:
    def __init__(self):
        self.tests = []

    def model_list(self, protocol, base_url, api_key):
        return {"models": [{"id": "synthetic-model", "contextTokens": 32768}], "supported": True}

    def model_test(self, protocol, base_url, api_key, model):
        self.tests.append((protocol, base_url, api_key, model))
        return {"ok": True, "latencyMs": 12}


def model_store(root):
    return ModelSourceStore(Path(root) / ".local" / "real-client-runtime" / "api-model-source.json", root=Path(root),
                            protect=lambda value: value.encode("utf-8")[::-1],
                            unprotect=lambda value: value[::-1].decode("utf-8"))


def serve(backend, control_token=None):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(backend, control_token=control_token))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
        conn.request(method, path, body, headers or {})
        response = conn.getresponse()
        status, payload = response.status, response.read()
        conn.close()
        return status, payload

    def stop():
        server.shutdown()
        server.server_close()
        thread.join()
    return server, request, stop


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = SyntheticSource(Path(self.temp.name))
        self.probe = FakeProbe()
        self.backend = Backend(self.source, model_source_store=model_store(self.temp.name), model_probe=self.probe)

    def test_account_switch_during_messages_read_never_returns_mixed_payload(self):
        self.source.add("friend", 1)
        original = self.source.messages

        def switch_after_read(*args, **kwargs):
            rows = original(*args, **kwargs)
            self.source.account = "account-b"
            return rows

        self.source.messages = switch_after_read
        with self.assertRaises(AccountChangedError):
            self.backend.messages("friend", 80)

    def test_messages_returns_page_without_full_stats_scan(self):
        self.source.add("friend", 2)
        self.source.stats = Mock(side_effect=AssertionError("full stats called"))
        result = self.backend.messages("friend", 80)
        self.assertEqual(len(result["messages"]), 2)
        self.assertIsNone(result["total"])
        self.source.stats.assert_not_called()

    def test_no_live_account_fails_closed_at_http_boundary(self):
        source = WeChatSource(factory=lambda **_kwargs: None, active_account_locator=lambda: None)
        _server, request, stop = serve(Backend(source, model_source_store=model_store(self.temp.name)))
        try:
            status, health = request("GET", "/api/health")
            health = json.loads(health)
            self.assertEqual(status, 200)
            self.assertEqual((health["ok"], health["data"]["state"]), (False, "account-unavailable"))
            self.assertNotIn("account", health["data"])
            self.assertNotIn("model", health)
            status, sessions = request("GET", "/api/sessions")
            sessions = json.loads(sessions)
            self.assertEqual((status, sessions["error"]), (503, "AccountUnavailableError"))
            self.assertNotIn("account", sessions)
        finally:
            stop()

    def test_stable_message_identity(self):
        row = {"local_id": 1, "sort_seq": 5, "server_id": 9}
        self.assertNotEqual(message_id("a", "user", "shard0", row), message_id("b", "user", "shard0", row))
        self.assertNotEqual(message_id("a", "user", "shard0", row), message_id("a", "room", "shard0", row))
        self.assertNotEqual(message_id("a", "user", "shard0", row), message_id("a", "user", "shard1", row))

    def test_health_and_http_contract_failures(self):
        self.assertTrue(self.backend.health()["ok"])
        self.source.db = None
        self.assertEqual(self.backend.health()["data"]["state"], "idle")
        server, request, stop = serve(self.backend)
        try:
            health = json.loads(request("GET", "/api/health")[1])
            self.assertEqual(health["version"], "real-ui-1")
            self.assertEqual(health["instanceId"], instance_id(ROOT))
            self.assertEqual(request("GET", "/api/health", headers={"Host": f"localhost:{server.server_port}"})[0], 200)
            self.assertEqual(request("GET", "/api/messages?user=friend&limit=-1")[0], 400)
            self.assertEqual(request("GET", "/api/media?user=friend&id=missing")[0], 404)
            valid = {"Content-Type": "application/json; charset=UTF-8", "Origin": f"http://localhost:{server.server_port}"}
            body = json.dumps({"account": "account-a", "users": ["friend"]})
            self.assertEqual(request("POST", "/api/messages/batch", json.dumps({"texts": ["no"]}), valid)[0], 400)
            self.assertEqual(request("POST", "/api/messages/batch", body, {**valid, "Origin": "null"})[0], 403)
            self.assertEqual(request("POST", "/api/messages/batch", body, {"Content-Type": "text/plain"})[0], 415)
            self.assertEqual(request("GET", "/", headers={"Host": f"evil.example:{server.server_port}"})[0], 403)
            self.assertEqual(request("GET", "/api/health", headers={"Origin": "https://evil.example"})[0], 403)
            # Retired analysis routes are gone rather than silently accepted.
            for path in ("/api/analysis?user=friend", "/api/profile?user=friend", "/api/model-insights?user=friend",
                         "/api/analysis-cache", "/api/runtime", "/api/local-model", "/api/analysis-workers"):
                self.assertEqual(request("GET", path)[0], 404, path)
            for path in ("/api/analyze", "/api/predict-reply", "/api/model-portrait", "/api/analysis-scope/clear"):
                self.assertEqual(request("POST", path, "{}", valid)[0], 404, path)
        finally:
            stop()

    def test_model_profile_is_api_only_and_tested_before_saving(self):
        self.assertEqual(self.backend.model_source()["mode"], "none")
        with self.assertRaises(ValueError):
            self.backend.model_source_activate({"mode": "local"})
        changed = []
        self.backend.add_model_listener(lambda: changed.append(self.backend.model_source_revision))
        saved = self.backend.model_source_activate({"mode": "api", "protocol": "chat_completions",
            "baseUrl": "https://api.example/v1", "apiKey": "synthetic-key", "model": "synthetic-model",
            "contextTokens": 32768})
        self.assertEqual((saved["mode"], saved["api"]["model"], saved["api"]["hasKey"]),
                         ("api", "synthetic-model", True))
        self.assertEqual(self.probe.tests, [("chat_completions", "https://api.example/v1", "synthetic-key",
                                             "synthetic-model")])
        self.assertEqual(changed, [1])
        # Saving the same profile again reuses the validated key without another probe.
        self.backend.model_source_activate({"mode": "api", "protocol": "chat_completions",
            "baseUrl": "https://api.example/v1", "model": "synthetic-model", "contextTokens": 65536})
        self.assertEqual(len(self.probe.tests), 1)
        self.assertEqual(self.backend.model_source()["api"]["contextTokens"], 65536)
        cleared = self.backend.model_source_clear_key({})
        self.assertFalse(cleared["api"]["hasKey"])
        listed = self.backend.model_source_list({"protocol": "chat_completions", "baseUrl": "https://api.example/v1"})
        self.assertEqual(listed["models"], [{"id": "synthetic-model", "contextTokens": 32768}])


class PluginHostRouteTests(unittest.TestCase):
    """Host routes added for plugin pages: picked-file grants and large plugin RPC bodies (2026-10-13)."""

    TOKEN = "ab" * 32

    def setUp(self):
        from core_services import CoreServices
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        folder = base / "plugins" / "kyf.grant-test"
        folder.mkdir(parents=True)
        (folder / "main.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
        (folder / "plugin.json").write_text(json.dumps({
            "schema": 1, "id": "kyf.grant-test", "name": "授权测试", "version": "1.0.0", "core": {"api": [1]},
            "runtime": {"type": "python", "entry": "main.py"}}), encoding="utf-8")
        self.picked = base / "picked.md"
        self.picked.write_text("# skill", encoding="utf-8")
        self.backend = Backend(SyntheticSource(base), model_source_store=model_store(base))
        self.core = self.backend._core = CoreServices(self.backend, ROOT, "http://127.0.0.1:0", local=base / ".local",
                                                      bundled_dir=base / "plugins", local_dir=base / "none")
        self.addCleanup(self.core.close)
        self.server, self.request, stop = serve(self.backend, self.TOKEN)
        self.addCleanup(stop)

    def grant(self, body, token=None, content_type="application/json; charset=utf-8"):
        headers = {"Content-Type": content_type}
        if token is not None:
            headers["X-KYF-Control-Token"] = token
        status, payload = self.request("POST", "/api/plugins/file-grant", json.dumps(body), headers)
        return status, json.loads(payload)

    def test_file_grant_requires_the_control_token_and_hides_the_path(self):
        body = {"plugin": "kyf.grant-test", "path": str(self.picked)}
        self.assertEqual(self.grant(body)[0], 403)
        self.assertEqual(self.grant(body, "cd" * 32)[0], 403)
        self.assertEqual(self.grant(body, self.TOKEN, "text/plain")[0], 415)
        status, granted = self.grant(body, self.TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(set(granted), {"token", "label"})
        self.assertEqual(granted["label"], "picked.md")
        self.assertNotIn(str(self.picked), json.dumps(granted, ensure_ascii=False))
        self.assertEqual(self.core.files.claim("kyf.grant-test", granted["token"])["path"], str(self.picked))
        self.assertEqual(self.grant({**body, "plugin": "kyf.unknown"}, self.TOKEN)[0], 404)
        self.assertEqual(self.grant({**body, "path": "relative.md"}, self.TOKEN)[0], 400)
        self.assertEqual(self.grant({**body, "extra": 1}, self.TOKEN)[0], 400)

    def test_file_grant_is_refused_without_a_configured_control_token(self):
        _server, request, stop = serve(self.backend)
        try:
            status, _payload = request("POST", "/api/plugins/file-grant",
                                       json.dumps({"plugin": "kyf.grant-test", "path": str(self.picked)}),
                                       {"Content-Type": "application/json", "X-KYF-Control-Token": ""})
            self.assertEqual(status, 403)
        finally:
            stop()

    def test_plugin_rpc_accepts_bodies_up_to_one_mebibyte(self):
        headers = {"Content-Type": "application/json; charset=utf-8"}

        def rpc(size):
            body = json.dumps({"plugin": "kyf.missing", "method": "m", "params": {"text": "x" * size}})
            return self.request("POST", "/api/plugins/rpc", body, headers)[0]
        # Large prompts reach the plugin host (an unknown plugin is then 503, not a size error).
        self.assertEqual(rpc(200_000), 503)
        self.assertEqual(rpc(1024 * 1024 - 200), 503)
        self.assertEqual(rpc(1024 * 1024 + 1), 400)
        other = json.dumps({"account": "account-a", "users": ["friend"], "pad": "x" * 70_000})
        self.assertEqual(self.request("POST", "/api/messages/batch", other, headers)[0], 400)

    def test_plugin_pages_may_use_inline_styles_but_not_inline_scripts(self):
        from real_http import PLUGIN_PAGE_POLICY
        self.assertIn("style-src 'self' 'unsafe-inline'", PLUGIN_PAGE_POLICY)
        self.assertIn("script-src 'self';", PLUGIN_PAGE_POLICY)
        self.assertIn("connect-src 'none'", PLUGIN_PAGE_POLICY)

    def test_ollama_service_root_is_the_host_root_and_keeps_its_fingerprint(self):
        from types import SimpleNamespace
        from core_contracts import model_fingerprint
        from core_services import gateway_base
        for base in ("http://127.0.0.1:11434", "http://127.0.0.1:11434/", "http://127.0.0.1:11434/v1",
                     "http://127.0.0.1:11434/api/"):
            self.assertEqual(gateway_base("ollama", base), "http://127.0.0.1:11434")
        self.assertEqual(gateway_base("chat_completions", "https://api.example/v1"), "https://api.example/v1")
        api = {"protocol": "ollama", "baseUrl": "http://127.0.0.1:11434/v1", "model": "m", "contextTokens": 32768}
        self.backend.model_source_store = SimpleNamespace(
            saved_selection=lambda: {"sourceId": "svc", "api": api}, resolve_key=lambda *_args: None)
        service = self.core._gateway_services()[0]
        self.assertEqual(service["baseUrl"], "http://127.0.0.1:11434")
        self.assertEqual(service["fingerprint"], model_fingerprint("ollama", api["baseUrl"], "m", 32768))
        self.assertEqual(service["timeoutMs"], 3_600_000)
        api["timeoutMs"] = 7_200_000
        changed = self.core._gateway_services()[0]
        self.assertEqual(changed["timeoutMs"], 7_200_000)
        self.assertEqual(changed["stamp"], service["stamp"])
        self.assertEqual(changed["fingerprint"], service["fingerprint"])

    def test_retired_assistant_routes_are_gone(self):
        headers = {"Content-Type": "application/json; charset=utf-8"}
        self.assertEqual(self.request("GET", "/api/advisor/catalog")[0], 404)
        self.assertEqual(self.request("POST", "/api/advisor/run", "{}", headers)[0], 404)


class LegacyPortraitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def database(self):
        path = result_database("account-a", self.root)
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE batch_progress_v1 (account TEXT, session TEXT, base_version TEXT, subject TEXT, "
                     "batch_version TEXT, state_json TEXT)")
        conn.execute("CREATE TABLE api_portrait_v1 (account TEXT, session TEXT, source_id TEXT, subject TEXT, "
                     "available_json TEXT, processed INTEGER, portrait_json TEXT, resume_json TEXT)")
        conn.execute("CREATE TABLE api_source_meta_v1 (account TEXT, source_id TEXT, protocol TEXT, model TEXT)")
        return conn

    def test_missing_database_and_group_chat_have_no_portrait(self):
        self.assertEqual(read_legacy_portrait("account-a", "friend", self.root), {"available": False, "items": []})
        self.assertEqual(read_legacy_portrait("account-a", "room@chatroom", self.root)["available"], False)

    def test_saved_local_and_api_statistics_are_read_without_writes(self):
        state = {"count": 4, "emotion": {"平静": 3.0, "开心": 1.0}, "intent": {"提问": 4.0},
                 "words": {"周末": 3, "电影": 1}, "styleCount": 2,
                 "style": {"socialEnergy": 1.0, "humor": .5, "composure": 2.0, "initiative": 0.0,
                           "care": 1.0, "affection": .2}, "scoreCount": 2, "scoreSum": 1.0, "scoreWeighted": .5}
        with closing(self.database()) as conn, conn:
            conn.execute("INSERT INTO batch_progress_v1 VALUES ('account-a','friend','v','friend','b',?)",
                         (json.dumps(state),))
            conn.execute("INSERT INTO batch_progress_v1 VALUES ('account-a','other','v','other','b',?)",
                         (json.dumps({**state, "count": 9}),))
            conn.execute("INSERT INTO api_portrait_v1 VALUES ('account-a','friend','s1','friend',?,3,?,?)",
                         (json.dumps({"targetTextCount": 5}), json.dumps({"summary": "old"}),
                          json.dumps({"portraitStatistics": {"state": {**state, "count": 3}}})))
            conn.execute("INSERT INTO api_source_meta_v1 VALUES ('account-a','s1','responses','synthetic-model')")
        path = result_database("account-a", self.root)
        before = path.read_bytes()
        result = read_legacy_portrait("account-a", "friend", self.root)
        self.assertTrue(result["available"])
        local, api = result["items"]
        self.assertEqual((local["source"], local["analyzed"]), ("local", 4))
        self.assertEqual(local["emotion"][0], {"label": "平静", "probability": .75})
        self.assertEqual(local["keywords"], [{"word": "周末", "count": 3}])
        self.assertEqual(local["traits"][0], {"key": "socialEnergy", "label": "表达活力", "val": 50})
        self.assertIsNotNone(local["affinity"])
        self.assertEqual((api["label"], api["analyzed"], api["total"]), ("API · synthetic-model", 3, 5))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(read_legacy_portrait("account-b", "friend", self.root)["available"], False)


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def test_session_sync_full_order_metadata_and_safe_preview(self):
        workdir = Path(self.temp.name)
        contact = workdir / "contact__contact.db"
        session = workdir / "session__session.db"
        with closing(sqlite3.connect(contact)) as conn, conn:
            conn.execute("CREATE TABLE contact (username TEXT,nick_name TEXT,remark TEXT,small_head_url TEXT,big_head_url TEXT)")
            conn.execute("INSERT INTO contact VALUES ('me','Self','','','')")
            conn.execute("INSERT INTO contact VALUES ('filehelper','','','https://img.example/small','https://img.example/big')")
        with closing(sqlite3.connect(session)) as conn, conn:
            conn.execute("CREATE TABLE SessionTable (username TEXT,unread_count INT,summary TEXT,last_timestamp INT,"
                         "last_msg_sender TEXT,last_sender_display_name TEXT,sort_timestamp INT,is_hidden INT,"
                         "last_msg_type INT,last_msg_sub_type INT,is_top INT)")
            sender_cases = {0: "me", 1: "", 2: None}
            conn.executemany("INSERT INTO SessionTable VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
                (f"friend{index}", index, "hello", 1000 + index, sender_cases.get(index, "sender"),
                 "Synthetic sender", index + 1, 0,
                 49 if index < 2 else 1, 5 if index == 0 else 6 if index == 1 else 0, 0)
                for index in range(205)])
            conn.execute("INSERT INTO SessionTable VALUES ('filehelper',2,'<msg>unsafe</msg>',0,'system','',999,0,3,0,1)")
            conn.execute("INSERT INTO SessionTable VALUES ('hidden',0,'hidden',1,'','',1000,1,1,0,0)")

        class FakeDB:
            account = "account-a"
            wxid = "me"
            _db_files = [("contact", "contact.db", None), ("session", "session.db", None)]

            def _open(self, rel):
                return sqlite3.connect(contact if rel == "contact" else session)

            def get_self_info(self):
                return {"username": "me"}

        source = WeChatSource(lambda: FakeDB(), lambda kind, content: (kind, content))
        sessions = source.sessions()
        self.assertEqual(len(sessions["sessions"]), 206)
        self.assertEqual([item["username"] for item in sessions["sessions"][:2]], ["filehelper", "friend204"])
        first = sessions["sessions"][0]
        self.assertEqual((first["name"], first["unreadCount"], first["preview"], first["time"]),
                         ("文件传输助手", 2, "[图片]", None))
        self.assertEqual(first["avatarCandidates"], ["https://img.example/small", "https://img.example/big"])
        self.assertEqual((first["sortTimestamp"], first["lastMsgType"], first["pinned"]), (999, 3, True))
        self.assertEqual([item["preview"] for item in sessions["sessions"][-2:]], ["[文件]", "[链接]"])
        by_username = {item["username"]: item for item in sessions["sessions"]}
        self.assertIs(by_username["friend0"]["lastMessageFromSelf"], True)
        self.assertIs(by_username["friend3"]["lastMessageFromSelf"], False)
        self.assertIsNone(by_username["friend1"]["lastMessageFromSelf"])
        self.assertIsNone(by_username["friend2"]["lastMessageFromSelf"])
        self.assertEqual(by_username["friend0"]["lastSender"], "Synthetic sender")
        with closing(sqlite3.connect(session)) as conn, conn:
            conn.execute("ALTER TABLE SessionTable DROP COLUMN last_msg_type")
            conn.execute("ALTER TABLE SessionTable DROP COLUMN last_msg_sub_type")
            conn.execute("ALTER TABLE SessionTable DROP COLUMN is_top")
        fallback = source.sessions()["sessions"][0]
        self.assertEqual((fallback["lastMsgType"], fallback["lastMsgSubType"], fallback["pinned"], fallback["preview"]),
                         (None, None, None, "[消息]"))

    def test_precise_local_image_resolution_and_rejection(self):
        workdir = Path(self.temp.name)
        user = "friend"
        table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
        base = workdir / "account" / "msg" / "attach" / hashlib.md5(user.encode()).hexdigest()
        base.mkdir(parents=True)
        contact = workdir / "contact__contact.db"
        with closing(sqlite3.connect(contact)) as conn, conn:
            conn.execute("CREATE TABLE contact (username TEXT,nick_name TEXT,remark TEXT,small_head_url TEXT,big_head_url TEXT)")
            conn.execute("INSERT INTO contact VALUES ('friend','Friend','','','')")
        image_bytes = {"a" * 32: b"\xff\xd8\xffone", "b" * 32: b"\x89PNG\r\n\x1a\ntwo"}
        for index, md5 in enumerate(image_bytes):
            (base / (md5 + ".dat")).write_bytes(b"local")
            with closing(sqlite3.connect(workdir / f"message__message_{index}.db")) as conn, conn:
                conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
                conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (3,'friend')")
                conn.execute(f"CREATE TABLE {table} (local_id INT,local_type INT,real_sender_id INT,create_time INT,"
                             "message_content BLOB,compress_content BLOB,server_id INT,sort_seq INT,packed_info_data BLOB)")
                conn.execute(f"INSERT INTO {table} VALUES (1,3,3,1000,'',NULL,?,?,?)",
                             (index + 10, index + 1, md5.encode()))

        class FakeDB:
            account = "account-a"
            wxid = "me"
            account_dir = str(workdir / "account")
            _db_files = [("contact", "contact.db", None)]

            def _open(self, rel):
                return sqlite3.connect(contact)

            def get_self_info(self):
                return {"username": "me"}

            def _msg_conns(self, _user):
                return [(sqlite3.connect(workdir / f"message__message_{index}.db"), table) for index in range(2)]

            def _msg_type_name(self, _type):
                return "图片"

            def _friendly_content(self, content, _type):
                return content.decode()

        class FakeDownloader:
            path_override = None

            def __init__(self, _db):
                pass

            def _img_md5(self, row):
                return row["packed_info"].decode()

            def _find_dat(self, _user, md5, _created, thumbnail=False):
                if self.path_override:
                    return str(self.path_override)
                path = base / (md5 + ".dat")
                return str(path) if path.exists() else None

            def decrypt_image(self, path, aes_key=None, xor_key=None):
                return image_bytes[Path(path).stem]

            def _derive_cfg_key(self):
                return None

            def _load_persisted_key(self):
                return None

            def _probe_ct(self, _path):
                return b"synthetic-probe"

            def _scan_aes_key(self, monitor):
                raise AssertionError("media lookup must never scan process memory")

            def _derive_xor_key(self, _path):
                return 0x88

        database = FakeDB()
        media_root = workdir / "media-data"
        source = WeChatSource(lambda: database, lambda _kind, _content: ("image", "[图片]"), FakeDownloader,
                              media_storage_root=media_root)
        # These rejection checks exercise the live file, without a previously retained copy.
        self.assertEqual(source.media_settings(database.account, {"mode": "reference"})["mode"], "reference")
        messages = source.messages(user, 2)
        self.assertEqual(len(messages), 2)
        self.assertEqual(source.media(user, messages[0]["id"]), (image_bytes["a" * 32], "image/jpeg"))
        self.assertEqual(source.media(user, messages[1]["id"]), (image_bytes["b" * 32], "image/png"))
        for md5 in image_bytes:
            (base / (md5 + ".dat")).write_bytes(b"\x07\x08\x56\x32\x08\x07synthetic")
        self.assertIsNone(source.media(user, messages[0]["id"]))
        self.assertIsNone(source.media(user, messages[1]["id"]))
        self.assertEqual(source.media_reason.value, "local-key-unavailable")
        unavailable_source = WeChatSource(lambda: database, lambda _kind, _content: ("image", "[图片]"), FakeDownloader,
                                          media_storage_root=media_root)
        self.assertEqual(unavailable_source.media_settings(database.account)["mode"], "reference")
        unavailable_source.messages(user, 2)
        self.assertIsNone(unavailable_source.media(user, messages[0]["id"]))
        self.assertIsNone(unavailable_source.media(user, messages[1]["id"]))
        self.assertEqual(unavailable_source.media_reason.value, "local-key-unavailable")
        for md5 in image_bytes:
            (base / (md5 + ".dat")).write_bytes(b"local")
        self.assertIsNone(source.media("other", messages[0]["id"]))
        self.assertIsNone(source.media(user, "wrong"))
        database.account = "account-b"
        self.assertIsNone(source.media(user, messages[0]["id"]))
        database.account = "account-a"
        image_bytes["a" * 32] = b"<svg>unsafe</svg>"
        self.assertIsNone(source.media(user, messages[0]["id"]))
        image_bytes["a" * 32] = b"\xff\xd8\xff" + b"x" * (8 * 1024 * 1024)
        self.assertIsNone(source.media(user, messages[0]["id"]))
        image_bytes["a" * 32] = b"\xff\xd8\xffone"
        outside = workdir / "outside.dat"
        outside.write_bytes(b"local")
        FakeDownloader.path_override = outside
        self.assertIsNone(source.media(user, messages[0]["id"]))
        FakeDownloader.path_override = None
        (base / ("a" * 32 + ".dat")).write_bytes(b"x" * (8 * 1024 * 1024 + 1))
        self.assertIsNone(source.media(user, messages[0]["id"]))
        (base / ("a" * 32 + ".dat")).unlink()
        self.assertIsNone(source.media(user, messages[0]["id"]))

    def test_shard_sender_scope_and_compressed_text(self):
        workdir = Path(self.temp.name)
        contact = workdir / "contact__contact.db"
        conn = sqlite3.connect(contact)
        try:
            conn.execute("CREATE TABLE contact (username TEXT,nick_name TEXT,remark TEXT,small_head_url TEXT,big_head_url TEXT)")
            conn.executemany("INSERT INTO contact VALUES (?,?,?,?,?)", [
                ("me", "Self", "", "", ""),
                ("alice", "Alice", "", "https://img.example/small", "https://img.example/big"),
                ("bob", "Bob", "", "javascript:bad", "http://img.example/bob"),
                ("newsapp", "newsapp", "", "", ""),
                ("brandsessionholder", "", "", "", ""),
            ])
            conn.commit()
        finally:
            conn.close()
        user = "room@chatroom"
        table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
        for index, sender in enumerate(("alice", "bob")):
            file = workdir / f"message__message_{index}.db"
            conn = sqlite3.connect(file)
            try:
                conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
                conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (3,?)", (sender,))
                if index == 0:
                    conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (2,'bob')")
                else:
                    conn.execute("INSERT INTO Name2Id(rowid,user_name) VALUES (4,'me')")
                conn.execute(f"CREATE TABLE {table} (local_id INT,local_type INT,real_sender_id INT,create_time INT,message_content BLOB,compress_content BLOB,server_id INT,sort_seq INT)")
                conn.execute(f"INSERT INTO {table} VALUES (1,1,3,1000,?,?,?,7)", (b"", f"synthetic {index}".encode(), index + 1))
                if index == 0:
                    conn.execute(f"INSERT INTO {table} VALUES (2,1,2,1001,'synthetic sender two',NULL,3,8)")
                    conn.execute(f"INSERT INTO {table} VALUES (3,1,3,1001,'synthetic same sequence',NULL,5,8)")
                    conn.execute(f"INSERT INTO {table} VALUES (4,1,3,1001,'synthetic system',NULL,6,8)")
                else:
                    conn.execute(f"INSERT INTO {table} VALUES (2,1,4,1002,'synthetic self',NULL,4,9)")
                conn.commit()
            finally:
                conn.close()

        class FakeDB:
            account = "account-a"
            wxid = "me"
            _db_files = [("contact", "contact.db", None)]

            def __init__(self):
                self.workdir = str(workdir)

            def _open(self, rel):
                return sqlite3.connect(contact)

            def get_self_info(self):
                return {"username": "me"}

            def _msg_conns(self, username):
                return [(sqlite3.connect(workdir / f"message__message_{index}.db"), table) for index in range(2)]

            def _msg_type_name(self, _type):
                return "文本"

            def _friendly_content(self, content, _type):
                return content.decode()

        source = WeChatSource(lambda: FakeDB(), lambda _kind, content: ("system", "") if content == "synthetic system" else ("text", content))
        messages = source.messages(user, 6)
        self.assertEqual([item["senderId"] for item in messages], ["alice", "bob", "bob", "alice", "me"])
        self.assertEqual([item["side"] for item in messages], ["other", "other", "other", "other", "self"])
        self.assertEqual([item["text"] for item in messages][:2], ["synthetic 0", "synthetic 1"])
        self.assertEqual(len({item["id"] for item in messages}), 5)
        self.assertEqual([item["_sort"][2] for item in messages if item["_sort"][0] == 8], [2, 3])
        self.assertEqual([item["_sort"][2] for item in source.messages(user, 3)], [3, 2])
        self.assertEqual([item["_sort"][2] for item in source.messages(user, 2)], [2])
        self.assertEqual(source.stats(user, "bob")[:2], (2, 2))
        self.assertEqual(source.stats(user, "me")[:2], (1, 1))
        self.assertEqual(source.contact("alice")["avatarCandidates"],
                         ["https://img.example/small", "https://img.example/big"])
        self.assertEqual(source.contact("bob")["avatarCandidates"], ["http://img.example/bob"])
        self.assertEqual(source.contact("filehelper")["name"], "文件传输助手")
        self.assertEqual(source.contact("newsapp")["name"], "腾讯新闻")
        self.assertEqual(source.contact("brandsessionholder")["name"], "公众号消息")
        with closing(sqlite3.connect(contact)) as conn, conn:
            conn.execute("UPDATE contact SET remark='实际备注' WHERE username='newsapp'")
        self.assertEqual(source.contact("newsapp")["name"], "实际备注")
        self.assertEqual(messages[0]["senderAvatarCandidates"], source.contact("alice")["avatarCandidates"])
        self.assertEqual(avatar_candidates("file:///bad", "https://img.example/ok", "https://img.example/ok"),
                         ["https://img.example/ok"])
        highwater, cursor, paged = source.history_highwater(user), None, []
        while True:
            page, cursor = source.history_page(user, highwater, cursor, page_size=2)
            if cursor is None:
                break
            paged.extend(page)
        self.assertEqual([item["id"] for item in paged], [item["id"] for item in messages])


if __name__ == "__main__":
    unittest.main()
