"""External-agent read entries (G1–G3) on synthetic saved data: find contacts, contact
details, recent conversations, recent messages, cross-contact search and the contact
brief -- in full, accounts and per-contact mode, offline (WeChat not running)."""

import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_account_db import AccountStores
from core_contracts import AdvisorError
from core_mcp_offline import SavedAccountScope
from core_mcp_proxy import REGISTRY, McpError
from friend_service import FriendService
from friend_store import FriendStore
from test_core_mcp import FINGERPRINT, headers, make_services

DAY = 86_400_000
NOW = int(time.time() * 1000)


def rows(user, messages, *, group=False):
    """``messages``: (days_ago, side, sender_name, text) tuples, oldest first."""
    result = []
    for index, (days_ago, side, sender, text) in enumerate(messages):
        result.append({"msg_id": f"{user}-{index}", "sort_key": json.dumps([NOW - days_ago * DAY, user, index]),
                       "group_chat": 1 if group else 0, "side": side, "sender_id": sender,
                       "sender_name": sender, "kind": "文本", "time_ms": NOW - int(days_ago * DAY),
                       "raw": "{}", "text": text})
    return result


class SavedData:
    """Two saved accounts with private chats, a group and human records."""

    def __init__(self):
        self.root = tempfile.TemporaryDirectory()
        base = Path(self.root.name)
        self.stores = AccountStores(base / ".local" / "advisor-data")
        a = self.stores.account("账号甲")
        self.add(a, "wxid_zhang", rows("wxid_zhang", [
            (40, "other", "张三", "好久不见"), (10, "self", "我", "最近忙什么"),
            (2, "other", "张三", "下个月准备搬家"), (1, "self", "我", "需要帮忙吗"),
            (0.5, "other", "张三", "长" * 2500)]))
        self.add(a, "wxid_li", rows("wxid_li", [(5, "other", "李四", "周末打球吗"), (3, "self", "我", "好啊")]))
        self.add(a, "123@chatroom", rows("123@chatroom", [(4, "other", "王五", "群里也有人搬家")], group=True))
        b = self.stores.account("账号乙")
        self.add(b, "wxid_zhao", rows("wxid_zhao", [(0.2, "other", "赵六", "我刚搬家完")]))
        self.add(b, "wxid_zhang2", rows("wxid_zhang2", [(20, "other", "张三丰", "太极")]))
        store = FriendStore(a)
        self.user_memory = store.create_user_memory("wxid_zhang", "喜欢爬山", ["爱好"])["id"]

        def pending(connection):
            connection.execute(
                "INSERT INTO memories(id,user,tags_json,text,norm,status,origin,created_at_ms,updated_at_ms)"
                " VALUES('mem-ai','wxid_zhang','[\"经历\"]','准备搬家','准备搬家','pending','ai',?,?)",
                (NOW, NOW))
        store.write(pending)
        store.save_goal("wxid_zhang", "了解近况", 0)
        store.save_observation("wxid_zhang", None, "做事靠谱", "", None, None)
        registry = base / ".local" / "real-client-data"
        registry.mkdir(parents=True, exist_ok=True)
        self.registry = registry / "accounts.json"
        self.registry.write_text(json.dumps({"accounts": [
            {"accountId": "id-A", "account": "账号甲", "nickname": "甲"},
            {"accountId": "id-B", "account": "账号乙", "nickname": "乙"}]}, ensure_ascii=False),
            encoding="utf-8")

    @staticmethod
    def add(database, user, messages):
        database.context_begin(user, "{}", source_id="synthetic-source")
        database.context_append_page(user, messages, "{}", "{}")
        database.context_finish(user, len(messages))

    def close(self):
        self.stores.close()
        self.root.cleanup()


class OfflineSource:
    """WeChat is not running: every live identity check fails."""

    def advisor_identity(self, messages=True):
        raise RuntimeError("微信未运行")


class FakeAnalysis:
    """The analysis bridge: saved-result reads only; any start is a test failure."""

    def __init__(self):
        self.pages = []
        self.started = []

    def resolve_account(self, account_id):
        return {"id-A": "账号甲", "id-B": "账号乙"}[account_id]

    def friend_analysis_page(self, account, user, method, params=None):
        self.pages.append((account, user, method))
        return {"summary": {"summary": "张三最近在准备搬家（推断）", "evidence": [{"msgId": "x"}],
                            "createdAtMs": NOW - DAY},
                "next": {"next": "问问搬家需不需要帮忙", "createdAtMs": NOW - DAY},
                "about": [{"kind": "memory", "label": "爱好", "text": "爬山"}], "stale": False}

    def start_friend_analysis(self, *args, **kwargs):
        self.started.append(args)
        raise AssertionError("简报不得启动任务")

    def start_message_insights(self, *args, **kwargs):
        self.started.append(args)
        raise AssertionError("简报不得启动任务")

    def start_self_analysis(self, *args, **kwargs):
        self.started.append(args)
        raise AssertionError("简报不得启动任务")


class ReadsHarness(unittest.TestCase):
    def setUp(self):
        self.data = SavedData()
        self.addCleanup(self.data.close)
        advisor = types.SimpleNamespace(stores=self.data.stores, source=OfflineSource(), context=None)
        self.friends = FriendService(advisor)
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.store = self.mcp.store
        names = {"账号甲": {"123@chatroom": "周末羽毛球群"}}
        self.mcp.offline = lambda: SavedAccountScope(self.friends, self.data.registry,
                                                     names=lambda account: names.get(account, {}))
        self.analysis = FakeAnalysis()
        self.mcp.analysis = self.analysis
        self.enabled_plugins = {"kyf.friend-analysis", "kyf.message-insights"}
        self.mcp.plugin_enabled = lambda plugin: plugin in self.enabled_plugins

    def pair(self, mode="full"):
        created = self.store.create_profile("合成客户端", mode)
        token = self.mcp.handshake({"clientId": created["clientId"], "credential": created["credential"]})
        return self.mcp.authenticate(headers(token["token"])), created

    def call(self, principal, action, params=None, *, account=None, subject=None):
        body = {"action": action, "params": params or {}}
        if account is not None:
            body["account"] = account
        if subject is not None:
            body["subjects"] = [{"accountId": subject[0], "subjectType": "wechat_contact",
                                 "subjectId": subject[1]}]
        return self.mcp.call(principal, body)

    def denied(self, principal, action, params=None, **kwargs):
        with self.assertRaises(McpError) as caught:
            self.call(principal, action, params, **kwargs)
        return caught.exception.code


class RegistryTests(unittest.TestCase):
    def test_new_tools_are_reads_without_plugins(self):
        for name in ("zhiji_find_contacts", "zhiji_get_contact", "zhiji_list_recent_conversations",
                     "zhiji_get_recent_messages", "zhiji_get_contact_brief", "zhiji_search_messages"):
            with self.subTest(name=name):
                entry = REGISTRY[name]
                self.assertEqual(entry["capability"], "read")
                self.assertIsNone(entry.get("plugin"))
                self.assertFalse(entry.get("write"))
                self.assertTrue(entry.get("handler"))
        for name in ("zhiji_find_contacts", "zhiji_list_recent_conversations", "zhiji_search_messages"):
            entry = REGISTRY[name]
            self.assertEqual((entry["subjects"], entry["aggregate"], entry["recomputable"]), (False, True, True))
        for name in ("zhiji_get_contact", "zhiji_get_recent_messages", "zhiji_get_contact_brief"):
            self.assertTrue(REGISTRY[name]["subjects"])


class FullModeTests(ReadsHarness):
    def test_find_contacts_by_name_group_and_id(self):
        principal, _ = self.pair()
        result = self.call(principal, "zhiji_find_contacts", {"query": "张三"})
        found = [(item["accountId"], item["subjectId"]) for item in result["contacts"]]
        # Exact match first, then the prefix match in the other account.
        self.assertEqual(found, [("id-A", "wxid_zhang"), ("id-B", "wxid_zhang2")])
        first = result["contacts"][0]
        self.assertEqual((first["name"], first["nameSource"], first["isGroup"], first["savedMessages"]),
                         ("张三", "message", False, 5))
        self.assertGreater(first["lastMessageTime"], NOW - DAY)
        group = self.call(principal, "zhiji_find_contacts", {"query": "羽毛球"})["contacts"]
        self.assertEqual([(item["subjectId"], item["isGroup"], item["nameSource"]) for item in group],
                         [("123@chatroom", True, "session")])
        by_id = self.call(principal, "zhiji_find_contacts", {"query": "WXID_LI"})["contacts"]
        self.assertEqual([item["subjectId"] for item in by_id], ["wxid_li"])
        only_b = self.call(principal, "zhiji_find_contacts", {"query": "张三"}, account="id-B")["contacts"]
        self.assertEqual([item["subjectId"] for item in only_b], ["wxid_zhang2"])
        self.assertEqual(self.denied(principal, "zhiji_find_contacts", {"query": "张三", "limit": 21}),
                         "invalid-request")
        self.assertEqual(self.denied(principal, "zhiji_find_contacts", {"query": " "}), "invalid-request")
        self.assertEqual(self.denied(principal, "zhiji_find_contacts", {"query": "张三"}, account="id-X"),
                         "account-unknown")

    def test_get_contact_reports_stats_and_readiness(self):
        principal, _ = self.pair()
        result = self.call(principal, "zhiji_get_contact", subject=("id-A", "wxid_zhang"))
        self.assertEqual((result["name"], result["isGroup"], result["savedMessages"]), ("张三", False, 5))
        self.assertEqual(result["readiness"]["state"], "ready")
        self.assertEqual(result["stats"]["bySide"], {"other": 3, "self": 2})
        self.assertLess(result["stats"]["firstTime"], result["stats"]["lastTime"])
        self.assertEqual(result["analyses"]["friendAnalysis"],
                         {"open": True, "readWith": "zhiji_get_friend_analysis"})
        self.assertEqual(self.denied(principal, "zhiji_get_contact", subject=("id-A", "wxid_nobody")),
                         "not-found")
        self.assertEqual(self.analysis.pages, [])

    def test_recent_conversations_newest_first_with_cursor(self):
        principal, _ = self.pair()
        first = self.call(principal, "zhiji_list_recent_conversations", {"limit": 2})
        order = [(item["accountId"], item["subjectId"]) for item in first["conversations"]]
        self.assertEqual(order, [("id-B", "wxid_zhao"), ("id-A", "wxid_zhang")])
        zhang = first["conversations"][1]
        self.assertEqual((zhang["lastSide"], len(zhang["lastMessagePreview"])), ("other", 80))
        self.assertEqual(first["total"], 5)
        second = self.call(principal, "zhiji_list_recent_conversations", {"limit": 2, "cursor": first["cursor"]})
        self.assertEqual([item["subjectId"] for item in second["conversations"]], ["wxid_li", "123@chatroom"])
        third = self.call(principal, "zhiji_list_recent_conversations", {"limit": 2, "cursor": second["cursor"]})
        self.assertEqual([item["subjectId"] for item in third["conversations"]], ["wxid_zhang2"])
        self.assertIsNone(third["cursor"])
        group = second["conversations"][1]
        self.assertEqual((group["name"], group["isGroup"]), ("周末羽毛球群", True))
        # Tampered, foreign-account and other-tool cursors are all refused.
        tampered = first["cursor"][:-1] + ("0" if first["cursor"][-1] != "0" else "1")
        self.assertEqual(self.denied(principal, "zhiji_list_recent_conversations", {"cursor": tampered}),
                         "cursor-invalid")
        self.assertEqual(self.denied(principal, "zhiji_list_recent_conversations", {"cursor": first["cursor"]},
                                     account="id-A"), "cursor-invalid")
        search = self.call(principal, "zhiji_search_messages", {"query": "搬家", "limit": 1})
        self.assertEqual(self.denied(principal, "zhiji_list_recent_conversations", {"cursor": search["cursor"]}),
                         "cursor-invalid")

    def test_recent_messages_page_backwards_in_chronological_order(self):
        principal, _ = self.pair()
        subject = ("id-A", "wxid_zhang")
        first = self.call(principal, "zhiji_get_recent_messages", {"limit": 2}, subject=subject)
        texts = [item["text"][:6] for item in first["messages"]]
        self.assertEqual(texts, ["需要帮忙吗", "长" * 6])
        newest = first["messages"][-1]
        self.assertEqual((len(newest["text"]), newest["truncated"], newest["side"], newest["senderName"]),
                         (2000, True, "other", "张三"))
        self.assertEqual(set(first["messages"][0]), {"id", "time", "side", "senderName", "kind", "text", "hasMedia"})
        self.assertEqual(first["sourceMode"], "cached")
        older = self.call(principal, "zhiji_get_recent_messages", {"limit": 2, "cursor": first["cursor"]},
                          subject=subject)
        self.assertEqual([item["text"] for item in older["messages"]], ["最近忙什么", "下个月准备搬家"])
        last = self.call(principal, "zhiji_get_recent_messages", {"limit": 2, "cursor": older["cursor"]},
                         subject=subject)
        self.assertEqual([item["text"] for item in last["messages"]], ["好久不见"])
        self.assertIsNone(last["cursor"])
        # A cursor is bound to its contact and to beforeTimeMs.
        self.assertEqual(self.denied(principal, "zhiji_get_recent_messages", {"cursor": first["cursor"]},
                                     subject=("id-A", "wxid_li")), "cursor-invalid")
        self.assertEqual(self.denied(principal, "zhiji_get_recent_messages",
                                     {"cursor": first["cursor"], "beforeTimeMs": NOW}, subject=subject),
                         "cursor-invalid")
        before = self.call(principal, "zhiji_get_recent_messages", {"beforeTimeMs": NOW - 5 * DAY},
                           subject=subject)
        self.assertEqual([item["text"] for item in before["messages"]], ["好久不见", "最近忙什么"])

    def test_cross_contact_search_and_single_contact_search(self):
        principal, _ = self.pair()
        result = self.call(principal, "zhiji_search_messages", {"query": "搬家"})
        hits = [(hit["accountId"], hit["subjectId"]) for hit in result["hits"]]
        self.assertEqual(hits, [("id-B", "wxid_zhao"), ("id-A", "wxid_zhang"), ("id-A", "123@chatroom")])
        self.assertEqual(result["hits"][1]["name"], "张三")
        self.assertTrue(result["hits"][2]["isGroup"])
        self.assertEqual(result["hits"][0]["message"]["text"], "我刚搬家完")
        self.assertIsNone(result["cursor"])
        self.assertEqual(result["scanLimitedAccounts"], [])
        paged = self.call(principal, "zhiji_search_messages", {"query": "搬家", "limit": 2})
        rest = self.call(principal, "zhiji_search_messages", {"query": "搬家", "limit": 2, "cursor": paged["cursor"]})
        self.assertEqual([hit["subjectId"] for hit in rest["hits"]], ["123@chatroom"])
        # The cursor is bound to its query.
        self.assertEqual(self.denied(principal, "zhiji_search_messages",
                                     {"query": "打球", "cursor": paged["cursor"]}), "cursor-invalid")
        only_a = self.call(principal, "zhiji_search_messages", {"query": "搬家"}, account="id-A")
        self.assertEqual({hit["accountId"] for hit in only_a["hits"]}, {"id-A"})
        # With a subject: the original per-contact search.
        single = self.call(principal, "zhiji_search_messages", {"query": "搬家"}, subject=("id-A", "wxid_zhang"))
        self.assertEqual([item["text"] for item in single["messages"]], ["下个月准备搬家"])
        self.assertEqual(self.denied(principal, "zhiji_search_messages", {}), "invalid-request")

    def test_brief_composes_sections_and_never_starts_tasks(self):
        principal, _ = self.pair()
        brief = self.call(principal, "zhiji_get_contact_brief", subject=("id-A", "wxid_zhang"))
        self.assertEqual(self.analysis.started, [])
        self.assertEqual(self.analysis.pages, [("账号甲", "wxid_zhang", "page.overview")])
        for section in ("contact", "stats", "recentMessages", "memories", "goal", "observations", "trend",
                        "friendAnalysis"):
            with self.subTest(section=section):
                self.assertIn("source", brief[section])
                self.assertIn("updatedAt", brief[section])
        self.assertEqual(brief["missing"], [])
        self.assertIn("inference", brief["notes"])
        self.assertEqual(brief["friendAnalysis"]["kind"], "inference")
        self.assertNotIn("evidence", json.dumps(brief["friendAnalysis"], ensure_ascii=False))
        self.assertEqual(brief["stats"]["last30Days"], {"self": 2, "other": 2, "total": 4})
        self.assertEqual(brief["stats"]["totalMessages"], 5)
        self.assertEqual(len(brief["recentMessages"]["items"]), 5)
        self.assertLessEqual(max(len(item["text"]) for item in brief["recentMessages"]["items"]), 200)
        memories = brief["memories"]
        self.assertEqual([(item["status"], item["recordedBy"]) for item in memories["items"]],
                         [("confirmed", "用户"), ("pending", "助手")])
        self.assertEqual(memories["counts"], {"confirmed": 1, "pending": 1})
        self.assertIn("非微信原文", memories["nature"])
        self.assertEqual(brief["goal"]["text"], "了解近况")
        self.assertEqual([item["judgement"] for item in brief["observations"]["items"]], ["做事靠谱"])
        self.assertEqual(brief["timeRange"]["coverage"], "ready")
        self.assertLessEqual(len(json.dumps(brief, ensure_ascii=False)), 16000)

    def test_brief_lists_missing_sections(self):
        principal, _ = self.pair()
        self.enabled_plugins.clear()
        brief = self.call(principal, "zhiji_get_contact_brief", subject=("id-A", "wxid_li"))
        self.assertNotIn("friendAnalysis", brief)
        self.assertEqual({item["section"] for item in brief["missing"]}, {"friendAnalysis", "memories"})
        self.assertEqual(self.analysis.pages, [])
        self.enabled_plugins.add("kyf.friend-analysis")
        group = self.call(principal, "zhiji_get_contact_brief", subject=("id-A", "123@chatroom"))
        reasons = {item["section"]: item["reason"] for item in group["missing"]}
        self.assertEqual(reasons["friendAnalysis"], "群聊不提供好友分析")
        self.assertEqual(self.analysis.pages, [])
        self.assertEqual(self.analysis.started, [])
        self.assertEqual(self.denied(principal, "zhiji_get_contact_brief", {"x": 1},
                                     subject=("id-A", "wxid_li")), "invalid-request")


class ScanLimitTests(ReadsHarness):
    def test_cross_search_reports_a_capped_scan(self):
        scope = self.mcp.offline()
        result = scope.search_all({"id-A": None}, query="好久不见", limit=10, offset=0, scan_max=3)
        # Only the three newest rows were scanned, so the oldest match is not found.
        self.assertEqual((result["hits"], result["scanLimitedAccounts"]), ([], ["id-A"]))
        result = scope.search_all({"id-A": None}, query="好久不见", limit=10, offset=0, scan_max=1000)
        self.assertEqual(([hit["subjectId"] for hit in result["hits"]], result["scanLimitedAccounts"]),
                         (["wxid_zhang"], []))

    def test_missing_account_data_is_reported_not_fatal(self):
        self.data.registry.write_text(json.dumps({"accounts": [
            {"accountId": "id-A", "account": "账号甲"}, {"accountId": "id-C", "account": "账号丙"}]},
            ensure_ascii=False), encoding="utf-8")
        principal, _ = self.pair()
        result = self.call(principal, "zhiji_list_recent_conversations", {})
        self.assertEqual(result["unavailableAccounts"], [{"accountId": "id-C", "reason": "cache-gap"}])
        self.assertEqual({item["accountId"] for item in result["conversations"]}, {"id-A"})


class AccountsModeTests(ReadsHarness):
    def setUp(self):
        super().setUp()
        self.principal, created = self.pair("accounts")
        self.store.grant_account(created["clientId"], FINGERPRINT, "id-A")
        self.principal = self.mcp.authenticate(headers(self.mcp.handshake(
            {"clientId": created["clientId"], "credential": created["credential"]})["token"]))

    def test_cross_contact_reads_stay_in_the_granted_account(self):
        found = self.call(self.principal, "zhiji_find_contacts", {"query": "张三"})["contacts"]
        self.assertEqual([(item["accountId"], item["subjectId"]) for item in found], [("id-A", "wxid_zhang")])
        conversations = self.call(self.principal, "zhiji_list_recent_conversations", {})["conversations"]
        self.assertEqual({item["accountId"] for item in conversations}, {"id-A"})
        hits = self.call(self.principal, "zhiji_search_messages", {"query": "搬家"})["hits"]
        self.assertEqual({hit["accountId"] for hit in hits}, {"id-A"})
        self.assertEqual(self.denied(self.principal, "zhiji_find_contacts", {"query": "张三"}, account="id-B"),
                         "account-not-authorized")
        self.assertEqual(self.denied(self.principal, "zhiji_search_messages", {"query": "搬家"}, account="id-B"),
                         "account-not-authorized")
        brief = self.call(self.principal, "zhiji_get_contact_brief", subject=("id-A", "wxid_zhang"))
        self.assertEqual(brief["requestScope"], "authorized_accounts")
        self.assertEqual(self.denied(self.principal, "zhiji_get_contact_brief", subject=("id-B", "wxid_zhao")),
                         "account-not-authorized")
        self.assertEqual(self.denied(self.principal, "zhiji_get_recent_messages", subject=("id-B", "wxid_zhao")),
                         "account-not-authorized")


class PerContactModeTests(ReadsHarness):
    def setUp(self):
        super().setUp()
        principal, created = self.pair("per_contact")
        self.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "wxid_zhang")
        self.principal = self.mcp.authenticate(headers(self.mcp.handshake(
            {"clientId": created["clientId"], "credential": created["credential"]})["token"]))

    def test_only_granted_contacts_appear(self):
        self.assertEqual(self.call(self.principal, "zhiji_find_contacts", {"query": "李"})["contacts"], [])
        found = self.call(self.principal, "zhiji_find_contacts", {"query": "张三"})["contacts"]
        self.assertEqual([(item["accountId"], item["subjectId"]) for item in found], [("id-A", "wxid_zhang")])
        conversations = self.call(self.principal, "zhiji_list_recent_conversations", {})
        self.assertEqual([item["subjectId"] for item in conversations["conversations"]], ["wxid_zhang"])
        self.assertEqual(conversations["requestScope"], "authorized_contacts")
        hits = self.call(self.principal, "zhiji_search_messages", {"query": "搬家"})["hits"]
        self.assertEqual({(hit["accountId"], hit["subjectId"]) for hit in hits}, {("id-A", "wxid_zhang")})
        self.assertEqual(self.denied(self.principal, "zhiji_find_contacts", {"query": "赵"}, account="id-B"),
                         "account-not-authorized")
        # Narrowing search to an ungranted contact is refused like any subject call.
        self.assertEqual(self.denied(self.principal, "zhiji_search_messages", {"query": "搬家"},
                                     subject=("id-A", "wxid_li")), "contact-not-authorized")
        self.call(self.principal, "zhiji_get_contact_brief", subject=("id-A", "wxid_zhang"))
        for action in ("zhiji_get_contact", "zhiji_get_recent_messages", "zhiji_get_contact_brief"):
            with self.subTest(action=action):
                self.assertEqual(self.denied(self.principal, action, subject=("id-A", "wxid_li")),
                                 "contact-not-authorized")
        self.assertEqual(self.analysis.started, [])

    def test_revocation_invalidates_the_scope_and_cursors(self):
        first = self.call(self.principal, "zhiji_get_recent_messages", {"limit": 1},
                          subject=("id-A", "wxid_zhang"))
        self.store.revoke_grant(self.principal["clientId"], FINGERPRINT, "id-A", "wechat_contact", "wxid_zhang") \
            if hasattr(self.store, "revoke_grant") else self.store.set_mode(self.principal["clientId"], "per_contact")
        self.assertEqual(self.denied(self.principal, "zhiji_get_recent_messages", {"cursor": first["cursor"]},
                                     subject=("id-A", "wxid_zhang")), "stale-authorization")


class SnapshotReadTests(ReadsHarness):
    """Reads use the read-only snapshot, never the writer lock or connection."""

    def test_reads_finish_while_the_writer_holds_the_lock(self):
        database = self.data.stores.saved_database("账号甲")
        held, release = threading.Event(), threading.Event()

        def writer():
            with database.lock:
                connection = database._connect()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE context SET read_count=read_count WHERE user='wxid_zhang'")
                held.set()
                release.wait(30)
                connection.rollback()

        holder = threading.Thread(target=writer, daemon=True)
        holder.start()
        self.assertTrue(held.wait(5))
        calls = {
            "recent_messages": lambda: self.friends.saved_recent_messages("账号甲", "wxid_zhang", limit=2),
            "contact_stats": lambda: self.friends.saved_contact_stats("账号甲", "wxid_zhang"),
            "contact_records": lambda: self.friends.saved_contact_records("账号甲", "wxid_zhang"),
            "conversations": lambda: self.friends.saved_conversations("账号甲", limit=2),
            "search_all": lambda: self.friends.saved_search_all("账号甲", query="搬家"),
        }
        try:
            for name, call in calls.items():
                with self.subTest(name=name):
                    outcome = {}

                    def run():
                        try:
                            outcome["value"] = call()
                        except AdvisorError as exc:
                            outcome["error"] = exc.code

                    worker = threading.Thread(target=run, daemon=True)
                    worker.start()
                    worker.join(8)
                    self.assertFalse(worker.is_alive(), f"{name} waited for the writer lock")
                    self.assertTrue("value" in outcome or outcome.get("error") == "data-busy", outcome)
        finally:
            release.set()
            holder.join(5)

    def test_contact_records_tolerate_corrupt_tags(self):
        store = FriendStore(self.data.stores.account("账号甲"))
        store.write(lambda connection: connection.execute(
            "UPDATE memories SET tags_json='{not json' WHERE id=?", (self.data.user_memory,)))
        records = self.friends.saved_contact_records("账号甲", "wxid_zhang")
        tags = {item["id"]: item["tags"] for item in records["memories"]}
        self.assertEqual(tags, {self.data.user_memory: [], "mem-ai": ["经历"]})
        self.assertEqual(records["memoryCounts"], {"confirmed": 1, "pending": 1})
        self.assertEqual([item["status"] for item in records["memories"]], ["confirmed", "pending"])


class WideGrantTests(ReadsHarness):
    def test_search_accepts_more_than_900_granted_contacts(self):
        scope = self.mcp.offline()
        users = frozenset({"wxid_zhang", *(f"wxid_grant_{index}" for index in range(900))})
        self.assertEqual(len(users), 901)
        result = scope.search_all({"id-A": users}, query="搬家", limit=10, offset=0, scan_max=1000)
        self.assertEqual([hit["subjectId"] for hit in result["hits"]], ["wxid_zhang"])
        self.assertEqual(result["unavailableAccounts"], [])
        listing = scope.recent_conversations({"id-A": users}, limit=10, offset=0)
        self.assertEqual([item["subjectId"] for item in listing["conversations"]], ["wxid_zhang"])
        found = scope.find_contacts({"id-A": users}, query="张三", limit=10)
        self.assertEqual([item["subjectId"] for item in found["contacts"]], ["wxid_zhang"])


class TieOrderTests(ReadsHarness):
    """Equal timestamps: one complete order in SQL truncation and in the merge."""

    QUERY = "unique-tie-query"
    EXPECTED = [("id-A", "tie_other-0"), ("id-A", "tie_user-2"), ("id-A", "tie_user-1"),
                ("id-A", "tie_user-0"), ("id-B", "tie_b-1"), ("id-B", "tie_b-0")]

    def setUp(self):
        super().setUp()
        a, b = self.data.stores.account("账号甲"), self.data.stores.account("账号乙")
        same = [(7, "other", "某人", self.QUERY)]
        self.data.add(a, "tie_user", rows("tie_user", same * 3))
        self.data.add(a, "tie_other", rows("tie_other", same))
        self.data.add(b, "tie_b", rows("tie_b", same * 2))

    def test_search_offset_paging_neither_repeats_nor_skips(self):
        principal, _ = self.pair()
        seen, cursor = [], None
        for _ in range(10):
            params = {"query": self.QUERY, "limit": 1}
            if cursor:
                params["cursor"] = cursor
            page = self.call(principal, "zhiji_search_messages", params)
            seen.extend((hit["accountId"], hit["message"]["id"]) for hit in page["hits"])
            cursor = page["cursor"]
            if cursor is None:
                break
        self.assertEqual(seen, self.EXPECTED)

    def test_search_keyset_paging_matches_the_total_order(self):
        scope = self.mcp.offline()
        allowed = {"id-A": None, "id-B": None}
        for size in (1, 2, 4):
            with self.subTest(size=size):
                seen, after = [], None
                for _ in range(10):
                    page = scope.search_all(allowed, query=self.QUERY, limit=size, offset=0, scan_max=1000,
                                            after=after, keyset=True)
                    seen.extend((hit["accountId"], hit["message"]["id"]) for hit in page["hits"])
                    after = page["next"]
                    if after is None:
                        break
                self.assertEqual(seen, self.EXPECTED)
        with self.assertRaises(McpError):
            scope.search_all(allowed, query=self.QUERY, limit=1, offset=0, scan_max=1000, after={"t": "x"})

    def test_conversation_keyset_paging_with_ties_and_new_messages(self):
        scope = self.mcp.offline()
        allowed = {"id-A": None, "id-B": None}
        full = scope.recent_conversations(allowed, limit=20, offset=0)
        order = [(item["accountId"], item["subjectId"]) for item in full["conversations"]]
        ties = [entry for entry in order if entry[1].startswith("tie_")]
        self.assertEqual(ties, [("id-A", "tie_other"), ("id-A", "tie_user"), ("id-B", "tie_b")])
        first = scope.recent_conversations(allowed, limit=2, offset=0, keyset=True)
        seen = [(item["accountId"], item["subjectId"]) for item in first["conversations"]]
        self.assertEqual(seen, order[:2])
        # wxid_li (not yet seen) gets a new message: it moves above the watermark.
        newest = {**rows("wxid_li", [(0, "other", "李四", "新消息")])[0], "msg_id": "wxid_li-new"}
        self.data.stores.account("账号甲").context_append_page("wxid_li", [newest], "{}", "{}")
        after = first["next"]
        while after is not None:
            page = scope.recent_conversations(allowed, limit=2, offset=0, after=after, keyset=True)
            seen.extend((item["accountId"], item["subjectId"]) for item in page["conversations"])
            after = page["next"]
        self.assertEqual(seen, [entry for entry in order if entry != ("id-A", "wxid_li")])
        self.assertEqual(len(seen), len(set(seen)))

    def page_all(self, principal, action, params, key, size):
        seen, cursor = [], None
        for _ in range(20):
            page = self.call(principal, action, {**params, "limit": size, **({"cursor": cursor} if cursor else {})})
            seen.extend(key(item) for item in page["hits" if "hits" in page else "conversations"])
            cursor = page["cursor"]
            if cursor is None:
                return seen
        self.fail("paging did not end")

    def test_tool_search_keyset_cursor_pages_ties_without_repeats(self):
        principal, _ = self.pair()
        for size in (1, 2, 4):
            with self.subTest(size=size):
                seen = self.page_all(principal, "zhiji_search_messages", {"query": self.QUERY},
                                     lambda hit: (hit["accountId"], hit["message"]["id"]), size)
                self.assertEqual(seen, self.EXPECTED)
        first = self.call(principal, "zhiji_search_messages", {"query": self.QUERY, "limit": 1})
        # The keyset position travels only inside the signed cursor; no raw ``next`` leaks.
        self.assertNotIn("next", first)
        self.assertNotIn("window", first["limits"])
        # A new matching message between pages is newer than the watermark: no repeats, no skips.
        newest = {**rows("tie_user", [(0, "other", "某人", self.QUERY)])[0], "msg_id": "tie_user-new"}
        self.data.stores.account("账号甲").context_append_page("tie_user", [newest], "{}", "{}")
        seen, cursor = [(hit["accountId"], hit["message"]["id"]) for hit in first["hits"]], first["cursor"]
        while cursor:
            page = self.call(principal, "zhiji_search_messages", {"query": self.QUERY, "limit": 2, "cursor": cursor})
            seen.extend((hit["accountId"], hit["message"]["id"]) for hit in page["hits"])
            cursor = page["cursor"]
        self.assertEqual(seen, self.EXPECTED)

    def test_tool_conversation_cursor_never_repeats_after_new_messages(self):
        principal, _ = self.pair()
        order = [(item["accountId"], item["subjectId"]) for item in
                 self.call(principal, "zhiji_list_recent_conversations", {"limit": 50})["conversations"]]
        for size in (1, 3):
            with self.subTest(size=size):
                self.assertEqual(self.page_all(principal, "zhiji_list_recent_conversations", {},
                                               lambda item: (item["accountId"], item["subjectId"]), size), order)
        first = self.call(principal, "zhiji_list_recent_conversations", {"limit": 3})
        self.assertNotIn("next", first)
        seen = [(item["accountId"], item["subjectId"]) for item in first["conversations"]]
        self.assertIn(("id-A", "wxid_zhang"), seen)
        self.assertNotIn(("id-B", "wxid_zhang2"), seen)
        # Between pages an already-seen and a not-yet-seen conversation get new messages. Both
        # move above the watermark: nothing repeats (offset paging would repeat the last row
        # seen once the unseen one jumps to the top) and the moved, unseen one waits for a restart.
        for account, user in (("账号甲", "wxid_zhang"), ("账号乙", "wxid_zhang2")):
            newest = {**rows(user, [(0, "other", "某人", "又一条")])[0], "msg_id": f"{user}-new"}
            self.data.stores.account(account).context_append_page(user, [newest], "{}", "{}")
        cursor = first["cursor"]
        while cursor:
            page = self.call(principal, "zhiji_list_recent_conversations", {"limit": 3, "cursor": cursor})
            seen.extend((item["accountId"], item["subjectId"]) for item in page["conversations"])
            cursor = page["cursor"]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(seen, [entry for entry in order if entry != ("id-B", "wxid_zhang2")])

    def test_offset_cursor_cannot_page_a_keyset_tool(self):
        principal, _ = self.pair()
        forged = self.mcp._next_cursor(principal, account_id=None, subject_id=None, kind="conversations",
                                       position=2)
        self.assertEqual(self.denied(principal, "zhiji_list_recent_conversations", {"cursor": forged}),
                         "cursor-invalid")
        # A keyset cursor carrying a malformed position is refused by the provider check.
        bad = self.mcp._cursor_encode({**self.mcp._cursor_payload(principal, None, None, 0),
                                       "k": "conversations", "n": {"t": "x"}})
        self.assertEqual(self.denied(principal, "zhiji_list_recent_conversations", {"cursor": bad}),
                         "cursor-invalid")


class CursorKindTests(ReadsHarness):
    """Per-contact page cursors are bound to their tool (and query/status), not just the subject."""

    def test_same_subject_tools_do_not_share_cursors(self):
        principal, _ = self.pair()
        subject = ("id-A", "wxid_zhang")
        memories = self.call(principal, "zhiji_list_memories", {"status": "active", "limit": 1}, subject=subject)
        self.assertIsNotNone(memories["cursor"])
        self.call(principal, "zhiji_list_memories", {"status": "active", "limit": 1, "cursor": memories["cursor"]},
                  subject=subject)
        self.assertEqual(self.denied(principal, "zhiji_list_observations", {"cursor": memories["cursor"]},
                                     subject=subject), "cursor-invalid")
        self.assertEqual(self.denied(principal, "zhiji_search_messages",
                                     {"query": "搬家", "cursor": memories["cursor"]}, subject=subject),
                         "cursor-invalid")
        self.assertEqual(self.denied(principal, "zhiji_list_memories",
                                     {"status": "pending", "cursor": memories["cursor"]}, subject=subject),
                         "cursor-invalid")
        search = self.call(principal, "zhiji_search_messages", {"query": "忙", "limit": 1}, subject=subject)
        self.assertIsNotNone(search["cursor"])
        self.call(principal, "zhiji_search_messages", {"query": "忙", "cursor": search["cursor"]}, subject=subject)
        self.assertEqual(self.denied(principal, "zhiji_search_messages",
                                     {"query": "搬家", "cursor": search["cursor"]}, subject=subject),
                         "cursor-invalid")
        self.assertEqual(self.denied(principal, "zhiji_list_memories", {"cursor": search["cursor"]},
                                     subject=subject), "cursor-invalid")


class BriefTruncationTests(ReadsHarness):
    def test_brief_marks_its_own_200_char_cut(self):
        a = self.data.stores.account("账号甲")
        self.data.add(a, "wxid_long", rows("wxid_long", [(1, "other", "长文", "字" * 300),
                                                         (0.9, "other", "长文", "短句")]))
        principal, _ = self.pair()
        items = self.call(principal, "zhiji_get_contact_brief", subject=("id-A", "wxid_long"))["recentMessages"]["items"]
        self.assertEqual([(len(item["text"]), item.get("truncated", False)) for item in items],
                         [(200, True), (2, False)])


class ErrorMappingTests(ReadsHarness):
    """Stable status/code mappings of the read entries (review G1-G3 P2-7)."""

    def caught(self, principal, action, params=None, **kwargs):
        with self.assertRaises(McpError) as caught:
            self.call(principal, action, params, **kwargs)
        return caught.exception.status, caught.exception.code

    def test_cross_tools_refuse_subjects_with_400(self):
        principal, _ = self.pair()
        for action, params in (("zhiji_find_contacts", {"query": "张"}), ("zhiji_list_recent_conversations", {})):
            with self.subTest(action=action):
                self.assertEqual(self.caught(principal, action, params, subject=("id-A", "wxid_zhang")),
                                 (400, "invalid-request"))

    def test_data_busy_maps_to_503_and_is_reported_per_account_in_listings(self):
        principal, _ = self.pair()

        def busy(*_args, **_kwargs):
            raise AdvisorError("data-busy", "本地资料正忙")

        for name in ("saved_recent_messages", "saved_contact_stats", "saved_conversations", "saved_search_all"):
            setattr(self.friends, name, busy)
        self.assertEqual(self.caught(principal, "zhiji_get_recent_messages", subject=("id-A", "wxid_zhang")),
                         (503, "data-busy"))
        self.assertEqual(self.caught(principal, "zhiji_get_contact_brief", subject=("id-A", "wxid_zhang")),
                         (503, "data-busy"))
        # Cross-account listings keep going and name the busy accounts instead of failing.
        listing = self.call(principal, "zhiji_list_recent_conversations", {})
        self.assertEqual(listing["conversations"], [])
        self.assertEqual({item["reason"] for item in listing["unavailableAccounts"]}, {"data-busy"})
        hits = self.call(principal, "zhiji_search_messages", {"query": "搬家"})
        self.assertEqual({item["accountId"] for item in hits["unavailableAccounts"]}, {"id-A", "id-B"})

    def test_cross_search_pages_past_the_old_500_hit_window(self):
        # The old offset window refused cursors past 500 hits ("cursor-invalid"); keyset paging
        # has no window: every hit is reachable, each exactly once.
        a = self.data.stores.account("账号甲")
        self.data.add(a, "wxid_many", rows("wxid_many", [(1 + index / 10_000, "other", "某人", "海量命中")
                                                         for index in range(520)]))
        principal, _ = self.pair()
        seen, cursor, pages = [], None, 0
        while True:
            page = self.call(principal, "zhiji_search_messages",
                             {"query": "海量命中", "limit": 50, **({"cursor": cursor} if cursor else {})})
            pages += 1
            seen.extend(hit["message"]["id"] for hit in page["hits"])
            cursor = page["cursor"]
            if cursor is None:
                break
        self.assertEqual((pages, len(seen), len(set(seen))), (11, 520, 520))
        self.assertEqual(seen[0], "wxid_many-0")  # smallest days_ago = newest


if __name__ == "__main__":
    unittest.main()
