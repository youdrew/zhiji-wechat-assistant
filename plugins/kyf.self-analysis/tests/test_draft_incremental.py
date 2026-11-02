"""Durable personal-profile reuse with synthetic chats, never a real model or DB.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import hashlib
import unittest

import test_draft as fixture
from test_self_analysis import message
import test_self_analysis as synthetic


class AuditedCore(synthetic.BatchCore):
    def __init__(self, live):
        super().__init__(live)
        self.positioned = True

    def history_status(self, account, user, fresh=False, audit=False, audit_highwater=None):
        value = super().history_status(account, user)
        value["sourceRevision"] = hashlib.sha256(json.dumps(self.live, sort_keys=True).encode()).hexdigest()
        if audit:
            ceiling = audit_highwater or value.get("highwater")
            prefix = []
            if ceiling:
                last = int(json.loads(ceiling)[1][1:])
                prefix = [row["id"] for row in self.live[user] if int(row["id"][1:]) <= last]
            value.update(sourcePrefixCeiling=ceiling,
                         sourcePrefixSignature=hashlib.sha256(json.dumps(prefix).encode()).hexdigest() if ceiling else None)
        return value


class IncrementalDraftTests(unittest.TestCase):
    setUp = fixture.DraftTests.setUp
    tearDown = fixture.DraftTests.tearDown
    finish = fixture.DraftTests.finish
    source_requests = fixture.DraftTests.source_requests

    def generate(self, **params):
        before = len(self.models.requests)
        self.service.draft(self.account, {"depth": "basic", **params})
        result = self.finish()
        self.assertEqual(result["state"], "done", result)
        return result, self.models.requests[before:]

    def test_completed_generation_and_restart_reuse_corpus_and_all_summaries(self):
        self.generate()
        pages = list(self.core.pages)
        for restart in (False, True):
            if restart:
                from self_service import Service
                self.service.close()
                self.service = Service(self.plugin)
            status, requests = self.generate()
            self.assertEqual(self.core.pages, pages)
            self.assertEqual([item["step"] for item in requests], ["profile-draft-final"])
            self.assertEqual(status["coverage"]["reusedMessages"], 6)
            self.assertEqual(status["coverage"]["newlyAnalyzedMessages"], 0)

    def test_changing_user_profile_only_rewrites_final_draft(self):
        self.generate()
        pages = list(self.core.pages)
        _, requests = self.generate(background="我是工程师，最近换了工作", style="尽量简洁")
        self.assertEqual(self.core.pages, pages)
        self.assertEqual([item["step"] for item in requests], ["profile-draft-final"])
        self.assertEqual(requests[0]["prompt"]["existing"]["background"], "我是工程师，最近换了工作")

    def test_append_in_new_time_window_only_reads_and_analyzes_new_message(self):
        self.generate()
        before = len(self.core.pages)
        self.core.live["friend-a"].append(message(7, "self", "今天想学习新的东西", time=1_800_000_000_000))
        status, requests = self.generate()
        self.assertEqual(self.core.pages[before:], [("friend-a", 6)])
        texts = [item["text"] for request in requests if request["step"] == "profile-draft"
                 for item in request["prompt"]["messages"]]
        self.assertEqual(texts, ["今天想学习新的东西"])
        self.assertEqual(status["coverage"]["reusedMessages"], 6)
        self.assertEqual(status["coverage"]["newlyAnalyzedMessages"], 1)

    def test_returning_to_previous_depth_reuses_its_persistent_summaries(self):
        self.generate(depth="deep")
        pages = list(self.core.pages)
        self.generate(depth="basic")
        _, requests = self.generate(depth="deep")
        self.assertEqual(self.core.pages, pages)
        self.assertEqual([item["step"] for item in requests], ["profile-draft-final"])

    def test_old_database_completed_summaries_are_migrated_without_model_reanalysis(self):
        self.generate()
        store = self.service.accounts[self.account][0]
        with store.lock, store.db:
            store.db.execute("DELETE FROM profile_analysis_cache_inputs")
            store.db.execute("DELETE FROM profile_analysis_cache")
            store.db.execute("DELETE FROM meta WHERE key IN ('profileAnalysisSignature','profileAnalysisIdentity')")
            store.db.execute("UPDATE profile_draft_parts SET fingerprint=NULL")
        _, requests = self.generate()
        self.assertEqual([item["step"] for item in requests], ["profile-draft-final"])
        self.assertGreater(store.rows("SELECT count(*) n FROM profile_analysis_cache")[0]["n"], 0)

    def test_legacy_summaries_exceeding_new_model_budget_are_reanalyzed(self):
        self.generate()
        store = self.service.accounts[self.account][0]
        with store.lock, store.db:
            store.db.execute("DELETE FROM profile_analysis_cache_inputs")
            store.db.execute("DELETE FROM profile_analysis_cache")
            store.db.execute("DELETE FROM meta WHERE key IN ('profileAnalysisSignature','profileAnalysisIdentity')")
            store.db.execute("UPDATE profile_draft_notes SET payload=? WHERE level=0",
                             (json.dumps({"background": "过长的旧摘要" * 100, "style": "简短"}),))
        self.models.context_tokens = 4096
        _, requests = self.generate()
        self.assertTrue(any(item["step"] == "profile-draft" for item in requests))

    def test_analysis_rule_change_invalidates_summary_cache_without_rereading_history(self):
        from unittest.mock import patch
        import self_draft
        self.generate()
        pages = list(self.core.pages)
        with patch.object(self_draft, "RULES", self_draft.RULES + "\n新的分析规则"):
            _, requests = self.generate()
        self.assertEqual(self.core.pages, pages)
        self.assertTrue(any(item["step"] == "profile-draft" for item in requests))

    def test_context_change_invalidates_affected_batches_and_keeps_other_summaries(self):
        # Long messages force separate batches, so a changed exchange must not
        # invalidate unrelated chats merely because they were analyzed earlier.
        self.models.context_tokens = 4096
        self.core.live = {
            "a": [message(1, "other", "你自己想参加吗？"), message(2, "self", "这是我的选择。" * 80)],
            "b": [message(1, "self", "另一段不受影响的合成记录。" * 80)],
        }
        self.generate()
        # Simulate Core reporting an explicit projection rebuild after editing.
        self.core.live["a"][0] = message(1, "other", "你只是陪别人参加吗？")
        self.core.history_prepare(self.account, "a", rebuild=True)
        status, requests = self.generate()
        analyzed = [item for request in requests if request["step"] == "profile-draft"
                    for item in request["prompt"]["messages"]]
        self.assertTrue(analyzed)
        self.assertTrue(all(item["chat"].endswith("a") for item in analyzed))
        self.assertIn("你只是陪别人参加吗？", json.dumps(analyzed, ensure_ascii=False))
        self.assertGreater(status["coverage"]["reusedMessages"], 0)

    def audited_core(self):
        self.core = AuditedCore(self.core.live)
        self.plugin.core = self.core

    def test_same_latest_old_message_edit_replaces_stale_summary_on_the_chats_next_change(self):
        self.audited_core()
        self.generate()
        self.core.live["friend-a"][1] = message(2, "self", "我后来明确改了主意")
        # An unchanged newest row and read prefix skip the chat without re-verifying its text.
        self.generate()
        self.assertEqual(self.core.rebuilds, [])
        self.core.live["friend-a"].append(message(7, "other", "后来呢", time=1_800_000_000_000))
        _, requests = self.generate()
        texts = [item["text"] for request in requests if request["step"] == "profile-draft"
                 for item in request["prompt"]["messages"]]
        self.assertIn("我后来明确改了主意", texts)
        self.assertNotIn("好呀0", texts)
        self.assertEqual(self.core.rebuilds, ["friend-a"])

    def test_unavailable_verification_keeps_data_and_blocks_stale_profile_then_recovers(self):
        self.audited_core()
        self.generate()
        store = self.service.accounts[self.account][0]
        count_before = store.rows("SELECT count(*) n FROM profile_corpus_messages")[0]["n"]
        self.core.live["friend-a"][1] = message(2, "self", "旧原文已经变化")
        self.core.live["friend-a"].append(message(7, "other", "新消息", time=1_800_000_000_000))
        self.core.unready.add("friend-a")
        before = len(self.models.requests)
        self.service.draft(self.account, {"depth": "basic"})
        status = self.finish()
        self.assertEqual(status["state"], "error", status)
        self.assertNotIn("result", status)
        self.assertEqual(len(self.models.requests), before)
        self.assertEqual(store.rows("SELECT count(*) n FROM profile_corpus_messages")[0]["n"], count_before)
        self.core.unready.clear()
        self.generate()

    def test_early_deletion_does_not_reanalyze_unrelated_later_context(self):
        self.audited_core()
        self.models.context_tokens = 4096
        self.core.live = {"a": [message(i, "self", "独立记录" + str(i) + "。" * 700,
                                       time=1_700_000_000_000 + i * 86_400_000) for i in range(1, 8)]}
        self.generate()
        del self.core.live["a"][0]
        for seq, row in enumerate(self.core.live["a"], 1):
            row["seq"] = seq
        status, requests = self.generate()
        self.assertEqual(status["coverage"]["ownMessages"], 6)
        self.assertEqual(status["coverage"]["reusedMessages"], 6)
        self.assertFalse(any(item["step"] == "profile-draft" for item in requests))


if __name__ == "__main__":
    unittest.main()
