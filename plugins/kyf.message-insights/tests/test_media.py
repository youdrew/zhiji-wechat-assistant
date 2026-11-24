"""Images attached to the analysis and reply calls: frames, quotas and numbering. Synthetic data only.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import base64
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugin-sdk/python"))
sys.path.insert(0, str(ROOT / "plugins/kyf.message-insights/backend"))
from media import MAX_ATTACHED_IMAGES, MediaObservations, attachments  # noqa: E402


def row(message_id, kind):
    return {"msg_id": message_id, "kind": kind, "raw": json.dumps({"media": {"kind": kind}})}


def image(tag, time_ms=None):
    item = {"mime": "image/jpeg", "data": base64.b64encode(tag.encode() * 3).decode()}
    return {**item, "timeMs": time_ms} if time_ms is not None else item


class FakeTask:
    account, user = "synthetic-account", "synthetic-friend"

    def __init__(self, payloads):
        self.payloads, self.queries = payloads, []
        self.core = SimpleNamespace(get=self.get)

    def get(self, path, **query):
        self.queries.append(query)
        value = self.payloads[query["id"]]
        if isinstance(value, Exception):
            raise value
        return value

    def check(self):
        pass


class CollectImagesTests(unittest.TestCase):
    def test_animated_sticker_is_asked_for_four_frames_and_kept_in_order(self):
        frames = [image(str(index), index * 100) for index in range(4)]
        task = FakeTask({"s": {"kind": "sticker", "sampling": "animated-frames", "items": frames}})
        found = MediaObservations.collect_images(task, [row("s", "sticker")], priority_ids=["s"])
        self.assertEqual(task.queries[0]["frames"], 4)
        self.assertTrue(found["s"]["animated"])
        self.assertEqual([item["data"] for item in found["s"]["items"]], [item["data"] for item in frames])
        self.assertNotIn("timeMs", found["s"]["items"][0])

    def test_pictures_are_read_without_frames_and_unreadable_ones_keep_a_note(self):
        task = FakeTask({"a": {"kind": "image", "items": [image("a")]},
                         "b": {"error": "not-current"}, "c": RuntimeError("synthetic")})
        found = MediaObservations.collect_images(task, [row("a", "image"), row("b", "image"), row("c", "image")])
        self.assertNotIn("frames", task.queries[0])
        self.assertEqual(found["a"]["kind"], "image")
        self.assertIn("不在本次重新核验", found["b"])
        self.assertIn("无法读取", found["c"])

    def test_round_media_first_and_a_late_animation_gives_up_frames_not_its_place(self):
        animated = {"kind": "sticker", "sampling": "animated-frames",
                    "items": [image(str(index), index) for index in range(4)]}
        still = {"kind": "sticker", "items": [image("s")]}
        rows = [row("old", "image"), row("r1", "sticker"), row("r2", "sticker"), row("r3", "sticker")]
        task = FakeTask({"old": {"kind": "image", "items": [image("o")]}, "r1": animated, "r2": still, "r3": animated})
        found = MediaObservations.collect_images(task, rows, priority_ids=["r1", "r2", "r3"])
        self.assertNotIn("old", found, "at most three media messages, round first")
        self.assertEqual(len(found["r3"]["items"]), 4)
        self.assertFalse(found["r2"]["animated"])
        self.assertEqual([item["data"] for item in found["r1"]["items"]],
                         [animated["items"][index]["data"] for index in (0, 2, 3)], "first and last frame kept")
        self.assertEqual(4 + 1 + 3, MAX_ATTACHED_IMAGES)

    def test_no_room_left_says_so(self):
        animated = {"kind": "sticker", "sampling": "animated-frames",
                    "items": [image(str(index), index) for index in range(4)]}
        task = FakeTask({key: animated for key in ("a", "b", "c")})
        found = MediaObservations.collect_images(task, [row(key, "sticker") for key in ("a", "b", "c")])
        self.assertIn("额度已用完", found["a"])


class AttachmentsTests(unittest.TestCase):
    def test_images_are_numbered_in_sending_order_and_dropped_messages_renumber(self):
        collected = {"a": {"kind": "sticker", "animated": True, "items": [image("1"), image("2")]},
                     "b": {"kind": "image", "animated": False, "items": [image("3")]},
                     "c": "[媒体未读取：本轮媒体读取额度已用完。]"}
        notes, images = attachments(collected)
        self.assertEqual(len(images), 3)
        self.assertIn('"images":"图1至图2"', notes["a"])
        self.assertIn("动态表情按时间顺序的 2 帧", notes["a"])
        self.assertIn('"images":"图3"', notes["b"])
        self.assertEqual(notes["c"], collected["c"])
        notes, images = attachments(collected, keep={"b"})
        self.assertNotIn("a", notes)
        self.assertIn('"images":"图1"', notes["b"])
        self.assertEqual(images, [{"mime": "image/jpeg", "data": image("3")["data"]}])


if __name__ == "__main__":
    unittest.main()
