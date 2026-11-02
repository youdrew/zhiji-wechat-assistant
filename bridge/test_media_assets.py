"""Synthetic local media only: no live accounts, network downloads or model calls.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
"""
import hashlib
import http.client
import io
import json
import math
import sqlite3
import struct
import subprocess
import tempfile
import threading
import unittest
import wave
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from http.server import ThreadingHTTPServer

from backend_contracts import AccountChangedError
from chat_server import classify
from media_assets import LocalMedia, MediaAsset, MediaUnavailable, media_metadata, analysis_image
from real_http import make_handler
from wechat_source import WeChatSource


class MetadataTests(unittest.TestCase):
    def test_media_fields_and_only_completed_wechat_transcripts(self):
        voice = '<msg><voicemsg voicelength="1800"/><voicetrans transtext="合成 &amp; 测试" istransend="1"/></msg>'
        self.assertEqual(media_metadata(34, voice), {"kind": "voice", "durationMs": 1800,
            "transcript": "合成 & 测试", "transcriptSource": "wechat"})
        self.assertNotIn("transcript", media_metadata(34, voice.replace('istransend="1"', 'istransend="0"')))
        self.assertEqual(media_metadata(43, '<msg><videomsg width="640" height="480" playlength="4"/></msg>'),
                         {"kind": "video", "width": 640, "height": 480, "durationMs": 4000})
        self.assertEqual(media_metadata(3, '<msg><img width="-1" height="999999999"/></msg>'), {"kind": "image"})
        self.assertEqual(media_metadata(34, '<!DOCTYPE msg [<!ENTITY a "x">]><msg>&a;</msg>'), {"kind": "voice"})
        self.assertIsNone(media_metadata(1, "plain text"))


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.user = "synthetic-friend"
        self.account = self.root / "account"
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.attach = self.account / "msg" / "attach" / hashlib.md5(self.user.encode()).hexdigest()
        self.attach.mkdir(parents=True)
        self.image_md5, self.video_md5 = "a" * 32, "b" * 32
        self.image_path = self.attach / (self.image_md5 + ".dat")
        self.image_path.write_bytes(b"synthetic-encrypted")
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (640, 480), "blue").save(output, "PNG")
        self.image_bytes = output.getvalue()
        voice_xml = '<msg><voicemsg voicelength="1000"/><voicetrans transtext="合成语音转写" istransend="1"/></msg>'
        self.table = "Msg_" + hashlib.md5(self.user.encode()).hexdigest()
        self.shards = [self.workdir / f"message__message_{i}.db" for i in range(2)]
        for i, path in enumerate(self.shards):
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
                conn.execute("INSERT INTO Name2Id VALUES (?)", (self.user,))
                conn.execute(f"CREATE TABLE {self.table} (local_id INT,local_type INT,real_sender_id INT,create_time INT,"
                    "message_content BLOB,compress_content BLOB,server_id INT,sort_seq INT,packed_info_data BLOB)")
                if i == 0:
                    conn.executemany(f"INSERT INTO {self.table} VALUES (?,?,1,1000,?,NULL,?,?,?)", [
                        (1, 3, b'<msg><img width="640" height="480"/></msg>', 11, 1, self.image_md5.encode()),
                        (2, 34, voice_xml.encode(), 12, 2, b""),
                        (3, 43, b'<msg><videomsg playlength="4"/></msg>', 13, 3, self.video_md5.encode()),
                    ])
                else:
                    # Same local ID and type in another shard must never select the latest wrong voice.
                    conn.execute(f"INSERT INTO {self.table} VALUES (2,34,1,1000,?,NULL,99,4,?)", (voice_xml.encode(), b""))
        self.voice_db = self.workdir / "media_0.db"
        with closing(sqlite3.connect(self.voice_db)) as conn, conn:
            conn.execute("CREATE TABLE Name2Id (user_name TEXT)")
            conn.execute("INSERT INTO Name2Id VALUES (?)", (self.user,))
            conn.execute("CREATE TABLE VoiceInfo (chat_name_id INT,svr_id INT,create_time INT,voice_data BLOB)")
        owner = self
        class DB:
            account = "synthetic-account"
            wxid = "synthetic-self"
            account_dir = str(owner.account)
            workdir = str(owner.workdir)
            _db_files = [("media_0.db", str(owner.voice_db), 0)]
            def _open(self, rel):
                return sqlite3.connect(owner.voice_db)
            def _msg_conns(self, _user):
                return [(sqlite3.connect(path), owner.table) for path in owner.shards]
            def get_self_info(self):
                return {"username": self.wxid}
            def _msg_type_name(self, value):
                return {3: "图片", 34: "语音", 43: "视频"}[value]
            def _friendly_content(self, value, _kind):
                return value.decode()
        class Downloader:
            def __init__(self, _db):
                pass
            def _img_md5(self, row):
                return row["packed_info"].decode()
            def _find_dat(self, _user, md5, _time, thumbnail=False):
                return str(owner.image_path) if owner.image_path.exists() else None
            def decrypt_image(self, path, **_kwargs):
                return owner.image_bytes
        self.db = DB()
        self.source = WeChatSource(lambda: self.db, classify, Downloader,
                                  media_storage_root=self.root / "stable-media")
        self.source._db = lambda **_kwargs: self.db
        self.source._contacts = lambda _db: {}
        self.source.require_messages_ready = lambda: None
        self.messages = self.source.messages(self.user, 10)
        self.by_type = {message["type"]: message for message in self.messages[:3]}
        self.engine = self.source._local_media(self.db)

    def install_voice(self):
        import pysilk
        pcm = b"".join(struct.pack("<h", int(2000 * math.sin(i * 2 * math.pi * 440 / 24000))) for i in range(24000))
        encoded = io.BytesIO()
        pysilk.encode(io.BytesIO(pcm), encoded, 24000, 24000, tencent=True)
        with closing(sqlite3.connect(self.voice_db)) as conn, conn:
            conn.execute("INSERT INTO VoiceInfo VALUES (1,12,1000,?)", (encoded.getvalue(),))
            conn.execute("INSERT INTO VoiceInfo VALUES (1,99,1000,?)", (b"wrong-other-shard",))
        return encoded.getvalue()

    def install_video(self):
        target = self.account / "msg" / "video" / "2026-10" / (self.video_md5 + ".mp4")
        target.parent.mkdir(parents=True)
        import os
        completed = subprocess.run([self.engine._ffmpeg(), "-nostdin", "-v", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=160x120:d=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)],
            capture_output=True, timeout=30, creationflags=0x08000000 if os.name == "nt" else 0)
        self.assertEqual(completed.returncode, 0)
        return target

    def test_reference_image_and_safely_scoped_identity(self):
        self.source.media_settings(self.db.account, {"mode": "reference"})
        message = self.by_type["图片"]
        self.assertEqual(message["media"], {"kind": "image", "width": 640, "height": 480})
        before = self.image_path.read_bytes()
        asset = self.source.media_asset(self.user, message["id"], expected_account=self.db.account)
        self.assertEqual(asset.read(), self.image_bytes)
        self.assertEqual(self.image_path.read_bytes(), before)
        self.assertEqual(self.engine.settings()["archiveBytes"], 0)
        self.assertEqual(self.engine.settings()["mode"], "reference")
        self.assertIsNone(self.source.media_asset("wrong-user", message["id"]))
        self.assertEqual(self.source.media_reason.value, "not-issued")
        with self.assertRaises(AccountChangedError):
            self.source.media_asset(self.user, message["id"], expected_account="other-account")
        thumbnail = self.source.media_asset(self.user, message["id"], "thumbnail")
        from PIL import Image
        with Image.open(io.BytesIO(thumbnail.read())) as image:
            self.assertEqual(image.size, (480, 360))

    def test_image_md5_is_recovered_from_only_the_verified_compressed_xml(self):
        import zstandard
        raw = f'<msg><img md5="{self.image_md5}" width="640" height="480"/></msg>'.encode()
        compressed = zstandard.ZstdCompressor().compress(raw)
        decode = self.db._friendly_content
        self.db._friendly_content = lambda data, kind: (zstandard.ZstdDecompressor().decompress(data).decode()
            if data.startswith(b"\x28\xb5\x2f\xfd") else decode(data, kind))
        with closing(sqlite3.connect(self.shards[0])) as conn, conn:
            conn.execute(f"UPDATE {self.table} SET message_content=?,compress_content=?,packed_info_data=? WHERE local_id=1",
                         (b"", compressed, b""))
        asset = self.source.media_asset(self.user, self.by_type["图片"]["id"])
        self.assertIsNotNone(asset, self.source.media_reason.value)
        self.assertEqual(asset.read(), self.image_bytes)

    def test_packed_local_image_identifier_is_preferred_to_different_xml_content_hash(self):
        message = self.by_type["图片"]
        with closing(sqlite3.connect(self.shards[0])) as conn, conn:
            conn.execute(f"UPDATE {self.table} SET message_content=? WHERE local_id=1",
                         (b'<msg><img md5="cccccccccccccccccccccccccccccccc"/></msg>',))
        looked_up = []
        find = self.engine.downloader._find_dat
        def lookup(user, identifier, created, thumbnail=False):
            looked_up.append(identifier)
            return find(user, identifier, created, thumbnail)
        with patch.object(self.engine.downloader, "_find_dat", side_effect=lookup):
            asset = self.source.media_asset(self.user, message["id"])
        self.assertIsNotNone(asset, self.source.media_reason.value)
        self.assertEqual(asset.read(), self.image_bytes)
        self.assertEqual(looked_up, [self.image_md5])

    def test_xml_image_identifier_fallback_when_packed_file_is_missing(self):
        message = self.by_type["图片"]
        absent = "c" * 32
        with closing(sqlite3.connect(self.shards[0])) as conn, conn:
            conn.execute(f"UPDATE {self.table} SET message_content=?,packed_info_data=? WHERE local_id=1",
                         (f'<msg><img md5="{self.image_md5}"/></msg>'.encode(), absent.encode()))
        looked_up = []
        find = self.engine.downloader._find_dat
        def lookup(user, identifier, created, thumbnail=False):
            looked_up.append(identifier)
            return None if identifier == absent else find(user, identifier, created, thumbnail)
        with patch.object(self.engine.downloader, "_find_dat", side_effect=lookup):
            asset = self.source.media_asset(self.user, message["id"])
        self.assertIsNotNone(asset, self.source.media_reason.value)
        self.assertEqual(asset.read(), self.image_bytes)
        self.assertEqual(looked_up[0], absent)
        self.assertEqual(looked_up[-1], self.image_md5)

    def test_voice_real_silk_decode_and_existing_transcript(self):
        self.source.media_settings(self.db.account, {"mode": "reference"})
        self.install_voice()
        message = self.by_type["语音"]
        self.assertEqual(message["media"]["transcriptSource"], "wechat")
        asset = self.source.media_asset(self.user, message["id"], "playback")
        self.assertIsNotNone(asset, self.source.media_reason.value)
        with wave.open(io.BytesIO(asset.read()), "rb") as wav:
            self.assertEqual((wav.getnchannels(), wav.getsampwidth(), wav.getframerate()), (1, 2, 24000))
            self.assertGreater(wav.getnframes(), 22000)
        self.assertGreater(self.engine.settings()["cacheBytes"], 0)
        self.assertEqual(self.engine.settings()["archiveBytes"], 0)
        analyzed = self.source.media_analysis(self.user, message["id"])
        self.assertEqual(analyzed["items"], [])
        self.assertEqual(analyzed["transcript"], "合成语音转写")
        # The other shard deliberately holds invalid bytes. Identity does not fall through to it.
        self.assertIsNone(self.source.media_asset(self.user, self.messages[-1]["id"], "playback"))
        self.assertEqual(self.source.media_reason.value, "voice-decode-failed")

    def test_voice_without_transcript_is_not_invented(self):
        self.assertEqual(self.engine.analysis(self.user, "a" * 64, {"local_type": 34}, {"kind": "voice"}),
                         {"kind": "voice", "items": [], "error": "wechat-transcript-unavailable"})

    def test_video_references_source_and_analysis_contains_bounded_timed_frames(self):
        self.source.media_settings(self.db.account, {"mode": "reference"})
        original = self.install_video()
        message = self.by_type["视频"]
        asset = self.source.media_asset(self.user, message["id"], "playback")
        self.assertEqual(asset.path, original)
        self.assertIsNone(asset.data)
        result = self.source.media_analysis(self.user, message["id"])
        self.assertNotIn("error", result)
        self.assertEqual([item["timeMs"] for item in result["items"]], [0, 1333, 2666])
        self.assertTrue(all(item["mime"] == "image/jpeg" and len(item["bytes"]) < 8 * 1024 * 1024 for item in result["items"]))
        self.assertEqual(self.engine.settings()["archiveBytes"], 0)
        stopped = threading.Event()
        stopped.set()
        self.assertEqual(self.source.media_analysis(self.user, message["id"], stopped)["error"], "cancelled")

    def test_video_xml_hash_finds_renamed_local_video_and_rejects_wrong_bytes(self):
        original = self.install_video()
        content = original.read_bytes()
        digest = hashlib.md5(content).hexdigest()
        renamed = original.with_name("recording-2026.mp4")
        original.rename(renamed)
        row = {"content": f'<msg><videomsg md5="{digest}" length="{len(content)}"/></msg>',
               "packed_info": b"unrelated " + b"c" * 32}
        self.assertEqual(self.engine._video(row).path, renamed)
        renamed.write_bytes(content[:20] + b"x" * (len(content) - 20))
        with self.assertRaises(MediaUnavailable):
            self.engine._video(row)

    def test_analysis_normalizes_animation_to_provider_safe_first_frame(self):
        from PIL import Image
        output = io.BytesIO()
        Image.new("RGB", (1800, 1200), "red").save(output, "GIF", save_all=True,
            append_images=[Image.new("RGB", (1800, 1200), "blue")], duration=100, loop=0)
        self.image_bytes = output.getvalue()
        result = self.source.media_analysis(self.user, self.by_type["图片"]["id"])
        self.assertEqual(result["sampling"], "animated-image-first-frame")
        self.assertEqual(result["items"][0]["mime"], "image/jpeg")
        self.assertLessEqual(len(result["items"][0]["bytes"]), 2 * 1024 * 1024)
        with Image.open(io.BytesIO(result["items"][0]["bytes"])) as image:
            self.assertLessEqual(max(image.size), 1600)

    def test_owned_storage_survives_snapshot_workdir_change(self):
        message = self.by_type["图片"]
        self.source.media_archive(self.db.account, self.user, message["id"])
        previous = self.engine.root
        self.db.workdir = str(self.root / "replacement-snapshot")
        self.assertEqual(self.source._local_media(self.db).root, previous)
        self.assertGreater(self.source.media_settings(self.db.account)["archiveBytes"], 0)

    def test_account_clear_waits_for_inflight_media_and_cannot_be_recreated_by_late_write(self):
        started, release, cleared = threading.Event(), threading.Event(), threading.Event()
        errors = []
        def convert():
            try:
                with self.engine.operation():
                    started.set()
                    release.wait(5)
                    self.engine._cache("d" * 64, "jpg", b"synthetic", "image/jpeg")
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=convert)
        worker.start()
        self.assertTrue(started.wait(2))
        def clear():
            try:
                self.source.clear_media_account(self.db.account)
                cleared.set()
            except Exception as exc:
                errors.append(exc)
        remover = threading.Thread(target=clear)
        remover.start()
        self.assertFalse(cleared.wait(.05))
        release.set()
        worker.join(5)
        remover.join(5)
        self.assertFalse(errors)
        self.assertTrue(cleared.is_set())
        self.assertFalse(self.engine.root.exists())
        self.assertIsNone(self.source.media_asset(self.user, self.by_type["图片"]["id"]))
        self.assertEqual(self.source.media_reason.value, "account-clearing")
        self.assertFalse(self.engine.root.exists())
        self.assertTrue(self.image_path.exists())

    def test_explicit_archive_survives_source_removal_and_cache_clear(self):
        self.source.media_settings(self.db.account, {"mode": "reference"})
        self.install_voice()
        image, voice = self.by_type["图片"], self.by_type["语音"]
        self.source.media_asset(self.user, voice["id"], "playback")
        result = self.source.media_archive(self.db.account, self.user, image["id"])
        self.assertTrue(result["archived"])
        self.image_path.unlink()
        self.assertEqual(self.source.media_asset(self.user, image["id"]).read(), self.image_bytes)
        cleared = self.source.media_settings(self.db.account, clear=True)
        self.assertEqual(cleared["cacheBytes"], 0)
        self.assertEqual(cleared["archiveBytes"], len(self.image_bytes))

    def test_default_archive_survives_source_removal_and_preserves_explicit_reference(self):
        self.assertEqual(self.engine.settings()["mode"], "archive")
        self.source.media_settings(self.db.account, {"cacheMaxBytes": 512 * 1024 * 1024})
        self.source.media_asset(self.user, self.by_type["图片"]["id"])
        new = LocalMedia(self.db, self.engine.downloader, self.engine.root)
        self.assertEqual(new.settings()["mode"], "archive")
        self.assertEqual(new.settings()["cacheMaxBytes"], 512 * 1024 * 1024)
        self.assertGreater(new.settings()["archiveBytes"], 0)
        self.image_path.unlink()
        self.source.media_settings(self.db.account, clear=True)
        self.assertEqual(self.source.media_asset(self.user, self.by_type["图片"]["id"]).read(), self.image_bytes)
        new.settings({"mode": "reference"})
        reopened = LocalMedia(self.db, self.engine.downloader, self.engine.root)
        self.assertEqual(reopened.settings()["mode"], "reference")
        with self.assertRaises(ValueError):
            new.settings({"mode": "download"})
        with self.assertRaises(ValueError):
            new.settings({"cacheMaxBytes": -1})

    def test_cache_limit_removes_only_derived_cache_files(self):
        self.engine.cache_max = 100
        self.engine._cache("a" * 64, "jpg", b"a" * 70, "image/jpeg")
        self.engine._cache("b" * 64, "jpg", b"b" * 70, "image/jpeg")
        self.assertLessEqual(self.engine.settings()["cacheBytes"], 100)
        self.assertFalse((self.engine.root / "cache" / ("a" * 64 + ".jpg")).exists())
        unrelated = self.engine.root / "cache" / "user-note.txt"
        unrelated.write_text("keep", encoding="utf-8")
        self.engine.clear_cache()
        self.assertTrue(unrelated.exists())

    def test_http_streaming_ranges_and_storage_settings(self):
        original = self.install_video()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(SimpleNamespace(source=self.source)))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def request(method, path, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            self.addCleanup(connection.close)
            connection.request(method, path, body=json.dumps(body) if body is not None else None,
                headers={**({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})})
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        url = f'/api/media?account={self.db.account}&user={self.user}&id={self.by_type["视频"]["id"]}&variant=playback'
        status, headers, body = request("GET", url, headers={"Range": "bytes=3-19"})
        self.assertEqual(status, 206)
        self.assertEqual(body, original.read_bytes()[3:20])
        self.assertEqual(headers["Content-Range"], f"bytes 3-19/{original.stat().st_size}")
        self.assertEqual(request("GET", url, headers={"Range": "bytes=999999999-"})[0], 416)
        self.assertEqual(request("GET", url, headers={"Range": "bytes=0-1,3-4"})[0], 416)
        suffix = request("GET", url, headers={"Range": "bytes=-8"})
        self.assertEqual(suffix[2], original.read_bytes()[-8:])
        status, _, raw = request("POST", "/api/media/settings", {"account": self.db.account, "mode": "archive"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["mode"], "archive")
        status, _, raw = request("POST", "/api/media/cache/clear", {"account": self.db.account})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["account"], self.db.account)
        self.assertNotIn(str(self.account).encode(), raw)


if __name__ == "__main__":
    unittest.main()
