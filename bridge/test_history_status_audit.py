"""Synthetic-only contract checks for scoped history metadata audits.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin-sdk" / "python"))

from kyf_plugin.core import CoreClient
from plugin_api import ApiError, PluginAPI


class HistoryStatusAuditContracts(unittest.TestCase):
    def test_sdk_keeps_audit_ceiling_and_account_together(self):
        ceiling = json.dumps([400, "message__message_0.db", 40])
        client = CoreClient("http://127.0.0.1:1/plugin-api/v1", "synthetic-token")
        client._request = Mock(return_value={"state": "ready"})
        self.assertEqual(client.history_status("synthetic-account", "friend", fresh=True, audit=True,
                                              audit_highwater=ceiling), {"state": "ready"})
        client._request.assert_called_once_with("GET", "history/status", {
            "account": "synthetic-account", "user": "friend", "fresh": "1", "audit": "1", "auditHighwater": ceiling})

    def test_route_forwards_audit_options_and_keeps_ordinary_status_compatible(self):
        ceiling = json.dumps([400, "message__message_0.db", 40])
        status = Mock(return_value={"state": "ready", "sourceRevision": "source-revision"})
        api = PluginAPI(SimpleNamespace(history=SimpleNamespace(status=status)))
        api._account = lambda account: account
        result = api._history("GET", ["status"], {"account": "synthetic-account", "user": "friend",
            "fresh": "1", "audit": "1", "auditHighwater": ceiling}, {})
        self.assertEqual(result["sourceRevision"], "source-revision")
        status.assert_called_once_with("synthetic-account", "friend", fresh=True, audit=True, audit_highwater=ceiling)
        status.reset_mock()
        api._history("GET", ["status"], {"account": "synthetic-account", "user": "friend"}, {})
        status.assert_called_once_with("synthetic-account", "friend", fresh=False)

    def test_sdk_and_route_batch_ordinary_status(self):
        client = CoreClient("http://127.0.0.1:1/plugin-api/v1", "synthetic-token")
        client._request = Mock(return_value={"results": []})
        client.history_status_many("synthetic-account", ("a", "b"))
        client._request.assert_called_once_with("POST", "history/status-many", {},
                                                {"account": "synthetic-account", "users": ["a", "b"]})
        many = Mock(return_value={"results": []})
        api = PluginAPI(SimpleNamespace(history=SimpleNamespace(status_many=many)))
        api._account = lambda account: account
        api._history("POST", ["status-many"], {}, {"account": "synthetic-account", "users": ["a"]})
        many.assert_called_once_with("synthetic-account", ["a"])
        for body, side_effect in (({"account": "synthetic-account", "users": "a"}, None),
                                  ({"account": "synthetic-account", "users": ["a"]}, ValueError("PRIVATE"))):
            many.side_effect = side_effect
            with self.subTest(body=body), self.assertRaises(ApiError) as error:
                api._history("POST", ["status-many"], {}, body)
            self.assertNotIn("PRIVATE", error.exception.message)

    def test_route_returns_bounded_error_when_audit_source_or_ceiling_is_invalid(self):
        status = Mock(side_effect=ValueError("PRIVATE_SOURCE_DETAIL"))
        api = PluginAPI(SimpleNamespace(history=SimpleNamespace(status=status)))
        api._account = lambda account: account
        with self.assertRaises(ApiError) as error:
            api._history("GET", ["status"], {"account": "synthetic-account", "user": "friend", "audit": "1"}, {})
        self.assertEqual((error.exception.status, error.exception.code), (409, "source-changed"))
        self.assertNotIn("PRIVATE", error.exception.message)


if __name__ == "__main__":
    unittest.main()
