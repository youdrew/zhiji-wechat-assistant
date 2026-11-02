"""Synthetic emoji-cache fixtures; never reads live chats or downloads media.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
"""
import base64
import hashlib
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from backend_contracts import AccountChangedError
from chat_server import classify
from media_assets import LocalMedia, MediaUnavailable, analysis_frames, analysis_image, media_metadata, sticker_identity
from wechat_source import WeChatSource, message_id


class StickerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.account = self.root / "account"
        self.account.mkdir()
        output = io.BytesIO()
        Image.new("RGB", (40, 60), "red").save(output, "GIF", save_all=True,
            append_images=[Image.new("RGB", (40, 60), "blue")], duration=100, loop=0)
        self.data = output.getvalue()
        self.digest = hashlib.md5(self.data).hexdigest()
        self.xml = (f'<msg><emoji md5="{self.digest}" width="40" height="60" '
                    'cdnurl="https://untrusted.invalid/not-fetched" aeskey="not-exported"/></msg>')
        self.row = {"local_type": 47, "decoded_content": self.xml}
        self.db = SimpleNamespace(account="synthetic-account", account_dir=str(self.account))
        self.engine = LocalMedia(self.db, None, self.root / "owned")
        # Source/cache fallback cases exercise the explicitly selected reference mode.
        self.engine.settings({"mode": "reference"})
        self.stable_id = "a" * 64

    def install(self, cache=False, data=None):
        base = self.account / "cache" / "2026-10" / "Emoticon" if cache else (
            self.account / "business" / "emoticon" / "Persist")
        path = base / self.digest[:2] / self.digest
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.data if data is None else data)
        return path

    def encrypt_local(self, data):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        key = bytes(range(16))  # Synthetic, never an account credential.
        self.engine._emoticon_keys = SimpleNamespace(resolve=lambda _path: key)
        padder = PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        encryptor = Cipher(algorithms.AES(key), modes.CBC(key)).encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    def install_store(self, offset=37, *, data=None, size=None):
        product = "synthetic-store-product"
        self.row["decoded_content"] = f'<msg><emoji md5="{self.digest}" productid="{product}"/></msg>'
        index = self.account / "db_storage" / "emoticon" / "emoticon.db"
        index.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(index)) as conn, conn:
            conn.execute("CREATE TABLE kStoreEmoticonFilesTable "
                         "(package_id_ TEXT,md5_ TEXT,emoticon_offset_ INT,emoticon_size_ INT)")
            conn.execute("INSERT INTO kStoreEmoticonFilesTable VALUES (?,?,?,?)",
                         (product, self.digest, offset, len(self.data) if size is None else size))
        self.db._db_files = [("emoticon/emoticon.db", str(index), index.stat().st_size)]
        self.db._open = lambda _rel: sqlite3.connect(index)
        package_id = hashlib.md5(product.encode()).hexdigest()
        package = self.account / "business" / "emoticon" / "PersistStore" / package_id[:2] / package_id
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_bytes(self.encrypt_local(b"x" * max(offset, 0) + (self.data if data is None else data)))
        return package, index

    def test_local_cbc_without_xml_key_preserves_original_animation(self):
        self.row["decoded_content"] = f'<msg><emoji md5="{self.digest}"/></msg>'
        path = self.install(cache=True, data=self.encrypt_local(self.data))
        before = path.read_bytes()
        with patch("sticker_download.download_sticker", side_effect=AssertionError("No network")):
            asset = self.engine.resolve("friend", self.stable_id, self.row)
        self.assertEqual(asset.read(), self.data)
        with Image.open(io.BytesIO(asset.read())) as image:
            self.assertEqual(image.n_frames, 2)
        self.assertEqual(path.read_bytes(), before)

    def test_local_cbc_wrong_thumbnail_falls_back_to_matching_original(self):
        original = self.install(cache=True, data=self.encrypt_local(self.data))
        older = Path(str(original).replace("2026-10", "2026-09"))
        older.parent.mkdir(parents=True, exist_ok=True)
        original.rename(older)
        output = io.BytesIO()
        Image.new("RGB", (10, 10), "green").save(output, "PNG")
        self.install(cache=True, data=self.encrypt_local(output.getvalue()))
        self.assertEqual(self.engine.resolve("friend", self.stable_id, self.row).read(), self.data)

    def test_local_cbc_rejects_wrong_key_hash_and_truncated_ciphertext(self):
        encrypted = self.encrypt_local(self.data)
        for data, key in ((encrypted, b"z" * 16), (encrypted[:-1], bytes(range(16))),
                          (self.encrypt_local(b"not-the-original-image"), bytes(range(16)))):
            with self.subTest(size=len(data)):
                self.install(cache=True, data=data)
                self.engine._emoticon_keys = SimpleNamespace(resolve=lambda _path: key)
                with self.assertRaises(MediaUnavailable):
                    self.engine.resolve("friend", self.stable_id, self.row)

    def test_store_cbc_exact_range_crosses_blocks_without_original_cache(self):
        package, _ = self.install_store()
        before = package.read_bytes()
        with patch("sticker_download.download_sticker", side_effect=AssertionError("No network")):
            asset = self.engine.resolve("friend", self.stable_id, self.row)
        self.assertEqual(asset.read(), self.data)
        self.assertEqual(package.read_bytes(), before)

    def test_store_cbc_first_block_and_aligned_offsets(self):
        package, index = self.install_store(offset=0)
        for offset in (0, 16, 31):
            with self.subTest(offset=offset):
                with closing(sqlite3.connect(index)) as conn, conn:
                    conn.execute("UPDATE kStoreEmoticonFilesTable SET emoticon_offset_=?", (offset,))
                package.write_bytes(self.encrypt_local(b"x" * offset + self.data))
                self.assertEqual(self.engine.resolve("friend", self.stable_id, self.row).read(), self.data)

    def test_bad_local_cache_falls_back_to_store_and_reads_only_target_blocks(self):
        package, _ = self.install_store(offset=37)
        # A downloaded pack may contain many more images after this one.
        with package.open("ab") as stream:
            stream.write(b"x" * (1024 * 1024))
        self.install(cache=True, data=self.encrypt_local(b"wrong-thumbnail"))
        open_path = Path.open
        reads = []

        class TrackedFile:
            def __init__(self, file):
                self.file = file
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                self.file.close()
            def __getattr__(self, name):
                return getattr(self.file, name)
            def read(self, size=-1):
                reads.append((self.file.tell(), size))
                return self.file.read(size)

        def tracked_open(path, *args, **kwargs):
            file = open_path(path, *args, **kwargs)
            return TrackedFile(file) if path == package else file

        with patch.object(Path, "open", tracked_open):
            asset = self.engine.resolve("friend", self.stable_id, self.row)
        self.assertEqual(asset.read(), self.data)
        end = (37 + len(self.data) + 15) // 16 * 16
        self.assertEqual(reads, [(16, 16), (32, end - 32)])

    def test_store_ambiguous_index_is_rejected(self):
        _, index = self.install_store()
        with closing(sqlite3.connect(index)) as conn, conn:
            conn.execute("INSERT INTO kStoreEmoticonFilesTable SELECT * FROM kStoreEmoticonFilesTable")
        with self.assertRaises(MediaUnavailable):
            self.engine.resolve("friend", self.stable_id, self.row)

    def test_unavailable_store_snapshot_does_not_block_existing_cdn_fallback(self):
        self.install_store()
        self.row["decoded_content"] = self.row["decoded_content"].replace(
            "/>", ' cdnurl="https://mmemoticon.qpic.cn/mmemoticon/synthetic"/>')
        with patch.object(self.db, "_open", side_effect=RuntimeError("snapshot unavailable")), patch(
                "sticker_download.download_sticker", return_value=self.data) as fetch:
            self.assertEqual(self.engine.resolve("friend", self.stable_id, self.row).read(), self.data)
            fetch.assert_called_once()

    def test_store_rejects_truncated_wrong_package_and_wrong_index(self):
        package, index = self.install_store()
        original = package.read_bytes()
        cases = [(original[:-1], 37, len(self.data)), (original[:-16], 37, len(self.data)),
                 (self.encrypt_local(b"x" * (37 + len(self.data))), 37, len(self.data)),
                 (original, 36, len(self.data)), (original, -1, len(self.data)),
                 (original, 37, 8 * 1024 * 1024 + 1), (original, 37, 0)]
        for data, offset, size in cases:
            with self.subTest(offset=offset, size=size, package_size=len(data)):
                package.write_bytes(data)
                with closing(sqlite3.connect(index)) as conn, conn:
                    conn.execute("UPDATE kStoreEmoticonFilesTable SET emoticon_offset_=?,emoticon_size_=?", (offset, size))
                with self.assertRaises(MediaUnavailable):
                    self.engine.resolve("friend", self.stable_id, self.row)

    def test_store_requires_exact_package_and_rejects_reparse_path(self):
        _, index = self.install_store()
        with patch("media_assets._reparse", side_effect=lambda path: path.name == "PersistStore"):
            with self.assertRaises(MediaUnavailable):
                self.engine.resolve("friend", self.stable_id, self.row)
        with closing(sqlite3.connect(index)) as conn, conn:
            conn.execute("UPDATE kStoreEmoticonFilesTable SET package_id_='another-product'")
        with self.assertRaises(MediaUnavailable):
            self.engine.resolve("friend", self.stable_id, self.row)

    def test_metadata_excludes_paths_urls_keys_and_leaves_text_non_analyzable(self):
        for kind in (47, 11000, 47 + (1 << 32)):
            self.assertEqual(media_metadata(kind, self.xml), {"kind": "sticker", "width": 40, "height": 60})
        self.assertEqual(classify("动画表情", self.xml), ("other", "[表情]"))

    def test_visual_analysis_normalizes_real_gif_pixels_and_marks_only_first_frame(self):
        self.install()
        result = self.engine.analysis("friend", self.stable_id, self.row, media_metadata(47, self.xml))
        self.assertEqual(result["kind"], "sticker")
        self.assertEqual(result["sampling"], "animated-image-first-frame")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["mime"], "image/jpeg")
        with Image.open(io.BytesIO(result["items"][0]["bytes"])) as image:
            red, green, blue = image.convert("RGB").getpixel((20, 30))
            self.assertGreater(red, 220)
            self.assertLess(green, 30)
            self.assertLess(blue, 30)

    def test_transparent_sticker_keeps_dark_lines_visible_in_model_jpeg(self):
        original = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        for x in range(15, 25):
            for y in range(15, 25):
                original.putpixel((x, y), (0, 0, 0, 255))
        output = io.BytesIO()
        original.save(output, "PNG")
        normalized, animated = analysis_image(output.getvalue())
        self.assertFalse(animated)
        with Image.open(io.BytesIO(normalized)) as image:
            self.assertGreater(min(image.getpixel((2, 2))), 240)
            self.assertLess(max(image.getpixel((20, 20))), 20)

    def test_persistent_and_monthly_cache_preserve_animation_in_both_variants(self):
        for cache in (False, True):
            with self.subTest(cache=cache):
                original = self.install(cache)
                for variant in ("original", "thumbnail"):
                    asset = self.engine.resolve("friend", self.stable_id, self.row, variant)
                    self.assertEqual(asset.mime, "image/gif")
                    self.assertEqual(asset.read(), self.data)
                    with Image.open(io.BytesIO(asset.read())) as image:
                        self.assertEqual(image.n_frames, 2)
                self.assertEqual(original.read_bytes(), self.data)
                original.unlink()
        self.assertEqual(self.engine.settings()["archiveBytes"], 0)

    def test_transparent_png_and_webp_are_not_converted_to_jpeg(self):
        for kind, mime in (("PNG", "image/png"), ("WEBP", "image/webp")):
            with self.subTest(kind=kind):
                output = io.BytesIO()
                Image.new("RGBA", (20, 20), (0, 255, 0, 0)).save(output, kind)
                previous = self.digest
                self.digest = hashlib.md5(output.getvalue()).hexdigest()
                self.row["decoded_content"] = self.xml.replace(previous, self.digest)
                self.xml = self.row["decoded_content"]
                self.install(data=output.getvalue())
                asset = self.engine.resolve("friend", self.stable_id, self.row)
                self.assertEqual(asset.mime, mime)
                self.assertEqual(asset.read(), output.getvalue())

    def test_missing_invalid_and_path_like_hash_do_not_resolve_other_files(self):
        self.install()
        for xml in ('<msg><emoji md5="../outside"/></msg>', '<msg><emoji/></msg>',
                    '<!DOCTYPE msg [<!ENTITY x "secret">]><msg><emoji md5="&x;"/></msg>',
                    '<msg><emoji', self.xml.replace(self.digest, "b" * 32)):
            with self.subTest(xml=xml[:20]), self.assertRaises(MediaUnavailable):
                self.engine.resolve("friend", self.stable_id, {**self.row, "decoded_content": xml})

    def test_corrupt_persist_falls_back_to_valid_cache_and_no_remote_download(self):
        self.install(data=b"GIF89a-corrupt")
        self.install(cache=True)
        with patch("urllib.request.urlopen", side_effect=AssertionError("No remote fetching")):
            self.assertEqual(self.engine.resolve("friend", self.stable_id, self.row).read(), self.data)
        for path in self.account.rglob(self.digest):
            path.unlink()
        with patch("urllib.request.urlopen", side_effect=AssertionError("No remote fetching")):
            with self.assertRaisesRegex(MediaUnavailable, "sticker-unavailable"):
                self.engine.resolve("friend", self.stable_id, self.row)

    def test_cdn_bytes_are_verified_cached_once_and_clearable(self):
        # Production downloader performs its own host/IP checks; no live network in tests.
        with patch("sticker_download.download_sticker", return_value=self.data) as fetch:
            for _ in range(2):
                self.assertEqual(self.engine.resolve("friend", self.stable_id, self.row).read(), self.data)
            self.assertEqual(fetch.call_count, 1)
            self.assertGreater(self.engine.settings()["cacheBytes"], 0)
            self.assertEqual(self.engine.settings()["archiveBytes"], 0)
            self.engine.clear_cache()
            self.engine.resolve("friend", self.stable_id, self.row)
            self.assertEqual(fetch.call_count, 2)
        paths = list(self.engine.root.rglob("*.sticker"))
        self.assertEqual(len(paths), 1)
        self.assertEqual(paths[0].read_bytes(), self.data)

    def test_wrong_cdn_hash_is_not_cached_and_failure_retries_are_cooled_down(self):
        output = io.BytesIO()
        Image.new("RGB", (20, 20), "green").save(output, "PNG")
        with patch("sticker_download.download_sticker", return_value=output.getvalue()) as fetch:
            for _ in range(2):
                with self.assertRaises(MediaUnavailable):
                    self.engine.resolve("friend", self.stable_id, self.row)
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.engine.settings()["cacheBytes"], 0)
        self.engine.clear_cache()
        with patch("sticker_download.download_sticker", return_value=self.data) as fetch:
            self.assertEqual(self.engine.resolve("friend", self.stable_id, self.row).read(), self.data)
            self.assertEqual(fetch.call_count, 1)

    def test_supported_aes_cdn_payload_preserves_all_frames(self):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.padding import PKCS7
        key = bytes(range(16))  # Synthetic key, never an account credential.
        padder = PKCS7(128).padder()
        encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
        padded = padder.update(self.data) + padder.finalize()
        encrypted = encryptor.update(padded) + encryptor.finalize()
        row = {**self.row, "decoded_content": self.xml.replace("not-exported", key.hex())}
        with patch("sticker_download.download_sticker", return_value=encrypted):
            self.assertEqual(self.engine.resolve("friend", self.stable_id, row).read(), self.data)

    def test_oversized_cache_and_reparse_components_are_rejected(self):
        self.install()
        with patch("media_assets.MAX_IMAGE", 10):
            with self.assertRaises(MediaUnavailable):
                self.engine.resolve("friend", self.stable_id, self.row)
        # Model a Windows junction independent of privileges needed to create one.
        with patch("media_assets._reparse", side_effect=lambda p: p.name == "Persist"):
            with self.assertRaises(MediaUnavailable):
                self.engine.resolve("friend", self.stable_id, self.row)

    def test_archive_retains_animation_after_source_removal_and_cache_clear(self):
        path = self.install()
        self.engine.settings({"mode": "archive"})
        self.engine.resolve("friend", self.stable_id, self.row)
        path.unlink()
        self.engine.clear_cache()
        asset = self.engine.resolve("friend", self.stable_id, self.row)
        self.assertEqual(asset.read(), self.data)
        self.assertEqual(self.engine.settings()["archiveBytes"], len(self.data))

    @staticmethod
    def desc(*pairs):
        """A synthetic ``desc`` attribute: base64 of ``{1: {1: language, 2: text}}`` entries."""
        def field(number, payload):
            return bytes([number << 3 | 2, len(payload)]) + payload
        return base64.b64encode(b"".join(field(1, field(1, language.encode()) + field(2, text.encode()))
                                         for language, text in pairs)).decode()

    def test_identity_reads_md5_store_pack_and_preferred_caption(self):
        xml = (f'<msg><emoji md5="{self.digest.upper()}" productid="com.tencent.xin.emoticon.synthetic" '
               f'desc="{self.desc(("default", "Good night"), ("zh_cn", "晚安"))}"/></msg>')
        self.assertEqual(sticker_identity(xml), {"id": self.digest, "pack": "com.tencent.xin.emoticon.synthetic",
                                                 "caption": "晚安"})
        self.assertEqual(sticker_identity(self.xml), {"id": self.digest})
        # An unreadable caption or pack only drops that part; no MD5 means no identity.
        self.assertEqual(sticker_identity(f'<msg><emoji md5="{self.digest}" productid="a b" desc="bm90LXByb3Rv"/></msg>'),
                         {"id": self.digest})
        for content in ('<msg><emoji md5="../../x"/></msg>', "<!DOCTYPE x><msg/>", "<msg>", None, "x" * 200000):
            self.assertIsNone(sticker_identity(content))

    def test_animated_frames_follow_playing_time_and_skip_repeats(self):
        frames, duration = analysis_frames(self.data)
        self.assertEqual(duration, 200)
        self.assertEqual([time_ms for _data, time_ms in frames], [0, 100])  # Four slots, two distinct frames.
        colors = []
        for data, _time_ms in frames:
            with Image.open(io.BytesIO(data)) as image:
                colors.append(image.convert("RGB").getpixel((20, 30)))
        self.assertGreater(colors[0][0], 220)
        self.assertGreater(colors[1][2], 220)
        output = io.BytesIO()
        Image.new("RGB", (8, 8), "green").save(output, "PNG")
        still, duration = analysis_frames(output.getvalue())
        self.assertEqual((len(still), still[0][1], duration), (1, 0, 0))

    def test_frame_library_keeps_stills_after_the_source_and_cache_are_gone(self):
        path = self.install()
        metadata = media_metadata(47, self.xml)
        result = self.engine.analysis("friend", self.stable_id, self.row, metadata, frames=4)
        self.assertEqual(result["sampling"], "animated-frames")
        self.assertEqual(result["durationMs"], 200)
        self.assertEqual([item["timeMs"] for item in result["items"]], [0, 100])
        library = self.root / "owned" / "stickers"
        self.assertTrue((library / (self.digest + ".json")).is_file())
        path.unlink()
        self.engine.clear_cache()
        with patch("sticker_download.download_sticker", side_effect=AssertionError("No network")):
            again = self.engine.analysis("friend", "b" * 64, self.row, metadata, frames=4)
        self.assertEqual(again["items"], result["items"])
        # A damaged index is ignored, never trusted.
        (library / (self.digest + ".json")).write_text('{"version": 1, "durationMs": 200, "frames": [0, 999]}')
        self.assertEqual(self.engine.analysis("friend", self.stable_id, self.row, metadata, frames=4)["error"],
                         "sticker-unavailable")
        self.assertEqual(len(self.engine.analysis("friend", self.stable_id, self.row, metadata)["items"]), 0)

    def test_store_captions_and_pack_names_come_from_the_emoticon_index(self):
        _package, index = self.install_store()
        product = "synthetic-store-product"
        identities = [{"id": self.digest, "pack": product}, {"id": "f" * 32}]
        self.assertEqual(self.engine.sticker_texts(identities), ({}, {}))  # Older index without the tables.
        with closing(sqlite3.connect(index)) as conn, conn:
            conn.execute("CREATE TABLE kStoreEmoticonCaptionsTable (package_id_ TEXT,md5_ TEXT,language_ TEXT,caption_ TEXT)")
            conn.executemany("INSERT INTO kStoreEmoticonCaptionsTable VALUES (?,?,?,?)",
                             [(product, self.digest.upper(), "default", "Good night"), (product, self.digest, "zh_cn", "晚安")])
            conn.execute("CREATE TABLE kStoreEmoticonPackageTable (package_id_ TEXT,package_name_ TEXT)")
            conn.execute("INSERT INTO kStoreEmoticonPackageTable VALUES (?,?)", (product, " 合成\n表情包 "))
        self.assertEqual(self.engine.sticker_texts(identities), ({self.digest: "晚安"}, {product: "合成 表情包"}))

    def test_source_identifies_only_requested_sticker_messages(self):
        user = "synthetic-friend"
        table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
        shard = self.root / "message__message_0.db"
        xml = f'<msg><emoji md5="{self.digest}" productid="synthetic-pack" desc="{self.desc(("zh_cn", "晚安"))}"/></msg>'
        with closing(sqlite3.connect(shard)) as connection, connection:
            connection.execute(f"CREATE TABLE {table} (local_id INT,local_type INT,real_sender_id INT,create_time INT,"
                               "message_content TEXT,compress_content BLOB,server_id INT,sort_seq INT)")
            connection.execute(f"INSERT INTO {table} VALUES (1,47,1,1000,?,NULL,101,1)", (xml,))
            connection.execute(f"INSERT INTO {table} VALUES (2,1,1,1001,'你好',NULL,102,2)")
        rel = os.path.join("message", "message_0.db")
        self.db._db_files = [(rel, str(shard), shard.stat().st_size)]
        self.db._open = lambda _rel: sqlite3.connect(shard)
        self.db._msg_type_name = lambda kind: "动画表情" if kind == 47 else "文本"
        self.db._friendly_content = lambda data, _kind: data
        source = WeChatSource(lambda: self.db, classify, lambda _db: None, media_storage_root=self.root / "source-owned")
        source._db = lambda **_kwargs: self.db
        source.require_messages_ready = lambda: None
        name = "message__message_0.db"
        sticker = message_id(self.db.account, user, name, {"local_id": 1, "sort_seq": 1, "server_id": 101})
        text = message_id(self.db.account, user, name, {"local_id": 2, "sort_seq": 2, "server_id": 102})
        found = source.sticker_details(user, [(name, 1, sticker), (name, 2, text), (name, 1, "f" * 64)])
        self.assertEqual(found, {sticker: {"id": self.digest, "pack": "synthetic-pack", "caption": "晚安"}})
        self.assertEqual(source.sticker_details("different-friend", [(name, 1, sticker)]), {})

    def test_source_issues_scoped_sticker_from_compressed_xml(self):
        import zstandard
        path = self.install()
        user = "synthetic-friend"
        table = "Msg_" + hashlib.md5(user.encode()).hexdigest()
        shard = self.root / "message__message_0.db"
        compressed = zstandard.ZstdCompressor().compress(self.xml.encode())
        with closing(sqlite3.connect(shard)) as connection, connection:
            connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
            connection.execute("INSERT INTO Name2Id VALUES (?)", (user,))
            connection.execute(f"CREATE TABLE {table} (local_id INT,local_type INT,real_sender_id INT,create_time INT,"
                               "message_content BLOB,compress_content BLOB,server_id INT,sort_seq INT,packed_info_data BLOB)")
            connection.execute(f"INSERT INTO {table} VALUES (1,47,1,1000,NULL,?,101,1,NULL)", (compressed,))
        self.db._msg_conns = lambda _user: [(sqlite3.connect(shard), table)]
        self.db._msg_type_name = lambda _kind: "动画表情"
        self.db._friendly_content = lambda data, _kind: zstandard.ZstdDecompressor().decompress(data).decode()
        self.db.get_self_info = lambda: {"username": "synthetic-self"}
        source = WeChatSource(lambda: self.db, classify, lambda _db: None, media_storage_root=self.root / "source-owned")
        source._db = lambda **_kwargs: self.db
        source._contacts = lambda _db: {}
        source.require_messages_ready = lambda: None
        message, = source.messages(user, 10)
        self.assertEqual(message["kind"], "other")
        self.assertEqual(message["media"], {"kind": "sticker", "width": 40, "height": 60})
        self.assertNotIn("untrusted", str(message))
        asset = source.media_asset(user, message["id"], "original", self.db.account)
        self.assertIsNotNone(asset, source.media_reason.value)
        self.assertEqual(asset.read(), self.data)
        self.assertIsNone(source.media_asset("different-friend", message["id"]))
        self.assertIsNone(source.media_asset(user, "f" * 64))
        with self.assertRaises(AccountChangedError):
            source.media_asset(user, message["id"], expected_account="different-account")
        source.media_archive(self.db.account, user, message["id"])
        path.unlink()
        self.assertEqual(source.media_asset(user, message["id"]).read(), self.data)


if __name__ == "__main__":
    unittest.main()
