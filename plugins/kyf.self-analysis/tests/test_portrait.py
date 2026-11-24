"""本人画像 for 分析与建议: compact facts and only what is recent. Synthetic data only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend")]
import self_portrait  # noqa: E402
from self_portrait import DAY_MS, compact_facts, recent_lines  # noqa: E402

NOW = 1_800_000_000_000


def dimension(key, label, inference=None, manual=None, report=None):
    return {"key": key, "label": label, "manual": manual, "selfReport": report, "inference": inference}


class PortraitTests(unittest.TestCase):
    def test_recent_keeps_only_this_year_periods_and_past_four_months_events(self):
        catalog = {
            "periods": [
                {"when": "2019年9月", "title": "研究生入学", "ongoing": True, "startMs": NOW - 2400 * DAY_MS},
                {"when": "2026年10月", "title": "重新拾起摄影", "ongoing": True, "startMs": NOW - 30 * DAY_MS},
                {"when": "2026年8月", "title": "已结束的阶段", "ongoing": False, "startMs": NOW - 60 * DAY_MS},
                {"when": "", "title": "时间不明", "ongoing": True, "startMs": None}],
            "events": [
                {"when": "2027年", "title": "明年的计划", "startMs": NOW + 90 * DAY_MS},
                {"when": "2026年11月", "title": "最近的事", "startMs": NOW - 3 * DAY_MS},
                {"when": "2025年", "title": "很久以前", "startMs": NOW - 400 * DAY_MS}]}
        self.assertEqual(recent_lines(catalog, NOW), ["近况·进行中：2026年10月 重新拾起摄影", "近况·事件：2026年11月 最近的事"])

    def test_facts_are_short_and_leave_out_politics_and_moral_foundations(self):
        persona = {"selfReports": {"mbti": {"text": "INFJ"}}, "dimensions": [
            dimension("big5_o", "开放性", {"text": "很长的结论。" * 40, "position": 0.9}),
            dimension("big5_e", "外向性", {"text": "偏内向；但熟人面前话多", "position": None}),
            dimension("pol_econ", "经济立场", {"text": "偏左", "position": 0.2}),
            dimension("moral_care", "关爱", {"text": "高", "position": 0.8}),
            dimension("att_anx", "依恋焦虑", manual={"text": "我自己觉得偏焦虑"}, inference={"text": "x", "position": 0.5})]}
        values = {"dimensions": [dimension("love", "感情观", report={"text": "尊重对方节奏"})]}
        with mock.patch.object(self_portrait.PersonaStore, "view", return_value=persona), \
                mock.patch.object(self_portrait.ValuesStore, "view", return_value=values), \
                mock.patch.object(self_portrait.PersonaStore, "__init__", return_value=None), \
                mock.patch.object(self_portrait.ValuesStore, "__init__", return_value=None):
            lines = compact_facts(object())
        self.assertEqual(lines, ["MBTI（自述）：INFJ", "性格·依恋焦虑（本人补充）：我自己觉得偏焦虑", "三观·感情观（自述）：尊重对方节奏",
                                 "性格·开放性：偏高（推测）", "性格·外向性：偏内向（推测）", "性格·依恋焦虑：中等（推测）"])


if __name__ == "__main__":
    unittest.main()
