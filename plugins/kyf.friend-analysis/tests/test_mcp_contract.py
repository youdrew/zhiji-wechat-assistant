"""MCP ↔ plugin contract on real plugin output: the fields Core's MCP tools read exist and mean what they say.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The bridge tests stub the plugin's answers; here the real 好友标签 / 好友时间线 functions answer
(synthetic chat, scripted model) behind Core's real ``McpServices`` → ``McpAnalysisBridge`` path.
The shim below routes RPCs exactly as ``main.py`` does, which is checked against its source.
"""
import re
import unittest
from pathlib import Path

import harness  # noqa: F401  (puts bridge, the SDK and the plugin backend on the import path)
import friend_tags
import friend_timeline_view as tl
from contracts import AdvisorError
from core_mcp_proxy import McpError
from core_mcp_tasks import McpAnalysisBridge
from core_plugin_process import PluginError
from kyf_plugin.actor import actor, agent_name
from test_analysis import ACCOUNT, USER
from test_core_mcp_runtime import headers, make_services, pair
from test_timeline import TimelineCase

SUBJECT = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": USER}]
BACKEND = Path(__file__).resolve().parents[1] / "backend"


class PluginShim:
    """``supervisor.rpc`` answered by the plugin's own page functions, routed as in ``main.py``."""

    def __init__(self, pages):
        self.pages, self.calls = pages, []
        self.routes = {
            "page.tags": lambda a, u, p, c: friend_tags.view(pages, a, u),
            "page.tagEdit": lambda a, u, p, c: friend_tags.edit(pages, a, u, p, actor(c), agent_name(c)),
            "page.tagUndoAgent": lambda a, u, p, c: friend_tags.undo(pages, a, u, p, actor(c), "agent"),
            "page.timeline": lambda a, u, p, c: tl.view(pages, a, u, p),
            "page.timelineEdit": lambda a, u, p, c: tl.edit(pages, a, u, p, c),
            "page.timelineUndoAgent": lambda a, u, p, c: tl.undo(pages, a, u, p, c, "agent"),
        }

    def rpc(self, plugin, method, params=None, context=None, timeout=None):
        self.calls.append((plugin, method, dict(context or {})))
        try:
            return self.routes[method](context["account"], context["user"], params or {}, context)
        except AdvisorError as exc:
            raise PluginError(exc.code, exc.message) from None


class Services:
    def __init__(self, supervisor):
        self.supervisor = supervisor

    def require_plugin(self, plugin, account=None):
        pass


class Bridge(McpAnalysisBridge):
    def resolve_account(self, account_id):
        if account_id != "id-A":
            raise McpError(404, "account-unknown", "该账号不是本机已保存账号")
        return ACCOUNT


class ContractTests(TimelineCase):
    def setUp(self):
        super().setUp()
        self.analyse()
        self.shim = PluginShim(self.friends.pages)
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.mcp.analysis = Bridge(Services(self.shim), "unused")
        _created, token = pair(self.mcp, name="合成 Agent")
        self.principal = self.mcp.authenticate(headers(token["token"]))

    def call(self, action, params, request_id=None):
        body = {"action": action, "subjects": SUBJECT, "params": params}
        if request_id:
            body["requestId"] = request_id
        return self.mcp.call(self.principal, body)

    def test_main_routes_the_methods_the_shim_uses(self):
        main = (BACKEND / "main.py").read_text(encoding="utf-8")
        for method, function in (("page.tags", "friend_tags.view"), ("page.tagEdit", "friend_tags.edit"),
                                 ("page.tagUndoAgent", "friend_tags.undo"), ("page.timeline", "friend_timeline_view.view"),
                                 ("page.timelineEdit", "friend_timeline_view.edit"),
                                 ("page.timelineUndoAgent", "friend_timeline_view.undo")):
            body = main.split('@plugin.method("%s")' % method, 1)[1].split("@plugin.method", 1)[0]
            self.assertIn(function + "(", body, method)
        self.assertRegex(main.split('@plugin.method("page.tagUndoAgent")', 1)[1], re.compile(r'"agent"\)\)'))

    def test_tag_tools_read_versions_and_edit_and_undo_as_the_agent(self):
        read = self.call("zhiji_get_friend_analysis", {"section": "tags"})
        for field in ("version", "habits", "tags", "categories", "undo"):
            self.assertIn(field, read)
        self.assertIsInstance(read["version"], int)
        habit = read["habits"][0]["key"]
        edited = self.call("zhiji_edit_friend_tags", {"action": "add", "name": "爱爬山", "category": "运动健身",
                                                      "expectedVersion": read["version"]}, "t-1")
        self.assertEqual((edited["version"], edited["id"], edited["label"]),
                         (read["version"] + 1, "爱爬山", "添加标签：爱爬山"))
        hidden = self.call("zhiji_edit_friend_tags", {"action": "hide", "key": habit,
                                                      "expectedVersion": edited["version"]}, "t-2")
        self.assertEqual((hidden["version"], hidden["id"]), (edited["version"] + 1, habit))
        self.assertEqual(self.shim.calls[-1][2]["actor"], "agent")
        self.assertEqual(self.shim.calls[-1][2]["agent"], "合成 Agent")
        with self.assertRaises(McpError) as caught:
            self.call("zhiji_edit_friend_tags", {"action": "confirm", "name": "爱爬山",
                                                 "expectedVersion": read["version"]}, "t-3")
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "version-conflict"))
        page = friend_tags.view(self.friends.pages, ACCOUNT, USER)
        self.assertEqual(page["undo"]["agent"]["count"], 2)
        self.assertEqual(page["undo"]["user"]["count"], 0, "MCP writes land on the Agent's stack only")
        undone = self.call("zhiji_undo_friend_tags", {"expectedVersion": hidden["version"]}, "t-4")
        self.assertEqual((undone["version"], undone["undone"], undone["skipped"]),
                         (hidden["version"] + 1, hidden["label"], None))
        friend_tags.edit(self.friends.pages, ACCOUNT, USER, {"action": "delete", "name": "爱爬山"})
        skipped = self.call("zhiji_undo_friend_tags", {"expectedVersion": undone["version"] + 1}, "t-5")
        self.assertIsNone(skipped["undone"])
        self.assertEqual(set(skipped["skipped"]), {"label", "reason"})
        self.assertEqual(skipped["skipped"]["label"], "添加标签：爱爬山")

    def test_timeline_tools_read_pages_and_edit_and_undo_as_the_agent(self):
        read = self.call("zhiji_get_friend_analysis", {"section": "timeline"})
        for field in ("version", "events", "nextOffset", "eventsTotal", "tracks", "periods", "undo"):
            self.assertIn(field, read)
        self.assertTrue(all("id" in event and "title" in event for event in read["events"]))
        target = read["events"][0]
        edited = self.call("zhiji_edit_friend_timeline", {"target": "event", "action": "edit", "id": target["id"],
                                                          "fields": {"title": "合成改名"},
                                                          "expectedVersion": read["version"]}, "e-1")
        self.assertEqual((edited["version"], edited["id"]), (read["version"] + 1, target["id"]))
        self.assertTrue(edited["label"])
        with self.assertRaises(McpError) as caught:
            self.call("zhiji_undo_friend_timeline", {"expectedVersion": read["version"]}, "e-2")
        self.assertEqual(caught.exception.code, "version-conflict")
        undone = self.call("zhiji_undo_friend_timeline", {"expectedVersion": edited["version"]}, "e-3")
        self.assertEqual((undone["undone"], undone["skipped"]), (edited["label"], None))
        self.assertGreater(undone["version"], edited["version"])


if __name__ == "__main__":
    unittest.main()
