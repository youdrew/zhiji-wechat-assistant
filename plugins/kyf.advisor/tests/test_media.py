"""Synthetic media-context boundaries; no personal data or external inference."""
# Copyright 2026 Know Your Friends contributors. Apache-2.0.
import base64
import json
import tempfile
import threading
import unittest
from pathlib import Path

from model_context import _format_line
from media import MediaObservations, media_metadata, row_media
from support import build_service
from contracts import AdvisorError as AccountChangedError
from support import FakeSource, FakeRuntime, FakeModelStore, make_message, wait_until, wire_media

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
ACCOUNT = "synthetic-account-1"
USER = "synthetic-friend"


def row(number, kind="image"):
    return {"msg_id": "synthetic-%d" % number, "kind": kind, "raw": json.dumps({"media": {"kind": kind}})}


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.source = FakeSource()
        self.source.media_analysis = lambda user, mid: {"kind": "image", "items": [{"bytes": PNG, "mime": "image/png"}]}
        self.media = MediaObservations()
        self.requests = []

    def inspect(self, payload):
        self.requests.append(payload)
        return {"text": "合成画面是一块白色区域。"}

    def collect(self, rows, **kwargs):
        options = {"account": ACCOUNT, "user": USER, "scope": "source-a", "model_fp": "model-a",
                   "read": lambda mid, frames=None: wire_media(self.source.media_analysis(USER, mid)),
                   "inspect": self.inspect, "check": lambda: None, **kwargs}
        return self.media.collect(rows, **options)

    def test_only_recent_three_visual_messages_and_real_bytes(self):
        result = self.collect([row(index) for index in range(40)])
        self.assertEqual(set(result), {"synthetic-37", "synthetic-38", "synthetic-39"})
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(base64.b64decode(self.requests[0]["images"][0]["data"]), PNG)
        self.assertIn("model_visual_observation", result["synthetic-39"])
        self.requests.clear()
        self.assertEqual(self.collect([row(1)] + [row(index, "text") for index in range(2, 25)]), {})
        self.assertEqual(self.requests, [])

    def test_cache_is_scoped_by_contact_model_and_bytes(self):
        self.collect([row(1)])
        self.collect([row(1)])
        self.assertEqual(len(self.requests), 1)
        self.collect([row(1)], user="different-friend")
        self.collect([row(1)], model_fp="different-model")
        self.source.media_analysis = lambda *_: {"kind": "image", "items": [{"bytes": PNG + b"changed", "mime": "image/png"}]}
        self.collect([row(1)])
        self.assertEqual(len(self.requests), 4)
        self.media.clear_account(ACCOUNT)
        self.assertFalse(self.media.cache)

    def test_video_reports_exact_samples_and_no_audio(self):
        self.source.media_analysis = lambda *_: {"kind": "video", "items": [
            {"bytes": PNG, "mime": "image/png", "timeMs": offset} for offset in (0, 1500, 3000)]}
        result = self.collect([row(1, "video"), row(2, "video"), row(3, "video")])
        self.assertEqual(len(self.requests), 2)  # Six frames total.
        self.assertIn("额度已用完", result["synthetic-1"])
        evidence = json.loads(result["synthetic-3"].split(" ", 1)[1])
        self.assertEqual(evidence["frameTimesMs"], [0, 1500, 3000])
        self.assertFalse(evidence["audioAnalyzed"])
        self.assertFalse(evidence["fullVideoAnalyzed"])

    def test_provider_failure_is_explicitly_unread_and_not_cached(self):
        def fail(_):
            raise RuntimeError("synthetic provider failed")
        result = self.collect([row(1)], inspect=fail)
        self.assertIn("媒体未读取", result["synthetic-1"])
        self.assertIn("视觉模型读取失败", result["synthetic-1"])
        self.assertFalse(self.media.cache)

    def test_sticker_metadata_numeric_and_legacy_type_reach_visual_model_as_pixels(self):
        self.assertEqual(media_metadata({"kind": "sticker", "width": 40, "url": "not-persisted"}),
                         {"kind": "sticker", "width": 40})
        for original_type in (47, 11000, "动画表情"):
            self.assertEqual(row_media({"kind": "other", "raw": json.dumps({"type": original_type})}), {"kind": "sticker"})
        self.source.media_analysis = lambda *_: {"kind": "sticker", "sampling": "animated-image-first-frame",
            "items": [{"bytes": PNG, "mime": "image/png"}]}
        result = self.collect([row(1, "sticker")])
        self.assertEqual(self.requests[0]["mediaKind"], "sticker")
        self.assertEqual(base64.b64decode(self.requests[0]["images"][0]["data"]), PNG)
        evidence = json.loads(result["synthetic-1"].split(" ", 1)[1])
        self.assertEqual(evidence["kind"], "sticker")
        self.assertFalse(evidence["fullAnimationAnalyzed"])

    def test_verified_target_media_is_read_through_core_capability(self):
        calls = []
        def read(message_id, frames=None):
            calls.append((message_id, frames))
            return wire_media({"kind": "sticker", "items": [{"bytes": PNG, "mime": "image/png"}]})
        result = self.collect([row(1, "sticker")], read=read, recent_messages=100, priority_ids=["synthetic-1"])
        self.assertEqual(calls, [("synthetic-1", 4)])
        self.assertIn("本轮视觉观察", result["synthetic-1"])
        self.assertEqual(len(self.requests), 1)

    def test_animated_sticker_frames_reach_the_model_in_order_within_the_image_quota(self):
        frames = [{"bytes": PNG, "mime": "image/png", "timeMs": offset} for offset in (0, 120, 240, 360)]
        self.source.media_analysis = lambda *_: {"kind": "sticker", "sampling": "animated-frames", "items": frames}
        result = self.collect([row(1, "sticker"), row(2, "sticker")])
        self.assertEqual(len(self.requests), 1)  # Four frames, then only two images remain.
        self.assertEqual(self.requests[0]["sampling"], "animated-frames")
        self.assertEqual([image["timeMs"] for image in self.requests[0]["images"]], [0, 120, 240, 360])
        evidence = json.loads(result["synthetic-2"].split(" ", 1)[1])
        self.assertEqual(evidence["frameTimesMs"], [0, 120, 240, 360])
        self.assertIn("额度已用完", result["synthetic-1"])
        self.requests.clear()
        self.source.media_analysis = lambda *_: {"kind": "sticker", "sampling": "animated-frames",
                                                 "items": list(reversed(frames))}
        self.assertIn("媒体未读取", self.collect([row(3, "sticker")])["synthetic-3"])
        self.assertEqual(self.requests, [])

    def test_labelled_stickers_are_not_read_again(self):
        result = self.collect([row(1, "sticker"), row(2)], skip_ids=["synthetic-1"])
        self.assertEqual(set(result), {"synthetic-2"})

    def test_failed_core_verification_cannot_issue_arbitrary_history_media(self):
        result = self.collect([row(1)], read=lambda _mid: {"error": "not-current"})
        self.assertIn("未读取", result["synthetic-1"])
        self.assertEqual(self.requests, [])

    def test_target_media_has_priority_without_expanding_message_or_image_quotas(self):
        result = self.collect([row(index) for index in range(40)], recent_messages=100, priority_ids=["synthetic-1"])
        self.assertEqual(set(result), {"synthetic-1", "synthetic-38", "synthetic-39"})
        self.assertEqual(len(self.requests), 3)
        self.assertTrue(all("本轮视觉观察" in value for value in result.values()))

    def test_core_rejects_snapshot_ids_outside_current_window(self):
        def read(message_id):
            if message_id != "synthetic-2":
                return {"error": "not-current"}
            return wire_media(self.source.media_analysis(USER, message_id))
        result = self.collect([row(1), row(2)], read=read)
        self.assertIn("重新核验", result["synthetic-1"])
        self.assertEqual(len(self.requests), 1)
        self.assertNotIn("not-in-frozen-snapshot", result)

    def test_animated_first_frame_is_provenance_and_changes_the_cache_key(self):
        self.collect([row(1)])
        self.source.media_analysis = lambda *_: {"kind": "image", "sampling": "animated-image-first-frame",
            "items": [{"bytes": PNG, "mime": "image/png"}]}
        result = self.collect([row(1)])
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(self.requests[-1]["sampling"], "animated-image-first-frame")
        evidence = json.loads(result["synthetic-1"].split(" ", 1)[1])
        self.assertFalse(evidence["fullAnimationAnalyzed"])

    def test_account_switch_during_read_cannot_become_a_fallback(self):
        changed = False
        def check():
            if changed:
                raise AccountChangedError("account-changed", "synthetic switch")
        def read(*_):
            nonlocal changed
            changed = True
            return {"kind": "image", "items": [{"bytes": PNG, "mime": "image/png"}]}
        self.source.media_analysis = read
        with self.assertRaises(AccountChangedError):
            self.collect([row(1)], check=check)
        self.assertEqual(self.requests, [])

    def test_oversize_wrong_kind_and_missing_video_timestamp_never_reach_model(self):
        cases = [
            {"kind": "image", "items": [{"bytes": b"x" * (2 * 1024 * 1024 + 1), "mime": "image/png"}]},
            {"kind": "video", "items": [{"bytes": PNG, "mime": "image/png"}]},
            {"kind": "image", "items": [{"bytes": PNG, "mime": "text/html"}]},
        ]
        for payload in cases:
            self.source.media_analysis = lambda *_, payload=payload: payload
            self.assertIn("媒体未读取", self.collect([row(1)])["synthetic-1"])
        self.source.media_analysis = lambda *_: cases[1]
        self.assertIn("媒体未读取", self.collect([row(1, "video")])["synthetic-1"])
        self.assertEqual(self.requests, [])


class MediaServiceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = FakeSource()
        messages = [make_message(1, side="self", kind="image", text="[图片]"),
                    make_message(2, kind="voice", text="[语音]")]
        messages[0]["media"] = {"kind": "image"}
        messages[1]["media"] = {"kind": "voice", "transcript": "周末一起喝咖啡", "transcriptSource": "wechat"}
        self.source.add_messages(USER, messages)
        self.source.media_analysis = lambda *_, **__: {"kind": "image", "items": [{"bytes": PNG, "mime": "image/png"}]}
        self.runtime = FakeRuntime()
        self.inspections = []
        def inspect(config, request, on_event, cancel):
            self.inspections.append(request)
            return {"text": "合成图片显示白色区域。"}
        self.runtime.inspect_media = inspect
        self.service = build_service(self.source, FakeModelStore(context_tokens=32768), self.root,
                                      runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)

    def test_manual_context_contains_visual_observation_and_wechat_transcript_not_bytes(self):
        result = self.service.start(ACCOUNT, USER, "builtin:advisor", None, "给我建议", "synthetic-media-request")
        run_id = result["run"]["id"]
        done = wait_until(lambda: self.service._runs[run_id].state if self.service._runs[run_id].state != "running" else None)
        self.assertEqual(done, "done")
        self.assertEqual(len(self.inspections), 1)
        context = self.runtime.respond_calls[0][1]["contextFileText"]
        self.assertIn("我: [图片]", context)
        self.assertIn("model_visual_observation", context)
        self.assertIn("周末一起喝咖啡", context)
        self.assertIn("微信已有语音转写", context)
        self.assertIn("最多 3 条图片/表情/视频", context)
        self.assertNotIn(base64.b64encode(PNG).decode(), context)
        rows = self.service.stores.account(ACCOUNT).context_messages(USER, None)
        self.assertNotIn("data", json.loads(rows[0]["raw"]).get("media", {}))
        self.assertNotIn("白色区域", rows[0]["raw"])

    def test_manual_assistant_sticker_is_preserved_in_projection_and_visually_read(self):
        message = self.source.users[USER][0]
        message.update(kind="other", type="动画表情", text="[表情]", media={"kind": "sticker"})
        self.source.media_analysis = lambda *_, **__: {"kind": "sticker", "sampling": "animated-image-first-frame",
            "items": [{"bytes": PNG, "mime": "image/png"}]}
        run_id = self.service.start(ACCOUNT, USER, "builtin:advisor", None, "结合这张表情给我建议", "synthetic-sticker-request")["run"]["id"]
        self.assertEqual(wait_until(lambda: self.service._runs[run_id].state if self.service._runs[run_id].state != "running" else None), "done")
        self.assertEqual(self.inspections[0]["mediaKind"], "sticker")
        self.assertEqual(base64.b64decode(self.inspections[0]["images"][0]["data"]), PNG)
        context = self.runtime.respond_calls[0][1]["contextFileText"]
        self.assertIn("model_visual_observation", context)
        self.assertIn("fullAnimationAnalyzed", context)
        self.assertEqual(json.loads(self.service.stores.account(ACCOUNT).context_messages(USER, None)[0]["raw"])["media"]["kind"], "sticker")

    def test_switching_only_image_model_refreshes_observations_and_preserves_main_cache_identity(self):
        models = self.service.plugin.models
        main = models.service_for("main")
        thread_id = None

        def ask(identifier):
            nonlocal thread_id
            started = self.service.start(ACCOUNT, USER, "builtin:advisor", thread_id, "看看图片", identifier)
            thread_id = started["threadId"]
            run = self.service._runs[started["run"]["id"]]
            self.assertEqual(wait_until(lambda: run.state if run.state != "running" else None), "done")

        ask("main-image")
        self.assertEqual(len(self.inspections), 1)
        self.assertEqual({key[4] for key in self.service.media_observations.cache}, {main["fingerprint"]})
        image = {**main, "id": "image-model", "model": "image-model", "roles": ["image"],
                 "stamp": "image-stamp", "fingerprint": "image-fingerprint"}
        models.service_for = lambda role, refresh=False: image if role == "image" else main
        ask("separate-image")
        self.assertEqual(len(self.inspections), 2)
        self.assertEqual({key[4] for key in self.service.media_observations.cache},
                         {main["fingerprint"], image["fingerprint"]})
        ask("same-image")
        self.assertEqual(len(self.inspections), 2)
        config = self.service._resolve_config(service=main)
        with self.assertRaises(AccountChangedError):
            self.service._media_service(config, "previous-image-stamp")

    def test_voice_transcript_update_changes_projection_revision(self):
        self.service.prepare_context(ACCOUNT, USER)
        self.assertTrue(wait_until(lambda: self.source.history_status(ACCOUNT, USER)["state"] == "ready"))
        self.service.stores.account(ACCOUNT).sync(USER)
        previous = self.service.stores.account(ACCOUNT).context(USER)
        self.source.users[USER][1]["media"]["transcript"] = "下周再约"
        self.source.history_prepare(ACCOUNT, USER)
        self.service.stores.account(ACCOUNT).sync(USER)
        self.assertNotEqual(self.service.stores.account(ACCOUNT).context(USER)["epoch"], previous["epoch"])
        self.assertIn("下周再约", _format_line(self.service.stores.account(ACCOUNT).context_messages(USER, None)[1]))

    def test_metadata_never_copies_arbitrary_bytes_paths_or_non_wechat_transcripts(self):
        metadata = media_metadata({"kind": "voice", "bytes": PNG, "path": "private/file", "data": "base64",
                                   "transcript": "untrusted", "transcriptSource": "guessed"})
        self.assertEqual(metadata, {"kind": "voice"})

    def test_ready_projection_after_restart_requests_media_from_core_again(self):
        self.service.prepare_context(ACCOUNT, USER)
        self.service.stores.account(ACCOUNT).sync(USER)
        self.service.shutdown()
        calls = []
        def media(account, user, message_id, **kwargs):
            calls.append((account, user, message_id))
            return wire_media({"kind": "image", "items": [{"bytes": PNG, "mime": "image/png"}]})
        self.source.media = media
        self.service = build_service(self.source, FakeModelStore(context_tokens=32768), self.root,
                                      runtime_factory=lambda: self.runtime)
        self.addCleanup(self.service.shutdown)
        started = self.service.start(ACCOUNT, USER, "builtin:advisor", None, "看看最近图片", "after-restart")
        identifier = started["run"]["id"]
        self.assertTrue(wait_until(lambda: self.service._runs[identifier].state != "running"))
        self.assertEqual(self.service._runs[identifier].state, "done")
        self.assertEqual(calls, [(ACCOUNT, USER, self.source.users[USER][0]["id"])])
        self.assertIn("model_visual_observation", self.runtime.respond_calls[-1][1]["contextFileText"])


if __name__ == "__main__":
    unittest.main()
