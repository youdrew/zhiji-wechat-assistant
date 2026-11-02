"""Narrow HTTP projection; viewer and producer capabilities are never interchangeable."""
from __future__ import annotations

import hmac
import re
from model_traces import TraceError

PREFIX = "/api/model-traces/"
SESSION_HEADER = "X-Model-Trace-Session"
PRODUCER_HEADER = "X-Model-Trace-Producer"
POST_PATHS = frozenset(PREFIX + name for name in ("session", "state", "clear", "health", "ingest", "ack"))


def get(service, path, query, headers):
    if path == PREFIX + "producer-state":
        return service.producer_state(headers.get(PRODUCER_HEADER, ""))
    token = headers.get(SESSION_HEADER, "")
    if path in (PREFIX + "state", PREFIX + "records", PREFIX + "events"):
        return service.read(token, path.rsplit("/", 1)[-1], query)
    match = re.fullmatch(re.escape(PREFIX) + r"records/([A-Za-z0-9_.:-]{1,128})(/body)?", path)
    if match:
        return service.read(token, "body" if match[2] else "detail", {**query, "recordId": match[1]})
    raise TraceError("not-found", "接口不存在", 404)


def post(service, path, request, headers, control_token):
    if path == PREFIX + "session":
        supplied = headers.get("X-KYF-Control-Token", "")
        if (not isinstance(control_token, str) or len(control_token) < 32 or
                not hmac.compare_digest(supplied, control_token)):
            raise TraceError("trace-forbidden", "查看客户端未获授权", 403)
        if set(request) - {"clientId", "windowId", "scope"}:
            raise TraceError("invalid-session", "查看会话参数不正确")
        return service.create_session(request.get("clientId", request.get("windowId")), request.get("scope", "account"))
    if path in (PREFIX + "ingest", PREFIX + "ack"):
        producer = headers.get(PRODUCER_HEADER, "")
        if path.endswith("/ack"):
            return service.ack(producer, request.get("collectionVersion"), request.get("lostEvents", 0))
        events = request.get("events")
        if events is None:
            return service.ingest(producer, request)
        if not isinstance(events, list) or len(events) > 64:
            raise TraceError("invalid-event", "采集事件批次不正确")
        return {"results": [service.ingest(producer, event) for event in events]}
    service.viewer(headers.get(SESSION_HEADER, ""))
    if path == PREFIX + "state":
        return service.update_settings(request)
    if path == PREFIX + "clear":
        if request != {"confirmAllAccounts": True}:
            raise TraceError("confirmation-required", "须明确确认清空本机所有账号的采集内容")
        return service.delete_scope(all_accounts=True)
    if path == PREFIX + "health":
        if request:
            raise TraceError("invalid-request", "健康检查无需参数")
        return service.check_health()
    raise TraceError("not-found", "接口不存在", 404)
