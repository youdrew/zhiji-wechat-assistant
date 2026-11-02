"""Synthetic Core projection boundary regressions moved out of the advisor plugin."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from core_account_db import AccountStores
from core_projection import ContextProjection
from core_contracts import AdvisorError as CoreDataError
from history_browser import encode_cursor
from wechat_source import WeChatSource
from test_support import FakeSource, make_message, wait_until
ACCOUNT, USER = "synthetic-account-1", "user-a"

class CoreProjectionBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = FakeSource()
        self.stores = AccountStores(self.root)
        self.projection = ContextProjection(self.stores, self.source)
        self.addCleanup(self.stores.close)
        self.addCleanup(self.projection.close)

    def import_ready(self, user=USER):
        self.projection.prepare(ACCOUNT, user)
        row = wait_until(lambda: self.projection.snapshot(ACCOUNT, user)
                        if self.projection.snapshot(ACCOUNT, user)["state"] in ("ready", "error") else None)
        self.assertIsNotNone(row)
        return row

    def test_empty_filtered_page_advances_to_later_text(self):
        self.source.add_messages(USER, [make_message(257)])
        original = self.source.history_page
        def page(user, highwater, after=None, page_size=256):
            if after is None:
                return [], (256, "message__message_1.db", 256)
            return original(user, highwater, after, page_size)
        self.source.history_page = page
        ready = self.import_ready()
        self.assertEqual((ready["state"], ready["readCount"]), ("ready", 1))
        self.assertIn("257", self.projection.messages(ACCOUNT, USER)[0]["text"])


    def test_full_original_long_message_and_quote_identity_survive(self):
        sender = "member-" + "x" * 160
        text = "原文" * 12000 + "不可遗漏的末尾"
        quote = {"id": "quote1", "senderId": sender, "senderName": "成员",
                 "text": "被引用的原文", "sentAtMs": 1700000000000}
        item = make_message(1, text=text, sender=sender)
        item["quote"] = quote
        self.source.add_messages("room@chatroom", [item])
        self.import_ready("room@chatroom")
        row = self.projection.messages(ACCOUNT, "room@chatroom")[0]
        self.assertEqual(row["text"], text)
        self.assertEqual(row["sender_id"], sender)
        self.assertEqual(json.loads(row["raw"])["inputMeta"]["quote"], quote)


    def test_invalid_row_is_error_not_silent_gap(self):
        invalid = make_message(1)
        invalid["_sort"] = None
        self.source.history_highwater = lambda _user: (3, "message__message_1.db", 3)
        self.source.history_page = lambda *_args, **_kwargs: ([invalid, make_message(3)], None)
        result = self.import_ready()
        self.assertEqual((result["state"], result["readCount"]), ("error", 0))
        self.assertEqual(self.projection.messages(ACCOUNT, USER), [])


    def test_repeated_page_is_deduped_by_message_identity_and_seq_is_dense(self):
        self.source.add_messages(USER, [make_message(1), make_message(2)])
        def page(user, highwater, after=None, page_size=256):
            if after is None:
                return [make_message(1)], (1, "message__message_1.db", 1)
            if after[0] == 1:
                return [make_message(1), make_message(2)], (2, "message__message_1.db", 1)
            return [], None
        self.source.history_page = page
        result = self.import_ready()
        self.assertEqual(result["readCount"], 2)
        self.assertEqual([row["seq"] for row in self.projection.messages(ACCOUNT, USER)], [1, 2])


    def test_workdir_change_rebuilds_source(self):
        self.source.seed_pages(USER, 2)
        self.import_ready()
        old = self.stores.account(ACCOUNT).context(USER)
        self.source.workdir = "C:/synthetic-new-workdir"
        self.source.users[USER] = [make_message(1, text="new root content")]
        self.import_ready()
        database = self.stores.account(ACCOUNT)
        self.assertNotEqual(database.context(USER)["epoch"], old["epoch"])
        self.assertEqual([row["text"] for row in self.projection.messages(ACCOUNT, USER)], ["new root content"])


    def test_highwater_regression_rebuilds_old_tail(self):
        self.source.seed_pages(USER, 5)
        self.import_ready()
        old = self.stores.account(ACCOUNT).context(USER)["epoch"]
        self.source.users[USER] = [make_message(1, text="restored source")]
        ready = self.import_ready()
        self.assertEqual(ready["readCount"], 1)
        self.assertNotEqual(self.stores.account(ACCOUNT).context(USER)["epoch"], old)


    def test_real_wechat_history_page_contract_with_only_synthetic_sqlite_files(self):
        user = "synthetic-room@chatroom"
        table = "Msg_" + "a" * 32
        files, raw_items = [], []
        for shard_number in (1, 2):
            path = self.root / ("message__message_%d.db" % shard_number)
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
                connection.executemany("INSERT INTO Name2Id(user_name) VALUES(?)", [("self-user",), ("member-b",)])
                connection.execute("CREATE TABLE " + table + "(local_id INTEGER,local_type INTEGER,real_sender_id INTEGER,"
                                   "create_time INTEGER,message_content TEXT,compress_content BLOB,server_id INTEGER,sort_seq INTEGER)")
                seq = shard_number
                record = (1, 1, shard_number, 1700000000 + seq, "native synthetic message %d" % seq,
                          None, 1000 + seq, seq)
                connection.execute("INSERT INTO " + table + " VALUES(?,?,?,?,?,?,?,?)", record)
                raw_items.append((record, path.name, {1: "self-user", 2: "member-b"}))
            files.append(path)
        db = SimpleNamespace(account=ACCOUNT, workdir="C:/synthetic-workdir",
                             _msg_conns=lambda _user: [(sqlite3.connect(path), table) for path in files])
        source = WeChatSource(factory=lambda: db)
        source.db = db
        source._db = lambda fresh=False: db
        source.identity = lambda: (ACCOUNT, "C:/synthetic-workdir")
        source._contacts = lambda _db: {"member-b": {"name": "群成员", "avatar": "", "avatarCandidates": []}}
        source.self_user = lambda _db=None: "self-user"
        source._classified_content = lambda _db, _type, content, _compressed: ("文本", "text", content)
        source._shard_rows = lambda _db, _user, limit: sorted(raw_items, key=lambda item: item[0][7], reverse=True)[:limit]
        self.projection.source = source
        ready = self.import_ready(user)
        self.assertEqual(ready["readCount"], 2)
        rows = self.projection.messages(ACCOUNT, user)
        self.assertEqual([row["side"] for row in rows], ["self", "other"])
        self.assertEqual([row["sender_id"] for row in rows], ["self-user", "member-b"])
        self.assertEqual([row["text"] for row in rows], ["native synthetic message 1", "native synthetic message 2"])
        self.assertEqual(rows[0]["time_ms"], 1700000001000)
        self.assertEqual(json.loads(rows[1]["raw"])["inputMeta"]["conversationId"], user)


    def test_cursor_only_messages_restore_scope_validated_sort(self):
        message = make_message(1, text="cursor-only original")
        message.pop("_sort")
        message["historyCursor"] = encode_cursor(ACCOUNT, USER, (1, "message__message_1.db", 1))
        self.source.history_highwater = lambda _user: (1, "message__message_1.db", 1)
        self.source.history_page = lambda _user, _highwater, after=None, page_size=256: ([message], None) if after is None else ([], None)
        ready = self.import_ready()
        self.assertEqual((ready["state"], ready["readCount"]), ("ready", 1))
        row = self.projection.messages(ACCOUNT, USER)[0]
        self.assertEqual(json.loads(row["sort_key"]), [1, "message__message_1.db", 1])
        self.assertEqual(row["text"], "cursor-only original")


    def test_foreign_cursor_or_disagreeing_sort_fails_without_committing(self):
        for cursor in (encode_cursor("different-account", USER, (1, "message__message_1.db", 1)),
                       encode_cursor(ACCOUNT, "different-room", (1, "message__message_1.db", 1)),
                       encode_cursor(ACCOUNT, USER, (2, "message__message_1.db", 1))):
            message = make_message(1)
            message["historyCursor"] = cursor
            with self.assertRaises(CoreDataError):
                self.projection._page_rows(USER, [message], 0)
        self.assertEqual(self.projection.messages(ACCOUNT, USER), [])


    def test_context_append_is_transactional_and_ignores_duplicate_sequences(self):
        database = self.stores.account("acct-context")
        database.context_begin("user-a", "[5, \"message__message_1.db\", 1]")
        rows = [self._context_row(1, "第一条"), self._context_row(2, "第二条")]
        inserted = database.context_append_page("user-a", rows, "[2, \"message__message_1.db\", 2]",
                                                "[2, \"message__message_1.db\", 2]")
        self.assertEqual(inserted, 2)
        row = database.context("user-a")
        self.assertEqual((row["state"], row["revision"], row["read_count"]), ("reading", 1, 2))
        inserted = database.context_append_page("user-a", rows, None, None)
        self.assertEqual(inserted, 0)
        self.assertEqual(database.context("user-a")["revision"], 1)
        database.context_finish("user-a", 2)
        finished = database.context("user-a")
        self.assertEqual((finished["state"], finished["total_count"], finished["revision"]),
                         ("ready", 2, 1))
        messages = database.context_messages("user-a", None)
        self.assertEqual([item["text"] for item in messages], ["第一条", "第二条"])


    @staticmethod
    def _context_row(seq, text):
        import json
        return {"seq": seq, "msg_id": "m-%d" % seq, "sort_key": "[%d, \"s\", %d]" % (seq, seq),
                "group_chat": 0, "side": "other", "sender_id": "wxid", "sender_name": "朋友",
                "kind": "text", "text": text, "time_ms": 1_700_000_000_000 + seq,
                "raw": json.dumps({"_sort": [seq, "s", seq]})}




"""Synthetic context provenance, budget and invalidation regressions. Apache-2.0.

Modified 2026-10-13: changed human data is announced to plugins through ``on_changed`` (human.changed).
Modified 2026-10-13: Core keeps the verified snapshot and its stamp; the prompt wording lives in the plugins
(here the kyf.advisor copy of ``structured_sources``). Core evidence verification stays in this integration suite.
"""
import importlib.util
import json
import unittest
from pathlib import Path
from unittest import mock

from friend_store import EVIDENCE_STATES, FriendStore, content_hash
from test_friend_service import ACCOUNT as FRIEND_ACCOUNT, USER as FRIEND_USER, FriendServiceCase

_SPEC = importlib.util.spec_from_file_location(
    "advisor_profile_sources", Path(__file__).resolve().parents[1] / "plugins" / "kyf.advisor" /
    "backend" / "profile_section.py")
SOURCES = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(SOURCES)


class FriendContextTests(FriendServiceCase):
    def setUp(self):
        super().setUp()
        self.build()
        self.database = self.service.stores.account(FRIEND_ACCOUNT)
        self.store = FriendStore(self.database)
        self.candidate = self.pending()["对方不吃香菜"]

    def snapshot(self):
        return self.friends._profile_snapshot(FriendStore(self.service.stores.account(FRIEND_ACCOUNT)), FRIEND_USER)

    def sources(self, budget=6000, recent="香菜 饮食"):
        value = self.snapshot()
        return SOURCES.structured_sources(value, budget, recent), value["stamp"]

    def context(self, budget=6000):
        items, stamp = self.sources(budget)
        return (SOURCES.HEADER + "\n" + "\n".join(item["text"] for item in items) + "\n") if items else "", stamp

    def original(self):
        evidence = self.store.evidence([self.candidate["id"]])[self.candidate["id"]][0]
        return self.database._row("SELECT * FROM context_messages WHERE user=? AND msg_id=?", (FRIEND_USER, evidence["msg_id"]))

    def update_original(self, **fields):
        row = self.original()
        def write(connection):
            connection.execute("UPDATE context_messages SET " + ",".join(key + "=?" for key in fields) +
                               " WHERE user=? AND msg_id=?", (*fields.values(), FRIEND_USER, row["msg_id"]))
            connection.execute("UPDATE context SET revision=revision+1 WHERE user=?", (FRIEND_USER,))
        self.store.write(write)

    def pending_diet(self):
        return [item for item in self.sources()[0] if item["status"] == "pending" and "对方不吃香菜" in item["text"]]

    def test_pending_has_friend_original_and_cannot_be_cited_as_confirmed(self):
        items, stamp = self.sources()
        item = self.pending_diet()[0]
        self.assertEqual(item["kind"], "memory")
        self.assertEqual(item["sourceLabel"], "自动提取待确认")
        self.assertIn("不是已确认事实", item["text"])
        self.assertIn("不证明提取结论成立", item["text"])
        self.assertIn("我不吃香菜，一点都不行", item["text"])
        self.assertEqual(item["messageId"], self.original()["msg_id"])
        self.assertRegex(item["sourceId"], r"^memory_[a-f0-9]{24}$")
        self.assertEqual(stamp, self.context()[1])
        self.assertEqual(stamp, self.friends.profile_stamp(self.store, FRIEND_USER))
        self.friends.memory_action(FRIEND_ACCOUNT, FRIEND_USER, {"action": "confirm", "id": self.candidate["id"]})
        updated, next_stamp = self.sources()
        self.assertNotEqual(stamp, next_stamp)
        confirmed = next(value for value in updated if "对方不吃香菜" in value["text"])
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertIn("用户已确认", confirmed["text"])
        self.assertNotIn("自动提取", confirmed["text"])

    def test_ignored_and_deleted_candidates_invalidate_and_disappear(self):
        _, first = self.sources()
        with mock.patch.object(self.friends, "on_changed") as changed:
            self.friends.memory_action(FRIEND_ACCOUNT, FRIEND_USER, {"action": "ignore", "id": self.candidate["id"]})
            changed.assert_called_with(FRIEND_ACCOUNT, FRIEND_USER, mock.ANY)
        self.assertEqual(self.pending_diet(), [])
        self.assertNotEqual(first, self.sources()[1])
        other = self.pending()["对方是急性子（自述）"]
        before = self.sources()[1]
        self.friends.memory_action(FRIEND_ACCOUNT, FRIEND_USER, {"action": "delete", "id": other["id"]})
        self.assertNotIn("急性子", self.context()[0])
        self.assertNotEqual(before, self.sources()[1])

    def test_every_non_ok_evidence_state_excludes_pending_without_revision_bump(self):
        for state in EVIDENCE_STATES:
            with self.subTest(state=state):
                self.store.set_evidence_status(FRIEND_USER, [(self.candidate["id"], 0, "ok")])
                before = self.sources()[1]
                self.store.set_evidence_status(FRIEND_USER, [(self.candidate["id"], 0, state)])
                self.assertEqual(bool(self.pending_diet()), state == "ok")
                if state != "ok":
                    self.assertNotEqual(before, self.sources()[1])

    def test_current_original_change_missing_and_wrong_source_are_not_old_facts(self):
        before = self.sources()[1]
        self.update_original(text="原句已编辑")
        self.assertEqual(self.pending_diet(), [])
        self.assertNotEqual(before, self.sources()[1])
        self.update_original(text="我不吃香菜，一点都不行")
        self.store.write(lambda connection: connection.execute(
            "UPDATE memory_evidence SET source_fp=? WHERE memory_id=?", ("different-source", self.candidate["id"])))
        self.assertEqual(self.pending_diet(), [])

    def test_wrong_speaker_forward_and_quoted_body_do_not_authorize_pending(self):
        original = self.original()
        cases = [{"sender_id": "another-friend"}, {"side": "self"},
                 {"raw": json.dumps({"type": "聊天记录"})},
                 {"raw": json.dumps({"inputMeta": {"quote": {"text": "我不吃香菜"}}})},
                 {"group_chat": 1}]
        for change in cases:
            with self.subTest(change=next(iter(change))):
                self.update_original(**{key: original[key] for key in ("side", "sender_id", "raw", "group_chat")})
                self.update_original(**change)
                # A matching checksum alone must never override speaker/quote ownership.
                digest = content_hash(self.original())
                self.store.write(lambda connection: connection.execute(
                    "UPDATE memory_evidence SET content_hash=? WHERE memory_id=?", (digest, self.candidate["id"])))
                self.assertEqual(self.pending_diet(), [])

    def test_recall_is_rechecked_even_when_snapshot_is_cached(self):
        recalled = set()
        self.source.message_recalled = lambda user, mid: mid in recalled
        first = self.sources()[1]
        self.assertTrue(self.pending_diet())
        recalled.add(self.original()["msg_id"])
        self.assertEqual(self.pending_diet(), [])
        self.assertNotEqual(first, self.sources()[1])

    def test_missing_or_malformed_original_metadata_cannot_authorize_candidate(self):
        for raw in ("not-json", "[]", '{"inputMeta": "unknown"}',
                    '{"media":{"kind":"voice","transcriptSource":"wechat","transcript":42}}'):
            with self.subTest(raw=raw):
                self.update_original(raw=raw)
                self.assertEqual(self.pending_diet(), [])
        self.store.write(lambda connection: (connection.execute(
            "DELETE FROM context_messages WHERE user=? AND msg_id=?", (FRIEND_USER, self.original()["msg_id"])),
            connection.execute("UPDATE context SET revision=revision+1 WHERE user=?", (FRIEND_USER,))))
        self.assertEqual(self.pending_diet(), [])

    def test_subjective_observations_have_own_attribution_and_version_every_edit_delete(self):
        first = self.sources()[1]
        request = {"judgement": "我觉得对方最近想安静一点", "note": "这是我的感觉，尚未问过对方"}
        with mock.patch.object(self.friends, "on_changed") as changed:
            result = self.friends.save_observation(FRIEND_ACCOUNT, FRIEND_USER, request)
            changed.assert_called_with(FRIEND_ACCOUNT, FRIEND_USER, mock.ANY)
        row = result["items"][0]
        values, second = self.sources()
        item = next(item for item in values if item["kind"] == "observation")
        self.assertNotEqual(first, second)
        self.assertIn("主观看法", item["sourceLabel"])
        self.assertIn("不是对方事实", item["text"])
        self.assertIn("尚未独立验证", item["text"])
        self.friends.save_observation(FRIEND_ACCOUNT, FRIEND_USER, {**request, "id": row["id"], "expectedVersion": row["version"],
                                                      "judgement": "我也可能理解错了"})
        third = self.sources()[1]
        self.assertNotEqual(second, third)
        self.friends.save_observation(FRIEND_ACCOUNT, FRIEND_USER, {"action": "delete", "id": row["id"]})
        self.assertNotEqual(third, self.sources()[1])
        self.assertFalse(any(item["kind"] == "observation" for item in self.sources()[0]))

    def test_budget_keeps_boundary_style_and_own_profile_roles_separate(self):
        self.store.create_user_memory(FRIEND_USER, "对方不接受突然打电话", ["边界"])
        for index in range(25):
            self.store.create_user_memory(FRIEND_USER, "对方的普通背景资料" + str(index) + "资料" * 80, ["基本信息"])
        self.store.save_account_profile("用户自己是一名设计师" + "背景" * 1200, "说话简短，别太热情" + "风格" * 1200, 0)
        self.store.save_goal(FRIEND_USER, "保持联系" + "目标" * 1200, 0)
        for budget in (0, 150, 240, 500, 1200, 6000):
            text, _ = self.context(budget)
            self.assertLessEqual(len(text), budget)
        text, _ = self.context(1200)
        self.assertIn("对方不接受突然打电话", text)
        self.assertIn("说话简短", text)
        self.assertIn("用户自己的背景；不是对方资料", text)
        self.assertIn("不是对方意愿或同意", text)
        self.assertLess(text.count("普通背景资料"), 25)

    def test_other_contact_data_and_uncertain_or_conflicting_proposals_are_excluded(self):
        self.store.create_user_memory("other-contact", "另一位朋友的专属资料", ["爱好"])
        self.store.save_observation("other-contact", None, "另一位朋友的观察", "", None, None)
        self.assertNotIn("另一位", self.context()[0])
        for relation in ("uncertain", "conflict"):
            self.store.write(lambda connection: (connection.execute(
                "UPDATE memories SET relation=? WHERE id=?", (relation, self.candidate["id"])),
                self.store.bump(connection, FRIEND_USER)))
            self.assertEqual(self.pending_diet(), [])

    def test_projection_append_does_not_change_profile_stamp_or_repeat_original_sql_on_cache_hit(self):
        first = self.sources()[1]
        with mock.patch.object(self.database, "_rows", wraps=self.database._rows) as reads:
            self.sources()
        self.assertFalse(any("msg_id IN" in str(call.args[0]) for call in reads.call_args_list))
        self.store.write(lambda connection: connection.execute("UPDATE context SET revision=revision+1 WHERE user=?", (FRIEND_USER,)))
        self.assertEqual(first, self.sources()[1])


if __name__ == "__main__":
    unittest.main()
