"""Synthetic settings gates: automatic events and explicit manual tasks stay distinct."""
import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from kyf_plugin import CoreError
from kyf_plugin.settings import read_settings
import scheme


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"values": {"autoUpdate": True}, "revision": 0}
        self.core = SimpleNamespace(get=Mock(side_effect=lambda path: copy.deepcopy(self.snapshot)), post=Mock())
        self.plugin = SimpleNamespace(core=self.core)
        self.accounts = Mock()
        self.analysis = Mock()
        self.analysis.maybe_auto.return_value = False

    def test_disabled_auto_update_does_not_sync_or_create_task(self):
        self.snapshot["values"]["autoUpdate"] = False
        start = Mock()
        self.assertFalse(scheme.auto_update(self.plugin, self.analysis, self.accounts, "a", "friend", start))
        self.accounts.account.assert_not_called()
        self.core.post.assert_not_called()
        start.assert_not_called()

    def test_enabled_auto_update_keeps_existing_small_update_gate(self):
        self.assertFalse(scheme.auto_update(self.plugin, self.analysis, self.accounts, "a", "friend", Mock()))
        self.accounts.account.return_value.sync.assert_called_once_with("friend")
        self.analysis.maybe_auto.assert_called_once()
        self.core.post.assert_not_called()

    def test_invalid_or_unavailable_settings_cannot_silently_enable_auto(self):
        for invalid in (None, {"values": {}, "revision": 0}, {"values": {"autoUpdate": 1}, "revision": 0},
                        {"values": {"autoUpdate": True}, "revision": True}):
            self.core.get.side_effect = None
            self.core.get.return_value = invalid
            with self.assertRaises(CoreError):
                scheme.auto_update(self.plugin, self.analysis, self.accounts, "a", "friend", Mock())
        self.accounts.account.assert_not_called()
        self.core.get.side_effect = CoreError(503, "settings-unavailable", "合成读取失败")
        with self.assertRaises(CoreError):
            read_settings(self.core, {"autoUpdate": bool})

    def test_manual_analysis_does_not_read_auto_update_setting(self):
        self.snapshot["values"]["autoUpdate"] = False
        self.analysis.run_task.return_value = "done"
        self.analysis.status.return_value = {"run": {"id": "synthetic-run", "state": "done"}}
        task = SimpleNamespace(account="a", user="friend", cancelled=threading.Event(), finish=Mock())
        scheme.run_scheme(self.analysis, self.accounts, task)
        self.analysis.run_task.assert_called_once_with(task)
        task.finish.assert_called_once_with("done")
        self.core.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
