"""Optional voice database key preparation with synthetic bytes only."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from media_assets import MediaUnavailable
from media_database import prepare_voice_databases, prepare_emoticon_databases


class MediaDatabaseFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.account = self.root / "account"
        self.path = self.account / "db_storage" / "message" / "media_0.db"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"s" * 16 + b"v" + b"x" * (4096 - 17))
        self.rel = "message/media_0.db"
        self.cache = self.root / "synthetic-cache.json"
        self.cache.write_text("synthetic", encoding="utf-8")
        self.reader = SimpleNamespace(account_dir=str(self.account), _db_files=[(self.rel, str(self.path), 4096)],
            _keys={"message/message_0.db": b"m" * 32}, keys_file=str(self.root / "unused.json"),
            _stable_key_file=lambda: str(self.cache), _load_key_cache=lambda _path: {})
        self.selection = SimpleNamespace(account_dir=self.account, processes=((123, 1),))
        validate = patch("live_source._valid_page_key", side_effect=lambda raw, _page: raw == b"v" * 32)
        validate.start()
        self.addCleanup(validate.stop)


class VoiceDatabaseTests(MediaDatabaseFixture, unittest.TestCase):
    def test_existing_media_cache_supplements_only_verified_keys_without_scan(self):
        self.reader._load_key_cache = lambda _path: {self.rel: b"v" * 32, "unrelated.db": b"x" * 32}
        with patch("live_source._scan_config_cipher_keys", side_effect=AssertionError("unexpected scan")):
            prepare_voice_databases(self.reader)
        self.assertEqual(self.reader._keys[self.rel], b"v" * 32)
        self.assertNotIn("unrelated.db", self.reader._keys)

    def test_optional_scan_is_scoped_to_voice_pages_and_stays_in_memory(self):
        calls = []
        def scan(pid, pages, _metrics):
            calls.append((pid, set(pages)))
            return {self.rel: b"v" * 32}
        with patch("live_source.active_account_snapshot", return_value=self.selection), \
                patch("live_source._scan_config_cipher_keys", side_effect=scan):
            prepare_voice_databases(self.reader)
            prepare_voice_databases(self.reader)
        self.assertEqual(calls, [(123, {self.rel})])
        self.assertEqual(self.reader._keys[self.rel], b"v" * 32)
        self.assertEqual(self.cache.read_text(), "synthetic")

    def test_changed_account_rejects_scan_and_never_installs_new_keys(self):
        with patch("live_source.active_account_snapshot", return_value=SimpleNamespace(account_dir=self.root / "other")), \
                patch("live_source._scan_config_cipher_keys", side_effect=AssertionError("unexpected scan")):
            with self.assertRaises(MediaUnavailable):
                prepare_voice_databases(self.reader)
        self.assertNotIn(self.rel, self.reader._keys)

    def test_unsuccessful_scan_backs_off_instead_of_rescanning_each_voice(self):
        with patch("live_source.active_account_snapshot", return_value=self.selection), \
                patch("live_source._scan_config_cipher_keys", return_value={}) as scanner:
            prepare_voice_databases(self.reader)
            prepare_voice_databases(self.reader)
        self.assertEqual(scanner.call_count, 1)


class EmoticonDatabaseTests(MediaDatabaseFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.voice_rel, self.voice_path = self.rel, self.path
        self.rel = "emoticon/emoticon.db"
        self.path = self.account / "db_storage" / self.rel
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(self.voice_path.read_bytes())
        self.reader._db_files.append((self.rel, str(self.path), 4096))

    def test_existing_media_cache_supplements_only_verified_keys_without_scan(self):
        self.reader._load_key_cache = lambda _path: {self.rel: b"v" * 32, self.voice_rel: b"v" * 32}
        with patch("live_source._scan_config_cipher_keys", side_effect=AssertionError("unexpected scan")):
            prepare_emoticon_databases(self.reader)
        self.assertEqual(self.reader._keys[self.rel], b"v" * 32)
        self.assertNotIn(self.voice_rel, self.reader._keys)

    def test_optional_scan_is_scoped_to_emoticon_and_stays_in_memory(self):
        with patch("live_source.active_account_snapshot", return_value=self.selection), \
                patch("live_source._scan_config_cipher_keys", return_value={self.rel: b"v" * 32}) as scanner:
            prepare_emoticon_databases(self.reader)
            prepare_emoticon_databases(self.reader)
        self.assertEqual(scanner.call_count, 1)
        self.assertEqual(set(scanner.call_args.args[1]), {self.rel})
        self.assertNotIn(self.voice_rel, self.reader._keys)
        self.assertEqual(self.cache.read_text(), "synthetic")

    def test_changed_account_rejects_scan_and_never_installs_new_keys(self):
        with patch("live_source.active_account_snapshot", return_value=SimpleNamespace(account_dir=self.root / "other")), \
                patch("live_source._scan_config_cipher_keys", side_effect=AssertionError("unexpected scan")):
            with self.assertRaises(MediaUnavailable):
                prepare_emoticon_databases(self.reader)
        self.assertNotIn(self.rel, self.reader._keys)

    def test_unsuccessful_scan_has_independent_emoticon_backoff(self):
        self.reader._voice_key_retry_at = float("inf")
        with patch("live_source.active_account_snapshot", return_value=self.selection), \
                patch("live_source._scan_config_cipher_keys", return_value={}) as scanner:
            prepare_emoticon_databases(self.reader)
            prepare_emoticon_databases(self.reader)
        self.assertEqual(scanner.call_count, 1)


if __name__ == "__main__":
    unittest.main()
