"""Authenticated, bounded, fail-open model diagnostics for this local runtime.

Copyright 2026 Know Your Friends contributors. Licensed under Apache-2.0.
The producer already sanitizes at the transport boundary; this service applies a
second structural defense. Raw model credentials must never be supplied here.
"""
from __future__ import annotations

import hashlib
import json
import math
import queue
import re
import secrets
import threading
import time
import uuid
from collections import deque
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from model_traces_store import BODY_KINDS, TraceStore, atomic_json

ENVELOPE_LIMIT = 65536
QUEUE_LIMIT = 8 * 1024 * 1024
PROCESS_GRANT = 6 * 1024 * 1024
PROCESS_GRANT_LIMIT = 8
MAX_PRODUCERS = 512
TOMBSTONE_LIMIT = 20000
HEADER_ALLOWLIST = frozenset(("content-type", "accept", "anthropic-version", "x-request-id"))
SENSITIVE_KEY = re.compile(r"authorization|cookie|api[-_]?key|access[-_]?token|auth[-_]?token|producerid|password|secret|signature|credential|control[-_]?token", re.I)
MAX_METADATA = 16384


class TraceError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def clean_url(value):
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port:
            host += ":" + str(parsed.port)
        query = [(key, "[REDACTED]" if SENSITIVE_KEY.search(key) or key.lower() in ("key", "sig", "token") else val)
                 for key, val in parse_qsl(parsed.query, keep_blank_values=True)]
        return urlunsplit((parsed.scheme, host, parsed.path, urlencode(query), ""))
    except (ValueError, TypeError):
        return "[URL omitted]"


def sanitize(value, key="", depth=0):
    if depth > 20:
        return "[nested content omitted]"
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        if key.lower() in ("headers", "requestheaders", "responseheaders"):
            return {name.lower(): sanitize(item, name, depth + 1) for name, item in value.items() if name.lower() in HEADER_ALLOWLIST}
        return {str(name)[:256]: sanitize(item, str(name), depth + 1) for name, item in list(value.items())[:256]}
    if isinstance(value, list):
        return [sanitize(item, "", depth + 1) for item in value[:2048]]
    if isinstance(value, str):
        if key.lower() in ("url", "endpoint", "baseurl", "serviceurl"):
            return clean_url(value)
        if value.startswith("data:") and ";base64," in value[:150]:
            return "[media binary omitted]"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[unsupported content omitted]"


def timestamp(value):
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value / 1000 if value > 1e12 else value
    return time.time()


def display_status(meta):
    if meta.get("applicationStatus") in ("cancelled", "canceled", "stopped"):
        return "cancelled"
    if meta.get("transportStatus") in ("failed", "interrupted") or meta.get("applicationStatus") in ("failed", "parse-failed", "validation-failed", "error"):
        return "error"
    if meta.get("collectionStatus") in ("stopped", "interrupted"):
        return "unknown"
    if meta.get("collectionStatus") == "recording":
        return "running"
    if meta.get("adoption") in ("discarded", "not-adopted", "not_adopted"):
        return "not_adopted"
    if meta.get("transportStatus") in ("completed", "success", "not-sent"):
        if meta.get("applicationStatus") in ("pending", "processing") or meta.get("adoption") == "pending":
            return "running"
        if meta.get("applicationStatus") in ("success", "completed") and meta.get("adoption") in ("adopted", "not-applicable", "not_applicable"):
            return "completed"
    return "unknown"


class ModelTraceService:
    def __init__(self, root, current_account=None, *, current_account_version=None, store_factory=TraceStore, ack_timeout=5.0):
        self.root, self.current_account = root, current_account or (lambda: None)
        self.current_account_version = current_account_version or (lambda: 0)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self._store_factory, self._store = store_factory, None
        self._enabled, self._collecting, self._loaded = False, False, False
        self._status, self._version, self._revision = "off", 1, 0
        self._delete_version = 0
        self._capture_delete_version = 0
        self._peak_known_records = 0
        self._retention = 7
        self._save_error, self._pause_reason = None, None
        self._pending_disable = False
        self._pause_history = deque(maxlen=20)
        self._producers, self._sessions, self._clients = {}, {}, {}
        self._process_grants, self._peak_grants = set(), 0
        self._process_losses = {}
        self._listeners, self._changes = [], deque(maxlen=2000)
        self._account, self._account_version = object(), 0
        self._queue = queue.Queue(maxsize=2048)
        self._queued_bytes, self._pending_bytes, self._peak_bytes = 0, 0, 0
        self._thread, self._closing = None, False
        self._ack_timeout, self._last_probe, self._retry_delay = ack_timeout, 0, 1
        self._read_blocked, self._lost_events = False, 0
        self.transport_url = None

    def _open(self):
        if self._loaded:
            return
        try:
            self._store = self._store_factory(self.root)
            self._enabled = self._store.settings["enabled"]
            self._retention = self._store.settings["retentionDays"]
            self._delete_version = self._store.delete_version
            self._capture_delete_version = self._delete_version
            try:
                history = json.loads((self._store.root / "health.json").read_text(encoding="utf-8"))
                self._pause_history.extend(history.get("pauseHistory", [])[-20:])
            except (OSError, ValueError, TypeError):
                pass
            if self._store.recovery_gap:
                self._pause_history.append(self._store.recovery_gap)
            self._collecting = self._enabled
            self._status = "recording" if self._enabled else "off"
            self._loaded = True
            self._store.reclaim()
            self._delete_version = self._store.delete_version
        except Exception as exc:
            # Persisted user preference may be unreadable; do not invent success.
            self._loaded = True
            self._collecting = False
            self._status = "paused"
            self._pause_reason = "记录存储不可用（" + type(exc).__name__ + "）"
            self._pause_history.append({"startedAt": time.time(), "finishedAt": None,
                                        "reason": self._pause_reason, "lostEvents": None})
        if self._thread is None:
            self._thread = threading.Thread(target=self._worker, name="model-trace-storage", daemon=True)
            self._thread.start()

    def _state(self):
        store = self._store
        count, usage = 0, 0
        try:
            if store and not self._read_blocked:
                count, usage = store.count(), store.usage()
        except Exception:
            pass
        return {"enabled": self._enabled, "collecting": self._collecting, "status": self._status,
                "retentionDays": self._retention, "collectionVersion": self._version,
                "deleteVersion": self._delete_version, "revision": self._revision,
                "captureDeleteVersion": self._capture_delete_version,
                "recordCount": count, "storageBytes": usage, "pauseReason": self._pause_reason,
                "pauseHistory": list(self._pause_history), "saveError": self._save_error,
                "lostEvents": self._lost_events, "bufferBytes": self._queued_bytes + self._pending_bytes,
                "peakBufferBytes": self._peak_bytes, "bufferLimitBytes": QUEUE_LIMIT,
                "producerBudgetBytes": len(self._process_grants) * PROCESS_GRANT,
                "peakProducerBudgetBytes": self._peak_grants,
                "globalBufferLimitBytes": 64 * 1024 * 1024,
                "knownRecordIds": self._known_record_count(), "knownRecordIdLimit": TOMBSTONE_LIMIT,
                "peakKnownRecordIds": self._peak_known_records,
                "pendingComponents": sum(1 for item in self._producers.values() if self._live(item) and item.get("ack") != self._version)}

    def snapshot(self):
        with self.lock:
            self._open()
            return self._state()

    def capture_available(self, context):
        """Check local producers without inspecting bodies or marking a process live."""
        with self.lock:
            producer = self._producers.get(context.get("producerId"))
            return bool(self._collecting and producer and
                        producer["scope"]["taskId"] == context.get("taskId"))

    def add_state_listener(self, callback):
        with self.lock:
            self._listeners.append(callback)

    def _notify(self):
        self._revision += 1
        self.condition.notify_all()
        state = self._state()
        for callback in self._listeners:
            try:
                callback(state)
            except Exception:
                pass

    def _live(self, item):
        return not item.get("retireAt") and bool(item.get("seen"))

    def _stop_records(self, reason, status="stopped"):
        if not self._store:
            return
        for row in self._store.db.execute("SELECT id,metadata FROM records WHERE active=1").fetchall():
            meta = json.loads(row["metadata"])
            meta.update(collectionStatus=status, complete=False, status="unknown", incompleteReason=reason)
            self._store.update(row["id"], meta, active=False)
        self._store.db.commit()

    def update_settings(self, request):
        if set(request) - {"enabled", "retentionDays"} or not request:
            raise TraceError("invalid-settings", "开发者模式设置不正确")
        if "enabled" in request and type(request["enabled"]) is not bool:
            raise TraceError("invalid-settings", "采集开关须为布尔值")
        if "retentionDays" in request and request["retentionDays"] not in (1, 7, 30):
            raise TraceError("invalid-settings", "保留期限仅支持 1、7 或 30 天")
        with self.condition:
            self._open()
            if not self._store:
                if request.get("enabled") is False:
                    # A user can always stop this run, even when the preference
                    # file is unavailable. Persist that decision when it recovers.
                    self._pending_disable = True
                    self._enabled, self._collecting, self._status = False, False, "off"
                    self._version += 1
                    self._discard_pending()
                    self._save_error = "本次已停止，关闭设置未保存，重启可能恢复原设置"
                    self._notify()
                    return self._state()
                raise TraceError("trace-unavailable", "记录存储不可用", 503)
            enabled = request.get("enabled", self._enabled)
            retention = request.get("retentionDays", self._retention)
            changed = enabled != self._enabled or (enabled and not self._collecting and self._status != "paused")
            self._save_error = None
            if changed or (not enabled and "enabled" in request):
                self._version += 1
                operation = self._version
                self._collecting = False
                self._status = "starting" if enabled else "stopping"
                self._enabled = False if not enabled else self._enabled
                self._discard_pending()
                self._stop_records("采集已停止，记录不完整")
                self._notify()
                deadline = time.monotonic() + self._ack_timeout
                while operation == self._version and any(self._live(item) and item.get("ack") != operation for item in self._producers.values()):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self.condition.wait(min(remaining, 0.1))
                if operation != self._version:
                    return self._state()
                unready = any(self._live(item) and item.get("ack") != operation for item in self._producers.values())
                if enabled and unready:
                    self._enabled, self._collecting, self._status = False, False, "off"
                    self._save_error = "开启失败：部分采集进程未确认，可重试"
                    self._notify()
                    return self._state()
                if not enabled and unready:
                    self._save_error = "已停止保存，部分采集进程未确认"
            try:
                self._store.save_settings({"enabled": enabled, "retentionDays": retention})
            except OSError:
                if not enabled:
                    self._enabled, self._collecting, self._status = False, False, "off"
                    self._save_error = "本次已停止，关闭设置未保存，重启可能恢复原设置"
                elif changed:
                    self._enabled, self._collecting, self._status = False, False, "off"
                    self._save_error = "开启设置未保存，采集保持关闭"
                else:
                    self._save_error = "保留期限保存失败，沿用原设置"
                self._notify()
                return self._state()
            self._enabled, self._retention = enabled, retention
            self._collecting = enabled and self._pause_reason is None
            self._status = "recording" if self._collecting else "paused" if enabled else "off"
            self._prune()
            self._notify()
            return self._state()

    def register_execution(self, scope):
        with self.lock:
            self._open()
            self._retire()
            if len(self._producers) >= MAX_PRODUCERS:
                raise TraceError("trace-capacity", "采集执行数量达到上限", 503)
            scope = {key: value for key, value in sanitize(scope).items() if key in (
                "accountId", "contactId", "sessionId", "runId", "source", "trigger", "businessJobId",
                "resumeOfTaskId", "retryOfTaskId", "stepId", "parentStepId", "templateVersion", "contextVersion")}
            if scope.get("businessJobId") and not scope.get("resumeOfTaskId") and not scope.get("retryOfTaskId") and self._store:
                previous = self._store.db.execute("SELECT task FROM records WHERE account=? "
                    "AND json_extract(metadata,'$.businessJobId')=? ORDER BY started DESC LIMIT 1",
                    (scope.get("accountId") or "", scope["businessJobId"])).fetchone()
                if previous:
                    scope["resumeOfTaskId"] = previous["task"]
            scope.update(taskId=uuid.uuid4().hex, executionId=uuid.uuid4().hex)
            producer = secrets.token_urlsafe(32)
            self._producers[producer] = {"scope": scope, "seen": 0, "ack": self._version, "records": set(), "retireAt": None}
            return {**scope, "producerId": producer, "collectionVersion": self._version,
                    "deleteVersion": self._capture_delete_version, "enabled": self._enabled,
                    "collecting": self._collecting, "captureEnabled": self._collecting,
                    "endpoint": self.transport_url, "revision": self._revision,
                    "retentionCutoffAt": self._retention_cutoff() * 1000}

    def _producer(self, token):
        item = self._producers.get(token)
        if not item or item.get("retireAt", float("inf")) is not None and item["retireAt"] < time.monotonic():
            raise TraceError("trace-producer-forbidden", "采集身份无效", 403)
        return item

    def producer_state(self, token):
        with self.lock:
            item = self._producer(token)
            item["seen"] = time.monotonic()
            return {"enabled": self._enabled, "collecting": self._collecting and item.get("transportAllowed", True),
                    "collectionVersion": self._version, "deleteVersion": self._capture_delete_version,
                    "viewerDeleteVersion": self._delete_version, "retentionCutoffAt": self._retention_cutoff() * 1000,
                    "status": self._status, "revision": self._revision, "bufferBudgetBytes": PROCESS_GRANT}

    def bind_transport(self, token, process_id):
        """Called only by the Python process owner, never by a remote producer."""
        with self.lock:
            item = self._producer(token)
            if not isinstance(process_id, int) or process_id <= 0:
                raise TraceError("invalid-producer", "采集进程不正确")
            item["processId"] = process_id
            if process_id not in self._process_grants and len(self._process_grants) >= PROCESS_GRANT_LIMIT:
                item["transportAllowed"] = False
                self._pause("诊断进程缓冲预算已用满")
            else:
                self._process_grants.add(process_id)
                item["transportAllowed"] = True
                self._peak_grants = max(self._peak_grants, len(self._process_grants) * PROCESS_GRANT)
            return {"bufferBudgetBytes": PROCESS_GRANT if item["transportAllowed"] else 0,
                    "collecting": self._collecting and item["transportAllowed"]}

    def ack(self, token, version, lost_events=0):
        with self.condition:
            item = self._producer(token)
            item["seen"] = time.monotonic()
            if isinstance(lost_events, int) and 0 < lost_events <= 1000000000:
                loss_key = item.get("processId", token)
                previous = self._process_losses.get(loss_key, 0)
                if lost_events > previous:
                    self._lost_events += lost_events - previous
                    self._process_losses[loss_key] = lost_events
                    self._pause("诊断传输中断，部分记录未保存")
            if version == self._version:
                item["ack"] = version
                self.condition.notify_all()
            return {"accepted": version == self._version, **self.producer_state(token)}

    def unregister(self, token):
        with self.condition:
            if token in self._producers:
                self._producers[token]["retireAt"] = time.monotonic() + 5
            self.condition.notify_all()

    def _retire(self):
        now = time.monotonic()
        for key, item in list(self._producers.items()):
            if item.get("retireAt") and item["retireAt"] < now:
                changes = []
                if self._store:
                    try:
                        for record_id in item["records"]:
                            meta = self._store.metadata(record_id)
                            if meta and meta.get("collectionStatus") == "recording":
                                meta.update(collectionStatus="interrupted", complete=False, status="unknown",
                                            incompleteReason="采集来源已退出，结果未知")
                                self._store.update(record_id, meta, active=False)
                                changes.append((record_id, meta.get("accountId")))
                        self._store.db.commit()
                    except Exception:
                        self._store.db.rollback()
                        raise
                for record_id, account in changes:
                    self._record_change(record_id, account, "interrupted")
                # Keep both the producer and its pending budget intact until
                # storage succeeds, so a maintenance retry releases them once.
                self._pending_bytes -= sum(value[1] for value in item.get("pendingRecordOutcomes", {}).values())
                del self._producers[key]
        self._process_grants.intersection_update(item.get("processId") for item in self._producers.values())
        loss_keys = {item.get("processId", key) for key, item in self._producers.items()}
        self._process_losses = {key: value for key, value in self._process_losses.items() if key in loss_keys}
        for item in self._producers.values():
            if item.get("transportAllowed") is False and not item.get("retireAt") and len(self._process_grants) < PROCESS_GRANT_LIMIT:
                self._process_grants.add(item["processId"])
                item["transportAllowed"] = True

    def ingest(self, producer_id, event):
        with self.lock:
            producer = self._producer(producer_id)
            if isinstance(event, dict) and event.get("type") == "health":
                self._lost_events += max(1, min(1000000, int(event.get("lostEvents", 1))))
                self._pause("采集来源报告传输缺口")
                return {"accepted": True, "reason": "trace-paused"}
            if not self._collecting:
                return {"accepted": False, "reason": "collection-disabled"}
            if not isinstance(event, dict):
                raise TraceError("invalid-event", "采集事件不正确")
            if event.get("collectionVersion") != self._version or event.get("deleteVersion") != self._capture_delete_version:
                return {"accepted": False, "reason": "version-expired"}
            if event.get("taskId") not in (None, producer["scope"]["taskId"]):
                raise TraceError("trace-scope-mismatch", "执行身份不匹配", 403)
            encoded = json.dumps(event, ensure_ascii=False).encode("utf-8")
            if len(encoded) > ENVELOPE_LIMIT:
                raise TraceError("event-too-large", "采集事件须分块", 413)
            safe = sanitize(event)
            # The capability is an authentication secret, never record content.
            safe.pop("producerId", None)
            if self._queued_bytes + self._pending_bytes + len(encoded) > QUEUE_LIMIT or self._queue.full():
                self._pause("诊断队列拥塞，已暂停采集")
                return {"accepted": False, "reason": "trace-paused"}
            self._queued_bytes += len(encoded)
            self._peak_bytes = max(self._peak_bytes, self._queued_bytes + self._pending_bytes)
            self._queue.put_nowait((producer_id, safe, len(encoded)))
            return {"accepted": True}

    def task_event(self, context, event):
        record_id = event.get("recordId") or context["taskId"] + "-task"
        return self.ingest(context["producerId"], {**event, "type": "task", "recordId": record_id,
                    "eventSeq": event.get("eventSeq", 0), "collectionVersion": context["collectionVersion"],
                    "deleteVersion": context["deleteVersion"]})

    def outcome(self, context, application_status, adoption=None, result=None):
        with self.lock:
            if not self._collecting:
                return
            collection_version, delete_version = self._version, self._capture_delete_version
        event = {"type": "scope-outcome", "recordId": context["taskId"], "eventSeq": 2147483646,
                 "applicationStatus": application_status, "adoption": adoption,
                 "collectionVersion": collection_version, "deleteVersion": delete_version}
        if result is not None:
            event["result"] = result
        self.ingest(context["producerId"], event)

    def record_outcome(self, context, record_id, application, adoption, result=None, error_code=None):
        """Attach a parser decision to the attempt returned by that same execution."""
        with self.lock:
            item = self._producer(context["producerId"])
            if item["scope"]["taskId"] != context.get("taskId"):
                raise TraceError("trace-scope-mismatch", "执行身份不匹配", 403)
            if not isinstance(record_id, str) or not 1 <= len(record_id) <= 128:
                raise TraceError("invalid-record", "调用标识不正确")
            known = self._store.metadata(record_id) if self._store else None
            if known and known["taskId"] != item["scope"]["taskId"]:
                raise TraceError("trace-scope-mismatch", "调用不属于此执行", 403)
            sequence = item.get("outcomeSeq", 2147400000)
            item["outcomeSeq"] = sequence + 1
            event = {"type": "record-outcome", "recordId": record_id, "eventSeq": sequence,
                     "applicationStatus": application, "adoption": adoption,
                     "collectionVersion": self._version, "deleteVersion": self._capture_delete_version}
            if error_code is not None and isinstance(error_code, str) and re.fullmatch(r"[A-Za-z0-9:_-]{1,80}", error_code):
                event["errorCode"] = error_code
            if result is not None:
                event["result"] = result
        return self.ingest(context["producerId"], event)

    def _record_change(self, record_id, account, kind):
        self._revision += 1
        self._changes.append({"revision": self._revision, "recordId": record_id, "account": account or "", "kind": kind})

    def _apply(self, token, event):
        if not self._collecting or event.get("collectionVersion") != self._version or event.get("deleteVersion") != self._capture_delete_version:
            return
        producer = self._producers.get(token)
        if not producer or not self._store:
            return
        if self._store.usage() >= self._store.space_limit - 262144:
            if not self._prune(reserve_bytes=262144):
                return
        if event.get("type") == "scope-outcome":
            producer["terminal"] = {key: value for key, value in event.items() if key != "result"}
            for record_id in tuple(producer["records"]):
                record = self._store.metadata(record_id)
                if record and record.get("collectionStatus") == "complete":
                    self._apply(token, {**event, "type": "outcome", "recordId": record_id})
            return
        record_id, kind = event.get("recordId"), event.get("type")
        seq = event.get("eventSeq")
        if not isinstance(record_id, str) or not 1 <= len(record_id) <= 128 or not isinstance(seq, int) or seq < 0:
            return
        meta = self._store.metadata(record_id)
        if meta and meta["taskId"] != producer["scope"]["taskId"]:
            return
        if meta and meta["startedAt"] < self._retention_cutoff():
            self._prune()
            meta = self._store.metadata(record_id)
        if kind == "record-outcome":
            if not meta:
                if record_id in producer["records"]:
                    return  # Expired/deleted content must not return via a late parser result.
                pending = producer.setdefault("pendingRecordOutcomes", {})
                if len(pending) >= 128 and record_id not in pending:
                    self._lost_events += 1
                    self._pause("调用处理结果等待队列已满")
                else:
                    if record_id in pending:
                        self._pending_bytes -= pending[record_id][1]
                    size = len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
                    self._pending_bytes += size
                    pending[record_id] = ({**event, "type": "outcome"}, size)
                return
            kind = "outcome"
            event = {**event, "type": "outcome"}
        if kind in ("start", "task"):
            if meta:
                previous = self._store.db.execute("SELECT digest FROM events WHERE record=? AND seq=?", (record_id, seq)).fetchone()
                digest = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if previous and previous[0] != digest:
                    meta.update(complete=False, collectionStatus="stopped", status="unknown", incompleteReason="同一开始事件出现不同内容")
                    self._store.update(record_id, meta, active=False)
                return
            if record_id in producer["records"]:
                return  # This ID was already observed, then deleted by retention/capacity.
            if not self._remember_record(producer, record_id):
                return
            source = event.get("meta") if isinstance(event.get("meta"), dict) else event
            if timestamp(source.get("startedAt")) < self._retention_cutoff():
                pending = producer.get("pendingRecordOutcomes", {}).pop(record_id, None)
                if pending:
                    self._pending_bytes -= pending[1]
                return
            if not self._prune(reserve=1 if kind == "start" else 0):
                return
            meta = {key: val for key, val in source.items() if key not in ("type", "text", "request", "response", "result", "producerId")}
            meta.update(producer["scope"], recordId=record_id, schemaVersion=1,
                        collectionVersion=self._version, deleteVersion=self._capture_delete_version,
                        startedAt=timestamp(source.get("startedAt")), transportStatus="running" if kind == "start" else "not-sent",
                        collectionStatus="recording" if kind == "start" else "complete",
                        applicationStatus="pending", adoption="unknown", complete=False,
                        status="running" if kind == "start" else "unknown", attempt=source.get("attempt", 1),
                        requestBytes=0, responseBytes=0)
            for field in ("source", "stepId", "parentStepId"):
                if isinstance(source.get(field), str) and 0 < len(source[field]) <= 128:
                    meta[field] = source[field]
            meta["taskSource"] = producer["scope"].get("source")
            if kind == "task":
                meta.update(kind="task-event", eventKind=event.get("kind"), requestCount=0, complete=True,
                            reason=event.get("reason", "未发出模型请求"))
            if len(json.dumps(meta, ensure_ascii=False).encode("utf-8")) > MAX_METADATA:
                meta = {**producer["scope"], "recordId": record_id, "startedAt": timestamp(None),
                        "collectionStatus": "stopped", "complete": False, "status": "incomplete",
                        "incompleteReason": "诊断元数据超过上限"}
            self._store.insert(meta)
            digest = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            self._store.db.execute("INSERT INTO events VALUES(?,?,?)", (record_id, seq, digest))
            pending = producer.get("pendingRecordOutcomes", {}).pop(record_id, None)
            if pending:
                self._pending_bytes -= pending[1]
                self._apply(token, pending[0])
        elif not meta or record_id not in producer["records"]:
            return
        elif meta.get("collectionStatus") in ("stopped", "interrupted"):
            return
        elif kind == "chunk":
            if event.get("kind") not in BODY_KINDS or not isinstance(event.get("text"), str):
                return
            status = self._store.append(record_id, event)
            if status == "duplicate":
                return
            if status == "conflict":
                meta.update(complete=False, collectionStatus="stopped", status="unknown", incompleteReason="同一分块出现不同内容")
            if status == "truncated":
                side = "request" if event["kind"] == "request" else "response"
                meta.update(complete=False, **{side + "Truncated": True}, incompleteReason="正文达到单侧保存上限")
            self._store.update(record_id, meta, active=meta.get("collectionStatus") == "recording")
        else:
            digest = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            previous = self._store.db.execute("SELECT digest FROM events WHERE record=? AND seq=?", (record_id, seq)).fetchone()
            if previous:
                if previous[0] != digest:
                    meta.update(complete=False, status="incomplete", collectionStatus="stopped", incompleteReason="同一事件出现不同内容")
                    self._store.update(record_id, meta, active=False)
                return
            self._store.db.execute("INSERT INTO events VALUES(?,?,?)", (record_id, seq, digest))
            allowed = ("transportStatus", "applicationStatus", "adoption", "error", "errorCode", "httpStatus", "usage", "normalizedUsage", "usageSource",
                       "firstByteAt", "firstContentAt", "firstResponseAt", "firstTextAt", "receivedAt", "responseHeaders",
                       "finishedAt", "durationMs", "finishReason", "incompleteReason", "timings")
            if kind == "metadata":
                for field in ("source", "trigger", "nativeMessageId", "stepId", "parentStepId"):
                    if isinstance(event.get(field), str) and 0 < len(event[field]) <= 128:
                        meta[field] = event[field]
            update = {key: event[key] for key in allowed if key in event}
            if seq == 2147483646:
                meta["taskApplicationStatus"], meta["taskAdoption"] = event.get("applicationStatus"), event.get("adoption")
                if meta.get("applicationStatus") not in (None, "pending", "unknown", "processing"):
                    update.pop("applicationStatus", None)
                if (meta.get("applicationStatus") in ("failed", "parse-failed", "validation-failed", "error") or
                        meta.get("adoption") not in (None, "pending", "unknown")):
                    update.pop("adoption", None)
                if meta.get("transportStatus") in ("failed", "interrupted"):
                    update.pop("applicationStatus", None)
                    update.pop("adoption", None)
            meta.update(update)
            if event.get("incomplete"):
                meta["incomplete"] = True
                meta["complete"] = False
            if kind == "end" and event.get("transportStatus") != "pending":
                missing = self._store.missing_chunks(record_id)
                meta["complete"] = not missing and not meta.get("incomplete") and not meta.get("requestTruncated") and not meta.get("responseTruncated") and event.get("complete", True)
                if missing:
                    meta["incompleteReason"] = "内容缺块或结束标记缺失"
                meta["collectionStatus"] = "complete"
                meta["transportStatus"] = event.get("transportStatus", "unknown")
                meta["finishedAt"] = timestamp(event.get("finishedAt"))
            if kind == "outcome" and event.get("result") is not None:
                value = event["result"] if isinstance(event["result"], str) else json.dumps(event["result"], ensure_ascii=False)
                result = self._store.append(record_id, {"eventSeq": seq, "kind": "result", "contentId": "result", "chunkIndex": 0, "text": value, "final": True})
                if result == "truncated":
                    meta.update(responseTruncated=True, complete=False)
            meta["status"] = display_status(meta)
            self._store.update(record_id, meta, active=meta.get("collectionStatus") == "recording")
            if kind == "end" and meta.get("collectionStatus") == "complete" and producer.get("terminal"):
                self._apply(token, {**producer["terminal"], "type": "outcome", "recordId": record_id})
        self._record_change(record_id, meta.get("accountId"), kind)

    def _pause(self, reason):
        if self._status == "paused":
            return
        self._collecting, self._status = False, "paused"
        self._pause_reason = reason
        self._version += 1
        self._discard_pending()
        self._pause_history.append({"startedAt": time.time(), "finishedAt": None, "reason": reason, "lostEvents": None})
        self._save_health()
        try:
            self._stop_records(reason, "interrupted")
        except Exception:
            pass
        self._notify()

    def _save_health(self):
        if self._store:
            try:
                atomic_json(self._store.root / "health.json", {"pauseHistory": list(self._pause_history)})
            except OSError:
                pass

    def _retention_cutoff(self):
        return time.time() - self._retention * 86400

    def _known_record_count(self):
        return sum(len(producer["records"]) for producer in self._producers.values())

    def _remember_record(self, producer, record_id):
        count = self._known_record_count()
        if count >= TOMBSTONE_LIMIT:
            self._pause("诊断记录失效标记达到容量上限")
            return False
        producer["records"].add(record_id)
        self._peak_known_records = max(self._peak_known_records, count + 1)
        return True

    def _prune(self, reserve=0, reserve_bytes=65536):
        before = self._store.delete_version
        if not self._store.reclaim(reserve=reserve, reserve_bytes=reserve_bytes):
            self._pause("记录空间已满，无法回收正在采集的内容")
            return False
        if self._store.delete_version != before:
            self._delete_version = self._store.delete_version
            # Viewer caches expire immediately; live producer attempts retain
            # their capture fence. Their observed IDs remain bounded tombstones.
            self._notify()
        return True

    def _clean_record_refs(self):
        surviving = {row[0] for row in self._store.db.execute("SELECT id FROM records")}
        for producer in self._producers.values():
            producer["records"].intersection_update(surviving)

    def _discard_pending(self, producer_ids=None):
        if producer_ids is not None and not producer_ids:
            return
        kept = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            token, _event, size = item
            if producer_ids is None or token in producer_ids:
                self._queued_bytes -= size
            else:
                kept.append(item)
            self._queue.task_done()
        for item in kept:
            self._queue.put_nowait(item)
        for token, producer in self._producers.items():
            if producer_ids is None or token in producer_ids:
                pending = producer.pop("pendingRecordOutcomes", {})
                self._pending_bytes -= sum(value[1] for value in pending.values())
                producer.pop("terminal", None)

    def _worker(self):
        last_maintenance = 0
        while not self._closing:
            try:
                item = self._queue.get(timeout=0.25)
            except queue.Empty:
                item = None
            with self.lock:
                if item:
                    token, event, size = item
                    try:
                        self._apply(token, event)
                        if self._store:
                            self._store.db.commit()
                    except Exception as exc:
                        self._pause("诊断写入失败（" + type(exc).__name__ + "）")
                    finally:
                        self._queued_bytes -= size
                        self._queue.task_done()
                now = time.monotonic()
                if now - last_maintenance > 1:
                    last_maintenance = now
                    try:
                        self._retire()
                        if self._store and not self._read_blocked:
                            self._prune()
                        if self._pause_reason is not None and now - self._last_probe >= self._retry_delay:
                            self.check_health()
                    except Exception as exc:
                        self._pause("存储维护失败（" + type(exc).__name__ + "）")

    def check_health(self):
        with self.lock:
            self._last_probe = time.monotonic()
            if self._read_blocked:
                return self._state()
            try:
                if self._store is None:
                    self._store = self._store_factory(self.root)
                    # Startup may have failed before preferences were readable.
                    # Restore them only after the store has replayed deletions.
                    self._enabled = self._store.settings["enabled"] and not self._pending_disable
                    self._retention = self._store.settings["retentionDays"]
                    self._delete_version = self._store.delete_version
                    self._capture_delete_version = self._delete_version
                    pending_history = list(self._pause_history)
                    saved_history = []
                    try:
                        history = json.loads((self._store.root / "health.json").read_text(encoding="utf-8"))
                        saved_history = history.get("pauseHistory", [])[-20:]
                    except (OSError, ValueError, TypeError):
                        pass
                    self._pause_history.clear()
                    self._pause_history.extend(saved_history)
                    if self._store.recovery_gap:
                        self._pause_history.append(self._store.recovery_gap)
                    self._pause_history.extend(pending_history)
                if self._pending_disable:
                    self._store.save_settings({"enabled": False, "retentionDays": self._retention})
                    self._pending_disable = False
                    self._save_error = None
                if not self._prune():
                    raise OSError("capacity")
                if self._known_record_count() >= TOMBSTONE_LIMIT:
                    raise OSError("tombstone capacity")
                if any(not item.get("transportAllowed", True) for item in self._producers.values() if not item.get("retireAt")):
                    raise OSError("buffer capacity")
                # A read-only SELECT/empty transaction cannot detect a full disk.
                # This tiny private probe proves a real write; it has no model data.
                probe = self._store.root / ".health-check"
                try:
                    import os
                    with probe.open("wb") as stream:
                        stream.write(b"ok")
                        stream.flush()
                        os.fsync(stream.fileno())
                finally:
                    probe.unlink(missing_ok=True)
                if self._pause_reason:
                    self._stop_records(self._pause_reason)
            except Exception:
                self._retry_delay = min(30, self._retry_delay * 2)
                return self._state()
            self._retry_delay = 1
            if self._pause_reason:
                self._pause_reason = None
                if self._pause_history and self._pause_history[-1]["finishedAt"] is None:
                    self._pause_history[-1]["finishedAt"] = time.time()
                self._save_health()
                self._version += 1
                self._delete_version = self._store.delete_version
                self._collecting = self._enabled
                self._status = "recording" if self._enabled else "off"
                self._notify()
            return self._state()

    def _resolve_account(self):
        # Source identity may take its own lock. Resolve it before taking the
        # diagnostics lock: account deletion already owns source -> diagnostics.
        try:
            account = self.current_account()
            account = str(account) if account else None
        except Exception:
            account = None
        try:
            epoch = self.current_account_version()
        except Exception:
            epoch = None
        return account, epoch

    def _effective_account(self, binding):
        if self._account != binding:
            self._account = binding
            self._account_version += 1
            self._sessions.clear()
            self._clients.clear()
        return binding[0]

    def create_session(self, client_id, scope="account"):
        binding = self._resolve_account()
        with self.lock:
            self._open()
            account = self._effective_account(binding)
            if scope not in ("account", "application") or not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
                raise TraceError("invalid-session", "查看会话参数不正确")
            old = self._clients.get(client_id)
            if old:
                self._sessions.pop(old, None)
            if len(self._sessions) >= 64:
                self._sessions.pop(next(iter(self._sessions)))
            token = secrets.token_urlsafe(32)
            session = {"accountId": account if scope == "account" else None, "scope": scope,
                       "sessionVersion": self._account_version, "clientId": client_id}
            self._sessions[token], self._clients[client_id] = session, token
            return {**session, "token": token, "deleteVersion": self._delete_version}

    def viewer(self, token):
        binding = self._resolve_account()
        with self.lock:
            self._effective_account(binding)
            return self._viewer_locked(token)

    def _viewer_locked(self, token):
        session = self._sessions.get(token)
        if not session or session["sessionVersion"] != self._account_version:
            raise TraceError("trace-session-expired", "账号或查看会话已变化，请重新打开记录", 409)
        return session

    def read(self, token, operation, params=None):
        params = params or {}
        self.viewer(token)
        with self.lock:
            session = self._viewer_locked(token)
            account = session["accountId"] or ""
            if params.get("account") and params["account"] != account:
                raise TraceError("trace-scope-mismatch", "不能读取其他账号的记录", 403)
            if operation == "state":
                return self._state()
            if self._read_blocked or not self._store:
                raise TraceError("trace-unavailable", "记录服务不可用或删除尚未完成", 503)
            self._prune()
            if operation == "records":
                result = self._store.list(account, params)
            elif operation in ("detail", "body"):
                record = self._store.metadata(params.get("recordId"), account)
                if not record:
                    raise TraceError("trace-record-unavailable", "记录已过期或删除", 410)
                if operation == "detail":
                    result = {"record": record}
                else:
                    kind = params.get("kind", "request")
                    if kind not in BODY_KINDS:
                        raise TraceError("invalid-kind", "正文类型不正确")
                    result = self._store.body(record["recordId"], kind, max(0, int(params.get("offset", 0))), min(65536, max(4, int(params.get("limit", 65536)))))
                    result.update(complete=record.get("complete", False), truncated=record.get("requestTruncated" if kind == "request" else "responseTruncated", False))
            elif operation == "events":
                cursor = max(0, int(params.get("cursor", 0)))
                reset = bool(self._changes and cursor < self._changes[0]["revision"] - 1)
                changes = [{key: value for key, value in item.items() if key != "account"} for item in self._changes if item["revision"] > cursor and item["account"] == account]
                result = {"reset": reset, "changes": changes[:500], "state": self._state()}
            else:
                raise TraceError("not-found", "接口不存在", 404)
            result = {**result, "revision": self._revision, "deleteVersion": self._delete_version}
        # Check identity again without the storage lock, then fence deletion in
        # the publication lock before returning any content to HTTP.
        self.viewer(token)
        with self.lock:
            self._viewer_locked(token)
            if self._read_blocked or result["deleteVersion"] != self._delete_version:
                raise TraceError("trace-record-unavailable", "记录已过期或删除", 410)
            return result

    def delete_scope(self, account_id=None, contact_id=None, session_id=None, *, all_accounts=False):
        with self.lock:
            self._open()
            scope = {}
            if not all_accounts:
                scope["accountId"] = account_id or ""
                if contact_id is not None:
                    scope["contactId"] = contact_id
                if session_id is not None:
                    scope["sessionId"] = session_id
            self._delete_version += 1
            self._read_blocked = True
            if all_accounts:
                self._capture_delete_version += 1
                self._version += 1
                self._collecting = False
                self._discard_pending()
            if not all_accounts:
                revoked = {token for token, producer in self._producers.items()
                           if all((producer["scope"].get(key) or "") == value for key, value in scope.items())}
                self._discard_pending(revoked)
                for token in revoked:
                    del self._producers[token]
            try:
                if not self._store:
                    raise OSError("store unavailable")
                self._store.delete(scope)
                self._delete_version = self._store.delete_version
                if all_accounts:
                    self._clean_record_refs()
            except Exception as exc:
                self._pause("记录删除未完成（" + type(exc).__name__ + "）")
                raise TraceError("trace-delete-failed", "记录删除未完成，本次运行已拒绝读取，请重试", 503) from None
            self._read_blocked = False
            self._collecting = self._enabled and self._pause_reason is None
            self._status = "recording" if self._collecting else "off" if not self._enabled else "paused"
            self._changes.clear()
            self._notify()
            return {"cleared": True, **self._state()}

    def flush(self, timeout=2):
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def close(self):
        self.flush()
        self._closing = True
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        with self.lock:
            if self._store:
                try:
                    self._store.close()
                except Exception:
                    pass  # Diagnostics must not prevent application shutdown.
                finally:
                    self._store = None
