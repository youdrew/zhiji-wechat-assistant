"""Synthetic first-generation and restored-watch contracts; no live data or model."""
import json
import sys
import tempfile
import threading
import unittest
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugin-sdk" / "python"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from insights import Insights
from prompts import InsightError

ACCOUNT, USER = "synthetic-account", "synthetic-friend"


class ManualGenerationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.calls = []
        self.recent = []
        self.subscribe_options = []
        self.messages = {"old-message": self.message("old-message", 1)}
        self.source_account = ACCOUNT
        self.core = SimpleNamespace(scope=lambda: {"account": self.source_account}, get=self.get, post=self.post,
                                    subscribe=self.subscribe,
                                    unsubscribe=lambda account, user: self.calls.append(("unsubscribe", account, user)))
        service = self.service = Insights.__new__(Insights)
        service.plugin = SimpleNamespace(core=self.core, data_dir=Path(temp.name),
                                         models=SimpleNamespace(services=lambda **_kwargs: [{"stamp": "model"}]))
        service.models = service.plugin.models
        service.clock = lambda: 105.7
        service.lock = threading.RLock()
        service.stores = {}
        service.entries = {}
        service.subscriptions = {}
        service.queue = OrderedDict()
        service.active = {}
        service.wake = threading.Event()
        self.store = service.store(ACCOUNT)
        self.store.set(USER, True)
        self.addCleanup(lambda: [store.close() for store in service.stores.values()])

    @staticmethod
    def message(identifier, seq, **changes):
        return {"id": identifier, "seq": seq, "sortKey": json.dumps([seq, "synthetic", seq]),
                "senderId": USER, "senderResolved": True, "side": "other", "kind": "text", "group": False,
                "text": "Synthetic source content", "raw": "{}", "time": 102000, **changes}

    def get(self, path, **query):
        self.calls.append(("get", path, query))
        if path == "settings":
            return {"revision": 0, "values": {"replyStyle": "natural"}}
        if path == "scope":
            return {"account": self.source_account}
        if path == "history/recent":
            return {"messages": self.recent, "hasMoreBefore": False}
        raise AssertionError(path)

    def subscribe(self, account, user, **options):
        self.calls.append(("subscribe", account, user))
        self.subscribe_options.append(options)

    def post(self, path, body):
        self.calls.append(("post", path, body))
        if path == "history/rows":
            self.assertEqual((body["account"], body["user"]), (ACCOUNT, USER))
            return {"messages": [self.messages[key] for key in body["ids"] if key in self.messages]}
        if path == "history/prepare":
            return {"state": "loading"}
        raise AssertionError(path)

    def test_explicit_generation_reads_core_id_and_registers_both_parts(self):
        result = self.service.generate(ACCOUNT, USER, "old-message")
        item, = result["items"]
        self.assertEqual((item["analysisState"], item["suggestionsState"]), ("pending", "pending"))
        spec = self.service.queue[(ACCOUNT, USER)]["jobs"]["old-message"]
        self.assertFalse(spec["automatic"])
        self.assertEqual(spec["parts"], ("analysis", "suggestions"))
        self.assertEqual(spec["message_time"], 102000)
        self.assertEqual(spec["message_order"], (1, "synthetic", 1))
        self.assertTrue(self.service.wake.is_set())
        source_calls = [call for call in self.calls if call[:2] == ("post", "history/rows")]
        self.assertEqual(source_calls[0][2]["ids"], ["old-message"])
        self.assertIs(source_calls[0][2]["verifySender"], True)

    def test_repeated_and_concurrent_first_generation_are_idempotent(self):
        failures = []
        def generate():
            try:
                self.service.generate(ACCOUNT, USER, "old-message")
            except Exception as error:
                failures.append(error)
        threads = [threading.Thread(target=generate) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        version = self.store.rows(USER)[0]["generation"]
        self.service.generate(ACCOUNT, USER, "old-message")
        self.assertEqual(self.store.rows(USER)[0]["generation"], version)
        self.assertEqual(self.service.queue[(ACCOUNT, USER)]["ids"], ["old-message"])

    def test_missing_or_untrusted_target_never_registers_work(self):
        for changes in ({"side": "self"}, {"senderId": "another-friend"}, {"senderId": ""},
                        {"group": True}, {"kind": "system"}, {"senderResolved": False}):
            with self.subTest(changes=changes):
                self.messages["old-message"] = self.message("old-message", 1, **changes)
                with self.assertRaises(InsightError):
                    self.service.generate(ACCOUNT, USER, "old-message")
        with self.assertRaises(InsightError):
            self.service.generate(ACCOUNT, USER, "unknown-message")
        self.assertTrue(any(call[:2] == ("post", "history/prepare") for call in self.calls))
        self.assertFalse(self.service.queue)
        self.assertEqual(self.store.rows(USER), [])

    def test_enable_keeps_original_message_priority_for_the_whole_unanswered_wave(self):
        self.store.set(USER, False)
        self.recent = [self.message("self-boundary", 1, side="self"),
                       self.message("earlier", 2, time=101000),
                       self.message("system", 3, kind="system"),
                       self.message("latest", 4, time=103000)]
        self.service.set(ACCOUNT, USER, True)
        entry = self.service.queue[(ACCOUNT, USER)]
        self.assertEqual(entry["ids"], ["earlier", "latest"])
        self.assertEqual(entry["jobs"]["earlier"]["message_time"], 101000)
        self.assertEqual(entry["jobs"]["latest"]["message_time"], 103000)
        self.assertEqual(entry["jobs"]["latest"]["message_order"], (4, "synthetic", 4))
        self.assertFalse(any(call[:2] == ("post", "history/rows") for call in self.calls))
        self.assertEqual(self.subscribe_options, [{"replay_live": True}])

    def test_retry_keeps_source_time_instead_of_retry_time(self):
        self.service.generate(ACCOUNT, USER, "old-message")
        original = self.store.rows(USER)[0]["generation"]
        self.store.finish(USER, ["old-message"], original, "error", error="synthetic failure")
        self.service.clock = lambda: 900.0
        self.service.retry(ACCOUNT, USER, "old-message", "suggestions", original)
        spec = self.service.queue[(ACCOUNT, USER)]["jobs"]["old-message"]
        self.assertNotEqual(spec["generation"], original)
        self.assertEqual(spec["parts"], ("suggestions",))
        self.assertEqual(spec["message_time"], 102000)
        self.assertEqual(spec["message_order"], (1, "synthetic", 1))

    def test_disabled_or_changed_account_cannot_generate(self):
        self.store.set(USER, False)
        with self.assertRaises(InsightError):
            self.service.generate(ACCOUNT, USER, "old-message")
        self.store.set(USER, True)
        self.source_account = "different-account"
        with self.assertRaises(InsightError):
            self.service.generate(ACCOUNT, USER, "old-message")
        self.assertFalse(self.service.queue)

    def test_status_stays_read_only_for_missing_results(self):
        for _ in range(2):
            result = self.service.status(ACCOUNT, USER, ["old-message"])
            self.assertEqual(result["items"], [])
        self.assertFalse(self.service.queue)
        self.assertFalse(self.calls)

    def test_restoration_only_subscribes_enabled_friends_without_backfill(self):
        self.store.set("disabled-friend", False)
        self.service.restore_subscriptions()
        self.service.restore_subscriptions()
        self.assertEqual([call for call in self.calls if call[0] == "subscribe"], [("subscribe", ACCOUNT, USER)])
        self.assertEqual(self.subscribe_options, [{"replay_live": True}])
        self.assertIsNone(self.service.entries[(ACCOUNT, USER)]["armed"])
        self.assertFalse(self.service.queue)
        self.assertEqual(self.store.rows(USER), [])

    def test_idempotent_enable_repairs_subscription_without_requeue(self):
        revision = self.store.settings(USER)["revision"]
        self.service.set(ACCOUNT, USER, True)
        self.assertIn(("subscribe", ACCOUNT, USER), self.calls)
        self.assertEqual(self.store.settings(USER)["revision"], revision)
        self.assertFalse(self.service.queue)

    def test_lazy_first_event_uses_core_start_boundary_and_skips_offline(self):
        self.service.restore_subscriptions()
        self.service.on_message({"account": ACCOUNT, "user": USER, "liveSince": 100000,
                                 "data": {"messages": [self.message("offline", 1, time=99000),
                                                        self.message("live", 2, time=102000)]}})
        self.assertEqual(self.service.queue[(ACCOUNT, USER)]["ids"], ["live"])
        self.assertEqual(self.service.entries[(ACCOUNT, USER)]["armed"], 100000)

    def test_explicit_enable_keeps_its_later_boundary(self):
        self.store.set(USER, False)
        self.service.set(ACCOUNT, USER, True)
        self.service.on_message({"account": ACCOUNT, "user": USER, "liveSince": 100000,
                                 "data": {"messages": [self.message("before-enable", 1, time=102000),
                                                        self.message("after-enable", 2, time=105000)]}})
        self.assertEqual(self.service.queue[(ACCOUNT, USER)]["ids"], ["after-enable"])

    def test_missing_or_invalid_live_boundary_does_not_guess(self):
        for boundary in (None, True, -1, 999999):
            with self.subTest(boundary=boundary), self.assertRaises(InsightError):
                self.service.on_message({"account": ACCOUNT, "user": USER, "liveSince": boundary,
                                         "data": {"messages": [self.message("live", 2)]}})
        self.assertFalse(self.service.queue)


if __name__ == "__main__":
    unittest.main()
