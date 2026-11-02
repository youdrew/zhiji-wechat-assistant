"""Allowlisted HTTP projection for the host parts of 知己分析 windows.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Modified 2026-10-13 (Apache-2.0): analysis, runs and pages are served by the scheme plugin
through Core RPC; these routes keep 关系记忆, evidence, goals, observations and drafts.
"""
from core_contracts import AdvisorError

GET_PATHS = frozenset({"/api/friend/overview", "/api/friend/memories", "/api/friend/memory",
                       "/api/friend/evidence", "/api/friend/settings", "/api/friend/observations",
                       "/api/friend/drafts", "/api/friend/message"})
POST_PATHS = frozenset({"/api/friend/open", "/api/friend/memory", "/api/friend/locate", "/api/friend/settings",
                        "/api/friend/observation", "/api/friend/draft", "/api/friend/verify"})


def _shape(request, required, optional=()):
    if not isinstance(request, dict) or not set(required) <= request.keys() or \
            not request.keys() <= set(required) | set(optional):
        raise AdvisorError("invalid-request", "请求参数不正确")


def _number(value, name, default=None):
    if value is None and default is not None:
        return default
    if not isinstance(value, str) or not value.isdecimal() or len(value) > 9:
        raise AdvisorError("invalid-request", name + " 参数不正确")
    return int(value)


def get(service, path, query):
    scope = ("account", "user")
    if path == "/api/friend/overview":
        _shape(query, scope)
        return service.overview(query["account"], query["user"])
    if path == "/api/friend/memories":
        _shape(query, scope, ("view", "status", "offset", "limit"))
        return service.memories(query["account"], query["user"], query.get("view", "tags"),
                                query.get("status", "active"), _number(query.get("offset"), "offset", 0),
                                _number(query.get("limit"), "limit", 50))
    if path == "/api/friend/memory":
        _shape(query, scope + ("id",))
        return service.memory_detail(query["account"], query["user"], query["id"])
    if path == "/api/friend/evidence":
        _shape(query, scope + ("id", "ordinal"))
        return service.evidence(query["account"], query["user"], query["id"], _number(query["ordinal"], "ordinal"))
    if path == "/api/friend/settings":
        _shape(query, scope)
        return service.settings(query["account"], query["user"])
    if path == "/api/friend/observations":
        _shape(query, scope)
        return service.observations(query["account"], query["user"])
    if path == "/api/friend/drafts":
        _shape(query, scope)
        return service.drafts(query["account"], query["user"])
    if path == "/api/friend/message":
        _shape(query, scope + ("id",))
        return service.message_context(query["account"], query["user"], query["id"])
    raise AdvisorError("invalid-request", "接口不存在")


def post(service, path, request):
    scope = ("account", "user")
    if path == "/api/friend/open":
        _shape(request, scope)
        return service.open(request["account"], request["user"])
    if path == "/api/friend/verify":
        _shape(request, scope)
        return service.verify_evidence(request["account"], request["user"], force=True)
    if path == "/api/friend/memory":
        _shape(request, scope + ("action",), ("id", "ids", "scope", "text", "tags", "eventTime", "expectedVersion",
                                              "expectedMemoryVersion", "choice"))
        return service.memory_action(request["account"], request["user"],
                                     {key: value for key, value in request.items() if key not in scope})
    if path == "/api/friend/locate":
        _shape(request, scope + ("id", "ordinal"))
        if type(request["ordinal"]) is not int:
            raise AdvisorError("invalid-request", "证据编号不正确")
        return service.locate(request["account"], request["user"], request["id"], request["ordinal"])
    if path == "/api/friend/settings":
        _shape(request, scope + ("kind", "expectedVersion"), ("text", "background", "style"))
        return service.save_settings(request["account"], request["user"], request)
    if path == "/api/friend/observation":
        _shape(request, scope, ("action", "id", "judgement", "note", "dimension", "expectedVersion"))
        return service.save_observation(request["account"], request["user"], request)
    if path == "/api/friend/draft":
        _shape(request, scope + ("entry", "generation"), ("seq", "payload", "baseVersion", "close"))
        return service.save_draft(request["account"], request["user"], request)
    raise AdvisorError("invalid-request", "接口不存在")
