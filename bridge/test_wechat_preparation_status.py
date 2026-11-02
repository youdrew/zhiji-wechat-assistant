"""Synthetic startup diagnostics regressions. Copyright 2026 Know Your Friends.

SPDX-License-Identifier: Apache-2.0
"""
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from backend_contracts import AccountUnavailableError
from backend_service import Backend
from wechat_source import WeChatSource, _wechat_process_running


class ProcessPresenceTests(unittest.TestCase):
    def query(self, names):
        api = SimpleNamespace(process_iter=Mock(return_value=[
            SimpleNamespace(info={"name": name}) for name in names]))
        with patch.dict(sys.modules, {"psutil": api}):
            result = _wechat_process_running()
        api.process_iter.assert_called_once_with(["name"])
        return result

    def test_recognizes_supported_names_without_reading_process_files(self):
        self.assertIs(self.query(["other.exe", "Weixin.exe"]), True)
        self.assertIs(self.query(["WECHAT.EXE"]), True)
        self.assertIs(self.query([None, "WeChat.exe"]), True)

    def test_only_complete_name_enumeration_proves_absence(self):
        self.assertIs(self.query(["other.exe"]), False)
        self.assertIs(self.query([]), False)
        self.assertIsNone(self.query([None, "other.exe"]))
        self.assertIsNone(self.query([""]))

    def test_missing_optional_dependency_is_unknown(self):
        with patch.dict(sys.modules, {"psutil": None}):
            self.assertIsNone(_wechat_process_running())

    def test_query_or_iteration_failure_is_unknown(self):
        def incomplete():
            yield SimpleNamespace(info={"name": "other.exe"})
            raise PermissionError("synthetic-private-detail")

        for api in (
            SimpleNamespace(process_iter=Mock(side_effect=PermissionError("private"))),
            SimpleNamespace(process_iter=Mock(return_value=incomplete())),
        ):
            with self.subTest(api=api), patch.dict(sys.modules, {"psutil": api}):
                self.assertIsNone(_wechat_process_running())


class PreparationStatusTests(unittest.TestCase):
    def setUp(self):
        # Do not instantiate live readers or inspect any real WeChat process/account.
        self.source = WeChatSource.__new__(WeChatSource)
        self.source.live_account = True
        self.source.factory = None
        self.source._db = Mock(side_effect=AssertionError("must not open a reader"))
        self.source.active_account_locator = Mock(side_effect=AssertionError("must not scan ownership"))
        self.presence = patch("wechat_source._wechat_process_running", return_value=True).start()
        self.addCleanup(patch.stopall)

    def tearDown(self):
        self.source._db.assert_not_called()
        self.source.active_account_locator.assert_not_called()

    def test_stopped_wechat_overrides_stale_factory_preparation(self):
        self.presence.return_value = False
        old_status = Mock(return_value={"reason": "scan_limit", "state": "failed"})
        self.source.factory = SimpleNamespace(preparation_status=old_status)
        result = self.source.preparation_status()
        self.assertEqual(result["reason"], "wechat_not_running")
        self.assertIn("打开电脑版微信并登录", result["message"])
        old_status.assert_not_called()

    def test_running_without_preparation_does_not_assert_logged_out(self):
        result = self.source.preparation_status()
        self.assertEqual(result["reason"], "active_account_unavailable")
        self.assertIn("微信已运行", result["message"])
        self.assertIn("暂时无法确认当前账号", result["message"])
        self.assertNotIn("未登录", result["message"])

    def test_unknown_presence_does_not_assert_running_or_stopped(self):
        self.presence.return_value = None
        result = self.source.preparation_status()
        self.assertEqual(result["reason"], "active_account_unavailable")
        self.assertNotIn("微信已运行", result["message"])
        self.assertNotIn("未检测到", result["message"])

    def test_preparing_and_failure_diagnostics_keep_account_filter_and_progress(self):
        for running in (True, None):
            for state, reason in (("preparing", "preparing"), ("failed", "scan_limit")):
                expected = {"reason": reason, "state": state,
                            "message": "Synthetic preparation status", "retryAfterSeconds": 30}
                factory_status = Mock(return_value=expected)
                self.source.factory = SimpleNamespace(preparation_status=factory_status)
                self.presence.return_value = running
                self.assertEqual(self.source.preparation_status("synthetic-account"), expected)
                factory_status.assert_called_once_with("synthetic-account")

    def test_factory_query_failure_is_not_reported_as_missing_wechat(self):
        self.source.factory = SimpleNamespace(preparation_status=Mock(
            side_effect=RuntimeError("synthetic-private-detail")))
        result = self.source.preparation_status()
        self.assertEqual(result["reason"], "active_account_unavailable")
        self.assertNotIn("synthetic-private-detail", str(result))

    def test_injected_sources_do_not_inspect_host_processes(self):
        self.source.live_account = False
        self.assertIsNone(self.source.preparation_status())
        expected = {"reason": "preparing", "state": "preparing"}
        self.source.factory = SimpleNamespace(preparation_status=lambda _: expected)
        self.assertEqual(self.source.preparation_status(), expected)
        self.presence.assert_not_called()

    def test_backend_prefers_source_and_filters_private_fields(self):
        self.source.factory = SimpleNamespace(preparation_status=Mock())
        self.source.preparation_status = Mock(return_value={
            "reason": "active_account_unavailable", "state": "unavailable",
            "message": "Synthetic status", "path": "synthetic-private-detail", "keys": "private"})
        backend = Backend.__new__(Backend)
        backend.source = self.source
        result = backend.preparation_status("synthetic-account")
        self.assertEqual(set(result), {"reason", "state", "message"})
        self.source.preparation_status.assert_called_once_with("synthetic-account")
        self.source.factory.preparation_status.assert_not_called()

    def test_backend_keeps_legacy_factory_hook(self):
        expected = {"reason": "scan_limit", "state": "failed", "message": "Synthetic status"}
        hook = Mock(return_value=expected)
        backend = Backend.__new__(Backend)
        backend.source = SimpleNamespace(factory=SimpleNamespace(preparation_status=hook))
        self.assertEqual(backend.preparation_status("synthetic-account"), expected)
        hook.assert_called_once_with("synthetic-account")

    def test_health_keeps_account_unavailable_with_actionable_preparation(self):
        self.presence.return_value = False
        self.source.advisor_identity = Mock(side_effect=AccountUnavailableError())
        backend = Backend.__new__(Backend)
        backend.source = self.source
        backend.analyzer = SimpleNamespace(model={"state": "ready"})
        result = backend.health()
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["state"], "account-unavailable")
        self.assertEqual(result["data"]["preparation"]["reason"], "wechat_not_running")
        self.assertNotIn("account", result["data"])


if __name__ == "__main__":
    unittest.main()
