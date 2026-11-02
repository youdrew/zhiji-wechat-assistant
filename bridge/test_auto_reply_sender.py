"""Synthetic sender provenance checks for automatic suggestions.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import unittest

from wechat_source import WeChatSource


class SenderProvenanceTests(unittest.TestCase):
    def test_display_fallback_is_not_evidence_of_a_friend_reply(self):
        class Database:
            account = "synthetic-account"

        source = WeChatSource(factory=lambda: Database())
        source._classified_content = lambda *_args: ("文本", "text", "synthetic message")
        record = (1, 1, 7, 1_800_000_000, b"synthetic", None, 1, 1)
        def render(senders):
            return source._render_row(Database(), "friend", (record, "message__message_1.db", senders), {}, "me")
        fallback = render({})
        self.assertEqual(fallback["side"], "other")
        self.assertEqual(fallback["senderId"], "friend")
        self.assertFalse(fallback["_senderResolved"])
        verified = render({7: "friend"})
        self.assertTrue(verified["_senderResolved"])
        own = render({7: "me"})
        self.assertEqual(own["side"], "self")
        self.assertTrue(own["_senderResolved"])


if __name__ == "__main__":
    unittest.main()
