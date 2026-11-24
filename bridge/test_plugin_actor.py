"""Call source (user / agent) on plugin RPCs: Core sets it, plugins read it, pages cannot forge it.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "plugin-sdk" / "python"))

import core_http  # noqa: E402
from core_mcp_tasks import agent_context  # noqa: E402
from kyf_plugin.actor import AGENT, USER, actor, agent_name  # noqa: E402


class RecordingCore:
    def __init__(self):
        self.calls = []
        self.supervisor = self

    def require_plugin(self, plugin, account=None):
        pass

    def rpc(self, plugin, method, params=None, context=None, timeout=None):
        self.calls.append((plugin, method, params, context))
        return {"actor": actor(context), "agent": agent_name(context)}


class ActorHelperTests(unittest.TestCase):
    def test_only_the_core_agent_context_reads_as_agent(self):
        self.assertEqual((actor(agent_context("甲", "好友1", "合成 Agent")),
                          agent_name(agent_context("甲", "好友1", "合成 Agent"))), (AGENT, "合成 Agent"))
        self.assertEqual(agent_name(agent_context("甲")), "")
        for context in ({"account": "甲", "user": "好友1"}, {}, None, "agent", {"actor": "Agent"},
                        {"actor": ["agent"]}, {"actor": "user", "agent": "冒充"}):
            self.assertEqual((actor(context), agent_name(context)), (USER, ""), context)

    def test_agent_names_are_single_line_and_bounded(self):
        name = agent_name({"actor": "agent", "agent": " 合成\n\tAgent " + "长" * 100})
        self.assertTrue(name.startswith("合成 Agent 长"))
        self.assertEqual(len(name), 64)
        self.assertEqual(agent_name({"actor": "agent", "agent": 7}), "")


class PageCallTests(unittest.TestCase):
    """A page call reaches the plugin as the user, whatever the page sends."""

    def setUp(self):
        self.core = RecordingCore()

    def page_rpc(self, request):
        return core_http.post(self.core, "/api/plugins/rpc", request)

    def test_page_context_never_carries_a_source(self):
        result = self.page_rpc({"plugin": "kyf.friend-analysis", "method": "page.timelineEdit",
                                "params": {"actor": "agent", "agent": "冒充"}, "account": "甲", "user": "好友1"})
        self.assertEqual(result["result"], {"actor": USER, "agent": ""})
        self.assertEqual(self.core.calls[-1][3], {"account": "甲", "user": "好友1"})

    def test_a_page_supplied_actor_is_refused(self):
        for extra in ({"actor": "agent"}, {"agent": "冒充"}, {"context": {"actor": "agent"}}):
            with self.assertRaises(core_http.HostError) as caught:
                self.page_rpc({"plugin": "kyf.friend-analysis", "method": "page.timelineUndoAgent",
                               "params": {}, "account": "甲", "user": "好友1", **extra})
            self.assertEqual((caught.exception.status, caught.exception.code), (400, "invalid-request"))
        self.assertEqual(self.core.calls, [])


if __name__ == "__main__":
    unittest.main()
