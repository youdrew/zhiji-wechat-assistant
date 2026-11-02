"""Plugin-owned wording and budget rules over Core snapshot fixtures."""
import copy
import unittest
from profile_section import context_section, normalize_text, structured_sources

class ProfileSectionTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"stamp": "synthetic-v1", "data": {"memories": [], "observations": [], "proofs": {},
                         "exclusions": [], "goal": "", "profile": {"background": "", "style": ""}}}

    def pending(self):
        item = {"id": "memory-1", "text": "对方不吃香菜", "norm": normalize_text("对方不吃香菜"),
                "status": "pending", "origin": "ai", "user_corrected": False, "relation": "new", "tags_json": "[]"}
        proof = {"status": "ok", "owned": True, "quoted": False, "voice": False,
                 "excerpt": "我不吃香菜", "messageId": "synthetic-message"}
        self.snapshot["data"]["memories"] = [item]
        self.snapshot["data"]["proofs"] = {item["id"]: [proof]}
        return item, proof

    def test_pending_is_attributed_and_preserves_core_stamp(self):
        self.pending()
        text, stamp = context_section(self.snapshot, 2000)
        self.assertIn("自动提取待确认", text)
        self.assertIn("不证明提取结论成立", text)
        self.assertIn("我不吃香菜", text)
        self.assertEqual(stamp, "synthetic-v1")

    def test_unverified_quoted_and_unowned_proofs_never_authorize_pending(self):
        _, proof = self.pending()
        for changed in ({"status": "changed"}, {"status": "recalled"}, {"status": "missing"}, {"owned": False}, {"quoted": True}):
            original = dict(proof)
            proof.update(changed)
            self.assertEqual(structured_sources(self.snapshot, 2000), [])
            proof.clear(); proof.update(original)

    def test_uncertain_conflicting_and_excluded_memories_are_absent(self):
        item, _ = self.pending()
        for relation in ("uncertain", "conflict"):
            item["relation"] = relation
            self.assertEqual(structured_sources(self.snapshot, 2000), [])
        item["relation"] = "new"
        self.snapshot["data"]["exclusions"] = [item["norm"]]
        self.assertEqual(structured_sources(self.snapshot, 2000), [])

    def test_confirmed_and_observation_roles_remain_separate(self):
        item, _ = self.pending()
        item["status"] = "confirmed"
        self.snapshot["data"]["observations"] = [{"id": "o", "judgement": "可能心情不好", "note": "仅凭感觉"}]
        text, _ = context_section(self.snapshot, 3000)
        self.assertIn("用户已确认", text)
        self.assertIn("不是对方事实", text)
        self.assertIn("尚未独立验证", text)
        self.assertNotIn("自动提取待确认；不是已确认事实", text)

    def test_budget_bounds_and_settings_have_distinct_roles(self):
        self.pending()
        self.snapshot["data"]["profile"] = {"background": "用户是设计师" * 1000, "style": "简短表达" * 1000}
        self.snapshot["data"]["goal"] = "保持联系" * 1000
        before = copy.deepcopy(self.snapshot)
        for budget in (0, 150, 240, 500, 1200, 6000):
            text, _ = context_section(self.snapshot, budget)
            self.assertLessEqual(len(text), budget)
        text, _ = context_section(self.snapshot, 1200)
        self.assertIn("用户自己的背景；不是对方资料", text)
        self.assertIn("不是对方意愿或同意", text)
        self.assertEqual(self.snapshot, before)

    def test_voice_quote_is_explicitly_existing_wechat_transcript(self):
        _, proof = self.pending()
        proof["voice"] = True
        text, _ = context_section(self.snapshot, 2000)
        self.assertIn("微信已有语音转写，可能有误", text)

    def test_plugin_settings_filter_sources_without_changing_snapshot_or_stamp(self):
        item, _ = self.pending()
        item["status"] = "confirmed"
        self.snapshot["data"]["observations"] = [{"id": "o", "judgement": "合成观察内容", "note": "仅凭感觉"}]
        self.snapshot["data"]["profile"]["style"] = "合成用户风格"
        before = copy.deepcopy(self.snapshot)
        for memories, observations in ((True, False), (False, True), (False, False)):
            text, stamp = context_section(self.snapshot, 3000, include_memories=memories,
                                          include_observations=observations)
            self.assertEqual("不吃香菜" in text, memories)
            self.assertEqual("合成观察内容" in text, observations)
            self.assertIn("合成用户风格", text)
            self.assertEqual(stamp, self.snapshot["stamp"])
        self.assertEqual(self.snapshot, before)


if __name__ == "__main__":
    unittest.main()
