"""Synthetic identity tests: text equality is never enough."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from tracking import match_observations


def row(text, y, **extra):
    return {'text': text, 'sender': 'other', 'kind': 'text', 'bbox': [.05, y, .55, .06], 'issues': [], **extra}


class TrackingTests(unittest.TestCase):
    def test_requires_geometry_and_positions(self):
        old = [row('哈哈', .5, id='a')]
        new = [row('哈哈', .3)]
        self.assertEqual(match_observations(old, new, {}), [])
        self.assertEqual(match_observations(old, new, {'reliable': True, 'offsetY': 0}), [])
        self.assertEqual(match_observations(old, new, {'reliable': True, 'offsetY': -.2})[0]['previousId'], 'a')

    def test_real_repeated_utterances_keep_separate_identity(self):
        old = [row('哈哈', .4, id='a'), row('哈哈', .6, id='b')]
        new = [row('哈哈', .2), row('哈哈', .4), row('哈哈', .6)]
        links = match_observations(old, new, {'reliable': True, 'offsetY': -.2})
        self.assertEqual([(x['currentIndex'], x['previousId']) for x in links], [(0, 'a'), (1, 'b')])

    def test_replaces_cropped_text_only_with_other_anchor(self):
        old = [row('完整气泡的清晰前半部分', .4, id='a', issues=['truncated']), row('另一个锚点', .7, id='b')]
        new = [row('完整气泡的清晰前半部分以及后半部分', .4), row('另一个锚点', .7)]
        self.assertEqual(match_observations(old[:1], new[:1], {'reliable': True, 'offsetY': 0}), [])
        links = match_observations(old, new, {'reliable': True, 'offsetY': 0})
        self.assertEqual(links[0]['action'], 'replace')

    def test_disagreement_is_conflict_never_silent_reuse(self):
        old = [row('一条气泡的正确文字', .3, id='a'), row('独立锚点', .6, id='b')]
        new = [row('一条气泡的错误文字', .3), row('独立锚点', .6)]
        links = match_observations(old, new, {'reliable': True, 'offsetY': 0})
        self.assertEqual(links[0]['action'], 'conflict')

    def test_ambiguous_or_crossing_alignment_is_not_merged(self):
        old = [row('哈哈', .4, id='a'), row('哈哈', .405, id='b')]
        self.assertEqual(match_observations(old, [row('哈哈', .4)], {'reliable': True, 'offsetY': 0}), [])
        old = [row('甲消息', .3, id='a'), row('乙消息', .6, id='b')]
        new = [row('乙消息', .6), row('甲消息', .3)]
        self.assertEqual(match_observations(old, new, {'reliable': True, 'offsetY': 0}), [])


if __name__ == '__main__':
    unittest.main()
