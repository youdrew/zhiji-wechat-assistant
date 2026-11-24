"""Synthetic 模拟对话 tests: turn lifecycle, reroll, cancel, reset, isolation and the leak guard.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

All chat text here is invented. The model is a stub; the encoder is a stub unless
``friend_sim_encode`` is importable, in which case one extra test runs the real one.
"""
from __future__ import annotations

import importlib
import json
import random
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
for path in (ROOT / "plugin-sdk" / "python", ROOT / "plugins" / "kyf.friend-analysis" / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import friend_sim  # noqa: E402
from account import AccountData  # noqa: E402
from analysis_store import FriendStore  # noqa: E402
from contracts import AdvisorError  # noqa: E402
from kyf_plugin import Cancelled  # noqa: E402

BACKEND = ROOT / "plugins" / "kyf.friend-analysis" / "backend"
ACCOUNT, USER = "synthetic-account", "wxid_synthetic_friend"
BASE_MS = int(time.mktime((2026, 9, 1, 10, 0, 0, 0, 0, -1)) * 1000)
MY_TEXT = "合成测试：周六一起去爬山吗"
FRIEND_TEXT = "合成回复：这周有点累，下次吧"


class FakeCore:
    """Records every Core call; the simulation must not make any."""

    def __init__(self):
        self.calls = []

    def get(self, path, **query):
        self.calls.append(("GET", path, query))
        if path.startswith("human/"):
            return {"memories": [], "observations": [], "goal": None, "memory_version": 0, "state": {}}
        return {}

    def post(self, path, body=None):
        self.calls.append(("POST", path, body))
        return {}

    def put(self, path, body=None):
        self.calls.append(("PUT", path, body))
        return {}


class FakeContext:
    """The parts of kyf_plugin.TaskContext the turn uses."""

    def __init__(self):
        self.cancelled = threading.Event()
        self.token = "synthetic-token"
        self.finished = None

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()

    def stop(self):
        self.cancelled.set()

    def finish(self, state="done", error=None):
        self.finished = (state, error)


class StubModels:
    def __init__(self):
        self.requests = []
        self.replies = []
        self.gate = None          # an Event: generate waits for it (or for cancellation)
        self.entered = threading.Event()
        self.saw_cancel = threading.Event()

    def services(self, refresh=False):
        return [{"id": "svc", "model": "synthetic", "contextTokens": 32768}]

    def service(self, refresh=False):
        return self.services()[0]

    def generate(self, task, request, step=None):
        task.check()
        self.requests.append((request, step))
        self.entered.set()
        if self.gate is not None:
            while not self.gate.wait(.02):
                if task.cancelled.is_set():
                    self.saw_cancel.set()
                    raise Cancelled()
        reply = self.replies.pop(0) if self.replies else {"stance": "简短回应", "messages": [FRIEND_TEXT]}
        return {"text": reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)}


class FakePlugin:
    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.core = FakeCore()
        self.models = StubModels()
        self.tasks = []
        self.logs = []
        self.threads = []

    def start_task(self, account, user, *, kind="interactive", title, params=None, handler):
        context = FakeContext()
        self.tasks.append({"account": account, "user": user, "kind": kind, "title": title, "params": params,
                           "context": context})

        def work():
            try:
                outcome = handler(context)
                if context.finished is None and not context.cancelled.is_set():
                    context.finish(outcome)
            except Cancelled:
                pass
        thread = threading.Thread(target=work, daemon=True)
        self.threads.append(thread)
        thread.start()
        return {"id": "task-%d" % len(self.tasks)}, context

    def log(self, message, level="info"):
        self.logs.append((level, message))


class StubEncoder:
    """friend_sim_encode's contract with fixed answers; records the arguments it was given."""

    def __init__(self):
        self.probability = 1.0
        self.sample_rows = [{"input": ["合成样例输入"], "reply": ["合成样例回复一", "合成样例回复二"],
                             "timeMs": BASE_MS, "seq": 2}]
        self.calls = []

    def refresh(self, database, user):
        self.calls.append(("refresh", user))

    def encode(self, database, store, user, before_ms=None):
        self.calls.append(("encode", before_ms))
        return {"analysed": False, "warning": "未做知己分析，模拟可能不准",
                "layers": [{"key": "rules", "title": "硬规则", "lines": ["合成规则"]}], "style": {},
                "bubbles": {1: [1, 2]}, "replyRate": {"rate": self.probability, "medianSeconds": 60, "samples": 9}}

    def samples(self, database, user, turns, limit=8, before_ms=None):
        self.calls.append(("samples", turns, limit, before_ms))
        return list(self.sample_rows)

    def prefix(self, profile, budget=3000):
        return "\n".join(line for layer in profile["layers"] for line in layer["lines"])[:budget]

    def bubble_count(self, profile, my_count, rng):
        self.calls.append(("bubble_count", my_count))
        return 1

    def reply_probability(self, profile):
        return self.probability


def seed(database, rows):
    with database.lock, database._connection:
        for seq, (side, text, offset) in enumerate(rows, 1):
            database._connection.execute(
                "INSERT INTO context_messages(user,seq,msg_id,sort_key,group_chat,side,sender_id,sender_name,kind,"
                "text,time_ms,raw) VALUES(?,?,?,?,0,?,'','','text',?,?,'{}')",
                (USER, seq, "m%d" % seq, "%08d" % seq, side, text, BASE_MS + offset))
        database._connection.execute(
            "INSERT INTO context(user,state,revision,read_count,total_count,error,source_id,epoch,synced_at_ms) "
            "VALUES(?, 'ready', 1, ?, ?, NULL, 'fp-synthetic', 'e1', 0)", (USER, len(rows), len(rows)))


def wait(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return False


class SimulationTests(unittest.TestCase):
    encoder_factory = StubEncoder

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin = FakePlugin(Path(self.tmp.name))
        self.database = AccountData(self.plugin, ACCOUNT)
        seed(self.database, [("self", "合成近况：最近忙吗", 0), ("other", "合成近况：还行", 60_000),
                             ("self", "合成近况：周末有空吗", 3_600_000), ("other", "合成近况：看情况", 3_660_000)])
        self.encoder = self.encoder_factory()
        self.sims = friend_sim.Simulations(self.plugin, self.scope, encoder=self.encoder, rng=random.Random(7))

    def tearDown(self):
        self.sims.close()
        for thread in self.plugin.threads:
            thread.join(5)
        self.database.close()
        self.tmp.cleanup()

    def scope(self, account, user):
        return account, user, "fp-synthetic", self.database, FriendStore(self.database)

    def done(self, request_id):
        self.assertTrue(wait(lambda: self.sims.status(ACCOUNT, USER, request_id)["state"]
                             not in ("queued", "running")))
        return self.sims.status(ACCOUNT, USER, request_id)

    def send(self, *messages, **extra):
        return self.sims.send(ACCOUNT, USER, {"messages": list(messages), **extra})["requestId"]

    # -- lifecycle ----------------------------------------------------------------------------------------------
    def test_reply_is_one_interactive_task_and_returns_a_public_turn(self):
        status = self.done(self.send(MY_TEXT))
        self.assertEqual(status["state"], "done")
        turn = status["turn"]
        self.assertEqual((turn["side"], turn["messages"], turn["noReply"]), ("friend", [FRIEND_TEXT], False))
        self.assertEqual(turn["samples"], [{"input": ["合成样例输入"], "reply": ["合成样例回复一", "合成样例回复二"],
                                            "timeMs": BASE_MS, "id": "m2"}])
        self.assertNotIn("stance", json.dumps(status, ensure_ascii=False), "stance stays in the backend")
        self.assertNotIn("debug", status)
        task = self.plugin.tasks[0]
        self.assertEqual((task["kind"], task["title"], task["params"]), ("interactive", "模拟对话", {"kind": "friend-sim"}))
        request, step = self.plugin.models.requests[0]
        self.assertEqual(step, "friend-sim")
        prompt = json.loads(request["prompt"])
        self.assertEqual(prompt["conversation"], [{"from": "me", "messages": [MY_TEXT]}])
        self.assertEqual([item["text"] for item in prompt["recent"]][-1], "合成近况：看情况")
        self.assertEqual(prompt["count"], 2, "the cap comes from the drawn sample's bubble count")
        self.assertIn("硬规则", request["system"])
        view = self.sims.view(ACCOUNT, USER)
        self.assertEqual([item["side"] for item in view["turns"]], ["me", "friend"])
        self.assertIsNone(view["pending"])
        self.assertEqual(len(view["context"]), 4)
        self.assertEqual(view["profile"], {"layers": [{"key": "rules", "title": "硬规则", "lines": ["合成规则"]}]})
        self.assertFalse(view["analysed"])

    def test_no_reply_roll_skips_the_model(self):
        self.encoder.probability = 0.0
        status = self.done(self.send(MY_TEXT))
        self.assertEqual(status["state"], "done")
        self.assertEqual((status["turn"]["noReply"], status["turn"]["messages"]), (True, []))
        self.assertEqual(self.plugin.models.requests, [], "a friend who does not reply costs no model call")

    def test_unknown_reply_rate_always_replies(self):
        self.encoder.probability = None
        self.assertFalse(self.done(self.send(MY_TEXT))["turn"]["noReply"])

    def test_extra_messages_fold_into_the_cap(self):
        self.encoder.sample_rows = []
        self.plugin.models.replies = [{"stance": "接住并展开", "messages": ["一", "二", "三"]}]
        turn = self.done(self.send("在吗", "问个事"))["turn"]
        self.assertEqual(turn["messages"], ["一 二 三"], "bubble_count's cap of 1 folds the rest into the last")
        self.assertIn(("bubble_count", 2), self.encoder.calls, "my trailing run sent two messages")

    def test_invalid_output_is_retried_once(self):
        self.plugin.models.replies = ["不是 JSON", {"stance": "新话题", "messages": ["好"]}]
        status = self.done(self.send(MY_TEXT))
        self.assertEqual((status["state"], status["turn"]["messages"]), ("done", ["好"]))
        self.assertEqual(len(self.plugin.models.requests), 2)

    def test_failed_turn_drops_my_turn_and_frees_the_session(self):
        # The page gives the lines back to the input, so the session must not keep them as well.
        self.plugin.models.replies = ["坏", "还是坏"]
        status = self.done(self.send(MY_TEXT))
        self.assertEqual(status["state"], "failed")
        self.assertTrue(status["error"])
        view = self.sims.view(ACCOUNT, USER)
        self.assertEqual((view["turns"], view["pending"]), ([], None))
        self.assertEqual(self.done(self.send(MY_TEXT))["state"], "done", "the lines can simply be sent again")

    def test_failed_or_cancelled_reroll_keeps_the_reply_it_would_replace(self):
        first = self.done(self.send(MY_TEXT))["turn"]
        self.plugin.models.replies = ["坏", "还是坏"]
        self.assertEqual(self.done(self.sims.reroll(ACCOUNT, USER, {})["requestId"])["state"], "failed")
        self.assertEqual([item["id"] for item in self.sims.view(ACCOUNT, USER)["turns"]][1:], [first["id"]])
        self.plugin.models.gate = threading.Event()
        request_id = self.sims.reroll(ACCOUNT, USER, {})["requestId"]
        self.assertTrue(self.plugin.models.entered.wait(5))
        self.sims.cancel(ACCOUNT, USER, request_id)
        self.assertEqual(self.done(request_id)["state"], "cancelled")
        view = self.sims.view(ACCOUNT, USER)
        self.assertEqual(([item["side"] for item in view["turns"]], view["turns"][1]["id"], view["pending"]),
                         (["me", "friend"], first["id"], None))

    def test_a_cancel_racing_the_finish_never_reports_cancelled_for_a_stored_reply(self):
        self.plugin.models.gate = threading.Event()
        request_id = self.send(MY_TEXT)
        self.assertTrue(self.plugin.models.entered.wait(5))
        job = self.sims.jobs[request_id]
        finish = self.sims.finish

        def cancel_then_finish(job_, turn):
            # The cancel lands after the reply passed its last check but before it is stored.
            self.sims.cancel(ACCOUNT, USER, request_id)
            return finish(job_, turn)
        self.sims.finish = cancel_then_finish
        self.plugin.models.gate.set()
        status = self.done(request_id)
        view = self.sims.view(ACCOUNT, USER)
        self.assertEqual((status["state"], view["turns"], view["pending"]), ("cancelled", [], None))
        self.assertTrue(job.stopped.is_set())

    def test_reroll_replaces_the_last_friend_turn(self):
        first = self.done(self.send(MY_TEXT))["turn"]
        self.plugin.models.replies = [{"stance": "明确婉拒", "messages": ["不去"]}]
        second = self.done(self.sims.reroll(ACCOUNT, USER, {})["requestId"])["turn"]
        turns = self.sims.view(ACCOUNT, USER)["turns"]
        self.assertEqual([item["side"] for item in turns], ["me", "friend"])
        self.assertEqual(turns[1]["id"], second["id"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(turns[1]["messages"], ["不去"])
        prompt = json.loads(self.plugin.models.requests[1][0]["prompt"])
        self.assertEqual(prompt["conversation"], [{"from": "me", "messages": [MY_TEXT]}],
                         "the rerolled reply never sees the reply it replaces")

    def test_reroll_includes_the_reply_roll(self):
        self.done(self.send(MY_TEXT))
        self.encoder.probability = 0.0
        turn = self.done(self.sims.reroll(ACCOUNT, USER, {})["requestId"])["turn"]
        self.assertTrue(turn["noReply"])
        self.assertEqual(len(self.sims.view(ACCOUNT, USER)["turns"]), 2)

    def test_reroll_without_my_turn_is_refused(self):
        with self.assertRaises(AdvisorError):
            self.sims.reroll(ACCOUNT, USER, {})

    def test_send_while_running_is_refused(self):
        self.plugin.models.gate = threading.Event()
        request_id = self.send(MY_TEXT)
        self.assertTrue(self.plugin.models.entered.wait(5))
        self.assertEqual(self.sims.view(ACCOUNT, USER)["pending"], request_id)
        with self.assertRaises(AdvisorError) as raised:
            self.send("再发一条")
        self.assertEqual(raised.exception.code, "request-conflict")
        with self.assertRaises(AdvisorError):
            self.sims.reroll(ACCOUNT, USER, {})
        self.plugin.models.gate.set()
        self.assertEqual(self.done(request_id)["state"], "done")
        self.assertEqual(len(self.sims.view(ACCOUNT, USER)["turns"]), 2, "the refused send added nothing")

    def test_cancel_stops_the_model_call_and_discards_the_reply(self):
        self.plugin.models.gate = threading.Event()
        request_id = self.send(MY_TEXT)
        self.assertTrue(self.plugin.models.entered.wait(5))
        self.assertEqual(self.sims.cancel(ACCOUNT, USER, request_id), {"ok": True})
        self.assertTrue(self.plugin.models.saw_cancel.wait(5), "the Core task's stop reaches the model call")
        self.assertEqual(self.done(request_id)["state"], "cancelled")
        self.assertTrue(self.plugin.tasks[0]["context"].cancelled.is_set())
        view = self.sims.view(ACCOUNT, USER)
        self.assertEqual((view["turns"], view["pending"]), ([], None), "my unanswered turn goes back to the input")
        self.plugin.models.gate = None
        self.assertEqual(self.done(self.send("那改天？"))["state"], "done", "the session is free again")

    def test_cancel_of_another_friends_request_is_ignored(self):
        self.plugin.models.gate = threading.Event()
        request_id = self.send(MY_TEXT)
        self.assertEqual(self.sims.cancel(ACCOUNT, "wxid_other", request_id), {"ok": True})
        with self.assertRaises(AdvisorError):
            self.sims.status(ACCOUNT, "wxid_other", request_id)
        self.plugin.models.gate.set()
        self.assertEqual(self.done(request_id)["state"], "done")

    def test_reset_clears_the_session_and_drops_a_late_reply(self):
        self.done(self.send(MY_TEXT))
        self.assertEqual(self.sims.reset(ACCOUNT, USER), {"ok": True})
        self.assertEqual(self.sims.view(ACCOUNT, USER)["turns"], [])
        self.plugin.models.gate = threading.Event()
        request_id = self.send("新的开场")
        self.assertTrue(self.plugin.models.entered.wait(5))
        self.sims.reset(ACCOUNT, USER)
        self.assertEqual(self.done(request_id)["state"], "cancelled")
        view = self.sims.view(ACCOUNT, USER)
        self.assertEqual((view["turns"], view["pending"]), ([], None))

    def test_sessions_are_per_friend(self):
        self.done(self.send(MY_TEXT))
        self.assertEqual(self.sims.view(ACCOUNT, "wxid_other")["turns"], [])

    def test_model_state_tells_missing_from_unavailable(self):
        view = self.sims.view(ACCOUNT, USER)
        self.assertEqual((view["model"], view["configured"]), ("ready", True))
        self.plugin.models.services = lambda refresh=False: []
        view = self.sims.view(ACCOUNT, USER)
        self.assertEqual((view["model"], view["configured"]), ("missing", False))

        # The gateway is down: Core answers model/services with 503, which the SDK raises as CoreError.
        from kyf_plugin.core import CoreError

        def down(refresh=False):
            raise CoreError(503, "gateway-crashed", "模型网关连续异常退出，请重新启动应用")
        self.plugin.models.services = down
        view = self.sims.view(ACCOUNT, USER)
        self.assertEqual((view["model"], view["configured"]), ("unavailable", True),
                         "an unreachable gateway is not reported as an unsaved model")

    def test_send_validation(self):
        for messages in ([], ["一"] * 7, [""], ["长" * 1001], [3]):
            with self.assertRaises(AdvisorError):
                self.sims.send(ACCOUNT, USER, {"messages": messages})
        with self.assertRaises(AdvisorError):
            self.sims.send(ACCOUNT, USER, {"messages": ["好"], "beforeTimeMs": "昨天"})
        self.assertEqual(self.sims.view(ACCOUNT, USER)["turns"], [])

    # -- leak guard ---------------------------------------------------------------------------------------------
    def test_before_time_is_passed_through(self):
        cut = BASE_MS + 3_600_000
        status = self.done(self.send(MY_TEXT, beforeTimeMs=cut))
        self.assertIn(("encode", cut), self.encoder.calls)
        samples = [call for call in self.encoder.calls if call[0] == "samples"]
        self.assertEqual(samples[-1][2:], (8, cut))
        self.assertEqual(samples[-1][1], [{"side": "me", "messages": [MY_TEXT]}])
        prompt = json.loads(self.plugin.models.requests[0][0]["prompt"])
        self.assertEqual([item["text"] for item in prompt["recent"]], ["合成近况：最近忙吗", "合成近况：还行"],
                         "recent messages stop before the cut too")
        self.assertEqual(status["debug"]["stance"], "简短回应", "evaluation requests can read the stance")
        self.assertEqual(status["debug"]["count"], 2)

    # -- isolation (§5) -----------------------------------------------------------------------------------------
    def test_simulated_text_never_reaches_storage_or_core(self):
        self.done(self.send(MY_TEXT))
        self.done(self.sims.reroll(ACCOUNT, USER, {})["requestId"])
        self.encoder.probability = 0.0
        self.done(self.send("合成测试：那算了"))
        self.sims.view(ACCOUNT, USER)
        # Every table of the plugin database, every text column.
        with self.database.lock:
            connection = self.database._connection
            tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                for row in connection.execute(f'SELECT * FROM "{table}"'):
                    blob = json.dumps([str(value) for value in row], ensure_ascii=False)
                    for text in (MY_TEXT, FRIEND_TEXT, "合成测试：那算了"):
                        self.assertNotIn(text, blob, f"simulated text leaked into {table}")
        self.assertEqual(self.plugin.core.calls, [], "no Core reads or writes: no memory, analysis, advisor or send")
        for task in self.plugin.tasks:
            self.assertNotIn(MY_TEXT, json.dumps({key: task[key] for key in ("title", "params")}, ensure_ascii=False))

    def test_simulation_has_no_send_path_or_writer_imports(self):
        source = (BACKEND / "friend_sim.py").read_text(encoding="utf-8")
        for forbidden in ("send_wechat", "messages/send", "control.", "candidates", "import memory",
                          "save_result", "INSERT", "UPDATE ", "DELETE", ".post(", ".put("):
            self.assertNotIn(forbidden, source)
        manifest = json.loads((ROOT / "plugins" / "kyf.friend-analysis" / "plugin.json").read_text(encoding="utf-8"))
        self.assertNotIn("send", json.dumps({key: value for key, value in manifest.items()
                                             if key not in ("description", "contributes")}).lower())
        self.assertFalse(any("send" in str(item).lower() for item in manifest.get("permissions", [])))
        main = (BACKEND / "main.py").read_text(encoding="utf-8")
        methods = [line for line in main.splitlines() if 'plugin.method("page.simulation' in line]
        self.assertEqual(len(methods), 6)

    def test_tag_and_timeline_edits_drop_the_cached_profile(self):
        self.sims.view(ACCOUNT, USER)
        self.sims.view(ACCOUNT, USER)
        self.assertEqual(sum(1 for call in self.encoder.calls if call[0] == "encode"), 1, "cached within a session")
        self.sims.forget_profile(ACCOUNT, "wxid_other_friend")
        self.sims.view(ACCOUNT, USER)
        self.assertEqual(sum(1 for call in self.encoder.calls if call[0] == "encode"), 1, "another friend's edit")
        self.sims.forget_profile(ACCOUNT, USER)
        self.sims.view(ACCOUNT, USER)
        self.assertEqual(sum(1 for call in self.encoder.calls if call[0] == "encode"), 2)
        main = (BACKEND / "main.py").read_text(encoding="utf-8")
        for method in ("tagEdit", "tagUndo", "tagUndoAgent", "timelineEdit", "timelineUndo", "timelineUndoAgent"):
            body = main.split('@plugin.method("page.%s")' % method, 1)[1].split("@plugin.method", 1)[0]
            self.assertIn("return edited(account, user,", body, method)

    def test_close_cancels_open_turns(self):
        self.plugin.models.gate = threading.Event()
        request_id = self.send(MY_TEXT)
        self.assertTrue(self.plugin.models.entered.wait(5))
        self.sims.close()
        self.assertEqual(self.done(request_id)["state"], "cancelled")
        with self.assertRaises(AdvisorError):
            self.send("退出后")


def real_encoder():
    try:
        return importlib.import_module("friend_sim_encode")
    except ImportError:
        return None


@unittest.skipIf(real_encoder() is None, "friend_sim_encode is not available yet")
class RealEncoderTests(unittest.TestCase):
    """The same turn against the real encoder on a synthetic mirror (interface check)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plugin = FakePlugin(Path(self.tmp.name))
        self.database = AccountData(self.plugin, ACCOUNT)
        rows, offset = [], 0
        for index in range(40):
            rows.append(("self", "合成问题%d：周末去哪玩" % index, offset))
            rows.append(("other", "合成回答%d：看天气吧" % index, offset + 60_000))
            rows.append(("other", "合成补充%d：不下雨就去" % index, offset + 90_000))
            offset += 86_400_000
        seed(self.database, rows)
        self.sims = friend_sim.Simulations(self.plugin, self.scope, encoder=real_encoder(), rng=random.Random(3))

    def tearDown(self):
        self.sims.close()
        for thread in self.plugin.threads:
            thread.join(5)
        self.database.close()
        self.tmp.cleanup()

    def scope(self, account, user):
        return account, user, "fp-synthetic", self.database, FriendStore(self.database)

    def test_turn_runs_with_the_real_encoder(self):
        view = self.sims.view(ACCOUNT, USER)
        self.assertIsInstance(view["profile"]["layers"], list)
        self.assertIsInstance(view["analysed"], bool)
        cut = BASE_MS + 20 * 86_400_000
        request_id = self.sims.send(ACCOUNT, USER, {"messages": ["周末去哪玩"], "beforeTimeMs": cut})["requestId"]
        self.assertTrue(wait(lambda: self.sims.status(ACCOUNT, USER, request_id)["state"] not in ("queued", "running"),
                             20))
        status = self.sims.status(ACCOUNT, USER, request_id)
        self.assertEqual(status["state"], "done", status)
        for sample in status["turn"]["samples"]:
            self.assertLess(sample["timeMs"], cut)
        self.assertTrue(status["turn"]["samples"], "the synthetic mirror yields reply samples")
        self.assertTrue(all(sample["id"].startswith("m") for sample in status["turn"]["samples"]),
                        "samples carry the message id page.message opens")
        if not status["turn"]["noReply"]:
            self.assertTrue(status["turn"]["messages"])

    def test_reply_context_for_the_advisor_describes_ta_and_adds_my_style(self):
        """分析与建议 (2026-11-09): the same profile, without role-play rules, plus how I talk to ta."""
        played = self.sims.reply_context(ACCOUNT, USER, {"budgetChars": 2400})
        described = self.sims.reply_context(ACCOUNT, USER, {"budgetChars": 2400, "audience": "advisor"})
        self.assertIn("你就是这位好友本人", played["text"])
        self.assertNotIn("mine", played)
        self.assertNotIn("你就是这位好友本人", described["text"])
        self.assertTrue(described["mine"].startswith("【我对 ta 说话的方式"), described)
        self.assertTrue(described["available"])


if __name__ == "__main__":
    unittest.main()
