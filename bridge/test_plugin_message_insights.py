"""kyf.message-insights end to end: Core, gateway, the bundled plugin and a fake model provider."""
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend_contracts import MessageWindow, MessageWindowBatch  # noqa: E402
from backend_service import Backend  # noqa: E402
from conversation_selection import ConversationSelectionStore  # noqa: E402
from core_account_db import AccountStores  # noqa: E402
from core_history import HistoryService  # noqa: E402
from core_human import HumanService  # noqa: E402
from core_projection import ContextProjection  # noqa: E402
from core_services import CoreServices  # noqa: E402
from real_http import make_handler  # noqa: E402
from test_support import FakeSource, make_message  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT, FRIEND = "acct-insights", "wxid_friend_x"
PROVIDER = {"calls": []}


class ProviderGate:
    def __init__(self, expected):
        self.expected, self.active, self.peak = expected, 0, 0
        self.lock = threading.Lock()
        self.entered, self.release = threading.Event(), threading.Event()
        self.steps = []

    def wait(self, target, step):
        with self.lock:
            self.active += 1
            self.peak = max(self.active, self.peak)
            self.steps.append((target, step))
            if self.active == self.expected:
                self.entered.set()
        self.release.wait(15)
        with self.lock:
            self.active -= 1


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        system = body["messages"][0]["content"]
        prompt = json.loads(body["messages"][-1]["content"])
        if prompt.get("stage") in ("extract", "review", "merge", "final"):
            step = {"extract": "profile-draft", "review": "profile-draft-review",
                    "merge": "profile-draft-merge", "final": "profile-draft-final"}[prompt["stage"]]
            PROVIDER["calls"].append(step)
            PROVIDER.setdefault("requests", []).append({"step": step, "prompt": prompt})
            return self.answer({"background": "我常约朋友看展。", "style": "先确认时间再答应。"})
        if system.startswith("你替用户本人决定怎么回复"):
            PROVIDER["calls"].append("self-trial-route")
            PROVIDER.setdefault("requests", []).append({"step": "self-trial-route", "prompt": prompt})
            return self.answer({"depth": "standard", "research": {"events": [], "periods": [], "queries": []}})
        if system.startswith("你扮演用户本人"):
            PROVIDER["calls"].append("self-trial")
            PROVIDER.setdefault("requests", []).append({"step": "self-trial", "prompt": prompt})
            return self.answer({"messages": ["我先看下时间", "晚点跟你说"][:prompt["count"]]})
        if system.startswith("你结合上下文分析用户本人的标签"):
            step = "self-tags-review" if "tags" in prompt else "self-tags"
            PROVIDER["calls"].append(step)
            PROVIDER.setdefault("requests", []).append({"step": step, "prompt": prompt})
            if step == "self-tags-review":
                return self.answer({"tags": prompt["tags"]})
            return self.answer({"tags": [{"name": name, "category": category, "evidence": [
                                            {"i": item["i"], "relation": "interest", "quote": item["me"],
                                             "reason": "本人明确表达喜爱或持续参与，前后文没有否定或转述"}]}
                                         for item in prompt["messages"]
                                         for word, name, category in (("篮球", "篮球", "运动健身"), ("象棋", "象棋", "游戏棋牌"))
                                         if word in item["me"]]})
        if system.startswith("你整理同一个人的标签"):
            PROVIDER["calls"].append("self-tags-merge")
            return self.answer({"merge": []})
        target = prompt["targets"][0]
        step = "suggestions" if system.startswith("你为用户提供") else "analysis"
        PROVIDER["calls"].append(step)
        PROVIDER.setdefault("requests", []).append({"step": step, "prompt": prompt})
        if PROVIDER.get("gate") is not None:
            PROVIDER["gate"].wait(target, step)
        if system.startswith("你为用户提供"):
            content = {"replies": [{"label": "直接", "text": "好呀，几点？"}, {"label": "确认", "text": "周六可以吗"},
                                   {"label": "轻松", "text": "听起来不错"}]}
        else:
            chat = next(source["sourceId"] for source in prompt["sources"]
                        if source["kind"] == "chat" and source.get("messageId") == target)
            content = {"analyses": [{"messageId": target, "summary": "邀请看展", "meaning": "想约周末一起看展",
                                     "uncertainty": "时间未定", "responseDirection": "确认时间", "evidenceIds": [chat]}]}
        self.answer(content)

    def answer(self, content):
        reply = json.dumps({"id": "c", "object": "chat.completion", "created": 1, "model": "model-a",
                            "choices": [{"index": 0, "finish_reason": "stop",
                                         "message": {"role": "assistant", "content": json.dumps(content, ensure_ascii=False)}}],
                            "usage": {"prompt_tokens": 50, "completion_tokens": 20}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


class LiveSource(FakeSource):
    def messages(self, user, limit):
        rows = sorted(self.users.get(user, []), key=lambda item: tuple(item["_sort"]))
        return MessageWindow([dict(row) for row in rows[-limit:]], len(rows) > limit)

    def message_windows(self, users, limit, expected_account=None):
        windows = {user: self.messages(user, limit) for user in users}
        return MessageWindowBatch(windows, {user: windows[user].has_more_before for user in users})

    def sessions(self):
        return {"account": ACCOUNT, "sessions": [{"username": FRIEND, "name": "小明", "isGroup": False}]}


class ModelStore:
    def __init__(self, url, concurrency=4):
        self.url = url
        self.concurrency = concurrency

    def saved_selection(self):
        return {"selectedMode": "api", "sourceId": "svc-insights", "api": {
            "protocol": "chat_completions", "baseUrl": self.url, "model": "model-a", "contextTokens": 32768,
            "encryptedKey": "cipher"}}

    def resolve_key(self, *_args):
        return "PROVIDER_KEY"

    def models(self):
        return {"models": [{"id": "svc-insights", "concurrency": self.concurrency}],
                "roles": {"main": "svc-insights", "judge": None, "image": None}}


class SyntheticEmbeddings:
    """No network/model downloads in integration tests; real ONNX has its own optional check."""
    def status(self):
        return {"state": "ready", "ready": True, "modelId": "synthetic-bge", "revision": "fixture-v1",
                "dimensions": 512, "totalBytes": 0, "downloadedBytes": 0}

    def prepare(self):
        return self.status()

    def embed(self, texts, kind="passage", *, plugin=None, check=None):
        if check:
            check()
        vectors = []
        for text in texts:
            topic = any(term in text for term in ("看展", "艺术馆"))
            vectors.append([1.0, 0.0] + [0.0] * 510 if topic else [0.0, 1.0] + [0.0] * 510)
        return {**self.status(), "vectors": vectors}

    def close(self):
        pass


def message(seq, side, text):
    value = make_message(seq, side=side, sender=FRIEND if side == "other" else "wxid_me", text=text,
                         time_ms=int(time.time() * 1000) + seq * 10)
    value["_senderResolved"] = True
    return value


class MessageInsightsTests(unittest.TestCase):
    def setUp(self):
        PROVIDER["calls"].clear()
        PROVIDER["requests"] = []
        PROVIDER["gate"] = None
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        threading.Thread(target=self.provider.serve_forever, daemon=True).start()
        self.source = LiveSource(account=ACCOUNT)
        self.source.add_messages(FRIEND, [message(1, "self", "最近怎么样"), message(2, "other", "周末一起去看展吗？")])
        selection = ConversationSelectionStore(base / "selection")
        selection.set_all_selected(ACCOUNT, [FRIEND])
        self.backend = Backend(self.source, selection_store=selection,
                               model_source_store=ModelStore(f"http://127.0.0.1:{self.provider.server_port}/v1"))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.backend))
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.core = CoreServices(self.backend, ROOT, self.url, local=base / ".local", bundled_dir=ROOT / "plugins",
                                 local_dir=base / "none")
        self.core._embeddings = SyntheticEmbeddings()
        self.stores = AccountStores(base)
        self.context = ContextProjection(self.stores, self.source)
        self.core._history = HistoryService(self.stores, self.context)
        self.core._human = HumanService(self.stores, source=self.source)
        self.core.events.poll_seconds = 0.2
        self.backend._core = self.core

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.core.close()
        self.context.close()
        self.stores.close()
        self.provider.shutdown()
        self.provider.server_close()
        self.temp.cleanup()

    def rpc(self, method, params=None):
        body = {"plugin": "kyf.message-insights", "method": method, "params": params or {},
                "account": ACCOUNT, "user": FRIEND}
        request = urllib.request.Request(self.url + "/api/plugins/rpc", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())["result"]

    def wait_ready(self, ids, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.rpc("annotations.status", {"messageIds": ids})
            if len(status["items"]) == len(ids) and all(item["state"] in ("ready", "error", "partial", "stale") for item in status["items"]):
                return status
            time.sleep(0.2)
        self.fail(f"not ready: {status}")

    def test_enable_analyses_the_unanswered_message_and_new_ones(self):
        target = self.source.users[FRIEND][-1]["id"]
        state = self.rpc("chatHeader.toggle", {"enabled": True})
        self.assertTrue(state["enabled"])
        status = self.wait_ready([target])
        item = status["items"][0]
        self.assertEqual((item["state"], item["applicability"]), ("ready", "current"))
        self.assertEqual(item["analysis"]["summary"], "邀请看展")
        self.assertEqual(item["analysis"]["evidence"][0]["messageId"], target)
        self.assertEqual([reply["text"] for reply in item["replies"]][0], "好呀，几点？")
        copied = self.rpc("annotations.copy", {"messageId": target, "version": item["version"], "index": 1})
        self.assertEqual(copied["text"], "周六可以吗")
        # A newer incoming message arrives through Core's message.new event.
        self.source.add_messages(FRIEND, [message(3, "other", "下午两点美术馆门口见")])
        newest = self.source.users[FRIEND][-1]["id"]
        status = self.wait_ready([target, newest])
        by_id = {entry["messageId"]: entry for entry in status["items"]}
        self.assertEqual(by_id[newest]["state"], "ready")
        self.assertEqual(by_id[target]["applicability"], "historical")
        records = self.core.ledger.records({"account": ACCOUNT})["records"]
        self.assertEqual({record["step"] for record in records}, {"analysis", "suggestions"})
        self.assertTrue(all(record["plugin"] == "kyf.message-insights" for record in records))

    def self_rpc(self, method, params=None):
        body = {"plugin": "kyf.self-analysis", "method": method, "params": params or {}, "account": ACCOUNT}
        request = urllib.request.Request(self.url + "/api/plugins/rpc", data=json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)["result"]

    def build_self_library(self):
        """Two private chats, one of them outside the selected conversation list; returns that one's id."""
        hidden = "synthetic-unselected-friend"
        # No Chinese word/character overlap with the hidden incoming message.
        # The fixture encoder maps the two situations, exercising independent semantic recall.
        self.source.users[FRIEND][-1]["text"] = "参观艺术馆，有空相聚么？"
        past = 1_700_000_000_000
        self.source.add_messages(hidden, [
            make_message(1, sender=hidden, text="周末一起去看展吗？", time_ms=past),
            make_message(2, side="self", sender="wxid_me", text="我先看下时间，晚点跟你说", time_ms=past + 1000),
            make_message(3, sender=hidden, text="好，晚点再说", time_ms=past + 2000),
        ])
        for row in self.source.users[hidden]:
            row["_senderResolved"] = True
        self.source.history_conversations = lambda: {
            "account": ACCOUNT, "conversations": [
                {"user": FRIEND, "name": "合成当前好友", "isGroup": False},
                {"user": hidden, "name": "合成未选私聊", "isGroup": False}],
            "coverage": {"state": "complete", "readableShards": 1, "failedShards": [], "unresolvedTables": 0}}
        self.self_rpc("self.build")
        deadline = time.monotonic() + 30
        state = None
        while time.monotonic() < deadline:
            state = self.self_rpc("self.status")
            if state["build"]["state"] in ("complete", "partial", "failed") and state["embedding"]["indexState"] == "ready":
                break
            time.sleep(0.1)
        return hidden, state

    def test_self_analysis_builds_unselected_private_history_and_supplies_only_reply_expression(self):
        """Real bundled processes + Core + fake provider; never a real account or model."""
        hidden, state = self.build_self_library()
        self_rpc = self.self_rpc
        self.assertEqual(state["build"]["state"], "complete", state)
        self.assertEqual(state["coverage"]["discoveredConversations"], 2)
        self.assertGreater(state["coverage"]["sampleCount"], 0)
        self.assertEqual(state["retrieval"]["mode"], "hybrid", state)
        self.assertEqual(state["retrieval"]["pendingSamples"], 0, state)
        found = self_rpc("self.search", {"query": "参观艺术馆，有空相聚么？"})
        self.assertEqual(found["mode"], "hybrid", found)
        match = next(row for row in found["sources"] if row["user"] == hidden)
        self.assertEqual(match["matchKinds"], ["semantic"])
        comparison = self_rpc("self.compare", {"query": "参观艺术馆，有空相聚么？", "limit": 4})
        self.assertTrue(comparison["consistent"], comparison)
        self.assertTrue(comparison["semantic"]["used"], comparison)
        self.assertEqual(comparison["results"]["lexical"]["sources"], [])
        compared = next(row for row in comparison["results"]["hybrid"]["sources"] if row["user"] == hidden)
        self.assertEqual(compared["id"], match["id"])
        self.assertEqual(comparison["overlap"]["hybridOnlyIds"], [match["id"]])
        self.assertEqual(PROVIDER["calls"], [], "local indexing does not require or call a generation model")
        target = self.source.users[FRIEND][-1]["id"]
        self.rpc("chatHeader.toggle", {"enabled": True})
        self.rpc("annotations.generate", {"messageId": target})
        item = self.wait_ready([target])["items"][0]
        self.assertEqual(item["suggestionsState"], "ready", item)
        requests = PROVIDER["requests"]
        analysis = next(row["prompt"] for row in requests if row["step"] == "analysis")
        reply = next(row["prompt"] for row in requests if row["step"] == "suggestions")
        self.assertNotIn("personalReplyStyle", analysis)
        examples = reply["personalReplyStyle"]["examples"]
        self.assertTrue(any(row["ownReply"] == "我先看下时间，晚点跟你说" and not row["sameContact"] for row in examples))
        self.assertNotIn(hidden, json.dumps(reply["personalReplyStyle"], ensure_ascii=False))
        self.assertTrue(all(row["kind"] != "example" for row in reply["sources"]))

    def test_self_analysis_drafts_profile_from_all_own_text_without_a_reply_library(self):
        """Real Core + plugin + gateway cover groups, proactive text and more than 1,500 messages."""
        private, group = "synthetic-profile-private", "synthetic-profile-group@chatroom"
        own_private = ["我主动说一下最近的安排", "好", "🙂", "长段落" * 240 + "结尾也必须读到"]
        own_private.extend(f"第 {index} 条合成主动发言" for index in range(1504))
        own_group = ["我在群里主动分享近况", "行"]
        self.source.add_messages(private, [make_message(index + 1, side="self", sender="wxid_me", text=text)
                                           for index, text in enumerate(own_private)])
        self.source.add_messages(group, [
            make_message(1, side="other", sender="wxid_member", text="别人的兴趣不能变成本人的"),
            make_message(2, side="self", sender="wxid_me", text=own_group[0]),
            make_message(3, side="self", sender="wxid_me", text=own_group[1]),
            make_message(4, side="self", sender="wxid_me", text="[图片]", kind="image")])
        self.source.history_conversations = lambda: {
            "account": ACCOUNT, "conversations": [{"user": private, "name": "合成主动私聊", "isGroup": False},
                                                   {"user": group, "name": "合成个人资料群", "isGroup": True}],
            "coverage": {"state": "complete", "readableShards": 1, "failedShards": [], "unresolvedTables": 0}}
        before = self.self_rpc("self.status")
        self.assertEqual(before["coverage"]["sampleCount"], 0, "profile generation must not require reply pairs")
        started = self.self_rpc("self.draft", {"background": "我在上海工作", "style": "", "depth": "basic"})
        deadline = time.monotonic() + 30
        status = started
        while status["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.1)
            status = self.self_rpc("self.draftStatus")
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(status["result"], {"background": "我常约朋友看展。", "style": "先确认时间再答应。"})
        coverage = status["coverage"]
        total = len(own_private) + len(own_group)
        self.assertGreater(total, 1500)
        self.assertEqual((coverage["ownMessages"], coverage["analyzedMessages"]), (total, total))
        self.assertEqual((coverage["privateMessages"], coverage["groupMessages"]), (len(own_private), len(own_group)))
        self.assertEqual((coverage["conversations"], coverage["readConversations"], coverage["failedConversations"]), (2, 2, 0))
        self.assertEqual(coverage["gaps"], [])
        self.assertFalse(coverage["directoryPartial"])
        self.assertEqual(coverage["analyzedParts"], coverage["totalParts"])
        prompts = [row["prompt"] for row in PROVIDER["requests"] if row["step"] == "profile-draft"]
        self.assertTrue(prompts)
        final = next(row["prompt"] for row in PROVIDER["requests"] if row["step"] == "profile-draft-final")
        self.assertEqual(final["existing"], {"background": "我在上海工作", "style": ""})
        messages = [item for prompt in prompts for item in prompt["messages"]]
        reconstructed = {}
        for item in messages:
            reconstructed.setdefault(item["id"], []).append(item)
        texts = ["".join(part["text"] for part in sorted(parts, key=lambda part: part["part"]))
                 for parts in reconstructed.values()]
        self.assertCountEqual(texts, own_private + own_group)
        self.assertNotIn("别人的兴趣不能变成本人的", texts)
        self.assertNotIn("[图片]", texts)
        records = self.core.ledger.records({"account": ACCOUNT})["records"]
        self.assertTrue(all(record["plugin"] == "kyf.self-analysis" for record in records))
        self.assertIn("profile-draft", {record["step"] for record in records})
        self.assertTrue(all(record["step"].startswith("profile-draft") for record in records))
        self.assertEqual(self.core.human.self_profile(ACCOUNT)["version"], 0, "a draft is never saved by the plugin")

    def test_self_analysis_depths_come_from_the_plugin_settings(self):
        self.assertEqual(self.self_rpc("self.analysisSettings")["depth"], "deep")
        saved = self.core.registry.settings("kyf.self-analysis")
        self.assertEqual(saved["values"], {"analysisDepth": "deep", "analysisGrain": "6", "replyDepth": "auto"})
        self.core.registry.save_settings("kyf.self-analysis", saved["revision"],
                                         {**saved["values"], "analysisDepth": "standard"}, saved["definition"])
        self.assertEqual(self.self_rpc("self.analysisSettings")["depth"], "standard")
        saved = self.core.registry.settings("kyf.self-analysis")
        self.core.registry.save_settings("kyf.self-analysis", saved["revision"],
                                         {**saved["values"], "analysisDepth": "deep"}, saved["definition"])
        self.source.history_conversations = lambda: {
            "account": ACCOUNT, "conversations": [{"user": FRIEND, "name": "合成当前好友", "isGroup": False}],
            "coverage": {"state": "complete", "readableShards": 1, "failedShards": [], "unresolvedTables": 0}}
        status = self.self_rpc("self.draft", {})
        deadline = time.monotonic() + 30
        while status["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.1)
            status = self.self_rpc("self.draftStatus")
        self.assertEqual(status["state"], "done", status)
        self.assertEqual(status["depth"], "deep")
        self.assertEqual(status["coverage"]["analyzedMessages"], 1)
        self.assertIn("profile-draft-review", PROVIDER["calls"])
        self.assertIn("profile-draft-final", PROVIDER["calls"])
        self.assertEqual(self.core.human.self_profile(ACCOUNT)["version"], 0)

    def test_self_analysis_draft_preserves_source_gaps_after_analyzing_readable_text(self):
        self.source.history_conversations = lambda: {
            "account": ACCOUNT, "conversations": [{"user": FRIEND, "name": "合成可读会话", "isGroup": False}],
            "coverage": {"state": "partial", "readableShards": 1,
                         "failedShards": ["synthetic-unreadable.db"], "unresolvedTables": 1}}
        status = self.self_rpc("self.draft", {"depth": "basic"})
        deadline = time.monotonic() + 30
        while status["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.1)
            status = self.self_rpc("self.draftStatus")
        self.assertEqual(status["state"], "done", status)
        self.assertTrue(status["coverage"]["directoryPartial"])
        self.assertTrue(status["coverage"]["gaps"])
        self.assertEqual(status["coverage"]["analyzedMessages"], 1)
        self.assertNotIn("synthetic-unreadable.db", json.dumps(status, ensure_ascii=False),
                         "coverage reports safe gap categories, never source database paths")

    def test_self_analysis_trial_replies_as_the_user_from_retrieved_own_replies(self):
        _hidden, state = self.build_self_library()
        self.assertEqual(state["build"]["state"], "complete", state)
        turns = [{"from": "friend", "text": "周末一起去看展吗？"}]
        status = self.self_rpc("self.reply", {"messages": turns})
        deadline = time.monotonic() + 30
        while status["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.1)
            status = self.self_rpc("self.replyStatus")
        self.assertEqual(status["state"], "done", status)
        prompt = next(row["prompt"] for row in PROVIDER["requests"] if row["step"] == "self-trial")
        self.assertEqual(status["result"], {"messages": ["我先看下时间", "晚点跟你说"][:prompt["count"]]})
        self.assertIn("我先看下时间，晚点跟你说", [row["reply"] for row in status["sources"]])
        self.assertEqual(prompt["conversation"], turns)
        self.assertEqual([row["reply"] for row in prompt["examples"]], [row["reply"] for row in status["sources"]])
        records = self.core.ledger.records({"account": ACCOUNT})["records"]
        self.assertEqual({(record["plugin"], record["step"]) for record in records},
                         {("kyf.self-analysis", "self-trial-route"), ("kyf.self-analysis", "self-trial")})
        self.assertEqual(self.core.human.self_profile(ACCOUNT)["version"], 0, "trial never saves the profile")

    def test_self_analysis_tags_read_own_messages_in_group_and_private_chats_with_evidence(self):
        group, private = "synthetic-group@chatroom", "synthetic-unselected-friend"
        past = 1_700_000_000_000
        self.source.add_messages(group, [
            make_message(1, sender="wxid_member", name="合成群友", text="周六谁来打球？", time_ms=past),
            make_message(2, side="self", sender="wxid_me", text="我来，我每周都打篮球", time_ms=past + 1000)])
        self.source.add_messages(private, [
            make_message(1, side="self", sender="wxid_me", text="最近迷上下象棋了", time_ms=past + 2000)])
        self.source.history_conversations = lambda: {
            "account": ACCOUNT, "conversations": [{"user": group, "name": "合成篮球群", "isGroup": True},
                                                  {"user": private, "name": "合成未选私聊", "isGroup": False}],
            "coverage": {"state": "complete", "readableShards": 1, "failedShards": [], "unresolvedTables": 0}}
        status = self.self_rpc("self.tagsAnalyze")
        deadline = time.monotonic() + 30
        while status["run"]["state"] == "running" and time.monotonic() < deadline:
            time.sleep(0.1)
            status = self.self_rpc("self.tags")
        self.assertEqual(status["run"]["state"], "complete", status["run"])
        self.assertEqual(status["counts"]["groups"], 1)
        self.assertEqual({(tag["name"], tag["category"], tag["level"]) for tag in status["tags"]},
                         {("篮球", "运动健身", "strong"), ("象棋", "游戏棋牌", "strong")})
        prompt = next(row["prompt"] for row in PROVIDER["requests"] if row["step"] == "self-tags")
        self.assertEqual(sorted(item["me"] for item in prompt["messages"]), sorted(["我来，我每周都打篮球", "最近迷上下象棋了"]),
                         "a group member's words are context, never the owner's tag source")
        evidence = self.self_rpc("self.tagEvidence", {"name": "篮球"})
        self.assertEqual([(item["chat"], item["isGroup"], item["context"], item["text"]) for item in evidence["items"]],
                         [("合成篮球群", True, "合成群友：周六谁来打球？", "我来，我每周都打篮球")])
        self.assertEqual([(item["relation"], item["quote"]) for item in evidence["items"]],
                         [("interest", "我来，我每周都打篮球")])
        self.assertTrue(all(item["reason"] for item in evidence["items"]))
        review = next(row["prompt"] for row in PROVIDER["requests"] if row["step"] == "self-tags-review")
        self.assertEqual(review["messages"], prompt["messages"], "independent review must receive the original evidence context")
        self.assertEqual({tag["name"] for tag in review["tags"]}, {"篮球", "象棋"})
        status = self.self_rpc("self.tagEdit", {"action": "delete", "name": "象棋"})
        self.assertEqual([tag["name"] for tag in status["tags"]], ["篮球"])
        records = self.core.ledger.records({"account": ACCOUNT})["records"]
        self.assertEqual({(record["plugin"], record["step"]) for record in records},
                         {("kyf.self-analysis", "self-tags"), ("kyf.self-analysis", "self-tags-review")})

    def test_existing_results_move_into_the_plugin_once(self):
        database = self.stores.account(ACCOUNT)
        connection = database._connect()
        with database.lock, connection:
            connection.executescript(
                "CREATE TABLE message_analysis_settings (user TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, "
                "revision INTEGER NOT NULL DEFAULT 0, unified_enabled INTEGER);"
                "CREATE TABLE message_analyses (user TEXT NOT NULL, message_id TEXT NOT NULL, generation TEXT NOT NULL, "
                "state TEXT NOT NULL, scope TEXT, config_stamp TEXT, profile_stamp TEXT, context_epoch TEXT, "
                "message_hash TEXT, analysis_json TEXT, error TEXT, updated_at_ms INTEGER NOT NULL, analysis_state TEXT, "
                "suggestions_state TEXT NOT NULL DEFAULT 'none', replies_json TEXT, suggestions_error TEXT, "
                "context_hashes_json TEXT, context_version TEXT, schema_version INTEGER NOT NULL DEFAULT 1, "
                "PRIMARY KEY(user,message_id));")
            connection.execute("INSERT INTO message_analysis_settings VALUES(?,0,3,0)", (FRIEND,))
            connection.execute("INSERT INTO message_analyses(user,message_id,generation,state,error,updated_at_ms,"
                               "analysis_state,suggestions_state,schema_version) VALUES(?,?,?,?,?,?,?,?,2)",
                               (FRIEND, "m-old", "analysis_old", "error", "旧错误", 1, "error", "none"))
        status = self.rpc("annotations.status", {"messageIds": ["m-old"]})
        self.assertFalse(status["enabled"])
        self.assertEqual((status["items"][0]["state"], status["items"][0]["error"]), ("error", "旧错误"))
        self.assertTrue(Path(database.path).with_name("advisor.pre-plugins.sqlite3").is_file())
        # The original rows stay for older versions; a second start does not copy again.
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM message_analyses").fetchone()[0], 1)

    def test_copy_rpc_recovers_from_one_failed_fresh_identity_check(self):
        from plugin_api import ApiError
        target = self.source.users[FRIEND][-1]["id"]
        self.rpc("chatHeader.toggle", {"enabled": True})
        item = self.wait_ready([target])["items"][0]
        self.assertTrue(item["copyAllowed"])
        calls_before = len(PROVIDER["calls"])
        original_status = self.core.history.status
        fresh_checks = []

        def status(account, user, fresh=False):
            if fresh:
                fresh_checks.append(True)
                if len(fresh_checks) == 1:
                    raise ApiError(503, "source-unavailable", "Synthetic ownership read unavailable")
            return original_status(account, user, fresh=fresh)

        with patch.object(self.core.history, "status", side_effect=status):
            copied = self.rpc("annotations.copy", {"messageId": target, "version": item["version"], "index": 0})
        self.assertEqual(copied["text"], item["replies"][0]["text"])
        self.assertEqual(len(fresh_checks), 3)
        self.assertEqual(len(PROVIDER["calls"]), calls_before)

    def test_copy_rpc_rechecks_import_race_but_still_blocks_a_real_source_switch(self):
        target = self.source.users[FRIEND][-1]["id"]
        self.rpc("chatHeader.toggle", {"enabled": True})
        item = self.wait_ready([target])["items"][0]
        original_status, original_rows = self.core.history.status, self.core.history.rows
        fresh_checks, row_reads = [], []

        def status(account, user, fresh=False):
            result = original_status(account, user, fresh=fresh)
            if fresh:
                fresh_checks.append(True)
                if len(fresh_checks) == 1:
                    result["state"] = "reading"
            return result

        def rows(*args, **kwargs):
            row_reads.append(True)
            return {"messages": []} if len(fresh_checks) == 1 else original_rows(*args, **kwargs)

        with patch.object(self.core.history, "status", side_effect=status), \
                patch.object(self.core.history, "rows", side_effect=rows):
            copied = self.rpc("annotations.copy", {"messageId": target, "version": item["version"], "index": 1})
        self.assertEqual(copied["text"], item["replies"][1]["text"])
        self.assertEqual(len(fresh_checks), 4)
        self.assertEqual(len(row_reads), 2)
        self.assertEqual(self.rpc("annotations.status", {"messageIds": [target]})["items"][0]["replies"], item["replies"])

        def changed(account, user, fresh=False):
            result = original_status(account, user, fresh=fresh)
            if fresh:
                result["currentSourceFingerprint"] = "synthetic-different-source"
            return result

        with patch.object(self.core.history, "status", side_effect=changed), self.assertRaises(urllib.error.HTTPError) as error:
            self.rpc("annotations.copy", {"messageId": target, "version": item["version"], "index": 1})
        self.assertEqual(error.exception.code, 422)
        with error.exception:
            self.assertEqual(json.load(error.exception)["error"], "source-changed")

    def test_explicit_generation_of_answered_history_is_complete_and_idempotent(self):
        target = self.source.users[FRIEND][-1]["id"]
        self.source.add_messages(FRIEND, [message(3, "self", "可以，周末见")])
        # This case exercises old, answered history. The live-message fixture
        # otherwise timestamps rows in the current (or next) instant, which can
        # legitimately enter the replay-live window depending on the wall clock.
        historical_time = int(time.time() * 1000) - 60_000
        for index, row in enumerate(self.source.users[FRIEND]):
            row["time"] = historical_time + index * 1000
        self.context.prepare(ACCOUNT, FRIEND)
        deadline = time.monotonic() + 10
        while self.core.history.status(ACCOUNT, FRIEND)["state"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.core.history.status(ACCOUNT, FRIEND)["state"], "ready")
        state = self.rpc("chatHeader.toggle", {"enabled": True})
        self.assertTrue(state["enabled"])
        self.assertEqual(state["items"], [])
        self.assertEqual(PROVIDER["calls"], [])

        self.rpc("annotations.generate", {"messageId": target})
        item, = self.wait_ready([target])["items"]
        self.assertEqual((item["analysisState"], item["suggestionsState"], item["applicability"]),
                         ("ready", "ready", "historical"))
        self.assertEqual(len(item["replies"]), 3)
        self.assertEqual(item["analysis"]["evidence"][0]["messageId"], target)
        again = self.rpc("annotations.generate", {"messageId": target})["items"][0]
        self.assertEqual((again["version"], again["state"]), (item["version"], "ready"))
        self.assertEqual(PROVIDER["calls"], ["analysis", "suggestions"])

    def test_migrated_enabled_friend_restores_watch_and_accepts_delayed_first_event(self):
        database = self.stores.account(ACCOUNT)
        connection = database._connect()
        with database.lock, connection:
            connection.execute("CREATE TABLE message_analysis_settings (user TEXT PRIMARY KEY, "
                               "enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, "
                               "unified_enabled INTEGER)")
            connection.execute("INSERT INTO message_analysis_settings VALUES(?,0,3,1)", (FRIEND,))
        self.assertEqual(self.core.store.subscriptions(account=ACCOUNT), [])
        # Core has been live for a while; the plugin has not started yet.
        self.core.events.started_at_ms = int(time.time() - 10) * 1000
        for row in self.source.users[FRIEND]:
            row["time"] = self.core.events.started_at_ms - 1000
        target = self.source.users[FRIEND][-1]["id"]
        state = self.rpc("annotations.status", {"messageIds": [target]})
        self.assertTrue(state["enabled"])
        self.assertEqual(state["items"], [])
        deadline = time.monotonic() + 10
        while not self.core.store.subscriptions(account=ACCOUNT) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.core.store.subscriptions(account=ACCOUNT)), 1)
        self.assertEqual(PROVIDER["calls"], [])
        incoming = message(3, "other", "合成的延迟投递消息")
        incoming["time"] = self.core.events.started_at_ms + 2000
        self.source.add_messages(FRIEND, [incoming])
        self.core.events.wake.set()
        item, = self.wait_ready([incoming["id"]])["items"]
        self.assertEqual((item["analysisState"], item["suggestionsState"]), ("ready", "ready"))
        self.assertEqual(self.rpc("annotations.status", {"messageIds": [target]})["items"], [])
        self.assertEqual(PROVIDER["calls"], ["analysis", "suggestions"])

    def test_first_restored_subscription_keeps_live_messages_arriving_before_plugin_start(self):
        database = self.stores.account(ACCOUNT)
        connection = database._connect()
        with database.lock, connection:
            connection.execute("CREATE TABLE message_analysis_settings (user TEXT PRIMARY KEY, "
                               "enabled INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0, "
                               "unified_enabled INTEGER)")
            connection.execute("INSERT INTO message_analysis_settings VALUES(?,0,3,1)", (FRIEND,))
        self.core.events.started_at_ms = int(time.time() - 10) * 1000
        for row in self.source.users[FRIEND]:
            row["time"] = self.core.events.started_at_ms - 1000
        old = self.source.users[FRIEND][-1]["id"]
        incoming = message(3, "other", "合成的启动后新消息")
        incoming["time"] = self.core.events.started_at_ms + 2000
        self.source.add_messages(FRIEND, [incoming])
        self.assertIsNone(self.core.store.position("kyf.message-insights", ACCOUNT, FRIEND))
        # The status read starts the lazy plugin; only its watch restores generation.
        self.rpc("annotations.status", {"messageIds": []})
        item, = self.wait_ready([incoming["id"]])["items"]
        self.assertEqual((item["analysisState"], item["suggestionsState"]), ("ready", "ready"))
        self.assertEqual(self.rpc("annotations.status", {"messageIds": [old]})["items"], [])
        self.assertEqual(PROVIDER["calls"], ["analysis", "suggestions"])

    def test_enable_does_not_lose_message_between_history_snapshot_and_subscription(self):
        initial = self.source.users[FRIEND][-1]["id"]
        recent = self.core.history.recent
        arrivals = []

        def snapshot_then_arrival(account, user, limit=200):
            snapshot = recent(account, user, limit)
            if not arrivals:
                incoming = message(3, "other", "合成的开启瞬间新消息")
                arrivals.append(incoming["id"])
                self.source.add_messages(FRIEND, [incoming])
            return snapshot

        self.core.history.recent = snapshot_then_arrival
        self.rpc("chatHeader.toggle", {"enabled": True})
        self.assertEqual(len(arrivals), 1)
        items = self.wait_ready([initial, arrivals[0]])["items"]
        self.assertTrue(all(item["state"] == "ready" for item in items))
        self.assertEqual(PROVIDER["calls"].count("analysis"), 2)
        self.assertEqual(PROVIDER["calls"].count("suggestions"), 2)

    def test_manual_generation_rejects_unresolved_sender_display_fallback(self):
        target = self.source.users[FRIEND][-1]
        target["_senderResolved"] = False
        # The source display fallback still looks like a normal incoming friend.
        self.assertEqual((target["side"], target["senderId"]), ("other", FRIEND))
        self.context.prepare(ACCOUNT, FRIEND)
        deadline = time.monotonic() + 10
        while self.core.history.status(ACCOUNT, FRIEND)["state"] != "ready" and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual(self.core.history.status(ACCOUNT, FRIEND)["state"], "ready")
        self.rpc("chatHeader.toggle", {"enabled": True})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.rpc("annotations.generate", {"messageId": target["id"]})
        with error.exception as response:
            self.assertEqual(json.loads(response.read())["error"], "not-found")
        self.assertEqual(self.rpc("annotations.status", {"messageIds": [target["id"]]})["items"], [])
        self.assertEqual(PROVIDER["calls"], [])

    def test_configured_three_tasks_reach_provider_concurrently_with_distinct_results(self):
        self.backend.model_source_store.concurrency = 3
        self.core.gateway.reconfigure()
        self.source.add_messages(FRIEND, [message(3, "other", "合成消息二"), message(4, "other", "合成消息三")])
        ids = [item["id"] for item in self.source.users[FRIEND] if item["side"] == "other"]
        gate = PROVIDER["gate"] = ProviderGate(3)
        try:
            self.rpc("chatHeader.toggle", {"enabled": True})
            self.assertTrue(gate.entered.wait(12), "three independent model requests did not reach the provider")
            with gate.lock:
                self.assertEqual((gate.active, gate.peak), (3, 3))
                self.assertEqual({target for target, _step in gate.steps}, set(ids))
                self.assertTrue(all(step == "analysis" for _target, step in gate.steps))
        finally:
            gate.release.set()
        items = self.wait_ready(ids)["items"]
        self.assertEqual(len(items), 3)
        for item in items:
            self.assertEqual((item["analysisState"], item["suggestionsState"]), ("ready", "ready"))
            self.assertEqual(item["analysis"]["evidence"][0]["messageId"], item["messageId"])
            self.assertEqual(len(item["replies"]), 3)
            self.assertEqual([step for target, step in gate.steps if target == item["messageId"]],
                             ["analysis", "suggestions"])

    def test_disable_stops_watching(self):
        self.rpc("chatHeader.toggle", {"enabled": True})
        self.wait_ready([self.source.users[FRIEND][-1]["id"]])
        state = self.rpc("chatHeader.toggle", {"enabled": False})
        self.assertFalse(state["enabled"])
        self.assertEqual(self.core.store.subscriptions(account=ACCOUNT), [])
        calls = len(PROVIDER["calls"])
        self.source.add_messages(FRIEND, [message(4, "other", "在吗")])
        time.sleep(1.0)
        self.assertEqual(len(PROVIDER["calls"]), calls)

    def test_new_arrival_gets_next_provider_slot_ahead_of_old_backlog(self):
        self.backend.model_source_store.concurrency = 1
        self.core.gateway.reconfigure()
        self.source.add_messages(FRIEND, [message(3, "other", "合成旧消息二"), message(4, "other", "合成旧消息三")])
        old_ids = [row["id"] for row in self.source.users[FRIEND] if row["side"] == "other"]
        gate = PROVIDER["gate"] = ProviderGate(1)
        try:
            self.rpc("chatHeader.toggle", {"enabled": True})
            self.assertTrue(gate.entered.wait(12), "first analysis did not reach the provider")
            with gate.lock:
                self.assertEqual(gate.steps, [(old_ids[-1], "analysis")])
            incoming = message(5, "other", "合成的紧急新消息")
            self.source.add_messages(FRIEND, [incoming])
            self.core.events.wake.set()
            deadline = time.monotonic() + 12
            queued = []
            while time.monotonic() < deadline:
                queued = self.rpc("annotations.status", {"messageIds": [incoming["id"]]})["items"]
                if queued:
                    break
                time.sleep(0.1)
            self.assertEqual([item["state"] for item in queued], ["pending"])
            with gate.lock:
                self.assertEqual((gate.active, gate.peak), (1, 1))
                self.assertEqual(gate.steps, [(old_ids[-1], "analysis")], "running task was interrupted")
        finally:
            gate.release.set()
        completed = self.wait_ready([*old_ids, incoming["id"]])["items"]
        self.assertTrue(all(item["state"] == "ready" for item in completed))
        with gate.lock:
            order = [target for target, step in gate.steps if step == "analysis"]
        self.assertEqual(order, [old_ids[-1], incoming["id"], *reversed(old_ids[:-1])])
        self.assertEqual(PROVIDER["calls"].count("analysis"), 4)
        self.assertEqual(PROVIDER["calls"].count("suggestions"), 4)


if __name__ == "__main__":
    unittest.main()
