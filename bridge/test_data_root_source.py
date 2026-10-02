"""DataRootSource persistence, validation, and env-sync checks."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data_root_source import CUSTOM_ROOT_ENV, DataRootError, DataRootSource


class DataRootSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = DataRootSource(self.root)
        env = patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(CUSTOM_ROOT_ENV, None)

    def _make_dir(self, name="xwechat_files"):
        path = self.root / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_select_persists_and_syncs_env(self):
        data_dir = self._make_dir()
        result = self.store.select(str(data_dir))
        self.assertEqual(result["path"], str(data_dir.resolve()))
        self.assertEqual(os.environ.get(CUSTOM_ROOT_ENV), str(data_dir.resolve()))
        config = json.loads(self.store.config.read_text(encoding="utf-8"))
        self.assertEqual(config, {"schema": 1, "path": str(data_dir.resolve())})

    def test_status_unset_initially(self):
        result = self.store.status()
        self.assertEqual(result, {"state": "unset", "path": "", "exists": False, "accounts": 0})

    def test_select_relative_path_rejected(self):
        with self.assertRaises(DataRootError):
            self.store.select("relative/path")

    def test_select_nonexistent_rejected(self):
        with self.assertRaises(DataRootError):
            self.store.select(str(self.root / "missing"))

    def test_select_file_rejected(self):
        file = self.root / "file.txt"
        file.write_text("x")
        with self.assertRaises(DataRootError):
            self.store.select(str(file))

    def test_select_empty_rejected(self):
        with self.assertRaises(DataRootError):
            self.store.select("")

    def test_db_storage_normalized_to_parent(self):
        acct = self.root / "xwechat_files" / "myaccount"
        (acct / "db_storage").mkdir(parents=True)
        result = self.store.select(str(acct / "db_storage"))
        self.assertEqual(result["path"], str(acct.resolve()))

    def test_clear_resets_state_and_env(self):
        data_dir = self._make_dir()
        self.store.select(str(data_dir))
        result = self.store.clear()
        self.assertEqual(result["state"], "unset")
        self.assertNotIn(CUSTOM_ROOT_ENV, os.environ)

    def test_apply_restores_env(self):
        data_dir = self._make_dir()
        self.store.select(str(data_dir))
        os.environ.pop(CUSTOM_ROOT_ENV, None)
        self.store.apply()
        self.assertEqual(os.environ.get(CUSTOM_ROOT_ENV), str(data_dir.resolve()))

    def test_apply_without_config_removes_env(self):
        os.environ[CUSTOM_ROOT_ENV] = "/some/path"
        self.store.apply()
        self.assertNotIn(CUSTOM_ROOT_ENV, os.environ)

    def test_status_missing_directory(self):
        data_dir = self._make_dir()
        self.store.select(str(data_dir))
        import shutil
        shutil.rmtree(data_dir)
        result = self.store.status()
        self.assertEqual(result["state"], "missing")
        self.assertFalse(result["exists"])

    def test_status_ready_with_accounts(self):
        base = self._make_dir()
        (base / "acct1" / "db_storage").mkdir(parents=True)
        (base / "acct2" / "db_storage").mkdir(parents=True)
        result = self.store.select(str(base))
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["accounts"], 2)

    def test_status_missing_no_accounts(self):
        base = self._make_dir()
        result = self.store.select(str(base))
        self.assertEqual(result["state"], "missing")
        self.assertTrue(result["exists"])


if __name__ == "__main__":
    unittest.main()
