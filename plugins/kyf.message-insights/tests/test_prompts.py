"""Synthetic structured-output compatibility without inventing reply content or evidence.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from prompts import SYSTEM, InsightError, correction_instruction, material, parse_reply_candidates, parse_result


TARGET = "synthetic-target"


class StructuredOutputTests(unittest.TestCase):
    def setUp(self):
        self.rows = [self.row("synthetic-previous", 1), self.row(TARGET, 2)]
        self.prompt, self.sources = material(self.rows, [TARGET], [], {}, 10000)
        self.target_source = json.loads(self.prompt)["targetSourceIds"][TARGET]
        self.previous_source = next(key for key, source in self.sources.items()
                                    if source.get("messageId") == "synthetic-previous")
        self.analysis = {"messageId": TARGET, "summary": "一句合成原意", "meaning": "结合合成语境理解",
                         "uncertainty": "仍需对方补充", "responseDirection": "可以自然回应",
                         "evidenceIds": [self.target_source]}
        self.replies = {"replies": [{"label": "简短", "text": "好呀，慢慢来。"},
                                    {"label": "关心", "text": "需要我帮忙吗？"},
                                    {"label": "接话", "text": "那我们晚点再聊。"}]}

    @staticmethod
    def row(message_id, number):
        return {"msg_id": message_id, "side": "other", "sender_id": "synthetic-friend",
                "kind": "text", "text": "合成测试原文", "time_ms": number * 1000, "raw": "{}"}

    def result_text(self, item=None):
        return json.dumps({"analyses": [self.analysis if item is None else item]}, ensure_ascii=False)

    def test_initial_prompt_names_actual_target_sources_and_no_invented_example_id(self):
        payload = json.loads(self.prompt)
        self.assertEqual(payload["targetSourceIds"], {TARGET: self.target_source})
        self.assertEqual(self.sources[self.target_source]["kind"], "chat")
        self.assertEqual(self.sources[self.target_source]["messageId"], TARGET)
        self.assertEqual(payload["contextWindow"], {"availableMessages": 2, "includedMessages": 2, "omittedMessages": 0})
        self.assertNotIn('"c1"', SYSTEM)
        self.assertIn("targetSourceIds", SYSTEM)

    def test_plain_bom_and_whole_json_fences_preserve_exact_validated_result(self):
        original = self.result_text()
        expected = parse_result(original, [TARGET], self.sources)
        for wrapper in (" \n{}\n ", "\ufeff{}", "```json\n{}\n```", "```JSON\r\n{}\r\n```", "```\n{}\n```"):
            with self.subTest(wrapper=wrapper):
                self.assertEqual(parse_result(wrapper.format(original), [TARGET], self.sources), expected)
        self.assertEqual(expected[TARGET]["evidence"], [{key: value for key, value in self.sources[self.target_source].items()
                                                      if key != "sourceId"}])

    def test_surrounding_prose_multiple_values_or_broken_fences_are_not_salvaged(self):
        original = self.result_text()
        for text in ("说明：" + original, original + "\n以上是结果", original + original,
                     "```json\n" + original, "```javascript\n" + original + "\n```",
                     "```json\n" + original + "\n```\n```json\n{}\n```", original[:-1]):
            with self.subTest(text=text[:35]), self.assertRaises(InsightError) as error:
                parse_result(text, [TARGET], self.sources)
            self.assertEqual(error.exception.code, "invalid-output")

    def test_duplicate_json_keys_are_rejected_instead_of_silently_picking_one(self):
        valid = self.result_text()
        cases = [valid.replace('"analyses":', '"analyses":[],"analyses":', 1),
                 valid.replace('"evidenceIds":', '"evidenceIds":["invented"],"evidenceIds":', 1),
                 valid.replace('"messageId":', '"messageId":"wrong-target","messageId":', 1)]
        for text in cases:
            with self.subTest(text=text[:55]), self.assertRaises(InsightError):
                parse_result(text, [TARGET], self.sources)

    def test_invented_mismatched_duplicate_or_missing_target_citations_stay_invalid(self):
        for evidence in (["c1"], [TARGET], [self.previous_source], [self.target_source, "not-in-input"],
                         [self.target_source, self.target_source], [], "s1", [1], [" " + self.target_source]):
            with self.subTest(evidence=evidence), self.assertRaises(InsightError):
                parse_result(self.result_text({**self.analysis, "evidenceIds": evidence}), [TARGET], self.sources)
        observation = {"kind": "observation", "messageId": TARGET, "text": "合成观察", "sourceLabel": "模型观察"}
        with self.assertRaises(InsightError):
            parse_result(self.result_text({**self.analysis, "evidenceIds": ["visual"]}), [TARGET],
                         {**self.sources, "visual": observation})

    def test_wrong_target_missing_fields_and_empty_content_are_not_filled_in(self):
        for item in ({**self.analysis, "messageId": "wrong"}, {**self.analysis, "meaning": " "},
                     {**self.analysis, "summary": "长" * 161}, {**self.analysis, "summary": None},
                     {key: value for key, value in self.analysis.items() if key != "uncertainty"}):
            with self.subTest(item=item), self.assertRaises(InsightError):
                parse_result(self.result_text(item), [TARGET], self.sources)
        with self.assertRaises(InsightError):
            parse_result(json.dumps({"analyses": [self.analysis, self.analysis]}), [TARGET], self.sources)

    def test_correction_uses_only_available_reference_ids_without_source_text(self):
        instruction = correction_instruction("analysis", [TARGET], self.sources)
        references = json.loads(instruction.split("允许的来源编号：", 1)[1])
        self.assertEqual(references, {"targetSourceIds": {TARGET: self.target_source},
                                      "allowedSourceIds": list(self.sources)})
        self.assertNotIn("合成测试原文", instruction)
        self.assertIn("只引用目标 chat 来源", instruction)
        self.assertIn("恰好三条不同回复", correction_instruction("suggestions", [TARGET], self.sources))
        with self.assertRaises(ValueError):
            correction_instruction("other", [TARGET], self.sources)

    def test_complete_reply_fences_keep_text_labels_and_linebreaks(self):
        self.replies["replies"][0]["text"] = "好呀，\n慢慢来。"
        original = json.dumps(self.replies, ensure_ascii=False)
        for wrapped in (original, "\ufeff" + original, "```json\n" + original + "\n```"):
            with self.subTest(wrapped=wrapped[:15]):
                self.assertEqual(parse_reply_candidates(wrapped), self.replies["replies"])

    def test_incomplete_duplicate_or_ambiguous_replies_are_never_repaired_locally(self):
        cases = [json.dumps({"replies": self.replies["replies"][:2]}),
                 json.dumps({"replies": ["相同", "相同", "不同"]}),
                 json.dumps({"replies": [{"text": "缺标签"}, *self.replies["replies"][1:]]}),
                 json.dumps(self.replies).replace('"replies":', '"replies":[],"replies":', 1),
                 "说明：" + json.dumps(self.replies), json.dumps(self.replies) + "{}"]
        for text in cases:
            with self.subTest(text=text[:40]), self.assertRaises(InsightError) as error:
                parse_reply_candidates(text)
            self.assertEqual(error.exception.code, "invalid-suggestions")

    def test_response_limits_and_non_json_constants_are_not_relaxed(self):
        for text in (None, " " * 64001, "[" * 2000):
            with self.subTest(text_type=type(text).__name__), self.assertRaises(InsightError):
                parse_result(text, [TARGET], self.sources)
        for text in (None, " " * 16001, '{"replies":[NaN,Infinity,-Infinity]}'):
            with self.subTest(text_type=type(text).__name__), self.assertRaises(InsightError):
                parse_reply_candidates(text)


if __name__ == "__main__":
    unittest.main()
