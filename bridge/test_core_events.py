"""Synthetic host-event boundaries for delayed delivery and application restart.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import tempfile
import threading
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core_events import EventService
from core_services import CoreServices
from core_store import CoreStore
from plugin_api import ApiError, PluginAPI

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin-sdk" / "python"))
from kyf_plugin.core import CoreClient


ACCOUNT, USER, PLUGIN = "synthetic-account", "synthetic-friend", "synthetic-plugin"


def message(number, timestamp):
    return {"id": f"message-{number}", "side": "other", "kind": "text", "text": "合成消息",
            "time": timestamp, "senderId": USER, "_senderResolved": True,
            "_sort": [timestamp, "synthetic-shard", number]}


class EventBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = CoreStore(Path(temporary.name) / "core.sqlite3")
        self.addCleanup(self.store.close)
        self.rows = [message(1, 99000)]
        self.supervisor = SimpleNamespace(on=Mock(), send=Mock())
        self.core = SimpleNamespace(store=self.store, supervisor=self.supervisor,
            source=SimpleNamespace(message_windows=lambda users, limit, expected_account: {
                user: self.rows[-limit:] for user in users}),
            registry=SimpleNamespace(enabled=lambda _plugin: True),
            scope=lambda: {"account": ACCOUNT}, require_plugin=Mock())
        self.store.subscribe(PLUGIN, ACCOUNT, USER, "message.new")
        self.store.set_position(PLUGIN, ACCOUNT, USER, self.rows[-1]["_sort"])

    def service(self, started):
        # Drive polling explicitly so the assertions do not depend on a timer race.
        with patch("core_events.time.time", return_value=started), patch("core_events.threading.Thread.start"):
            service = EventService(self.core)
        self.addCleanup(service.close)
        return service

    def without_cursor(self):
        self.store.unsubscribe(PLUGIN, ACCOUNT, USER, "message.new")
        self.store._write(lambda db: db.execute("DELETE FROM event_positions"))

    def subscribe(self, service, **options):
        self.core.events = service
        self.core.ensure_account = Mock()
        self.supervisor.plugin_for_token = Mock(return_value=PLUGIN)
        return PluginAPI(self.core).handle("POST", "/plugin-api/v1/subscriptions", {}, {},
                                          {"account": ACCOUNT, "user": USER, **options})

    def test_first_live_replay_keeps_delayed_messages_but_not_downtime_history(self):
        self.without_cursor()
        service = self.service(100.875)
        self.rows.extend([message(2, 100000), message(3, 102000)])
        with patch("core_events.time.time", return_value=105.5):
            self.assertEqual(self.subscribe(service, replayLive=True), (200, {"ok": True}))
            service.poll()
        event = self.supervisor.send.call_args.args[1]
        self.assertEqual(event["liveSince"], 100000)
        self.assertEqual([row["id"] for row in event["data"]["messages"]], ["message-2", "message-3"])
        self.assertLess(self.store.position(PLUGIN, ACCOUNT, USER), self.rows[1]["_sort"])
        service._ack(PLUGIN, {"id": event["id"]})
        self.assertEqual(self.store.position(PLUGIN, ACCOUNT, USER), self.rows[-1]["_sort"])

    def test_default_subscription_still_starts_at_current_end(self):
        self.without_cursor()
        service = self.service(100.875)
        self.rows.append(message(2, 102000))
        self.subscribe(service)
        service.poll()
        self.supervisor.send.assert_not_called()
        self.assertEqual(self.store.position(PLUGIN, ACCOUNT, USER), self.rows[-1]["_sort"])
        self.rows.append(message(3, 103000))
        service.poll()
        self.assertEqual([row["id"] for row in self.supervisor.send.call_args.args[1]["data"]["messages"]],
                         ["message-3"])

    def test_snapshot_to_subscribe_gap_is_replayed_and_repeated_subscribe_never_rewinds(self):
        self.without_cursor()
        service = self.service(100.875)
        initial_snapshot = list(self.rows)
        self.rows.append(message(2, 102000))
        self.subscribe(service, replayLive=True)
        service.poll()
        event = self.supervisor.send.call_args.args[1]
        self.assertNotIn(event["data"]["messages"][0]["id"], [row["id"] for row in initial_snapshot])
        service._ack(PLUGIN, {"id": event["id"]})
        saved = self.store.position(PLUGIN, ACCOUNT, USER)
        self.subscribe(service, replayLive=True)
        self.assertEqual(self.store.position(PLUGIN, ACCOUNT, USER), saved)
        service.poll()
        self.assertEqual(self.supervisor.send.call_count, 1)

    def test_live_replay_uses_plugin_reactivation_boundary(self):
        self.without_cursor()
        service = self.service(100.875)
        self.rows.extend([message(2, 103000), message(3, 105000)])
        with patch("core_events.time.time", return_value=104.875):
            service.reenabled(PLUGIN)
        self.subscribe(service, replayLive=True)
        service.poll()
        event = self.supervisor.send.call_args.args[1]
        self.assertEqual(event["liveSince"], 104000)
        self.assertEqual([row["id"] for row in event["data"]["messages"]], ["message-3"])

    def test_subscription_source_failure_leaves_no_cursor_and_can_retry(self):
        self.without_cursor()
        service = self.service(100.875)
        source = self.core.source.message_windows
        for failed in (Mock(side_effect=RuntimeError("synthetic unavailable")),
                       Mock(return_value={}), Mock(return_value={USER: None})):
            with self.subTest(source=failed):
                self.core.source.message_windows = failed
                with self.assertRaises(ApiError) as error:
                    self.subscribe(service, replayLive=True)
                self.assertEqual((error.exception.status, error.exception.code), (503, "messages-unavailable"))
                self.assertIsNone(self.store.position(PLUGIN, ACCOUNT, USER))
                self.assertEqual(self.store.subscriptions(plugin=PLUGIN), [])
        self.rows.append(message(2, 102000))
        self.core.source.message_windows = source
        self.subscribe(service, replayLive=True)
        service.poll()
        self.assertEqual(self.supervisor.send.call_args.args[1]["data"]["messages"][0]["id"], "message-2")

    def test_poll_during_cursor_initialization_does_not_skip_or_rewind_an_ack(self):
        self.without_cursor()
        service = self.service(100.875)
        self.store.subscribe(PLUGIN, ACCOUNT, USER, "message.new")
        self.rows.append(message(2, 102000))
        captured, release = threading.Event(), threading.Event()
        source = self.core.source.message_windows
        errors = []

        def windows(users, limit, expected_account):
            snapshot = source(users, limit, expected_account)
            if threading.current_thread().name == "slow-subscribe":
                captured.set()
                if not release.wait(2):
                    raise AssertionError("initialization was not released")
            return snapshot

        def initialize():
            try:
                service.subscribed(PLUGIN, ACCOUNT, USER, replay_live=True)
            except Exception as exc:
                errors.append(exc)

        self.core.source.message_windows = windows
        thread = threading.Thread(target=initialize, name="slow-subscribe")
        thread.start()
        try:
            self.assertTrue(captured.wait(2))
            service.poll()
            self.assertIsNone(self.store.position(PLUGIN, ACCOUNT, USER))
            self.supervisor.send.assert_not_called()
            service.subscribed(PLUGIN, ACCOUNT, USER, replay_live=True)
            self.rows.append(message(3, 103000))
            service.poll()
            event = self.supervisor.send.call_args.args[1]
            service._ack(PLUGIN, {"id": event["id"]})
            acknowledged = self.rows[-1]["_sort"]
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.store.position(PLUGIN, ACCOUNT, USER), acknowledged)

    def test_subscription_option_is_strict_bool_and_sdk_default_stays_compatible(self):
        self.without_cursor()
        service = self.service(100.875)
        for value in (None, 1, 0, "true", [], {}):
            with self.subTest(value=value), self.assertRaises(ApiError) as error:
                self.subscribe(service, replayLive=value)
            self.assertEqual((error.exception.status, error.exception.code), (400, "invalid-request"))
        self.assertIsNone(self.store.position(PLUGIN, ACCOUNT, USER))
        client = CoreClient("http://127.0.0.1/synthetic", "synthetic-token")
        client.post = Mock()
        client.subscribe(ACCOUNT, USER)
        client.post.assert_called_with("subscriptions", {"account": ACCOUNT, "user": USER, "event": "message.new"})
        client.subscribe(ACCOUNT, USER, replay_live=True)
        self.assertIs(client.post.call_args.args[1]["replayLive"], True)

    def test_delayed_first_delivery_preserves_host_start_and_whole_second_precision(self):
        service = self.service(100.875)
        self.rows.extend([message(2, 100000), message(3, 102000)])
        with patch("core_events.time.time", return_value=105.5):
            service.poll()
        plugin, event = self.supervisor.send.call_args.args
        self.assertEqual(plugin, PLUGIN)
        self.assertEqual(event["liveSince"], 100000)
        self.assertEqual([item["id"] for item in event["data"]["messages"]], ["message-2", "message-3"])
        self.assertTrue(all(item["time"] >= event["liveSince"] for item in event["data"]["messages"]))
        # Metadata does not bypass the existing acknowledgment cursor.
        self.assertEqual(self.store.position(PLUGIN, ACCOUNT, USER), self.rows[0]["_sort"])
        service._ack(PLUGIN, {"id": event["id"]})
        self.assertEqual(self.store.position(PLUGIN, ACCOUNT, USER), self.rows[-1]["_sort"])
        service.poll()
        self.assertEqual(self.supervisor.send.call_count, 1)

    def test_restart_boundary_distinguishes_downtime_backlog_from_delayed_live_messages(self):
        first = self.service(100.875)
        first.close()
        # The persisted subscription cursor predates a message received while closed.
        self.rows.extend([message(2, 103000), message(3, 105000)])
        restarted = self.service(104.875)
        with patch("core_events.time.time", return_value=108):
            restarted.poll()
        event = self.supervisor.send.call_args.args[1]
        self.assertEqual(event["liveSince"], 104000)
        self.assertEqual([item["id"] for item in event["data"]["messages"]], ["message-2", "message-3"])
        before, after = event["data"]["messages"]
        self.assertLess(before["time"], event["liveSince"])
        self.assertGreaterEqual(after["time"], event["liveSince"])
        # Core preserves event replay; the plugin applies its no-backlog policy.
        self.assertEqual(self.store.position(PLUGIN, ACCOUNT, USER), self.rows[0]["_sort"])

    def test_message_payload_cannot_override_the_host_boundary(self):
        service = self.service(100.875)
        payload = {"liveSince": 0, "messages": [{**message(2, 102000), "liveSince": 0}]}
        service._deliver(PLUGIN, ACCOUNT, USER, "message.new", payload)
        event = self.supervisor.send.call_args.args[1]
        self.assertEqual(event["liveSince"], 100000)
        self.assertEqual(event["data"], payload)
        service._deliver(PLUGIN, ACCOUNT, USER, "friend.opened", {})
        self.assertNotIn("liveSince", self.supervisor.send.call_args.args[1])

    def test_fast_plugin_reenable_updates_boundary_without_a_poll_or_blocking_shutdown(self):
        service = self.service(100.875)
        self.core.events = service
        enabled = {PLUGIN: True}

        def set_enabled(plugin, value):
            enabled[plugin] = value
            return {"enabled": value}

        def stop(_plugin, _reason):
            acquired = []
            def take_lock():
                with service.lock:
                    acquired.append(True)
            thread = threading.Thread(target=take_lock)
            thread.start()
            thread.join(timeout=1)
            self.assertEqual(acquired, [True], "shutdown must not hold the event acknowledgment lock")

        self.core.registry = SimpleNamespace(enabled=lambda plugin: enabled.get(plugin, True), set_enabled=set_enabled)
        self.core.tasks = SimpleNamespace(stop=Mock())
        self.supervisor.stop, self.supervisor.reset = stop, Mock()
        self.supervisor.reconcile_dependencies = Mock()
        self.core._close_windows_of_unavailable_plugins = Mock()
        with patch("core_events.time.time", return_value=101.8):
            CoreServices.set_enabled(self.core, PLUGIN, False)
        self.supervisor.reconcile_dependencies.assert_called_once_with()
        self.core._close_windows_of_unavailable_plugins.assert_called_once_with(PLUGIN)
        self.rows.extend([message(2, 103000), message(3, 105000)])
        with patch("core_events.time.time", return_value=104.875):
            CoreServices.set_enabled(self.core, PLUGIN, True)
        with patch("core_events.time.time", return_value=106.875):
            CoreServices.set_enabled(self.core, PLUGIN, True)  # Idempotent settings save.
            service.poll()
        event = self.supervisor.send.call_args.args[1]
        self.assertEqual(event["liveSince"], 104000)
        before, after = event["data"]["messages"]
        self.assertLess(before["time"], event["liveSince"])
        self.assertGreaterEqual(after["time"], event["liveSince"])

    def test_reenable_releases_only_its_old_delivery_and_ignores_late_ack(self):
        service = self.service(100.875)
        self.rows.append(message(2, 102000))
        service.poll()
        old = self.supervisor.send.call_args.args[1]
        service._deliver("other-plugin", ACCOUNT, USER, "message.new", {}, position=self.rows[-1]["_sort"])
        other = self.supervisor.send.call_args.args[1]
        with patch("core_events.time.time", return_value=104.875):
            service.reenabled(PLUGIN)
        self.assertNotIn(old["id"], service.in_flight)
        self.assertIn(other["id"], service.in_flight)
        service._ack(PLUGIN, {"id": old["id"]})
        self.assertEqual(self.store.position(PLUGIN, ACCOUNT, USER), self.rows[0]["_sort"])
        self.rows.append(message(3, 105000))
        service.poll()
        event = self.supervisor.send.call_args.args[1]
        self.assertEqual(event["liveSince"], 104000)
        self.assertEqual(event["data"]["messages"][-1]["id"], "message-3")


if __name__ == "__main__":
    unittest.main()
