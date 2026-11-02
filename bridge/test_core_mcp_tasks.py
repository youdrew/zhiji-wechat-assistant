"""Phase-3b analysis-task tools on synthetic data: per-contact authorization, task
ownership, revocation killing queued and running tasks, and late results never
writing back. The long-task tests use a fake task executor; nothing calls a model."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_mcp_proxy import McpError, McpServices
from core_mcp_tasks import McpAnalysisBridge
from core_plugin_process import PluginError, PluginUnavailable
from core_store import CoreStore
from core_tasks import TaskManager
from test_core_mcp import stored_dump
from test_core_mcp_runtime import FINGERPRINT, make_services, pair, headers

SUBJECT_A = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友1"}]
SUBJECT_B = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "好友2"}]


class FakeAnalysis:
    """Synthetic analysis service; records every call the proxy makes."""

    def __init__(self):
        self.calls = []
        self.tasks = {}
        self.sequence = 0

    def resolve_account(self, account_id):
        if account_id != "id-A":
            raise McpError(404, "account-unknown", "该账号不是本机已保存账号")
        return "账号甲"

    def start_friend_analysis(self, account, user, scheme=None, conflict=None):
        self.calls.append(("start", account, user, scheme, conflict))
        self.sequence += 1
        task_id = f"task-{self.sequence:03d}"
        self.tasks[task_id] = {"id": task_id, "plugin": "kyf.friend-analysis", "kind": "scheme",
                               "account": account, "user": user, "scheme": scheme or "default",
                               "title": "分析", "params": {}, "state": "running", "progress": None,
                               "error": None, "afterTask": None, "createdAt": 1, "startedAt": 2,
                               "finishedAt": None}
        return {"task": self.tasks[task_id]}

    def task(self, task_id):
        return self.tasks.get(task_id)

    def stop_task(self, task_id):
        self.calls.append(("stop", task_id))
        task = self.tasks.get(task_id)
        if task is not None:
            task["state"] = "stopping"
        return task

    def friend_analysis_page(self, account, user, method, params=None):
        self.calls.append(("page", account, user, method))
        return {"account": account, "user": user, "method": method, "payload": [{"id": 1}]}

    def start_message_insights(self, account, user, message_id):
        self.calls.append(("insight.start", account, user, message_id))
        return {"items": [{"messageId": message_id, "state": "pending", "analysisState": "pending",
                           "suggestionsState": "pending", "applicability": None,
                           "sourceState": "checking", "sourceMessage": ""}]}

    def message_insights(self, account, user, message_ids):
        self.calls.append(("insight.status", account, user, list(message_ids)))
        return {"enabled": True, "pendingCount": 1, "error": None,
                "items": [{"messageId": message_id, "state": "ready", "analysisState": "ready",
                           "suggestionsState": "ready", "applicability": "current",
                           "sourceState": "verified", "sourceMessage": "",
                           "analysis": {"summary": "合成逐句解读"},
                           "replies": [{"text": "合成回复建议一"}]}
                          for message_id in message_ids]}

    def start_self_analysis(self, account, params):
        self.calls.append(("self.start", account, params))
        return {"state": "running", "stopping": False, "steps": [{"key": "profile", "detail": "长" * 50}]}

    def self_analysis_status(self, account):
        self.calls.append(("self.status", account))
        return {"state": "done", "steps": [{"key": "profile", "state": "done"}]}

    def stop_self_analysis(self, account):
        self.calls.append(("self.stop", account))
        return {"state": "stopping", "stopping": True}

    def self_analysis_page(self, account, method, params=None):
        self.calls.append(("self.page", account, method, params))
        return {"account": account, "method": method, "dimensions": [{"id": "persona"}]}

    def edit_self_timeline(self, account, params):
        self.calls.append(("self.timelineEdit", account, params))
        if params["expectedVersion"] != 7:
            raise McpError(409, "version-conflict", "时间线已修改")
        return {"version": 8, "editedId": "manual-1", "editLabel": "添加“入职”", "events": [{"id": "x"}]}

    def undo_self_timeline(self, account, params):
        self.calls.append(("self.timelineUndo", account, params))
        return {"version": 9, "undone": "添加“入职”", "events": [{"id": "x"}]}


class TaskToolHarness(unittest.TestCase):
    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.provider = FakeAnalysis()
        self.mcp.analysis = self.provider

    def principal(self, name="合成客户端", mode="full"):
        created, token = pair(self.mcp, name=name, mode=mode)
        return self.mcp.authenticate(headers(token["token"])), created

    def re_pair(self, created):
        token = self.mcp.handshake({"clientId": created["clientId"],
                                    "credential": created["credential"]})
        return self.mcp.authenticate(headers(token["token"]))

    def call(self, principal, body):
        return self.mcp.call(principal, body)

    def write(self, principal, action, params, request_id="t1", subjects=SUBJECT_A, account=None):
        body = {"action": action, "params": params, "requestId": request_id}
        if subjects is not None:
            body["subjects"] = subjects
        if account is not None:
            body["account"] = account
        return self.call(principal, body)


class FriendAnalysisToolTests(TaskToolHarness):
    def test_start_returns_a_task_id_and_read_by_section(self):
        principal, _created = self.principal()
        result = self.write(principal, "zhiji_start_friend_analysis", {})
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["requestScope"], "installation")
        # 缺省方案由桥接层补默认值，提供方收到 None 由 TaskManager 校验方案归属。
        self.assertEqual(self.provider.calls, [("start", "账号甲", "好友1", None, None)])
        status = self.call(principal, {"action": "zhiji_get_task",
                                       "params": {"taskId": result["taskId"]}})
        self.assertEqual(status["task"]["taskId"], result["taskId"])
        self.assertEqual(status["task"]["state"], "running")
        overview = self.call(principal, {"action": "zhiji_get_friend_analysis", "subjects": SUBJECT_A,
                                         "params": {"section": "overview"}})
        self.assertEqual(overview["section"], "overview")
        self.assertEqual(self.provider.calls[-1], ("page", "账号甲", "好友1", "page.overview"))

    def test_identical_start_retry_returns_the_same_task_id(self):
        principal, _created = self.principal()
        first = self.write(principal, "zhiji_start_friend_analysis", {}, request_id="same-1")
        second = self.write(principal, "zhiji_start_friend_analysis", {}, request_id="same-1")
        self.assertEqual(first["taskId"], second["taskId"])
        self.assertEqual(len(self.provider.calls), 1)  # 幂等重试没有再次进入执行器

    def test_same_request_id_with_different_scheme_is_rejected(self):
        principal, _created = self.principal()
        self.write(principal, "zhiji_start_friend_analysis", {"scheme": "default"},
                   request_id="dup-1")
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_start_friend_analysis", {"scheme": "other"},
                       request_id="dup-1")
        self.assertEqual(caught.exception.code, "request-id-conflict")

    def test_groups_are_refused_before_any_dispatch(self):
        principal, _created = self.principal()
        group = [{"accountId": "id-A", "subjectType": "wechat_contact", "subjectId": "房间@chatroom"}]
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_start_friend_analysis", {}, subjects=group)
        self.assertEqual(caught.exception.code, "invalid-request")
        self.assertEqual(self.provider.calls, [])

    def test_unknown_account_id_never_reaches_core(self):
        principal, _created = self.principal()
        foreign = [{"accountId": "id-X", "subjectType": "wechat_contact", "subjectId": "好友1"}]
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_start_friend_analysis", {}, subjects=foreign)
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("account-unknown", 404))

    def test_task_id_is_bound_to_the_starting_client(self):
        principal, _created = self.principal()
        started = self.write(principal, "zhiji_start_friend_analysis", {})
        other, _created = self.principal("另一个客户端")
        for body in ({"action": "zhiji_get_task", "params": {"taskId": started["taskId"]}},
                     {"action": "zhiji_stop_task", "params": {"taskId": started["taskId"]},
                      "requestId": "stop-1"}):
            with self.assertRaises(McpError) as caught:
                self.call(other, body)
            self.assertEqual(caught.exception.code, "not-found", body)
        stopped = self.call(principal, {"action": "zhiji_stop_task",
                                        "params": {"taskId": started["taskId"]},
                                        "requestId": "stop-2"})
        self.assertEqual(stopped["task"]["state"], "stopping")
        self.assertEqual(self.provider.calls[-1], ("stop", started["taskId"]))

    def test_task_tools_need_a_task_id_and_known_sections(self):
        principal, _created = self.principal()
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_task", "params": {}})
        self.assertEqual(caught.exception.code, "invalid-request")
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_friend_analysis", "subjects": SUBJECT_A,
                                  "params": {"section": "全部给我"}})
        self.assertEqual(caught.exception.code, "invalid-request")

    def test_missing_analysis_service_reports_not_open(self):
        self.mcp.analysis = None
        principal, _created = self.principal()
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_friend_analysis", "subjects": SUBJECT_A,
                                  "params": {"section": "status"}})
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("capability-not-open", 501))


class TaskAuthorizationTests(TaskToolHarness):
    def test_per_contact_mode_cannot_start_or_read_unauthorized_subjects(self):
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友2")
        principal = self.re_pair(created)
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_start_friend_analysis", {}, subjects=SUBJECT_A)
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("contact-not-authorized", 403))
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_friend_analysis", "subjects": SUBJECT_A,
                                  "params": {"section": "persona"}})
        self.assertEqual(caught.exception.code, "contact-not-authorized")
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_start_message_insights", {"messageId": "m1"})
        self.assertEqual(caught.exception.code, "contact-not-authorized")
        # 未授权主体从未到达执行器或插件。
        self.assertEqual(self.provider.calls, [])

    def test_per_contact_mode_authorized_subject_flows_end_to_end(self):
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        principal = self.re_pair(created)
        started = self.write(principal, "zhiji_start_friend_analysis", {})
        self.assertEqual(started["requestScope"], "authorized_contacts")
        self.assertEqual(self.provider.calls, [("start", "账号甲", "好友1", None, None)])
        insights = self.call(principal, {"action": "zhiji_get_message_insights",
                                         "subjects": SUBJECT_A, "params": {"messageIds": ["m1"]}})
        self.assertEqual(insights["requestScope"], "authorized_contacts")
        self.assertEqual(insights["items"][0]["analysis"], {"summary": "合成逐句解读"})

    def test_self_analysis_is_blocked_in_per_contact_mode(self):
        principal, _created = self.principal("逐联系人客户端", mode="per_contact")
        for body in ({"action": "zhiji_start_self_analysis", "params": {}, "account": "id-A",
                      "requestId": "self-1"},
                     {"action": "zhiji_get_self_analysis", "params": {"section": "persona"},
                      "account": "id-A"},
                     {"action": "zhiji_stop_self_analysis", "params": {}, "account": "id-A",
                      "requestId": "self-2"},
                     {"action": "zhiji_get_self_analysis", "params": {"section": "timeline"},
                      "account": "id-A"},
                     {"action": "zhiji_edit_self_timeline", "account": "id-A", "requestId": "self-3",
                      "params": {"target": "event", "action": "confirm", "id": "e1", "expectedVersion": 7}},
                     {"action": "zhiji_undo_self_timeline", "account": "id-A", "requestId": "self-4",
                      "params": {"expectedVersion": 7}}):
            with self.assertRaises(McpError) as caught:
                self.call(principal, body)
            self.assertEqual((caught.exception.code, caught.exception.status),
                             ("aggregate-blocked", 403), body)
        self.assertEqual(self.provider.calls, [])

    def test_self_analysis_works_in_full_mode(self):
        principal, _created = self.principal()
        started = self.write(principal, "zhiji_start_self_analysis", {"depth": "deep"},
                             subjects=None, account="id-A")
        self.assertEqual((started["state"], started["requestScope"]), ("running", "installation"))
        self.assertNotIn("steps", started)  # 开始结果保持紧凑，完整视图走读取工具
        view = self.call(principal, {"action": "zhiji_get_self_analysis",
                                     "params": {"section": "persona"}, "account": "id-A"})
        self.assertEqual(view["section"], "persona")
        self.assertEqual(self.provider.calls[-1], ("self.page", "账号甲", "self.persona", {}))
        stopped = self.write(principal, "zhiji_stop_self_analysis", {}, subjects=None,
                             account="id-A", request_id="self-stop")
        self.assertEqual(stopped["state"], "stopping")
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_start_self_analysis", {"depth": "离谱"},
                       subjects=None, account="id-A", request_id="self-bad")
        self.assertEqual(caught.exception.code, "invalid-request")

    def test_every_self_analysis_section_reads_its_own_method(self):
        principal, _created = self.principal()
        for section, params, method, forwarded in (
                ("tags", {}, "self.tags", {}),
                ("tagEvidence", {"name": "篮球"}, "self.tagEvidence", {"name": "篮球"}),
                ("periods", {}, "self.periods", {}),
                ("periodDetail", {"period": "2024H1", "key": "O"}, "self.periodDetail",
                 {"period": "2024H1", "key": "O"}),
                ("timelineEvidence", {"id": "e1"}, "self.timelineEvidence", {"id": "e1"})):
            view = self.call(principal, {"action": "zhiji_get_self_analysis", "account": "id-A",
                                         "params": {"section": section, **params}})
            self.assertEqual(view["section"], section)
            self.assertEqual(self.provider.calls[-1], ("self.page", "账号甲", method, forwarded))

    def test_timeline_read_is_paged_inside_the_mcp_result_limit(self):
        principal, _created = self.principal()
        self.call(principal, {"action": "zhiji_get_self_analysis", "account": "id-A",
                              "params": {"section": "timeline", "offset": 40}})
        call = self.provider.calls[-1]
        self.assertEqual(call[:3], ("self.page", "账号甲", "self.timeline"))
        self.assertEqual(call[3]["offset"], 40)
        self.assertLess(call[3]["pageBytes"], McpServices.ANALYSIS_RESULT_MAX_CHARS)

    def test_section_rejects_parameters_it_does_not_take(self):
        principal, _created = self.principal()
        for params in ({"section": "persona", "offset": 1}, {"section": "tags", "id": "e1"},
                       {"section": "timeline", "pageBytes": 9_999_999}, {"section": "nonsense"}):
            with self.assertRaises(McpError) as caught:
                self.call(principal, {"action": "zhiji_get_self_analysis", "account": "id-A",
                                      "params": params})
            self.assertEqual(caught.exception.code, "invalid-request", params)
        self.assertEqual(self.provider.calls, [])

    def test_start_accepts_timeline_and_periods_outputs(self):
        principal, _created = self.principal()
        self.write(principal, "zhiji_start_self_analysis", {"outputs": ["timeline", "periods"]},
                   subjects=None, account="id-A")
        self.assertEqual(self.provider.calls[-1],
                         ("self.start", "账号甲", {"outputs": ["timeline", "periods"]}))

    def test_timeline_edit_and_undo_answer_compactly(self):
        principal, _created = self.principal()
        edited = self.write(principal, "zhiji_edit_self_timeline",
                            {"target": "event", "action": "add", "fields": {"title": "入职", "when": "2024.05"},
                             "expectedVersion": 7}, subjects=None, account="id-A")
        self.assertEqual({key: edited[key] for key in ("version", "id", "label")},
                         {"version": 8, "id": "manual-1", "label": "添加“入职”"})
        self.assertNotIn("events", edited)
        undone = self.write(principal, "zhiji_undo_self_timeline", {"expectedVersion": 8},
                            subjects=None, account="id-A", request_id="undo-1")
        self.assertEqual((undone["version"], undone["undone"]), (9, "添加“入职”"))
        self.assertNotIn("events", undone)

    def test_timeline_edit_is_idempotent_and_needs_its_version(self):
        principal, _created = self.principal()
        params = {"target": "event", "action": "confirm", "id": "e1", "expectedVersion": 7}
        first = self.write(principal, "zhiji_edit_self_timeline", params, subjects=None, account="id-A")
        second = self.write(principal, "zhiji_edit_self_timeline", params, subjects=None, account="id-A")
        self.assertEqual(first["version"], second["version"])
        self.assertEqual(len(self.provider.calls), 1)
        for bad in ({"target": "event", "action": "confirm", "id": "e1"},
                    {**params, "expectedVersion": -1}, {**params, "pageBytes": 1}):
            with self.assertRaises(McpError) as caught:
                self.write(principal, "zhiji_edit_self_timeline", bad, subjects=None, account="id-A",
                           request_id="bad-" + str(len(bad)) + str(bad.get("expectedVersion")))
            self.assertEqual(caught.exception.code, "invalid-request", bad)
        with self.assertRaises(McpError) as caught:
            self.write(principal, "zhiji_edit_self_timeline", {**params, "expectedVersion": 6},
                       subjects=None, account="id-A", request_id="stale")
        self.assertEqual(caught.exception.code, "version-conflict")


class TimelineBridgeTests(unittest.TestCase):
    def test_a_stale_plugin_edit_becomes_the_stable_conflict_code(self):
        bridge = McpAnalysisBridge(None, "unused")
        def stale(plugin, method, params, account=None):
            self.assertEqual((plugin, method, account), ("kyf.self-analysis", "self.timelineUndo", "账号甲"))
            raise McpError(409, "conflict", "时间线已修改")
        bridge._rpc = stale
        with self.assertRaises(McpError) as caught:
            bridge.undo_self_timeline("账号甲", {"expectedVersion": 1})
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "version-conflict"))


class RevocationTests(TaskToolHarness):
    def started_task(self, principal):
        return self.write(principal, "zhiji_start_friend_analysis", {}, request_id="rev-0")

    def test_contact_revocation_stops_the_running_task(self):
        principal, created = self.principal("逐联系人客户端", mode="per_contact")
        self.mcp.store.grant_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        principal = self.re_pair(created)
        started = self.started_task(principal)
        task_id = started["taskId"]
        self.mcp.store.revoke_contact(created["clientId"], FINGERPRINT, "id-A", "wechat_contact", "好友1")
        # 版本侦听同步停止了任务，绑定也被清除；旧令牌同时因授权版本失效被拒。
        self.assertEqual(self.provider.calls[-1], ("stop", task_id))
        with self.assertRaises(McpError) as caught:
            self.call(principal, {"action": "zhiji_get_task", "params": {"taskId": task_id}})
        self.assertEqual(caught.exception.code, "stale-authorization")

    def test_mode_switch_and_profile_revocation_invalidate_tasks_too(self):
        principal, created = self.principal("切换客户端")
        started = self.started_task(principal)
        self.mcp.store.set_mode(created["clientId"], "per_contact")
        self.assertEqual(self.provider.calls[-1], ("stop", started["taskId"]))
        principal2, created2 = self.principal("撤销客户端")
        started2 = self.write(principal2, "zhiji_start_friend_analysis", {}, request_id="rev-1")
        self.mcp.store.revoke_profile(created2["clientId"])
        self.assertEqual(self.provider.calls[-1], ("stop", started2["taskId"]))
        # 未发起任务的客户端不受影响。
        self.assertEqual([call for call in self.provider.calls if call[0] == "stop"],
                         [("stop", started["taskId"]), ("stop", started2["taskId"])])

    def test_unrelated_version_bumps_leave_the_task_alone(self):
        principal, _created = self.principal()
        started = self.started_task(principal)
        other, _created = self.principal("无关客户端")
        self.mcp.store.set_mode(other["clientId"], "per_contact")
        status = self.call(principal, {"action": "zhiji_get_task",
                                       "params": {"taskId": started["taskId"]}})
        self.assertEqual(status["task"]["state"], "running")
        self.assertNotIn(("stop", started["taskId"]), self.provider.calls)

    def test_call_records_carry_no_analysis_payloads(self):
        principal, _created = self.principal()
        self.write(principal, "zhiji_start_friend_analysis", {})
        self.call(principal, {"action": "zhiji_get_friend_analysis", "subjects": SUBJECT_A,
                              "params": {"section": "overview"}})
        dump = stored_dump(self.mcp.store)
        self.assertNotIn("合成逐句解读", dump)
        self.assertNotIn("payload", dump)


class FakePluginServices:
    """The services surface the bridge uses, with a recording require_plugin."""

    def __init__(self, tasks, supervisor):
        self.tasks = tasks
        self.supervisor = supervisor
        self.required = []

    def require_plugin(self, plugin, account=None):
        self.required.append((plugin, account))
        if account == "封禁账号":
            raise PluginUnavailable("account-cleared", "账号资料正在清理或已清除，请重新打开应用")


class FakeSupervisor:
    def __init__(self):
        self.messages = []
        self.process = SimpleNamespace(token="process-1",
                                       send=lambda message: self.messages.append(message))
        self.rpc_results = {}

    def on(self, *_args): pass
    def on_exit(self, *_args): pass
    def add_ticker(self, *_args): pass
    def ensure(self, _plugin): return self.process
    def running(self, _plugin): return self.process
    def send(self, plugin, message, **_kwargs): self.messages.append((plugin, message))
    def rpc(self, plugin, method, params=None, context=None, timeout=None):
        outcome = self.rpc_results.get((plugin, method))
        if isinstance(outcome, Exception):
            raise outcome
        return {"plugin": plugin, "method": method, "params": params, "context": context}


class BridgeTests(unittest.TestCase):
    """The bridge against a real TaskManager and a recording plugin supervisor."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = CoreStore(Path(self.temp.name) / "core.sqlite3")
        self.addCleanup(self.store.close)
        self.supervisor = FakeSupervisor()
        registry = SimpleNamespace(enabled=lambda _plugin: True, get=lambda _plugin: {
            "version": "1.0.0", "contributes": {"schemes": [{"id": "default"}]}})
        self.tasks = TaskManager(self.store, self.supervisor, registry)
        self.services = FakePluginServices(self.tasks, self.supervisor)
        (Path(self.temp.name) / "accounts.json").write_text(json.dumps(
            {"accounts": [{"accountId": "id-A", "account": "账号甲", "nickname": "甲", "wechatId": ""}]},
            ensure_ascii=False), encoding="utf-8")
        self.bridge = McpAnalysisBridge(self.services, Path(self.temp.name) / "accounts.json")

    def test_resolve_account_uses_the_explicit_registry(self):
        self.assertEqual(self.bridge.resolve_account("id-A"), "账号甲")
        with self.assertRaises(McpError) as caught:
            self.bridge.resolve_account("id-其他")
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("account-unknown", 404))

    def test_start_friend_analysis_creates_a_shared_scheme_task(self):
        result = self.bridge.start_friend_analysis("账号甲", "好友1")
        self.assertEqual(result["task"]["state"], "running")
        self.assertEqual(result["task"]["plugin"], "kyf.friend-analysis")
        self.assertEqual(result["task"]["user"], "好友1")
        self.assertEqual(self.services.required, [("kyf.friend-analysis", "账号甲")])
        self.assertTrue(any(isinstance(message, dict) and message.get("type") == "task.start"
                            for message in self.supervisor.messages))

    def test_scheme_conflict_maps_to_a_stable_mcp_error(self):
        self.bridge.start_friend_analysis("账号甲", "好友1")
        with self.assertRaises(McpError) as caught:
            self.bridge.start_friend_analysis("账号甲", "好友1")
        self.assertEqual((caught.exception.code, caught.exception.status), ("scheme-busy", 409))
        queued = self.bridge.start_friend_analysis("账号甲", "好友1", conflict="queue")
        self.assertEqual(queued["task"]["state"], "queued")

    def test_account_cleanup_blocks_new_tasks_via_the_shared_latch(self):
        self.services.require_plugin = lambda plugin, account=None: (
            (_ for _ in ()).throw(PluginUnavailable("account-cleared", "账号资料正在清理或已清除"))
            if account == "封禁账号" else None)
        with self.assertRaises(McpError) as caught:
            self.bridge.start_friend_analysis("封禁账号", "好友1")
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("account-cleared", 503))
        # 共享闩锁同样在 TaskManager 内生效。
        self.tasks.blocked_accounts.add("账号甲")
        with self.assertRaises(McpError) as caught:
            self.bridge.start_friend_analysis("账号甲", "好友1")
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("account-cleared", 409))

    def test_plugin_errors_keep_their_stable_code(self):
        self.supervisor.rpc_results[("kyf.self-analysis", "self.analyze")] = \
            PluginError("analysis-busy", "统一分析正在进行")
        with self.assertRaises(McpError) as caught:
            self.bridge.start_self_analysis("账号甲", {})
        self.assertEqual((caught.exception.code, caught.exception.status), ("analysis-busy", 409))
        self.supervisor.rpc_results[("kyf.friend-analysis", "page.overview")] = \
            PluginError("not-found", "没有已保存结果")
        with self.assertRaises(McpError) as caught:
            self.bridge.friend_analysis_page("账号甲", "好友1", "page.overview")
        self.assertEqual((caught.exception.code, caught.exception.status), ("not-found", 404))
        self.supervisor.rpc_results[("kyf.message-insights", "annotations.status")] = \
            PluginUnavailable("plugin-timeout", "插件响应超时")
        with self.assertRaises(McpError) as caught:
            self.bridge.message_insights("账号甲", "好友1", ["m1"])
        self.assertEqual((caught.exception.code, caught.exception.status),
                         ("plugin-timeout", 503))

    def test_stop_revokes_the_execution_so_late_results_cannot_write_back(self):
        # MCP 发起的任务与 GUI 任务共用代次/吊销：停止后插件迟到的 finish 一律拒绝。
        created = self.tasks.create("kyf.friend-analysis", kind="scheme", account="账号甲",
                                    user="好友1", scheme="default", title="分析", from_plugin=True)
        token = created["execution"]["token"]
        task_id = created["task"]["id"]
        self.bridge.stop_task(task_id)
        self.assertEqual(self.tasks.task(task_id)["state"], "stopping")
        with self.assertRaises(Exception) as caught:
            self.tasks.finish(token, "kyf.friend-analysis", task_id, "done")
        self.assertEqual(getattr(caught.exception, "code", None), "execution-revoked")
        self.assertEqual(self.tasks.task(task_id)["state"], "stopping")


if __name__ == "__main__":
    unittest.main()
