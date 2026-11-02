"""Synthetic queue settings: changed preferences affect future jobs and never send a reply."""
import tempfile
import threading
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

from kyf_plugin import CoreError
from insights import Insights
from prompts import SUGGESTIONS_SYSTEM, suggestions_system


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.snapshot = {"values": {"replyStyle": "natural"}, "revision": 0}
        core = SimpleNamespace(get=lambda path: self.snapshot)
        self.insights = Insights.__new__(Insights)
        self.insights.plugin = SimpleNamespace(core=core, data_dir=Path(temp.name))
        self.insights.lock = threading.RLock()
        self.insights.stores = {}
        self.insights.queue = OrderedDict()
        self.addCleanup(lambda: [store.close() for store in self.insights.stores.values()])

    def enqueue(self, message_id):
        self.insights.enqueue("synthetic-account", "synthetic-friend", [message_id], {"revision": 1},
                              automatic=True, unified=True)
        return self.insights.queue[("synthetic-account", "synthetic-friend")]["jobs"][message_id]

    def test_queue_freezes_style_and_new_jobs_read_new_saved_value(self):
        first = self.enqueue("msg-1")
        self.snapshot["values"]["replyStyle"] = "polite"
        self.snapshot["revision"] = 1
        second = self.enqueue("msg-2")
        self.assertEqual(first["settings"], {"values": {"replyStyle": "natural"}, "revision": 0})
        self.assertEqual(second["settings"]["values"]["replyStyle"], "polite")
        self.assertTrue(first["automatic"])
        self.assertEqual(first["parts"], ("analysis", "suggestions"))

    def test_invalid_preferences_do_not_queue_a_task_or_create_result(self):
        self.snapshot["values"]["replyStyle"] = "unsupported"
        with self.assertRaises(CoreError):
            self.enqueue("msg-1")
        self.assertFalse(self.insights.queue)
        self.assertEqual(self.insights.store("synthetic-account").rows("synthetic-friend"), [])

    def test_style_prompt_preserves_three_candidates_and_manual_copy_contract(self):
        prompts = [suggestions_system(style) for style in ("natural", "brief", "polite")]
        self.assertEqual(len(set(prompts)), 3)
        for prompt in prompts:
            self.assertTrue(prompt.startswith(SUGGESTIONS_SYSTEM))
            self.assertIn("replies恰好3条", prompt)
            self.assertIn("绝不发送消息", prompt)
            self.assertIn("对方边界优先", prompt)
        self.assertIn("20 字以内", prompts[1])
        self.assertIn("礼貌温和", prompts[2])


if __name__ == "__main__":
    unittest.main()
