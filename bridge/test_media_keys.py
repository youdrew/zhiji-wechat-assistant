"""Synthetic image-key ownership, initialization and stable-cache regressions.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from media_keys import (ImageKeyResolver, EmoticonKeyResolver, V2_MAGIC, _cfg_candidates,
                        derive_image_keys, derive_emoticon_key, validates)


class MediaKeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = SimpleNamespace(account_dir=self.root, wxid="synthetic-user")
        self.selected = SimpleNamespace(account_dir=self.root, processes=((123, 456.0),))
        self.value = 123456
        self.keys = derive_image_keys(self.value, self.db.wxid)
        cipher = Cipher(algorithms.AES(self.keys[0].encode()), modes.ECB()).encryptor()
        self.probe = cipher.update(b"\xff\xd8\xff" + b"synthetictest") + cipher.finalize()
        self.path = self.root / "synthetic.dat"
        self.path.write_bytes(V2_MAGIC + bytes(9) + self.probe)
        self.resolver = ImageKeyResolver(self.db)
        stable = tempfile.TemporaryDirectory()
        self.addCleanup(stable.cleanup)
        self.stable = Path(stable.name)
        environment = patch.dict("os.environ", {"WECHATAUTO_KEYS_DIR": str(self.stable)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_owned_candidate_validates_requested_file_and_is_saved_outside_wechat(self):
        before = sorted(path.name for path in self.root.iterdir())
        with patch("media_keys._selection", return_value=self.selected), patch("media_keys._read_cfg", return_value=[
                (self.value, "another-user"), (self.value, self.db.wxid)]) as read:
            self.assertEqual(self.resolver.resolve(self.path), self.keys)
            self.assertEqual(self.resolver.resolve(self.path), self.keys)
            self.assertEqual(read.call_count, 1)
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), before)
        saved = json.loads((self.stable / (self.root.name + ".media.json")).read_text(encoding="utf-8"))
        self.assertEqual(saved, {"wxid": self.db.wxid, "image": list(self.keys)})

    def test_saved_key_opens_files_after_restart_without_reading_wechat(self):
        with patch("media_keys._selection", return_value=self.selected), patch("media_keys._read_cfg", return_value=[
                (self.value, self.db.wxid)]):
            self.assertEqual(self.resolver.resolve(self.path), self.keys)
        restarted = ImageKeyResolver(self.db)
        with patch("media_keys._selection", return_value=self.selected), patch("media_keys._read_cfg") as read:
            self.assertEqual(restarted.resolve(self.path), self.keys)
            read.assert_not_called()

    def test_saved_key_for_another_wxid_or_failing_validation_still_reads_wechat(self):
        target = self.stable / (self.root.name + ".media.json")
        target.write_text(json.dumps({"wxid": "someone-else", "image": list(self.keys)}), encoding="utf-8")
        with patch("media_keys._selection", return_value=self.selected), patch("media_keys._read_cfg", return_value=[
                (self.value, self.db.wxid)]) as read:
            self.assertEqual(self.resolver.resolve(self.path), self.keys)
            self.assertEqual(read.call_count, 1)
        target.write_text(json.dumps({"wxid": self.db.wxid, "image": ["0" * 16, 0]}), encoding="utf-8")
        restarted = ImageKeyResolver(self.db)
        with patch("media_keys._selection", return_value=self.selected), patch("media_keys._read_cfg", return_value=[
                (self.value, self.db.wxid)]) as read:
            self.assertEqual(restarted.resolve(self.path), self.keys)
            self.assertEqual(read.call_count, 1)
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["image"], list(self.keys))

    def test_unowned_account_and_wrong_candidate_never_authorize_a_key(self):
        other = SimpleNamespace(account_dir=self.root / "another-account", processes=self.selected.processes)
        with patch("media_keys._selection", return_value=other), patch("media_keys._read_cfg") as read:
            self.assertIsNone(self.resolver.resolve(self.path))
            read.assert_not_called()
        with patch("media_keys._selection", return_value=self.selected), patch("media_keys._read_cfg", return_value=[(self.value, "other")]):
            self.assertIsNone(self.resolver.resolve(self.path))

    def test_source_outside_account_and_non_v2_files_do_not_start_process_reads(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "sample.dat"
            outside.write_bytes(self.path.read_bytes())
            with patch("media_keys._selection") as selection, patch("media_keys._read_cfg") as read:
                self.assertIsNone(self.resolver.resolve(outside))
                self.path.write_bytes(b"non-encrypted")
                self.assertIsNone(self.resolver.resolve(self.path))
                selection.assert_not_called()
                read.assert_not_called()

    def test_ownership_change_during_read_discards_the_candidate(self):
        changed = SimpleNamespace(account_dir=self.root, processes=((789, 999.0),))
        with patch("media_keys._selection", side_effect=[self.selected, changed]), patch(
                "media_keys._read_cfg", return_value=[(self.value, self.db.wxid)]):
            self.assertIsNone(self.resolver.resolve(self.path))
            self.assertIsNone(self.resolver.keys)

    def test_wrong_probe_rejects_key_and_a_miss_is_not_repeated_on_every_thumbnail(self):
        self.path.write_bytes(V2_MAGIC + bytes(25))
        with patch("media_keys._selection", return_value=self.selected), patch(
                "media_keys._read_cfg", return_value=[(self.value, self.db.wxid)]) as read:
            self.assertIsNone(self.resolver.resolve(self.path))
            self.assertIsNone(self.resolver.resolve(self.path))
            self.assertEqual(read.call_count, 1)
        self.assertFalse(validates(bytes(16), self.keys[0]))

    def test_thumbnails_with_different_headers_do_not_each_reread_wechat(self):
        with patch("media_keys._selection", return_value=self.selected), patch(
                "media_keys._read_cfg", return_value=[]) as read:
            for index in range(4):
                path = self.root / f"thumb-{index}.dat"
                path.write_bytes(V2_MAGIC + bytes(9) + bytes([index + 1]) * 16)
                self.assertIsNone(self.resolver.resolve(path))
            self.assertEqual(read.call_count, 1)

    def test_new_owned_process_can_retry_after_an_earlier_miss(self):
        with patch("media_keys._selection", return_value=self.selected), patch("media_keys._read_cfg", return_value=[]):
            self.assertIsNone(self.resolver.resolve(self.path))
        changed = SimpleNamespace(account_dir=self.root, processes=((789, 999.0),))
        with patch("media_keys._selection", return_value=changed), patch(
                "media_keys._read_cfg", return_value=[(self.value, self.db.wxid)]):
            self.assertEqual(self.resolver.resolve(self.path), self.keys)

    def test_cfg_reader_accepts_known_pointer_layouts_and_bounds_account_strings(self):
        for offset in (0x138, 0x130):
            with self.subTest(offset=offset):
                base, position, owner, cfg = 0x100000, 400, 0x200000, 0x300000
                image = bytearray(512)
                image[position:position + 13] = b"global_config"
                struct.pack_into("<I", image, position + 16, 13)
                struct.pack_into("<I", image, position + 24, 15)
                account = b"synthetic-user"
                memory = {
                    (base + position + 16 - offset, 8): struct.pack("<Q", owner),
                    (owner + 0x68, 8): struct.pack("<Q", cfg),
                    (cfg + 0x40, 4): struct.pack("<I", self.value),
                    (cfg + 0x48 + 16, 8): struct.pack("<Q", len(account)),
                    (cfg + 0x48, len(account)): account,
                }
                read = lambda address, count: memory.get((address, count), b"")
                self.assertEqual(list(_cfg_candidates(bytes(image), base, read)), [(self.value, self.db.wxid)])
                memory[(cfg + 0x48 + 16, 8)] = struct.pack("<Q", 10_000_000)
                self.assertEqual(list(_cfg_candidates(bytes(image), base, read)), [])


class EmoticonKeyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = SimpleNamespace(account_dir=self.root, wxid="synthetic-user")
        self.selected = SimpleNamespace(account_dir=self.root, processes=((123, 456.0),))
        self.uin = 2**40 + 123456
        self.key = derive_emoticon_key(self.uin, self.db.wxid)
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key)).encryptor()
        self.path = self.root / "synthetic-emoticon"
        self.path.write_bytes(encryptor.update(b"GIF89a" + bytes(10)) + encryptor.finalize())
        self.resolver = EmoticonKeyResolver(self.db)
        stable = tempfile.TemporaryDirectory()
        self.addCleanup(stable.cleanup)
        self.stable = Path(stable.name)
        environment = patch.dict("os.environ", {"WECHATAUTO_KEYS_DIR": str(self.stable)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_full_uin_and_cbc_key_are_reused_only_after_validation(self):
        before = list(self.root.iterdir())
        original = self.path.read_bytes()
        with patch("media_keys._selection", return_value=self.selected), patch("media_keys._read_cfg", return_value=[
                (self.uin, "another-user"), (self.uin & 0xFFFFFFFF, self.db.wxid), (self.uin, self.db.wxid)]) as read:
            self.assertEqual(self.resolver.resolve(self.path), self.key)
            self.assertEqual(self.resolver.resolve(self.path), self.key)
            read.assert_called_once_with(123, 456.0, full_uin=True)
            self.path.write_bytes(bytes(16))
            self.assertIsNone(self.resolver.resolve(self.path))
            self.assertIsNone(self.resolver.keys)
            self.path.write_bytes(original)
            self.assertEqual(self.resolver.resolve(self.path), self.key)
        self.assertEqual(list(self.root.iterdir()), before)
        saved = json.loads((self.stable / (self.root.name + ".media.json")).read_text(encoding="utf-8"))
        self.assertEqual(saved, {"wxid": self.db.wxid, "emoticon": self.key.hex()})

    def test_unowned_and_outside_files_never_read_process_data(self):
        other = SimpleNamespace(account_dir=self.root / "other", processes=self.selected.processes)
        with patch("media_keys._selection", return_value=other), patch("media_keys._read_cfg") as read:
            self.assertIsNone(self.resolver.resolve(self.path))
            read.assert_not_called()
        with tempfile.TemporaryDirectory() as outside:
            path = Path(outside) / "synthetic"
            path.write_bytes(self.path.read_bytes())
            with patch("media_keys._selection") as select, patch("media_keys._read_cfg") as read:
                self.assertIsNone(self.resolver.resolve(path))
                select.assert_not_called()
                read.assert_not_called()

    def test_account_change_during_key_read_discards_result(self):
        changed = SimpleNamespace(account_dir=self.root, processes=((789, 999.0),))
        with patch("media_keys._selection", side_effect=[self.selected, changed]), patch(
                "media_keys._read_cfg", return_value=[(self.uin, self.db.wxid)]):
            self.assertIsNone(self.resolver.resolve(self.path))
            self.assertIsNone(self.resolver.keys)

    def test_bad_newer_cache_does_not_block_an_older_valid_original(self):
        broken = self.root / "newer-cache"
        broken.write_bytes(bytes(16))
        with patch("media_keys._selection", return_value=self.selected), patch(
                "media_keys._read_cfg", return_value=[(self.uin, self.db.wxid)]) as read:
            self.assertIsNone(self.resolver.resolve(broken))
            self.assertIsNone(self.resolver.resolve(broken))
            self.assertEqual(read.call_count, 1)
            # The valid original is checked against the same read, without rereading WeChat.
            self.assertEqual(self.resolver.resolve(self.path), self.key)
            self.assertEqual(read.call_count, 1)

    def test_distinct_unreadable_stickers_share_one_process_read_per_window(self):
        files = []
        for index in range(4):
            files.append(self.root / f"other-{index}")
            files[-1].write_bytes(bytes([index + 1]) * 16)
        with patch("media_keys._selection", return_value=self.selected), patch(
                "media_keys._read_cfg", return_value=[(self.uin & 0xFFFFFFFF, self.db.wxid)]) as read:
            for path in files:
                self.assertIsNone(self.resolver.resolve(path))
            self.assertEqual(read.call_count, 1)
            self.resolver.retry_after = 0  # The bounded window has elapsed.
            self.assertIsNone(self.resolver.resolve(files[0]))
            self.assertEqual(read.call_count, 2)

    def test_cfg_reader_preserves_high_uin_bits_without_changing_image_derivation(self):
        base, position, owner, cfg = 0x100000, 400, 0x200000, 0x300000
        image = bytearray(512)
        image[position:position + 13] = b"global_config"
        struct.pack_into("<I", image, position + 16, 13)
        struct.pack_into("<I", image, position + 24, 15)
        account = self.db.wxid.encode()
        memory = {
            (base + position + 16 - 0x138, 8): struct.pack("<Q", owner),
            (owner + 0x68, 8): struct.pack("<Q", cfg),
            (cfg + 0x40, 4): struct.pack("<I", self.uin & 0xFFFFFFFF),
            (cfg + 0x40, 8): struct.pack("<Q", self.uin),
            (cfg + 0x48 + 16, 8): struct.pack("<Q", len(account)),
            (cfg + 0x48, len(account)): account,
        }
        read = lambda address, count: memory.get((address, count), b"")
        self.assertEqual(list(_cfg_candidates(bytes(image), base, read, full_uin=True)), [(self.uin, self.db.wxid)])
        self.assertEqual(list(_cfg_candidates(bytes(image), base, read)), [(self.uin & 0xFFFFFFFF, self.db.wxid)])


if __name__ == "__main__":
    unittest.main()
