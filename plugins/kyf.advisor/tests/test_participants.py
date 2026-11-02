"""Friends @-mentioned in a discussion: sticky participants and their on-demand files (synthetic data only)."""
import tempfile
import time
import unittest
from pathlib import Path

import participants
from contracts import AdvisorError
from support import FakeModelStore, FakeRuntime, FakeSource, build_service, make_message, wait_until

ACCOUNT = "synthetic-account-1"
USER = "user-a"
FRIEND = "user-b"
GROUP = "synthetic-group@chatroom"
AGENT = "builtin:advisor"


class FileReadingRuntime(FakeRuntime):
    """Keeps what the participant files held during the call; the service removes them afterwards."""

    def __init__(self):
        super().__init__()
        self.files = []

    def respond(self, config, request, on_event, cancel_event):
        self.files.append({path: Path(path).read_text(encoding="utf-8") for path in request.get("participantFiles", [])})
        return super().respond(config, request, on_event, cancel_event)


class ParticipantServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = FakeSource(account=ACCOUNT)
        self.source.seed_pages(USER, 20)
        self.source.add_messages(FRIEND, [
            make_message(1, side="other", sender=FRIEND, name="小李", text="SYNTHETIC_FRIEND_B_ONLY 周末一起吃饭吗"),
            make_message(2, side="self", sender="self", name="我", text="好呀", time_ms=1_710_000_000_000),
            make_message(3, side="other", sender=FRIEND, name="小李", text="那就说定了", time_ms=1_710_000_060_000)])
        self.source.friend_list = [{"user": USER, "name": "小王", "isGroup": False},
                                   {"user": FRIEND, "name": "小李", "isGroup": False},
                                   {"user": GROUP, "name": "合成群", "isGroup": True}]
        self.source.snapshot["data"]["goal"] = "保持联系"
        self.runtime = FileReadingRuntime()
        self.service = build_service(self.source, FakeModelStore(context_tokens=32768), self.root,
                                     runtime_factory=lambda: self.runtime)
        self.runtime.participant_root = lambda account: (self.service.stores.directory(account) /
                                                         "engine" / "engine" / "workspace" / "participants")
        self.addCleanup(self.service.shutdown)

    def ask(self, thread_id, message, request_id, mentions=None):
        started = self.accepted = self.service.start(ACCOUNT, USER, AGENT, thread_id, message, request_id, mentions=mentions)
        terminal = wait_until(lambda: (value if value["run"]["state"] != "running" else None)
                              if (value := self.service.events(ACCOUNT, USER, started["run"]["id"], 0)) else None)
        self.assertEqual(terminal["run"]["state"], "done", terminal)
        wait_until(lambda: not any(run.worker.is_alive() for run in self.service._runs.values()))
        return started["threadId"], terminal

    def test_mentioned_friend_stays_in_the_discussion_and_is_read_on_demand(self):
        thread_id, terminal = self.ask(None, "对比 @小李 和他对我的感情", "req-1", mentions=[FRIEND])
        self.assertEqual(self.accepted["participants"], [{"user": FRIEND, "name": "小李"}], "chips can show before the answer")
        self.assertEqual(terminal["thread"]["participants"], [{"user": FRIEND, "name": "小李"}])
        request = self.runtime.respond_calls[0][1]
        self.assertNotIn("SYNTHETIC_FRIEND_B_ONLY", request["contextFileText"])
        files = self.runtime.files[0]
        self.assertEqual(sorted(files), sorted(request["participantFiles"]))
        self.assertIn(request["participantIndex"], files)
        index = files[request["participantIndex"]]
        self.assertIn("小李（user-b）", index)
        self.assertIn("3 条消息：我 1 条，对方 2 条", index)
        self.assertIn("按月位置", index)
        chat = next(text for path, text in files.items() if path.endswith("-chat.txt"))
        profile = next(text for path, text in files.items() if path.endswith("-profile.txt"))
        self.assertIn("SYNTHETIC_FRIEND_B_ONLY", chat)
        self.assertIn("保持联系", profile)
        self.assertFalse(any(Path(path).exists() for path in files), "files are removed when the run ends")

        # Later questions keep the friend without mentioning them again.
        self.ask(thread_id, "那她最近呢？", "req-2")
        self.assertIn("participantIndex", self.runtime.respond_calls[1][1])

        removed = self.service.remove_participant(ACCOUNT, USER, AGENT, thread_id, FRIEND)
        self.assertEqual(removed["thread"]["participants"], [])
        self.ask(thread_id, "只看当前好友", "req-3")
        self.assertNotIn("participantIndex", self.runtime.respond_calls[2][1])
        self.assertNotIn("participantFiles", self.runtime.respond_calls[2][1])

    def test_mentions_must_be_added_one_to_one_chats_and_the_current_friend_is_ignored(self):
        thread_id = self.service.thread(ACCOUNT, USER, AGENT)["thread"]["id"]
        for mention, message in ((GROUP, "暂不支持 @ 群聊"), ("user-unknown", "@ 的好友不在已添加的会话中")):
            with self.subTest(mention=mention), self.assertRaises(AdvisorError) as caught:
                self.service.start(ACCOUNT, USER, AGENT, thread_id, "问题", "req-" + mention[:6], mentions=[mention])
            self.assertEqual((caught.exception.code, caught.exception.message), ("invalid-request", message))
        self.assertEqual(self.service.thread(ACCOUNT, USER, AGENT, thread_id)["thread"]["messages"], [])
        _thread, terminal = self.ask(thread_id, "问自己", "req-self", mentions=[USER, USER])
        self.assertEqual(terminal["thread"]["participants"], [])
        self.assertNotIn("participantIndex", self.runtime.respond_calls[0][1])

    def test_a_friend_no_longer_added_is_listed_as_unavailable(self):
        thread_id, _terminal = self.ask(None, "看看 @小李", "req-1", mentions=[FRIEND])
        self.source.friend_list = [item for item in self.source.friend_list if item["user"] != FRIEND]
        self.ask(thread_id, "再看看", "req-2")
        files = self.runtime.files[1]
        self.assertEqual(list(files), [self.runtime.respond_calls[1][1]["participantIndex"]])
        self.assertIn("本轮资料不可用：已不在已添加的会话中", next(iter(files.values())))

    def test_friend_list_offers_added_one_to_one_chats_except_the_current_friend(self):
        self.assertEqual(self.service.friends(ACCOUNT, USER), {"friends": [{"user": FRIEND, "name": "小李"}]})

    def test_draft_keeps_selected_identity_without_joining_or_loading_friend_context(self):
        thread_id = self.service.thread(ACCOUNT, USER, AGENT)["thread"]["id"]
        selected = [{"user": FRIEND, "name": "选择时的名字"}]
        saved = self.service.save_draft(ACCOUNT, USER, AGENT, thread_id, "@选择时的名字 问题", 0,
                                        mentions=selected + selected)["draft"]
        self.assertEqual(saved["mentions"], selected)
        self.source.friend_list[1]["name"] = "后来改名"
        thread = self.service.thread(ACCOUNT, USER, AGENT, thread_id)["thread"]
        self.assertEqual(thread["draft"]["mentions"], selected, "names label text; IDs select the friend")
        self.assertEqual(thread["participants"], [], "draft identities never join before send")
        self.assertIsNone(self.service.stores.account(ACCOUNT).context(FRIEND))
        self.assertEqual(self.runtime.respond_calls, [])
        self.assertEqual(self.runtime.compact_calls, [])

    def test_invalid_or_out_of_scope_draft_mentions_preserve_the_saved_draft(self):
        thread_id = self.service.thread(ACCOUNT, USER, AGENT)["thread"]["id"]
        original = self.service.save_draft(ACCOUNT, USER, AGENT, thread_id, "保留草稿", 0)["draft"]
        invalid = ["not-a-list", [FRIEND], [{"user": FRIEND}], [{"user": FRIEND, "name": "小李", "extra": True}],
                   [{"user": FRIEND, "name": ""}], [{"user": FRIEND, "name": "x" * 241}],
                   [{"user": FRIEND, "name": "小\n李"}], [{"user": "", "name": "小李"}],
                   [{"user": "foreign-account-person", "name": "外部"}], [{"user": GROUP, "name": "群聊"}]]
        for mentions in invalid:
            with self.subTest(mentions=mentions), self.assertRaises(AdvisorError):
                self.service.save_draft(ACCOUNT, USER, AGENT, thread_id, "不应覆盖", original["version"], mentions=mentions)
        with self.assertRaises(AdvisorError):
            self.service.save_draft(ACCOUNT, FRIEND, AGENT, thread_id, "归属错误", original["version"],
                                    mentions=[{"user": USER, "name": "小王"}])
        self.assertEqual(self.service.thread(ACCOUNT, USER, AGENT, thread_id)["thread"]["draft"], original)

    def test_deleting_a_discussion_removes_its_participants(self):
        database = self.service.stores.account(ACCOUNT)
        thread = database.create_thread(USER, AGENT, "source-synthetic")
        database.session_add_participants(thread["id"], [{"user": FRIEND, "name": "小李"}])
        operation = database.session_delete_begin(USER, AGENT, thread["id"], "req-delete")
        database.session_delete_finish(operation["id"])
        self.assertEqual(database.session_participants(thread["id"]), [])


class ParticipantFileTests(unittest.TestCase):
    def test_long_records_wrap_and_month_positions_point_at_their_first_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            march = int(time.mktime((2025, 3, 2, 12, 0, 0, 0, 0, -1)) * 1000)
            april = int(time.mktime((2025, 4, 2, 12, 0, 0, 0, 0, -1)) * 1000)
            rows = [{"seq": 1, "msg_id": "m1", "side": "other", "sender_name": "小李", "text": "长" * 1500, "time_ms": march, "raw": "{}"},
                    {"seq": 2, "msg_id": "m2", "side": "self", "text": "APRIL_FIRST", "time_ms": april, "raw": "{}"}]
            snapshot = {"data": {"memories": [], "observations": [], "goal": "", "profile": {"background": "", "style": ""},
                                 "proofs": {}, "exclusions": []}, "stamp": "s"}
            materials = participants.Materials(root, root / "participants" / "run-1")
            materials.add("user-b", "小李", rows, snapshot)
            index, files = materials.finish()
            self.assertEqual(files[0], index)
            self.assertEqual(len(files), 2, "no profile file without 知己分析资料")
            lines = Path(files[1]).read_text(encoding="utf-8").split("\n")
            self.assertTrue(all(len(line) <= participants.WRAP_CODEPOINTS for line in lines))
            text = Path(index).read_text(encoding="utf-8")
            self.assertIn("知己分析资料：暂无", text)
            position = int(text.split("2025-04 第 ")[1].split(" 行")[0])
            self.assertIn("APRIL_FIRST", lines[position - 1])
            participants.discard_stale(root / "participants", active_runs=set())
            self.assertFalse((root / "participants" / "run-1").exists())


if __name__ == "__main__":
    unittest.main()
