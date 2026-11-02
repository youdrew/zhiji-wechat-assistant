"""WeChat 4.x video attachment layouts using synthetic bytes only. Apache-2.0."""
import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_assets import LocalMedia, MediaAsset, MediaUnavailable


class VideoLocatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user = "synthetic-contact"
        self.account = self.root / "account"
        self.work = self.root / "work"
        self.work.mkdir()
        self.store = LocalMedia(SimpleNamespace(account="synthetic-owner", account_dir=self.account,
                                                workdir=self.work), None)
        self.data = b"\0\0\0\x18ftypisom" + b"synthetic-video-content" * 5
        self.digest = hashlib.md5(self.data).hexdigest()
        self.row = {"local_type": 43, "decoded_content":
                    f'<msg><videomsg md5="{self.digest}" length="{len(self.data)}"/></msg>',
                    "packed_info": b"unrelated-cache-id " + b"a" * 32}

    def install(self, user, filename):
        path = self.account / "msg" / "attach" / hashlib.md5(user.encode()).hexdigest() / "2026-10" / "Video" / filename
        path.parent.mkdir(parents=True)
        path.write_bytes(self.data)
        return path

    def test_renamed_attachment_video_is_verified_and_referenced_without_copy(self):
        self.store.settings({"mode": "reference"})
        video = self.install(self.user, "different-cache-name.mp4")
        asset = self.store.resolve(self.user, "b" * 64, self.row, "playback")
        self.assertEqual(asset.path, video)
        self.assertIsNone(asset.data)
        self.assertEqual(self.store.settings()["archiveBytes"], 0)
        self.assertEqual(self.store.settings()["cacheBytes"], 0)

    def test_same_size_and_filename_are_insufficient_if_message_hash_differs(self):
        video = self.install(self.user, self.digest + ".mp4")
        video.write_bytes(self.data[:12] + b"x" * (len(self.data) - 12))
        with self.assertRaises(MediaUnavailable):
            self.store._video(self.row, user=self.user)

    def test_other_contact_attachment_tree_is_never_used(self):
        self.install("another-synthetic-contact", self.digest + ".mp4")
        with self.assertRaises(MediaUnavailable):
            self.store._video(self.row, user=self.user)

    def test_thumbnail_inventory_does_not_exhaust_video_scan_budget(self):
        video = self.install(self.user, "different-cache-name.mp4")
        attach = self.account / "msg" / "attach" / hashlib.md5(self.user.encode()).hexdigest()
        def walk(base, **_kwargs):
            if base == attach:
                yield str(attach / "2026-09" / "Thumb"), [], [f"thumb-{index}.jpg" for index in range(20001)]
                yield str(video.parent), [], [video.name]
        with patch("media_assets.os.walk", side_effect=walk):
            self.assertEqual(self.store._video(self.row, user=self.user).path, video)

    def test_recent_validated_location_avoids_rescan_but_rechecks_file(self):
        video = self.install(self.user, "different-cache-name.mp4")
        self.store._video(self.row, user=self.user)
        with patch("media_assets.os.walk", side_effect=AssertionError("unchanged video should not rescan")):
            self.assertEqual(self.store._video(self.row, user=self.user).path, video)
        video.unlink()
        with self.assertRaises(MediaUnavailable):
            self.store._video(self.row, user=self.user)

    def test_same_size_replacement_cannot_reuse_a_previously_verified_hash(self):
        video = self.install(self.user, "different-cache-name.mp4")
        self.store._video(self.row, user=self.user)
        before = video.stat()
        video.write_bytes(self.data[:12] + b"x" * (len(self.data) - 12))
        original_stat = Path.stat
        def unchanged_stamp(path, *args, **kwargs):
            return before if path == video else original_stat(path, *args, **kwargs)
        # Deliberately hide both mtime/ctime changes: the content proof must still fail.
        with patch.object(Path, "stat", unchanged_stamp), self.assertRaises(MediaUnavailable):
            self.store._video(self.row, user=self.user)

    def test_analysis_resolves_video_once_for_three_frames(self):
        video = self.install(self.user, "different-cache-name.mp4")
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (8, 8), "blue").save(output, "JPEG")
        with patch.object(self.store, "resolve", wraps=self.store.resolve) as resolve, patch.object(
                self.store, "_frame", return_value=MediaAsset("image/jpeg", data=output.getvalue())) as frame:
            result = self.store.analysis(self.user, "b" * 64, self.row, {"durationMs": 9000})
            self.assertNotIn("error", result)
            self.assertEqual(len(result["items"]), 3)
            self.assertEqual(resolve.call_count, 1)
            self.assertEqual([call.args[1].path for call in frame.call_args_list], [video] * 3)


if __name__ == "__main__":
    unittest.main()
