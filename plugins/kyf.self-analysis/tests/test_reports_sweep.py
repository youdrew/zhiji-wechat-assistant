"""Deterministic self-report recall: the regex sweep over the own-message corpus.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend"),
                str(ROOT / "plugins/kyf.self-analysis/tests")]
from self_persona import sweep_self_reports
from self_profile_corpus import ProfileCorpus, Reader
from self_store import Store
from test_profile_corpus import Context, Core, message


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name), "synthetic-account")
        self.corpus = ProfileCorpus(self.store)
        self.core = Core({"friend-a": [message(1, "我是INFP"),
                                       message(2, "我纯纯的infp，但我同事觉得我是j人"),
                                       message(3, "我是ENFP或是INFP拉"),
                                       message(4, "我测过是INTJ"),
                                       message(5, "我的MBTI：INFJ"),
                                       message(6, "这也是我很喜欢intj这个性格原因 会严格要求自己。"),
                                       message(7, "如果我是enfj就可以更好的保护一持啦"),
                                       message(8, "我想要个INTJ男朋友"),
                                       message(9, "小狐是啥mbti？"),
                                       message(10, "今天天气不错"),
                                       message(11, "我不是INFP，别猜了"),
                                       message(12, "我觉得我是ENTP，同事也这么说")],
                          "group@chatroom": [message(1, "我是ISTJ", "other", senderName="朋友", senderId="other")]})
        Reader(self.core, "synthetic-account", self.corpus, Context(), None).run()

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def by_message(self, rows):
        found = {}
        for row in rows:
            found.setdefault(row["id"], set()).add(row["value"])
        return found

    def test_first_person_claims_are_recalled_with_every_stated_code(self):
        rows = sweep_self_reports(self.corpus)
        found = self.by_message(rows)
        values = {code for codes in found.values() for code in codes}
        self.assertIn("INFP", values)
        self.assertIn("ENFP", values)
        self.assertIn("INTJ", values)
        self.assertIn("INFJ", values)
        self.assertIn("ENTP", values)
        # “我是ENFP或是INFP拉” states both; each is its own proof.
        alternation = [codes for identifier, codes in found.items()
                       if any("或是" in row["quote"] for row in rows if row["id"] == identifier)]
        self.assertEqual(sorted(alternation[0]) if alternation else [], ["ENFP", "INFP"])
        # Every row carries a usable quote and the sweep reason.
        self.assertTrue(all(row["quote"] and "正则补捞" in row["reason"] for row in rows))

    def test_affect_hypothetical_denial_and_other_people_are_not_self_reports(self):
        rows = sweep_self_reports(self.corpus)
        quotes = " ".join(row["quote"] for row in rows)
        self.assertNotIn("很喜欢intj", quotes)
        self.assertNotIn("如果我是enfj", quotes)
        self.assertNotIn("INTJ男朋友", quotes)
        self.assertNotIn("啥mbti", quotes)
        self.assertNotIn("我不是INFP", quotes)

    def test_group_lines_from_others_never_enter_the_own_corpus_sweep(self):
        rows = sweep_self_reports(self.corpus)
        self.assertNotIn("ISTJ", {row["value"] for row in rows})

    def test_sweep_is_idempotent_across_calls(self):
        first = sweep_self_reports(self.corpus)
        second = sweep_self_reports(self.corpus)
        self.assertEqual(self.by_message(first), self.by_message(second))


if __name__ == "__main__":
    unittest.main()
