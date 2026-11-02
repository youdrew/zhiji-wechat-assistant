"""Synthetic SDK wait regressions; no provider, credentials or chat data.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugin-sdk" / "python"))
from kyf_plugin import models
from kyf_plugin.channel import Cancelled


class ModelTimeoutTests(unittest.TestCase):
    def test_saved_limit_validation(self):
        self.assertEqual(models.model_timeout_ms(), 3_600_000)
        for value in (60_000, 3_600_000, 10_800_000):
            self.assertEqual(models.model_timeout_ms(value), value)
        for value in (True, False, "3600000", 0, 59_999, 10_800_001, 60_000.0, {}, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                models.model_timeout_ms(value)

    def run_call(self, mode, saved_timeout=3_600_000):
        clock = [0.0]
        sent = []
        reads = []
        cancelled = threading.Event()
        task = SimpleNamespace(check=lambda: None, token="synthetic", cancelled=cancelled)
        service = {"id": "synthetic", "timeoutMs": saved_timeout}
        def services():
            reads.append(True)
            return {"services": [service.copy()]}
        client = models.ModelClient(SimpleNamespace(core=SimpleNamespace(model_services=services)))
        # Existing metadata can be cached; a new inference must still see saved settings.
        client._services = (0.0, [{"id": "synthetic", "timeoutMs": 60_000}])
        class Event:
            def __init__(self):
                self.polls = 0
            def wait(self, interval):
                self.polls += 1
                if mode == "cancel":
                    cancelled.set()
                    return False
                # Since 2026-10-25 the client never expires a wait on its own: the
                # gateway applies Core's deadline once the request leaves its queue,
                # so queueing time cannot fail the call and no deadline sends a cancel.
                clock[0] += 30 * 24 * 3_600_000 if mode == "late" else 301
                if self.polls < (3 if mode == "late" else 2):
                    return False
                waiter = next(iter(client.pending.values()))
                waiter["message"] = {"result": {"text": "synthetic reply"}}
                return True
        with patch.object(client, "_start", return_value=object()), \
             patch.object(client, "_send", side_effect=lambda _process, message: sent.append(message)), \
             patch.object(models.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(models.threading, "Event", Event):
            if mode == "cancel":
                with self.assertRaises(Cancelled):
                    client.generate(task, {"prompt": "synthetic"}, timeout=1)
            else:
                self.assertEqual(client.generate(task, {"prompt": "synthetic", "timeoutMs": 45_000}, timeout=1),
                                 {"text": "synthetic reply"})
        self.assertEqual(len(reads), 1)
        self.assertEqual(sent[0]["payload"]["service"]["timeoutMs"], saved_timeout)
        self.assertEqual(client.pending, {})
        return sent

    def test_slow_prefill_outlives_legacy_plugin_and_metadata_cache_limits(self):
        self.assertEqual(len(self.run_call("success")), 1)

    def test_waiting_never_expires_and_the_client_never_cancels_on_its_own(self):
        # Sixty simulated days past the request, the worker's answer still returns and
        # only the original generate was sent: no client-side deadline exists to cancel.
        sent = self.run_call("late", 10_800_000)
        self.assertEqual([message["cmd"] for message in sent], ["generate"])

    def test_user_stop_remains_immediate_during_long_wait(self):
        self.assertEqual(self.run_call("cancel")[-1]["cmd"], "cancel")


if __name__ == "__main__":
    unittest.main()
