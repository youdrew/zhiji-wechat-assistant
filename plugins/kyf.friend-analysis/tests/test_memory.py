"""Synthetic relationship-memory extraction and plugin-store tests with a scripted model.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Moved from Core's friend_service/friend_store tests with the extraction, 2026-10-13: candidates
reach Core's 关系记忆 through the plugin interface; jobs and runs live in the plugin database.
"""
import re
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_analysis import ACCOUNT, USER, ScriptedRuntime, WindowSource

import friend_extract
import memory
from account import AccountData
from analysis_store import FriendStore, now_ms
from friend_extract import MAX_EVIDENCE
from friend_run_store import RunStore
from harness import Friends, Harness
from test_support import FakeModelStore, make_message, wait_until

class AdvisorRuntimeError(RuntimeError):
    """A model-engine failure with a stable code (the shape the engine client raises)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


RULES = [
    ("急性子", {"text": "对方是急性子（自述）", "tags": ["性格"]}),
    ("香菜", {"text": "对方不吃香菜", "tags": ["偏好与忌口"]}),
    ("爬山吧", {"text": "双方一起去爬山", "tags": ["共同事件"], "sharedEventConfirmed": True}),
    ("喜欢滑雪", {"text": "对方喜欢滑雪", "tags": ["爱好"]}),
    ("电影", {"text": "对方喜欢火星探险", "tags": ["爱好"]}),
    ("吉他", {"text": "对方最近在学吉他", "tags": ["爱好"]}),
    ("没耐心", {"text": "对方说自己没耐心", "tags": ["性格"]}),
]


def message(seq, side, text, **extra):
    item = make_message(seq, side=side, text=text, sender="self" if side == "self" else USER,
                        name="我" if side == "self" else "朋友")
    item.update(extra)
    return item


class MemoryRuntime(ScriptedRuntime):
    """Extraction follows RULES over the numbered lines; deduplication answers with ``self.dedup``."""

    def __init__(self):
        super().__init__()
        self.dedup = lambda batch, records: [{"id": item, "relation": "new", "target": None} for item in batch]
        self.truncate_over = None

    def answer_extract(self, message):
        lines = re.findall(r"^\[(\d+)\] .*?: (.*)$", message, re.M)
        if self.truncate_over is not None and len(lines) > self.truncate_over:
            raise AdvisorRuntimeError("output-truncated")
        return {"memories": [{**item, "evidence": [int(number)], "eventTime": None}
                             for number, text in lines for keyword, item in RULES if keyword in text]}

    def answer_dedup(self, message):
        batch = re.findall(r"^(c\d+) ", message, re.M)
        records = re.findall(r"^((?:m|x):\S+) ", message, re.M)
        return {"results": self.dedup(batch, records)}

    def extract_sizes(self):
        return [len(re.findall(r"^\[\d+\] ", text, re.M)) for kind, text in self.calls if kind == "extract"]


class MemoryCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.source = WindowSource(account=ACCOUNT)
        self.source.add_messages(USER, [
            message(1, "other", "我是急性子，等不了人"), message(2, "self", "哈哈我知道"),
            message(3, "other", "我不吃香菜，一点都不行"), message(4, "self", "周末一起去爬山吧？"),
            message(5, "other", "他说他喜欢滑雪", type="聊天记录"), message(6, "other", "上次那场电影真不错"),
        ] + [message(seq, "self" if seq % 2 else "other", "日常闲聊 %d" % seq) for seq in range(7, 20)])
        self.runtime = MemoryRuntime()
        self.harness = Harness(self.source, FakeModelStore(context_tokens=32768), self.runtime, Path(temp.name),
                               ACCOUNT, USER)
        self.addCleanup(self.harness.close)
        self.core = self.harness.service.friends
        self.friends = Friends(self.harness)

    def open_ready(self):
        self.friends.open(ACCOUNT, USER)
        ready = wait_until(lambda: self.friends.overview(ACCOUNT, USER)["history"]["state"] == "ready" and
                           not self.friends.overview(ACCOUNT, USER)["history"]["refreshing"])
        self.assertTrue(ready, "history did not become ready")

    def run_state(self):
        return self.friends.analysis_status(ACCOUNT, USER)["run"]

    def wait_run(self):
        def settled():
            run = self.run_state()
            tasks = self.harness.core.tasks.tasks(account=ACCOUNT, user=USER, states=("queued", "running", "stopping"))
            return run if run and run["state"] not in ("queued", "running") and not tasks and \
                not self.friends.analysis.active(ACCOUNT, USER) else None
        run = wait_until(settled, timeout=20)
        self.assertIsNotNone(run, "run did not finish")
        return run

    def analyse(self, mode="update"):
        self.open_ready()
        self.friends.analysis_start(ACCOUNT, USER, None, mode)
        return self.wait_run()

    def update_after_new_messages(self, rows):
        """New messages, then reopening the window: a small update starts by itself."""
        previous = self.run_state()["id"]
        self.source.add_messages(USER, rows)
        self.open_ready()
        self.assertTrue(wait_until(lambda: self.run_state()["id"] != previous), "the update did not start")
        return self.wait_run()

    def job(self):
        return FriendStore(self.harness.accounts.account(ACCOUNT)).latest_job(USER)

    def pending(self):
        return {item["text"]: item for item in self.core.memories(ACCOUNT, USER, status="pending")["items"]}

    def decide(self, action, memory_id):
        self.core.memory_action(ACCOUNT, USER, {"action": action, "id": memory_id})
        self.wait_run()


class ExtractionTests(MemoryCase):
    def test_analysis_proposes_only_supported_friend_statements(self):
        self.open_ready()
        self.assertTrue(self.friends.analysis_estimate(ACCOUNT, USER)["historyReady"])
        self.assertEqual(self.runtime.calls, [], "opening and estimating never call the model")
        self.analyse()
        job = self.job()
        self.assertEqual((job["kind"], job["state"]), ("full", "done"))
        self.assertEqual(job["done_messages"], job["total_messages"])
        self.assertEqual(set(self.pending()), {"对方是急性子（自述）", "对方不吃香菜"})
        detail = self.core.memory_detail(ACCOUNT, USER, self.pending()["对方不吃香菜"]["id"])
        self.assertEqual((detail["evidence"][0]["role"], detail["evidence"][0]["status"]), ("friend", "ok"))
        measured = self.harness.accounts.account(ACCOUNT)._row("SELECT COUNT(*) AS total FROM friend_measurements")
        self.assertGreater(measured["total"], 0, "a finished segment measures the model's pace")

    def test_a_segment_with_many_candidates_is_saved_whole(self):
        # 2026-10-25: one segment yielded 68 candidates; Core then took at most 50 per request and
        # the rejection read as "模型请求格式不正确". Core now takes any number.
        self.source.add_messages(USER, [message(seq, "other", "我收藏了编号%d的邮票" % seq) for seq in range(20, 90)])
        extract = self.runtime.answer_extract
        self.runtime.answer_extract = lambda text: {"memories": extract(text)["memories"] + [
            {"text": "对方收藏了编号%s的邮票" % stamp, "tags": ["爱好"], "evidence": [int(number)], "eventTime": None}
            for number, stamp in re.findall(r"^\[(\d+)\] .*?: 我收藏了编号(\d+)的邮票$", text, re.M)]}
        self.analyse()
        self.assertEqual(self.runtime.extract_sizes(), [89], "the test needs every message in one segment")
        job = self.job()
        self.assertEqual((job["state"], job["error"], job["candidates"]), ("done", None, 72))
        self.assertEqual(self.core.memories(ACCOUNT, USER, status="pending", limit=200)["total"], 72)

    def test_over_cited_candidate_is_trimmed_to_core_evidence_limit(self):
        # 2026-10-25: a plan spread over 13 messages made the model cite all of them and Core
        # rejected the whole batch as "候选证据不正确", stalling every retry at the same segment.
        self.source.add_messages(USER, [message(seq, "other", "我是急性子，等不了人") for seq in range(20, 29)])
        extract = self.runtime.answer_extract
        self.runtime.answer_extract = lambda text: {"memories": extract(text)["memories"] + [
            {"text": "对方是急性子（自述）", "tags": ["性格"],
             "evidence": [int(number) for number, _line in
                          re.findall(r"^\[(\d+)\] .*?: (我是急性子.*)$", text, re.M)],
             "eventTime": None}]}
        self.analyse()
        job = self.job()
        self.assertEqual((job["state"], job["error"]), ("done", None))
        detail = self.core.memory_detail(ACCOUNT, USER, self.pending()["对方是急性子（自述）"]["id"])
        self.assertEqual(len(detail["evidence"]), MAX_EVIDENCE)
        self.assertTrue(all(entry["role"] == "friend" for entry in detail["evidence"]))

    def test_evidence_trim_keeps_friend_citations_first(self):
        # Trimming must not strip the friend's own words: a self-report is only accepted with them.
        rows = [{"msg_id": "m%d" % seq, "side": "other" if seq % 2 else "self", "sender_id": "",
                 "kind": "文本", "text": "我是急性子，等不了人 %d" % seq, "raw": "{}",
                 "time_ms": 1_700_000_000_000 + seq * 60_000, "sort_key": None} for seq in range(1, 13)]
        items = [{"text": "对方是急性子（自述）", "tags": ["性格"], "evidence": list(range(1, 13)),
                  "eventTime": None}]
        candidate = friend_extract.validate_candidates(items, rows, USER, "fp")[0]
        self.assertEqual(len(candidate["evidence"]), MAX_EVIDENCE)
        self.assertEqual([entry["role"] for entry in candidate["evidence"]], ["friend"] * 6 + ["self"] * 2)

    def test_incremental_update_reads_only_new_messages_and_skips_duplicates(self):
        self.analyse()
        first = self.pending()
        self.decide("confirm", first["对方不吃香菜"]["id"])
        self.decide("ignore", first["对方是急性子（自述）"]["id"])
        calls = len(self.runtime.calls)
        self.update_after_new_messages([message(20, "other", "我最近在学吉他"), message(21, "other", "真的不吃香菜"),
                                        message(22, "other", "我就是急性子")])
        self.assertEqual(self.job()["kind"], "incremental")
        prompt = [text for kind, text in self.runtime.calls[calls:] if kind == "extract"][0]
        self.assertNotIn("一点都不行", prompt)
        self.assertIn("学吉他", prompt)
        self.assertEqual(set(self.pending()), {"对方最近在学吉他"})
        links = self.harness.service.stores.account(ACCOUNT)._rows("SELECT relation,target_kind FROM memory_dedup")
        self.assertEqual(sorted((row["relation"], row["target_kind"]) for row in links),
                         [("duplicate", "exclusion"), ("duplicate", "memory")])

    def test_semantic_duplicates_and_uncertain_matches(self):
        self.analyse()
        self.decide("confirm", self.pending()["对方是急性子（自述）"]["id"])
        self.runtime.dedup = lambda batch, records: [{"id": item, "relation": "duplicate", "target": records[0]}
                                                     for item in batch]
        self.update_after_new_messages([message(20, "other", "我这人没耐心")])
        self.assertNotIn("对方说自己没耐心", self.pending())
        self.runtime.dedup = lambda batch, records: [{"id": item, "relation": "uncertain", "target": None}
                                                     for item in batch]
        self.update_after_new_messages([message(21, "other", "说真的我很没耐心")])
        self.assertEqual(self.pending()["对方说自己没耐心"]["relation"], "uncertain")

    def test_cancel_and_continue_reuse_the_memory_checkpoint(self):
        self.open_ready()
        self.runtime.gate["extract"] = threading.Event()
        run = self.friends.analysis_start(ACCOUNT, USER, None, "update")["run"]
        self.assertTrue(wait_until(lambda: self.runtime.entered.get("extract") and self.runtime.entered["extract"].is_set()))
        self.friends.analysis_cancel(ACCOUNT, USER, run["id"])
        self.runtime.gate.pop("extract").set()
        self.assertEqual(self.wait_run()["state"], "stopped")
        self.assertEqual(self.pending(), {})
        self.open_ready()
        self.assertEqual(self.wait_run()["state"], "stopped", "reopening never bypasses a stopped run")
        self.friends.analysis_resume(ACCOUNT, USER, run["id"])
        self.assertEqual(self.wait_run()["state"], "done")
        self.assertEqual(len(self.pending()), 2)
        self.assertEqual(self.job()["state"], "done")

    def test_clear_during_extraction_discards_the_late_batch(self):
        self.open_ready()
        self.runtime.gate["extract"] = threading.Event()
        self.friends.analysis_start(ACCOUNT, USER, None, "update")
        self.assertTrue(wait_until(lambda: self.runtime.entered.get("extract") and self.runtime.entered["extract"].is_set()))
        self.core.memory_action(ACCOUNT, USER, {"action": "clear"})
        self.runtime.gate.pop("extract").set()
        self.wait_run()
        self.assertNotEqual(self.job()["state"], "done")
        self.assertEqual(self.core.memories(ACCOUNT, USER)["total"], 0, "Core rejects a batch read before the clear")
        self.friends.analysis_start(ACCOUNT, USER, None, "update")
        self.wait_run()
        self.assertEqual((self.job()["kind"], self.job()["state"]), ("full", "done"), "extraction starts over")
        self.assertEqual(len(self.pending()), 2)

    def test_truncated_segment_is_split_instead_of_resent(self):
        self.runtime.truncate_over = 10
        self.analyse()
        job = self.job()
        self.assertEqual(job["state"], "done", job)
        sizes = self.runtime.extract_sizes()
        self.assertEqual(sizes[0], 19)
        self.assertEqual(sum(sizes[1:]), 19, "the two halves cover the segment exactly once")
        self.assertEqual(len(sizes), 3)
        self.assertEqual((job["done_segments"], job["total_segments"], job["attempts"]), (2, 2, 0))
        self.assertEqual(job["done_messages"], job["total_messages"])
        self.assertEqual(set(self.pending()), {"对方是急性子（自述）", "对方不吃香菜"})

    def test_truncation_after_the_split_limit_fails_with_saved_progress(self):
        self.runtime.truncate_over = 0
        with mock.patch.object(memory, "RETRY_DELAYS", (0.0, 0.0)):
            self.analyse()
        job = self.job()
        self.assertEqual(job["state"], "error")
        self.assertEqual(job["error"], "模型输出过长被截断，可以继续")
        self.assertEqual(len(self.runtime.extract_sizes()), 2 + memory.SEGMENT_RETRIES + 1,
                         "two halvings, then the smallest piece only gets the normal retries")
        self.assertEqual(job["done_messages"], 0)
        self.assertEqual(self.pending(), {})


class StoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.plugin = SimpleNamespace(data_dir=Path(temp.name))

    def open(self):
        database = AccountData(self.plugin, "synthetic-account")
        self.addCleanup(database.close)
        return database

    def test_request_ids_and_restart_marks_running_jobs_interrupted(self):
        database = self.open()
        fields = {"source_fp": "fp", "context_epoch": "e", "extract_version": "v", "model_fp": "m", "clear_epoch": 0,
                  "upto_seq": 1, "start_count": 0, "total_messages": 1, "done_messages": 0, "total_segments": 1}
        first = FriendStore(database).create_job(USER, "full", "req-1", fields)
        self.assertEqual(FriendStore(database).create_job(USER, "full", "req-1", fields)["id"], first["id"])
        database.close()
        self.assertEqual(FriendStore(self.open()).job(first["id"])["state"], "interrupted")

    def test_copied_legacy_runs_gain_their_mode_and_keep_results(self):
        """Tables copied from a v9 advisor.sqlite3 have no run mode yet; opening adds it."""
        database = self.open()
        runs = RunStore(FriendStore(database), now_ms)
        run = runs.create_run(USER, "manual", None, None, [])
        runs.set_run(run["id"], state="done")
        runs.save_result(USER, "summary", {"summary": "synthetic preserved summary"}, {}, None, lambda _conn: None)
        open_run = runs.create_run(USER, "manual", None, None, [], "rebuild")
        path = database.path
        database.close()
        connection = sqlite3.connect(path)
        connection.execute("ALTER TABLE friend_runs DROP COLUMN mode")
        connection.execute("UPDATE friend_runs SET state='running' WHERE id=?", (open_run["id"],))
        connection.commit()
        connection.close()
        runs = RunStore(FriendStore(self.open()), now_ms)
        self.assertEqual(runs.run(run["id"])["mode"], "update")
        self.assertEqual(runs.result(USER, "summary")["payload"]["summary"], "synthetic preserved summary")
        self.assertEqual(runs.run(open_run["id"])["state"], "interrupted")
        self.assertEqual(runs.run(run["id"])["state"], "done")


if __name__ == "__main__":
    unittest.main()
