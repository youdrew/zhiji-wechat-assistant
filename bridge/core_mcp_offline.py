"""The explicit "saved account" offline scope behind the MCP read tools.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Every call names one saved account from the local registry (never "the most
recent online account"); its projection is the only data source touched, and
results carry ``accountId``, ``sourceMode`` (live/cached), the data time,
coverage and source version. A missing projection, missing media and invalidated
evidence are reported for what they are instead of silently degrading. The
current WeChat account is verified separately by live operations elsewhere; this
scope only decides whether the projection is synced from a live session.
"""
from __future__ import annotations

import json
from pathlib import Path

from core_contracts import AdvisorError
from core_mcp_proxy import McpError

REGISTRY_LIMIT = 4096
SEARCH_PAGE_MAX = 100


def registry_records(path):
    """The saved-account registry as ``{accountId: record}``; a broken file means no accounts."""
    try:
        registry = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    records = registry.get("accounts") if isinstance(registry, dict) else None
    result = {}
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        account, identifier = record.get("account"), record.get("accountId")
        if not isinstance(account, str) or not account or not isinstance(identifier, str) or not identifier:
            continue
        nickname = record.get("nickname") if isinstance(record.get("nickname"), str) else ""
        wechat_id = record.get("wechatId") if isinstance(record.get("wechatId"), str) else ""
        result[identifier] = {"accountId": identifier, "account": account,
                              "label": nickname or wechat_id or identifier[:12]}
    return result


class SavedAccountScope:
    """Bridges the MCP read tools to FriendService's explicit saved-account methods."""

    def __init__(self, friends, registry_path, *, live_reader=None):
        self.friends = friends
        self.registry_path = Path(registry_path)
        # Live media stills come from Core's bounded history reader; injectable for tests.
        self._live_reader = live_reader

    # -- registry -------------------------------------------------------------
    def records(self):
        return registry_records(self.registry_path)

    def resolve(self, account_id):
        """The saved record of one explicit accountId; unknown ids never fall back to another account."""
        if not isinstance(account_id, str) or not account_id or len(account_id) > 128:
            raise McpError(400, "invalid-request", "账号标识不正确")
        record = self.records().get(account_id)
        if record is None:
            raise McpError(404, "account-unknown", "该账号不是本机已保存账号")
        return record

    def _live_media_reader(self):
        """Core's controlled media reader; a test may inject one via ``live_reader``."""
        if self._live_reader is not None:
            return self._live_reader
        advisor = getattr(self.friends, "advisor", None)
        stores, context = getattr(advisor, "stores", None), getattr(advisor, "context", None)
        if stores is None or context is None:
            return None
        from core_history import HistoryService
        return HistoryService(stores, context)

    # -- tools ----------------------------------------------------------------
    def list_accounts(self, allowed_subjects=None):
        """Saved accounts with their offline status.

        ``allowed_subjects`` is ``None`` for full mode or ``{accountId: [subjectId, ...]}``
        for per-contact mode; the per-contact coverage is recomputed over exactly the
        granted contacts and the result is marked ``scope=authorized_contacts``.
        """
        summaries = []
        for identifier in sorted(self.records()):
            if allowed_subjects is not None and identifier not in allowed_subjects:
                continue
            record = self.records()[identifier]
            try:
                if allowed_subjects is None:
                    summary = self.friends.saved_account_summary(record["account"])
                else:
                    summary = self.friends.saved_contacts_summary(record["account"],
                                                                  list(allowed_subjects[identifier]))
            except AdvisorError as exc:
                if exc.code in ("account-closed", "account-paused"):
                    summary = {"accountId": record["account"], "saved": False, "sourceMode": "cached",
                               "dataTimeMs": None, "contacts": 0, "readContacts": 0, "messages": 0,
                               "sourceVersion": None}
                else:
                    raise
            summaries.append({"accountId": identifier, "label": record["label"],
                              "saved": summary["saved"], "sourceMode": summary["sourceMode"],
                              "dataTimeMs": summary["dataTimeMs"],
                              "coverage": {"contacts": summary["contacts"],
                                           "readContacts": summary["readContacts"],
                                           "messages": summary["messages"]},
                              "sourceVersion": summary["sourceVersion"],
                              "scope": "installation" if allowed_subjects is None else "authorized_contacts"})
        return {"accounts": summaries,
                "scope": "installation" if allowed_subjects is None else "authorized_contacts"}

    def search_messages(self, account_id, subject_id, *, query, limit, offset):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_search(record["account"], subject_id, query=query,
                                             limit=limit, offset=offset)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def message_context(self, account_id, subject_id, message_id):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_message_context(record["account"], subject_id, message_id)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def list_memories(self, account_id, subject_id, *, status, limit, offset):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_memories(record["account"], subject_id, status=status,
                                               offset=offset, limit=limit)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def memory_evidence(self, account_id, subject_id, memory_id, ordinal=None):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_memory_evidence(record["account"], subject_id, memory_id,
                                                      ordinal=ordinal)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def media(self, account_id, subject_id, message_id, *, frames=1):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_media(record["account"], subject_id, message_id, frames=frames,
                                            live_reader=self._live_media_reader())
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    # -- phase 3b: versioned human-data writes (§5) ----------------------------
    # Each call names one explicit saved contact; FriendService re-checks the
    # version inside the account-database transaction, so a stale expectedVersion
    # can never commit, live or offline.

    def memory_write(self, account_id, subject_id, request):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_memory_action(record["account"], subject_id, request)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def contact_goal(self, account_id, subject_id):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_contact_goal(record["account"], subject_id)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def save_contact_goal(self, account_id, subject_id, *, text, expected_version):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_contact_goal_write(record["account"], subject_id, text,
                                                         expected_version)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def self_profile(self, account_id):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_self_profile(record["account"])
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def save_self_profile(self, account_id, *, background, style, expected_version):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_self_profile_write(record["account"], background, style,
                                                         expected_version)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def observations(self, account_id, subject_id, *, limit, offset):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_observations(record["account"], subject_id,
                                                   offset=offset, limit=limit)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def observation_write(self, account_id, subject_id, request):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_observation_write(record["account"], subject_id, request)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None


def _from_advisor(exc):
    """Map a scoped AdvisorError onto the stable codes the MCP route reports."""
    mapping = {"invalid-request": (400, "invalid-request"),
               "account-closed": (404, "cache-gap"),
               "account-paused": (409, "account-paused"),
               "not-found": (404, "not-found"),
               # 版本条件或状态机不满足：稳定的 version-conflict，客户端重读后再试。
               "request-conflict": (409, "version-conflict")}
    status, code = mapping.get(exc.code, (503, "account-unavailable"))
    return McpError(status, code, exc.message)
