"""Phase-3b MCP writes on synthetic data: versioned human-data tools, request
idempotency, authorization re-checks at commit time and the stable error codes."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_mcp_proxy import McpError, McpServices
from core_mcp_offline import SavedAccountScope
from test_core_mcp import stored_dump
from test_core_mcp_runtime import FINGERPRINT, make_friends, make_services, pair, headers

SUBJECTS = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友1"}]


class WriteHarness(unittest.TestCase):
    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.data, self.friends = make_friends()
        self.addCleanup(self.data.close)
        self.scope = SavedAccountScope(self.friends,
                                       Path(self.data.root.name) / ".local" / "real-client-data"
                                       / "accounts.json")
        self.mcp.offline = lambda: self.scope

    def principal(self, name="写入客户端", mode="full"):
        created, token = pair(self.mcp, name=name, mode=mode)
        return self.mcp.authenticate(headers(token["token"])), created

    def re_pair(self, created):
        token = self.mcp.handshake({"clientId": created["clientId"],
                                    "credential": created["credential"]})
        return self.mcp.authenticate(headers(token["token"]))

    def call(self, principal, body):
        return self.mcp.call(principal, body)

    def write(self, principal, action, params, request_id="w1", subjects=SUBJECTS, account=None):
        body = {"action": action, "params": params, "requestId": request_id}
        if subjects is not None:
            body["subjects"] = subjects
        if account is not None:
            body["account"] = account
        return self.call(principal, body)

    def listed_ids(self, principal, status="active", subject=SUBJECTS):
        result = self.call(principal, {"action": "zhiji_list_memories", "subjects": subject,
                                       "params": {"status": status, "limit": 100}})
        return [item["id"] for item in result["items"]], result["memoryVersion"]


class MemoryWriteTests(WriteHarness):
    def test_create_is_confirmed_and_consistent_on_read_back(self):
        principal, _created = self.principal()
        result = self.write(principal, "zhiji_create_memory",
                            {"text": "用户新增的合成记忆", "tags": ["爱好"], "eventTime": "2026-01"})
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(result["version"], 1)
        self.assertEqual(result["requestScope"], "installation")
        ids, memory_version = self.listed_ids(principal, "confirmed")
        self.assertIn(result["memoryId"], ids)
        self.assertEqual(memory_version, result["memoryVersion"])
        listed = self.call(principal, {"action": "zhiji_list_memories", "subjects": SUBJECTS,
                                       "params": {"status": "confirmed", "limit": 100}})
        text = next(item["text"] for item in listed["items"] if item["id"] == result["memoryId"])
        self.assertEqual(text, "用户新增的合成记忆")

    def test_identical_retry_returns_the_original_result_without_reapplying(self):
        principal, _created = self.principal()
        first = self.write(principal, "zhiji_create_memory",
                           {"text": "幂等合成记忆", "tags": ["经历"]}, request_id="same-1")
        before = self.listed_ids(principal)
        second = self.write(principal, "zhiji_create_memory",
                            {"text": "幂等合成记忆", "tags": ["经历"]}, request_id="same-1")
        self.assertEqual(first, second)
        self.assertEqual(self.listed_ids(principal), before)  # 重试未产生第二条记忆

    def test_same_id_with_different_parameters_is_rejected(self):
        principal, _created = self.principal()
        self.write(principal, "zhiji_create_memory", {"text": "第一条", "tags": ["经历"]},
                   request_id="dup-1")
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_create_memory", {"text": "另一条", "tags": ["经历"]},
                       request_id="dup-1")
        self.assertEqual(caught.exception.code, "request-id-conflict")
        # 失败的写请求同样保住参数守卫：同 ID 换参数依旧拒绝。
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_update_memory",
                       {"memoryId": "mem-无", "text": "x", "tags": ["经历"], "eventTime": None,
                        "expectedVersion": 1}, request_id="dup-1")
        self.assertEqual(caught.exception.code, "request-id-conflict")

    def test_writes_require_a_request_id(self):
        principal, _created = self.principal()
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_create_memory",
                                  "params": {"text": "没有请求标识", "tags": ["经历"]},
                                  "subjects": SUBJECTS})
        self.assertEqual(caught.exception.code, "invalid-request")

    def test_extra_fields_are_rejected_before_anything_is_written(self):
        principal, _created = self.principal()
        before = self.listed_ids(principal)
        for index, params in enumerate(({"text": "x", "tags": ["经历"], "expectedVersion": 1},
                                        {"text": "x", "tags": ["经历"], "eventTime": "2026-01",
                                         "origin": "ai"})):
            with self.assertRaises(McpError) as caught:
                self.write(principal, "zhiji_update_memory",
                           {"memoryId": self.data.confirmed_id, **params},
                           request_id=f"extra-{index}")
            self.assertEqual(caught.exception.code, "invalid-request", params)
        self.assertEqual(self.listed_ids(principal), before)

    def test_update_version_conflict_then_success_and_read_back(self):
        principal, _created = self.principal()
        created = self.write(principal, "zhiji_create_memory",
                             {"text": "待改写的合成记忆", "tags": ["经历"], "eventTime": "2026-02"})
        memory_id = created["memoryId"]
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_update_memory",
                       {"memoryId": memory_id, "text": "改写后的合成记忆", "tags": ["经历"],
                        "eventTime": "2026-03", "expectedVersion": created["version"] + 1},
                       request_id="upd-1")
        self.assertEqual((caught.exception.code, caught.exception.status), ("version-conflict", 409))
        # 版本修正后是新的请求：换新的请求标识重试。
        updated = self.write(principal, "zhiji_update_memory",
                             {"memoryId": memory_id, "text": "改写后的合成记忆", "tags": ["边界"],
                              "eventTime": "2026-03", "expectedVersion": created["version"]},
                             request_id="upd-2")
        self.assertEqual(updated["version"], created["version"] + 1)
        listed = self.call(principal, {"action": "zhiji_list_memories", "subjects": SUBJECTS,
                                       "params": {"status": "confirmed", "limit": 100}})
        item = next(item for item in listed["items"] if item["id"] == memory_id)
        self.assertEqual((item["text"], item["tags"], item["eventTime"]),
                         ("改写后的合成记忆", ["边界"], "2026-03"))

    def test_confirm_respects_the_pending_only_state_machine(self):
        principal, _created = self.principal()
        confirmed = self.write(principal, "zhiji_confirm_memory",
                               {"memoryId": "mem-p1", "expectedVersion": 1}, request_id="cfm-1")
        self.assertEqual(confirmed["status"], "confirmed")
        # 再次确认同一候选：状态机不允许，返回稳定的版本冲突。
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_confirm_memory",
                       {"memoryId": "mem-p1", "expectedVersion": confirmed["version"]},
                       request_id="cfm-2")
        self.assertEqual(caught.exception.code, "version-conflict")

    def test_delete_removes_the_memory_and_rejects_stale_versions(self):
        principal, _created = self.principal()
        created = self.write(principal, "zhiji_create_memory",
                             {"text": "将被删除的合成记忆", "tags": ["经历"]}, request_id="del-0")
        memory_id = created["memoryId"]
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_delete_memory",
                       {"memoryId": memory_id, "expectedVersion": created["version"] + 3},
                       request_id="del-1")
        self.assertEqual(caught.exception.code, "version-conflict")
        deleted = self.write(principal, "zhiji_delete_memory",
                             {"memoryId": memory_id, "expectedVersion": created["version"]},
                             request_id="del-2")
        self.assertTrue(deleted["deleted"])
        self.assertNotIn(memory_id, self.listed_ids(principal)[0])
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_delete_memory",
                       {"memoryId": memory_id, "expectedVersion": created["version"]},
                       request_id="del-3")
        self.assertEqual(caught.exception.code, "not-found")

    def test_records_carry_no_text_anywhere_in_the_authorization_store(self):
        principal, created = self.principal()
        self.write(principal, "zhiji_create_memory",
                   {"text": "绝密合成正文不应入库标记", "tags": ["经历"]}, request_id="log-1")
        self.write(principal, "zhiji_create_memory",
                   {"text": "绝密合成正文不应入库标记", "tags": ["经历"]}, request_id="log-1")
        dump = stored_dump(self.mcp.store)
        self.assertNotIn("绝密合成正文", dump)
        self.assertNotIn(created["credential"], dump)

    def test_provider_write_rechecks_authorization_at_commit_time(self):
        # 模拟撤权落在入口判定与提交之间：入口通过、提交前复核必须拦下。
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        principal = self.re_pair(created)
        original = self.mcp.store.has_grant
        seen = {"n": 0}

        def flaky(*args, **kwargs):
            seen["n"] += 1
            return seen["n"] < 2  # 入口判定通过，提交前复核失败

        self.mcp.store.has_grant = flaky
        self.addCleanup(setattr, self.mcp.store, "has_grant", original)
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_create_memory", {"text": "撤权后的写入", "tags": ["经历"]},
                       request_id="revoke-1")
        self.assertEqual(caught.exception.code, "contact-not-authorized")
        rows = self.data.database._rows("SELECT text FROM memories WHERE text=?", ("撤权后的写入",))
        self.assertEqual(rows, [])  # 提交前被拦下，什么都没有写入


class WriteAuthorizationTests(WriteHarness):
    def test_per_contact_mode_cannot_write_unauthorized_subjects(self):
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友2")
        principal = self.re_pair(created)
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_create_memory",
                       {"text": "越权合成记忆", "tags": ["经历"]}, request_id="nope-1")
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("contact-not-authorized", 403))
        # 未授权联系人从未被写入。
        rows = self.data.database._rows("SELECT text FROM memories WHERE text=?", ("越权合成记忆",))
        self.assertEqual(rows, [])

    def test_revocation_kills_in_flight_writes_and_old_tokens(self):
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        principal = self.re_pair(created)
        self.write(principal, "zhiji_create_memory",
                   {"text": "撤权前的合成记忆", "tags": ["经历"]}, request_id="rev-0")
        self.mcp.store.revoke_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        # 旧令牌的授权版本已过期：在途写请求立即失效。
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_create_memory",
                       {"text": "撤权后的合成记忆", "tags": ["经历"]}, request_id="rev-1")
        self.assertEqual(caught.exception.code, "stale-authorization")
        # 重新配对后也因联系人未授权而拒绝。
        principal = self.re_pair(created)
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_create_memory",
                       {"text": "撤权后的合成记忆", "tags": ["经历"]}, request_id="rev-1")
        self.assertEqual(caught.exception.code, "contact-not-authorized")

    def test_account_level_self_profile_is_blocked_in_per_contact_mode(self):
        principal, _created = self.principal("逐联系人客户端", mode="per_contact")
        for body in ({"action": "zhiji_get_self_profile", "params": {}, "account": "id-A"},
                     {"action": "zhiji_save_self_profile", "params": {"background": "x", "style": "y",
                                                                      "expectedVersion": 0},
                      "account": "id-A", "requestId": "self-1"}):
            with self.assertRaises(McpError) as caught:
                self.call(principal, body)
            self.assertEqual(caught.exception.code, "aggregate-blocked", body)


class GoalAndObservationTests(WriteHarness):
    def test_contact_goal_read_save_and_conflict(self):
        principal, _created = self.principal()
        first = self.call(principal, {"action": "zhiji_get_contact_goal", "subjects": SUBJECTS,
                                      "params": {}})
        self.assertEqual((first["goal"]["text"], first["goal"]["version"]), ("", 0))
        self.assertTrue(first["goal"]["presets"])
        saved = self.write(principal, "zhiji_save_contact_goal",
                           {"text": "了解近况", "expectedVersion": 0}, request_id="goal-1")
        self.assertEqual(saved["goal"], {"text": "了解近况", "version": 1})
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_save_contact_goal",
                       {"text": "覆盖", "expectedVersion": 0}, request_id="goal-2")
        self.assertEqual(caught.exception.code, "version-conflict")
        again = self.write(principal, "zhiji_save_contact_goal",
                           {"text": "保持联系", "expectedVersion": 1}, request_id="goal-3")
        self.assertEqual(again["goal"]["version"], 2)
        current = self.call(principal, {"action": "zhiji_get_contact_goal", "subjects": SUBJECTS,
                                        "params": {}})
        self.assertEqual(current["goal"]["text"], "保持联系")

    def test_self_profile_read_save_and_read_back(self):
        principal, _created = self.principal()
        first = self.call(principal, {"action": "zhiji_get_self_profile", "params": {},
                                      "account": "id-A"})
        self.assertEqual((first["background"], first["style"], first["version"]), ("", "", 0))
        saved = self.write(principal, "zhiji_save_self_profile",
                           {"background": "合成背景", "style": "合成风格", "expectedVersion": 0},
                           request_id="self-1", subjects=None, account="id-A")
        self.assertEqual(saved["version"], 1)
        current = self.call(principal, {"action": "zhiji_get_self_profile", "params": {},
                                        "account": "id-A"})
        self.assertEqual((current["background"], current["style"], current["version"]),
                         ("合成背景", "合成风格", 1))
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_save_self_profile",
                       {"background": "x", "style": "y", "expectedVersion": 0},
                       request_id="self-2", subjects=None, account="id-A")
        self.assertEqual(caught.exception.code, "version-conflict")

    def test_observations_save_update_delete_with_versions(self):
        principal, _created = self.principal()
        created = self.write(principal, "zhiji_save_observation",
                             {"judgement": "合成观察判断", "note": "合成依据", "dimension": "性格"},
                             request_id="obs-1")
        self.assertIsNotNone(created["observationId"])
        self.assertEqual(created["version"], 1)
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_save_observation",
                       {"observationId": created["observationId"], "judgement": "更新判断",
                        "note": "", "dimension": None, "expectedVersion": 99},
                       request_id="obs-2")
        self.assertEqual(caught.exception.code, "version-conflict")
        updated = self.write(principal, "zhiji_save_observation",
                             {"observationId": created["observationId"], "judgement": "更新判断",
                              "note": "", "dimension": None, "expectedVersion": created["version"]},
                             request_id="obs-3")
        self.assertEqual(updated["version"], created["version"] + 1)
        listed = self.call(principal, {"action": "zhiji_list_observations", "subjects": SUBJECTS,
                                       "params": {"limit": 100}})
        self.assertEqual([item["judgement"] for item in listed["items"]], ["更新判断"])
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_delete_observation",
                       {"observationId": created["observationId"], "expectedVersion": 1},
                       request_id="obs-4")
        self.assertEqual(caught.exception.code, "version-conflict")
        deleted = self.write(principal, "zhiji_delete_observation",
                             {"observationId": created["observationId"],
                              "expectedVersion": updated["version"]}, request_id="obs-5")
        self.assertTrue(deleted["deleted"])
        listed = self.call(principal, {"action": "zhiji_list_observations", "subjects": SUBJECTS,
                                       "params": {"limit": 100}})
        self.assertEqual(listed["items"], [])


if __name__ == "__main__":
    unittest.main()
