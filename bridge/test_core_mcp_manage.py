"""Phase-5 MCP application management and high-impact deletion confirmation, on synthetic data.

Zero tokens, no real WeChat, no real credentials: model keys are fake strings, the
account registry is a synthetic file and the deleter/presenter are recording fakes.
"""

import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_mcp_store
from core_mcp_admin import McpAdminError, handle_admin
from core_mcp_confirm import CONFIRM_TTL_SECONDS, ConfirmUnavailable, McpDeletionConfirmService
from core_mcp_manage import McpManagementBridge
from core_mcp_proxy import REGISTRY, McpError, McpServices
from usage_ledger import AGGREGATE_KEYS

FINGERPRINT = "synthetic-install-fingerprint"
SECRET_KEY = "sk-synthetic-not-a-real-key-123456"
ACCOUNTS = [{"accountId": "acc-a", "account": "账号A", "nickname": "甲", "wechatId": "wxid_a"},
            {"accountId": "acc-b", "account": "账号B", "nickname": "乙", "wechatId": "wxid_b"}]
SCHEDULING = {"searchSource": "none", "searxngUrl": "http://127.0.0.1:8080",
              "searchOpenPages": True}
CONNECTION_API = {"protocol": "deepseek", "baseUrl": "https://api.example.com", "model": "synthetic-model",
                  "contextTokens": 8192, "timeoutMs": 120000, "hasKey": True}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def headers(token):
    return {"Authorization": "Bearer " + token}


def make_services(clock=None):
    directory = tempfile.TemporaryDirectory()
    clock = clock or Clock()
    services = McpServices(directory.name, source_fingerprint=lambda: FINGERPRINT, monotonic=clock)
    services._directory = directory  # keep the temp directory alive
    services._clock = clock
    return services


def handshake(mcp, created):
    """A fresh session for an existing profile (授权版本提升后必须重新配对)."""
    token = mcp.handshake({"clientId": created["clientId"], "credential": created["credential"]})
    return mcp.authenticate(headers(token["token"]))


def pair(mcp, name="合成客户端", mode="full", **kwargs):
    created = mcp.store.create_profile(name, mode, **kwargs)
    return handshake(mcp, created), created


class FakeWindow:
    """Injectable stand-in for the Electron confirm window.

    ``decision=True/False`` resolves through the confirm service with the payload's
    one-time token, exactly like the settings channel would; ``decision=None``
    blocks forever to model a window that never answers.
    """

    def __init__(self, service, decision=True):
        self.service = service
        self.decision = decision
        self.payloads = []
        self.shown = threading.Event()
        self._hold = threading.Event()

    def __call__(self, payload):
        self.payloads.append(payload)
        self.shown.set()
        if self.decision is None:
            self._hold.wait(30)
            return True
        self.service.resolve(payload["confirmId"], payload["token"], self.decision)
        return self.decision

    def wait_shown(self, timeout=5):
        if not self.shown.wait(timeout):
            raise AssertionError("确认窗未在时限内展示")
        return self.payloads[0]


def wait_state(confirm, confirm_id, state, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if confirm.view(confirm_id)["state"] == state:
                return
        except McpError:
            return  # 已清理的记录同样视为结束。
        time.sleep(0.01)
    raise AssertionError(f"确认状态未到达 {state}")


def make_accounts_file(records=ACCOUNTS):
    directory = tempfile.TemporaryDirectory()
    path = Path(directory.name) / "accounts.json"
    path.write_text(json.dumps({"version": 1, "accounts": records}), encoding="utf-8")
    return path, directory


class FakeLedger:
    """Per-account aggregates only; rows mirror usage_ledger.by_friend's shape."""

    def __init__(self):
        self.summary_calls = []

    def summary(self, filters):
        self.summary_calls.append(dict(filters))
        row = {key: 0 for key in AGGREGATE_KEYS}
        row.update({"requests": 4, "totalTokens": 130, "lastAt": 30})
        return {**row, "droppedRecords": 0}

    def by_plugin(self, filters):
        row = {key: 0 for key in AGGREGATE_KEYS}
        row.update({"requests": 4, "totalTokens": 130})
        return [{"plugin": "kyf.friend-analysis", **row}]

    def by_friend(self, filters):
        base = {key: 0 for key in AGGREGATE_KEYS}
        return [{"scope": "user", "user": "好友1", **{**base, "requests": 2, "totalTokens": 60, "lastAt": 10}},
                {"scope": "user", "user": "好友2", **{**base, "requests": 1, "totalTokens": 40, "lastAt": 20}},
                {"scope": "self", "user": None, **{**base, "requests": 1, "totalTokens": 30, "lastAt": 30}}]


def make_manage(mcp, *, presenter=None, deleter=None, records=ACCOUNTS, clock=None):
    """The real bridge over a synthetic CoreServices surface."""
    path, directory = make_accounts_file(records)
    services = SimpleNamespace(mcp=mcp, ledger=FakeLedger(),
                               model_settings=lambda: {"revision": 1, "values": dict(SCHEDULING)},
                               save_model_settings=lambda revision, values:
                                   {"revision": revision + 1, "values": dict(values)},
                               listing=lambda: {"plugins": [
                                   {"id": "kyf.friend-analysis", "name": "好友分析", "version": "1.0",
                                    "description": "分析", "enabled": True, "process": {"state": "running"},
                                    "contributes": {"schemes": [{"id": "default", "huge": "x" * 4096}]}},
                                   {"id": "kyf.mcp", "name": "MCP", "version": "1.0", "description": "MCP",
                                    "enabled": True, "process": None}]},
                               registry=SimpleNamespace(get=lambda plugin_id: {"id": plugin_id}),
                               set_enabled=lambda plugin_id, enabled: {"id": plugin_id, "enabled": enabled})
    services.backend = SimpleNamespace(
        model_source=lambda: {"mode": "api", "status": "active", "api": dict(CONNECTION_API)},
        model_source_activate=None)
    confirm = McpDeletionConfirmService(presenter, monotonic=clock or time.monotonic)
    bridge = McpManagementBridge(services, path, deleter=deleter, presenter=presenter, confirm=confirm)
    mcp.manage = bridge
    mcp.confirm = bridge.confirm
    bridge._directory = directory  # keep the temp directory alive
    return bridge


def activate_recorder(result_api=None):
    """A model_source_activate stand-in that records the request, never returns the key."""
    requests = []

    def activate(request):
        requests.append(dict(request))
        api = dict(CONNECTION_API)
        if result_api:
            api.update(result_api)
        return {"mode": "api", "status": "active", "api": api}
    activate.requests = requests
    return activate


class ManagementAuthorizationTests(unittest.TestCase):
    """逐联系人模式的全局管理开关与完整模式的开放范围（§1/§4.2）。"""

    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.store = self.mcp.store
        self.mcp.manage = SimpleNamespace(
            model_settings=lambda: {"scheduling": {"revision": 1, "values": {}}},
            plugins=lambda: {"plugins": []})

    def test_per_contact_mode_defaults_to_closed_management(self):
        principal, created = pair(self.mcp, "逐联系人客户端", "per_contact")
        with self.assertRaises(McpError) as caught:
            self.mcp.call(principal, {"action": "zhiji_get_model_settings"})
        self.assertEqual(caught.exception.code, "management-not-enabled")
        self.assertFalse(self.store.client(created["clientId"])["global_management"])

    def test_global_management_switch_opens_management_only(self):
        _principal, created = pair(self.mcp, "逐联系人客户端", "per_contact")
        self.store.set_global_management(created["clientId"], True)
        principal = handshake(self.mcp, created)  # 开关提升版本后需重新配对
        result = self.mcp.call(principal, {"action": "zhiji_get_model_settings"})
        self.assertEqual(result["requestScope"], "authorized_contacts")
        # 开关只增加管理动作，绝不扩大联系人权限。
        with self.assertRaises(McpError) as caught:
            self.mcp.call(principal, {"action": "zhiji_search_messages",
                                      "subjects": [{"accountId": "acc-a", "subjectType": "wechat_contact",
                                                    "subjectId": "好友1"}]})
        self.assertEqual(caught.exception.code, "contact-not-authorized")

    def test_full_mode_opens_management_tools(self):
        principal, _created = pair(self.mcp, "完整客户端", "full")
        result = self.mcp.call(principal, {"action": "zhiji_get_plugins"})
        self.assertIn("plugins", result)

    def test_management_actions_are_registered_as_app_management(self):
        for name in ("zhiji_get_model_settings", "zhiji_save_model_settings", "zhiji_get_plugins",
                     "zhiji_set_plugin_enabled", "zhiji_get_usage", "zhiji_delete_account",
                     "zhiji_clear_local_data"):
            self.assertEqual(REGISTRY[name]["kind"], "app_management")


class ModelSettingsTests(unittest.TestCase):
    """密钥只写不读：设置接受明文，任何响应只给 hasKey。"""

    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.bridge = make_manage(self.mcp)
        self.principal, _created = pair(self.mcp, "完整客户端", "full")

    def test_query_masks_the_key(self):
        result = self.mcp.call(self.principal, {"action": "zhiji_get_model_settings"})
        self.assertEqual(result["scheduling"]["values"], SCHEDULING)
        api = result["connection"]["api"]
        self.assertTrue(api["hasKey"])
        self.assertNotIn("apiKey", json.dumps(result))
        self.assertNotIn(SECRET_KEY, json.dumps(result))

    def test_save_scheduling_and_connection_and_never_echo_the_key(self):
        activate = activate_recorder()
        self.bridge.services.backend.model_source_activate = activate
        result = self.mcp.call(self.principal, {
            "action": "zhiji_save_model_settings", "requestId": "req-settings-1",
            "params": {"scheduling": {"revision": 1, "values": dict(SCHEDULING, searchOpenPages=False)},
                       "connection": {"protocol": "deepseek", "baseUrl": "https://api.example.com",
                                      "model": "synthetic-model", "apiKey": SECRET_KEY,
                                      "contextTokens": 8192, "timeoutMs": 120000}}})
        self.assertEqual(result["scheduling"]["values"]["searchOpenPages"], False)
        self.assertTrue(activate.requests and activate.requests[0]["apiKey"] == SECRET_KEY)
        # 响应与调用记录都不含密钥明文。
        self.assertNotIn(SECRET_KEY, json.dumps(result))
        self.assertTrue(result["connection"]["api"]["hasKey"])
        calls = self.mcp.store.recent_calls(10)
        self.assertNotIn(SECRET_KEY, json.dumps(calls))
        self.assertIn("zhiji_save_model_settings", [item["action"] for item in calls])

    def test_save_requires_a_known_field_shape(self):
        with self.assertRaises(McpError) as caught:
            self.mcp.call(self.principal, {"action": "zhiji_save_model_settings",
                                           "requestId": "req-bad", "params": {"apiKey": SECRET_KEY}})
        self.assertEqual(caught.exception.code, "invalid-request")

    def test_bridge_maps_model_errors_to_stable_codes(self):
        def broken_activate(request):
            raise ValueError("invalid contextTokens")
        self.bridge.services.backend.model_source_activate = broken_activate
        with self.assertRaises(McpError) as caught:
            self.bridge.save_model_settings({"connection": {"protocol": "deepseek"}})
        self.assertEqual(caught.exception.code, "invalid-request")
        self.assertEqual(caught.exception.status, 400)

    def test_unavailable_model_source_maps_to_503(self):
        from backend_contracts import ModelSourceUnavailable
        def unavailable(request):
            raise ModelSourceUnavailable("model connection failed")
        self.bridge.services.backend.model_source_activate = unavailable
        with self.assertRaises(McpError) as caught:
            self.bridge.save_model_settings({"connection": {"protocol": "deepseek", "baseUrl": "https://x",
                                                            "model": "m", "apiKey": SECRET_KEY}})
        self.assertEqual(caught.exception.status, 503)


class PluginManagementTests(unittest.TestCase):
    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.bridge = make_manage(self.mcp)
        self.principal, _created = pair(self.mcp, "完整客户端", "full")

    def test_listing_is_a_bounded_view_without_manifest_payloads(self):
        result = self.mcp.call(self.principal, {"action": "zhiji_get_plugins"})
        dumped = json.dumps(result)
        self.assertIn("kyf.friend-analysis", dumped)
        self.assertNotIn("contributes", dumped)
        self.assertNotIn("huge", dumped)

    def test_enable_and_disable_go_through_the_shared_registry(self):
        calls = []

        def set_enabled(plugin_id, enabled):
            calls.append((plugin_id, enabled))
            return {"id": plugin_id, "enabled": enabled}
        self.bridge.services.set_enabled = set_enabled
        result = self.mcp.call(self.principal, {"action": "zhiji_set_plugin_enabled", "requestId": "req-p1",
                                                "params": {"pluginId": "kyf.friend-analysis", "enabled": False}})
        self.assertEqual(calls, [("kyf.friend-analysis", False)])
        self.assertFalse(result["plugin"]["enabled"])

    def test_mcp_itself_cannot_be_disabled_and_unknown_plugins_404(self):
        with self.assertRaises(McpError) as caught:
            self.mcp.call(self.principal, {"action": "zhiji_set_plugin_enabled", "requestId": "req-p2",
                                           "params": {"pluginId": "kyf.mcp", "enabled": False}})
        self.assertEqual(caught.exception.code, "mcp-self-forbidden")
        self.bridge.services.registry = SimpleNamespace(get=lambda plugin_id: None)
        with self.assertRaises(McpError) as caught:
            self.bridge.set_plugin_enabled("nope.plugin", True)
        self.assertEqual(caught.exception.code, "plugin-unknown")


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.store = self.mcp.store
        self.bridge = make_manage(self.mcp, clock=self.mcp._clock)
        self.principal, _created = pair(self.mcp, "完整客户端", "full")
        self.per_principal, per_created = pair(self.mcp, "逐联系人客户端", "per_contact")
        self.store.grant_contact(per_created["clientId"], FINGERPRINT, "acc-a", "wechat_contact", "好友1")
        # 用量是应用管理动作：逐联系人模式需开启全局管理，但只重算已授权联系人。
        self.store.set_global_management(per_created["clientId"], True)
        self.per_principal = handshake(self.mcp, per_created)

    def test_full_mode_returns_account_scoped_aggregates(self):
        result = self.mcp.call(self.principal, {"action": "zhiji_get_usage", "account": "acc-a"})
        self.assertEqual(result["scope"], "installation")
        self.assertEqual(result["usage"]["summary"]["requests"], 4)
        # 账号以注册表解析成真实账号名后到达共享账本。
        self.assertEqual(self.bridge.services.ledger.summary_calls[-1]["account"], "账号A")

    def test_per_contact_mode_recomputes_over_authorized_contacts_only(self):
        result = self.mcp.call(self.per_principal, {"action": "zhiji_get_usage", "account": "acc-a"})
        self.assertEqual(result["scope"], "authorized_contacts")
        self.assertEqual(result["usage"]["summary"]["requests"], 2)
        self.assertTrue(result["usage"]["excludedUnattributed"])
        self.assertEqual([row["user"] for row in result["usage"]["contacts"]], ["好友1"])
        dumped = json.dumps(result)
        self.assertNotIn("好友2", dumped)  # 未授权联系人的行绝不出现
        self.assertNotIn("账号A", dumped)  # 账号名也不出现在逐联系人结果里

    def test_per_contact_mode_without_grants_is_refused(self):
        _third_principal, third = pair(self.mcp, "客户端三", "per_contact")
        self.store.set_global_management(third["clientId"], True)
        third_principal = handshake(self.mcp, third)
        with self.assertRaises(McpError) as caught:
            self.mcp.call(third_principal, {"action": "zhiji_get_usage", "account": "acc-b"})
        self.assertEqual(caught.exception.code, "contact-not-authorized")


class DeletionConfirmationTests(unittest.TestCase):
    """§7：无确认不执行；确认一次性、短时、绑定参数/客户端/授权版本/数据根。"""

    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.store = self.mcp.store
        self.deleted = []
        self.bridge = make_manage(self.mcp, deleter=self.deleted.append, clock=self.mcp._clock)
        self.principal, created = pair(self.mcp, "完整客户端", "full")
        # 另一个持有该账号授权主体的客户端，用于验证执行后授权失效。
        _other_principal, other = pair(self.mcp, "逐联系人客户端", "per_contact")
        self.other_id = other["clientId"]
        self.store.grant_contact(self.other_id, FINGERPRINT, "acc-a", "wechat_contact", "好友1")
        self.events = []
        self.store.add_version_listener(self.events.append)

    def begin(self, decision=True, account="acc-a", action="zhiji_delete_account",
              request_id="req-del-1"):
        window = FakeWindow(self.bridge.confirm, decision)
        self.bridge.confirm.presenter = window
        result = self.mcp.call(self.principal, {"action": action, "account": account,
                                                "requestId": request_id, "params": {}})
        payload = window.wait_shown()
        return result, payload

    def execute(self, confirmation_id, request_id="req-del-2", account="acc-a",
                action="zhiji_delete_account"):
        return self.mcp.call(self.principal, {"action": action, "account": account,
                                              "requestId": request_id,
                                              "params": {"confirmationId": confirmation_id}})

    def test_first_call_returns_intent_and_never_executes(self):
        result, payload = self.begin()
        self.assertEqual(result["state"], "pending_confirmation")
        self.assertEqual(payload["kindLabel"], "删除整个账号")
        self.assertEqual(payload["clientName"], "完整客户端")
        self.assertEqual(payload["accountLabel"], "甲")
        self.assertIn("不可恢复", payload["impact"])
        self.assertEqual(self.deleted, [])

    def test_denied_confirmation_is_refused(self):
        result, _payload = self.begin(decision=False)
        wait_state(self.bridge.confirm, result["confirmationId"], "denied")
        with self.assertRaises(McpError) as caught:
            self.execute(result["confirmationId"])
        self.assertEqual(caught.exception.code, "confirmation-denied")
        self.assertEqual(self.deleted, [])

    def test_execute_after_confirmation_runs_the_cleanup_chain_and_revokes_state(self):
        cursor = self.mcp._cursor_encode(self.mcp._cursor_payload(self.principal, "acc-a", "好友1", 5))
        result, _payload = self.begin()
        wait_state(self.bridge.confirm, result["confirmationId"], "confirmed")
        done = self.execute(result["confirmationId"])
        self.assertTrue(done["deleted"])
        self.assertEqual(self.deleted, ["acc-a"])
        # 授权主体撤销 + 版本提升 + 侦听通知（任务绑定/缓存随版本失效）。
        self.assertEqual(self.store.grants(self.other_id), [])
        self.assertEqual(self.store.client(self.other_id)["auth_version"], 2)
        self.assertIn(self.other_id, self.events)
        # 未完成游标全部失效。
        with self.assertRaises(McpError) as caught:
            self.mcp._cursor_decode(cursor, principal=self.principal, account_id="acc-a", subject_id="好友1")
        self.assertEqual(caught.exception.code, "cursor-invalid")

    def test_confirmation_is_one_time_only(self):
        result, _payload = self.begin()
        wait_state(self.bridge.confirm, result["confirmationId"], "confirmed")
        self.execute(result["confirmationId"])
        with self.assertRaises(McpError) as caught:
            self.execute(result["confirmationId"], request_id="req-del-3")
        self.assertEqual(caught.exception.code, "confirmation-consumed")
        self.assertEqual(self.deleted, ["acc-a"])  # 只执行了一次

    def test_parameters_may_not_change_after_confirmation(self):
        result, _payload = self.begin(account="acc-a")
        wait_state(self.bridge.confirm, result["confirmationId"], "confirmed")
        with self.assertRaises(McpError) as caught:
            self.execute(result["confirmationId"], account="acc-b")
        self.assertEqual(caught.exception.code, "confirmation-mismatch")
        self.assertEqual(self.deleted, [])

    def test_authorization_version_change_invalidates_the_confirmation(self):
        result, _payload = self.begin()
        wait_state(self.bridge.confirm, result["confirmationId"], "confirmed")
        regenerated = self.store.regenerate_credential(self.principal["clientId"])
        # 旧会话先失效。
        with self.assertRaises(McpError) as caught:
            self.execute(result["confirmationId"])
        self.assertEqual(caught.exception.code, "stale-authorization")
        # 新会话的授权版本也不再匹配确认绑定。
        fresh = self.mcp.authenticate(headers(self.mcp.handshake(
            {"clientId": self.principal["clientId"], "credential": regenerated["credential"]})["token"]))
        with self.assertRaises(McpError) as caught:
            self.mcp.call(fresh, {"action": "zhiji_delete_account", "account": "acc-a",
                                  "requestId": "req-del-3",
                                  "params": {"confirmationId": result["confirmationId"]}})
        self.assertEqual(caught.exception.code, "confirmation-mismatch")
        self.assertEqual(self.deleted, [])

    def test_expired_confirmation_is_refused(self):
        clock = self.mcp._clock
        window = FakeWindow(self.bridge.confirm, None)  # 窗口一直不给决定
        self.bridge.confirm.presenter = window
        result = self.mcp.call(self.principal, {"action": "zhiji_delete_account", "account": "acc-a",
                                                "requestId": "req-del-1", "params": {}})
        window.wait_shown()
        clock.advance(CONFIRM_TTL_SECONDS + 1)
        with self.assertRaises(McpError) as caught:
            self.execute(result["confirmationId"])
        self.assertEqual(caught.exception.code, "confirmation-expired")
        self.assertEqual(self.deleted, [])

    def test_no_confirm_window_means_no_deletion(self):
        self.bridge.confirm.presenter = None
        with self.assertRaises(McpError) as caught:
            self.mcp.call(self.principal, {"action": "zhiji_delete_account", "account": "acc-a",
                                           "requestId": "req-del-1", "params": {}})
        self.assertEqual(caught.exception.code, "confirmation-unavailable")
        self.assertEqual(self.deleted, [])

    def test_unavailable_desktop_never_executes(self):
        def broken_window(payload):
            raise ConfirmUnavailable("无可交互桌面")
        self.bridge.confirm.presenter = broken_window
        result = self.mcp.call(self.principal, {"action": "zhiji_delete_account", "account": "acc-a",
                                                "requestId": "req-del-1", "params": {}})
        wait_state(self.bridge.confirm, result["confirmationId"], "unavailable")
        with self.assertRaises(McpError) as caught:
            self.execute(result["confirmationId"])
        self.assertEqual(caught.exception.code, "confirmation-unavailable")
        self.assertEqual(self.deleted, [])

    def test_clear_local_data_deletes_every_saved_account(self):
        window = FakeWindow(self.bridge.confirm, True)
        self.bridge.confirm.presenter = window
        result = self.mcp.call(self.principal, {"action": "zhiji_clear_local_data",
                                                "requestId": "req-clear-1", "params": {}})
        payload = window.wait_shown()
        self.assertEqual(payload["kindLabel"], "清空本机数据")
        wait_state(self.bridge.confirm, result["confirmationId"], "confirmed")
        done = self.mcp.call(self.principal, {"action": "zhiji_clear_local_data",
                                              "requestId": "req-clear-2",
                                              "params": {"confirmationId": result["confirmationId"]}})
        self.assertEqual(done["cleared"], ["acc-a", "acc-b"])
        self.assertEqual(self.deleted, ["acc-a", "acc-b"])
        self.assertEqual(self.store.grants(self.other_id), [])

    def test_unknown_account_is_refused_before_any_confirmation(self):
        with self.assertRaises(McpError) as caught:
            self.mcp.call(self.principal, {"action": "zhiji_delete_account", "account": "acc-z",
                                           "requestId": "req-del-1", "params": {}})
        self.assertEqual(caught.exception.code, "account-unknown")

    def test_deletion_records_only_ids_in_the_call_log(self):
        result, _payload = self.begin()
        calls = json.dumps(self.store.recent_calls(10))
        self.assertIn("zhiji_delete_account", calls)
        self.assertNotIn(result["confirmationId"], calls)
        self.assertNotIn(SECRET_KEY, calls)

    def test_mcp_tools_can_never_reach_the_confirmation_channel(self):
        with self.assertRaises(McpError) as caught:
            self.mcp.call(self.principal, {"action": "confirmations"})
        self.assertEqual(caught.exception.code, "action-not-registered")
        with self.assertRaises(McpError) as caught:
            self.mcp.call(self.principal, {"action": "data.destructive_delete"})
        self.assertEqual(caught.exception.code, "fixed-exclusion")


class ConfirmServiceTests(unittest.TestCase):
    """确认服务自身的语义：一次性令牌、绑定校验、拒绝与超时。"""

    def setUp(self):
        self.clock = Clock()
        self.confirm = McpDeletionConfirmService(monotonic=self.clock)
        self.bind = dict(action="zhiji_delete_account", kind="delete_account", client_id="mcp_x",
                         auth_version=1, fingerprint=FINGERPRINT, account_id="acc-a",
                         params_hash="hash-a")

    def begin(self, presenter=None):
        """默认 presenter 只展示不决定；token 由测试经 payload 取出后显式 resolve。"""
        box, hold = [], threading.Event()

        def default_presenter(payload):
            box.append(payload)
            hold.wait(30)  # 真实窗口阻塞到用户决定；决定由测试经服务/设置通道回传
            return True

        self.confirm.presenter = presenter or default_presenter
        view = self.confirm.begin(client_name="合成客户端", account_label="甲", **self.bind)
        if presenter is None:
            deadline = time.monotonic() + 5
            while not box and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(box)
        return view, (box[0]["token"] if box else None)

    def test_resolve_requires_the_windows_one_time_token(self):
        view, token = self.begin()
        with self.assertRaises(McpError) as caught:
            self.confirm.resolve(view["confirmId"], "wrong-token", True)
        self.assertEqual(caught.exception.code, "confirmation-token-invalid")
        self.assertEqual(self.confirm.view(view["confirmId"])["state"], "pending")
        with self.assertRaises(McpError) as caught:
            self.confirm.check_use(view["confirmId"], **self.bind)
        self.assertEqual(caught.exception.code, "confirmation-pending")

    def test_double_resolution_is_rejected(self):
        view, token = self.begin()
        self.confirm.resolve(view["confirmId"], token, True)
        with self.assertRaises(McpError) as caught:
            self.confirm.resolve(view["confirmId"], token, False)
        self.assertEqual(caught.exception.code, "confirmation-already-resolved")

    def test_check_use_consumes_and_binding_is_enforced(self):
        view, token = self.begin()
        self.confirm.resolve(view["confirmId"], token, True)
        self.confirm.check_use(view["confirmId"], **self.bind)
        with self.assertRaises(McpError) as caught:
            self.confirm.check_use(view["confirmId"], **self.bind)
        self.assertEqual(caught.exception.code, "confirmation-consumed")
        with self.assertRaises(McpError) as caught:
            self.confirm.check_use(view["confirmId"], **dict(self.bind, params_hash="hash-b"))
        self.assertEqual(caught.exception.code, "confirmation-mismatch")

    def test_presenter_failure_marks_unavailable(self):
        def broken(payload):
            raise ConfirmUnavailable("没有可交互桌面")
        view, _token = self.begin(broken)
        wait_state(self.confirm, view["confirmId"], "unavailable")
        with self.assertRaises(McpError) as caught:
            self.confirm.check_use(view["confirmId"], **self.bind)
        self.assertEqual(caught.exception.code, "confirmation-unavailable")

    def test_expired_pending_intent_reports_timeout(self):
        hold = threading.Event()
        view, _token = self.begin(presenter=lambda payload: hold.wait(30) or True)
        self.clock.advance(CONFIRM_TTL_SECONDS + 1)
        with self.assertRaises(McpError) as caught:
            self.confirm.check_use(view["confirmId"], **self.bind)
        self.assertEqual(caught.exception.code, "confirmation-expired")


class AdminConfirmationChannelTests(unittest.TestCase):
    """确认窗经 MCP 设置通道回传决定；该通道不在 MCP 认证路由上。"""

    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.bridge = make_manage(self.mcp, clock=self.mcp._clock)

    def test_resolve_through_the_settings_channel(self):
        box, shown, decided = [], threading.Event(), threading.Event()

        def presenter(payload):
            box.append(payload)
            shown.set()
            decided.wait(5)  # 真实窗口阻塞到用户决定；决定经设置通道回传
            return True

        self.bridge.confirm.presenter = presenter
        view = self.bridge.confirm.begin(action="zhiji_delete_account", kind="delete_account",
                                         client_id="mcp_x", client_name="合成客户端", auth_version=1,
                                         fingerprint=FINGERPRINT, account_id="acc-a",
                                         params_hash="hash-a", account_label="甲")
        self.assertTrue(shown.wait(5))
        payload = box[0]
        result = handle_admin(self.mcp, "POST", ["confirmations"], {},
                              {"confirmId": view["confirmId"], "token": payload["token"],
                               "granted": True})
        decided.set()
        self.assertEqual(result["state"], "confirmed")
        with self.assertRaises(McpAdminError) as caught:
            handle_admin(self.mcp, "POST", ["confirmations"], {},
                         {"confirmId": view["confirmId"], "token": payload["token"], "granted": False})
        self.assertEqual(caught.exception.code, "confirmation-already-resolved")

    def test_wrong_token_is_rejected(self):
        hold = threading.Event()
        self.bridge.confirm.presenter = lambda payload: hold.wait(30) or True
        view = self.bridge.confirm.begin(action="zhiji_delete_account", kind="delete_account",
                                         client_id="mcp_x", client_name="合成客户端", auth_version=1,
                                         fingerprint=FINGERPRINT, account_id="acc-a",
                                         params_hash="hash-a", account_label="甲")
        with self.assertRaises(McpAdminError) as caught:
            handle_admin(self.mcp, "POST", ["confirmations"], {},
                         {"confirmId": view["confirmId"], "token": "bad", "granted": True})
        self.assertEqual(caught.exception.code, "confirmation-token-invalid")


class CallRecordTests(unittest.TestCase):
    def setUp(self):
        self.mcp = make_services()
        self.addCleanup(self.mcp.close)
        self.bridge = make_manage(self.mcp, deleter=lambda account_id: None, clock=self.mcp._clock)
        self.principal, _created = pair(self.mcp, "完整客户端", "full")

    def test_management_actions_enter_the_call_record_with_limits(self):
        for _index in range(3):
            self.mcp.call(self.principal, {"action": "zhiji_get_plugins"})
            self.mcp.call(self.principal, {"action": "zhiji_get_model_settings"})
        self.mcp.call(self.principal, {"action": "zhiji_get_usage", "account": "acc-a"})
        records = self.mcp.store.recent_calls(core_mcp_store.MAX_CALL_RECORDS)
        actions = [item["action"] for item in records[:7]]
        for action in ("zhiji_get_plugins", "zhiji_get_model_settings", "zhiji_get_usage"):
            self.assertIn(action, actions)
        self.assertLessEqual(len(records), core_mcp_store.MAX_CALL_RECORDS)


class ContractSyncTests(unittest.TestCase):
    """登记目录、适配器目录与打包清单的一致性。"""

    def test_every_registered_action_has_an_adapter_catalog_entry(self):
        adapter = (Path(__file__).resolve().parents[1] / "scripts" / "mcp-stdio-adapter.py").read_text(
            encoding="utf-8")
        for name, entry in REGISTRY.items():
            if entry.get("handler"):
                self.assertIn(f'"{name}"', adapter, name)

    def test_new_runtime_files_are_staged(self):
        stage_path = Path(__file__).resolve().parents[1] / "scripts" / "stage-real-client.py"
        spec = importlib.util.spec_from_file_location("stage_for_manage_test", stage_path)
        stage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(stage)
        self.assertTrue({"core_mcp_manage.py", "core_mcp_confirm.py"} <= set(stage.BRIDGE))


if __name__ == "__main__":
    unittest.main()
