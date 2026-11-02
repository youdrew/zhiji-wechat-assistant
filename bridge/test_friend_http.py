"""Loopback HTTP tests for the host 知己分析 routes with a synthetic source.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Modified 2026-10-13 (Apache-2.0): analysis, jobs and trends are served by the scheme plugin;
memory candidates arrive through Core's plugin intake.
"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from core_accounts import AccountDataService
from core_human import HumanService
from real_http import make_handler
from test_support import FakeSource, wait_until
from test_friend_service import ACCOUNT, PLUGIN, USER, evidence, message


class FriendHttpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = FakeSource(account=ACCOUNT)
        self.source.add_messages(USER, [message(1, "other", "我不吃香菜，一点都不行"), message(2, "self", "记住了")])
        self.accounts = AccountDataService(self.source, Path(temporary.name))
        self.addCleanup(self.accounts.shutdown)

        class Backend:
            def __init__(self, service):
                self.service = service

            def account_data(self):
                return self.service

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(Backend(self.accounts)))
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_port

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join(3)
        self.addCleanup(stop)

    def request(self, path, body=None, origin=None, query=None):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = origin
        if query is not None:
            path += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        try:
            response = urllib.request.urlopen(urllib.request.Request(self.base + path, data=data, headers=headers), timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def scope(self, **extra):
        return {"account": ACCOUNT, "user": USER, **extra}

    def propose_from_history(self):
        """The scheme plugin's candidate for the friend's statement, through Core's intake."""
        database = self.accounts.stores.account(ACCOUNT)
        row = database._row("SELECT * FROM context_messages WHERE user=? AND side='other'", (USER,))
        candidate = {"text": "对方不吃香菜", "tags": ["偏好与忌口"], "proposalKey": "http-build",
                     "evidence": [evidence(row, database.context(USER)["source_id"])]}
        HumanService(self.accounts.stores, source=self.source).submit_candidates(
            ACCOUNT, USER, PLUGIN, {"task": "task-http"}, [candidate])

    def test_routes_serve_memories_and_reject_bad_shapes(self):
        self.assertEqual(self.request("/api/friend/open", self.scope(), origin="https://foreign.example")[0], 403)
        status, overview = self.request("/api/friend/open", self.scope())
        self.assertEqual(status, 200, overview)
        self.assertTrue(wait_until(lambda: self.request("/api/friend/overview", query=self.scope())[1]["history"]["state"] == "ready"))
        for retired in ("/api/friend/job/start", "/api/friend/analysis/start"):
            self.assertEqual(self.request(retired, self.scope(kind="full"))[0], 404, "analysis runs are Core tasks")
        self.propose_from_history()
        status, listing = self.request("/api/friend/memories", query=self.scope(status="pending"))
        self.assertEqual(status, 200)
        self.assertEqual([item["text"] for item in listing["items"]], ["对方不吃香菜"])
        memory = listing["items"][0]
        status, evidence = self.request("/api/friend/evidence", query=self.scope(id=memory["id"], ordinal="0"))
        self.assertEqual((status, evidence["status"]), (200, "ok"))
        self.assertEqual(self.request("/api/friend/evidence", query=self.scope(id=memory["id"], ordinal="x"))[0], 400)
        status, located = self.request("/api/friend/locate", self.scope(id=memory["id"], ordinal=0))
        self.assertEqual(status, 200)
        self.assertEqual((located["account"], located["user"]), (ACCOUNT, USER))
        status, _ = self.request("/api/friend/memory", self.scope(action="confirm", id=memory["id"], expectedVersion=1))
        self.assertEqual(status, 200)
        stale = self.request("/api/friend/memory", self.scope(action="edit", id=memory["id"], text="改", tags=["爱好"],
                                                              expectedVersion=1))
        self.assertEqual(stale[0], 409)
        self.assertEqual(self.request("/api/friend/memory", self.scope(action="drop-table"))[0], 400)

    def test_other_contacts_groups_and_unknown_records_stay_isolated(self):
        self.request("/api/friend/open", self.scope())
        status, created = self.request("/api/friend/memory", self.scope(action="create", text="对方喜欢猫", tags=["爱好"]))
        self.assertEqual(status, 200)
        identifier = created["result"]["id"]
        other = {"account": ACCOUNT, "user": "wxid_someone_else"}
        self.assertEqual(self.request("/api/friend/memory", query={**other, "id": identifier})[0], 404)
        self.assertEqual(self.request("/api/friend/memory", {**other, "action": "delete", "id": identifier})[0], 404)
        self.assertEqual(self.request("/api/friend/overview", query={"account": ACCOUNT, "user": "1@chatroom"})[0], 400)
        self.assertNotEqual(self.request("/api/friend/overview", query={"account": "another-account", "user": USER})[0], 200)
        for retired in ("/api/friend/trends", "/api/friend/page/overview", "/api/friend/job"):
            self.assertEqual(self.request(retired, query=self.scope())[0], 404, "scheme pages come from the plugin")
        status, saved = self.request("/api/friend/draft", self.scope(entry="memory:new", generation=1, seq=1,
                                                                     payload={"text": "草稿"}))
        self.assertEqual((status, saved["saved"]), (200, True))
        self.assertEqual(self.request("/api/friend/drafts", query=other)[1]["drafts"], [])
        self.assertEqual(self.request("/api/friend/draft", self.scope(entry="../x", generation=1, seq=1, payload={}))[0], 400)
        status, settings = self.request("/api/friend/settings", self.scope(kind="goal", text="保持联系", expectedVersion=0))
        self.assertEqual((status, settings["goal"]["text"]), (200, "保持联系"))
        self.assertEqual(self.request("/api/friend/settings", query=other)[1]["goal"]["text"], "")


if __name__ == "__main__":
    unittest.main()
