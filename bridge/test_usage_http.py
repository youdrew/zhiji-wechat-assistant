"""Synthetic usage endpoints, filter precision and request validation."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core_http import GET_PATHS, HostError, get
from usage_ledger import MAX_SAFE_INTEGER, TIMELINE_BUCKET_LIMIT, UsageLedger


class UsageHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = UsageLedger(Path(self.temp.name) / "usage.sqlite3")
        self.core = SimpleNamespace(
            ledger=self.ledger,
            registry=SimpleNamespace(get=lambda plugin: {"name": "聊天助手"} if plugin == "chat" else None),
            usage_friend_names=lambda account: {"friend": "合成好友"})
        for number, plugin, step, model in ((1, "chat", "answer", "a"), (2, "chat", "summary", "b"),
                                          (3, "analysis", "overview", "a"), (4, "chat", "answer", "b")):
            self.ledger.record({"attemptId": str(number), "at": number * 1000, "plugin": plugin, "step": step,
                                "model": model, "account": "account", "user": "friend", "status": "done",
                                "task": "task-1" if plugin == "chat" else "task-2", "protocol": "responses",
                                "inputTokens": 10, "outputTokens": 2, "totalInputTokens": 10,
                                "totalOutputTokens": 2, "totalTokens": 12, "accountingVersion": 1,
                                "totalTokensSource": "provider"})
        self.assertTrue(self.ledger.flush())

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def read(self, endpoint, **query):
        return get(self.core, "/api/usage/" + endpoint, {"account": "account", **query})

    def test_modules_and_timeline_routes_expose_all_normalized_data(self):
        self.assertIn("/api/usage/modules", GET_PATHS)
        self.assertIn("/api/usage/timeline", GET_PATHS)
        modules = self.read("modules")["modules"]
        self.assertEqual((modules[0]["plugin"], modules[0]["name"], modules[0]["totalTokens"]),
                         ("chat", "聊天助手", 36))
        self.assertEqual([(item["step"], item["totalTokens"]) for item in modules[0]["steps"]],
                         [("answer", 24), ("summary", 12)])
        self.assertIsNone(modules[1]["name"])
        timeline = self.read("timeline", boundaries="0,2000,4000,6000")
        self.assertEqual([item["requests"] for item in timeline["buckets"]], [1, 2, 1])
        self.assertEqual((timeline["firstAt"], timeline["lastAt"]), (1000, 4000))
        self.assertEqual(timeline["buckets"][1]["totalTokens"], 24)
        self.assertEqual({item["plugin"] for item in timeline["buckets"][1]["modules"]}, {"chat", "analysis"})

    def test_step_task_time_and_model_filters_agree_across_endpoints(self):
        filters = {"plugin": "chat", "step": "answer", "task": "task-1", "since": "0", "until": "4000"}
        summary = self.read("summary", **filters, model="a")
        self.assertEqual((summary["requests"], summary["totalTokens"], summary["models"]), (1, 12, ["a"]))
        # With a longer time window, model options still honor the step/task
        # filters while allowing the user to switch away from the current model.
        self.assertEqual(self.read("summary", **{**filters, "until": "5000"}, model="a")["models"], ["a", "b"])
        self.assertEqual((summary["firstAt"], summary["lastAt"]), (1000, 1000))
        friends = self.read("friends", **filters, order="total")["friends"]
        self.assertEqual((friends[0]["name"], friends[0]["requests"], friends[0]["totalTokens"]),
                         ("合成好友", 1, 12))
        record = self.read("records", **filters)["records"][0]
        self.assertEqual((record["step"], record["task"], record["totalTokensSource"], record["accountingVersion"]),
                         ("answer", "task-1", "provider", 1))
        scoped = self.read("timeline", **filters, boundaries="0,2000,4000,6000")
        self.assertEqual([bucket["requests"] for bucket in scoped["buckets"]], [1, 0, 0])

    def test_invalid_boundaries_fail_before_querying(self):
        invalid = (None, "", "0", "0,0", "2,1", "-1,1", "0,1.5", "0,NaN", "0,١",
                   "0," + str(MAX_SAFE_INTEGER + 1), ",".join(str(i) for i in range(TIMELINE_BUCKET_LIMIT + 2)))
        for bounds in invalid:
            with self.subTest(bounds=str(bounds)[:30]), self.assertRaises(HostError) as error:
                self.read("timeline", boundaries=bounds)
            self.assertEqual(error.exception.status, 400)

    def test_self_filter_is_consistent_across_usage_routes_and_keeps_user_empty(self):
        for identifier in ("self-old", "self-new"):
            self.ledger.record({"attemptId": identifier, "at": 5000, "plugin": "kyf.self-analysis",
                                "account": "account", "user": None, "status": "ok", "step": "profile-draft",
                                "protocol": "responses", "model": "self-model", "inputTokens": 10, "outputTokens": 2})
        self.assertTrue(self.ledger.flush())
        with self.ledger.lock:
            self.ledger.connection.execute("UPDATE attempts SET scope='none' WHERE id='self-old'")
        summary = self.read("summary", friend="self")
        self.assertEqual((summary["requests"], summary["totalTokens"], summary["models"]), (2, 24, ["self-model"]))
        records = self.read("records", friend="self")["records"]
        self.assertEqual({row["attemptId"] for row in records}, {"self-new", "self-old"})
        self.assertTrue(all(row["scope"] == "self" and row["user"] is None for row in records))
        friends = self.read("friends", friend="self")["friends"]
        self.assertEqual([(row["scope"], row["user"], row["name"], row["requests"]) for row in friends],
                         [("self", None, None, 2)])
        self.assertEqual(self.read("modules", friend="self")["modules"][0]["requests"], 2)
        timeline = self.read("timeline", friend="self", boundaries="0,4000,6000")
        self.assertEqual([bucket["requests"] for bucket in timeline["buckets"]], [0, 2])
        self.assertEqual(self.read("summary", friend="none")["requests"], 0)

    def test_invalid_time_pagination_and_unknown_query_parameters_fail(self):
        invalid = (("summary", {"since": "100", "until": "100"}),
                   ("summary", {"since": ""}), ("summary", {"since": "-1"}),
                   ("summary", {"until": "9007199254740992"}), ("summary", {"extra": "1"}),
                   ("modules", {"boundaries": "0,1"}), ("friends", {"order": "no-such-order"}),
                   ("records", {"before": "1"}), ("records", {"before": "1,"}),
                   ("records", {"before": "-1,id"}), ("records", {"limit": "0"}),
                   ("records", {"limit": "101"}), ("records", {"limit": "1.5"}))
        for endpoint, query in invalid:
            with self.subTest(endpoint=endpoint, query=query), self.assertRaises(HostError) as error:
                self.read(endpoint, **query)
            self.assertEqual(error.exception.status, 400)
        with self.assertRaises(HostError) as error:
            self.read("unrecognized")
        self.assertEqual(error.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
