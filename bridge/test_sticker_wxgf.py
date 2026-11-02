"""Synthetic WXGF mapping, profile and bounded decoder regressions; no live data.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
"""
import hashlib
import io
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from media_assets import LocalMedia, MediaUnavailable
from sticker_wxgf import _bounded_process, _picture, decode_still, local_still


def container(payload, width=32, height=32):
    return (b"wxgf\x12\x00\x00" + width.to_bytes(2, "big") + height.to_bytes(2, "big") +
            bytes.fromhex("00000000400f22") + len(payload).to_bytes(4, "big") + payload)


class WxgfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = LocalMedia._ffmpeg()
        generated = subprocess.run([cls.ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "color=c=red:s=32x32:r=1", "-frames:v", "1", "-c:v", "libx265", "-x265-params",
            "pools=none:frame-threads=1:log-level=error", "-f", "hevc", "pipe:1"],
            capture_output=True, timeout=15, creationflags=0x08000000 if os.name == "nt" else 0)
        if generated.returncode:
            raise AssertionError("synthetic-hevc-encoder-failed")
        cls.payload = generated.stdout
        cls.wxgf = container(cls.payload)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.account = self.root / "account"
        self.identifier = "a" * 32
        self.index = self.account / "db_storage" / "emoticon" / "emoticon.db"
        self.index.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.index)) as db, db:
            db.execute("CREATE TABLE kNonStoreEmoticonTable(md5 TEXT,extern_md5 TEXT)")
            db.execute("INSERT INTO kNonStoreEmoticonTable VALUES (?,?)",
                       (self.identifier, hashlib.md5(self.wxgf).hexdigest()))
        self.db = SimpleNamespace(account_dir=self.account,
            _db_files=[("emoticon/emoticon.db", self.index, 0)], _open=lambda _rel: sqlite3.connect(self.index))
        self.key = bytes(range(16))
        self.path = self.account / "business" / "emoticon" / "Persist" / self.identifier[:2] / self.identifier
        self.path.parent.mkdir(parents=True)
        self.install()

    def install(self, plain=None):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        pad = PKCS7(128).padder()
        padded = pad.update(self.wxgf if plain is None else plain) + pad.finalize()
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.key)).encryptor()
        self.path.write_bytes(cipher.update(padded) + cipher.finalize())

    def read(self, key=None):
        return local_still(self.db, self.identifier, lambda _path: self.key if key is None else key, lambda: self.ffmpeg)

    def test_real_decoder_preserves_synthetic_single_frame_color(self):
        jpeg = decode_still(self.wxgf, self.ffmpeg)
        with Image.open(io.BytesIO(jpeg)) as image:
            self.assertEqual(image.size, (32, 32))
            r, g, b = image.getpixel((16, 16))
            self.assertGreater(r, 200)
            self.assertLess(g, 20)
            self.assertLess(b, 20)

    def test_exact_mapping_cbc_and_analysis_only_no_derived_cache(self):
        before = self.path.read_bytes()
        engine = LocalMedia(self.db, None, self.root / "output")
        engine._emoticon_keys = SimpleNamespace(resolve=lambda _path: self.key)
        row = {"local_type": 47, "decoded_content": f'<msg><emoji md5="{self.identifier}"/></msg>'}
        with patch("sticker_download.download_sticker", side_effect=MediaUnavailable("unavailable")):
            result = engine.analysis("synthetic", "b" * 64, row, {}, frames=4)
        self.assertEqual(len(result["items"]), 1)
        self.assertNotIn("durationMs", result)
        self.assertNotIn("sampling", result)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(list((self.root / "output").rglob("*.jpg")))
        self.assertFalse(list((self.root / "output").rglob("*.sticker")))
        with patch("sticker_download.download_sticker", side_effect=MediaUnavailable("unavailable")):
            with self.assertRaises(MediaUnavailable):
                engine.resolve("synthetic", "b" * 64, row, "original")

    def test_available_original_animation_wins_over_static_external_alias(self):
        from media_assets import MediaAsset
        gif = io.BytesIO()
        Image.new("RGB", (32, 32), "red").save(gif, "GIF", save_all=True,
            append_images=[Image.new("RGB", (32, 32), "blue")], duration=100, loop=0)
        engine = LocalMedia(self.db, None, self.root / "output")
        row = {"local_type": 47, "decoded_content": f'<msg><emoji md5="{self.identifier}"/></msg>'}
        with patch.object(engine, "resolve", return_value=MediaAsset("image/gif", data=gif.getvalue())), \
                patch("sticker_wxgf.local_still", side_effect=AssertionError("original animation replaced")):
            result = engine.analysis("synthetic", "b" * 64, row, {}, frames=4)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["durationMs"], 200)
        self.assertEqual(result["sampling"], "animated-frames")

    def test_current_mapping_rechecked_no_provenance_free_reuse(self):
        self.assertTrue(self.read().startswith(b"\xff\xd8\xff"))
        with closing(sqlite3.connect(self.index)) as db, db:
            db.execute("UPDATE kNonStoreEmoticonTable SET extern_md5=?", ("b" * 32,))
        with self.assertRaises(MediaUnavailable):
            self.read()

    def test_missing_ambiguous_invalid_and_wrong_mapping_rejected_before_decode(self):
        for rows in ([], [(self.identifier, "bad")], [(self.identifier, "b" * 32)],
                     [(self.identifier, hashlib.md5(self.wxgf).hexdigest())] * 2,
                     [("b" * 32, hashlib.md5(self.wxgf).hexdigest())]):
            with self.subTest(rows=len(rows)):
                with closing(sqlite3.connect(self.index)) as db, db:
                    db.execute("DELETE FROM kNonStoreEmoticonTable")
                    db.executemany("INSERT INTO kNonStoreEmoticonTable VALUES (?,?)", rows)
                with patch("sticker_wxgf.decode_still", side_effect=AssertionError("untrusted input")):
                    with self.assertRaises(MediaUnavailable):
                        self.read()

    def test_wrong_key_truncated_or_unencrypted_bytes_rejected(self):
        with self.assertRaises(MediaUnavailable):
            self.read(b"z" * 16)
        encrypted = self.path.read_bytes()
        for bad in (encrypted[:-1], self.wxgf, encrypted + b"x" * 16):
            self.path.write_bytes(bad)
            with self.assertRaises(MediaUnavailable):
                self.read()

    def test_exact_month_path_is_supported(self):
        target = self.account / "cache" / "2026-10" / "Emoticon" / self.identifier[:2] / self.identifier
        target.parent.mkdir(parents=True)
        self.path.rename(target)
        self.assertTrue(self.read().startswith(b"\xff\xd8\xff"))

    def test_external_account_database_or_noncanonical_path_rejected(self):
        self.db._db_files = [("emoticon/emoticon.db", self.root / "other" / "db_storage" / "emoticon" / "emoticon.db", 0)]
        with self.assertRaisesRegex(MediaUnavailable, "emoticon-database"):
            self.read()

    def test_reparse_file_and_parent_rejected(self):
        for blocked in (self.path, self.path.parent, self.index, self.index.parent):
            with self.subTest(component=blocked.name), patch("media_assets._reparse", side_effect=lambda p: p == blocked):
                with patch("sticker_wxgf.decode_still", side_effect=AssertionError("reparse followed")):
                    with self.assertRaises(MediaUnavailable):
                        self.read()

    def test_unknown_alpha_animation_tail_and_oversized_profiles_rejected(self):
        mutated = bytearray(self.wxgf)
        mutated[15] = 0x81  # Alpha/animated profiles are outside the supported subset.
        for bad in (bytes(mutated), self.wxgf + b"tail", container(self.payload * 2),
                    container(self.payload, 65535, 65535), b"wxgf", b"x" * (8 * 1024 * 1024 + 1)):
            with self.assertRaises(MediaUnavailable):
                _picture(bad)

    def test_auxiliary_layer_forbidden_bit_temporal_zero_rejected(self):
        offset = 22 + 4  # First VPS header in the synthetic Annex-B stream.
        for at, value in ((offset, self.wxgf[offset] | 128), (offset + 1, 9), (offset + 1, 0)):
            data = bytearray(self.wxgf)
            data[at] = value
            with self.assertRaises(MediaUnavailable):
                _picture(bytes(data))

    def test_nal_count_stops_scan_after_bounded_number_of_matches(self):
        match = SimpleNamespace(start=lambda: 0)
        visited = []

        def many():
            for index in range(1000000):
                visited.append(index)
                yield match

        with patch("sticker_wxgf.re.finditer", return_value=many()), self.assertRaises(MediaUnavailable):
            _picture(self.wxgf)
        self.assertEqual(len(visited), 65)

    def test_cropping_mismatch_rejected(self):
        with self.assertRaises(MediaUnavailable):
            decode_still(container(self.payload, 32, 31), self.ffmpeg)

    def test_decoder_error_gray_and_second_frame_rejected(self):
        good = b"[info] n: 0 fmt:yuv420p s:32x32\n"
        rgb = b"\xff\x00\x00" * 1024
        for output, log in ((rgb, good + b"[error] damaged bitstream"), (rgb, good.replace(b"yuv420p", b"gray")),
                            (rgb * 2, good + good.replace(b"n: 0", b"n: 1")), (rgb[:-1], good)):
            with patch("sticker_wxgf._bounded_process", return_value=(output, log)):
                with self.assertRaises(MediaUnavailable):
                    decode_still(self.wxgf, self.ffmpeg)

    def test_process_stdout_stderr_caps_and_timeout_kill(self):
        commands = [("import sys;sys.stdout.buffer.write(b'x'*1000000)", 100, 2),
                    ("import sys;sys.stderr.buffer.write(b'x'*1000000)", 100, 2),
                    ("import os\nwhile True: os.write(1,b'x'*1024);os.write(2,b'y'*1024)", 100, 2),
                    ("import time;time.sleep(10)", 100, .1)]
        processes = []
        spawn = subprocess.Popen

        def track(*args, **kwargs):
            process = spawn(*args, **kwargs)
            processes.append(process)
            return process

        for code, limit, timeout in commands:
            with self.subTest(mode=code[:20]), patch("sticker_wxgf.subprocess.Popen", side_effect=track):
                with self.assertRaises(MediaUnavailable):
                    _bounded_process([sys.executable, "-c", code], b"", limit, timeout)
                self.assertIsNotNone(processes[-1].poll())
                self.assertTrue(all(s.closed for s in (processes[-1].stdin, processes[-1].stdout, processes[-1].stderr)))

    def test_real_two_frame_stream_rejected_without_truncating_to_first(self):
        generated = subprocess.run([self.ffmpeg, "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "testsrc2=s=32x32:r=1", "-frames:v", "2", "-c:v", "libx265", "-x265-params",
            "pools=none:frame-threads=1:log-level=error", "-f", "hevc", "pipe:1"],
            capture_output=True, timeout=15, creationflags=0x08000000 if os.name == "nt" else 0)
        self.assertEqual(generated.returncode, 0)
        with self.assertRaises(MediaUnavailable):
            decode_still(container(generated.stdout), self.ffmpeg)

    def test_oversized_file_and_missing_mapping_never_get_key(self):
        self.path.write_bytes(b"x" * (8 * 1024 * 1024 + 32))
        with self.assertRaises(MediaUnavailable):
            local_still(self.db, self.identifier, lambda _p: self.fail("oversized source read"), lambda: self.ffmpeg)
        self.install()
        with closing(sqlite3.connect(self.index)) as db, db:
            db.execute("DELETE FROM kNonStoreEmoticonTable")
        with self.assertRaises(MediaUnavailable):
            local_still(self.db, self.identifier, lambda _p: self.fail("mapping missing"), lambda: self.ffmpeg)


if __name__ == "__main__":
    unittest.main()
