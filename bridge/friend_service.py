"""关系记忆 and the other human-owned friend data: memories, evidence, goals, observations, drafts.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The service is owned by :class:`core_accounts.AccountDataService` and reuses its
account databases and managed chat projection. Every public call re-checks that
``account`` is the current WeChat account and reads only rows belonging to
``(account, user)``.

Modified by Know Your Friends, 2026-10-13 (Apache-2.0): plugin architecture. Analysis,
memory extraction and the analysis pages moved to the kyf.friend-analysis plugin; this
host service keeps the data the user owns and verifies evidence against the projection.
Changes are announced to plugins as ``human.changed``; an opened window as ``friend.opened``.
Modified 2026-10-13: owned by Core's AccountDataService; the prompt rendering of this data
(context_section/context_sources) moved to the plugins that build prompts.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from core_contracts import AdvisorError, valid_scope_value
from friend_store import TAGS, FriendStore, content_hash
from friend_context import message_role, snapshot as friend_snapshot

PAGE_SIZE = 50
EVIDENCE_CONTEXT = 5
JOIN_SECONDS = 200
GOAL_PRESETS = ("自然交流", "保持联系", "了解近况", "讨论具体事项", "安排见面")
# MCP 读取入口的文本上限：最近消息单条 2000 字、搜索命中 500 字、会话预览 80 字。
MESSAGE_TEXT_MAX = 2000
SEARCH_TEXT_MAX = 500
PREVIEW_TEXT_MAX = 80


def _message_view(row, limit):
    """One saved message in the MCP read shape; media without a transcript has no text."""
    try:
        raw = json.loads(row.get("raw") or "{}")
    except ValueError:
        raw = {}
    media = raw.get("media") if isinstance(raw, dict) else None
    text = row.get("text") or ""
    if isinstance(media, dict):
        text = "[语音转写] " + media["transcript"] if media.get("kind") in ("voice", "audio") and \
            isinstance(media.get("transcript"), str) and media["transcript"] else ""
    side = row.get("side")
    time_ms = row.get("time_ms")
    view = {"id": row["msg_id"], "time": time_ms if isinstance(time_ms, int) and time_ms > 0 else None,
            "side": side, "senderName": row.get("sender_name") or ("我" if side == "self" else ""),
            "kind": row.get("kind") or "", "text": text[:limit], "hasMedia": isinstance(media, dict)}
    if len(text) > limit:
        view["truncated"] = True
    return view


def _safe_tags(raw):
    """Stored memory tags as a list of strings; unreadable or malformed tags read as none."""
    try:
        tags = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [tag for tag in tags if isinstance(tag, str)] if isinstance(tags, list) else []


def _friendly_error(exc):
    if isinstance(exc, AdvisorError):
        return exc.message
    code = getattr(exc, "code", None) or str(exc)
    return {"advisor-timeout": "模型响应超时，可以继续", "timeout": "模型响应超时，可以继续",
            "auth": "模型服务认证失败，请检查模型设置", "rate-limit": "模型服务请求受限，请稍后继续",
            "context-too-long": "模型拒绝了当前分段长度，请调大上下文设置后继续",
            "output-truncated": "模型输出过长被截断，可以继续",
            "advisor-engine-missing": "模型引擎未安装", "engine-missing": "模型引擎未安装",
            "provider-error": "模型服务响应失败，可以继续", "invalid-request": "模型请求格式不正确",
            }.get(code, "模型生成失败，请检查模型设置后继续")


def _public_memory(row, evidence):
    states = [item["status"] for item in evidence]
    return {
        "id": row["id"], "text": row["text"], "tags": json.loads(row["tags_json"]), "status": row["status"],
        "origin": row["origin"], "userCorrected": bool(row["user_corrected"]), "eventTime": row["event_time"],
        "firstMentionMs": row["first_mention_ms"], "relation": row["relation"], "relatedId": row["related_id"],
        "version": row["version"], "createdAtMs": row["created_at_ms"], "updatedAtMs": row["updated_at_ms"],
        "evidenceCount": len(evidence),
        "evidenceStates": sorted(set(states)),
    }


class FriendService:
    def __init__(self, advisor):
        self.advisor = advisor
        self.context = advisor.context
        self.condition = threading.Condition(threading.RLock())
        self._history = {}
        self._verified = {}
        self._profile_snapshots = {}
        self._profile_cache_lock = threading.Lock()
        self._paused = set()
        self._closing = False
        # Set by Core: ``on_opened(account, user)`` after an open-time history refresh and
        # ``on_changed(account, user, version)`` after the user changes human data.
        self.on_opened = None
        self.on_changed = None

    @staticmethod
    def friendly_error(exc):
        return _friendly_error(exc)

    def _scope(self, account, user):
        account, fingerprint = self.advisor._require_scope(account, with_fingerprint=True)
        user = valid_scope_value(user, "user")
        if user.endswith("@chatroom"):
            raise AdvisorError("invalid-request", "群聊暂不提供知己分析")
        with self.condition:
            if self._closing or account in self._paused:
                raise AdvisorError("account-paused", "账号正在清理中")
        database = self.advisor.stores.account(account)
        return account, user, fingerprint, database, FriendStore(database)

    def profile_stamp(self, store, user):
        return self._profile_snapshot(store, user)["stamp"]

    def _profile_snapshot(self, store, user):
        key = (store.database.account, user)
        with self._profile_cache_lock:
            cached = self._profile_snapshots.get(key)
        # Never hold the cache lock while waiting for the account database lock.
        value = friend_snapshot(store, user, self.advisor.source, cached)
        with self._profile_cache_lock:
            self._profile_snapshots[key] = value
            if len(self._profile_snapshots) > 32:
                self._profile_snapshots.pop(next(iter(self._profile_snapshots)), None)
        return value

    def overview(self, account, user):
        account, user, _fingerprint, database, store = self._scope(account, user)
        context = database.context(user)
        counts = {status: 0 for status in ("pending", "confirmed")}
        for row in store._rows("SELECT status,COUNT(*) AS total FROM memories WHERE user=? GROUP BY status", (user,)):
            if row["status"] in counts:
                counts[row["status"]] = row["total"]
        with self.condition:
            history = self._history.get((account, user))
            history_running = history is not None and history.is_alive()
        return {
            "account": account, "user": user, "memoryVersion": store.state(user)["memory_version"],
            "profileStamp": self.profile_stamp(store, user),
            "history": {"state": context["state"] if context else "empty",
                        "readCount": context["read_count"] if context else 0,
                        "totalCount": context["total_count"] if context else None,
                        "error": context["error"] if context else None, "refreshing": history_running},
            "memory": {"counts": counts},
            "goalVersion": store.goal(user)["version"], "accountProfileVersion": store.account_revision(),
        }

    def memories(self, account, user, view="tags", status="active", offset=0, limit=PAGE_SIZE):
        account, user, _fingerprint, _database, store = self._scope(account, user)
        if view not in ("tags", "timeline") or status not in ("active", "pending", "confirmed", "rejected"):
            raise AdvisorError("invalid-request", "列表参数不正确")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise AdvisorError("invalid-request", "分页参数不正确")
        statuses = {"active": ("pending", "confirmed")}.get(status, (status,))
        rows = store.memories(user, statuses)
        if view == "timeline":
            def when(row):
                stamp = row["event_time"] or (time.strftime("%Y-%m-%d", time.localtime(row["first_mention_ms"] / 1000))
                                              if row["first_mention_ms"] else None)
                return (stamp is None, stamp or "", row["created_at_ms"])
            rows.sort(key=when)
        else:
            order = {tag: index for index, tag in enumerate(TAGS)}
            rows.sort(key=lambda row: (row["status"] != "pending", order.get(json.loads(row["tags_json"])[0], 99),
                                       row["created_at_ms"]))
        page = rows[offset:offset + limit]
        evidence = store.evidence([row["id"] for row in page])
        return {"account": account, "user": user, "view": view, "total": len(rows), "offset": offset,
                "memoryVersion": store.state(user)["memory_version"],
                "items": [_public_memory(row, evidence.get(row["id"], [])) for row in page]}

    def memory_detail(self, account, user, memory_id):
        account, user, _fingerprint, _database, store = self._scope(account, user)
        row = store.memory(user, memory_id)
        evidence = store.evidence([memory_id]).get(memory_id, [])
        related = None
        if row["related_id"]:
            try:
                related = _public_memory(store.memory(user, row["related_id"]), [])
            except AdvisorError:
                related = None
        return {"memory": _public_memory(row, evidence), "related": related,
                "evidence": [{"ordinal": item["ordinal"], "role": item["role"], "timeMs": item["time_ms"],
                              "excerpt": item["excerpt"], "status": item["status"]} for item in evidence],
                "versions": [{"version": item["version"], "text": item["text"], "reason": item["reason"],
                              "createdAtMs": item["created_at_ms"]} for item in store.versions(memory_id)]}

    def memory_action(self, account, user, request):
        account, user, _fingerprint, _database, store = self._scope(account, user)
        action = request.get("action")
        expected = request.get("expectedVersion")
        before = self.profile_stamp(store, user)
        if action == "create":
            result = store.create_user_memory(user, request.get("text"), request.get("tags"), request.get("eventTime"))
        elif action == "edit":
            result = store.edit(user, request.get("id"), request.get("text"), request.get("tags"),
                                request.get("eventTime"), expected)
        elif action == "delete":
            result = store.delete(user, request.get("id"), expected)
        elif action in ("confirm", "ignore"):
            result = store.decide(user, request.get("id"), action, expected, request.get("choice"))
        elif action in ("batch-confirm", "batch-ignore"):
            ids = request.get("ids")
            if "scope" in request:
                scope = request["scope"]
                if (request.get("expectedMemoryVersion") != store.state(user)["memory_version"]):
                    raise AdvisorError("request-conflict", "候选列表已有更新，请刷新后再批量处理")
                if scope == {"all": True}:
                    tag = None
                elif isinstance(scope, dict) and set(scope) == {"tag"} and scope["tag"] in TAGS:
                    tag = scope["tag"]
                else:
                    raise AdvisorError("invalid-request", "批量范围不正确")
                ids = [row["id"] for row in store.memories(user, ("pending",))
                       if tag is None or tag in json.loads(row["tags_json"])]
                if not ids:
                    return {"result": {"done": 0, "skipped": 0}, "memoryVersion": store.state(user)["memory_version"]}
            result = store.decide_many(user, ids, action.split("-", 1)[1])
        elif action == "clear":
            trace_factory = getattr(self.advisor, "trace_service", None)
            traces = trace_factory() if callable(trace_factory) else None
            if traces is not None:
                traces.delete_scope(account, contact_id=user)
            result = store.clear(user)
        else:
            raise AdvisorError("invalid-request", "记忆操作不正确")
        if self.profile_stamp(store, user) != before:
            self._changed(account, user, store)
        return {"result": result if not isinstance(result, dict) or "id" not in result else
                _public_memory(result, store.evidence([result["id"]]).get(result["id"], [])),
                "memoryVersion": store.state(user)["memory_version"]}

    def _evidence_status(self, database, user, item, context):
        if context is None:
            return "unready"
        if context["source_id"] != item["source_fp"]:
            return "source-changed"
        row = database._row("SELECT * FROM context_messages WHERE user=? AND msg_id=?", (user, item["msg_id"]))
        if row is None:
            return "missing" if context["state"] == "ready" else "unready"
        recalled = getattr(self.advisor.source, "message_recalled", None)
        if callable(recalled) and recalled(user, item["msg_id"]) is True:
            return "recalled"
        return "ok" if content_hash(row) == item["content_hash"] else "changed"

    def verify_evidence(self, account, user, force=False):
        """Re-check stored evidence against the current projection; flag only mismatches."""
        account, user, _fingerprint, database, store = self._scope(account, user)
        context = database.context(user)
        if context is None or context["state"] != "ready":
            return {"checked": 0, "changed": 0}
        key = (account, user, "evidence")
        stamp = (context["epoch"], context["revision"], store.state(user)["memory_version"])
        if not force and self._verified.get(key) == stamp:
            return {"checked": 0, "changed": 0}
        rows = store.evidence_for_user(user)
        changes = []
        for item in rows:
            status = self._evidence_status(database, user, item, context)
            if status != item["status"]:
                changes.append((item["memory_id"], item["ordinal"], status))
        store.set_evidence_status(user, changes)
        self._verified[key] = (context["epoch"], context["revision"], store.state(user)["memory_version"])
        return {"checked": len(rows), "changed": len(changes)}

    def evidence(self, account, user, memory_id, ordinal):
        account, user, _fingerprint, database, store = self._scope(account, user)
        store.memory(user, memory_id)
        if type(ordinal) is not int or ordinal < 0:
            raise AdvisorError("invalid-request", "证据编号不正确")
        item = next((entry for entry in store.evidence([memory_id]).get(memory_id, []) if entry["ordinal"] == ordinal), None)
        if item is None:
            raise AdvisorError("not-found", "证据不存在")
        context = database.context(user)
        status = self._evidence_status(database, user, item, context)
        if status != item["status"]:
            store.set_evidence_status(user, [(memory_id, ordinal, status)])
        result = {"status": status, "role": item["role"], "timeMs": item["time_ms"], "excerpt": item["excerpt"],
                  "messages": [], "locatable": False}
        if status not in ("ok", "changed"):
            return result
        target = database._row("SELECT seq FROM context_messages WHERE user=? AND msg_id=?", (user, item["msg_id"]))
        rows = database._rows("SELECT * FROM context_messages WHERE user=? AND seq BETWEEN ? AND ? ORDER BY seq",
                              (user, target["seq"] - EVIDENCE_CONTEXT, target["seq"] + EVIDENCE_CONTEXT))
        for row in rows:
            raw = json.loads(row["raw"] or "{}")
            media = raw.get("media") if isinstance(raw.get("media"), dict) else None
            result["messages"].append({
                "id": row["msg_id"], "target": row["msg_id"] == item["msg_id"], "role": message_role(row),
                "speaker": "我" if row["side"] == "self" else (row["sender_name"] or "对方"),
                "timeMs": row["time_ms"] if isinstance(row["time_ms"], int) and row["time_ms"] > 0 else None,
                "text": row["text"] if media is None else "",
                "media": {"kind": media.get("kind")} if media else None,
                "quote": bool(raw.get("inputMeta", {}).get("quote")),
            })
        result["locatable"] = bool(item["cursor"])
        return result

    def locate(self, account, user, memory_id, ordinal):
        """Return the verified chat position for the main window's controlled jump."""
        account, user, _fingerprint, database, store = self._scope(account, user)
        store.memory(user, memory_id)
        item = next((entry for entry in store.evidence([memory_id]).get(memory_id, []) if entry["ordinal"] == ordinal), None)
        if item is None or not item["cursor"]:
            raise AdvisorError("not-found", "这条证据无法在聊天中定位")
        if self._evidence_status(database, user, item, database.context(user)) not in ("ok", "changed"):
            raise AdvisorError("not-found", "原消息当前不可定位")
        return {"account": account, "user": user, "messageId": item["msg_id"], "cursor": item["cursor"]}

    def open(self, account, user):
        """Window open/focus: refresh shared history, then auto-update a built memory."""
        account, user, fingerprint, _database, _store = self._scope(account, user)
        self._start_history(account, user, fingerprint)
        return self.overview(account, user)

    def _check_open(self, account, cancel=None):
        with self.condition:
            if self._closing or account in self._paused or (cancel is not None and cancel.is_set()):
                raise AdvisorError("stopping", "已停止")

    def _start_history(self, account, user, fingerprint):
        key = (account, user)
        with self.condition:
            current = self._history.get(key)
            if current is not None and current.is_alive():
                return
            cancel = threading.Event()
            thread = threading.Thread(target=self._history_worker, args=(account, user, fingerprint, cancel),
                                      name="friend-history", daemon=True)
            thread.cancel = cancel
            self._history[key] = thread
            thread.start()

    def _history_worker(self, account, user, fingerprint, cancel):
        try:
            self.context.prepare(account, user, cancel, fingerprint, refresh=True)
            self.context.wait_ready(account, user, cancel, lambda: self._check_open(account, cancel), fingerprint)
            self.verify_evidence(account, user)
            if self.on_opened is not None:
                self.on_opened(account, user)
        except Exception:
            # The projection keeps its own visible error state.
            return

    def settings(self, account, user):
        account, user, _fingerprint, _database, store = self._scope(account, user)
        goal, profile = store.goal(user), store.account_profile()
        return {"goal": {"text": goal["goal"], "version": goal["version"], "presets": list(GOAL_PRESETS),
                         "default": GOAL_PRESETS[0]},
                "accountProfile": {"background": profile["background"], "style": profile["style"],
                                   "version": profile["version"]}}

    def save_settings(self, account, user, request):
        account, user, _fingerprint, _database, store = self._scope(account, user)
        kind = request.get("kind")
        if kind == "goal":
            store.save_goal(user, request.get("text"), request.get("expectedVersion"))
        elif kind == "account-profile":
            store.save_account_profile(request.get("background"), request.get("style"), request.get("expectedVersion"))
        else:
            raise AdvisorError("invalid-request", "设置类型不正确")
        self._changed(account, user, store)
        return self.settings(account, user)

    def observations(self, account, user):
        account, user, _fingerprint, _database, store = self._scope(account, user)
        return {"items": [{"id": row["id"], "judgement": row["judgement"], "note": row["note"],
                           "dimension": row["dimension"], "version": row["version"],
                           "updatedAtMs": row["updated_at_ms"]} for row in store.observations(user)]}

    def save_observation(self, account, user, request):
        account, user, _fingerprint, _database, store = self._scope(account, user)
        if request.get("action") == "delete":
            store.delete_observation(user, request.get("id"))
        else:
            store.save_observation(user, request.get("id"), request.get("judgement"), request.get("note", ""),
                                   request.get("dimension"), request.get("expectedVersion"))
        self._changed(account, user, store)
        return self.observations(account, user)

    def drafts(self, account, user):
        account, user, _fingerprint, _database, store = self._scope(account, user)
        return store.drafts(user)

    def save_draft(self, account, user, request):
        account, user, _fingerprint, _database, store = self._scope(account, user)
        if request.get("close") is True:
            return store.close_draft(user, request.get("entry"), request.get("generation"))
        return store.save_draft(user, request.get("entry"), request.get("generation"), request.get("seq"),
                                request.get("payload"), request.get("baseVersion"))

    def _changed(self, account, user, store):
        """Tell plugins that this friend's human data changed (memories, goal, profile, observations)."""
        if self.on_changed is not None:
            try:
                self.on_changed(account, user, store.state(user)["memory_version"])
            except Exception:
                pass

    def message_context(self, account, user, msg_id):
        """The cited message with five rows on each side, from the managed projection only (原文查看)."""
        account, user, _fingerprint, database, _store = self._scope(account, user)
        return self._message_context(database, user, msg_id)

    # -- 显式「已保存账号」离线读取作用域（§3.2） ---------------------------------
    # These reads never verify the current WeChat identity and never create data:
    # they report exactly what the saved projection holds, marked cached or live.

    def _live_account(self):
        """当前已核验的微信账号；微信未运行、未登录或读取未就绪时返回 None。"""
        leased = getattr(self.advisor.source, "advisor_identity", None)
        try:
            if callable(leased):
                return str(leased(messages=False)[0])
            return str(self.advisor.source.identity()[0])
        except Exception:
            return None

    def _saved_account(self, account):
        """Offline scope base: one explicit saved account's database, without the live check."""
        account = valid_scope_value(account, "account")
        with self.condition:
            if self._closing or account in self._paused:
                raise AdvisorError("account-paused", "账号正在清理中")
        database = self.advisor.stores.saved_database(account)
        if database is None:
            raise AdvisorError("account-closed", "该账号在本机没有已保存资料")
        return account, database

    def _saved_scope(self, account, user):
        """Offline scope: the saved projection of one explicit saved account, without the live check."""
        account, database = self._saved_account(account)
        user = valid_scope_value(user, "user")
        return account, user, database

    def _saved_metadata(self, account, database, user, *, reader=None):
        """来源元数据：账号、实时/缓存标记、资料时间、覆盖状态和来源版本。

        ``reader`` (an open read-only snapshot) keeps the read off the writer lock.
        """
        context = reader.read_row("SELECT * FROM context WHERE user=?", (user,)) if reader is not None             else database.context(user)
        return {"accountId": account,
                "sourceMode": "live" if self._live_account() == account else "cached",
                "dataTimeMs": context["updated_at_ms"] if context else None,
                "coverage": {"state": context["state"] if context else "empty",
                             "readCount": context["read_count"] if context else 0,
                             "totalCount": context["total_count"] if context else None},
                "sourceVersion": {"epoch": context["epoch"] if context else None,
                                  "revision": context["revision"] if context else 0}}

    def saved_search(self, account, user, *, query, limit=20, offset=0):
        """Bounded text search in one saved contact's projection of one saved account."""
        account, user, database = self._saved_scope(account, user)
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise AdvisorError("invalid-request", "查询条件不正确")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise AdvisorError("invalid-request", "分页参数不正确")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = database._rows(
            "SELECT * FROM context_messages WHERE user=? AND text IS NOT NULL AND text LIKE ? ESCAPE '\\'"
            " ORDER BY seq LIMIT ? OFFSET ?", (user, f"%{escaped}%", limit, offset))
        messages = []
        for row in rows:
            raw = json.loads(row.get("raw") or "{}")
            media = raw.get("media") if isinstance(raw, dict) else None
            text = row.get("text") or ""
            if isinstance(media, dict):
                if media.get("kind") in ("voice", "audio") and media.get("transcript"):
                    text = "[语音转写] " + media["transcript"]
                else:
                    text = ""
            messages.append({"id": row["msg_id"], "seq": row["seq"],
                             "side": row["side"], "sender": row["sender_name"],
                             "timeMs": row["time_ms"], "text": text[:200],
                             "hasMedia": bool(media)})
        return {"account": account, "user": user, "messages": messages,
                "limit": limit, "offset": offset,
                **self._saved_metadata(account, database, user)}

    def saved_message_context(self, account, user, msg_id):
        """The cited message with five rows on each side, from one saved account's projection."""
        account, user, database = self._saved_scope(account, user)
        if not isinstance(msg_id, str) or not msg_id or len(msg_id) > 200:
            raise AdvisorError("invalid-request", "消息编号不正确")
        return {"account": account, "user": user,
                **self._message_context(database, user, msg_id),
                **self._saved_metadata(account, database, user)}

    def saved_memories(self, account, user, *, status="active", offset=0, limit=50):
        """Bounded memory list of one saved contact, with evidence references and versions.

        Read-only: evidence statuses are reported, never rewritten here (阶段 3a 只读).
        ``status`` filters 已确认/待确认/用户观察（用户亲自记录或修正过的记忆）。
        """
        account, user, database = self._saved_scope(account, user)
        if status not in ("active", "pending", "confirmed", "rejected", "user_observed") or \
                type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise AdvisorError("invalid-request", "列表参数不正确")
        statuses = {"active": ("pending", "confirmed"), "pending": ("pending",),
                    "confirmed": ("confirmed",), "rejected": ("rejected",)}.get(status)
        store = FriendStore(database)
        if statuses is None:  # 用户观察：用户亲自记录或修正过的记忆，不限确认状态
            rows = [row for row in store.memories(user) if row["user_corrected"] or row["origin"] == "user"]
        else:
            rows = store.memories(user, statuses)
        page = rows[offset:offset + limit]
        evidence = store.evidence([row["id"] for row in page])
        return {"account": account, "user": user, "status": status, "total": len(rows),
                "offset": offset, "memoryVersion": store.state(user)["memory_version"],
                "items": [_public_memory(row, evidence.get(row["id"], [])) for row in page],
                **self._saved_metadata(account, database, user)}

    def saved_memory_evidence(self, account, user, memory_id, ordinal=None):
        """One memory's evidence with excerpts, versions and its bounded chat window.

        The window is the projection's ±5 rows around each cited message; a stored
        evidence status is re-checked against the projection without touching the
        live source, so an offline read reports ``unready``/``source-changed`` honestly.
        """
        account, user, database = self._saved_scope(account, user)
        store = FriendStore(database)
        row = store.memory(user, memory_id)
        items = store.evidence([memory_id]).get(memory_id, [])
        if ordinal is not None:
            if type(ordinal) is not int or ordinal < 0:
                raise AdvisorError("invalid-request", "证据编号不正确")
            items = [item for item in items if item["ordinal"] == ordinal]
            if not items:
                raise AdvisorError("not-found", "证据不存在")
        context = database.context(user)
        evidence = []
        for item in items:
            evidence.append({"ordinal": item["ordinal"], "role": item["role"], "timeMs": item["time_ms"],
                             "excerpt": item["excerpt"],
                             "status": self._saved_evidence_status(database, user, item, context),
                             "messages": self._message_context(database, user, item["msg_id"])["messages"]})
        return {"account": account, "user": user, "memoryId": memory_id,
                "memory": _public_memory(row, store.evidence([memory_id]).get(memory_id, [])),
                "versions": [{"version": entry["version"], "text": entry["text"], "reason": entry["reason"],
                              "createdAtMs": entry["created_at_ms"]} for entry in store.versions(memory_id)],
                "evidence": evidence, **self._saved_metadata(account, database, user)}

    @staticmethod
    def _saved_evidence_status(database, user, item, context):
        """The offline evidence check: projection facts only, never the live source."""
        if context is None:
            return "unready"
        if context["source_id"] != item["source_fp"]:
            return "source-changed"
        row = database._row("SELECT * FROM context_messages WHERE user=? AND msg_id=?", (user, item["msg_id"]))
        if row is None:
            return "missing" if context["state"] == "ready" else "unready"
        return "ok" if content_hash(row) == item["content_hash"] else "changed"

    def saved_contacts_summary(self, account, subjects):
        """Coverage recomputed over exactly the given contacts of one saved account.

        The per-contact aggregate boundary: counts never include unauthorized
        contacts, so the MCP layer can serve it as ``scope=authorized_contacts``.
        """
        account = valid_scope_value(account, "account")
        if not isinstance(subjects, list) or not subjects or len(subjects) > 256 or \
                any(not isinstance(subject, str) or not subject for subject in subjects):
            raise AdvisorError("invalid-request", "联系人范围不正确")
        with self.condition:
            if self._closing or account in self._paused:
                raise AdvisorError("account-paused", "账号正在清理中")
        database = self.advisor.stores.saved_database(account)
        if database is None:
            return {"accountId": account, "saved": False, "sourceMode": "cached",
                    "dataTimeMs": None, "contacts": 0, "readContacts": 0,
                    "messages": 0, "sourceVersion": None}
        marks = ",".join("?" for _ in subjects)
        rows = database._rows(f"SELECT state, COUNT(*) AS contacts, COALESCE(SUM(read_count),0) AS messages,"
                              f" MAX(updated_at_ms) AS data_time, MAX(revision) AS revision"
                              f" FROM context WHERE user IN ({marks}) GROUP BY state", subjects)
        total = sum(row["contacts"] for row in rows)
        read = sum(row["contacts"] for row in rows if row["state"] == "ready")
        latest = max((row["data_time"] for row in rows if row["data_time"]), default=None)
        revision = max((row["revision"] for row in rows), default=0)
        return {"accountId": account, "saved": True,
                "sourceMode": "live" if self._live_account() == account else "cached",
                "dataTimeMs": latest, "contacts": total, "readContacts": read,
                "messages": sum(row["messages"] for row in rows),
                "sourceVersion": {"revision": revision}}

    def saved_media(self, account, user, message_id, *, frames=1, live_reader=None):
        """Controlled media for one projected message of a saved account.

        Only bounded inline stills are ever returned (each <= 2 MiB, at most four
        frames, image/sticker/video) and only when the current verified WeChat
        account is the requested one; otherwise availability is reported for what
        it is -- no local disk paths, no pretend-live caching.
        """
        account, user, database = self._saved_scope(account, user)
        if not isinstance(message_id, str) or not message_id or len(message_id) > 200:
            raise AdvisorError("invalid-request", "消息编号不正确")
        if type(frames) is not int or not 1 <= frames <= 4:
            raise AdvisorError("invalid-request", "帧数参数不正确")
        row = database._row("SELECT raw FROM context_messages WHERE user=? AND msg_id=?", (user, message_id))
        if row is None:
            raise AdvisorError("not-found", "消息不在该联系人的已保存范围内")
        try:
            raw = json.loads(row.get("raw") or "{}")
        except ValueError:
            raw = {}
        media = raw.get("media") if isinstance(raw, dict) and isinstance(raw.get("media"), dict) else None
        if media is None:
            raise AdvisorError("not-found", "该消息不是媒体消息")
        metadata = {"kind": media["kind"]} if isinstance(media.get("kind"), str) else {}
        for key in ("width", "height", "durationMs"):
            if type(media.get(key)) is int and 0 < media[key] <= 50_000_000:
                metadata[key] = media[key]
        transcript = media.get("transcript")
        if isinstance(transcript, str) and transcript and len(transcript) <= 20000:
            metadata["transcript"] = transcript
        result = {"account": account, "user": user, "messageId": message_id, "frames": frames,
                  "metadata": metadata, "available": False, "reason": "offline", "items": [],
                  **self._saved_metadata(account, database, user)}
        inline_kind = metadata.get("kind") in ("image", "sticker", "video")
        if not inline_kind:
            result["reason"] = "kind-not-inline"
            return result
        if self._live_account() != account or live_reader is None:
            return result
        try:
            payload = live_reader.media(account, user, message_id, frames=frames)
        except Exception:
            result["reason"] = "unavailable"
            return result
        if not isinstance(payload, dict) or payload.get("error") or payload.get("kind") not in ("image", "sticker", "video"):
            result["reason"] = "not-in-current-window" if isinstance(payload, dict) and \
                payload.get("error") == "not-current" else "unavailable"
            return result
        items = []
        for item in payload.get("items") or []:
            body = item.get("data") if isinstance(item, dict) else None
            # 2 MiB of bytes encode to at most ~2.84 MiB of base64; the bound is re-checked here.
            if not isinstance(body, str) or not body or len(body) > ((2 * 1024 * 1024) * 4 // 3) + 4:
                items, result["reason"] = [], "unavailable"
                break
            entry = {"mime": item.get("mime") if isinstance(item.get("mime"), str) else "image/jpeg",
                     "data": body}
            if type(item.get("timeMs")) is int:
                entry["timeMs"] = item["timeMs"]
            items.append(entry)
        if items:
            result.update(available=True, reason=None, items=items,
                          sampling=payload.get("sampling") if isinstance(payload.get("sampling"), str) else None)
        return result

    def saved_account_summary(self, account):
        """Coverage summary of one saved account's whole projection, for zhiji_list_accounts."""
        account = valid_scope_value(account, "account")
        with self.condition:
            if self._closing or account in self._paused:
                raise AdvisorError("account-paused", "账号正在清理中")
        database = self.advisor.stores.saved_database(account)
        if database is None:
            return {"accountId": account, "saved": False, "sourceMode": "cached",
                    "dataTimeMs": None, "contacts": 0, "readContacts": 0,
                    "messages": 0, "sourceVersion": None}
        rows = database._rows("SELECT state, COUNT(*) AS contacts, COALESCE(SUM(read_count),0) AS messages,"
                              " MAX(updated_at_ms) AS data_time, MAX(epoch) AS epoch,"
                              " MAX(revision) AS revision FROM context GROUP BY state")
        total = sum(row["contacts"] for row in rows)
        read = sum(row["contacts"] for row in rows if row["state"] == "ready")
        latest = max((row["data_time"] for row in rows if row["data_time"]), default=None)
        revision = max((row["revision"] for row in rows), default=0)
        return {"accountId": account, "saved": True,
                "sourceMode": "live" if self._live_account() == account else "cached",
                "dataTimeMs": latest, "contacts": total, "readContacts": read,
                "messages": sum(row["messages"] for row in rows),
                "sourceVersion": {"revision": revision}}

    # -- 显式「已保存账号」离线写入作用域（§3.2/§5 阶段 3b） --------------------
    # 人工资料写路径：版本条件在账户数据库事务内复核（FriendStore），授权、请求
    # 幂等与范围判定由 MCP 能力代理层负责。结果只带新版本/状态，不回显正文。

    @staticmethod
    def _memory_state(store, user, memory_id):
        """Compact post-write state; a confirmed candidate may have been merged away."""
        try:
            row = store.memory(user, memory_id)
        except AdvisorError:
            return {"memoryId": memory_id, "merged": True}
        return {"memoryId": row["id"], "version": row["version"], "status": row["status"]}

    def saved_memory_action(self, account, user, request):
        """Version-checked human memory writes on one saved contact (create/edit/delete/confirm)."""
        account, user, database = self._saved_scope(account, user)
        store = FriendStore(database)
        action = request.get("action")
        expected = request.get("expectedVersion")
        if action == "create":
            row = store.create_user_memory(user, request.get("text"), request.get("tags"),
                                           request.get("eventTime"))
            result = {"memoryId": row["id"], "version": row["version"], "status": row["status"]}
        elif action == "edit":
            row = store.edit(user, request.get("id"), request.get("text"), request.get("tags"),
                             request.get("eventTime"), expected)
            result = {"memoryId": row["id"], "version": row["version"], "status": row["status"]}
        elif action == "delete":
            result = {"memoryId": store.delete(user, request.get("id"), expected)["deleted"],
                      "deleted": True}
        elif action == "confirm":
            # 状态机保持原样：只有待确认候选可确认；冲突候选需明确 choice。
            store.decide(user, request.get("id"), "confirm", expected, request.get("choice"))
            result = self._memory_state(store, user, request.get("id"))
        else:
            raise AdvisorError("invalid-request", "记忆操作不正确")
        self._changed(account, user, store)
        return {**result, "memoryVersion": store.state(user)["memory_version"]}

    def saved_contact_goal(self, account, user):
        """The contact goal of one saved contact, with its version for the next save."""
        account, user, database = self._saved_scope(account, user)
        goal = FriendStore(database).goal(user)
        return {"account": account, "user": user,
                "goal": {"text": goal["goal"], "version": goal["version"], "presets": list(GOAL_PRESETS)},
                **self._saved_metadata(account, database, user)}

    def saved_contact_goal_write(self, account, user, text, expected_version):
        account, user, database = self._saved_scope(account, user)
        store = FriendStore(database)
        goal = store.save_goal(user, text, expected_version)
        self._changed(account, user, store)
        return {"account": account, "user": user,
                "goal": {"text": goal["goal"], "version": goal["version"]},
                "memoryVersion": store.state(user)["memory_version"]}

    def saved_self_profile(self, account):
        """账号级自身资料（自身背景与表达风格）；不含任何联系人内容。"""
        account, database = self._saved_account(account)
        profile = FriendStore(database).account_profile()
        return {"accountId": account, "background": profile["background"], "style": profile["style"],
                "version": profile["version"],
                "sourceMode": "live" if self._live_account() == account else "cached"}

    def saved_self_profile_write(self, account, background, style, expected_version):
        """Account-level self profile save. 版本条件在账户数据库事务内复核。

        账号级变更没有单一联系人可通告，插件按现有约定在下次使用时重读，这里不广播。
        """
        account, database = self._saved_account(account)
        profile = FriendStore(database).save_account_profile(background, style, expected_version)
        return {"accountId": account, "background": profile["background"], "style": profile["style"],
                "version": profile["version"],
                "sourceMode": "live" if self._live_account() == account else "cached"}

    def saved_observations(self, account, user, *, offset=0, limit=50):
        """Bounded page of one saved contact's manual observations, each with its version."""
        account, user, database = self._saved_scope(account, user)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise AdvisorError("invalid-request", "分页参数不正确")
        rows = FriendStore(database).observations(user)
        return {"account": account, "user": user, "total": len(rows), "offset": offset,
                "items": [{"id": row["id"], "judgement": row["judgement"], "note": row["note"],
                           "dimension": row["dimension"], "version": row["version"],
                           "updatedAtMs": row["updated_at_ms"]} for row in rows[offset:offset + limit]],
                **self._saved_metadata(account, database, user)}

    def saved_observation_write(self, account, user, request):
        """Create/update/delete one manual observation; updates and deletes check the version."""
        account, user, database = self._saved_scope(account, user)
        store = FriendStore(database)
        if request.get("action") == "delete":
            result = {"observationId": store.delete_observation(user, request.get("id"),
                                                                request.get("expectedVersion"))["deleted"],
                      "deleted": True}
        else:
            row = store.save_observation(user, request.get("id"), request.get("judgement"),
                                         request.get("note", ""), request.get("dimension"),
                                         request.get("expectedVersion"))
            result = {"observationId": row["id"], "version": row["version"]}
        self._changed(account, user, store)
        return {**result, "account": account, "user": user}

    # -- MCP 读取入口（外部 Agent 衔接 G1–G3）：找人、最近会话、最近消息、跨联系人搜索、简报 --
    # 全部只读已保存投影与人工资料，不核验当前微信身份、不读微信、不调模型、不建任务。
    # 范围（哪些账号、哪些联系人）由 MCP 能力代理按授权传入，这里只按给定范围取数。
    # 所有查询走只读 WAL 快照（每条语句 5 秒期限，超时为 data-busy），绝不持有
    # AccountDatabase.lock，因此不会挡住消息写入，也不会被写入挡住。

    # 会话排序键：最后一条已保存消息的时间（未知记 0）降序，再按联系人 id 升序。
    _CONVERSATION_BASE = (
        "SELECT c.user AS user, c.state AS state, c.read_count AS read_count,"
        " c.total_count AS total_count, c.updated_at_ms AS updated_at_ms,"
        " (SELECT MAX(seq) FROM context_messages WHERE user=c.user) AS last_seq"
        " FROM context c")
    _OTHER_NAME = (
        "CASE WHEN {u} LIKE '%@chatroom' THEN NULL ELSE"
        " (SELECT n.sender_name FROM context_messages n WHERE n.user={u} AND n.side='other'"
        " AND n.group_chat=0 AND n.sender_name IS NOT NULL AND n.sender_name!=''"
        " ORDER BY n.seq DESC LIMIT 1) END")

    @staticmethod
    def _users_filter(column, wanted):
        """``column IN (...)`` for any number of ids: one JSON parameter, no SQL variable limit."""
        return column + " IN (SELECT value FROM json_each(?))", (json.dumps(wanted, ensure_ascii=False),)

    @staticmethod
    def _keyset(after, time_expr, user_col, seq_col=None):
        """SQL bound for "strictly after ``after``" in (time desc, user asc[, seq desc]) order.

        ``after`` is ``{"timeMs": t}`` (older than t only), ``{"timeMs": t, "sameTime": True}``
        (t and older) or ``{"timeMs": t, "user": u[, "seq": s]}`` (exact position).
        """
        if after is None:
            return "", ()
        if not isinstance(after, dict) or type(after.get("timeMs")) is not int:
            raise AdvisorError("invalid-request", "分页位置不正确")
        t = after["timeMs"]
        if after.get("user") is None:
            return (f" AND {time_expr}<=?" if after.get("sameTime") else f" AND {time_expr}<?"), (t,)
        user = after["user"]
        if not isinstance(user, str):
            raise AdvisorError("invalid-request", "分页位置不正确")
        if seq_col is None:
            return f" AND ({time_expr}<? OR ({time_expr}=? AND {user_col}>?))", (t, t, user)
        seq = after.get("seq")
        if type(seq) is not int:
            raise AdvisorError("invalid-request", "分页位置不正确")
        return (f" AND ({time_expr}<? OR ({time_expr}=? AND ({user_col}>? OR ({user_col}=? AND {seq_col}<?))))",
                (t, t, user, user, seq))

    def _conversation_item(self, row):
        last = _message_view(row, PREVIEW_TEXT_MAX) if row.get("msg_id") else None
        return {"user": row["user"], "isGroup": row["user"].endswith("@chatroom") or bool(row.get("group_chat")),
                "state": row["state"], "readCount": row["read_count"], "totalCount": row["total_count"],
                "updatedAtMs": row["updated_at_ms"], "otherName": row.get("other_name") or None,
                "lastSeq": row.get("seq"), "last": last, "sortTime": row.get("k") or 0}

    def saved_conversations(self, account, users=None, *, limit=None, after=None, match=None):
        """Saved conversations of one saved account, newest first, each with its latest saved message.

        Order: last saved message time (0 when unknown) descending, then contact id
        ascending -- a complete order, so a page boundary never reorders. ``users`` (an
        iterable of ids, any size) restricts the listing; ``None`` means all. ``limit``
        caps the rows (``total`` still counts every conversation in ``users``);
        ``after`` is a keyset position (see :meth:`_keyset`, without ``seq``).
        ``match = {"needle": casefolded text, "users": [ids]}`` keeps only conversations
        whose id or latest other-side name contains the needle (case-folded) or whose
        id is listed (the caller's own cached-name matches).

        The display name is the other side's latest sender name in a private chat
        (groups carry member names there, so they get none); the caller may prefer a
        cached session name.
        """
        account, database = self._saved_account(account)
        wanted = None if users is None else sorted(set(users))
        if wanted is not None and not wanted:
            return {"account": account, "conversations": [], "total": 0}
        where, params = "", ()
        if wanted is not None:
            clause, params = self._users_filter("c.user", wanted)
            where = " WHERE " + clause
        bound, bound_params = self._keyset(after, "k", "user")
        inner = ("SELECT i.*, COALESCE((SELECT CASE WHEN time_ms>0 THEN time_ms END FROM context_messages"
                 " WHERE user=i.user AND seq=i.last_seq), 0) AS k FROM (" + self._CONVERSATION_BASE + where +
                 ") i")
        inner_params = params
        if match is not None:
            needle = match.get("needle")
            named = sorted(set(match.get("users") or ()))
            if not isinstance(needle, str) or not needle:
                raise AdvisorError("invalid-request", "查询条件不正确")
            inner = ("SELECT * FROM (SELECT x.*, " + self._OTHER_NAME.format(u="x.user") + " AS other_name FROM ("
                     + inner + ") x) WHERE instr(kyf_casefold(user), ?)>0 OR instr(kyf_casefold(other_name), ?)>0"
                     " OR user IN (SELECT value FROM json_each(?))")
            inner_params = (*params, needle, needle, json.dumps(named, ensure_ascii=False))
        page = "SELECT * FROM (" + inner + ") WHERE 1=1" + bound + " ORDER BY k DESC, user ASC"
        page_params = (*inner_params, *bound_params)
        if limit is not None:
            if type(limit) is not int or limit < 1:
                raise AdvisorError("invalid-request", "分页参数不正确")
            page += " LIMIT ?"
            page_params = (*page_params, limit)
        other = "p.other_name" if match is not None else self._OTHER_NAME.format(u="p.user")
        sql = ("SELECT p.user AS user, p.state AS state, p.read_count AS read_count, p.total_count AS total_count,"
               " p.updated_at_ms AS updated_at_ms, p.k AS k, m.seq AS seq, m.msg_id AS msg_id, m.side AS side,"
               " m.sender_name AS sender_name, m.kind AS kind, m.text AS text, m.raw AS raw, m.time_ms AS time_ms,"
               " m.group_chat AS group_chat, " + other + " AS other_name"
               " FROM (" + page + ") p LEFT JOIN context_messages m ON m.user=p.user AND m.seq=p.last_seq"
               " ORDER BY p.k DESC, p.user ASC")
        with self._snapshot(database) as reader:
            rows = reader.read_rows(sql, page_params)
            if limit is None:
                total = len(rows) if after is None else None
            else:
                total = reader.read_rows("SELECT COUNT(*) AS n FROM context c" + where, params)[0]["n"]
        return {"account": account, "total": total, "conversations": [self._conversation_item(row) for row in rows]}

    def saved_recent_messages(self, account, user, *, limit=30, before_seq=None, before_time_ms=None):
        """One newest-first page of a saved contact's projection, returned in chronological order."""
        account, user, database = self._saved_scope(account, user)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AdvisorError("invalid-request", "分页参数不正确")
        clauses, params = ["user=?"], [user]
        if before_seq is not None:
            clauses.append("seq<?")
            params.append(before_seq)
        if before_time_ms is not None:
            clauses.append("time_ms>0 AND time_ms<?")
            params.append(before_time_ms)
        where = " AND ".join(clauses)
        with self._snapshot(database) as reader:
            rows = reader.read_rows("SELECT * FROM context_messages WHERE " + where + " ORDER BY seq DESC LIMIT ?",
                                    (*params, limit))
            rows.reverse()
            more = False
            if rows:
                more = reader.read_row("SELECT 1 AS found FROM context_messages WHERE " + where +
                                       " AND seq<? LIMIT 1", (*params, rows[0]["seq"])) is not None
            metadata = self._saved_metadata(account, database, user, reader=reader)
        return {"account": account, "user": user,
                "messages": [_message_view(row, MESSAGE_TEXT_MAX) for row in rows],
                "oldestSeq": rows[0]["seq"] if rows else None, "hasMore": more, **metadata}

    def saved_search_all(self, account, *, query, users=None, limit=20, scan_max=200_000, after=None):
        """Bounded text search across the given contacts (``None`` = all, any number) of one saved account.

        Only the ``scan_max`` most recently written rows (of those contacts) are
        scanned; ``scanLimited`` says whether older rows were left out. Hits come in
        one complete order -- message time (0 when unknown) descending, contact id
        ascending, seq descending -- applied before ``limit``, so the caller can merge
        accounts and page without ties reordering. ``after`` restricts to hits
        strictly after a position in that order (see :meth:`_keyset`). Each hit
        carries ``sortTime`` and ``seq`` for that order.
        """
        account, database = self._saved_account(account)
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise AdvisorError("invalid-request", "查询条件不正确")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise AdvisorError("invalid-request", "分页参数不正确")
        wanted = None if users is None else sorted(set(users))
        if wanted is not None and not wanted:
            return {"account": account, "hits": [], "scanLimited": False}
        where, params = "", ()
        if wanted is not None:
            clause, params = self._users_filter("user", wanted)
            where = " WHERE " + clause
        bound, bound_params = self._keyset(after, "k", "user", "seq")
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        with self._snapshot(database) as reader:
            rows = reader.read_rows(
                "SELECT * FROM (SELECT *, CASE WHEN time_ms>0 THEN time_ms ELSE 0 END AS k FROM"
                " (SELECT rowid AS rid, * FROM context_messages" + where + " ORDER BY rowid DESC LIMIT ?)"
                " WHERE text IS NOT NULL AND text LIKE ? ESCAPE '\\') WHERE 1=1" + bound +
                " ORDER BY k DESC, user ASC, seq DESC LIMIT ?",
                (*params, scan_max, f"%{escaped}%", *bound_params, limit))
            limited = reader.read_rows(
                "SELECT 1 AS found FROM context_messages" + where + " ORDER BY rowid DESC LIMIT 1 OFFSET ?",
                (*params, scan_max))
        return {"account": account, "scanLimited": bool(limited),
                "hits": [{"user": row["user"], "seq": row["seq"], "sortTime": row["k"],
                          "message": _message_view(row, SEARCH_TEXT_MAX)} for row in rows]}

    def saved_contact_stats(self, account, user, *, now_ms=None, months=6):
        """Deterministic message statistics of one saved contact (counts only, no content)."""
        account, user, database = self._saved_scope(account, user)
        now = now_ms if type(now_ms) is int else int(time.time() * 1000)
        since = now - 30 * 86_400_000
        start = time.localtime(now / 1000)
        index = start.tm_year * 12 + start.tm_mon - 1 - (months - 1)
        month_start = int(time.mktime((index // 12, index % 12 + 1, 1, 0, 0, 0, 0, 0, -1)) * 1000)
        with self._snapshot(database) as reader:
            rows = reader.read_rows(
                "SELECT side, COUNT(*) AS total, MIN(CASE WHEN time_ms>0 THEN time_ms END) AS first_ms,"
                " MAX(CASE WHEN time_ms>0 THEN time_ms END) AS last_ms,"
                " SUM(CASE WHEN time_ms>=? THEN 1 ELSE 0 END) AS recent"
                " FROM context_messages WHERE user=? GROUP BY side", (since, user))
            month_rows = reader.read_rows(
                "SELECT strftime('%Y-%m', time_ms/1000, 'unixepoch', 'localtime') AS month, side, COUNT(*) AS n"
                " FROM context_messages WHERE user=? AND time_ms>=? GROUP BY month, side", (user, month_start))
            metadata = self._saved_metadata(account, database, user, reader=reader)
        by_side = {row["side"] or "unknown": row["total"] for row in rows}
        recent = {row["side"] or "unknown": row["recent"] or 0 for row in rows}
        firsts = [row["first_ms"] for row in rows if row["first_ms"]]
        lasts = [row["last_ms"] for row in rows if row["last_ms"]]
        monthly = {}
        for row in month_rows:
            entry = monthly.setdefault(row["month"], {"month": row["month"], "self": 0, "other": 0})
            if row["side"] in ("self", "other"):
                entry[row["side"]] = row["n"]
        return {"account": account, "user": user, "totalMessages": sum(by_side.values()), "bySide": by_side,
                "firstTimeMs": min(firsts) if firsts else None, "lastTimeMs": max(lasts) if lasts else None,
                "last30Days": {"self": recent.get("self", 0), "other": recent.get("other", 0),
                               "total": sum(recent.values())},
                "months": [monthly[key] for key in sorted(monthly)], **metadata}

    def saved_contact_records(self, account, user, *, memory_limit=20, observation_limit=10):
        """The human records of one saved contact: memories (confirmed first), goal, observations.

        Counts and the bounded pages come from SQL on a read-only snapshot; a memory
        whose stored tags are unreadable is still listed, with no tags.
        """
        account, user, database = self._saved_scope(account, user)
        with self._snapshot(database) as reader:
            counts = {row["status"]: row["n"] for row in reader.read_rows(
                "SELECT status, COUNT(*) AS n FROM memories WHERE user=? AND status IN ('confirmed','pending')"
                " GROUP BY status", (user,))}
            memory_rows = reader.read_rows(
                "SELECT * FROM memories WHERE user=? AND status IN ('confirmed','pending')"
                " ORDER BY CASE status WHEN 'confirmed' THEN 0 ELSE 1 END, updated_at_ms DESC, created_at_ms, id"
                " LIMIT ?", (user, memory_limit))
            goal = reader.read_row("SELECT * FROM friend_settings WHERE user=?", (user,)) or {
                "goal": "", "version": 0, "updated_at_ms": 0}
            observation_total = reader.read_row(
                "SELECT COUNT(*) AS n FROM friend_observations WHERE user=?", (user,))["n"]
            observations = reader.read_rows(
                "SELECT * FROM friend_observations WHERE user=? ORDER BY updated_at_ms DESC LIMIT ?",
                (user, observation_limit))
        memories = [{"id": row["id"], "text": row["text"], "tags": _safe_tags(row.get("tags_json")),
                     "status": row["status"], "origin": row["origin"], "eventTime": row["event_time"],
                     "updatedAtMs": row["updated_at_ms"]} for row in memory_rows]
        return {"account": account, "user": user,
                "memoryCounts": {"confirmed": counts.get("confirmed", 0), "pending": counts.get("pending", 0)},
                "memories": memories,
                "goal": {"text": goal["goal"], "version": goal["version"], "updatedAtMs": goal["updated_at_ms"] or None},
                "observationTotal": observation_total,
                "observations": [{"id": row["id"], "judgement": row["judgement"], "note": row["note"],
                                  "dimension": row["dimension"], "updatedAtMs": row["updated_at_ms"]}
                                 for row in observations]}

    @staticmethod
    @contextmanager
    def _snapshot(database):
        """A read-only WAL snapshot (5 s per statement); an overrun or lock is a stable busy error."""
        try:
            with database.read_snapshot() as reader:
                yield reader
        except sqlite3.OperationalError:
            raise AdvisorError("data-busy", "资料查询超时，请缩小范围后重试") from None

    def _message_context(self, database, user, msg_id):
        """Shared context-window read; the caller has already established the scope."""
        if not isinstance(msg_id, str) or not msg_id or len(msg_id) > 200:
            raise AdvisorError("invalid-request", "消息编号不正确")
        target = database._row("SELECT seq FROM context_messages WHERE user=? AND msg_id=?", (user, msg_id))
        if target is None:
            context = database.context(user)
            return {"status": "missing" if context and context["state"] == "ready" else "unready", "messages": []}
        rows = database._rows("SELECT * FROM context_messages WHERE user=? AND seq BETWEEN ? AND ? ORDER BY seq",
                              (user, target["seq"] - EVIDENCE_CONTEXT, target["seq"] + EVIDENCE_CONTEXT))
        def text(row):
            raw = json.loads(row.get("raw") or "{}")
            media = raw.get("media") if isinstance(raw, dict) else None
            if isinstance(media, dict):
                if media.get("kind") in ("voice", "audio") and media.get("transcript"):
                    return "[语音转写] " + media["transcript"]
                return None
            return row.get("text") or None
        return {"status": "ok", "messages": [{
            "id": row["msg_id"], "target": row["msg_id"] == msg_id,
            "speaker": "我" if row["side"] == "self" else (row["sender_name"] or "对方"),
            "timeMs": row["time_ms"] if isinstance(row["time_ms"], int) and row["time_ms"] > 0 else None,
            "text": text(row) or "[媒体或非文字消息]"} for row in rows]}

    def pause_account(self, account):
        with self.condition:
            self._paused.add(account)
            histories = [thread for (owner, _user), thread in self._history.items() if owner == account]
            for thread in histories:
                thread.cancel.set()
        deadline = time.monotonic() + JOIN_SECONDS
        for worker in histories:
            if worker is not None:
                worker.join(max(0.0, deadline - time.monotonic()))
                if worker.is_alive():
                    raise RuntimeError("friend history worker did not finish")
        for key in [key for key in self._verified if key[0] == account]:
            self._verified.pop(key, None)
        with self._profile_cache_lock:
            for key in [key for key in self._profile_snapshots if key[0] == account]:
                self._profile_snapshots.pop(key, None)

    def resume_accounts(self, accounts):
        with self.condition:
            self._paused.difference_update(accounts)

    def close(self):
        with self._profile_cache_lock:
            self._profile_snapshots.clear()
        with self.condition:
            self._closing = True
            histories = list(self._history.values())
            for thread in histories:
                thread.cancel.set()
        deadline = time.monotonic() + JOIN_SECONDS
        for worker in histories:
            if worker is not None:
                worker.join(max(0.0, deadline - time.monotonic()))
        with self._profile_cache_lock:
            self._profile_snapshots.clear()
