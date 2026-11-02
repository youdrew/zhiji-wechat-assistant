"""Synthetic-only indexing/retrieval tests. No local WeChat or model service is opened.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import copy
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend")]
from self_contracts import SelfError
from self_samples import extract
from self_service import Service
from self_store import Store

BASE = 1_700_000_000_000


def message(n, side, text, time=None, **extra):
    row = {"id": "m" + str(n), "seq": n, "side": side, "text": text, "kind": "text",
           "time": BASE + n * 1000 if time is None else time, "raw": "{}", "type": "文本",
           "senderId": "synthetic-account" if side == "self" else "", **extra}
    row["hash"] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    return row


def dialogue(count=3, topic="周末一起吃饭", prefix="好呀"):
    return [row for n in range(count) for row in (message(n * 2 + 1, "other", topic + str(n)),
                                                 message(n * 2 + 2, "self", prefix + str(n)))]


class Core:
    def __init__(self, live=None):
        self.account = "synthetic-account"
        self.source = "synthetic-source"
        self.live = live or {"friend-a": dialogue(), "not-selected-friend": dialogue(prefix="行呀")}
        self.projected = {}
        self.epochs = {}
        self.fail = set()
        self.pages = []
        self.verifies = []
        self.rebuilds = []
        self.hook = None
        self.version = 1
        self.positioned = False  # report directory ``latest`` and status ``highwater`` like Core
        self.prepares = []

    def scope(self):
        return {"account": self.account}

    def get(self, path, **query):
        """Plugin settings as Core serves them (manifest defaults unless a test changes them); no external chats."""
        if path == "external/friends":
            return {"friends": []}
        assert path == "settings", path
        values = getattr(self, "plugin_settings", None) or {"analysisDepth": "deep", "replyDepth": "standard", "analysisGrain": "6"}
        return {"values": dict(values), "revision": 1}

    def self_profile(self, account):
        return {"style": "自然简短", "background": "不应进入风格上下文的背景", "version": self.version}

    def history_conversations(self, account, after=None, limit=500):
        users = sorted(self.live)
        start = users.index(after) + 1 if after else 0
        page = users[start:start + 1]  # exercise real directory pagination
        return {"account": account, "sourceFingerprint": self.source,
                "conversations": [{"user": user, "name": "合成朋友" + user, "isGroup": False,
                                   **({"latest": self.position(self.live[user])} if self.positioned else {})}
                                  for user in page],
                "next": page[-1] if start + len(page) < len(users) else None, "coverage": {"state": "complete"}}

    @staticmethod
    def position(rows):
        return json.dumps(["synthetic-shard", rows[-1]["id"]]) if rows else None

    def history_prepare(self, account, user, refresh=False, rebuild=False):
        self.prepares.append(user)
        if user in self.fail:
            raise RuntimeError("synthetic failure with PRIVATE_TEXT must not be persisted")
        if rebuild or user not in self.projected:
            self.projected[user] = copy.deepcopy(self.live[user])
            self.epochs[user] = self.epochs.get(user, 0) + 1
            if rebuild:
                self.rebuilds.append(user)
        else:
            known = {row["id"] for row in self.projected[user]}
            self.projected[user] += copy.deepcopy([row for row in self.live[user] if row["id"] not in known])
        return self.history_status(account, user)

    def history_status(self, account, user):
        rows = self.projected.get(user, [])
        return {"state": "ready", "sourceFingerprint": self.source, "currentSourceFingerprint": self.source,
                "epoch": str(self.epochs.get(user, 0)), "revision": len(rows), "readCount": len(rows),
                "totalCount": len(rows), "watermark": {"seq": rows[-1]["seq"]} if rows else None,
                **({"highwater": self.position(rows)} if self.positioned else {})}

    def history_messages(self, account, user, after=None, limit=500):
        self.pages.append((user, after))
        if self.hook:
            self.hook(user, after)
        rows = copy.deepcopy([row for row in self.projected[user] if row["seq"] > (after or 0)])
        return {"messages": rows[:limit], "next": rows[limit - 1]["seq"] if len(rows) > limit else None,
                "sourceFingerprint": self.source, "epoch": str(self.epochs[user])}

    def post(self, path, body):
        assert path == "history/verify"
        self.verifies.append(body)
        live = {row["id"]: row for row in self.live.get(body["user"], [])}
        return {"results": [{"id": item["id"], "state": "missing" if item["id"] not in live else
                             "same" if live[item["id"]]["hash"] == item["hash"] else "changed"}
                            for item in body["items"]]}


class BatchCore(Core):
    """Answers Core's history/verify-many with the same per-message rule."""

    def __init__(self, live=None):
        super().__init__(live)
        self.batches = []
        self.unready = set()

    def history_verify_many(self, account, groups):
        self.batches.append(copy.deepcopy(groups))
        results = []
        for group in groups:
            checked = Core.post(self, "history/verify", {"account": account, "user": group["user"], "fresh": True,
                                                        "items": group["items"]})["results"]
            if group["user"] in self.unready:
                checked = [{**item, "state": "unready"} for item in checked]
            results.append({"user": group["user"], "results": checked})
        return {"results": results}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.core = Core()
        self.service = Service(SimpleNamespace(core=self.core, data_dir=Path(self.temp.name)))
        self.account = self.core.account

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def build(self, mode="incremental"):
        self.service.build(self.account, mode)
        store, worker = self.service.accounts[self.account]
        worker.thread.join(5)
        self.assertFalse(worker.thread.is_alive())
        return store, self.service.status(self.account)

    def test_build_all_private_without_model_and_search_across_contacts(self):
        store, status = self.build()
        self.assertEqual(status["build"]["state"], "complete")
        self.assertTrue(status["coverage"]["complete"])
        self.assertEqual(status["coverage"]["indexedConversations"], 2)
        self.assertEqual(status["build"]["sampleCount"], 6)
        result = self.service.search(self.account, {"query": "周末吃饭", "limit": 8}, "friend-a")
        self.assertEqual(len(result["sources"]), 4)
        self.assertTrue(any(not row["sameContact"] for row in result["sources"]))
        self.assertTrue(all("合成朋友" in row["name"] for row in result["sources"]))
        self.assertTrue(all(call["fresh"] for call in self.core.verifies))

    def test_incremental_skips_reprocessing_unchanged_and_upserts_reply_run(self):
        store, _ = self.build()
        first_pages = len(self.core.pages)
        self.build()
        self.assertEqual(first_pages, len(self.core.pages))
        generation = store.meta("generation", 0)
        self.core.live["friend-a"].append(message(7, "self", "我来订位置"))
        self.build()
        self.assertEqual(store.counts()["samples"], 6)
        row = store.rows("SELECT reply FROM samples WHERE user='friend-a' ORDER BY to_seq DESC LIMIT 1")[0]
        self.assertIn("我来订位置", row["reply"])
        self.assertGreater(store.meta("generation"), generation)
        self.assertEqual(self.core.pages[-1], ("friend-a", 6))

    def test_incremental_skips_unchanged_conversations_without_source_calls(self):
        self.core.positioned = True
        store, _ = self.build()
        self.assertEqual(sorted(self.core.prepares), ["friend-a", "not-selected-friend"])
        self.core.prepares.clear()
        self.core.verifies.clear()
        _, status = self.build()
        self.assertEqual(self.core.prepares, [])
        self.assertEqual(self.core.verifies, [])
        self.assertEqual((status["build"]["state"], status["coverage"]["indexedConversations"]), ("complete", 2))
        # A new newest message or a new-message event brings a chat back.
        self.core.live["friend-a"].append(message(7, "self", "我来订位置"))
        store.mark_stale("not-selected-friend")
        self.build()
        self.assertEqual(sorted(self.core.prepares), ["friend-a", "not-selected-friend"])
        row = store.rows("SELECT reply FROM samples WHERE user='friend-a' ORDER BY to_seq DESC LIMIT 1")[0]
        self.assertIn("我来订位置", row["reply"])
        self.core.prepares.clear()
        self.build()
        self.assertEqual(self.core.prepares, [])
        # A chat whose last read failed is retried even though its newest row is unchanged.
        self.core.live["friend-a"].append(message(8, "other", "那就七点"))
        self.core.fail.add("friend-a")
        _, status = self.build()
        self.assertEqual(status["build"]["failedCount"], 1)
        self.core.fail.clear()
        self.core.prepares.clear()
        _, status = self.build()
        self.assertEqual(self.core.prepares, ["friend-a"])
        self.assertEqual(status["build"]["state"], "complete")

    def test_incremental_tails_are_verified_in_bounded_groups(self):
        self.service.close()
        self.core = BatchCore({"friend-%02d" % index: dialogue(1, prefix="好的%d" % index) for index in range(20)})
        self.service = Service(SimpleNamespace(core=self.core, data_dir=Path(self.temp.name)))
        store, _ = self.build()
        self.assertEqual(self.core.batches, [], "a first build has no stored tail to check")
        self.core.live["friend-03"][1] = message(2, "self", "改过的回复")
        self.core.unready.add("friend-07")
        _, status = self.build()
        self.assertEqual([len(groups) for groups in self.core.batches], [16, 4])
        self.assertTrue(all(sum(len(group["items"]) for group in groups) <= 500 for groups in self.core.batches))
        self.assertEqual(self.core.rebuilds, ["friend-03"])
        self.assertEqual(store.rows("SELECT reply FROM samples WHERE user='friend-03'")[0]["reply"], "改过的回复")
        self.assertEqual(status["build"]["failedCount"], 1)
        self.assertEqual(store.conversation("friend-07")["state"], "failed")
        self.assertEqual(store.conversation("friend-08")["state"], "done")

    def test_time_cutoff_excludes_current_and_future_and_deduplicates(self):
        self.build()
        result = self.service.search(self.account, {"query": "周末", "beforeTimeMs": BASE + 5000,
                                                   "beforeSeq": 3, "messageId": "m2"}, "friend-a")
        self.assertTrue(all(row["timeMs"] < BASE + 5000 for row in result["sources"]))
        self.assertFalse(any(row["sameContact"] for row in result["sources"]))

    def test_deleted_edited_and_removed_conversation_are_reconciled(self):
        store, _ = self.build()
        old = self.service.search(self.account, {"query": "周末"})
        self.core.live["friend-a"][1] = message(2, "self", "修改后的本人回复")
        self.core.live["friend-a"] = self.core.live["friend-a"][:2]
        self.build()
        self.assertIn("friend-a", self.core.rebuilds)
        self.assertEqual(len(store.rows("SELECT * FROM samples WHERE user='friend-a'")), 1)
        self.assertEqual(store.rows("SELECT reply FROM samples WHERE user='friend-a'")[0]["reply"], "修改后的本人回复")
        del self.core.live["not-selected-friend"]
        self.build()
        self.assertFalse(store.rows("SELECT * FROM samples WHERE user='not-selected-friend'"))

    def test_retrieval_fresh_check_suppresses_deleted_proof_before_rebuild(self):
        store, _ = self.build()
        self.core.live["friend-a"] = []
        result = self.service.search(self.account, {"query": "周末", "limit": 8})
        self.assertFalse(any(row["user"] == "friend-a" for row in result["sources"]))
        self.assertFalse(store.rows("SELECT * FROM samples WHERE user='friend-a'"))
        self.build()
        self.assertIn("friend-a", self.core.rebuilds)
        self.assertFalse(store.rows("SELECT * FROM samples WHERE user='friend-a'"))

    def test_failure_retry_is_truthful_and_does_not_repeat_done_contacts(self):
        self.core.fail.add("friend-a")
        store, status = self.build()
        self.assertEqual(status["build"]["state"], "partial")
        self.assertEqual(status["build"]["failedCount"], 1)
        self.assertNotIn("PRIVATE_TEXT", json.dumps(store.rows("SELECT * FROM conversations")))
        before = len(self.core.pages)
        self.core.fail.clear()
        _, status = self.build("retry")
        self.assertEqual(status["build"]["state"], "complete")
        self.assertEqual([user for user, _ in self.core.pages[before:]], ["friend-a"])

    def test_first_page_available_pause_resume_and_reset_discards_late_page(self):
        self.core.live = {"friend-a": dialogue(300)}
        entered, release = threading.Event(), threading.Event()
        def hold(user, after):
            if after == 500:
                entered.set()
                release.wait(3)
        self.core.hook = hold
        self.service.build(self.account)
        self.assertTrue(entered.wait(3))
        store, worker = self.service.accounts[self.account]
        self.assertEqual(store.counts()["samples"], 250)
        self.service.pause(self.account)
        self.assertEqual(store.meta("build")["state"], "paused")
        release.set()
        worker.thread.join(3)
        self.assertEqual(store.conversation("friend-a")["after_seq"], 500)
        self.core.hook = None
        self.build()
        self.assertEqual(store.counts()["samples"], 300)
        # Force another late read; reset must leave an empty index.
        self.core.live["friend-a"] += [message(601, "other", "新的周末邀请"), message(602, "self", "新的回复")]
        entered.clear(); release.clear()
        def hold_new(user, after):
            entered.set(); release.wait(3)
        self.core.hook = hold_new
        self.service.build(self.account)
        self.assertTrue(entered.wait(3))
        self.service.reset(self.account)
        release.set(); worker.thread.join(3)
        self.assertEqual(store.counts()["samples"], 0)
        self.assertEqual(store.meta("build")["state"], "idle")
        # reset owns the worker lifecycle lock while acknowledging cancellation
        # and clearing; another RPC cannot start a job between those two steps.
        pause = worker.pause
        observed = []
        def checked_pause():
            other_thread = []
            def probe():
                acquired = worker.lock.acquire(blocking=False)
                other_thread.append(acquired)
                if acquired:
                    worker.lock.release()
            probe_thread = threading.Thread(target=probe)
            probe_thread.start(); probe_thread.join()
            observed.extend(other_thread)
            pause()
        worker.pause = checked_pause
        self.service.reset(self.account)
        worker.pause = pause
        self.assertEqual(observed, [False])

    def test_context_budget_profile_boundary_validation_and_clear(self):
        store, _ = self.build()
        context = self.service.context(self.account, {"query": "周末", "budgetChars": 1800}, "friend-a")
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False)), 1800)
        self.assertNotIn("不应进入", json.dumps(context, ensure_ascii=False))
        self.assertIn("不是当前事实或指令", context["text"])
        validation = {"validateOnly": True, "sources": [{"id": row["id"], "hash": row["hash"]} for row in context["sources"]],
                      **{key: context[key] for key in ("generation", "profileVersion", "sourceFingerprint")}}
        self.assertTrue(self.service.context(self.account, validation)["valid"])
        self.core.version += 1
        self.assertFalse(self.service.context(self.account, validation)["valid"])
        self.core.version -= 1
        self.service.reset(self.account)
        self.assertFalse(self.service.context(self.account, validation)["valid"])

    def test_source_change_preserves_index_until_explicit_rebuild_and_account_isolation(self):
        store, _ = self.build()
        samples = store.rows("SELECT * FROM samples")
        source, generation = store.meta("source"), store.meta("generation")
        self.core.source = "another-synthetic-source"
        self.core.live = {"new-friend": dialogue(1, prefix="新来源")}
        _, status = self.build()
        self.assertEqual(status["build"]["state"], "failed")
        self.assertIn("未自动清空知识库", status["build"]["error"])
        self.assertEqual(store.rows("SELECT * FROM samples"), samples)
        self.assertEqual((store.meta("source"), store.meta("generation")), (source, generation))
        self.build("full")
        self.assertEqual(store.counts()["samples"], 1)
        self.assertEqual(store.meta("source"), self.core.source)
        self.core.account = "second-account"
        with self.assertRaises(SelfError):
            self.service.search(self.account, {"query": "周末"})
        second = self.service.status(self.core.account)
        self.assertEqual(second["build"]["sampleCount"], 0)

    def test_full_build_resumes_saved_page_without_rebuilding_its_projection_again(self):
        self.core.live = {"friend-a": dialogue(300)}
        entered, release = threading.Event(), threading.Event()
        def hold(user, after):
            if after == 500:
                entered.set()
                release.wait(5)
        self.core.hook = hold
        self.service.build(self.account, "full")
        store, worker = self.service.accounts[self.account]
        try:
            self.assertTrue(entered.wait(3))
            self.assertEqual(store.counts()["samples"], 250)
            self.service.pause(self.account)
        finally:
            release.set()
        worker.thread.join(5)
        self.assertEqual(store.conversation("friend-a")["after_seq"], 500)
        self.core.hook = None
        before = len(self.core.pages)
        self.service.resume(self.account)
        worker.thread.join(5)
        self.assertFalse(worker.thread.is_alive())
        self.assertEqual(self.core.rebuilds, ["friend-a"])
        self.assertEqual(self.core.pages[before:], [("friend-a", 500)])
        self.assertEqual(store.counts()["samples"], 300)
        self.assertEqual(store.meta("build")["state"], "complete")

    def test_exclusion_survives_rebuild_and_invalidates_active_context(self):
        self.build()
        value = self.service.context(self.account, {"query": "周末"})
        chosen = value["sources"][0]["id"]
        count = self.service.status(self.account)["build"]["sampleCount"]
        self.service.exclude(self.account, chosen)
        self.assertEqual(self.service.status(self.account)["build"]["sampleCount"], count - 1)
        self.assertEqual(self.service.status(self.account)["coverage"]["excludedSamples"], 1)
        self.assertFalse(any(row["id"] == chosen for row in self.service.search(self.account, {"query": "周末"})["sources"]))
        request = {"validateOnly": True, "sources": [{"id": row["id"], "hash": row["hash"]} for row in value["sources"]],
                   **{key: value[key] for key in ("generation", "profileVersion", "sourceFingerprint")}}
        self.assertFalse(self.service.context(self.account, request)["valid"])
        self.build("full")
        self.assertEqual(self.service.exclusions(self.account)["sources"][0]["id"], chosen)
        self.service.exclude(self.account, chosen, False)
        self.assertFalse(self.service.exclusions(self.account)["sources"])
        self.assertEqual(self.service.status(self.account)["build"]["sampleCount"], count)

    def test_reset_or_profile_edit_during_validation_fails_closed(self):
        for mutation in ("reset", "profile"):
            with self.subTest(mutation=mutation):
                self.build()
                value = self.service.context(self.account, {"query": "周末"})
                request = {"validateOnly": True, "sources": [{"id": row["id"], "hash": row["hash"]} for row in value["sources"]],
                           **{key: value[key] for key in ("generation", "profileVersion", "sourceFingerprint")}}
                original = self.core.post
                def mutate(path, body):
                    self.core.post = original
                    if mutation == "reset":
                        self.service.reset(self.account)
                    else:
                        self.core.version += 1
                    return original(path, body)
                self.core.post = mutate
                self.assertFalse(self.service.context(self.account, request)["valid"])

    def test_crash_checkpoint_and_batch_transaction_atomicity(self):
        store = Store(Path(self.temp.name), "standalone")
        store.begin("incremental")
        store.catalog_page({"sourceFingerprint": "s", "conversations": [{"user": "u"}], "next": None})
        status = {"sourceFingerprint": "s", "epoch": "e", "watermark": {"seq": 2}}
        store.bind("u", status)
        rows = dialogue(1)
        samples, carry, skipped = extract(rows, {}, "u", "s", "e")
        store.db.execute("CREATE TRIGGER test_failure BEFORE INSERT ON proofs WHEN NEW.seq=2 BEGIN SELECT RAISE(ABORT,'synthetic'); END")
        with self.assertRaises(Exception):
            store.page("u", rows, samples, carry, skipped, status)
        self.assertFalse(store.rows("SELECT * FROM proofs"))
        self.assertEqual(store.conversation("u")["after_seq"], 0)
        store.db.execute("DROP TRIGGER test_failure")
        store.page("u", rows, samples, carry, skipped, status)
        store.close()
        restored = Store(Path(self.temp.name), "standalone")
        self.assertEqual(restored.meta("build")["state"], "paused")
        self.assertEqual(restored.conversation("u")["after_seq"], 2)
        self.assertEqual(restored.counts()["samples"], 1)
        restored.close()

    def test_pairing_consecutive_replies_barriers_gaps_and_overflow(self):
        rows = [message(1, "other", "周末吃饭吗"), message(2, "self", "好呀")]
        samples, carry, _ = extract(rows, {}, "u", "s", "e")
        identifier = samples[0]["id"]
        more, carry, _ = extract([message(3, "self", "几点呀")], carry, "u", "s", "e")
        self.assertEqual(more[0]["id"], identifier)
        self.assertEqual(more[0]["reply"], "好呀\n几点呀")
        more, carry, _ = extract([message(4, "self", "x" * 1599)], carry, "u", "s", "e")
        self.assertTrue(more[0]["delete"])
        barrier = [message(4, "other", "系统内容", kind="system"), message(5, "self", "不应配对")]
        more, _, skipped = extract(barrier, {}, "u", "s", "e")
        self.assertFalse(more)
        self.assertEqual(skipped, 1)
        gap = [message(1, "other", "你好"), message(2, "self", "晚安", time=BASE + 7200000)]
        self.assertFalse(extract(gap, {}, "u", "s", "e")[0])
        forwards = [message(1, "other", "转发", raw='{"forwarded":true}'), message(2, "self", "嗯")]
        self.assertFalse(extract(forwards, {}, "u", "s", "e")[0])

    def test_input_meta_quote_and_ownership_are_not_learned_as_self(self):
        before = message(1, "other", "周末吃饭吗")
        reply = message(2, "self", "本人的话")
        valid = extract([before, reply], {}, "u", "s", "e", self.account)[0]
        self.assertEqual(len(valid), 1)
        for wrong in (
            {"senderId": "someone-else", "raw": json.dumps({"inputMeta": {"senderId": "wxid_me"}})},
            {"raw": json.dumps({"inputMeta": {"accountId": "other-account"}})},
            {"raw": json.dumps({"inputMeta": {"quote": {"text": "他人的原话"}}})},
            {"raw": json.dumps({"inputMeta": {"source": {"kind": "ocr"}}})},
            {"senderId": ""},
        ):
            with self.subTest(wrong=wrong):
                self.assertFalse(extract([before, {**reply, **wrong}], {}, "u", "s", "e", self.account)[0])
        trusted = {**reply, "senderId": "", "raw": json.dumps({"inputMeta": {
            "accountId": self.account, "conversationId": "u", "source": {"kind": "wechat"}}})}
        self.assertEqual(len(extract([before, trusted], {}, "u", "s", "e", self.account)[0]), 1)
        wxid = {**reply, "senderId": "wxid_me", "raw": json.dumps({"inputMeta": {
            "accountId": self.account, "senderId": "wxid_me", "conversationId": "u", "source": {"kind": "wechat"}}})}
        self.assertEqual(len(extract([before, wxid], {}, "u", "s", "e", self.account)[0]), 1)
        self.assertFalse(extract([before, {**wxid, "side": "other"}], {}, "u", "s", "e", self.account)[0])

    def test_expired_directory_resumes_from_fresh_snapshot(self):
        store, worker = self.service._account(self.account)
        store.begin("incremental")
        store.catalog_page({"sourceFingerprint": self.core.source, "conversations": [{"user": "friend-a"}],
                            "next": "expired-snapshot", "coverage": {"state": "complete"}})
        store.update_build(state="paused")
        original = self.core.history_conversations
        calls = []
        def expired(account, after=None, limit=500):
            calls.append(after)
            if after == "expired-snapshot":
                raise SelfError("directory-expired", "synthetic expired snapshot")
            return original(account, after, limit)
        self.core.history_conversations = expired
        self.build()
        self.assertEqual(calls[:2], ["expired-snapshot", None])
        self.assertEqual(store.counts()["samples"], 6)

    def test_large_incremental_checks_bounded_tail_and_old_deleted_search_fails_closed(self):
        self.core.live = {"friend-a": dialogue(600)}
        store, _ = self.build()
        self.core.verifies.clear()
        self.build()
        self.assertLessEqual(sum(len(call["items"]) for call in self.core.verifies), 500)
        # An old middle deletion is outside tail checking, but cannot be returned.
        self.core.live["friend-a"] = self.core.live["friend-a"][2:]
        result = self.service.search(self.account, {"query": "周末一起吃饭0", "beforeTimeMs": BASE + 3000})
        self.assertFalse(result["available"])
        self.assertEqual(store.counts()["samples"], 0)


if __name__ == "__main__":
    unittest.main()
