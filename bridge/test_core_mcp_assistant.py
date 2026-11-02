"""Phase-3b chat-assistant and external-import tools on synthetic data: per-contact
authorization of every @ participant, independent external_contact grants, idempotent
submissions, revocation stopping question runs and import batches, subject-filtered
reads and paging. Nothing here calls a model or touches a real credential."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_external_store import ExternalChatStore
from core_mcp_assistant import McpAssistantBridge
from core_mcp_import import McpImportBridge
from core_mcp_proxy import EXTERNAL_ACCOUNT, McpError, McpServices
from core_plugin_process import PluginError, PluginUnavailable
from test_core_mcp import stored_dump
from test_core_mcp_runtime import FINGERPRINT, make_services, pair, headers

SUBJECT_A = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友1"}]
SUBJECT_B = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友2"}]


def external_subject(friend_id):
    return [{"accountId": EXTERNAL_ACCOUNT, "subjectType": "external_contact",
             "subjectId": friend_id}]


class FakeAssistant:
    """Synthetic advisor surface; records every dispatch the proxy makes."""

    def __init__(self):
        self.calls = []
        self.sequence = 0
        self.catalog_view = {"agents": [{"id": "agent-1", "name": "合成模板", "enabled": True}],
                             "skills": [], "engine": {"state": "available"}}

    def resolve_account(self, account_id):
        if account_id != "id-A":
            raise McpError(404, "account-unknown", "该账号不是本机已保存账号")
        return "账号甲"

    def catalog(self):
        self.calls.append(("catalog.get",))
        return self.catalog_view

    def save_template(self, agent):
        self.calls.append(("catalog.saveAgent", dict(agent)))
        return self.catalog_view

    def enable_template(self, agent_id, enabled):
        self.calls.append(("catalog.enableAgent", agent_id, enabled))
        return self.catalog_view

    def delete_template(self, agent_id):
        self.calls.append(("catalog.deleteAgent", agent_id))
        return self.catalog_view

    def import_skill(self, name, description, content):
        self.calls.append(("catalog.importSkill", name))
        return self.catalog_view

    def delete_skill(self, skill_id):
        self.calls.append(("catalog.deleteSkill", skill_id))
        return self.catalog_view

    def thread(self, account, user, agent_id, thread_id, before_seq=None):
        self.calls.append(("thread.get", account, user, agent_id, thread_id, before_seq))
        return {"thread": {"id": thread_id, "draft": {"text": "用户自己的草稿", "version": 3},
                           "queue": {"version": 1, "items": []}}, "run": None}

    def threads(self, account, user, agent_id=None):
        self.calls.append(("threads.list", account, user, agent_id))
        return {"threads": [{"id": "t1", "title": "合成讨论"}], "selectedThreadId": "t1"}

    def friends(self, account, user):
        self.calls.append(("friends.list", account, user))
        return {"friends": [{"user": "好友1", "name": "好友一"},
                            {"user": "好友2", "name": "好友二"},
                            {"user": "好友3", "name": "好友三"}]}

    def new_thread(self, account, user, agent_id):
        self.calls.append(("thread.new", account, user, agent_id))
        return {"thread": {"id": "t2", "title": "开始新讨论"}}

    def start(self, account, user, agent_id, thread_id, message, *, thinking=True, mentions=None):
        self.calls.append(("run.start", account, user, agent_id, thread_id, message,
                           thinking, list(mentions or [])))
        self.sequence += 1
        return {"run": {"id": f"run-{self.sequence:03d}", "state": "running"},
                "threadId": thread_id, "participants": [{"user": mention, "name": mention}
                                                        for mention in (mentions or [])]}

    def stop(self, account, user, run_id):
        self.calls.append(("run.stop", account, user, run_id))
        return {"run": {"id": run_id, "state": "stopped"}}

    def events(self, account, user, run_id, after=0):
        self.calls.append(("run.events", account, user, run_id, after))
        return {"events": [{"seq": after + 1, "type": "delta", "text": "合成回答片段"}],
                "run": {"id": run_id, "state": "running"}}


class FakeImports:
    """Synthetic import surface mirroring the bridge's public API."""

    def __init__(self, friends, batches):
        self._friends = friends
        self._batches = batches
        self.calls = []

    def friends(self):
        self.calls.append(("friends",))
        return self._friends

    def batches(self, friend_id):
        self.calls.append(("batches", friend_id))
        if friend_id not in {friend["id"] for friend in self._friends}:
            raise McpError(404, "not-found", "外部联系人不存在")
        return self._batches

    def batch(self, batch_id, subject_id):
        self.calls.append(("batch", batch_id, subject_id))
        return self._batch(batch_id, subject_id)

    def candidates(self, batch_id, subject_id, limit, offset):
        self.calls.append(("candidates", batch_id, subject_id, limit, offset))
        batch = self._batch(batch_id, subject_id)
        items = [{"candidateId": f"c-{index:03d}", "text": "合成转录文本", "sender": "对方",
                  "issues": [], "reviewed": False} for index in range(batch["total"])]
        return {"batchId": batch_id, "state": batch["state"], "total": batch["total"],
                "items": items[offset:offset + limit]}

    def messages(self, friend_id, *, platform=None, batch_id=None, query="", limit=50, offset=0):
        self.calls.append(("messages", friend_id, platform, batch_id, query, limit, offset))
        items = [{"messageId": f"m-{index:03d}", "text": "已保存的合成记录"}
                 for index in range(7)]
        return {"friendId": friend_id, "total": len(items), "items": items[offset:offset + limit],
                "gapCount": 1, "sources": [], "revision": 4, "warnings": [],
                "coverage": {"ordering": "evidence-sequence"}}

    def start(self, batch_id, subject_id, reprocess=False):
        self.calls.append(("start", batch_id, subject_id, reprocess))
        return self._batch(batch_id, subject_id)

    def stop(self, batch_id, subject_id):
        self.calls.append(("stop", batch_id, subject_id))
        return self._batch(batch_id, subject_id)

    def stop_client_batch(self, batch_id):
        self.calls.append(("stop-client", batch_id))
        return None

    def save(self, batch_id, subject_id, candidate_ids=None):
        self.calls.append(("save", batch_id, subject_id, candidate_ids))
        return self._batch(batch_id, subject_id)

    def remove_material(self, batch_id, subject_id, material_id):
        self.calls.append(("remove-material", batch_id, subject_id, material_id))
        return self._batch(batch_id, subject_id)

    def _batch(self, batch_id, subject_id):
        if batch_id != "batch-1" or subject_id != "friend-1":
            raise McpError(404, "not-found", "导入批次不存在或不属于该外部联系人")
        return {"id": batch_id, "friendId": subject_id, "state": "review", "total": 9}


class ToolHarness(unittest.TestCase):
    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.assistant = FakeAssistant()
        self.mcp.assistant = self.assistant
        self.imports = FakeImports(
            [{"id": "friend-1", "name": "外部好友甲", "wechatBound": False},
             {"id": "friend-2", "name": "外部好友乙", "wechatBound": True}],
            [{"id": "batch-1", "friendId": "friend-1", "state": "review", "total": 9}])
        self.mcp.imports = self.imports

    def principal(self, name="合成客户端", mode="full"):
        created, token = pair(self.mcp, name=name, mode=mode)
        return self.mcp.authenticate(headers(token["token"])), created

    def re_pair(self, created):
        token = self.mcp.handshake({"clientId": created["clientId"],
                                    "credential": created["credential"]})
        return self.mcp.authenticate(headers(token["token"]))

    def call(self, principal, body):
        return self.mcp.call(principal, body)

    def write(self, principal, action, params, request_id=None, subjects=SUBJECT_A, account=None):
        body = {"action": action, "params": params, "requestId": request_id or ("req-" + action)}
        if subjects is not None:
            body["subjects"] = subjects
        if account is not None:
            body["account"] = account
        return self.call(principal, body)


class AssistantToolTests(ToolHarness):
    def test_catalog_reads_and_template_writes(self):
        principal, _created = self.principal()
        catalog = self.call(principal, {"action": "zhiji_get_assistant_catalog", "params": {}})
        self.assertEqual(catalog["agents"][0]["id"], "agent-1")
        saved = self.write(principal, "zhiji_save_assistant_template",
                           {"agent": {"name": "新模板", "description": "", "prompt": "P",
                                      "skillIds": []}})
        self.assertEqual(saved["requestScope"], "installation")
        self.assertEqual(self.assistant.calls[-1][0], "catalog.saveAgent")
        enabled = self.write(principal, "zhiji_enable_assistant_template",
                             {"id": "agent-1", "enabled": False})
        self.assertEqual(self.assistant.calls[-1], ("catalog.enableAgent", "agent-1", False))
        self.assertEqual(enabled["agents"][0]["id"], "agent-1")
        self.write(principal, "zhiji_import_assistant_skill",
                   {"name": "技能", "description": "说明", "content": "正文"})
        self.assertEqual(self.assistant.calls[-1][0], "catalog.importSkill")

    def test_template_contract_rejects_extra_fields(self):
        principal, _created = self.principal()
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_save_assistant_template",
                       {"agent": {"name": "X", "builtin": True}})
        self.assertEqual(caught.exception.code, "invalid-request")
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_delete_assistant_skill", {"id": "s1", "extra": 1})
        self.assertEqual(caught.exception.code, "invalid-request")
        self.assertEqual(self.assistant.calls, [])  # 契约拒绝先于任何插件分发

    def test_discussion_reads_carry_the_authorized_subject(self):
        principal, _created = self.principal()
        view = self.call(principal, {"action": "zhiji_get_assistant_thread", "subjects": SUBJECT_A,
                                     "params": {"agentId": "agent-1", "threadId": "t1"}})
        self.assertEqual(view["thread"]["draft"]["text"], "用户自己的草稿")
        self.assertEqual(self.assistant.calls[-1], ("thread.get", "账号甲", "好友1", "agent-1",
                                                    "t1", None))
        listed = self.call(principal, {"action": "zhiji_list_assistant_threads",
                                       "subjects": SUBJECT_A, "params": {"agentId": "agent-1"}})
        self.assertEqual(listed["threads"][0]["id"], "t1")
        # 读取绝不隐式新建讨论：缺 threadId 是参数错误。
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_assistant_thread", "subjects": SUBJECT_A,
                                  "params": {"agentId": "agent-1"}})
        self.assertEqual(caught.exception.code, "invalid-request")
        self.assertNotIn("thread.new", [call[0] for call in self.assistant.calls])

    def test_submit_question_binds_the_run_to_this_client(self):
        principal, _created = self.principal()
        other, _created = self.principal("另一个客户端")
        started = self.write(principal, "zhiji_submit_assistant_question",
                             {"agentId": "agent-1", "threadId": "t1", "message": "帮我看看"},
                             request_id="q-1")
        self.assertEqual(started["run"]["state"], "running")
        self.assertEqual(self.assistant.calls[-1],
                         ("run.start", "账号甲", "好友1", "agent-1", "t1", "帮我看看", True, []))
        # 提问是写操作：同一请求标识幂等返回同一运行。
        again = self.write(principal, "zhiji_submit_assistant_question",
                           {"agentId": "agent-1", "threadId": "t1", "message": "帮我看看"},
                           request_id="q-1")
        self.assertEqual(again["run"]["id"], started["run"]["id"])
        self.assertEqual(len([call for call in self.assistant.calls if call[0] == "run.start"]), 1)
        events = self.call(principal, {"action": "zhiji_get_assistant_run", "subjects": SUBJECT_A,
                                       "params": {"runId": started["run"]["id"], "after": 2}})
        self.assertEqual(events["events"][0]["seq"], 3)
        stopped = self.write(principal, "zhiji_stop_assistant_run",
                             {"runId": started["run"]["id"]}, request_id="s-1")
        self.assertEqual(stopped["run"]["state"], "stopped")
        self.assertEqual(self.assistant.calls[-1], ("run.stop", "账号甲", "好友1", started["run"]["id"]))

    def test_stop_requires_the_authorized_subject(self):
        principal, _created = self.principal()
        self.write(principal, "zhiji_submit_assistant_question",
                   {"agentId": "agent-1", "threadId": "t1", "message": "问"}, request_id="q-1")
        other, _created = self.principal("逐联系人客户端", mode="per_contact")
        with self.assertRaises(McpError) as caught:
            self.write(other, "zhiji_stop_assistant_run", {"runId": "run-001"})
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("contact-not-authorized", 403))
        self.assertNotIn("run.stop", [call[0] for call in self.assistant.calls])

    def test_participants_are_authorized_one_by_one_in_per_contact_mode(self):
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        principal = self.re_pair(created)
        params = {"agentId": "agent-1", "threadId": "t1", "message": "@一下",
                  "mentions": ["好友1", "好友2"]}
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_submit_assistant_question", params)
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("participant-not-authorized", 403))
        self.assertIn("好友2", caught.exception.message)
        self.assertNotIn("好友1", caught.exception.message)
        self.assertEqual(self.assistant.calls, [])  # 任一参与者未授权即整体拒绝
        # 全部参与者已授权后放行。
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友2")
        principal = self.re_pair(created)
        started = self.write(principal, "zhiji_submit_assistant_question", params)
        self.assertEqual(started["participants"], [{"user": "好友1", "name": "好友1"},
                                                   {"user": "好友2", "name": "好友2"}])
        self.assertEqual(self.assistant.calls[-1][-1], ["好友1", "好友2"])

    def test_full_mode_does_not_check_participants(self):
        principal, _created = self.principal()
        started = self.write(principal, "zhiji_submit_assistant_question",
                             {"agentId": "agent-1", "threadId": "t1", "message": "问一下",
                              "mentions": ["任何人"]})
        self.assertEqual(started["run"]["state"], "running")

    def test_friends_list_is_filtered_to_grants_in_per_contact_mode(self):
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        principal = self.re_pair(created)
        friends = self.call(principal, {"action": "zhiji_list_assistant_friends",
                                        "subjects": SUBJECT_A, "params": {}})
        self.assertEqual([friend["user"] for friend in friends["friends"]], ["好友1"])
        self.assertEqual(friends["requestScope"], "authorized_contacts")

    def test_unauthorized_subject_never_reaches_the_assistant(self):
        principal, _created = self.principal("逐联系人客户端", mode="per_contact")
        for body in ({"action": "zhiji_get_assistant_thread", "subjects": SUBJECT_A,
                      "params": {"agentId": "agent-1", "threadId": "t1"}},
                     {"action": "zhiji_submit_assistant_question",
                      "params": {"agentId": "agent-1", "threadId": "t1", "message": "问"},
                      "requestId": "q-9", "subjects": SUBJECT_A}):
            with self.assertRaises(McpError) as caught:
                self.call(principal, body)
            self.assertEqual((caught.exception.code, caught.exception.status),
                             ("contact-not-authorized", 403), body)
        self.assertEqual(self.assistant.calls, [])

    def test_responses_and_records_carry_no_model_keys(self):
        principal, _created = self.principal()
        self.call(principal, {"action": "zhiji_get_assistant_catalog", "params": {}})
        self.write(principal, "zhiji_submit_assistant_question",
                   {"agentId": "agent-1", "threadId": "t1", "message": "机密提问内容"})
        for view in (json.dumps(self.assistant.catalog_view), stored_dump(self.mcp.store)):
            self.assertNotIn("sk-", view)
            self.assertNotIn("机密提问内容", view)  # 调用记录只存动作与主体，不存正文


class ImportToolTests(ToolHarness):
    def test_contact_listing_is_recomputed_from_grants(self):
        principal, _created = self.principal()
        listing = self.call(principal, {"action": "zhiji_list_import_contacts", "params": {}})
        self.assertEqual([contact["id"] for contact in listing["contacts"]],
                         ["friend-1", "friend-2"])
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, EXTERNAL_ACCOUNT,
                                     "external_contact", "friend-2")
        principal = self.re_pair(created)
        listing = self.call(principal, {"action": "zhiji_list_import_contacts", "params": {}})
        self.assertEqual([contact["id"] for contact in listing["contacts"]], ["friend-2"])
        self.assertEqual(listing["requestScope"], "authorized_contacts")

    def test_external_grant_is_independent_of_wechat_subjects_and_names(self):
        # 微信联系人授权不带来任何外部联系人能力，反之亦然；显示名不是主体。
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        # 用外部好友的显示名冒充微信主体也换不到授权。
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "外部好友甲")
        principal = self.re_pair(created)
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_list_import_batches",
                                  "subjects": external_subject("friend-1"), "params": {}})
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("contact-not-authorized", 403))
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_assistant_thread", "subjects": SUBJECT_B,
                                  "params": {"agentId": "agent-1", "threadId": "t1"}})
        self.assertEqual(caught.exception.code, "contact-not-authorized")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, EXTERNAL_ACCOUNT,
                                     "external_contact", "friend-1")
        principal = self.re_pair(created)
        batches = self.call(principal, {"action": "zhiji_list_import_batches",
                                        "subjects": external_subject("friend-1"), "params": {}})
        self.assertEqual(batches["batches"][0]["id"], "batch-1")

    def test_import_tools_only_accept_the_external_subject_shape(self):
        principal, _created = self.principal()
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_import_batch", "subjects": SUBJECT_A,
                                  "params": {"batchId": "batch-1"}})
        self.assertEqual(caught.exception.code, "invalid-request")
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_import_batch",
                                  "subjects": [{"accountId": "id-A", "subjectType": "external_contact",
                                                "subjectId": "friend-1"}],
                                  "params": {"batchId": "batch-1"}})
        self.assertEqual(caught.exception.code, "invalid-request")
        self.assertEqual(self.imports.calls, [])

    def test_batch_reads_are_bounded_to_the_authorized_friend(self):
        principal, _created = self.principal()
        detail = self.call(principal, {"action": "zhiji_get_import_batch",
                                       "subjects": external_subject("friend-1"),
                                       "params": {"batchId": "batch-1"}})
        self.assertEqual(detail["id"], "batch-1")
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_import_batch",
                                  "subjects": external_subject("friend-2"),
                                  "params": {"batchId": "batch-1"}})
        # 别人的批次不存在于本主体：主体核对在提供方按批次归属执行。
        self.assertEqual(caught.exception.code, "not-found")
        self.assertEqual(self.imports.calls[-1], ("batch", "batch-1", "friend-2"))

    def test_candidate_paging_with_subject_bound_cursors(self):
        principal, _created = self.principal()
        page = self.call(principal, {"action": "zhiji_list_import_candidates",
                                     "subjects": external_subject("friend-1"),
                                     "params": {"batchId": "batch-1", "limit": 4}})
        self.assertEqual(len(page["items"]), 4)
        self.assertIsNotNone(page["cursor"])
        second = self.call(principal, {"action": "zhiji_list_import_candidates",
                                       "subjects": external_subject("friend-1"),
                                       "params": {"batchId": "batch-1", "limit": 4,
                                                  "cursor": page["cursor"]}})
        self.assertEqual([item["candidateId"] for item in second["items"]],
                         ["c-004", "c-005", "c-006", "c-007"])
        # 绑定到另一主体或另一客户端的游标一律拒绝。
        other, _created = self.principal("另一个客户端")
        for body in ({"action": "zhiji_list_import_candidates",
                      "subjects": external_subject("friend-2"),
                      "params": {"batchId": "batch-1", "limit": 4, "cursor": page["cursor"]}},
                     {"action": "zhiji_list_import_candidates",
                      "subjects": external_subject("friend-1"),
                      "params": {"batchId": "batch-1", "limit": 4, "cursor": page["cursor"]}}):
            with self.assertRaises(McpError) as caught:
                self.call(other, body)
            self.assertEqual(caught.exception.code, "cursor-invalid", body)

    def test_message_read_is_paged_and_keeps_coverage(self):
        principal, _created = self.principal()
        page = self.call(principal, {"action": "zhiji_get_import_messages",
                                     "subjects": external_subject("friend-1"),
                                     "params": {"limit": 5, "query": "合成"}})
        self.assertEqual(page["total"], 7)
        self.assertEqual(len(page["items"]), 5)
        self.assertEqual(page["gapCount"], 1)
        self.assertEqual(page["coverage"]["ordering"], "evidence-sequence")
        self.assertIsNotNone(page["cursor"])

    def test_controlled_operations_are_idempotent_and_subject_checked(self):
        principal, _created = self.principal()
        started = self.write(principal, "zhiji_start_import_recognition",
                             {"batchId": "batch-1"}, subjects=external_subject("friend-1"))
        self.assertEqual(started["state"], "review")
        again = self.write(principal, "zhiji_start_import_recognition",
                           {"batchId": "batch-1"}, subjects=external_subject("friend-1"))
        self.assertEqual(again["id"], started["id"])
        self.assertEqual([call for call in self.imports.calls if call[0] == "start"],
                         [("start", "batch-1", "friend-1", False)])
        saved = self.write(principal, "zhiji_save_import_batch",
                           {"batchId": "batch-1", "candidateIds": ["c-1"]},
                           subjects=external_subject("friend-1"), request_id="save-1")
        self.assertEqual(self.imports.calls[-1], ("save", "batch-1", "friend-1", ["c-1"]))
        self.assertEqual(saved["id"], "batch-1")
        self.write(principal, "zhiji_remove_import_material",
                   {"batchId": "batch-1", "materialId": "mat-1"},
                   subjects=external_subject("friend-1"), request_id="rm-1")
        self.assertEqual(self.imports.calls[-1], ("remove-material", "batch-1", "friend-1", "mat-1"))
        stopped = self.write(principal, "zhiji_stop_import_recognition",
                             {"batchId": "batch-1"}, subjects=external_subject("friend-1"))
        self.assertEqual(self.imports.calls[-1], ("stop", "batch-1", "friend-1"))
        self.assertEqual(stopped["id"], "batch-1")


class RevocationTests(ToolHarness):
    def test_revocation_stops_mcp_started_question_runs(self):
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        principal = self.re_pair(created)
        started = self.write(principal, "zhiji_submit_assistant_question",
                             {"agentId": "agent-1", "threadId": "t1", "message": "问"},
                             request_id="q-1")
        self.mcp.store.revoke_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        self.assertEqual(self.assistant.calls[-1],
                         ("run.stop", "账号甲", "好友1", started["run"]["id"]))

    def test_revocation_stops_import_batches_without_a_subject(self):
        principal, _created = self.principal()
        self.write(principal, "zhiji_start_import_recognition", {"batchId": "batch-1"},
                   subjects=external_subject("friend-1"))
        self.mcp.store.set_mode(_created["clientId"], "per_contact")
        self.assertEqual(self.imports.calls[-1], ("stop-client", "batch-1"))

    def test_unrelated_bumps_leave_everything_running(self):
        principal, _created = self.principal()
        self.write(principal, "zhiji_submit_assistant_question",
                   {"agentId": "agent-1", "threadId": "t1", "message": "问"}, request_id="q-2")
        other, created = self.principal("无关客户端")
        self.mcp.store.set_mode(created["clientId"], "per_contact")
        self.assertNotIn("run.stop", [call[0] for call in self.assistant.calls])
        self.assertNotIn("stop-client", [call[0] for call in self.imports.calls])


class FakePluginServices:
    def __init__(self, supervisor):
        self.supervisor = supervisor
        self.required = []

    def require_plugin(self, plugin, account=None):
        self.required.append((plugin, account))
        if account == "封禁账号":
            raise PluginUnavailable("account-cleared", "账号资料正在清理或已清除，请重新打开应用")


class FakeSupervisor:
    def __init__(self):
        self.rpc_calls = []
        self.outcomes = {}

    def rpc(self, plugin, method, params=None, context=None, timeout=None):
        outcome = self.outcomes.get((plugin, method))
        if isinstance(outcome, Exception):
            raise outcome
        self.rpc_calls.append((plugin, method, params, context))
        return {"plugin": plugin, "method": method}


class AssistantBridgeTests(unittest.TestCase):
    def setUp(self):
        self.supervisor = FakeSupervisor()
        self.services = FakePluginServices(self.supervisor)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        (Path(temp.name) / "accounts.json").write_text(json.dumps(
            {"accounts": [{"accountId": "id-A", "account": "账号甲", "nickname": "甲", "wechatId": ""}]},
            ensure_ascii=False), encoding="utf-8")
        self.bridge = McpAssistantBridge(self.services, Path(temp.name) / "accounts.json")

    def test_rpc_carries_host_supplied_scope_and_named_methods(self):
        self.bridge.thread("账号甲", "好友1", "agent-1", "t1", before_seq=5)
        plugin, method, params, context = self.supervisor.rpc_calls[-1]
        self.assertEqual((plugin, method), ("kyf.advisor", "thread.get"))
        self.assertEqual(context, {"account": "账号甲", "user": "好友1"})
        self.assertEqual(params, {"agentId": "agent-1", "threadId": "t1", "beforeSeq": 5})
        self.assertEqual(self.services.required, [("kyf.advisor", "账号甲")])

    def test_catalog_management_is_unscoped(self):
        self.bridge.catalog()
        self.assertEqual(self.services.required, [("kyf.advisor", None)])
        self.assertEqual(self.supervisor.rpc_calls[-1][3], {})

    def test_plugin_errors_keep_their_stable_code(self):
        self.supervisor.outcomes[("kyf.advisor", "run.stop")] = PluginError("run-unknown", "运行不存在")
        with self.assertRaises(McpError) as caught:
            self.bridge.stop("账号甲", "好友1", "run-1")
        self.assertEqual((caught.exception.code, caught.exception.status), ("run-unknown", 409))
        self.supervisor.outcomes[("kyf.advisor", "run.stop")] = \
            PluginUnavailable("plugin-timeout", "插件响应超时")
        with self.assertRaises(McpError) as caught:
            self.bridge.stop("账号甲", "好友1", "run-1")
        self.assertEqual((caught.exception.code, caught.exception.status), ("plugin-timeout", 503))

    def test_foreign_account_ids_never_resolve(self):
        with self.assertRaises(McpError) as caught:
            self.bridge.resolve_account("id-其他")
        self.assertEqual((caught.exception.code, caught.exception.status), ("account-unknown", 404))


class ImportBridgeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = ExternalChatStore(Path(temp.name) / "external-chat")
        self.addCleanup(self.store.close)
        self.service = SimpleNamespace(store=self.store)
        self.services = SimpleNamespace(external_import=self.service)
        self.bridge = McpImportBridge(self.services)
        self.friend = self.store.create_friend("外部好友甲")
        self.material = Path(temp.name) / "材料.png"
        self.material.write_bytes(b"\x89PNG synthetic")

    def test_batch_and_candidates_read_from_the_real_store(self):
        batch = self.store.create_batch(self.friend["id"], "wechat", "own", "conv")
        self.store.add_material(batch["id"], self.material, "image")
        detail = self.bridge.batch(batch["id"], self.friend["id"])
        self.assertEqual(detail["materialCount"], 1)
        self.assertEqual(detail["materials"][0]["kind"], "image")
        self.assertNotIn("segments", detail)  # 原始分段不进入 MCP 契约
        page = self.bridge.candidates(batch["id"], self.friend["id"], 50, 0)
        self.assertEqual((page["state"], page["total"], page["items"]), ("draft", 0, []))
        summary = self.bridge.batches(self.friend["id"])[0]
        self.assertEqual(summary["id"], batch["id"])

    def test_messages_read_returns_saved_results_with_coverage(self):
        result = self.bridge.messages(self.friend["id"], query="什么都没有")
        self.assertEqual((result["total"], result["items"], result["gapCount"]), (0, [], 0))

    def test_subject_mismatch_hides_other_friends_batches(self):
        other = self.store.create_friend("外部好友乙")
        batch = self.store.create_batch(other["id"], "wechat", "own", "conv")
        with self.assertRaises(McpError) as caught:
            self.bridge.batch(batch["id"], self.friend["id"])
        self.assertEqual((caught.exception.code, caught.exception.status), ("not-found", 404))

    def test_store_errors_map_to_stable_codes(self):
        with self.assertRaises(McpError) as caught:
            self.bridge.batches("friend-不存在")
        self.assertEqual((caught.exception.code, caught.exception.status), ("not-found", 404))
        batch = self.store.create_batch(self.friend["id"], "wechat", "own", "conv")
        self.store.remove_material(batch["id"], "mat-不存在")  # 幂等：不存在即无效果
        with self.assertRaises(McpError) as caught:
            self.bridge.save(batch["id"], self.friend["id"], ["ghost"])
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("invalid-request", 400))


if __name__ == "__main__":
    unittest.main()
