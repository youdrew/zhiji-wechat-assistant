"""分析与建议 coordinator: per-friend switch, freshness of saved results and the work queue.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Moved from Core (message_analysis.MessageAnalysis) into kyf.message-insights, 2026-10-13.
New messages arrive as Core ``message.new`` events instead of polling WeChat here.
Modified 2026-11-09: work is per round. A friend's new message (re)starts a quiet timer; once the
friend has been quiet for the configured seconds, one job covers every message since the user's
last reply. A newer message cancels the running round and the timer starts again; the user's own
reply ends the round.
"""
import json
import threading
import time
import uuid
from collections import OrderedDict
from kyf_plugin import CoreError

from prompts import UNIFIED_VERSION, VERSION, InsightError, message_hash, \
    row_from_view, valid_ids
from store import AnalysisStore, result_bindings, round_ids, saved_result, source_binding
from kyf_plugin.settings import read_settings

ROWS_BATCH = 500
COPY_CHECK_ATTEMPTS = 3
COPY_CHECK_DELAY = 0.25
COPY_CHECK_RETRY_WINDOW = 12.0
SETTING_FIELDS = {"replyStyle": ("natural", "brief", "polite")}  # quietSeconds is optional, see quiet_seconds
QUIET_DEFAULT, QUIET_MIN, QUIET_MAX = 20, 5, 300
SUPERSEDED = "对方又发来新消息，已按整轮重新生成"
ANSWERED = "你已回复，本轮未继续生成"
PARTS = ("analysis", "suggestions")


def enabled_parts(setting):
    return tuple(part for part in PARTS if setting[part + "_enabled"])


def covered_at(messages):
    times = [message.get("time") for message in messages if type(message.get("time")) is int]
    return max(times, default=-1)


def quiet_seconds(settings):
    value = (settings or {}).get("values", {}).get("quietSeconds")
    return min(QUIET_MAX, max(QUIET_MIN, value)) if type(value) is int else QUIET_DEFAULT


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
            item = {"messageId": row["message_id"], "roundIds": round_ids(row), "state": row["state"],
                    "version": row["generation"],
                    "analysisState": row["analysis_state"], "suggestionsState": row["suggestions_state"],
                    "contextVersion": row["context_version"],
                    "applicability": applicability, "sourceState": source_state, "sourceMessage": source_message,
                    "analysisSourceState": states["analysis"][0], "suggestionsSourceState": states["suggestions"][0],
                    "copyAllowed": bool(setting["suggestions_enabled"] and values["suggestions"] is not None)}
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
                  "analysisEnabled": bool(setting["analysis_enabled"]), "suggestionsEnabled": bool(setting["suggestions_enabled"]),
                  "legacy": legacy, "migrationRequired": setting["unified_enabled"] is None and any(legacy.values()),
                  "pendingCount": store.pending_count(user)}
        with self.lock:
            entry = self.entries.get((account, user), {})
            error, due = entry.get("error"), entry.get("due")
        if error:
            result["error"] = error
        if due is not None and setting["unified_enabled"]:
            result["roundDueInMs"] = max(0, int((due - self.clock()) * 1000))
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
                raise InsightError("request-conflict", "请先开启这位好友的分析或建议")
            row = next(iter(store.rows(user, ids)), None)
            if row and (row["state"] in ("pending", "running") or
                        all(row[p + "_state"] != "none" for p in enabled_parts(setting))):
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

    def set(self, account, user, enabled, part="all"):
        if type(enabled) is not bool or part not in (*PARTS, "all"):
            raise InsightError("invalid-request", "分析与建议开关不正确")
        self.check_account(account)
        store = self.store(account)
        if enabled:
            self.require_model()
        armed = int(self.clock()) * 1000
        messages = self.unanswered(account, user) if enabled else []
        ids = [message["id"] for message in messages]
        plugin_settings = read_settings(self.core, SETTING_FIELDS) if enabled else None
        key = (account, user)
        with self.lock, store.lock:
            previous = store.settings(user)
            before = enabled_parts(previous)
            analysis = enabled if part in ("analysis", "all") else bool(previous["analysis_enabled"])
            suggestions = enabled if part in ("suggestions", "all") else bool(previous["suggestions_enabled"])
            changed = (previous["unified_enabled"] is None or analysis != previous["analysis_enabled"] or
                       suggestions != previous["suggestions_enabled"])
            setting = store.set(user, analysis, suggestions) if changed else previous
            parts = enabled_parts(setting)
            if parts:
                entry = self.entries.setdefault(key, {"armed": None, "error": None})
                entry["revision"] = setting["revision"]
                added = tuple(p for p in parts if p not in before)
                if added and not before:
                    entry.update(armed=armed, covered=set(ids), covered_at=covered_at(messages))
                anchor = ids[-1] if ids and "due" not in entry else None
                if changed:
                    self.reconfigure_jobs(account, user, setting, anchor, added)
                if added and anchor:
                    self.enqueue(account, user, [anchor], setting, automatic=True, unified=True, parts=added,
                                 retry_failed=True, plugin_settings=plugin_settings, source_messages=messages)
                if key not in self.subscriptions or not changed:
                    self.core.subscribe(account, user, replay_live=True)
                self.subscriptions[key] = setting["revision"]
            else:
                self.cancel(account, user)
                self.entries.pop(key, None)
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
            allowed = enabled_parts(setting)
            if not allowed or (part != "all" and part not in allowed) or not rows or rows[0]["generation"] != expected_version:
                raise InsightError("request-conflict", "结果或开关已变化，请重新选择")
            row = rows[0]
            if row["state"] not in ("pending", "running"):
                requested = allowed if part == "all" else (part,)
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
        if not setting["suggestions_enabled"] or row is None or row["generation"] != version or replies is None:
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
        with self.lock:
            setting = store.settings(user)
            if not setting["unified_enabled"]:
                return
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
            live = [message for message in event.get("data", {}).get("messages", [])
                    if type(message.get("time")) is int and entry["armed"] <= message["time"] <= now + 5000]
            # A replayed live message already inside the started round is not new: it must not cancel that round.
            fresh = [message for message in live if self.incoming(message, user) and
                     message["id"] not in entry.get("covered", ())]
            # Only the user's message after the round's last one answers it (not a replayed older one).
            replied = any(message.get("side") == "self" and message.get("kind") != "system" and
                          message["time"] > entry.get("covered_at", -1) for message in live)
            entry["error"] = None
            if fresh:
                try:
                    settings = read_settings(self.core, SETTING_FIELDS)
                except CoreError as exc:
                    entry["error"] = exc.message
                    raise
                # The friend is still typing: the running round is incomplete, restart the quiet timer.
                self.supersede(account, user)
                entry["due"] = self.clock() + quiet_seconds(settings)
                entry["settings"] = settings
            elif replied:
                # The user answered; this round needs no suggestions any more.
                entry.pop("due", None)
                store.remove(user, [message_id for message_id in self.supersede(account, user, ANSWERED)
                                    if not any(saved_result(row, part) for row in store.rows(user, [message_id])
                                               for part in ("analysis", "suggestions"))])
        self.wake.set()

    def supersede(self, account, user, error=SUPERSEDED):
        """Cancel automatic round jobs of this friend (queued or running); manual ones stay."""
        dropped = []
        with self.lock:
            store = self.store(account)
            entry = self.queue.get((account, user))
            if entry is not None:
                for message_id in list(entry["ids"]):
                    job = entry["jobs"][message_id]
                    if job["automatic"]:
                        entry["ids"].remove(message_id)
                        entry["jobs"].pop(message_id)
                        store.finish(user, [message_id], job["generation"], "error", error=error)
                        dropped.append(message_id)
                if not entry["ids"]:
                    self.queue.pop((account, user), None)
            for job in self.active.values():
                if (job.account, job.user) == (account, user) and job.spec["automatic"] and \
                        not job.cancelled.is_set():
                    job.cancel()
                    store.finish(user, job.ids, job.spec["generation"], "error", error=error)
                    dropped.extend(job.ids)
        return dropped

    def due_rounds(self):
        """Start the rounds whose friend has been quiet long enough."""
        now = self.clock()
        with self.lock:
            ready = [(key, entry) for key, entry in self.entries.items()
                     if entry.get("due") is not None and entry["due"] <= now]
            for _key, entry in ready:
                entry.pop("due", None)
        for (account, user), entry in ready:
            try:
                self.start_round(account, user, entry.get("settings"))
            except Exception as exc:
                with self.lock:
                    entry["error"] = getattr(exc, "message", None) or "本轮分析未能开始，请稍后重试"

    def start_round(self, account, user, settings=None):
        self.check_account(account)
        store = self.store(account)
        messages = self.unanswered(account, user)
        if not messages:
            return
        ids = [message["id"] for message in messages]
        with self.lock:
            setting = store.settings(user)
            if not setting["unified_enabled"]:
                return
            # Earlier rounds of these same messages were cut short by this one.
            store.remove(user, [row["message_id"] for row in store.rows(user, ids[:-1])
                                if row.get("round_json") or SUPERSEDED in (row.get("error"), row.get("suggestions_error"))])
            self.enqueue(account, user, ids[-1:], setting, automatic=True, unified=True,
                         plugin_settings=settings, source_messages=messages)
            self.entries.setdefault((account, user), {"armed": None, "revision": setting["revision"],
                                                      "error": None}).update(covered=set(ids), covered_at=covered_at(messages))
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

    def enqueue(self, account, user, ids, setting, *, automatic, unified=False, parts=None, force=False, retry_failed=False,
                plugin_settings=None, source_messages=None):
        store = self.store(account)
        ids = list(dict.fromkeys(ids))
        previous = {row["message_id"]: row for row in store.rows(user, ids)}
        if parts is None:
            parts = enabled_parts(setting) if unified else ("analysis",)
        needed = {}
        for value in ids:
            row = previous.get(value)
            if force or row is None or (unified and row["schema_version"] != 2):
                needed[value] = parts
            elif row["state"] not in ("pending", "running"):
                retry = retry_failed or row["state"] == "error"
                needed[value] = tuple(p for p in parts if row[p + "_state"] == "none" or
                                      (retry and row[p + "_state"] != "ready"))
        ids = [value for value in ids if needed.get(value)]
        if not ids:
            return
        settings = plugin_settings if plugin_settings is not None else read_settings(self.core, SETTING_FIELDS)
        entry = self.queue.setdefault((account, user), {"ids": [], "jobs": {}})
        entry["ids"].extend(value for value in ids if value not in entry["ids"])
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
                "unified": unified, "parts": needed[message_id],
                "preserved_context": None}
            store.begin(user, [message_id], generation, parts=needed[message_id], unified=unified)

    def reconfigure_jobs(self, account, user, setting, anchor, added):
        """Keep unfinished enabled work, including manual jobs on older rounds. Caller holds both locks."""
        key = (account, user)
        store = self.store(account)
        queue = self.queue.get(key, {"ids": [], "jobs": {}})
        jobs = {mid: (job.spec, job) for (owner, friend, mid), job in self.active.items()
                if (owner, friend) == key and not job.cancelled.is_set()}
        jobs.update({mid: (spec, None) for mid, spec in queue["jobs"].items()})
        allowed = enabled_parts(setting)
        for mid, (spec, active) in jobs.items():
            row = next(iter(store.rows(user, [mid])), None)
            if not row or row["generation"] != spec["generation"] or row["state"] not in ("pending", "running"):
                continue
            unfinished = tuple(p for p in spec["parts"] if row[p + "_state"] in ("pending", "running"))
            wanted = tuple(p for p in allowed if p in unfinished or
                           (mid == anchor and p in added and row[p + "_state"] != "ready"))
            if wanted == unfinished:
                spec["revision"] = setting["revision"]
                continue
            if active:
                active.cancel()
            else:
                queue["ids"].remove(mid)
                queue["jobs"].pop(mid)
            for p in unfinished:
                if p not in wanted:
                    store.withdraw(user, mid, spec["generation"], p, row)
            if wanted:
                generation = "analysis_" + uuid.uuid4().hex
                queue["ids"].append(mid)
                queue["jobs"][mid] = {**spec, "generation": generation, "parts": wanted, "revision": setting["revision"]}
                store.begin(user, [mid], generation, parts=wanted, unified=True)
        if queue["ids"]:
            self.queue[key] = queue
        else:
            self.queue.pop(key, None)

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
                self.due_rounds()
            except Exception:
                pass  # Each round records its own error; the next tick continues.
            try:
                self._dispatch()
            except Exception:
                with self.lock:
                    for key in self.queue:
                        if key in self.entries:
                            self.entries[key]["error"] = "任务调度暂未就绪，请稍后重试"
