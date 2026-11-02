"""Synthetic 知己分析 host-service tests with a fake WeChat source.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Modified 2026-10-13 (Apache-2.0): memory extraction moved to the kyf.friend-analysis plugin
(its tests port the extraction cases); here candidates arrive through Core's plugin intake.
Modified 2026-10-13: the host service is Core's AccountDataService; the assistant case moves to the
kyf.advisor plugin's tests.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core_accounts import AccountDataService
from core_contracts import AdvisorError
from core_human import HumanService
from friend_context import message_role
from friend_store import content_hash
from test_support import FakeSource, make_message, wait_until

ACCOUNT = "synthetic-account-1"
USER = "wxid_synthetic_friend"
AGENT = "builtin:advisor"
PLUGIN = "kyf.friend-analysis"
# What the plugin proposes for the friend's own statements in the synthetic history.
PROPOSALS = (("急性子", "对方是急性子（自述）", ["性格"]), ("香菜", "对方不吃香菜", ["偏好与忌口"]))


def message(seq, side, text, **extra):
    item = make_message(seq, side=side, text=text, sender="self" if side == "self" else USER,
                        name="我" if side == "self" else "朋友")
    item.update(extra)
    return item


def evidence(row, source_fp):
    """One projected message as a plugin cites it in a candidate."""
    raw = json.loads(row.get("raw") or "{}")
    return {"sourceFp": source_fp, "msgId": row["msg_id"], "hash": content_hash(row), "sortKey": row.get("sort_key"),
            "cursor": raw.get("historyCursor") or None, "role": message_role(row), "time": row.get("time_ms"),
            "excerpt": (row.get("text") or "")[:500]}


class FriendServiceCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = FakeSource(account=ACCOUNT)
        self.source.add_messages(USER, [
            message(1, "other", "我是急性子，等不了人"), message(2, "self", "哈哈我知道"),
            message(3, "other", "我不吃香菜，一点都不行"), message(4, "self", "周末一起去爬山吧？"),
            message(5, "other", "他说他喜欢滑雪", type="聊天记录"), message(6, "other", "上次那场电影真不错"),
        ] + [message(seq, "self" if seq % 2 else "other", "日常闲聊 %d" % seq) for seq in range(7, 20)])
        self.service = AccountDataService(self.source, Path(temp.name))
        self.addCleanup(self.service.shutdown)
        self.friends = self.service.friends
        self.human = HumanService(self.service.stores, source=self.source)

    def open_ready(self):
        self.friends.open(ACCOUNT, USER)
        ready = wait_until(lambda: self.friends.overview(ACCOUNT, USER)["history"]["state"] == "ready" and
                           not self.friends.overview(ACCOUNT, USER)["history"]["refreshing"])
        self.assertTrue(ready, "history did not become ready")

    def rows(self):
        return self.service.stores.account(ACCOUNT)._rows(
            "SELECT * FROM context_messages WHERE user=? ORDER BY seq", (USER,))

    def propose(self, candidates, **options):
        """Core's intake of a plugin's memory candidates (``POST human/candidates``)."""
        return self.human.submit_candidates(ACCOUNT, USER, PLUGIN, {"task": "task-synthetic"}, candidates, **options)

    def candidate(self, text, tags, row, **extra):
        source_fp = self.service.stores.account(ACCOUNT).context(USER)["source_id"]
        return {"text": text, "tags": tags, "proposalKey": "key-%s-%s" % (row["msg_id"], len(text)),
                "evidence": [evidence(row, source_fp)], **extra}

    def pending(self):
        return {item["text"]: item for item in self.friends.memories(ACCOUNT, USER, status="pending")["items"]}

    def build(self):
        """Read the history, then receive the plugin's candidates for the friend's own statements."""
        self.open_ready()
        return self.propose([self.candidate(text, tags, row) for row in self.rows() if row["side"] == "other"
                             for keyword, text, tags in PROPOSALS if keyword in (row["text"] or "")])


class HostTests(FriendServiceCase):
    def test_plugin_candidates_become_pending_memories_with_their_evidence(self):
        result = self.build()
        self.assertEqual((len(result["inserted"]), result["skipped"]), (2, 0))
        self.assertEqual(set(self.pending()), {"对方是急性子（自述）", "对方不吃香菜"})
        detail = self.friends.memory_detail(ACCOUNT, USER, self.pending()["对方不吃香菜"]["id"])
        self.assertEqual((detail["evidence"][0]["role"], detail["evidence"][0]["status"]), ("friend", "ok"))
        self.assertEqual(detail["memory"]["origin"], "ai")
        self.assertEqual(self.build()["inserted"], [], "a repeated batch adds nothing")
        overview = self.friends.overview(ACCOUNT, USER)
        self.assertEqual(overview["memory"]["counts"], {"pending": 2, "confirmed": 0})

    def test_plugins_hear_about_opened_windows_and_changed_human_data(self):
        self.friends.on_opened, self.friends.on_changed = mock.Mock(), mock.Mock()
        self.open_ready()
        self.assertTrue(wait_until(lambda: self.friends.on_opened.called))
        self.friends.on_opened.assert_called_with(ACCOUNT, USER)
        created = self.friends.memory_action(ACCOUNT, USER, {"action": "create", "text": "对方喜欢猫", "tags": ["爱好"]})
        self.friends.on_changed.assert_called_with(ACCOUNT, USER, created["memoryVersion"])
        self.friends.on_changed.reset_mock()
        self.friends.save_settings(ACCOUNT, USER, {"kind": "goal", "text": "保持联系", "expectedVersion": 0})
        self.friends.on_changed.assert_called_once()
        self.friends.on_changed.side_effect = RuntimeError("plugin host unavailable")
        self.friends.save_observation(ACCOUNT, USER, {"judgement": "最近比较忙", "note": ""})
        self.assertEqual(len(self.friends.observations(ACCOUNT, USER)["items"]), 1, "a failing hook never loses data")

    def test_message_context_reads_only_the_projection_around_the_message(self):
        self.open_ready()
        target = self.rows()[9]
        context = self.friends.message_context(ACCOUNT, USER, target["msg_id"])
        self.assertEqual(context["status"], "ok")
        self.assertEqual([item["target"] for item in context["messages"]].count(True), 1)
        self.assertEqual(len(context["messages"]), 11)
        self.assertEqual(self.friends.message_context(ACCOUNT, USER, "unknown")["status"], "missing")
        with self.assertRaises(AdvisorError):
            self.friends.message_context(ACCOUNT, USER, "")

    def test_group_chats_are_rejected(self):
        with self.assertRaises(AdvisorError):
            self.friends.overview(ACCOUNT, "12345@chatroom")


class EvidenceAndContextTests(FriendServiceCase):
    def test_evidence_context_and_reverification_after_projection_rebuild(self):
        self.build()
        item = self.pending()["对方不吃香菜"]
        view = self.friends.evidence(ACCOUNT, USER, item["id"], 0)
        self.assertEqual(view["status"], "ok")
        self.assertEqual([entry["target"] for entry in view["messages"]].count(True), 1)
        self.assertEqual({entry["role"] for entry in view["messages"] if entry["target"]}, {"friend"})
        self.assertLessEqual(len(view["messages"]), 11)
        self.assertTrue(self.friends.locate(ACCOUNT, USER, item["id"], 0)["cursor"])
        # A changed original rebuilds the projection; only that evidence is flagged.
        self.source.users[USER][2]["text"] = "我不吃香菜（已编辑）"
        window, _after = self.source.history_page(USER, None)
        self.service.observe_message_window(ACCOUNT, self.source.workdir, USER, window, False)
        self.open_ready()
        wait_until(lambda: self.friends.memory_detail(ACCOUNT, USER, item["id"])["evidence"][0]["status"] == "changed")
        detail = self.friends.memory_detail(ACCOUNT, USER, item["id"])
        self.assertEqual(detail["evidence"][0]["status"], "changed")
        self.assertEqual(detail["memory"]["status"], "pending")
        other = self.pending()["对方是急性子（自述）"]
        self.assertEqual(self.friends.memory_detail(ACCOUNT, USER, other["id"])["evidence"][0]["status"], "ok")

    @unittest.skip("assistant runs are the kyf.advisor plugin's; this case moves to its tests (WP3)")
    def test_advisor_receives_confirmed_memories_and_new_session_after_edits(self):
        self.build()
        pending = self.pending()
        self.friends.memory_action(ACCOUNT, USER, {"action": "confirm", "id": pending["对方不吃香菜"]["id"]})
        self.friends.save_settings(ACCOUNT, USER, {"kind": "goal", "text": "保持联系", "expectedVersion": 0})
        started = self.service.start(ACCOUNT, USER, AGENT, None, "周末吃什么？", "run-1")
        wait_until(lambda: self.service.events(ACCOUNT, USER, started["run"]["id"])["run"]["state"] == "done")
        request = self.runtime.respond_calls[-1][1]
        self.assertIn("知己分析资料", request["contextFileText"])
        self.assertIn("对方不吃香菜", request["contextFileText"])
        self.assertNotIn("急性子", request["contextFileText"].split("[最近聊天记录]")[0].split("[对方的已确认记忆")[1]
                         if "[对方的已确认记忆" in request["contextFileText"] else "")
        self.assertIn("保持联系", request["contextFileText"])
        memory = self.friends.memories(ACCOUNT, USER, status="confirmed")["items"][0]
        self.friends.memory_action(ACCOUNT, USER, {"action": "edit", "id": memory["id"], "text": "对方完全不吃香菜",
                                                   "tags": ["偏好与忌口"], "expectedVersion": memory["version"]})
        again = self.service.start(ACCOUNT, USER, AGENT, started["threadId"], "那火锅呢？", "run-2")
        wait_until(lambda: self.service.events(ACCOUNT, USER, again["run"]["id"])["run"]["state"] == "done")
        second = self.runtime.respond_calls[-1][1]
        self.assertIsNone(second.get("runtimeSessionId"))
        self.assertIn("对方完全不吃香菜", second["contextFileText"])
        texts = [item["text"] for item in self.service.thread(ACCOUNT, USER, AGENT)["thread"]["messages"]]
        self.assertTrue(any("知己分析资料已更新" in text for text in texts))


if __name__ == "__main__":
    unittest.main()
