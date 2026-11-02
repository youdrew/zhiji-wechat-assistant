"""分析与建议 coordinator: per-friend switch, freshness of saved results and the work queue.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Moved from Core (message_analysis.MessageAnalysis) into kyf.message-insights, 2026-10-13.
New messages arrive as Core ``message.new`` events instead of polling WeChat here.
"""
import json
import threading
import time
import uuid
from collections import OrderedDict
from kyf_plugin import CoreError

from prompts import UNIFIED_VERSION, VERSION, InsightError, message_hash, \
    row_from_view, valid_ids
from store import AnalysisStore, result_bindings, saved_result, source_binding
from kyf_plugin.settings import read_settings

ROWS_BATCH = 500
COPY_CHECK_ATTEMPTS = 3
COPY_CHECK_DELAY = 0.25
COPY_CHECK_RETRY_WINDOW = 12.0


class Insights:
    def __init__(self, plugin, clock=time.time):
        self.plugin = plugin
        # Construct the SDK's lazy shared client before workers can race on it.
        self.models = plugin.models
        self.clock = clock
        self.lock = threading.RLock()
        self.stores = {}
        self.entries = {}
        self.subscriptions = {}
        self.queue = OrderedDict()
        self.queue_sequence = 0
        self.active = {}
        self.dispatch_limit = None
        self.wake = threading.Event()
        self.closed = False
        from worker import Worker
        self.worker = Worker(self)
        self.plugin.on_shutdown.append(self.close)
        threading.Thread(target=self._loop, daemon=True, name="insights-dispatch").start()

    # Plumbing ---------------------------------------------------------------------------------------------------------
    @property
    def core(self):
        return self.plugin.core

    def store(self, account):
        with self.lock:
            store = self.stores.get(account)
            if store is None:
                store = self.stores[account] = AnalysisStore(self.plugin.data_dir, account)
            return store

    def check_account(self, account):
        scope = self.core.scope()
        if not scope.get("account") or scope["account"] != account:
            raise InsightError("account-changed", "微信账号已变化")

    def config_stamp(self, refresh=False):
        services = self.models.services(refresh)
        return services[0].get("stamp") if services else None

    def binding(self, account, user, *, unified, status=None, profile=None):
        status = status or self.core.history_status(account, user)
        profile = profile or self.core.get(f"human/{user}", account=account, view="snapshot")["stamp"]
        return (status.get("sourceFingerprint"),
                (self.config_stamp() or "unconfigured") + ":" + (UNIFIED_VERSION if unified else VERSION),
                profile, status.get("epoch"))

    def projected(self, account, user, ids):
        rows = []
        for start in range(0, len(ids), ROWS_BATCH):
            batch = list(ids)[start:start + ROWS_BATCH]
            result = self.core.post("history/rows", {"account": account, "user": user, "ids": batch})
            rows.extend(row_from_view(view) for view in result["messages"])
        return rows

    # Freshness --------------------------------------------------------------------------------------------------------
    def fresh_rows(self, account, user, store, ids):
        # Display reads must never turn a temporary outage into a permanent
        # generation failure, nor cancel an independent running task.
        return store.rows(user, ids)

    def verify_sources(self, account, user, rows, *, parts=("analysis", "suggestions"), fresh=False):
        checks, wanted = {}, set()
        for row in rows:
            bindings = result_bindings(row)
            for part in parts:
                if saved_result(row, part) is None:
                    continue
                binding = bindings.get(part) or source_binding(row)
                if not isinstance(binding, dict):
                    binding = {}
                hashes = binding.get("context_hashes")
                if not isinstance(hashes, dict) or any(not isinstance(key, str) for key in hashes):
                    hashes = {row["message_id"]: None}
                hashes = dict(hashes)
                if row["message_id"] not in hashes:
                    hashes[row["message_id"]] = binding.get("message_hash")
                checks[(row["message_id"], part)] = {"binding": binding, "hashes": hashes}
                wanted.update(hashes)
        if not checks:
            return {}, None
        unavailable = ("unavailable", "原消息暂时无法核对，已保存结果仍可查看")
        try:
            before = self.core.history_status(account, user, fresh=fresh)
            candidates = {row["msg_id"]: row for row in self.projected(account, user, sorted(wanted))}
            after = self.core.history_status(account, user, fresh=fresh)
            self.check_account(account)
        except Exception:
            return {key: unavailable for key in checks}, None
        source_keys = ("sourceFingerprint", "currentSourceFingerprint", "epoch")
        stable = all(before.get(key) == after.get(key) for key in source_keys)
        result = {}
        for key, check in checks.items():
            binding, hashes = check["binding"], check["hashes"]
            scope = binding.get("scope")
            current_scope = after.get("currentSourceFingerprint")
            if scope and current_scope and scope != current_scope:
                value = ("changed", "聊天数据来源已切换，原结果保留供参考")
            elif not stable or any(status.get("state") in ("reading", "idle") for status in (before, after)):
                value = ("checking", "聊天记录正在同步，原结果保留供参考")
            elif any(status.get("state") != "ready" for status in (before, after)) or not scope or not current_scope:
                value = unavailable
            elif after.get("sourceFingerprint") != scope:
                value = ("checking", "聊天记录正在重新核对，原结果已保留")
            elif not hashes or any(not isinstance(digest, str) or not digest for digest in hashes.values()):
                value = ("unavailable", "旧结果缺少来源记录，原结果保留供参考")
            elif any(message_id not in candidates or message_hash(candidates[message_id]) != digest
                     for message_id, digest in hashes.items()):
                value = ("changed", "原消息或引用的聊天内容已变化，原结果保留供参考")
            else:
                value = ("verified", "")
            result[key] = value
        return result, after

    @staticmethod
    def incoming(message, user):
        return (message.get("senderResolved") is True and message.get("side") == "other" and
                message.get("senderId") == user and message.get("kind") != "system" and
                isinstance(message.get("id"), str) and bool(message["id"]))

    def latest(self, account, user):
        recent = self.core.get("history/recent", account=account, user=user, limit=50)
        return next((item for item in reversed(recent["messages"]) if item.get("kind") != "system"), None)

    # Interface --------------------------------------------------------------------------------------------------------
    def status(self, account, user, ids=None):
        if ids is not None:
            ids = valid_ids(ids)
        self.check_account(account)
        store = self.store(account)
        rows = self.fresh_rows(account, user, store, ids)
        sources, history = self.verify_sources(account, user, rows)
        latest = None
        if rows:
            try:
                latest = self.latest(account, user)
            except Exception:
                pass
        current_binding = None
        if sources and history and history.get("state") == "ready":
            try:
                current_binding = self.binding(account, user, unified=True, status=history)
            except Exception:
                pass  # Background/model availability is not a copying requirement.
        setting = store.settings(user)
        items = []
        for row in rows:
            values = {part: saved_result(row, part) for part in ("analysis", "suggestions")}
            states = {part: sources.get((row["message_id"], part), ("checking", "")) for part in values}
            existing = [states[part] for part in values if values[part] is not None]
            source_state, source_message = max(existing, key=lambda value:
                {"verified": 0, "checking": 1, "unavailable": 2, "changed": 3}[value[0]]) if existing else ("checking", "")
            bindings = result_bindings(row)
            background_changed = current_binding is not None and any(
                (binding.get("config_stamp"), binding.get("profile_stamp")) != current_binding[1:3]
                for part, binding in bindings.items() if part in values and values[part] is not None and
                isinstance(binding, dict))
            applicability = ("needs-update" if source_state == "changed" else "unverified"
                if existing and source_state in ("checking", "unavailable") else "historical"
                if background_changed or latest is None or latest["id"] != row["message_id"] or
                not self.incoming(latest, user) else "current")
            if not source_message and background_changed:
                source_message = "背景资料或模型已更新，原结果保留供参考"
            item = {"messageId": row["message_id"], "state": row["state"], "version": row["generation"],
                    "analysisState": row["analysis_state"], "suggestionsState": row["suggestions_state"],
                    "contextVersion": row["context_version"],
                    "applicability": applicability, "sourceState": source_state, "sourceMessage": source_message,
                    "analysisSourceState": states["analysis"][0], "suggestionsSourceState": states["suggestions"][0],
                    "copyAllowed": bool(setting["unified_enabled"] and values["suggestions"] is not None)}
            if values["analysis"] is not None:
                item["analysis"] = values["analysis"]
            if values["suggestions"] is not None:
                item["replies"] = values["suggestions"]
            if row["error"]:
                item["error"] = item["analysisError"] = row["error"]
            if row["suggestions_error"]:
                item["suggestionsError"] = row["suggestions_error"]
            items.append(item)
        legacy = {"analysisEnabled": bool(setting["enabled"]), "suggestionsEnabled": store.legacy_auto_reply(user)}
        result = {"account": account, "user": user, "enabled": bool(setting["unified_enabled"]), "items": items,
                  "legacy": legacy, "migrationRequired": setting["unified_enabled"] is None and any(legacy.values()),
                  "pendingCount": store.pending_count(user)}
        with self.lock:
            error = self.entries.get((account, user), {}).get("error")
        if error:
            result["error"] = error
        return result

    def require_model(self):
        if not self.models.services(refresh=True):
            raise InsightError("api-not-configured", "尚未配置生成模型，请先在设置中保存模型")

    def restore_subscriptions(self):
        """Restore watches in the dispatcher, without generating historical results."""
        account = self.core.scope().get("account")
        if not account:
            return
        # An account-scoped Core call completes legacy migration before we open
        # the plugin database. A bare scope read does not perform that migration.
        self.core.get("scope", account=account)
        store = self.store(account)
        for user in store.enabled_users():
            with self.lock:
                setting = store.settings(user)
                if not setting["unified_enabled"]:
                    continue
                key = (account, user)
                if self.subscriptions.get(key) == setting["revision"]:
                    continue
                entry = self.entries.get(key)
                if entry is None or entry["revision"] != setting["revision"]:
                    self.entries[key] = {"armed": None, "revision": setting["revision"], "error": None}
                self.core.subscribe(account, user, replay_live=True)
                self.subscriptions[key] = setting["revision"]

    def generate(self, account, user, message_id):
        """Explicit first generation; Core owns the target facts and chat scope."""
        ids = valid_ids([message_id])
        self.check_account(account)
        store = self.store(account)
        with self.lock:
            setting = store.settings(user)
            if not setting["unified_enabled"]:
                raise InsightError("request-conflict", "请先开启这位好友的分析与建议")
            if store.rows(user, ids):
                return self.status(account, user, ids)
        self.require_model()
        request = {"account": account, "user": user, "ids": ids, "verifySender": True}
        views = self.core.post("history/rows", request)["messages"]
        if not views:
            self.core.post("history/prepare", {"account": account, "user": user, "refresh": True})
            views = self.core.post("history/rows", request)["messages"]
        if (len(views) != 1 or views[0].get("senderResolved") is not True or
                views[0].get("side") != "other" or views[0].get("senderId") != user or
                views[0].get("group") or views[0].get("kind") == "system"):
            raise InsightError("not-found", "未找到可核对的好友消息；记录若正在整理，请稍后重试")
        self.check_account(account)
        with self.lock:
            current = store.settings(user)
            if not current["unified_enabled"] or current["revision"] != setting["revision"]:
                raise InsightError("request-conflict", "分析与建议开关已变化，请重试")
            # A second click or a live event may have already registered it.
            if not store.rows(user, ids):
                self.enqueue(account, user, ids, current, automatic=False, unified=True, source_messages=views)
        self.wake.set()
        return self.status(account, user, ids)

    def unanswered(self, account, user):
        """The friend's latest messages after the user's last reply."""
        limit = 200
        while True:
            recent = self.core.get("history/recent", account=account, user=user, limit=limit)
            messages, boundary = [], False
            for message in reversed(recent["messages"]):
                if message.get("kind") == "system":
                    continue
                if not self.incoming(message, user):
                    boundary = True
                    break
                messages.append(message)
            if boundary or not recent.get("hasMoreBefore") or limit >= 2000:
                return list(reversed(messages))[-240:]
            limit *= 2

    def set(self, account, user, enabled):
        if type(enabled) is not bool:
            raise InsightError("invalid-request", "分析与建议开关不正确")
        self.check_account(account)
        store = self.store(account)
        if enabled:
            self.require_model()
        armed = int(self.clock()) * 1000
        messages = self.unanswered(account, user) if enabled else []
        ids = [message["id"] for message in messages]
        plugin_settings = read_settings(self.core, {"replyStyle": ("natural", "brief", "polite")}) if enabled else None
        with self.lock:
            previous = store.settings(user)
            unchanged = previous["unified_enabled"] is not None and bool(previous["unified_enabled"]) == enabled
            setting = previous if unchanged else store.set(user, enabled)
            key = (account, user)
            if not unchanged:
                self.cancel(account, user)
                self.entries.pop(key, None)
                if enabled:
                    self.entries[key] = {"armed": armed, "revision": setting["revision"], "error": None}
                    self.enqueue(account, user, ids, setting, automatic=True, unified=True,
                                 plugin_settings=plugin_settings, source_messages=messages)
            if enabled:
                if key not in self.entries:
                    self.entries[key] = {"armed": None, "revision": setting["revision"], "error": None}
                self.core.subscribe(account, user, replay_live=True)
                self.subscriptions[key] = setting["revision"]
            else:
                self.core.unsubscribe(account, user)
                self.subscriptions.pop(key, None)
        self.wake.set()
        return self.status(account, user)

    def retry(self, account, user, message_id, part, expected_version):
        ids = valid_ids([message_id])
        if part not in ("analysis", "suggestions", "all") or not isinstance(expected_version, str):
            raise InsightError("invalid-request", "重试参数不正确")
        self.check_account(account)
        self.require_model()
        store = self.store(account)
        with self.lock:
            setting = store.settings(user)
            rows = self.fresh_rows(account, user, store, ids)
            if not setting["unified_enabled"] or not rows or rows[0]["generation"] != expected_version:
                raise InsightError("request-conflict", "结果或开关已变化，请重新选择")
            row = rows[0]
            if row["state"] not in ("pending", "running"):
                requested = ("analysis", "suggestions") if part == "all" else (part,)
                parts = tuple(value for value in requested if part != "all" or row[value + "_state"] != "ready" or
                              saved_result(row, value) is None)
                if parts:
                    sources = self.core.post("history/rows", {"account": account, "user": user, "ids": ids})["messages"]
                    self.enqueue(account, user, ids, setting, automatic=False, unified=True, parts=parts, force=True,
                                 source_messages=sources)
        self.wake.set()
        return self.status(account, user, ids)

    def copy(self, account, user, message_id, version, index):
        valid_ids([message_id])
        if not isinstance(version, str) or type(index) is not int or not 0 <= index < 3:
            raise InsightError("invalid-request", "候选标识不正确")
        self.check_account(account)
        store = self.store(account)
        setting = store.settings(user)
        row = next(iter(store.rows(user, [message_id])), None)
        replies = saved_result(row, "suggestions") if row else None
        if not setting["unified_enabled"] or row is None or row["generation"] != version or replies is None:
            raise InsightError("request-conflict", "候选或上下文已变化，请重新选择")
        deadline = time.monotonic() + COPY_CHECK_RETRY_WINDOW
        for attempt in range(COPY_CHECK_ATTEMPTS):
            if attempt and time.monotonic() >= deadline:
                raise InsightError("source-unavailable", message)
            # A fresh ownership read or a concurrent import can be unavailable for
            # one check. Re-read the whole proof; never fall back to cached identity.
            self.check_account(account)
            with self.lock:
                current = next(iter(store.rows(user, [message_id])), None)
                if (store.settings(user) != setting or current is None or current["generation"] != version or
                        saved_result(current, "suggestions") != replies):
                    raise InsightError("request-conflict", "候选或开关已变化，请重新选择")
            sources, history = self.verify_sources(account, user, [current], parts=("suggestions",), fresh=True)
            state, message = sources.get((message_id, "suggestions"), ("unavailable", "原消息暂时无法核对，请稍后再试"))
            if state == "verified":
                break
            # Missing legacy bindings and a real change are permanent for this
            # candidate. Only unstable reads and temporary read failures may retry.
            transient = state == "checking" or (state == "unavailable" and history is None)
            if not transient or attempt + 1 >= COPY_CHECK_ATTEMPTS or time.monotonic() >= deadline:
                raise InsightError("source-changed" if state == "changed" else "source-unavailable", message)
            time.sleep(min(COPY_CHECK_DELAY * (attempt + 1), max(0, deadline - time.monotonic())))
        self.check_account(account)
        with self.lock:
            current = next(iter(store.rows(user, [message_id])), None)
            if (store.settings(user) != setting or current is None or current["generation"] != version or
                    saved_result(current, "suggestions") != replies):
                raise InsightError("request-conflict", "候选或开关已变化，请重新选择")
        return {"account": account, "user": user, "messageId": message_id, "version": version, "index": index,
                "text": replies[index]["text"]}

    # Events and queue -------------------------------------------------------------------------------------------------
    def on_message(self, event):
        account, user = event.get("account"), event.get("user")
        store = self.store(account)
        setting = store.settings(user)
        if not setting["unified_enabled"]:
            return
        with self.lock:
            entry = self.entries.get((account, user))
            if entry is None or entry["revision"] != setting["revision"]:
                entry = self.entries[(account, user)] = {"armed": None,
                                                         "revision": setting["revision"], "error": None}
            if entry["armed"] is None:
                # Core starts before lazy plugins and supplies the live boundary.
                # Using this event's delivery time would discard its first batch.
                live_since = event.get("liveSince")
                if type(live_since) is not int or not 0 <= live_since <= int(self.clock() * 1000) + 5000:
                    entry["error"] = "消息监听尚未就绪，请重新开启分析与建议"
                    raise InsightError("context-error", entry["error"])
                entry["armed"] = live_since
            now = int(self.clock() * 1000)
            fresh = [message for message in event.get("data", {}).get("messages", [])
                     if self.incoming(message, user) and type(message.get("time")) is int and
                     entry["armed"] <= message["time"] <= now + 5000]
            entry["error"] = None
            try:
                self.enqueue(account, user, [message["id"] for message in fresh], setting,
                             automatic=True, unified=True, source_messages=fresh)
            except CoreError as exc:
                entry["error"] = exc.message
                raise
        self.wake.set()

    @staticmethod
    def message_order(message):
        value = message.get("sortKey")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                return ()
        if (isinstance(value, (list, tuple)) and len(value) == 3 and type(value[0]) is int and
                isinstance(value[1], str) and type(value[2]) is int):
            return tuple(value)
        return ()

    def enqueue(self, account, user, ids, setting, *, automatic, unified=False, parts=None, force=False,
                plugin_settings=None, source_messages=None):
        store = self.store(account)
        ids = list(dict.fromkeys(ids))
        previous = {row["message_id"]: row for row in store.rows(user, ids)}
        ids = [value for value in ids if force or value not in previous or
               previous[value]["state"] not in ("ready", "pending", "running", "partial") or
               (unified and previous[value]["schema_version"] != 2)]
        if not ids:
            return
        settings = plugin_settings if plugin_settings is not None else read_settings(
            self.core, {"replyStyle": ("natural", "brief", "polite")})
        entry = self.queue.setdefault((account, user), {"ids": [], "jobs": {}})
        entry["ids"].extend(value for value in ids if value not in entry["ids"])
        parts = parts or (("analysis", "suggestions") if unified else ("analysis",))
        sources = {message["id"]: message for message in source_messages or []}
        for message_id in ids:
            generation = "analysis_" + uuid.uuid4().hex
            source = sources.get(message_id, {})
            sent_at = source.get("time")
            self.queue_sequence = getattr(self, "queue_sequence", 0) + 1
            entry["jobs"][message_id] = {
                "generation": generation, "revision": setting["revision"], "automatic": automatic,
                "settings": settings,
                "message_time": sent_at if type(sent_at) is int and sent_at >= 0 else 0,
                "message_order": self.message_order(source), "queued_order": self.queue_sequence,
                "unified": unified, "parts": parts,
                "preserved_context": None}
            store.begin(user, [message_id], generation, parts=parts, unified=unified)

    def cancel(self, account, user):
        with self.lock:
            entry = self.queue.pop((account, user), None)
            if entry is not None:
                store = self.store(account)
                for message_id, job in entry["jobs"].items():
                    store.finish(user, [message_id], job["generation"], "error", error="任务已取消，可以重试")
            for job in self.active.values():
                if (job.account, job.user) == (account, user):
                    job.cancel()
                    self.store(account).finish(user, job.ids, job.spec["generation"], "error",
                                               error="任务已取消，可以重试")

    def close(self):
        with self.lock:
            self.closed = True
            for account, user in set(self.queue) | {(job.account, job.user) for job in self.active.values()}:
                self.cancel(account, user)
        self.wake.set()

    def _dispatch(self):
        account = self.core.scope().get("account")
        with self.lock:
            self.active = {key: job for key, job in self.active.items() if job.thread.is_alive()}
            owners = set(self.queue) | {(job.account, job.user) for job in self.active.values()}
            for owner, user in owners:
                if owner != account:
                    self.cancel(owner, user)
            for key in list(self.entries):
                if key[0] != account:
                    self.entries.pop(key, None)
                    self.subscriptions.pop(key, None)
            if self.closed or not account or not self.queue:
                return
        # 每模型并发随主模型服务下发，每次派发重读（统一调度总额设置已退役）。
        services = self.models.services(refresh=True)
        if not services:
            raise CoreError(503, "api-not-configured", "尚未配置生成模型")
        limit = services[0].get("concurrency")
        if type(limit) is not int or not 1 <= limit <= 8:
            limit = 4
        self.dispatch_limit = limit
        with self.lock:
            # Match Core's capacity for local backpressure and recent-message
            # priority; the gateway owns the actual limit across all plugins.
            # Running and cancelled jobs keep slots until their threads exit.
            while not self.closed and len(self.active) < limit and self.queue:
                selected = None
                selected_priority = None
                for (owner, user), entry in self.queue.items():
                    if owner != account:
                        continue
                    eligible = [value for value in entry["ids"] if (owner, user, value) not in self.active]
                    if not eligible:
                        continue
                    # Original order is meaningful within one conversation. For
                    # same-time messages across friends, prefer the latest arrival.
                    message_id = max(eligible, key=lambda value: (
                        entry["jobs"][value]["message_time"], entry["jobs"][value]["message_order"],
                        entry["jobs"][value]["queued_order"]))
                    spec = entry["jobs"][message_id]
                    priority = (spec["message_time"], spec["queued_order"])
                    if selected_priority is None or priority > selected_priority:
                        selected = owner, user, message_id
                        selected_priority = priority
                if selected is None:
                    break
                owner, user, message_id = selected
                entry = self.queue[(owner, user)]
                entry["ids"].remove(message_id)
                spec = entry["jobs"].pop(message_id)
                if not entry["ids"]:
                    self.queue.pop((owner, user))
                store = self.store(owner)
                setting = store.settings(user)
                row = next(iter(store.rows(user, [message_id])), None)
                if (not setting["unified_enabled"] or setting["revision"] != spec["revision"] or
                        row is None or row["generation"] != spec["generation"] or row["state"] != "pending"):
                    store.finish(user, [message_id], spec["generation"], "error", error="任务设置已变化，可以重试")
                    continue
                try:
                    self.active[selected] = self.worker.start(owner, user, [message_id], spec)
                except Exception:
                    store.finish(user, [message_id], spec["generation"], "error", error="本条任务未能启动，可以重试")
            for entry in self.entries.values():
                if entry.get("error") == "任务调度暂未就绪，请稍后重试":
                    entry["error"] = None

    def _loop(self):
        while not self.closed:
            self.wake.wait(1)
            self.wake.clear()
            if self.closed:
                return
            try:
                self.restore_subscriptions()
            except Exception:
                pass  # A later dispatch retries; status reads never enqueue work.
            try:
                self._dispatch()
            except Exception:
                with self.lock:
                    for key in self.queue:
                        if key in self.entries:
                            self.entries[key]["error"] = "任务调度暂未就绪，请稍后重试"
