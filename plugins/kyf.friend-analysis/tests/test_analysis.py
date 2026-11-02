"""Synthetic tests for the 知己分析 v2 unified analysis with a scripted model.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Moved with the analysis into the kyf.friend-analysis plugin, 2026-10-13: Core's real
history, human-data and task services run in process (see harness.py).
"""
import json
import itertools
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path

from harness import Friends, Harness
from kyf_plugin import persona as persona_catalog

import friend_chat
import friend_overview
import friend_persona
from analysis_store import FriendStore, now_ms
from backend_contracts import MessageWindow
from friend_extract import DEDUP_SYSTEM, EXTRACT_SYSTEM
from friend_run_store import RunStore
from test_support import FakeModelStore, FakeRuntime, FakeSource, make_message, wait_until

ACCOUNT = "synthetic-account-1"
USER = "wxid_synthetic_friend"
DAY = 86_400_000


class WindowSource(FakeSource):
    """Adds the latest-window read that the main chat uses."""

    def messages(self, user, limit, offset=0):
        rows = sorted(self.users.get(user, []), key=lambda item: tuple(item["_sort"]))
        return MessageWindow([dict(item) for item in rows[-limit:]], len(rows) > limit)


class ScriptedRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.gate = {}
        self.entered = {}
        self.lock = threading.Lock()

    def kind(self, system):
        return {EXTRACT_SYSTEM: "extract", DEDUP_SYSTEM: "dedup", friend_overview.SUMMARY_SYSTEM: "summary",
                friend_overview.NEXT_SYSTEM: "next", friend_chat.CHAT_SYSTEM: "chat",
                **{friend_chat.repair_system({"synthetic": fields}): "chat"
                   for count in range(1, 5)
                   for fields in itertools.combinations(("intent", "emotion", "response", "invite"), count)},
                friend_persona.SYNTH_SYSTEM: "synth", friend_persona.CONDENSE_SYSTEM: "condense",
                friend_persona.AUDIT_SYSTEM: "audit",
                **{system: "evidence" for system in friend_persona.EVIDENCE_SYSTEMS}}[system]

    def generate(self, config, request, on_event, cancel_event):
        kind = self.kind(request["system"])
        with self.lock:
            self.calls.append((kind, request["message"]))
        self.entered.setdefault(kind, threading.Event()).set()
        gate = self.gate.get(kind)
        while gate is not None and not gate.wait(0.01):
            if cancel_event.is_set():
                raise RuntimeError("cancelled")
        if kind == "evidence":
            text = self.answer_evidence(request["system"], request["message"])
        else:
            text = getattr(self, "answer_" + kind)(request["message"])
        return {"text": json.dumps(text, ensure_ascii=False), "usage": {"inputTokens": 100, "outputTokens": 10}}

    def count(self, kind):
        return sum(1 for item, _message in self.calls if item == kind)

    @staticmethod
    def answer_extract(message):
        return {"memories": []}

    @staticmethod
    def answer_dedup(message):
        return {"results": []}

    @staticmethod
    def answer_summary(message):
        return {"summary": "最近聊了周末爬山的安排，对方说最近工作有点忙。", "evidence": [1]}

    @staticmethod
    def answer_next(message):
        return {"next": "可以问问对方周末是否方便一起爬山。"}

    @staticmethod
    def answer_chat(message):
        blocks = []
        inviting = {int(match.group(1)) for match in re.finditer(
            r"^B(\d+) [^\n]*【(?:需标注|仅供参考)】\n((?:  .*\n?)*)", message, re.M)
            if "要不要" in match.group(2)}
        for match in re.finditer(r"^B(\d+) (我|对方) [^\n]*?(（回应 B\d+[^）]*）)?【需标注】\n((?:  .*\n?)*)", message, re.M):
            body = match.group(4)
            intent = "邀请" if "要不要" in body else "提问" if "？" in body else "分享"
            item = {"b": int(match.group(1)), "intent": intent, "emotion": "平静", "response": None, "invite": None}
            if match.group(3):
                item["response"] = "接住并展开" if "好呀" in body else "简短回应"
                asked = int(re.search(r"回应 B(\d+)", match.group(3)).group(1))
                if asked in inviting:
                    # The synthetic provider must answer explicitly too; do not
                    # depend on the old production fallback for missing invites.
                    item["invite"] = "明确接受" if "好呀" in body else "无法判断"
            blocks.append(item)
        return {"blocks": blocks}

    @staticmethod
    def answer_evidence(system, message):
        items = []
        for match in re.finditer(r"^\[(\d+)\] \S+ \S+ 对方: (.*)$", message, re.M):
            line = match.group(2)
            if "big5_a " in system and "我直说" in line:
                items.append({"dim": "big5_a", "lean": "低", "note": "对方直接说出想法",
                              "msgs": [int(match.group(1))], "topic": None, "value": None})
            if "self_mbti" in system and "我是INFP" in line:
                items.append({"dim": "self_mbti", "lean": None, "note": "对方自述 MBTI", "msgs": [int(match.group(1))],
                              "topic": None, "value": "INFP"})
            if "love " in system and "我希望关系里能长期相互支持" in line:
                items.extend([{"dim": "love", "lean": "selfReport", "note": "明确谈到亲密关系的期待",
                               "msgs": [int(match.group(1))], "value": "我希望关系里能长期相互支持"},
                              {"dim": "love", "lean": "support", "note": "表达长期相互支持的期待",
                               "msgs": [int(match.group(1))], "value": None}])
            if "love " in system and "我看重长期稳定的伴侣关系" in line:
                items.append({"dim": "love", "lean": "support", "note": "再次强调长期稳定的关系",
                              "msgs": [int(match.group(1))], "value": None})
            if "cog_need " in system and "搞明白" in line:
                items.append({"dim": "cog_need", "lean": "高", "note": "对方想弄懂原理",
                              "msgs": [int(match.group(1))], "topic": None, "value": None})
            if "optimism " in system and "肯定能成" in line:
                items.append({"dim": "optimism", "lean": "乐观", "note": "对方预期结果会好",
                              "msgs": [int(match.group(1))], "topic": None, "value": None})
            if "time_focus " in system and "五年规划" in line:
                items.append({"dim": "time_focus", "lean": "未来", "note": "对方做长期打算",
                              "msgs": [int(match.group(1))], "topic": None, "value": None})
            if "direct " in system and "有话直说" in line:
                items.append({"dim": "direct", "lean": "低语境", "note": "对方有话直说",
                              "msgs": [int(match.group(1))], "topic": None, "value": None})
            if "moral_scope " in system and "陌生人和动物" in line:
                items.append({"dim": "moral_scope", "lean": "宽", "note": "对方关心陌生人",
                              "msgs": [int(match.group(1))], "topic": None, "value": None})
            if "humor" in system and "自嘲" in line:
                items.append({"dim": "humor", "lean": None, "note": "对方拿自己开涮",
                              "msgs": [int(match.group(1))], "topic": None, "value": "自嘲型"})
            if "conflict" in system and "各让一步" in line:
                items.append({"dim": "conflict", "lean": None, "note": "对方提议各让一步",
                              "msgs": [int(match.group(1))], "topic": None, "value": "妥协"})
        return {"evidence": items}

    @staticmethod
    def answer_synth(message):
        dims = []
        for block in re.split(r"^## ", message, flags=re.M)[1:]:
            dim = block.split()[0]
            labels = re.findall(r"^\s*(e\d+) ", block, re.M)
            if labels:
                dims.append({"id": dim, "label": "看重长期支持" if dim == "love" else "偏低",
                             "detail": "多次表达对稳定伴侣关系的期待。" if dim == "love" else "多次直接说出想法。",
                             "confidence": "较有把握",
                             "support": labels, "counter": [], "periods": None})
        return {"dimensions": dims}

    @staticmethod
    def answer_condense(message):
        return {"summary": "多次直接表达。", "keep": re.findall(r"^(e\d+) ", message, re.M)[:4]}

    @staticmethod
    def answer_audit(message):
        return {"verdicts": [{"id": dim, "verdict": "pass", "reason": "有原文"}
                             for dim in re.findall(r"^## (\S+)", message, re.M)]}


def msg(seq, side, text, time_ms=None):
    return make_message(seq, side=side, text=text, sender="self" if side == "self" else USER,
                        name="我" if side == "self" else "朋友",
                        time_ms=time_ms if time_ms is not None else 1_700_000_000_000 + seq * 60_000)


def dimension(persona, key):
    return next(item for item in persona["dimensions"] if item["id"] == key)


class AnalysisCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = WindowSource(account=ACCOUNT)
        base = 1_700_000_000_000
        rows = []
        for index in range(30):
            stamp = base + index * 5 * DAY
            rows += [msg(index * 4 + 1, "self", "周末要不要去爬山？" if index % 3 == 0 else "最近怎么样？", stamp),
                     msg(index * 4 + 2, "self", "我想听听你的看法", stamp + 1000),
                     msg(index * 4 + 3, "other", "好呀，我直说吧，我很想去" if index % 3 == 0 else "还行，我直说就是有点忙",
                         stamp + 60_000),
                     msg(index * 4 + 4, "other", "我是INFP" if index == 5 else "你呢", stamp + 120_000)]
        self.source.add_messages(USER, rows)
        self.runtime = ScriptedRuntime()
        self.model_store = FakeModelStore(context_tokens=32768)
        self.harness = Harness(self.source, self.model_store, self.runtime, Path(temp.name), ACCOUNT, USER)
        self.addCleanup(self.harness.close)
        self.service = self.harness.service
        self.friends = Friends(self.harness)

    def plugin_database(self):
        return self.harness.accounts.account(ACCOUNT)

    def open_ready(self):
        self.friends.open(ACCOUNT, USER)
        ready = wait_until(lambda: self.friends.overview(ACCOUNT, USER)["history"]["state"] == "ready" and
                           not self.friends.overview(ACCOUNT, USER)["history"]["refreshing"])
        self.assertTrue(ready, "history did not become ready")

    def run_state(self):
        return self.friends.analysis_status(ACCOUNT, USER)["run"]

    def wait_run(self, states=("done", "partial", "stopped", "interrupted", "error")):
        def settled():
            run = self.run_state()
            tasks = self.harness.core.tasks.tasks(account=ACCOUNT, user=USER, states=("queued", "running", "stopping"))
            return run if run and run["state"] in states and not tasks and \
                not self.friends.analysis.active(ACCOUNT, USER) else None
        run = wait_until(settled, timeout=20)
        self.assertIsNotNone(run, "run did not finish")
        return run

    def analyse(self):
        self.open_ready()
        self.friends.analysis_start(ACCOUNT, USER, "req-1", "update")
        return self.wait_run()


class FirstRunTests(AnalysisCase):
    def test_first_open_waits_for_a_click_and_estimates_without_a_model_call(self):
        self.open_ready()
        time.sleep(0.2)
        self.assertIsNone(self.run_state())
        estimate = self.friends.analysis_estimate(ACCOUNT, USER)
        self.assertTrue(estimate["historyReady"])
        self.assertGreater(estimate["full"]["tokens"][1], 0)
        self.assertIsNone(estimate["full"]["seconds"], "no measurement yet must not invent a duration")
        self.assertEqual(self.runtime.calls, [])

    def test_full_run_completes_every_step_and_records_usage(self):
        run = self.analyse()
        self.assertEqual(run["state"], "done", run)
        self.assertEqual([step["key"] for step in run["steps"]],
                         ["history", "recent", "archive", "chat", "persona", "overview"])
        self.assertTrue(all(step["state"] in ("done", "skipped") for step in run["steps"]), run["steps"])
        self.assertGreater(run["usage"]["inputTokens"], 0)
        self.assertFalse(run["usage"]["estimated"])
        overview = self.friends.page(ACCOUNT, USER, "overview")
        self.assertIn("爬山", overview["summary"]["summary"])
        self.assertEqual(overview["next"]["next"], "可以问问对方周末是否方便一起爬山。")
        persona = self.friends.page(ACCOUNT, USER, "persona")["result"]
        agreeable = dimension(persona, "big5_a")
        self.assertEqual((agreeable["label"], agreeable["confidence"]), ("偏低", "较有把握"))
        self.assertLess(agreeable["position"], 0, "every lean is toward the low end")
        self.assertGreater(agreeable["position"], -1, "the count is shrunk toward the middle")
        self.assertIsNone(dimension(persona, "big5_e")["position"], "no evidence, no point")
        self.assertEqual(persona["selfReports"]["mbti"]["value"], "INFP")
        self.assertEqual([item["id"] for item in persona["values"]], list(friend_persona.VALUE_DIMS))
        self.assertTrue(all(item["selfReport"] is None and item["inference"]["label"] is None
                            for item in persona["values"]), "no values evidence still produces five empty cards")
        chat = self.friends.page(ACCOUNT, USER, "chat")
        invite = next(group for group in chat["statistics"]["responses"] if group["ask"] == "邀请")
        self.assertEqual(invite["rounds"], 10, "two of my messages plus the answer count as one round")
        self.assertEqual(chat["reply"]["replied"], chat["reply"]["eligible"])

    def test_reopen_without_new_messages_calls_no_model(self):
        first = self.analyse()
        calls = len(self.runtime.calls)
        estimate = self.friends.analysis_estimate(ACCOUNT, USER)
        self.assertEqual(estimate["full"]["tokens"], [0, 0], "nothing left to do costs nothing")
        self.open_ready()
        time.sleep(0.3)
        self.wait_run()
        self.assertEqual(len(self.runtime.calls), calls)
        self.assertEqual(self.run_state()["id"], first["id"], "an empty update does not start another run")

    def test_v2_persona_is_marked_stale_and_reextracts_life_views(self):
        self.analyse()
        store = FriendStore(self.plugin_database())
        old = self.friends.page(ACCOUNT, USER, "persona")["result"]
        old.pop("values")
        def downgrade(connection):
            connection.execute("UPDATE friend_scans SET version=? WHERE user=? AND step='persona' AND published=1",
                               ("friend-persona-v2", USER))
            connection.execute("UPDATE friend_results SET payload=?,deps=? WHERE user=? AND page='persona'",
                               (json.dumps(old), json.dumps({"v": "friend-persona-v2"}), USER))
        store.write(downgrade)
        self.assertTrue(self.friends.page(ACCOUNT, USER, "persona")["stale"])
        before = self.runtime.count("evidence")
        self.assertGreater(self.friends.analysis_estimate(ACCOUNT, USER)["full"]["tokens"][1], 0)
        self.friends.analysis_start(ACCOUNT, USER, None, "update")
        self.assertEqual(self.wait_run()["state"], "done")
        self.assertGreater(self.runtime.count("evidence"), before)
        self.assertEqual(len(self.friends.page(ACCOUNT, USER, "persona")["result"]["values"]), 5)

    def test_reopen_with_new_messages_runs_incrementally(self):
        self.analyse()
        calls = len(self.runtime.calls)
        later = 1_700_000_000_000 + 200 * DAY
        self.source.add_messages(USER, [msg(500, "self", "最近在忙什么？", later), msg(501, "other", "在学吉他", later + 60_000),
                                        msg(502, "self", "厉害", later + 120_000)])
        self.open_ready()
        run = wait_until(lambda: self.run_state() if self.run_state()["trigger"] == "auto" else None)
        self.assertIsNotNone(run)
        run = self.wait_run()
        self.assertEqual(run["state"], "done")
        chat_prompts = [message for kind, message in self.runtime.calls[calls:] if kind == "chat"]
        self.assertTrue(chat_prompts)
        self.assertNotIn("我想听听你的看法\n  对方", chat_prompts[0][:200])
        self.assertTrue(all("在学吉他" in prompt or "最近在忙什么" in prompt for prompt in chat_prompts))

    def test_changing_the_goal_regenerates_only_the_next_step(self):
        self.analyse()
        before = {kind: self.runtime.count(kind) for kind in ("summary", "next", "chat", "evidence", "synth")}
        self.friends.save_settings(ACCOUNT, USER, {"kind": "goal", "text": "多约出来见面", "expectedVersion": 0})
        run = wait_until(lambda: self.run_state() if self.run_state()["trigger"] == "auto" else None)
        self.assertIsNotNone(run, "a changed goal re-checks dependencies under the automatic threshold")
        self.assertEqual(self.wait_run()["state"], "done")
        self.assertEqual(self.runtime.count("next"), before["next"] + 1)
        for kind in ("summary", "chat", "evidence", "synth"):
            self.assertEqual(self.runtime.count(kind), before[kind], kind)


class CancelTests(AnalysisCase):
    def test_cancel_keeps_results_and_waits_for_continue(self):
        self.open_ready()
        self.runtime.gate["chat"] = threading.Event()
        run = self.friends.analysis_start(ACCOUNT, USER, None, "update")["run"]
        self.assertTrue(wait_until(lambda: self.runtime.entered.get("chat") and self.runtime.entered["chat"].is_set()))
        self.friends.analysis_cancel(ACCOUNT, USER, run["id"])
        stopped = self.wait_run()
        self.assertEqual(stopped["state"], "stopped")
        self.runtime.gate.pop("chat").set()
        self.assertIsNotNone(self.friends.page(ACCOUNT, USER, "overview")["summary"],
                             "the recent summary published before the cancel stays")
        calls = len(self.runtime.calls)
        self.open_ready()
        time.sleep(0.3)
        self.assertEqual(self.run_state()["state"], "stopped", "reopening never continues by itself")
        self.assertEqual(len(self.runtime.calls), calls)
        self.friends.analysis_resume(ACCOUNT, USER, run["id"])
        self.assertEqual(self.wait_run()["state"], "done")
        self.assertEqual(self.runtime.count("summary"), 1, "unchanged recent inputs are not summarised again")


class ManualObservationTests(AnalysisCase):
    def test_editing_my_correction_never_triggers_persona_work(self):
        self.analyse()
        persona_calls = self.runtime.count("synth") + self.runtime.count("audit")
        self.friends.save_observation(ACCOUNT, USER, {"judgement": "其实很委婉", "note": "", "dimension": "big5_a"})
        self.open_ready()
        time.sleep(0.3)
        self.wait_run()
        self.assertEqual(self.runtime.count("synth") + self.runtime.count("audit"), persona_calls)
        page = self.friends.page(ACCOUNT, USER, "persona")
        self.assertFalse(page["stale"])
        self.assertEqual(page["observations"][0]["dimension"], "big5_a")

    def test_value_observation_is_kept_separate_from_ai_result(self):
        self.analyse()
        before = self.friends.page(ACCOUNT, USER, "persona")["result"]["values"]
        self.friends.save_observation(ACCOUNT, USER, {"judgement": "我觉得对方很看重家庭", "note": "我的观察", "dimension": "family"})
        page = self.friends.page(ACCOUNT, USER, "persona")
        self.assertEqual(page["observations"][0]["dimension"], "family")
        self.assertEqual(page["result"]["values"], before)


class ValueAnalysisTests(AnalysisCase):
    def test_value_self_report_and_audited_inference_keep_their_sources(self):
        later = 1_700_000_000_000 + 200 * DAY
        self.source.add_messages(USER, [msg(500, "other", "我希望关系里能长期相互支持。", later),
                                        msg(501, "other", "我看重长期稳定的伴侣关系。", later + DAY)])
        self.assertEqual(self.analyse()["state"], "done")
        page = self.friends.page(ACCOUNT, USER, "persona")
        love = next(item for item in page["result"]["values"] if item["id"] == "love")
        self.assertEqual(love["selfReport"]["text"], "我希望关系里能长期相互支持。")
        self.assertEqual(love["selfReport"]["statements"][0]["text"], love["selfReport"]["text"])
        self.assertEqual(love["inference"]["label"], "看重长期支持")
        self.assertEqual(love["inference"]["confidence"], "有一定依据")
        self.assertEqual(len(love["inference"]["support"]), 2)
        ids = love["selfReport"]["evidence"] + love["inference"]["support"]
        cited = self.friends.page(ACCOUNT, USER, "persona-evidence", ids)["items"]
        self.assertEqual(len(cited), len(set(ids)))
        self.assertTrue(all(item["status"] == "ok" for item in cited))


class ReviewRegressionTests(AnalysisCase):
    def gated_start(self, kind, mode="update"):
        self.runtime.entered.setdefault(kind, threading.Event()).clear()
        self.runtime.gate[kind] = threading.Event()
        run = self.friends.analysis_start(ACCOUNT, USER, None, mode)["run"]
        self.assertTrue(wait_until(lambda: self.runtime.entered[kind].is_set()))
        return run

    def test_changed_input_rejects_pending_summary_even_without_an_epoch_change(self):
        self.open_ready()
        self.gated_start("summary")
        database = self.service.stores.account(ACCOUNT)
        epoch = database.context(USER)["epoch"]
        target = self.source.users[USER][-8]["id"]
        FriendStore(database).write(lambda connection: connection.execute(
            "UPDATE context_messages SET text='synthetic replacement' WHERE user=? AND msg_id=?", (USER, target)))
        self.assertEqual(database.context(USER)["epoch"], epoch)
        self.runtime.gate.pop("summary").set()
        self.assertEqual(self.wait_run()["state"], "interrupted")
        self.assertIsNone(self.friends.page(ACCOUNT, USER, "overview")["summary"])

    def test_reset_input_rejects_pending_summary(self):
        self.open_ready()
        self.gated_start("summary")
        database = self.service.stores.account(ACCOUNT)
        epoch = database.context(USER)["epoch"]
        self.source.users[USER][-8]["text"] = "synthetic replacement"
        self.service.context.observe_window(ACCOUNT, self.source.workdir, USER, self.source.users[USER], False)
        self.assertNotEqual(database.context(USER)["epoch"], epoch)
        self.runtime.gate.pop("summary").set()
        self.assertEqual(self.wait_run()["state"], "interrupted")
        self.assertIsNone(self.friends.page(ACCOUNT, USER, "overview")["summary"])

    def test_append_during_summary_keeps_valid_work_and_refreshes_recent_result(self):
        self.open_ready()
        self.gated_start("summary")
        database = self.service.stores.account(ACCOUNT)
        epoch = database.context(USER)["epoch"]
        self.source.add_messages(USER, [msg(500, "self", "synthetic new message", 1_700_000_000_000 + 200 * DAY)])
        self.service.context.observe_window(ACCOUNT, self.source.workdir, USER, self.source.users[USER], False)
        self.assertEqual(database.context(USER)["epoch"], epoch)
        self.runtime.gate.pop("summary").set()
        self.assertEqual(self.wait_run()["state"], "done")
        self.assertEqual(self.friends.page(ACCOUNT, USER, "overview")["summary"]["lastMsgId"],
                         self.source.users[USER][-1]["id"])

    def test_recalled_text_marks_existing_summary_stale_without_changing_last_id(self):
        self.analyse()
        database = self.service.stores.account(ACCOUNT)
        target = self.source.users[USER][-8]["id"]
        FriendStore(database).write(lambda connection: connection.execute(
            "UPDATE context_messages SET text='synthetic replacement' WHERE user=? AND msg_id=?", (USER, target)))
        self.assertTrue(self.friends.page(ACCOUNT, USER, "overview")["stale"])

    def test_window_cutting_one_long_session_marks_summary_partial_and_current(self):
        self.model_store.context_tokens = 131072
        self.source.users[USER] = [msg(index, "self" if index % 2 else "other", "synthetic short text")
                                   for index in range(1, 501)]
        self.assertEqual(self.analyse()["state"], "done")
        page = self.friends.page(ACCOUNT, USER, "overview")
        self.assertEqual(page["summary"]["messages"], 400)
        self.assertFalse(page["summary"]["complete"], "the recent session starts before the retained window")
        self.assertFalse(page["stale"], "page and analysis compare the same partial recent range")

    def test_model_switch_rejects_pending_analysis_and_changes_status_fingerprint(self):
        self.open_ready()
        initial = self.friends.analysis_status(ACCOUNT, USER)["configFingerprint"]
        self.gated_start("summary")
        self.model_store.context_tokens = 65536
        changed = self.friends.analysis_status(ACCOUNT, USER)["configFingerprint"]
        self.assertNotEqual(initial, changed)
        self.assertRegex(changed, r"^[a-f0-9]{64}$")
        self.runtime.gate.pop("summary").set()
        self.assertEqual(self.wait_run()["state"], "interrupted")
        self.assertIsNone(self.friends.page(ACCOUNT, USER, "overview")["summary"])

    def test_rebuild_reprocesses_every_step_and_estimates_its_full_cost(self):
        self.analyse()
        kinds = ("summary", "next", "extract", "evidence", "chat", "synth", "audit")
        before = {kind: self.runtime.count(kind) for kind in kinds}
        runs = RunStore(FriendStore(self.plugin_database()), now_ms)
        generations = {step: runs.published_scan(USER, step)["generation"] for step in ("chat", "persona")}
        self.assertEqual(self.friends.analysis_estimate(ACCOUNT, USER)["full"]["tokens"], [0, 0])
        self.assertGreater(self.friends.analysis_estimate(ACCOUNT, USER, "rebuild")["full"]["tokens"][1], 0)
        self.friends.analysis_start(ACCOUNT, USER, None, "rebuild")
        self.assertEqual(self.wait_run()["state"], "done")
        for kind in kinds:
            self.assertGreater(self.runtime.count(kind), before[kind], kind)
        for step, generation in generations.items():
            self.assertGreater(runs.published_scan(USER, step)["generation"], generation)
        job = FriendStore(self.plugin_database()).latest_job(USER)
        self.assertEqual((job["kind"], job["done_messages"]), ("full", 120))

    def test_early_cancelled_rebuild_resumes_all_unfinished_steps(self):
        self.analyse()
        before = {kind: self.runtime.count(kind) for kind in ("extract", "evidence", "chat", "synth", "audit")}
        run = self.gated_start("summary", "rebuild")
        self.friends.analysis_cancel(ACCOUNT, USER, run["id"])
        self.assertEqual(self.wait_run()["state"], "stopped")
        self.runtime.gate.pop("summary").set()
        self.friends.analysis_resume(ACCOUNT, USER, run["id"])
        self.assertEqual(self.wait_run()["state"], "done")
        for kind in before:
            self.assertGreater(self.runtime.count(kind), before[kind], kind)

    def changed_evidence(self):
        answer = self.runtime.answer_evidence
        def revised(system, message):
            result = answer(system, message)
            for item in result["evidence"]:
                item["note"] += "（重做）"
            return result
        self.runtime.answer_evidence = revised

    def preserved_persona(self, old):
        page = self.friends.page(ACCOUNT, USER, "persona")
        self.assertEqual(page["result"], old)
        self.assertTrue(page["stale"], "preserved prior results must indicate the pending rebuild")
        ids = dimension(old, "big5_a")["support"]
        self.assertEqual(len(self.friends.page(ACCOUNT, USER, "persona-evidence", ids)["items"]), len(ids))

    def test_cancelled_rebuild_preserves_old_citations_and_resume_publishes_new_pair(self):
        self.analyse()
        old = self.friends.page(ACCOUNT, USER, "persona")["result"]
        self.changed_evidence()
        run = self.gated_start("synth", "rebuild")
        self.preserved_persona(old)
        self.friends.analysis_cancel(ACCOUNT, USER, run["id"])
        self.assertEqual(self.wait_run()["state"], "stopped")
        self.preserved_persona(old)
        scanned = self.runtime.count("evidence")
        self.runtime.gate.pop("synth").set()
        self.friends.analysis_resume(ACCOUNT, USER, run["id"])
        self.assertEqual(self.wait_run()["state"], "done")
        self.assertEqual(self.runtime.count("evidence"), scanned, "resume uses completed unpublished evidence")
        new = self.friends.page(ACCOUNT, USER, "persona")["result"]
        ids = dimension(new, "big5_a")["support"]
        self.assertNotEqual(ids, dimension(old, "big5_a")["support"])
        self.assertEqual(len(self.friends.page(ACCOUNT, USER, "persona-evidence", ids)["items"]), len(ids))

    def test_failed_rebuild_keeps_old_persona_citations(self):
        self.analyse()
        old = self.friends.page(ACCOUNT, USER, "persona")["result"]
        self.changed_evidence()
        self.runtime.answer_synth = lambda _message: {"invalid": []}
        self.friends.analysis_start(ACCOUNT, USER, None, "rebuild")
        self.assertEqual(self.wait_run()["state"], "partial")
        self.preserved_persona(old)

    def test_model_change_rebuilds_memory_instead_of_reusing_its_old_checkpoint(self):
        self.analyse()
        before = self.runtime.count("extract")
        self.model_store.context_tokens = 65536
        self.friends.analysis_start(ACCOUNT, USER, None, "update")
        self.assertEqual(self.wait_run()["state"], "done")
        self.assertGreater(self.runtime.count("extract"), before)
        job = FriendStore(self.plugin_database()).latest_job(USER)
        self.assertEqual((job["kind"], job["done_messages"]), ("full", 120))


class ExtensionCase(AnalysisCase):
    """Corpus with the new dimensions' markers, so their channel, rule and payload are exercised."""

    MARKERS = (("我觉得这事肯定能成",) * 6 + ("哈哈我这水平也就自嘲一下了",) * 10 + ("我想搞明白这里的原理",) * 3 +
               ("我先做个五年规划",) * 3 + ("我有话直说，别绕弯子",) * 3 + ("算了咱们各让一步吧",) * 2 +
               ("我在乎陌生人和动物的处境",) * 3)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = WindowSource(account=ACCOUNT)
        base = 1_700_000_000_000
        rows = []
        for index in range(30):
            stamp = base + index * 5 * DAY
            rows += [msg(index * 5 + 1, "self", "周末要不要去爬山？", stamp),
                     msg(index * 5 + 2, "other", self.MARKERS[index], stamp + 60_000),
                     msg(index * 5 + 3, "other", "我直说吧，我很想去" if index % 3 == 0 else "你呢", stamp + 120_000)]
            if index == 5:
                rows.append(msg(index * 5 + 4, "other", "我是INFP", stamp + 180_000))
        self.source.add_messages(USER, rows)
        self.runtime = ScriptedRuntime()
        self.model_store = FakeModelStore(context_tokens=32768)
        self.harness = Harness(self.source, self.model_store, self.runtime, Path(temp.name), ACCOUNT, USER)
        self.addCleanup(self.harness.close)
        self.service = self.harness.service
        self.friends = Friends(self.harness)

    def cited_ids(self, persona):
        return [identifier for item in persona["dimensions"] for identifier in item["support"] + item["counter"]]

    def test_channels_cover_every_dim_and_keep_the_base_cues_verbatim(self):
        seen = [dim for channel in friend_persona.CHANNELS
                for dim in channel.dims + channel.type_keys + channel.self_dims + channel.value_dims]
        self.assertEqual(sorted(seen), sorted(friend_persona.EVIDENCE_DIMS), "exactly once")
        for channel in friend_persona.CHANNELS:
            for dim in channel.dims:
                self.assertIn(friend_persona._SLIDER_CUES[dim], channel.system)
            self.assertTrue(all(friend_persona._SLIDER_CUES[dim] not in channel.system
                                for dim in friend_persona.EVIDENCE_DIMS if dim not in channel.dims and
                                dim in friend_persona._SLIDER_CUES), "a dim lives in one channel only")
        self.assertIn("- humor 幽默风格（类型）：", friend_persona.CHANNELS[2].system)

    def test_new_dimensions_get_positions_types_and_channel_prefixed_ids(self):
        run = self.analyse()
        self.assertEqual(run["state"], "done")
        persona = self.friends.page(ACCOUNT, USER, "persona")["result"]
        optimism = dimension(persona, "optimism")
        self.assertEqual(optimism["position"], round(6 / 8, 3), "position=(high-low)/(total+SHRINK)")
        self.assertEqual((optimism["tier"], optimism["confidence"]), ("main", "有一定依据"))
        self.assertEqual(optimism["years"].get("2023", {}).get("high"), 6,
                         "per-year counts are served for the year charts")
        humor = dimension(persona, "humor")
        self.assertEqual((humor["position"], humor["label"]), (None, "自嘲型为主"))
        self.assertEqual(humor["types"], {"亲和型": 0, "自强型": 0, "攻击型": 0, "自嘲型": 10})
        self.assertEqual(humor["dominant"], {"type": "自嘲型", "count": 10})
        conflict = dimension(persona, "conflict")
        self.assertIsNone(conflict["label"], "two items never claim a dominant type")
        self.assertIsNone(conflict["dominant"])
        self.assertEqual(conflict["types"], {"竞争": 0, "合作": 0, "妥协": 2, "回避": 0, "迁就": 0})
        self.assertEqual(dimension(persona, "direct")["position"], round(3 / 5, 3))
        self.assertEqual(dimension(persona, "moral_scope")["position"], round(3 / 5, 3))
        self.assertEqual(persona["selfReports"]["mbti"]["value"], "INFP")
        ids = self.cited_ids(persona)
        self.assertTrue(all(identifier.startswith(("pe_p_", "pe_v_", "pe_m_")) for identifier in ids))
        self.assertEqual(len(ids), len(set(ids)), "channels merge without duplicate ids")
        self.assertEqual(persona["drift"], [], "a first run has nothing to drift against")

    def test_type_evidence_from_two_channels_with_the_same_words_never_collides(self):
        # The base corpus triggers big5_a in the personality channel; the same segment text
        # read by another channel cannot reuse that hash because of the channel prefix.
        rows = [{"seq": 1, "msg_id": "m1", "side": "other", "text": "我直说吧", "kind": "text", "time_ms": 1000,
                 "sort_key": "[1]", "sender_id": "", "sender_name": "", "raw": "{}"}]
        item = {"dim": "big5_a", "lean": "低", "note": "对方直接说出想法", "msgs": [1]}
        ids = {friend_persona.validate_evidence([item], rows, channel=key)[0]["id"]
               for key in ("", "p", "v", "m")}
        self.assertEqual(len(ids), 4)

    def test_audit_reject_falls_back_to_unknown_and_keeps_the_distribution(self):
        self.analyse()

        def rejecting(message):
            dims = re.findall(r"^## (\S+)", message, re.M)
            return {"verdicts": [{"id": dim, "verdict": "reject" if dim == "humor" else "pass", "reason": "越界"}
                                 for dim in dims]}

        self.runtime.answer_audit = rejecting
        self.friends.analysis_start(ACCOUNT, USER, None, "rebuild")
        self.assertEqual(self.wait_run()["state"], "done")
        persona = self.friends.page(ACCOUNT, USER, "persona")["result"]
        humor = dimension(persona, "humor")
        self.assertIsNone(humor["label"])
        self.assertEqual(humor["confidence"], "无法判断")
        self.assertEqual(humor["types"]["自嘲型"], 10, "the distribution survives the rejected inference")
        self.assertEqual(persona["removed"], 1)

    def test_low_confidence_is_tiered_reference_and_drift_is_recorded(self):
        self.analyse()
        old = self.friends.page(ACCOUNT, USER, "persona")["result"]
        self.assertEqual(dimension(old, "big5_a")["tier"], "main")
        answer = self.runtime.answer_evidence

        def revised(system, message):
            result = answer(system, message)
            for item in result["evidence"]:
                if item["dim"] == "big5_a":
                    item["lean"] = "高"
                    item["note"] += "（重做）"
            return result

        self.runtime.answer_evidence = revised
        base_synth = self.runtime.answer_synth

        def synth(message):
            payload = base_synth(message)
            for item in payload["dimensions"]:
                if item["id"] == "big5_a":
                    item["label"] = "偏高"
            return payload

        self.runtime.answer_synth = synth
        self.friends.analysis_start(ACCOUNT, USER, None, "rebuild")
        self.assertEqual(self.wait_run()["state"], "done")
        persona = self.friends.page(ACCOUNT, USER, "persona")["result"]
        moved = dimension(persona, "big5_a")
        self.assertEqual(moved["label"], "偏高")
        self.assertEqual(moved["previous"]["label"], "偏低", "the last conclusion stays beside the new one")
        self.assertEqual(moved["confidence"], "有一定依据", "a conflicting conclusion drops one confidence notch")
        self.assertEqual(moved["position"], round(10 / 12, 3))
        drift = {item["id"]: item for item in persona["drift"]}
        self.assertEqual((drift["big5_a"]["from"], drift["big5_a"]["to"]), (-round(10 / 12, 3), round(10 / 12, 3)))
        self.assertNotIn("previous", dimension(persona, "att_anx"), "untouched dims carry no previous")


class RuleTests(unittest.TestCase):
    def rows(self, items):
        rows = []
        for index, (side, text, stamp) in enumerate(items, 1):
            rows.append({"seq": index, "msg_id": "m%d" % index, "side": side, "text": text, "kind": "text",
                         "time_ms": stamp, "sort_key": json.dumps([index, "s", index]), "sender_id": "",
                         "sender_name": "", "raw": "{}"})
        return rows

    def test_reply_rate_counts_blocks_and_keeps_open_windows_apart(self):
        now = 10 * DAY
        rows = self.rows([("self", "a", 1000), ("self", "b", 2000), ("self", "c", 3000), ("other", "d", 4000),
                          ("self", "e", DAY), ("other", "f", 3 * DAY), ("self", "g", now - 1000)])
        stats = friend_chat.reply_rate(rows, now_ms=now, history_ready=True)
        self.assertEqual((stats["eligible"], stats["replied"], stats["observing"]), (2, 1, 1))

    def test_month_change_compares_complete_months_only_when_history_covers_both(self):
        from datetime import datetime
        import friend_pages
        now = int(datetime(2026, 10, 12, 12).timestamp() * 1000)
        stamp = lambda month, day: int(datetime(2026, month, day, 9).timestamp() * 1000)
        rows = [{"side": "self", "time_ms": stamp(7, 31)}, {"side": "self", "time_ms": stamp(8, 30)},
                {"side": "other", "time_ms": stamp(9, 1)},
                {"side": "self", "time_ms": stamp(9, 1)}, {"side": "other", "time_ms": stamp(9, 20)},
                {"side": "unknown", "time_ms": stamp(9, 21)}, {"side": "self", "time_ms": stamp(10, 2)}]
        self.assertEqual(friend_pages.month_change(rows, now), {"label": "2026 年 9 月", "days": 2, "previousDays": 1})
        self.assertIsNone(friend_pages.month_change(rows[1:], now), "history starting inside the earlier month")
        self.assertIsNone(friend_pages.month_change([], now))

    def test_scan_end_leaves_the_trailing_round_for_later(self):
        rows = self.rows([("other", "a", 1000), ("self", "b", 2000), ("other", "c", 3000)])
        self.assertEqual(friend_chat.scan_end(rows), 1)

    def test_the_extension_never_rewords_a_base_cue(self):
        """提示词回归：the extension catalog must not change one character of a v2 cue."""
        frozen = (
            ("big5_o", "低", "高", "对新想法、新体验、抽象或艺术话题的好奇（高），或偏好熟悉、具体、常规（低）"),
            ("big5_c", "低", "高", "守时、有计划、按约完成、有条理（高），或随性、拖延、临时变动（低）"),
            ("big5_e", "低", "高", "主动发起聊天和聚会、热情、从社交中得到能量（高），或偏好独处、少发起、安静（低）"),
            ("big5_a", "低", "高", "体谅、合作、温和、信任他人（高），或直率批评、争辩、计较（低）"),
            ("big5_n", "低", "高", "焦虑、担忧、情绪起伏、容易受压力影响（高），或平稳、放松（低）"),
            ("att_anx", "低", "高", "在亲近关系里担心被冷落、需要反复确认、对回应快慢敏感（高），或对关系有安全感（低）"),
            ("att_avo", "低", "高", "回避亲密和依赖、不愿谈感受、主动拉开距离（高），或自在地亲近和依靠对方（低）"),
            ("pol_axis", "进步", "保守", "对时事、社会政策等公共议题的立场：支持变革、平等与多元（进步），或维护传统、秩序与现状（保守）；不看个人生活选择"),
            ("pol_ext", "温和", "极端", "谈公共议题时的方式：非黑即白、绝对化、过度自信、贬斥不同意见（极端），或承认复杂、愿意听取异见（温和）"),
            ("pol_econ", "左派", "右派", "对财富与分配的明确观点：重平等、福利、国家投入或约束资本（左派），或重市场、效率、民营与低税（右派）"),
            ("pol_lib", "自由", "威权", "对管制与强制的态度：支持个人选择、言论自由、多元开放（自由），或支持管制、等级、秩序与强制统一（威权）"),
            ("val_change", "求变", "守成", "自己怎么生活：换城市、跳槽、尝鲜、说走就走（求变），或求稳、按部就班、不愿挪窝（守成）；不看对公共议题的立场"),
            ("val_self", "利己", "利他", "看重成就、地位、享乐和个人利益（利己），或关心他人福祉、公平与自然（利他）"),
            ("moral_care", "轻视", "重视", "评判对错时诉诸伤害与关怀"),
            ("moral_fair", "轻视", "重视", "评判对错时诉诸公平、平等、欺骗或按付出分配"),
            ("moral_loyal", "轻视", "重视", "评判对错时诉诸对家人、集体或群体的忠诚与背叛"),
            ("moral_auth", "轻视", "重视", "评判对错时诉诸尊重和服从长辈、权威与规矩"),
            ("moral_sanct", "轻视", "重视", "评判对错时诉诸纯洁、底线、恶心或神圣"),
            ("moral_liberty", "轻视", "重视", "评判对错时诉诸个人自由、反对强迫与控制"),
        )
        self.assertEqual(tuple((key, low, high, cue) for key, _label, low, high, cue in persona_catalog.DIMENSIONS),
                         frozen)
        self.assertIn("把玩笑当攻击", friend_persona.AUDIT_SYSTEM)
        self.assertIn("把单次拖延或一次迟到当成时间观", friend_persona.AUDIT_SYSTEM)

    def test_firm_confidence_needs_time_spread(self):
        evidence = {"e%d" % index: {"id": "x%d" % index, "dim": "big5_a", "msg_ids": ["m%d" % index],
                                    "first_ms": index * 1000} for index in range(3)}
        by_id = {item["id"]: item for item in evidence.values()}
        label_map = {label: item["id"] for label, item in evidence.items()}
        result = friend_persona.validate_synthesis(
            {"dimensions": [{"id": "big5_a", "label": "偏低", "detail": "", "confidence": "较有把握",
                             "support": list(evidence), "counter": [], "periods": None}]},
            ["big5_a"], label_map, by_id)
        self.assertEqual(result["big5_a"]["confidence"], "有一定依据")

    def test_positions_are_counted_from_leans_and_shrunk_toward_the_middle(self):
        evidence = [{"dim": "pol_axis", "lean": lean} for lean in ("low", "low", "low", "high", "mixed")]
        counted = friend_persona.positions(evidence)
        self.assertEqual(counted["pol_axis"]["counts"], {"low": 3, "high": 1, "mixed": 1})
        self.assertEqual(counted["pol_axis"]["position"], round((1 - 3) / 7, 3))
        self.assertIsNone(counted["pol_ext"]["position"])

    def test_extended_dimensions_use_the_same_position_and_shrink_rule(self):
        evidence = [{"dim": "optimism", "lean": "high"} for _ in range(4)]
        counted = friend_persona.positions(evidence)
        self.assertEqual(counted["optimism"]["position"], round(4 / 6, 3), "position=(high-low)/(total+SHRINK)")

    def test_type_evidence_needs_a_vocabulary_value_and_dominant_needs_three_across_dates(self):
        rows = [{"seq": 1, "msg_id": "m1", "side": "other", "text": "x", "kind": "text", "time_ms": 1000,
                 "sort_key": "[1]", "sender_id": "", "sender_name": "", "raw": "{}"}]
        good = {"dim": "humor", "value": "自嘲型", "note": "对方拿自己开涮", "msgs": [1], "lean": None}
        accepted = friend_persona.validate_evidence([good], rows, channel="m")
        self.assertEqual((accepted[0]["value"], accepted[0]["lean"]), ("自嘲型", None))
        self.assertTrue(accepted[0]["id"].startswith("pe_m_"), "channel-prefixed evidence id")
        self.assertEqual(friend_persona.validate_evidence([dict(good, value="胡说型")], rows), [])
        self.assertEqual(friend_persona.validate_evidence([good], rows, channel="m"),
                         friend_persona.validate_evidence([good], rows, channel="m"), "deterministic id")
        self.assertNotEqual(accepted[0]["id"],
                            friend_persona.validate_evidence([good], rows, channel="p")[0]["id"],
                            "different channels never share an id")
        day = 86_400_000
        three = [{"id": "h%d" % i, "dim": "humor", "value": "自嘲型", "first_ms": 1000 + i * 3 * day} for i in range(3)]
        self.assertEqual(friend_persona.dominant_type(three), {"type": "自嘲型", "count": 3})
        self.assertIsNone(friend_persona.dominant_type(three[:2]), "fewer than three items never dominate")
        self.assertIsNone(friend_persona.dominant_type([dict(item, first_ms=1000) for item in three]),
                          "same-day items never dominate")
        self.assertIsNone(friend_persona.dominant_type(three[:2] + [{"id": "h9", "dim": "humor", "value": "亲和型",
                                                                     "first_ms": 5000}]),
                          "a tie has no dominant type")

    def test_type_synthesis_label_is_decided_locally_and_needs_the_rule(self):
        def by_id_for(values):
            return {"x%d" % index: {"id": "x%d" % index, "dim": "humor", "value": value,
                                    "msg_ids": ["m%d" % index], "first_ms": 1000 + index * 3 * 86_400_000}
                    for index, value in enumerate(values)}

        def run(values):
            by_id = by_id_for(values)
            labels = {label: identifier for label, identifier in
                      (("e%d" % (index + 1), item["id"]) for index, item in enumerate(by_id.values()))}
            payload = {"dimensions": [{"id": "humor", "label": "自嘲型为主", "detail": "多次自嘲。",
                                       "confidence": "有一定依据", "support": list(labels), "counter": [],
                                       "periods": None}]}
            return friend_persona.validate_synthesis(payload, ["humor"], labels, by_id)["humor"]

        self.assertEqual(run(("自嘲型",) * 3)["label"], "自嘲型为主", "the rule passes: label from the local count")
        self.assertIsNone(run(("自嘲型", "自嘲型", "亲和型"))["label"], "a tie has no dominant label")

    def test_self_reports_need_the_friend_s_own_unhedged_words(self):
        rows = [{"seq": 1, "msg_id": "m1", "side": "other", "text": "我政治上偏左吧", "kind": "text", "time_ms": 1000,
                 "sort_key": "[1]", "sender_id": "", "sender_name": "", "raw": "{}"}]
        item = {"dim": "self_politics", "lean": None, "note": "对方自述政治位置", "msgs": [1], "value": "保守"}
        accepted = friend_persona.validate_evidence([item], rows)
        self.assertEqual(accepted[0]["value"], "进步", "the value comes from the words, not the model")
        rows[0]["text"] = "朋友说我偏左"
        self.assertEqual(friend_persona.validate_evidence([item], rows), [])
        bad_lean = {"dim": "big5_e", "lean": "外向", "note": "对方主动约人", "msgs": [1]}
        self.assertEqual(friend_persona.validate_evidence([bad_lean], rows), [], "lean must name an end")

    def test_life_view_evidence_needs_literal_first_person_statement(self):
        rows = [{"seq": 1, "msg_id": "m1", "side": "other", "text": "我想以后攒钱买房。", "kind": "text",
                 "time_ms": 1000, "sort_key": "[1]", "sender_id": "", "sender_name": "", "raw": "{}"}]
        item = {"dim": "money", "lean": "selfReport", "note": "对方谈到储蓄计划", "msgs": [1],
                "value": "我想以后攒钱买房"}
        accepted = friend_persona.validate_evidence([item], rows)
        self.assertEqual(accepted[0]["value"], rows[0]["text"])
        support = {**item, "lean": "support", "value": None}
        other = friend_persona.validate_evidence([support], rows)
        self.assertNotEqual(accepted[0]["id"], other[0]["id"], "self-report and support keep distinct citations")
        self.assertEqual(friend_persona.validate_evidence([{**item, "value": "我很富有"}], rows), [])
        rows[0]["text"] = "我妈说我要攒钱买房。"
        self.assertEqual(friend_persona.validate_evidence([{**item, "value": "我妈说我要攒钱买房"}], rows), [])
        rows[0]["text"] = "朋友说我想以后攒钱买房。"
        self.assertEqual(friend_persona.validate_evidence([item], rows), [])
        rows[0]["side"] = "self"
        rows[0]["text"] = "我想以后攒钱买房。"
        self.assertEqual(friend_persona.validate_evidence([item], rows), [])
        rows[0]["side"] = "other"
        rows[0]["text"] = "我想结婚，但现在不想了。"
        truncated = {**item, "value": "我想结婚"}
        accepted = friend_persona.validate_evidence([truncated], rows)[0]
        self.assertEqual(accepted["value"], rows[0]["text"],
                         "a model substring cannot hide a later change of mind")
        self.assertEqual(accepted["excerpt"], rows[0]["text"], "the source viewer must show the same full statement")
        rows[0]["text"] = "我重视存钱，" + "每个月都会认真做预算，" * 22 + "但最近决定先不买房。"
        self.assertEqual(friend_persona.validate_evidence([{**item, "value": "我重视存钱"}], rows)[0]["excerpt"],
                         rows[0]["text"], "a late qualification must remain visible beyond 200 characters")
        rows[0]["text"] = "我以前不想结婚，现在想了。"
        self.assertEqual(friend_persona.validate_evidence([{**item, "value": "我以前不想结婚"}], rows)[0]["value"],
                         rows[0]["text"])
        rows[0]["text"] = "我不是很看重钱。"
        self.assertEqual(friend_persona.validate_evidence([{**item, "value": "我不是很看重钱"}], rows)[0]["value"],
                         rows[0]["text"])

    def test_life_view_reports_keep_recent_distinct_statements(self):
        evidence = [{"id": "v%d" % index, "dim": "family", "lean": "selfReport", "value": value,
                     "first_ms": index * 1000} for index, value in enumerate(
                         ["我想住在老家", "我想住在老家", "我想搬去外地", "我想和家人住得近"])]
        report = friend_persona.value_reports(evidence)["family"]
        self.assertEqual([item["text"] for item in report["statements"]],
                         ["我想和家人住得近", "我想搬去外地", "我想住在老家"])
        self.assertEqual(report["evidence"], ["v3", "v2", "v1", "v0"])

    def test_life_view_self_reports_cannot_count_as_inference_support(self):
        evidence = {"v1": {"id": "v1", "dim": "love", "lean": "selfReport", "msg_ids": ["m1"], "first_ms": 1000},
                    "v2": {"id": "v2", "dim": "love", "lean": "selfReport", "msg_ids": ["m2"], "first_ms": 2000}}
        draft = {"dimensions": [{"id": "love", "label": "重视亲密关系", "detail": "", "confidence": "有一定依据",
                                 "support": ["e1", "e2"], "counter": [], "periods": None}]}
        result = friend_persona.validate_synthesis(draft, ["love"], {"e1": "v1", "e2": "v2"}, evidence)
        self.assertIsNone(result["love"]["label"])


if __name__ == "__main__":
    unittest.main()
