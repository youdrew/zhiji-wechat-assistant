"""Full-profile source coverage tests using synthetic chats and a temporary DB only.

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
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "plugin-sdk/python"), str(ROOT / "plugins/kyf.self-analysis/backend")]
from kyf_plugin import Cancelled
from self_contracts import SelfError
from self_profile_corpus import CORPUS_VERSION, ProfileCorpus, Reader
from self_store import Store


def message(seq, text, side="self", **extra):
    return {"id": f"message-{seq}", "seq": seq, "time": 1_700_000_000_000 + seq,
            "side": side, "kind": "text", "text": text, "raw": "{}", **extra}


class Context:
    def __init__(self):
        self.cancelled = threading.Event()

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()


class Core:
    def __init__(self, live):
        self.live = copy.deepcopy(live)
        self.source, self.epoch = "synthetic-source", "epoch-1"
        self.fail = set()
        self.page_size = 2
        self.pages, self.catalog_calls = [], []
        self.hook = None
        self.coverage = {"state": "complete"}
        self.extra_status = {}

    def get(self, path, **query):
        """Plugin settings as Core serves them (manifest defaults unless a test changes them); no external chats."""
        if path == "external/friends":
            return {"friends": []}
        assert path == "settings", path
        values = getattr(self, "plugin_settings", None) or {"analysisDepth": "deep", "replyDepth": "standard", "analysisGrain": "6"}
        return {"values": dict(values), "revision": 1}

    def history_conversations(self, account, after=None, limit=500):
        self.catalog_calls.append(after)
        users = sorted(self.live)
        offset = users.index(after) + 1 if after else 0
        selected = users[offset:offset + 1]
        return {"sourceFingerprint": self.source, "revision": "directory-1", "coverage": self.coverage,
                "conversations": [{"user": user, "name": "合成聊天", "isGroup": user.endswith("@chatroom")} for user in selected],
                "next": selected[-1] if selected and offset + 1 < len(users) else None}

    def history_prepare(self, account, user, refresh=False):
        if user in self.fail:
            raise RuntimeError("PRIVATE_MESSAGE_AND_PATH_MUST_NOT_ESCAPE")
        return self.history_status(account, user)

    def history_status(self, account, user):
        return {"state": "ready", "sourceFingerprint": self.source, "currentSourceFingerprint": self.source,
                "epoch": self.epoch, "revision": len(self.live[user]), "readCount": len(self.live[user]),
                "totalCount": len(self.live[user]), "watermark": {"seq": len(self.live[user])}, **self.extra_status}

    def history_messages(self, account, user, after=0, limit=500):
        self.pages.append((user, after))
        rows = [row for row in self.live[user] if row["seq"] > after][:min(limit, self.page_size)]
        if self.hook:
            self.hook(user, after)
        return {"messages": copy.deepcopy(rows), "sourceFingerprint": self.source, "epoch": self.epoch}


def source_hash(row):
    facts = [row.get(key) for key in ("id", "seq", "time", "side", "kind", "text", "raw")]
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


class AuditedCore(Core):
    """A separate source and projection, so rebuilding is necessary to see old edits."""
    def __init__(self, live):
        super().__init__(live)
        self.originals = copy.deepcopy(live)
        self.source_revision = "source-revision-1"
        self.epochs = {}
        self.rebuilds, self.verifications, self.audits, self.status_batches = [], [], [], []
        self.verify_state = None
        self.directory_latest = False
        self.prepared = []

    def history_conversations(self, account, after=None, limit=500):
        result = super().history_conversations(account, after, limit)
        if self.directory_latest:
            # The source's newest row, in the same encoding as a projection highwater.
            for row in result["conversations"]:
                size = len(self.originals[row["user"]])
                row["latest"] = json.dumps([size, row["user"], size])
        return result

    def history_prepare(self, account, user, refresh=False, rebuild=False):
        self.prepared.append(user)
        if rebuild:
            self.rebuilds.append(user)
            self.live[user] = copy.deepcopy(self.originals[user])
            self.epochs[user] = self.epochs.get(user, 0) + 1
        else:
            tail = len(self.live[user])
            self.live[user].extend(copy.deepcopy([row for row in self.originals[user] if row["seq"] > tail]))
        return self.history_status(account, user)

    def history_status(self, account, user, fresh=False, audit=False, audit_highwater=None):
        result = super().history_status(account, user)
        result.update(epoch=f"epoch-{self.epochs.get(user, 0)}", sourceRevision=self.source_revision,
                      highwater=json.dumps([len(self.live[user]), user, len(self.live[user])]))
        if audit:
            ceiling = audit_highwater or result["highwater"]
            upto = json.loads(ceiling)[0]
            prefix = [[row["id"], row["seq"]] for row in self.originals[user] if row["seq"] <= upto]
            result.update(sourcePrefixCeiling=ceiling, sourcePrefixSignature=json.dumps(prefix))
            self.audits.append((user, ceiling))
        return result

    def history_status_many(self, account, users):
        self.status_batches.append(list(users))
        statuses = []
        for user in users:
            status = {key: value for key, value in self.history_status(account, user).items() if key != "sourceRevision"}
            statuses.append({"user": user, **status})
        return {"currentSourceFingerprint": self.source, "results": statuses}

    def history_messages(self, account, user, after=0, limit=500):
        result = super().history_messages(account, user, after, limit)
        result["epoch"] = f"epoch-{self.epochs.get(user, 0)}"
        for row in result["messages"]:
            row["hash"] = source_hash(row)
        return result

    def history_verify_many(self, account, groups):
        self.verifications.extend(copy.deepcopy(groups))
        response = []
        for group in groups:
            saved = {row["id"]: row for row in self.originals[group["user"]]}
            results = []
            for item in group["items"]:
                row = saved.get(item["id"])
                state = self.verify_state or ("missing" if row is None else
                                              "same" if source_hash(row) == item["hash"] else "changed")
                results.append({"id": item["id"], "state": state})
            response.append({"user": group["user"], "results": results})
        return {"results": response}


def external(identifier, order, text, sender="self", **extra):
    return {"id": identifier, "sourceId": "source-a", "order": f"000000000000:{order:012d}", "text": text,
            "sender": sender, "kind": "text", "time": None, **extra}


class ExternalCore(Core):
    """Core's read-only external-chat routes over synthetic saved records."""
    def __init__(self, live, friends, histories):
        super().__init__(live)
        self.friends, self.histories, self.external_fail = friends, histories, False

    def get(self, path, **query):
        if self.external_fail:
            raise RuntimeError("external store unavailable")
        if path == "external/friends":
            return {"friends": copy.deepcopy(self.friends)}
        assert path == "external/history"
        return copy.deepcopy(self.histories[query["friendId"]])


class ProfileCorpusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name), "synthetic-account")
        self.corpus, self.context = ProfileCorpus(self.store), Context()
        self.progress = []

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def cancel_verifying(self, core, user):
        original = core.history_verify_many

        def verify(account, groups):
            if any(group["user"] == user for group in groups):
                self.context.cancelled.set()
            return original(account, groups)

        core.history_verify_many = verify

    def reader(self, core, **options):
        return Reader(core, "synthetic-account", self.corpus, self.context, self.progress.append, **options)

    def test_full_text_short_opening_groups_and_context_survive_pagination(self):
        long_text = "长消息" * 500
        core = Core({"friend": [message(1, "好"), message(2, "？"), message(3, "🙂"),
                                message(4, "对方问题", "other"), message(5, long_text)],
                     "group@chatroom": [message(1, "群聊开场"), message(2, "收到", "other"), message(3, "嗯")]})
        counts = self.reader(core).run()
        rows = self.corpus.iter_messages()
        self.assertEqual([row["text"] for row in rows], ["好", "？", "🙂", long_text, "群聊开场", "嗯"])
        self.assertEqual((counts["conversations"], counts["readConversations"], counts["messagesRead"]), (2, 2, 8))
        self.assertEqual((counts["ownMessages"], counts["privateMessages"], counts["groupMessages"]), (6, 4, 2))
        self.assertIn("对方问题", rows[3]["context"])
        self.assertTrue(all(len(row["context_before"]) <= 12 and len(row["context_after"]) <= 6 for row in rows))
        self.assertTrue(all(len(item["text"]) <= 600 for row in rows for item in row["context_before"] + row["context_after"]))
        self.assertEqual(rows[0]["context_after"][0]["text"], "？")
        self.assertEqual(counts["gaps"], [])
        self.assertTrue(counts["catalogComplete"])
        self.assertGreater(len(self.progress), 4)
        self.assertEqual(self.corpus.iter_messages(after_id=rows[1]["rowid"], limit=1), [rows[2]])

    def external_core(self, messages):
        friends = [{"id": "friend-x", "name": "合成外部好友", "wechatAccount": "synthetic-account", "wechatUser": "friend"},
                   {"id": "friend-other", "name": "另一个账号的好友", "wechatAccount": "another-account", "wechatUser": "x"}]
        histories = {"friend-x": {"messages": messages, "sources": [{"id": "source-a", "platform": "合成来源"}]},
                     "friend-other": {"messages": [{**external("other-1", 1, "不属于本账号"), "sourceId": "source-b"}],
                                      "sources": [{"id": "source-b", "platform": "别处"}]}}
        return ExternalCore({"friend": [message(1, "微信里的话")]}, friends, histories)

    def test_saved_external_chat_joins_as_its_own_chat_in_screen_order(self):
        core = self.external_core([external("m3", 3, "我晚点回你"), external("m1", 1, "周末去爬山吗", "other"),
                                   external("m2", 2, "好呀，我最喜欢爬山"), external("m4", 4, "[图片]", kind="image")])
        counts = self.reader(core).run()
        rows = self.corpus.iter_messages()
        external_rows = [row for row in rows if row["user"] == "external:source-a"]
        self.assertEqual([row["text"] for row in external_rows], ["好呀，我最喜欢爬山", "我晚点回你"])
        self.assertEqual(external_rows[0]["name"], "合成外部好友（合成来源·外部聊天）")
        self.assertEqual(external_rows[0]["time_ms"], 0)
        # Untimed screenshots still give each line its on-screen neighbors.
        self.assertEqual([item["text"] for item in external_rows[0]["context_before"]], ["周末去爬山吗"])
        self.assertEqual([item["speaker"] for item in external_rows[0]["context_before"]], ["对方"])
        self.assertEqual([item["text"] for item in external_rows[0]["context_after"]], ["我晚点回你"])
        self.assertFalse(external_rows[0]["context_info"]["timeUnknown"])
        self.assertIn("对方：周末去爬山吗", external_rows[0]["context"])
        self.assertEqual((counts["ownMessages"], counts["privateMessages"], counts["externalMessages"]), (3, 1, 2))
        self.assertEqual(counts["gaps"], [])
        self.assertFalse(any(row["user"] == "external:source-b" for row in rows), "another account's friend stays out")

    def test_external_chat_follows_corrections_and_removal_without_wechat_checks(self):
        core = self.external_core([external("m1", 1, "原来的话"), external("m2", 2, "不变的话")])
        self.reader(core).run()
        kept = {row["id"]: row["rowid"] for row in self.corpus.iter_messages()}
        revision = self.corpus.meta("revision")
        core.histories["friend-x"]["messages"][0]["text"] = "更正后的话"
        # A new round re-reads WeChat; the external chat never reaches WeChat status or verification.
        self.reader(core).run(resume=False)
        rows = {row["id"]: row for row in self.corpus.iter_messages()}
        self.assertEqual(rows['["external:source-a", "m1"]']["text"], "更正后的话")
        self.assertEqual(rows['["external:source-a", "m2"]']["rowid"], kept['["external:source-a", "m2"]'])
        self.assertGreater(self.corpus.meta("revision"), revision)
        self.assertEqual(self.corpus.rows("SELECT state FROM profile_corpus_conversations WHERE user='external:source-a'"),
                         [{"state": "done"}])
        core.histories["friend-x"] = {"messages": [], "sources": []}
        counts = self.reader(core).run(resume=False)
        self.assertEqual((counts["conversations"], counts["externalMessages"]), (1, 0))
        self.assertEqual(self.corpus.rows("SELECT count(*) n FROM profile_corpus_context WHERE user LIKE 'external:%'"), [{"n": 0}])

    def test_unreadable_external_chats_keep_the_last_copy_and_report_a_gap(self):
        core = self.external_core([external("m1", 1, "已同步的话")])
        self.reader(core).run()
        core.external_fail = True
        counts = self.reader(core).run(resume=False)
        self.assertEqual(counts["externalMessages"], 1)
        self.assertIn({"kind": "external-unreadable", "count": 1}, counts["gaps"])
        core.external_fail = False
        self.assertEqual(self.reader(core).run(resume=False)["gaps"], [])

    def test_reset_only_touches_own_tables(self):
        self.store._meta("tagRun", {"preserve": True})
        self.store.db.commit()
        self.reader(Core({"friend": [message(1, "合成文本")]})).run()
        self.corpus.reset()
        self.assertEqual(self.corpus.counts()["ownMessages"], 0)
        self.assertEqual(self.corpus.rows("SELECT count(*) n FROM profile_corpus_context")[0]["n"], 0)
        self.assertEqual(self.store.meta("tagRun"), {"preserve": True})

    def test_quoted_reply_keeps_only_current_own_words(self):
        raw = {"type": "应用消息", "inputMeta": {"quote": {"text": "OTHER_QUOTED_BODY"}}}
        core = Core({"friend": [message(1, "我自己的当前回复", raw=json.dumps(raw))]})
        counts = self.reader(core).run()
        self.assertEqual(counts["ownMessages"], 1)
        rows = self.corpus.iter_messages()
        self.assertEqual(rows[0]["text"], "我自己的当前回复")
        self.assertNotIn("OTHER_QUOTED_BODY", json.dumps(rows))

    def test_literal_markup_in_typed_text_is_not_a_system_message(self):
        text = "这段 XML 的 <sysmsg> 标签是什么意思？"
        self.reader(Core({"friend": [message(1, text)]})).run()
        self.assertEqual(self.corpus.iter_messages()[0]["text"], text)

    def test_unknown_timestamp_does_not_remove_own_text(self):
        core = Core({"friend": [message(1, "日期未知也要读", time=None), message(2, "浮点日期", time=1700000000000.5)]})
        counts = self.reader(core).run()
        self.assertEqual(counts["ownMessages"], 2)
        self.assertEqual([row["time_ms"] for row in self.corpus.iter_messages()], [0, 1700000000000])

    def test_exclusions_and_known_directory_and_source_gaps_are_counted(self):
        core = Core({"friend": [message(1, "合成文本"), message(2, "系统通知", kind="system"),
                                message(3, "转发", raw=json.dumps({"forward": True})), message(4, "", kind="image")]})
        core.coverage = {"state": "partial", "failedShards": [{"shard": "PRIVATE_PATH"}], "unresolvedTables": 2}
        core.extra_status = {"totalCount": 7, "partial": True}
        counts = self.reader(core).run()
        self.assertEqual((counts["messagesRead"], counts["ownMessages"], counts["excludedMessages"], counts["unreadMessages"]), (4, 1, 3, 3))
        self.assertTrue(counts["directoryPartial"])
        self.assertIn({"kind": "failed-shards", "count": 1}, counts["gaps"])
        self.assertIn({"kind": "unread-messages", "count": 3}, counts["gaps"])
        self.assertNotIn("PRIVATE", json.dumps(counts))

    def test_failed_chat_is_retried_without_rereading_finished_chats(self):
        core = Core({"a": [message(1, "一")], "b": [message(1, "二")]})
        core.fail.add("b")
        first = self.reader(core).run()
        self.assertEqual((first["readConversations"], first["failedConversations"]), (1, 1))
        self.assertNotIn("PRIVATE", json.dumps(first))
        core.fail.clear()
        final = self.reader(core).run(resume=True)
        self.assertEqual((final["readConversations"], final["failedConversations"]), (2, 0))
        self.assertEqual(core.pages, [("a", 0), ("b", 0)])

    def test_cancelled_page_not_written_and_resume_uses_last_checkpoint(self):
        core = Core({"friend": [message(n, str(n)) for n in range(1, 7)]})
        core.hook = lambda user, after: self.context.cancelled.set() if after == 2 else None
        with self.assertRaises(Cancelled):
            self.reader(core).run()
        self.assertEqual(self.corpus.counts()["ownMessages"], 2)
        # A new wrapper over the same DB restores durable page/carry state.
        self.corpus = ProfileCorpus(self.store)
        self.context.cancelled.clear()
        core.hook = None
        counts = self.reader(core).run(resume=True)
        self.assertEqual(counts["ownMessages"], 6)
        self.assertEqual(core.pages, [("friend", 0), ("friend", 2), ("friend", 2), ("friend", 4)])
        self.assertIn("本人：2", self.corpus.iter_messages()[2]["context"])

    def test_noncontiguous_page_does_not_advance_checkpoint(self):
        core = Core({"friend": [message(1, "一"), message(3, "三")]})
        core.extra_status = {"watermark": {"seq": 3}}
        counts = self.reader(core).run()
        self.assertEqual(counts["ownMessages"], 0)
        self.assertEqual(counts["unreadMessages"], 3)
        self.assertIn({"kind": "noncontiguous-messages", "count": 1}, counts["gaps"])

    def test_source_and_epoch_changes_abort_before_mixed_page_write(self):
        for field in ("source", "epoch"):
            with self.subTest(field=field):
                self.context.cancelled.clear()
                core = Core({"friend": [message(n, str(n)) for n in range(1, 5)]})
                core.hook = lambda user, after: setattr(core, field, "changed") if after == 2 else None
                with self.assertRaises(SelfError) as caught:
                    self.reader(core).run()
                self.assertEqual(caught.exception.code, "source-changed")
                self.assertEqual(self.corpus.counts()["ownMessages"], 2)

    def test_expired_directory_restarts_once_without_losing_discovered_rows(self):
        core = Core({"a": [message(1, "一")], "b": [message(1, "二")]})
        original = core.history_conversations
        expired = [False]

        def directory(account, after=None, limit=500):
            if after and not expired[0]:
                expired[0] = True
                raise SelfError("directory-expired", "合成过期")
            return original(account, after, limit)

        core.history_conversations = directory
        counts = self.reader(core).run()
        self.assertEqual(counts["readConversations"], 2)
        self.assertEqual(counts["ownMessages"], 2)

    def test_prepare_failure_keeps_known_unread_count(self):
        core = Core({"friend": []})
        core.extra_status = {"state": "error", "totalCount": 11, "error": "PRIVATE_PROVIDER_DETAIL"}
        counts = self.reader(core).run()
        self.assertEqual(counts["unreadMessages"], 11)
        self.assertEqual(counts["failedConversations"], 1)
        self.assertNotIn("PRIVATE", json.dumps(counts))

    def test_preparation_with_continuous_source_progress_can_exceed_three_minutes(self):
        for field in ("readCount", "watermark"):
            with self.subTest(progress=field):
                self.corpus.reset()
                core = Core({"synthetic-user-must-not-appear": []})
                reader = self.reader(core)
                reader._discover()
                clock, waits, updates, polls = [0.0], [], [], []
                self.context.progress = lambda **values: updates.append((clock[0], values))

                def status(account, user):
                    polls.append(clock[0])
                    advanced = int(clock[0])
                    ready = clock[0] >= 201
                    return {"state": "ready" if ready else "preparing", "importing": not ready,
                            "sourceFingerprint": core.source, "currentSourceFingerprint": core.source,
                            "epoch": core.epoch, "totalCount": 201,
                            "readCount": advanced if field == "readCount" else 0,
                            "watermark": {"seq": advanced if field == "watermark" else 0}}

                def wait(seconds):
                    waits.append(seconds)
                    clock[0] += seconds

                core.history_prepare = lambda account, user, refresh=False: status(account, user)
                core.history_status = status
                with patch("self_profile_corpus.time.monotonic", side_effect=lambda: clock[0]), \
                        patch.object(self.context.cancelled, "wait", side_effect=wait):
                    result = reader._ready("synthetic-user-must-not-appear")
                self.assertEqual(result["state"], "ready")
                self.assertEqual(clock[0], 201)
                self.assertTrue(all(seconds == 0.25 for seconds in waits))
                self.assertTrue(all(later[0] - earlier[0] >= 1 for earlier, later in zip(updates, updates[1:])))
                self.assertEqual((updates[-1][1]["phase"], updates[-1][1]["currentChatDone"],
                                  updates[-1][1]["currentChatTotal"], updates[-1][1]["sourceMessagesRead"]),
                                 ("preparing", 200, 201, 0))
                self.assertNotIn("synthetic-user-must-not-appear", json.dumps(updates))
                self.assertEqual(len(polls), 1 + len(waits))

    def test_preparation_without_new_read_highwater_still_times_out(self):
        for mode in ("stationary", "total-only", "oscillating"):
            with self.subTest(mode=mode):
                self.corpus.reset()
                core = Core({"friend": []})
                reader = self.reader(core)
                reader._discover()
                clock = [0.0]

                def status(account, user):
                    read = 7 - (int(clock[0]) % 2 if mode == "oscillating" else 0)
                    return {"state": "preparing", "importing": True, "sourceFingerprint": core.source,
                            "currentSourceFingerprint": core.source, "epoch": core.epoch,
                            "readCount": read, "watermark": {"seq": read},
                            "totalCount": 11 + (int(clock[0]) if mode == "total-only" else 0)}

                core.history_prepare = lambda account, user, refresh=False: status(account, user)
                core.history_status = status
                with patch("self_profile_corpus.time.monotonic", side_effect=lambda: clock[0]), \
                        patch.object(self.context.cancelled, "wait", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)):
                    with self.assertRaises(SelfError) as caught:
                        reader._ready("friend")
                self.assertEqual(caught.exception.code, "history-error")
                self.assertEqual(clock[0], 180)
                self.assertGreater(self.corpus.counts()["unreadMessages"], 0)

    def test_preparation_cancel_interrupts_wait_before_another_status_rpc(self):
        core = Core({"friend": []})
        reader = self.reader(core)
        reader._discover()
        core.extra_status = {"state": "preparing", "importing": True, "totalCount": 99}
        polls = []
        original = core.history_status

        def status(account, user):
            polls.append(user)
            return original(account, user)

        core.history_status = status
        with patch("self_profile_corpus.time.monotonic", return_value=0), \
                patch.object(self.context.cancelled, "wait", side_effect=lambda seconds: self.context.cancelled.set()):
            with self.assertRaises(Cancelled):
                reader._ready("friend")
        self.assertEqual(polls, ["friend"])
        self.assertEqual(self.corpus.counts()["failedConversations"], 0)

    def test_preparation_source_switch_and_explicit_errors_do_not_wait_for_timeout(self):
        for state in ("source-switch", "error", "unsupported"):
            with self.subTest(state=state):
                self.corpus.reset()
                core = Core({"friend": []})
                reader = self.reader(core)
                reader._discover()
                core.extra_status = {"state": "preparing" if state == "source-switch" else state,
                                     "importing": True, "totalCount": 11,
                                     **({"currentSourceFingerprint": "different-source"} if state == "source-switch" else {})}
                with patch.object(self.context.cancelled, "wait") as wait:
                    with self.assertRaises(SelfError) as caught:
                        reader._ready("friend")
                self.assertEqual(caught.exception.code, "source-changed" if state == "source-switch" else "history-error")
                wait.assert_not_called()

    def test_context_has_twelve_prior_and_six_following_messages_with_stable_identity(self):
        rows = [message(seq, f"第{seq}条上下文", "self" if seq == 20 else "other") for seq in range(1, 32)]
        core = Core({"friend": rows})
        self.reader(core).run()
        own = self.corpus.iter_messages()[0]
        self.assertEqual([item["seq"] for item in own["context_before"]], list(range(8, 20)))
        self.assertEqual([item["seq"] for item in own["context_after"]], list(range(21, 27)))
        self.assertEqual(own["context_info"], {"truncated": False, "timeUnknown": False, "windowMinutes": 60,
                                                "beforeLimit": 12, "afterLimit": 6,
                                                "omittedUnknownTime": 0, "omittedInconsistentTime": 0})
        for item in own["context_before"] + own["context_after"]:
            self.assertEqual(set(item), {"id", "seq", "time", "side", "speaker", "text", "truncated"})
            self.assertEqual(json.loads(item["id"]), ["friend", f"message-{item['seq']}"])
            self.assertEqual(item["time"], 1_700_000_000_000 + item["seq"])
            self.assertEqual((item["side"], item["speaker"]), ("other", "对方"))

    def test_group_context_distinguishes_different_speakers_and_own_words(self):
        core = Core({"group@chatroom": [message(1, "这是甲的观点", "other", senderName="同名群友", senderId="member-a"),
                                        message(2, "这是乙的观点", "other", senderName="同名群友", senderId="member-b"),
                                        message(3, "这是我自己的话"), message(4, "这是丙的观点", "other", senderId="member-c"),
                                        message(5, "我再补一句"), message(6, "原始元数据中的群友", "other", raw=json.dumps({
                                            "inputMeta": {"senderId": "metadata-member", "senderName": "元数据群友"}}))]})
        self.reader(core).run()
        own = self.corpus.iter_messages()
        first_before = own[0]["context_before"]
        self.assertNotEqual(first_before[0]["speaker"], first_before[1]["speaker"])
        self.assertIn("member-a", first_before[0]["speaker"])
        self.assertIn("member-b", first_before[1]["speaker"])
        self.assertIn("member-c", own[0]["context_after"][0]["speaker"])
        self.assertEqual((own[0]["context_after"][1]["side"], own[0]["context_after"][1]["speaker"]), ("self", "本人"))
        self.assertIn("metadata-member", own[1]["context_after"][0]["speaker"])
        self.assertEqual([item["text"] for item in first_before], ["这是甲的观点", "这是乙的观点"])

    def test_context_window_excludes_distant_and_unknown_times_without_dropping_own_text(self):
        anchor = 1_700_000_000_000
        core = Core({"friend": [message(1, "之前时间不明", "other", time=None),
                                 message(2, "之前超过一小时", "other", time=anchor - 61 * 60_000),
                                 message(3, "之前一小时整", "other", time=anchor - 60 * 60_000),
                                 message(4, "我自己的当前观点", time=anchor),
                                 message(5, "之后一小时整", "other", time=anchor + 60 * 60_000),
                                 message(6, "之后超过一小时", "other", time=anchor + 61 * 60_000),
                                 message(7, "之后时间不明", "other", time=None),
                                 message(8, "我自己的未知日期文字", time=None)]})
        self.reader(core).run()
        known, unknown = self.corpus.iter_messages()
        self.assertEqual([item["seq"] for item in known["context_before"]], [3])
        self.assertEqual([item["seq"] for item in known["context_after"]], [5])
        self.assertTrue(known["context_info"]["timeUnknown"])
        self.assertTrue(known["context_info"]["truncated"])
        self.assertEqual(known["context_info"]["omittedUnknownTime"], 3)
        self.assertEqual(unknown["text"], "我自己的未知日期文字")
        self.assertEqual((unknown["context_before"], unknown["context_after"], unknown["context"]), ([], [], ""))
        self.assertTrue(unknown["context_info"]["timeUnknown"])

    def test_inconsistent_neighbor_times_are_reported_but_normal_window_boundaries_are_not_gaps(self):
        anchor = 1_700_000_000_000
        core = Core({"friend": [message(1, "顺序在前但时间在后", "other", time=anchor + 60_000),
                                 message(2, "我当前这句", time=anchor),
                                 message(3, "顺序在后但时间在前", "other", time=anchor - 60_000)]})
        self.reader(core).run()
        row = self.corpus.iter_messages()[0]
        self.assertEqual((row["context_before"], row["context_after"]), ([], []))
        self.assertEqual(row["context_info"]["omittedInconsistentTime"], 2)
        self.assertTrue(row["context_info"]["truncated"])
        core.live = {"friend": [message(1, "超过前文时间窗口", "other", time=anchor - 61 * 60_000),
                                 message(2, "我当前这句", time=anchor),
                                 message(3, "超过后文时间窗口", "other", time=anchor + 61 * 60_000)]}
        self.corpus.reset()
        self.reader(core).run()
        row = self.corpus.iter_messages()[0]
        self.assertEqual((row["context_before"], row["context_after"]), ([], []))
        self.assertFalse(row["context_info"]["truncated"])
        self.assertEqual(row["context_info"]["omittedInconsistentTime"], 0)

    def test_long_group_speaker_metadata_is_bounded_and_marked_incomplete(self):
        name = "姓名开头" + "甲" * 500 + "姓名结尾"
        sender = "member-start-" + "x" * 500 + "-member-end"
        core = Core({"group@chatroom": [message(1, "群友的短消息", "other", senderName=name, senderId=sender),
                                        message(2, "我自己的短回复")]})
        self.reader(core).run()
        row = self.corpus.iter_messages()[0]
        entry = row["context_before"][0]
        self.assertLessEqual(len(entry["speaker"]), 205)
        self.assertIn("姓名开头", entry["speaker"])
        self.assertIn("姓名结尾", entry["speaker"])
        self.assertIn("member-start-", entry["speaker"])
        self.assertIn("-member-end", entry["speaker"])
        self.assertTrue(entry["truncated"])
        self.assertTrue(row["context_info"]["truncated"])

    def test_context_excerpts_keep_both_ends_and_full_own_text_is_never_truncated(self):
        other = "他人开头" + "中间内容" * 500 + "他人结尾"
        own = "本人开头" + "完整本人文字" * 500 + "本人结尾"
        core = Core({"friend": [message(1, other, "other"), message(2, own),
                                 message(3, "后文开头" + "后文" * 500 + "后文结尾", "other"), message(4, "本人下一句")]})
        self.reader(core).run()
        first, second = self.corpus.iter_messages()
        self.assertEqual(first["text"], own)
        excerpt = first["context_before"][0]
        self.assertTrue(excerpt["text"].startswith("他人开头"))
        self.assertTrue(excerpt["text"].endswith("他人结尾"))
        self.assertLessEqual(len(excerpt["text"]), 600)
        self.assertTrue(excerpt["truncated"])
        self.assertTrue(first["context_info"]["truncated"])
        prior_own = next(item for item in second["context_before"] if item["side"] == "self")
        self.assertTrue(prior_own["text"].startswith("本人开头"))
        self.assertTrue(prior_own["text"].endswith("本人结尾"))
        self.assertTrue(prior_own["truncated"])

    def test_media_does_not_hide_following_text_or_erase_earlier_context(self):
        core = Core({"friend": [message(1, "我最爱篮球"), message(2, "图片", "other", kind="image"),
                                 message(3, "你在开玩笑吗", "other"), message(4, "当然是开玩笑的")]})
        counts = self.reader(core).run()
        first, last = self.corpus.iter_messages()
        self.assertEqual([item["seq"] for item in first["context_after"]], [3, 4])
        self.assertEqual([item["seq"] for item in last["context_before"]], [1, 3])
        self.assertEqual((counts["messagesRead"], counts["ownMessages"], counts["excludedMessages"]), (4, 2, 1))

    def test_resume_restores_following_context_and_keeps_the_original_snapshot_ceiling(self):
        core = Core({"friend": [message(1, "你喜欢篮球吗", "other"), message(2, "我最爱篮球"),
                                 message(3, "我只是在开玩笑"), message(4, "知道了", "other")]})
        core.hook = lambda user, after: self.context.cancelled.set() if after == 2 else None
        with self.assertRaises(Cancelled):
            self.reader(core).run()
        self.assertEqual(self.corpus.counts()["messagesRead"], 2)
        self.corpus = ProfileCorpus(self.store)
        self.context.cancelled.clear()
        core.hook = None
        core.live["friend"].append(message(5, "本次开始以后才出现的新消息"))
        counts = self.reader(core).run(resume=True)
        first = self.corpus.iter_messages()[0]
        self.assertEqual([item["text"] for item in first["context_after"]], ["我只是在开玩笑", "知道了"])
        self.assertEqual((counts["messagesRead"], counts["ownMessages"]), (4, 2))
        self.assertEqual(self.corpus.rows("SELECT max(seq) n FROM profile_corpus_context")[0]["n"], 4)
        self.assertEqual(core.pages, [("friend", 0), ("friend", 2), ("friend", 2)])

    def test_repeated_sync_reuses_messages_rowids_and_corpus_revision(self):
        core = Core({"friend": [message(1, "对方问题", "other"), message(2, "我的回答")]})
        first = self.reader(core).run()
        saved = self.corpus.iter_messages()
        revision = self.corpus.meta("revision")
        core.pages.clear()
        final = self.reader(core).run()
        self.assertEqual(final, first)
        self.assertEqual(core.pages, [])
        self.assertEqual(self.corpus.iter_messages(), saved)
        self.assertEqual(self.corpus.meta("revision"), revision)

    def test_incremental_sync_reads_only_new_rows_and_extends_old_following_context(self):
        core = Core({"friend": [message(1, "我的看法")]})
        self.reader(core).run()
        original = self.corpus.iter_messages()[0]
        revision = self.corpus.meta("revision")
        core.live["friend"].extend([message(2, "对方回应", "other"), message(3, "我的补充")])
        core.pages.clear()
        counts = self.reader(core).run()
        rows = self.corpus.iter_messages()
        self.assertEqual(core.pages, [("friend", 1)])
        self.assertEqual((counts["messagesRead"], counts["ownMessages"]), (3, 2))
        self.assertEqual(rows[0]["rowid"], original["rowid"])
        self.assertEqual([item["text"] for item in rows[0]["context_after"]], ["对方回应", "我的补充"])
        self.assertGreater(self.corpus.meta("revision"), revision)

    def test_epoch_rebuild_reconciles_by_identity_without_replacing_unchanged_rowids(self):
        core = Core({"friend": [message(1, "保留的本人文字"), message(2, "将删除的本人文字"), message(3, "第三条")]})
        self.reader(core).run()
        original = {row["id"]: row["rowid"] for row in self.corpus.iter_messages()}
        core.epoch = "new-epoch"
        core.live["friend"] = [message(1, "保留的本人文字"), message(2, "第三条", id="message-3")]
        counts = self.reader(core).run()
        final = self.corpus.iter_messages()
        self.assertEqual(counts["ownMessages"], 2)
        self.assertEqual([row["text"] for row in final], ["保留的本人文字", "第三条"])
        self.assertEqual([row["rowid"] for row in final], [original[row["id"]] for row in final])

    def test_rebuild_pause_preserves_committed_messages_and_resumes_staging(self):
        core = Core({"friend": [message(1, "旧内容"), message(2, "第二条"), message(3, "第三条")]})
        self.reader(core).run()
        saved = self.corpus.iter_messages()
        core.epoch = "epoch-2"
        core.live["friend"][0]["text"] = "新内容"
        core.hook = lambda user, after: self.context.cancelled.set() if after == 2 else None
        with self.assertRaises(Cancelled):
            self.reader(core).run()
        self.assertEqual(self.corpus.iter_messages(), saved)
        self.context.cancelled.clear()
        core.hook = None
        core.pages.clear()
        self.reader(core).run(resume=True)
        self.assertEqual(core.pages, [("friend", 2)])
        self.assertEqual(self.corpus.iter_messages()[0]["text"], "新内容")
        self.assertEqual([row["rowid"] for row in self.corpus.iter_messages()], [row["rowid"] for row in saved])

    def test_same_length_projection_revision_change_rechecks_only_that_chat(self):
        core = Core({"a": [message(1, "原内容")], "b": [message(1, "另一聊天")]})
        self.reader(core).run()
        core.live["a"][0]["text"] = "修改后的内容"
        original_status = core.history_status
        core.history_status = lambda account, user: {**original_status(account, user),
                                                     **({"revision": 9} if user == "a" else {})}
        core.pages.clear()
        self.reader(core).run()
        self.assertEqual(core.pages, [("a", 0)])
        self.assertEqual(self.corpus.iter_messages()[0]["text"], "修改后的内容")

    def test_complete_catalog_removes_deleted_chats_but_partial_catalog_keeps_them(self):
        core = Core({"a": [message(1, "一")], "b": [message(1, "二")]})
        self.reader(core).run()
        original = core.history_conversations
        core.history_conversations = lambda account, after=None, limit=500: {
            "sourceFingerprint": core.source, "revision": "directory-2", "next": None,
            "coverage": {"state": "partial"}, "conversations": [{"user": "a", "name": "合成聊天"}]}
        self.reader(core).run()
        self.assertEqual(self.corpus.counts()["ownMessages"], 2)
        core.history_conversations = lambda account, after=None, limit=500: {
            "sourceFingerprint": core.source, "revision": "directory-3", "next": None,
            "coverage": {"state": "complete"}, "conversations": [{"user": "a", "name": "合成聊天"}]}
        self.reader(core).run()
        self.assertEqual(self.corpus.counts()["ownMessages"], 1)
        core.history_conversations = original

    def test_new_source_is_rejected_without_losing_the_committed_corpus(self):
        core = Core({"friend": [message(1, "原来源文字")]})
        self.reader(core).run()
        saved = self.corpus.iter_messages()
        core.source = "another-source"
        with self.assertRaises(SelfError) as caught:
            self.reader(core).run()
        self.assertEqual(caught.exception.code, "source-changed")
        self.assertEqual(self.corpus.iter_messages(), saved)

    def test_source_body_edit_with_same_projection_count_triggers_only_affected_chat_rebuild(self):
        core = AuditedCore({"a": [message(1, "旧观点")], "b": [message(1, "未修改的观点")]})
        self.reader(core).run()
        saved = {row["id"]: row["rowid"] for row in self.corpus.iter_messages()}
        core.originals["a"][0]["text"] = "新观点"
        core.source_revision = "source-revision-2"
        core.pages.clear()
        self.reader(core).run()
        self.assertEqual(core.rebuilds, ["a"])
        self.assertEqual(core.pages, [("a", 0)])
        final = self.corpus.iter_messages()
        self.assertEqual([row["text"] for row in final], ["新观点", "未修改的观点"])
        self.assertEqual([row["rowid"] for row in final], [saved[row["id"]] for row in final])

    def test_old_edit_and_new_tail_in_same_sync_cannot_be_mistaken_for_append_only(self):
        core = AuditedCore({"friend": [message(1, "旧观点"), message(2, "旧回应", "other")]})
        self.reader(core).run()
        core.originals["friend"][0]["text"] = "已修改的旧观点"
        core.originals["friend"].append(message(3, "新增观点"))
        core.source_revision = "source-revision-2"
        self.reader(core).run()
        self.assertEqual(core.rebuilds, ["friend"])
        self.assertEqual([row["text"] for row in self.corpus.iter_messages()], ["已修改的旧观点", "新增观点"])

    def test_old_insertions_are_detected_by_the_same_source_prefix(self):
        core = AuditedCore({"friend": [message(1, "原消息"), message(2, "仍在原位置的尾消息", "other")]})
        self.reader(core).run()
        core.originals["friend"][1]["seq"] = 3
        core.originals["friend"].insert(1, message(2, "新增的较早本人文字", id="old-inserted"))
        core.source_revision = "source-revision-2"
        self.reader(core).run()
        self.assertEqual(core.rebuilds, ["friend"])

    def test_unready_verification_blocks_the_run_and_preserves_committed_corpus(self):
        core = AuditedCore({"friend": [message(1, "已有资料")]})
        self.reader(core).run()
        saved = self.corpus.iter_messages()
        core.source_revision = "source-revision-2"
        core.verify_state = "unready"
        with self.assertRaises(SelfError) as caught:
            self.reader(core).run()
        self.assertEqual(caught.exception.code, "verification-unavailable")
        self.assertEqual(self.corpus.iter_messages(), saved)
        self.assertEqual(core.rebuilds, [])

    def test_staged_rebuild_failure_blocks_generation_and_keeps_the_old_corpus(self):
        core = AuditedCore({"friend": [message(1, "旧内容"), message(2, "第二条"), message(3, "第三条")]})
        self.reader(core).run()
        saved = self.corpus.iter_messages()
        core.originals["friend"][0]["text"] = "已更改的内容"
        core.source_revision = "source-revision-2"
        original = core.history_messages

        def unreadable(account, user, after=0, limit=500):
            if after:
                raise RuntimeError("PRIVATE_SOURCE_ERROR")
            return original(account, user, after, limit)

        core.history_messages = unreadable
        counts = self.reader(core).run()
        self.assertEqual(counts["failedConversations"], 1)
        self.assertIn({"kind": "unreadable-conversations", "count": 1}, counts["gaps"])
        self.assertNotIn("PRIVATE", json.dumps(counts))
        self.assertEqual(self.corpus.iter_messages(), saved)
        self.assertTrue(self.corpus.round_open())

    def test_irrelevant_message_volume_does_not_expand_source_verification(self):
        core = AuditedCore({"friend": [message(index, "无关的对方文字", "other") for index in range(1, 1001)] +
                                     [message(1001, "唯一的本人文字")]})
        core.page_size = 500
        self.reader(core).run()
        core.source_revision = "source-revision-2"
        core.pages.clear()
        self.reader(core).run()
        verified = sum(len(group["items"]) for group in core.verifications)
        self.assertEqual(verified, 13)
        self.assertEqual(core.pages, [])
        self.assertEqual(core.rebuilds, [])

    def test_unchanged_source_metadata_skips_all_source_body_verification(self):
        core = AuditedCore({"friend": [message(1, "已有资料"), message(2, "对方回应", "other")]})
        self.reader(core).run()
        core.pages.clear()
        self.reader(core).run()
        self.assertEqual(core.verifications, [])
        self.assertEqual(core.pages, [])

    def directory_core(self, live):
        core = AuditedCore(live)
        core.directory_latest = True
        self.reader(core).run()
        core.source_revision = "source-revision-2"
        core.pages.clear()
        core.prepared.clear()
        core.verifications.clear()
        return core

    def test_directory_latest_skips_unchanged_chats_without_rereading_or_reverifying(self):
        core = self.directory_core({"a": [message(1, "甲"), message(2, "回应", "other")], "b": [message(1, "乙")]})
        saved = [row for row in self.corpus.iter_messages() if row["user"] == "a"]
        core.originals["b"].append(message(2, "乙新增"))
        counts = self.reader(core).run()
        self.assertEqual((counts["readConversations"], counts["failedConversations"]), (2, 0))
        self.assertEqual(core.prepared, ["b"])
        self.assertEqual(core.pages, [("b", 1)])
        self.assertEqual({group["user"] for group in core.verifications}, {"b"})
        self.assertEqual([row for row in self.corpus.iter_messages() if row["user"] == "a"], saved)
        self.assertIn("乙新增", [row["text"] for row in self.corpus.iter_messages()])
        self.assertFalse(self.corpus.round_open())

    def test_unchanged_chats_are_confirmed_in_batches_without_per_chat_audits(self):
        live = {f"chat-{index:03d}": [message(1, f"本人{index}")] for index in range(4)}
        core = self.directory_core(live)
        core.audits.clear()
        core.status_batches.clear()
        core.originals["chat-001"].append(message(2, "新增"))
        with patch("self_profile_corpus.STATUS_BATCH", 2):
            counts = self.reader(core).run()
        self.assertEqual(counts["readConversations"], 4)
        self.assertEqual(core.status_batches, [["chat-000", "chat-002"], ["chat-003"]])
        self.assertEqual(core.prepared, ["chat-001"])
        self.assertEqual({user for user, _ceiling in core.audits}, {"chat-001"})

    def test_recent_reuse_batches_projection_checks_and_reads_only_a_changed_chat_without_an_event(self):
        core = self.directory_core({f'chat-{index:03d}': [message(1, f'本人{index}')] for index in range(4)})
        saved = [row for row in self.corpus.iter_messages() if row['user'] != 'chat-002']
        core.originals['chat-002'].append(message(2, '已进入Core投影的新发言'))
        core.history_prepare('synthetic-account', 'chat-002', refresh=True)
        core.prepared.clear()
        core.audits.clear()
        core.status_batches.clear()
        self.assertTrue(self.corpus.recently_synced())
        with patch('self_profile_corpus.STATUS_BATCH', 2):
            counts = self.reader(core, reuse_recent=True).run()
        self.assertEqual(counts['ownMessages'], 5)
        self.assertEqual(core.status_batches, [['chat-000', 'chat-001'], ['chat-002', 'chat-003'], ['chat-002']])
        self.assertEqual(core.prepared, ['chat-002'])
        self.assertEqual(core.pages, [('chat-002', 1)])
        self.assertEqual({user for user, _ in core.audits}, {'chat-002'})
        self.assertEqual([row for row in self.corpus.iter_messages() if row['user'] != 'chat-002'], saved)

    def test_recent_reuse_checks_rebuilt_chats_beyond_the_first_one(self):
        core = self.directory_core({'a': [message(1, '甲')], 'b': [message(1, '乙')]})
        core.epochs['b'] = 7
        self.reader(core, reuse_recent=True).run()
        self.assertEqual(core.prepared, ['b'])
        self.assertEqual(self.corpus.rows("SELECT epoch FROM profile_corpus_conversations WHERE user='b'")[0]['epoch'], 'epoch-7')

    def test_recent_reuse_uses_only_batch_metadata_when_unchanged_and_rejects_a_source_switch(self):
        core = self.directory_core({'a': [message(1, '甲')], 'b': [message(1, '乙')]})
        core.audits.clear()
        core.status_batches.clear()
        before = self.corpus.iter_messages()
        self.reader(core, reuse_recent=True).run()
        self.assertEqual(core.status_batches, [['a', 'b']])
        self.assertEqual((core.prepared, core.pages, core.audits, core.verifications), ([], [], [], []))
        original = core.history_status_many
        core.history_status_many = lambda account, users: {**original(account, users), 'currentSourceFingerprint': 'different-source'}
        with self.assertRaises(SelfError) as caught:
            self.reader(core, reuse_recent=True).run()
        self.assertEqual(caught.exception.code, 'source-changed')
        self.assertEqual(self.corpus.iter_messages(), before)
        self.assertEqual(core.prepared, [])

    def test_recent_reuse_rechecks_unready_or_changed_projection_metadata_even_with_the_same_revision(self):
        core = self.directory_core({'a': [message(1, '甲')], 'b': [message(1, '乙')]})
        original = core.history_status_many
        saved = self.corpus.iter_messages()
        for changed in ({'state': 'reading'}, {'importing': True}, {'error': 'synthetic-error'},
                        {'highwater': 'different-position'}, {'watermark': {'seq': 2}}, {'totalCount': 2}):
            with self.subTest(changed=changed):
                def statuses(account, users):
                    response = original(account, users)
                    response['results'] = [{**status, **changed} if status['user'] == 'b' else status
                                           for status in response['results']]
                    return response
                core.history_status_many = statuses
                core.prepared.clear()
                self.reader(core, reuse_recent=True).run()
                self.assertEqual(core.prepared, ['b'])
                self.assertEqual(self.corpus.iter_messages(), saved)

    def test_recent_reuse_does_not_accept_a_nonfirst_chat_from_another_source(self):
        core = self.directory_core({'a': [message(1, '甲')], 'b': [message(1, '乙')]})
        saved = self.corpus.iter_messages()
        original = core.history_status
        core.history_status = lambda account, user, **options: {
            **original(account, user, **options), **({'sourceFingerprint': 'different-source'} if user == 'b' else {})}
        with self.assertRaises(SelfError) as caught:
            self.reader(core, reuse_recent=True).run()
        self.assertEqual(caught.exception.code, 'source-changed')
        self.assertEqual(self.corpus.iter_messages(), saved)

    def test_recent_reuse_keeps_a_failed_reopened_chat_resumable(self):
        core = self.directory_core({'a': [message(1, '甲')], 'b': [message(1, '乙')]})
        full_audit_closed = self.corpus.meta('roundClosedAt')
        core.originals['b'].append(message(2, '新增'))
        core.history_prepare('synthetic-account', 'b', refresh=True)
        original = core.history_prepare
        def unavailable(*args, **kwargs):
            raise RuntimeError('synthetic failure')
        core.history_prepare = unavailable
        counts = self.reader(core, reuse_recent=True).run()
        self.assertEqual(counts['failedConversations'], 1)
        self.assertTrue(self.corpus.round_open())
        core.history_prepare = original
        counts = self.reader(core, reuse_recent=True).run()
        self.assertEqual((counts['failedConversations'], counts['ownMessages']), (0, 3))
        self.assertFalse(self.corpus.round_open())
        self.assertEqual(self.corpus.meta('roundClosedAt'), full_audit_closed)

    def test_recent_reuse_does_not_apply_partial_batches_after_cancellation(self):
        core = self.directory_core({f'chat-{index}': [message(1, f'本人{index}')] for index in range(4)})
        before = self.corpus.iter_messages()
        original = core.history_status_many
        calls = []
        def statuses(account, users):
            calls.append(users)
            if len(calls) == 2:
                raise Cancelled()
            response = original(account, users)
            response['results'][0]['revision'] += 1
            return response
        core.history_status_many = statuses
        with patch('self_profile_corpus.STATUS_BATCH', 2), self.assertRaises(Cancelled):
            self.reader(core, reuse_recent=True).run()
        self.assertEqual(core.prepared, [])
        self.assertEqual(self.corpus.iter_messages(), before)
        self.assertFalse(self.corpus.rows("SELECT 1 FROM profile_corpus_conversations WHERE state<>'done'"))

    def test_batched_skip_rereads_a_chat_whose_projection_was_rebuilt(self):
        core = self.directory_core({"a": [message(1, "甲")], "b": [message(1, "乙")]})
        core.epochs["b"] = 7
        self.reader(core).run()
        self.assertEqual(core.prepared, ["b"])

    def test_batched_skip_stops_when_the_source_switched(self):
        core = self.directory_core({"a": [message(1, "甲")]})
        original = core.history_status_many
        core.history_status_many = lambda account, users: {**original(account, users), "currentSourceFingerprint": "other"}
        with self.assertRaises(SelfError) as caught:
            self.reader(core).run()
        self.assertEqual(caught.exception.code, "source-changed")

    def test_core_without_the_batch_route_falls_back_to_prefix_audits(self):
        core = self.directory_core({"a": [message(1, "甲"), message(2, "回应", "other")]})

        def missing(_account, _users):
            from kyf_plugin.core import CoreError
            raise CoreError(404, "not-found", "接口不存在")

        core.history_status_many = missing
        core.originals["a"][0]["id"] = "replaced-message"
        self.reader(core).run()
        self.assertEqual(core.rebuilds, ["a"])

    def test_changed_read_prefix_is_not_skipped_even_when_the_newest_row_is_the_same(self):
        # Only the per-chat prefix audit sees this; batched skipping leaves it for the chat's next change.
        core = self.directory_core({"a": [message(1, "甲"), message(2, "回应", "other")]})
        core.history_status_many = None
        core.originals["a"][0]["id"] = "replaced-message"
        self.reader(core).run()
        self.assertEqual(core.rebuilds, ["a"])
        batched = self.directory_core({"b": [message(1, "乙"), message(2, "回应", "other")]})
        batched.originals["b"][0]["id"] = "replaced-message"
        self.reader(batched).run()
        self.assertEqual((batched.rebuilds, batched.prepared), ([], []))

    def test_skipped_chat_keeps_saved_text_after_an_in_place_edit(self):
        # Accepted trade-off: an in-place edit behind an unchanged prefix waits for the chat's next change.
        core = self.directory_core({"a": [message(1, "旧观点")]})
        core.originals["a"][0]["text"] = "新观点"
        self.reader(core).run()
        self.assertEqual((core.prepared, core.verifications, core.rebuilds), ([], [], []))
        self.assertEqual([row["text"] for row in self.corpus.iter_messages()], ["旧观点"])

    def test_source_without_prefix_audit_is_still_verified(self):
        core = self.directory_core({"a": [message(1, "甲")]})
        core.history_status_many = None
        original = core.history_status
        core.history_status = lambda account, user, **options: {
            key: value for key, value in original(account, user, **options).items() if key != "sourcePrefixSignature"}
        self.reader(core).run()
        self.assertEqual(core.prepared, ["a"])

    def test_corpus_without_directory_positions_reads_the_directory_again(self):
        self.reader(AuditedCore({"a": [message(1, "甲")]})).run()
        self.assertTrue(self.corpus.meta("catalogComplete"))
        self.store.db.execute("ALTER TABLE profile_corpus_conversations DROP COLUMN latest")
        self.store.db.commit()
        self.assertFalse(ProfileCorpus(self.store).meta("catalogComplete"))

    def test_partial_rebuild_cannot_delete_messages_from_the_committed_corpus(self):
        core = AuditedCore({"friend": [message(1, "已有第一条资料"), message(2, "已有第二条资料")]})
        self.reader(core).run()
        saved = self.corpus.iter_messages()
        core.originals["friend"][0]["text"] = "修改后但来源不完整"
        core.source_revision = "source-revision-2"
        core.extra_status = {"partial": True, "totalCount": 4}
        counts = self.reader(core).run()
        self.assertEqual(counts["failedConversations"], 1)
        self.assertEqual(self.corpus.iter_messages(), saved)

    def test_one_failed_chat_does_not_stop_the_round_and_is_retried_next_run(self):
        core = AuditedCore({"a": [message(1, "甲")], "b": [message(1, "乙已有")], "c": [message(1, "丙")]})
        self.reader(core).run()
        saved = [row for row in self.corpus.iter_messages() if row["user"] == "b"]
        core.source_revision = "source-revision-2"
        core.originals["b"][0]["text"] = "乙已更改"
        original = core.history_messages

        def unreadable(account, user, after=0, limit=500):
            if user == "b":
                raise RuntimeError("PRIVATE_SOURCE_ERROR")
            return original(account, user, after, limit)

        core.history_messages = unreadable
        counts = self.reader(core).run()
        self.assertEqual((counts["readConversations"], counts["failedConversations"]), (2, 1))
        self.assertEqual([row for row in self.corpus.iter_messages() if row["user"] == "b"], saved)
        self.assertTrue(self.corpus.round_open())
        core.history_messages = original
        core.verifications.clear()
        rounds = self.corpus.meta("syncRound")
        counts = self.reader(core).run()
        self.assertEqual((counts["readConversations"], counts["failedConversations"]), (3, 0))
        self.assertEqual(self.corpus.meta("syncRound"), rounds)
        self.assertEqual({group["user"] for group in core.verifications} - {"b"}, set())
        self.assertIn("乙已更改", [row["text"] for row in self.corpus.iter_messages()])
        self.assertFalse(self.corpus.round_open())

    def test_rerun_after_interruption_skips_chats_finished_this_round(self):
        core = AuditedCore({"a": [message(1, "甲")], "b": [message(1, "乙")], "c": [message(1, "丙")]})
        self.reader(core).run()
        core.source_revision = "source-revision-2"
        self.cancel_verifying(core, "c")
        with self.assertRaises(Cancelled):
            self.reader(core).run()
        self.assertEqual(self.corpus.counts()["readConversations"], 2)
        verified = {group["user"] for group in core.verifications}
        core.verifications.clear()
        core.pages.clear()
        del core.history_verify_many
        self.context.cancelled.clear()
        self.progress.clear()
        counts = self.reader(core).run()
        self.assertEqual(counts["readConversations"], 3)
        self.assertEqual(self.progress[0]["readConversations"], 2)
        self.assertTrue({"a", "b"} <= verified)
        self.assertEqual({group["user"] for group in core.verifications} & {"a", "b"}, set())
        self.assertFalse(self.corpus.round_open())

    def test_rerun_reopens_a_finished_chat_that_received_new_messages(self):
        core = AuditedCore({"a": [message(1, "甲")], "b": [message(1, "乙")]})
        self.reader(core).run()
        core.source_revision = "source-revision-2"
        self.cancel_verifying(core, "b")
        with self.assertRaises(Cancelled):
            self.reader(core).run()
        del core.history_verify_many
        self.context.cancelled.clear()
        core.originals["a"].append(message(2, "甲新增"))
        core.source_revision = "source-revision-3"
        self.store.mark_stale("a")
        core.pages.clear()
        self.reader(core).run()
        self.assertIn(("a", 1), core.pages)
        self.assertIn("甲新增", [row["text"] for row in self.corpus.iter_messages()])

    def test_rerun_after_restart_keeps_finished_chats_despite_a_new_global_source_revision(self):
        # The source revision covers all shards and the source process; a restart changes it for every chat.
        core = AuditedCore({"a": [message(1, "甲")], "b": [message(1, "乙")], "c": [message(1, "丙")]})
        self.reader(core).run()
        core.source_revision = "source-revision-2"
        self.cancel_verifying(core, "c")
        with self.assertRaises(Cancelled):
            self.reader(core).run()
        self.context.cancelled.clear()
        core.source_revision = "source-revision-after-restart"
        del core.history_verify_many
        core.verifications.clear()
        self.progress.clear()
        self.assertEqual(self.reader(core).run()["readConversations"], 3)
        self.assertEqual(self.progress[0]["readConversations"], 2)
        self.assertEqual({group["user"] for group in core.verifications} & {"a", "b"}, set())

    def test_append_only_audited_source_uses_the_checkpoint_and_preserves_rowids(self):
        core = AuditedCore({"friend": [message(1, "第一条本人文字")]})
        self.reader(core).run()
        rowid = self.corpus.iter_messages()[0]["rowid"]
        core.originals["friend"].extend([message(2, "对方新回应", "other"), message(3, "第二条本人文字")])
        core.source_revision = "source-revision-2"
        core.pages.clear()
        self.reader(core).run()
        self.assertEqual(core.pages, [("friend", 1)])
        self.assertEqual(core.rebuilds, [])
        self.assertEqual(self.corpus.iter_messages()[0]["rowid"], rowid)

    def test_new_own_text_checks_previously_unused_old_context_before_analysis(self):
        core = AuditedCore({"friend": [message(index, "暂未进入任何本人消息前后文", "other") for index in range(1, 21)]})
        core.page_size = 500
        self.reader(core).run()
        self.assertEqual(self.corpus.counts()["ownMessages"], 0)
        core.originals["friend"][18]["text"] = "旧对方文字已修改"
        core.originals["friend"].append(message(21, "后来才有的本人回答"))
        core.source_revision = "source-revision-2"
        self.reader(core).run()
        self.assertEqual(core.rebuilds, ["friend"])
        first = self.corpus.iter_messages()[0]
        self.assertIn("旧对方文字已修改", first["context"])
        self.assertNotIn("暂未进入", first["context_before"][-2]["text"])

    def test_resuming_an_old_corpus_version_rebuilds_context_from_the_source(self):
        core = Core({"friend": [message(1, "旧的本人消息")]})
        self.reader(core).run()
        with self.store.lock, self.store.db:
            self.corpus._meta("version", CORPUS_VERSION - 1)
            self.store.db.execute("DELETE FROM profile_corpus_context")
        core.live["friend"] += [message(2, "后来的补充说明", "other"), message(3, "我是在转述朋友的话")]
        self.corpus = ProfileCorpus(self.store)
        counts = self.reader(core).run(resume=True)
        self.assertEqual(self.corpus.meta("version"), CORPUS_VERSION)
        self.assertEqual((counts["messagesRead"], counts["ownMessages"]), (3, 2))
        self.assertEqual([item["seq"] for item in self.corpus.iter_messages()[0]["context_after"]], [2, 3])
        self.assertEqual(core.pages, [("friend", 0), ("friend", 0), ("friend", 2)])

    def test_committed_counts_follow_append_rebuild_chat_removal_and_reset(self):
        core = AuditedCore({"friend": [message(1, "本人第一句"), message(2, "对方一句", "other"), message(3, "本人第二句")],
                            "group@chatroom": [message(1, "群聊本人一句"), message(2, "群友一句", "other")]})
        self.reader(core).run()
        self.assertEqual(tuple(self.corpus.counts()[key] for key in ("ownMessages", "privateMessages", "groupMessages")), (3, 2, 1))
        core.originals["friend"].append(message(4, "本人追加一句"))
        core.source_revision = "source-revision-2"
        self.reader(core).run()
        self.assertEqual(self.corpus.counts()["ownMessages"], 4)
        core.originals["friend"][0] = message(1, "原来这句属于对方", "other")
        core.source_revision = "source-revision-3"
        self.reader(core).run()
        self.assertEqual(tuple(self.corpus.counts()[key] for key in ("ownMessages", "privateMessages", "groupMessages")), (3, 2, 1))
        del core.originals["group@chatroom"]
        del core.live["group@chatroom"]
        self.reader(core).run()
        self.assertEqual(tuple(self.corpus.counts()[key] for key in ("ownMessages", "privateMessages", "groupMessages")), (2, 2, 0))
        self.corpus.reset()
        self.assertEqual(tuple(self.corpus.counts()[key] for key in ("ownMessages", "privateMessages", "groupMessages")), (0, 0, 0))

    def test_counter_migration_backfills_existing_rows_once_without_source_reread(self):
        core = Core({"friend": [message(1, "已有本人第一句"), message(2, "已有本人第二句")],
                     "group@chatroom": [message(1, "已有群聊本人一句")]})
        self.reader(core).run()
        saved, pages = self.corpus.iter_messages(), len(core.pages)
        with self.store.lock, self.store.db:
            self.store.db.execute("ALTER TABLE profile_corpus_conversations DROP COLUMN own_count")
            self.store.db.execute("ALTER TABLE profile_corpus_conversations DROP COLUMN context_count")
            self.store.db.execute("DELETE FROM profile_corpus_meta WHERE key='countsVersion'")
        self.corpus = ProfileCorpus(self.store)
        self.assertEqual(self.corpus.iter_messages(), saved)
        self.assertEqual(tuple(self.corpus.counts()[key] for key in ("ownMessages", "privateMessages", "groupMessages")), (3, 2, 1))
        self.reader(core).run()
        self.assertEqual(len(core.pages), pages)

    def test_counts_only_scans_conversation_metadata_not_the_message_corpus(self):
        self.reader(Core({"friend": [message(seq, "合成本人文字") for seq in range(1, 101)]})).run()
        queries = []
        self.store.db.set_trace_callback(queries.append)
        try:
            counts = self.corpus.counts()
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual(counts["ownMessages"], 100)
        self.assertFalse(any("FROM profile_corpus_messages" in query or "JOIN profile_corpus_messages" in query
                             for query in queries), "progress must not rescan all saved messages on every page")

    def test_page_failure_rolls_back_both_message_rows_and_transactional_counts(self):
        core = AuditedCore({"friend": [message(1, "已经提交的本人文字")]})
        self.reader(core).run()
        core.originals["friend"].append(message(2, "不得提交的下一页文字"))
        core.source_revision = "source-revision-2"
        with self.store.lock, self.store.db:
            self.store.db.execute("CREATE TRIGGER synthetic_context_failure BEFORE INSERT ON profile_corpus_context "
                                  "BEGIN SELECT RAISE(ABORT,'synthetic page rollback'); END")
        self.assertEqual(self.reader(core).run()["failedConversations"], 1)
        self.assertEqual(self.corpus.counts()["ownMessages"], 1)
        self.assertEqual(self.corpus.rows("SELECT count(*) n FROM profile_corpus_messages")[0]["n"], 1)

    def test_progress_reports_only_this_readers_source_pages_and_current_chat_position(self):
        updates = []
        self.context.progress = lambda **values: updates.append(values)
        core = Core({"synthetic-user-must-not-appear": [message(1, "本人文字"), message(2, "对方文字", "other"),
                                                       message(3, "媒体", kind="image")]})
        self.reader(core).run()
        self.assertEqual(updates[-1]["sourceMessagesRead"], 3)
        self.assertEqual((updates[-1]["currentChatDone"], updates[-1]["currentChatTotal"]), (3, 3))
        self.assertEqual((updates[-1]["verifiedConversationsDone"], updates[-1]["verifiedConversationsTotal"]), (1, 1))
        self.assertEqual((updates[-1]["phase"], updates[-1]["unit"]), ("reading", "个聊天"))
        self.assertNotIn("synthetic-user-must-not-appear", json.dumps(updates))
        core.pages.clear()
        self.reader(core).run()
        self.assertEqual(updates[-1]["sourceMessagesRead"], 0)
        self.assertEqual(core.pages, [])
        core.live["synthetic-user-must-not-appear"].append(message(4, "追加的本人文字"))
        self.reader(core).run()
        self.assertEqual(updates[-1]["sourceMessagesRead"], 1)
        self.assertEqual((updates[-1]["currentChatDone"], updates[-1]["currentChatTotal"]), (4, 4))
        self.assertEqual(self.corpus.counts()["messagesRead"], 4)

    def test_progress_resets_local_read_count_when_resuming_a_cancelled_chat(self):
        updates = []
        self.context.progress = lambda **values: updates.append(values)
        core = Core({"friend": [message(seq, "合成文本") for seq in range(1, 7)]})
        core.hook = lambda user, after: self.context.cancelled.set() if after == 2 else None
        with self.assertRaises(Cancelled):
            self.reader(core).run()
        self.assertEqual((updates[-1]["sourceMessagesRead"], updates[-1]["currentChatDone"], updates[-1]["currentChatTotal"]),
                         (2, 2, 6))
        self.context.cancelled.clear()
        core.hook = None
        self.reader(core).run(resume=True)
        self.assertEqual((updates[-1]["sourceMessagesRead"], updates[-1]["currentChatDone"], updates[-1]["currentChatTotal"]),
                         (4, 6, 6))
        self.assertEqual(self.corpus.counts()["messagesRead"], 6)

    def test_source_reverification_reports_progress_without_claiming_new_reads(self):
        updates = []
        self.context.progress = lambda **values: updates.append(values)
        core = AuditedCore({"friend": [message(seq, "已有合成文字") for seq in range(1, 7)]})
        self.reader(core).run()
        core.pages.clear()
        updates.clear()
        core.source_revision = "source-revision-2"
        self.reader(core).run()
        verified = [item for item in updates if item.get("phase") == "verifying"]
        self.assertTrue(verified)
        self.assertEqual(verified[-1]["sourceMessagesVerified"], 6)
        self.assertEqual(verified[-1]["sourceMessagesRead"], 0)
        self.assertEqual(core.pages, [])

    def test_dense_group_proof_window_matches_actual_context_neighbors_across_media_and_unknown_times(self):
        rows = [message(seq, "合成文本", "self" if 800 <= seq < 2400 and seq % 2 == 0 else "other",
                        kind="image" if seq % 17 == 0 else "text", time=0 if seq % 19 == 0 else 1_700_000_000_000 + seq)
                for seq in range(1, 3001)]
        core = AuditedCore({"group@chatroom": rows})
        core.page_size = 500
        queries = []
        self.store.db.set_trace_callback(queries.append)
        try:
            self.reader(core).run()
        finally:
            self.store.db.set_trace_callback(None)
        context = [row for row in rows if row["kind"] == "text"]
        expected = set()
        for index, row in enumerate(context):
            if row["side"] == "self":
                expected.update(item["seq"] for item in context[max(0, index - 12):index + 7])
        actual = {row["seq"] for row in self.corpus.rows("SELECT seq FROM profile_corpus_proofs WHERE relevant=1")}
        self.assertEqual(actual, expected)
        self.assertTrue(any("ROWS BETWEEN 6 PRECEDING AND 12 FOLLOWING" in query for query in queries))
        self.assertTrue(all(row["kind"] != "image" for row in rows if row["seq"] in actual))
        self.assertTrue(any(row["time"] == 0 for row in rows if row["seq"] in actual))

    def test_proof_page_looks_up_sync_round_once_per_page(self):
        core = AuditedCore({"group@chatroom": [message(seq, "合成文本", "other") for seq in range(1, 201)]})
        core.page_size = 100
        queries = []
        self.store.db.set_trace_callback(queries.append)
        try:
            self.reader(core).run()
        finally:
            self.store.db.set_trace_callback(None)
        lookups = [query for query in queries if "SELECT value FROM profile_corpus_meta WHERE key='syncRound'" in query]
        # begin() reads the previous round once, then each persisted page reads it once.
        self.assertEqual(len(lookups), 1 + len(core.pages))
        self.assertEqual(self.corpus.rows("SELECT DISTINCT verified_round FROM profile_corpus_proofs"), [{"verified_round": 1}])

    def test_sparse_proof_pagination_uses_the_sequence_index_without_repeated_sorting(self):
        rows = [message(seq, "合成文本", "self" if seq % 10 == 0 else "other") for seq in range(1, 2501)]
        core = AuditedCore({"group@chatroom": rows})
        core.page_size = 500
        queries = []
        self.store.db.set_trace_callback(queries.append)
        try:
            self.reader(core).run()
        finally:
            self.store.db.set_trace_callback(None)
        self.assertEqual(len(self.corpus.rows("SELECT seq FROM profile_corpus_proofs WHERE relevant=1")), 2500)
        self.assertFalse(any("ROWS BETWEEN" in query for query in queries))
        pages = [query for query in queries if "SELECT id,seq FROM profile_corpus_messages" in query]
        self.assertTrue(pages)
        for query in pages:
            plan = self.corpus.rows("EXPLAIN QUERY PLAN " + query)
            self.assertFalse(any("TEMP B-TREE" in row["detail"] for row in plan))


if __name__ == "__main__":
    unittest.main()
