"""Mechanical media checks using generated pixels only; no model or chat data."""
from __future__ import annotations

import base64
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import core_external_media as media
from core_external_media import evidence_frames, frame_alignment, inspect_material, model_image


class ExternalMediaQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def picture(self, name, size, *, noise=False):
        path = self.root / name
        image = (Image.fromarray(np.random.default_rng(17).integers(0, 256, (*size[::-1], 3), dtype=np.uint8))
                 if noise else Image.new("RGB", size, "white"))
        image.save(path)
        return path

    def decode(self, encoded):
        mime, payload = encoded.split(",", 1)
        data = base64.b64decode(payload)
        return mime, data, Image.open(io.BytesIO(data))

    def test_low_resolution_view_upscales_without_modifying_evidence(self):
        path = self.picture("small.png", (220, 480))
        original = path.read_bytes()
        mime, _, view = self.decode(model_image(path, min_width=660, format="png"))
        self.assertEqual(mime, "data:image/png;base64")
        self.assertEqual(view.size, (660, 1440))
        self.assertEqual(path.read_bytes(), original)
        _, _, legacy = self.decode(model_image(path))
        self.assertEqual(legacy.size, (220, 480))
        self.assertEqual(legacy.format, "JPEG")

    def test_image_size_and_payload_remain_bounded(self):
        ordinary = self.picture("ordinary.png", (800, 1200))
        _, _, ordinary_view = self.decode(model_image(ordinary, min_width=660, format="png"))
        self.assertEqual(ordinary_view.size, (800, 1200))
        tall = self.picture("tall.png", (220, 2000))
        _, _, tall_view = self.decode(model_image(tall, min_width=1800, max_upscale=20, max_width=10000, format="png"))
        self.assertLessEqual(tall_view.width, 1800)
        self.assertLessEqual(tall_view.height, 2400)
        self.assertLessEqual(tall_view.width, 660)
        noisy = self.picture("noise.png", (1200, 1600), noise=True)
        mime, data, _ = self.decode(model_image(noisy, max_bytes=20_000, format="png"))
        self.assertEqual(mime, "data:image/png;base64")
        self.assertLessEqual(len(data), 20_000)

    def chat(self, name, offset=0, seed=29):
        canvas = Image.new("RGB", (360, 1500), "#f3f3f3")
        draw = ImageDraw.Draw(canvas)
        rng = np.random.default_rng(seed)
        font = ImageFont.load_default(size=17)
        for index, top in enumerate(range(10, 1450, 81)):
            left = 18 if index % 2 else 123
            right = min(344, left + int(rng.integers(155, 220)))
            color = "#d6eec8" if index % 2 else "#ffffff"
            draw.rounded_rectangle((left, top, right, top + 62), radius=9, fill=color, outline="#c5c5c5")
            text = " ".join(str(value) for value in rng.integers(1, 9999, 4))
            draw.text((left + 6, top + 5), f"Message {index}: {text[:12]}", font=font, fill="#242424")
            draw.text((left + 6, top + 30), text[10:25], font=font, fill="#414141")
        view = canvas.crop((0, offset, 360, offset + 720))
        fixed = ImageDraw.Draw(view)
        fixed.rectangle((0, 0, 359, 100), fill="#dddddd")
        fixed.text((20, 30), "Fixed app toolbar", font=font, fill="black")
        fixed.rectangle((0, 620, 359, 719), fill="#dddddd")
        fixed.text((20, 660), "Fixed composer", font=font, fill="black")
        path = self.root / name
        view.save(path)
        return path

    def test_alignment_reports_scroll_and_stationary_geometry(self):
        first = self.chat("previous.png", 100)
        second = self.chat("current.png", 188)
        result = frame_alignment(first, second)
        self.assertTrue(result["reliable"], result)
        self.assertAlmostEqual(result["offsetY"], -88 / 720, delta=.006)
        self.assertFalse(result["stationary"], result)
        still = frame_alignment(first, first)
        self.assertTrue(still["reliable"], still)
        self.assertTrue(still["stationary"], still)
        self.assertAlmostEqual(still["offsetY"], 0, delta=.002)

    def test_alignment_rejects_blank_unrelated_and_scaled_views(self):
        first = self.chat("previous.png", 100)
        blank = self.picture("blank.png", (360, 720))
        self.assertFalse(frame_alignment(blank, blank)["reliable"])
        self.assertFalse(frame_alignment(first, self.picture("smaller.png", (180, 360)))["reliable"])
        unrelated = self.chat("unrelated.png", 100, seed=113)
        self.assertFalse(frame_alignment(first, unrelated)["reliable"])
        with Image.open(first) as image:
            image.resize((432, 864)).crop((36, 72, 396, 792)).save(self.root / "zoom.png")
        self.assertFalse(frame_alignment(first, self.root / "zoom.png")["reliable"])

    def video(self, *, stationary=False):
        path = self.root / ("stationary.mp4" if stationary else "scrolling.mp4")
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 20, (360, 720))
        if not writer.isOpened():
            self.skipTest("synthetic video encoder unavailable")
        with Image.open(self.chat("video-base.png", 100)) as image:
            frame = cv2.cvtColor(np.asarray(image), cv2.COLOR_RGB2BGR)
        try:
            for index in range(40):
                current = frame.copy()
                if not stationary:
                    cv2.putText(current, str(index), (130, 300), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
                    if index == 15:
                        current = cv2.GaussianBlur(current, (19, 19), 5)
                writer.write(current)
        finally:
            writer.release()
        return path

    def test_window_sampling_is_bounded_clearer_and_retry_stable(self):
        path = self.video()
        plan = {"video": {"intervalMs": 750, "maxFrames": 240, "sampleWindowMs": 10000}}
        first = list(evidence_frames(path, "video", plan, self.root / "first"))
        second = list(evidence_frames(path, "video", plan, self.root / "second"))
        reverse = list(evidence_frames(path, "video", plan, self.root / "reverse", direction="reverse"))
        self.assertEqual([(f["key"], f["videoMs"]) for f in first], [(f["key"], f["videoMs"]) for f in second])
        self.assertEqual([(f["key"], f["videoMs"]) for f in first], [(f["key"], f["videoMs"]) for f in reversed(reverse)])
        self.assertEqual(first[0]["videoMs"], 0)
        self.assertEqual(first[-1]["videoMs"], 1950)
        self.assertTrue(all(abs(f["videoMs"] - f["anchorMs"]) <= 100 for f in first))
        self.assertTrue(all(b["videoMs"] - a["videoMs"] <= 950 for a, b in zip(first, first[1:])))
        self.assertNotEqual(next(f for f in first if f["anchorMs"] == 750)["videoMs"], 750)
        self.assertLessEqual(len(first), 240)
        with Image.open(first[0]["path"]) as retained:
            self.assertEqual(retained.size, (360, 720))

    def test_stationary_video_retains_anchor_coverage_and_explicit_maximum(self):
        path = self.video(stationary=True)
        plan = {"video": {"sampleWindowMs": 100}}
        frames = list(evidence_frames(path, "video", plan, self.root / "still"))
        self.assertEqual(len(frames), 4)
        self.assertEqual([f["anchorMs"] for f in frames], [0, 750, 1500, 1950])
        limited = list(evidence_frames(path, "video", {"video": {"sampleWindowMs": 100, "maxFrames": 3}}, self.root / "limited"))
        self.assertLessEqual(len(limited), 3)

    def test_failed_tail_seek_recovers_the_exact_last_frame_in_both_directions(self):
        path = self.video()
        real_capture = cv2.VideoCapture
        reference = real_capture(str(path))
        last_index = int(reference.get(cv2.CAP_PROP_FRAME_COUNT)) - 1
        while True:
            ok, frame = reference.read()
            if not ok:
                break
            final_frame = frame
        reference.release()

        class FailedTailSeek:
            def __init__(self, name):
                self.capture, self.failed_seek = real_capture(name), False

            def __getattr__(self, name):
                return getattr(self.capture, name)

            def set(self, prop, value):
                self.failed_seek = prop == cv2.CAP_PROP_POS_FRAMES and value == last_index
                return self.capture.set(prop, value)

            def read(self):
                return (False, None) if self.failed_seek else self.capture.read()

        with patch.object(cv2, "VideoCapture", FailedTailSeek):
            for window in (0, 100):
                for direction in ("forward", "reverse"):
                    with self.subTest(window=window, direction=direction):
                        frames = list(evidence_frames(path, "video", {"video": {"sampleWindowMs": window}},
                            self.root / f"recover-{window}-{direction}", direction=direction))
                        last = frames[-1] if direction == "forward" else frames[0]
                        self.assertEqual(last["videoMs"], 1950)
                        self.assertTrue(np.array_equal(cv2.imread(str(last["path"])), final_frame),
                                        "the real last frame must be retained, not an earlier substitute")
                        self.assertEqual(len(frames), 4)

    def test_sequential_fallback_does_not_hide_a_genuinely_unreadable_tail(self):
        path = self.video()
        real_capture = cv2.VideoCapture

        class MissingTail:
            def __init__(self, name):
                self.capture = real_capture(name)

            def __getattr__(self, name):
                return getattr(self.capture, name)

            def read(self):
                if self.capture.get(cv2.CAP_PROP_POS_FRAMES) >= 39:
                    return False, None
                return self.capture.read()

        with patch.object(cv2, "VideoCapture", MissingTail):
            for window in (0, 100):
                with self.subTest(window=window), self.assertRaisesRegex(ValueError, "无法读取录屏"):
                    list(evidence_frames(path, "video", {"video": {"sampleWindowMs": window}}, self.root / f"missing-{window}"))

    def test_edit_list_trimmed_leading_frames_do_not_fail_the_tail(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("optional synthetic edit-list encoder unavailable")
        source, path = self.root / "keyframe-once.mp4", self.root / "trimmed.mov"
        subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc=size=360x720:rate=20:duration=2",
                        "-c:v", "mpeg4", "-g", "100", "-y", str(source)], check=True, capture_output=True)
        # Stream copy from a non-keyframe keeps the leading packets but marks
        # them discarded, as phone screen recordings do.
        subprocess.run([ffmpeg, "-v", "error", "-ss", "0.5", "-i", str(source), "-c", "copy", "-y", str(path)],
                       check=True, capture_output=True)
        capture = cv2.VideoCapture(str(path))
        counted, decoded = capture.get(cv2.CAP_PROP_FRAME_COUNT), 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            decoded, final_frame = decoded + 1, frame
        capture.release()
        if counted <= decoded:
            self.skipTest("this FFmpeg build did not write a trimming edit list")
        for window in (0, 100):
            with self.subTest(window=window):
                frames = list(evidence_frames(path, "video", {"video": {"sampleWindowMs": window}},
                                              self.root / f"edit-list-{window}"))
                self.assertEqual(frames[-1]["videoMs"], (decoded - 1) * 50)
                self.assertTrue(np.array_equal(cv2.imread(str(frames[-1]["path"])), final_frame))

    def test_irregular_video_timestamps_are_retained_for_evidence_positions(self):
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.skipTest("optional synthetic VFR encoder unavailable")
        source = self.video()
        for name, expression, last_ms in [("single-gap", "gte(N\\,15)", 2000), ("large-drift", "min(N\\,20)", 2950)]:
            with self.subTest(name=name):
                path = self.root / f"variable-{name}.mp4"
                subprocess.run([ffmpeg, "-v", "error", "-i", str(source), "-vf", f"setpts=PTS+{expression}/(20*TB)",
                                "-fps_mode", "vfr", "-c:v", "mpeg4", "-threads", "1", "-y", str(path)],
                               check=True, capture_output=True)
                metadata = inspect_material(path)
                self.assertNotEqual(metadata["fps"], 20)
                plan = {"video": {"sampleWindowMs": 100}}
                forward = list(evidence_frames(path, "video", plan, self.root / f"vfr-forward-{name}"))
                reverse = list(evidence_frames(path, "video", plan, self.root / f"vfr-reverse-{name}", direction="reverse"))
                self.assertEqual([(item["key"], item["videoMs"]) for item in forward],
                                 [(item["key"], item["videoMs"]) for item in reversed(reverse)])
                self.assertEqual(forward[-1]["videoMs"], last_ms,
                                 "evidence position comes from decoded PTS, not the average frame rate")
                self.assertNotEqual(forward[-1]["videoMs"], forward[-1]["anchorMs"])
                self.assertTrue(all(abs(item["videoMs"] - item["anchorMs"]) <= 100 for item in forward))

    def test_an_unreadable_optional_neighbor_does_not_discard_a_readable_anchor(self):
        path = self.video()
        reader = media._read_video_frame

        def one_bad_neighbor(capture, frame_index, metadata, *, check, exact=False):
            if frame_index == 13:
                raise media._UnreadableVideoFrame("synthetic optional frame is unreadable")
            return reader(capture, frame_index, metadata, check=check, exact=exact)

        with patch.object(media, "_read_video_frame", one_bad_neighbor):
            frames = list(evidence_frames(path, "video", {"video": {"sampleWindowMs": 100}}, self.root / "bad-neighbor"))
        self.assertEqual(len(frames), 4)
        self.assertTrue(any(item["anchorMs"] == 750 for item in frames))

    def test_cancellation_interrupts_sequential_seek_recovery_without_a_replacement_frame(self):
        path = self.video()
        real_capture, captures = cv2.VideoCapture, []

        class TailSeekFailure:
            def __init__(self, name):
                self.capture, self.failed_seek, self.recovering = real_capture(name), False, False
                captures.append(self)

            def __getattr__(self, name):
                return getattr(self.capture, name)

            def set(self, prop, value):
                if prop == cv2.CAP_PROP_POS_FRAMES and value == 0:
                    self.recovering = True
                self.failed_seek = prop == cv2.CAP_PROP_POS_FRAMES and value == 39
                return self.capture.set(prop, value)

            def read(self):
                return (False, None) if self.failed_seek else self.capture.read()

        class Cancelled(Exception):
            pass

        def check():
            if any(capture.recovering and capture.get(cv2.CAP_PROP_POS_FRAMES) >= 5 for capture in captures):
                raise Cancelled("synthetic cancellation")

        output = self.root / "cancelled-recovery"
        with patch.object(cv2, "VideoCapture", TailSeekFailure), self.assertRaises(Cancelled):
            list(evidence_frames(path, "video", {"video": {"sampleWindowMs": 100}}, output, direction="reverse", check=check))
        self.assertFalse(list(output.glob("*.png")))

    def test_invalid_decoded_timestamps_are_reported_instead_of_inventing_source_times(self):
        path = self.video()
        real_capture = cv2.VideoCapture
        for timestamp in (float("nan"), float("inf"), -1):
            class InvalidTimestamp:
                def __init__(self, name):
                    self.capture = real_capture(name)

                def __getattr__(self, name):
                    return getattr(self.capture, name)

                def get(self, prop):
                    return timestamp if prop == cv2.CAP_PROP_POS_MSEC else self.capture.get(prop)

            with self.subTest(timestamp=timestamp), patch.object(cv2, "VideoCapture", InvalidTimestamp), self.assertRaisesRegex(ValueError, "画面时间无法读取"):
                list(evidence_frames(path, "video", {"video": {"sampleWindowMs": 100}}, self.root / "invalid-time"))


if __name__ == "__main__":
    unittest.main()
