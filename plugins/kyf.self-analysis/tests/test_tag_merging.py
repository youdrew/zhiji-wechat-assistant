"""Tag merges preserve semantic meaning, using temporary synthetic evidence only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend")]

from self_contracts import SelfError
from self_store import Store
from self_tag_store import TagStore


class TagMergeSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name), "synthetic-tag-merge-account")
        self.tags = TagStore(self.store)
        self.user, self.after = "synthetic-friend", 0
        self.tags.catalog({"sourceFingerprint": "synthetic-source", "conversations": [
            {"user": self.user, "name": "合成朋友", "isGroup": False}], "next": None})
        self.tags.bind(self.user, "synthetic-epoch")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add_message(self, text, *labels, category="运动健身"):
        seq = self.after + 1
        identifier = f"{self.user}/m{seq}"
        self.tags.page(self.user, [{"id": identifier, "seq": seq, "time_ms": 1791849600000 + seq * 1000,
                                   "text": text, "context": "", "followup": "", "truncated": False}],
                       [], seq, expected_after=self.after)
        self.tags.save_mentions([identifier], [(identifier, {
            "name": name, "category": category, "relation": relation, "quote": text,
            "reason": "合成证据的语义判断，用于验证合并边界"}) for name, relation in labels])
        self.after = seq
        return identifier

    def snapshot(self):
        return {tag["name"]: (tag["relation"], tag["level"], tag["mentions"]) for tag in self.tags.tags()}

    def assert_merge_preserves_both(self, into, source):
        before = self.snapshot()
        evidence = {name: self.tags.evidence(name) for name in (into, source)}
        try:
            self.tags.merge(into, [source])
        except SelfError:
            pass
        self.assertEqual(self.snapshot(), before, "an incompatible merge must leave both meanings intact")
        for name, previous in evidence.items():
            self.assertEqual(self.tags.evidence(name), previous, "a rejected merge must preserve original evidence")

    def test_mention_and_negative_trait_never_merge_in_either_direction(self):
        self.add_message("篮球场门口等你", ("篮球", "mention"))
        self.add_message("我不喜欢篮球", ("不喜欢篮球", "trait"))
        self.assertEqual(self.snapshot(), {"篮球": ("mention", "possible", 1),
                                           "不喜欢篮球": ("trait", "strong", 1)})
        groups = self.tags.merge_candidates()
        self.assertEqual(set(groups), {("运动健身", "mention"), ("运动健身", "trait")})
        self.assertEqual([tag["name"] for tag in groups[("运动健身", "mention")]], ["篮球"])
        self.assertEqual([tag["name"] for tag in groups[("运动健身", "trait")]], ["不喜欢篮球"])
        for into, source in (("篮球", "不喜欢篮球"), ("不喜欢篮球", "篮球")):
            with self.subTest(into=into):
                self.assert_merge_preserves_both(into, source)

    def test_occasional_participation_cannot_merge_into_an_interest(self):
        self.add_message("明天陪朋友打篮球一次", ("打篮球", "occasional"))
        self.add_message("我喜欢篮球，这是我的爱好", ("篮球", "interest"))
        self.assertEqual(self.snapshot(), {"打篮球": ("occasional", "possible", 1),
                                           "篮球": ("interest", "strong", 1)})
        self.assertEqual(set(self.tags.merge_candidates()), {("运动健身", "occasional"),
                                                            ("运动健身", "interest")})
        for into, source in (("篮球", "打篮球"), ("打篮球", "篮球")):
            with self.subTest(into=into):
                self.assert_merge_preserves_both(into, source)

    def check_same_relation_synonyms(self, relation, level):
        first = self.add_message("合成文字同时包含篮球和打篮球", ("篮球", relation), ("打篮球", relation))
        second = self.add_message("合成篮球文字甲", ("篮球", relation))
        third = self.add_message("合成打篮球文字乙", ("打篮球", relation))
        self.assertEqual(self.snapshot(), {"篮球": (relation, level, 2), "打篮球": (relation, level, 2)})
        groups = self.tags.merge_candidates()
        self.assertEqual(set(groups), {("运动健身", relation)})
        self.assertEqual({tag["name"] for tag in groups[("运动健身", relation)]}, {"篮球", "打篮球"})

        self.tags.merge("篮球", ["打篮球"])

        self.assertEqual(self.snapshot(), {"篮球": (relation, level, 3)},
                         "synonyms merge while shared messages count once and semantics stay unchanged")
        evidence = self.tags.evidence("篮球")
        self.assertEqual(evidence["total"], 3)
        self.assertEqual([item["id"] for item in evidence["items"]], [third, second, first])
        self.assertEqual({item["relation"] for item in evidence["items"]}, {relation})

    def test_same_relation_mentions_merge_without_promoting_frequency_to_interest(self):
        self.check_same_relation_synonyms("mention", "possible")

    def test_same_relation_interests_merge_and_keep_distinct_message_evidence(self):
        self.check_same_relation_synonyms("interest", "strong")

    def test_positive_and_negative_traits_never_merge_in_either_direction(self):
        self.add_message("我习惯喝咖啡", ("喝咖啡", "trait"), category="美食饮食")
        self.add_message("我不喝咖啡", ("不喝咖啡", "trait"), category="美食饮食")
        self.assertEqual(self.snapshot(), {"喝咖啡": ("trait", "strong", 1),
                                           "不喝咖啡": ("trait", "strong", 1)})
        for into, source in (("喝咖啡", "不喝咖啡"), ("不喝咖啡", "喝咖啡")):
            with self.subTest(into=into):
                self.assert_merge_preserves_both(into, source)

    def test_edits_made_after_merge_candidates_were_read_keep_evidence_under_its_original_name(self):
        scenarios = (("篮球", "打篮球", "confirm", "source"),
                     ("象棋", "下象棋", "delete", "source"),
                     ("羽毛球", "打羽毛球", "delete", "target"))
        for into, source, action, edited_side in scenarios:
            with self.subTest(action=action, side=edited_side):
                self.add_message("合成目标主题：" + into, (into, "mention"))
                self.add_message("合成同义主题：" + source, (source, "mention"))
                offered = self.tags.merge_candidates()[("运动健身", "mention")]
                self.assertTrue({into, source} <= {tag["name"] for tag in offered},
                                "both names were offered before the model request completed")
                self.tags.edit(action, source if edited_side == "source" else into)

                self.assert_merge_preserves_both(into, source)


if __name__ == "__main__":
    unittest.main()
