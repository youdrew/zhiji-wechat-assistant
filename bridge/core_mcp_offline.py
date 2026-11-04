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

    def __init__(self, friends, registry_path, *, live_reader=None, names=None):
        self.friends = friends
        self.registry_path = Path(registry_path)
        # Live media stills come from Core's bounded history reader; injectable for tests.
        self._live_reader = live_reader
        # ``names(account) -> {user: displayName}``: session names Core already observed
        # (the same labels as the MCP settings directory). Never opens WeChat; may be empty.
        self._names = names

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
    def list_accounts(self, allowed_subjects=None, *, accounts=None):
        """Saved accounts with their offline status.

        ``allowed_subjects`` is ``None`` for full mode or ``{accountId: [subjectId, ...]}``
        for per-contact mode; the per-contact coverage is recomputed over exactly the
        granted contacts and the result is marked ``scope=authorized_contacts``.
        ``accounts`` (accounts mode) limits the listing to those granted account ids,
        each summarized in full and marked ``scope=authorized_accounts``; accounts
        never granted are not read at all.
        """
        summaries = []
        whole = "installation" if accounts is None else "authorized_accounts"
        for identifier in sorted(self.records()):
            if allowed_subjects is not None and identifier not in allowed_subjects:
                continue
            if accounts is not None and identifier not in accounts:
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
                              "scope": whole if allowed_subjects is None else "authorized_contacts"})
        return {"accounts": summaries,
                "scope": whole if allowed_subjects is None else "authorized_contacts"}

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


    # -- 外部 Agent 读取入口（G1–G3） -------------------------------------------
    # ``allowed`` 一律是代理按授权算好的 ``{accountId: frozenset(users) | None}``
    # （None = 该账号全部联系人）；这里只在给定范围内取数，绝不自行扩大范围。

    def _cached_names(self, account):
        if self._names is None:
            return {}
        try:
            names = self._names(account)
        except Exception:
            return {}
        return names if isinstance(names, dict) else {}

    def _conversations(self, account_id, users=None, **options):
        """Saved conversations of one account as contact entries (name, recency, size).

        ``options`` pass through to ``FriendService.saved_conversations`` (limit,
        after, match); returns ``(contacts, total)``.
        """
        record = self.resolve(account_id)
        try:
            listing = self.friends.saved_conversations(record["account"], users, **options)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None
        cached = self._cached_names(record["account"])
        contacts = []
        for item in listing["conversations"]:
            user = item["user"]
            session_name = cached.get(user) if isinstance(cached.get(user), str) and cached.get(user) else None
            name, source = (session_name, "session") if session_name else \
                (item["otherName"], "message") if item["otherName"] else (user, "id")
            last = item["last"]
            contacts.append({"accountId": account_id, "subjectId": user, "name": name, "nameSource": source,
                             "names": [value for value in dict.fromkeys((session_name, item["otherName"]))
                                       if value],
                             "isGroup": item["isGroup"], "state": item["state"],
                             "savedMessages": item["readCount"], "totalCount": item["totalCount"],
                             "dataTimeMs": item["updatedAtMs"],
                             "lastMessageTime": last["time"] if last else None, "last": last,
                             "sortTime": item["sortTime"]})
        return contacts, listing["total"]

    def _contacts(self, account_id, users=None):
        return self._conversations(account_id, users)[0]

    def _each_account(self, allowed, action):
        """Run ``action(accountId, users)`` per readable account; an account without
        local data is reported as unavailable instead of failing the whole listing."""
        results, unavailable = [], []
        for account_id in sorted(allowed):
            try:
                results.extend(action(account_id, allowed[account_id]))
            except McpError as exc:
                if exc.code in ("cache-gap", "account-paused", "account-unknown", "data-busy"):
                    unavailable.append({"accountId": account_id, "reason": exc.code})
                    continue
                raise
        return results, unavailable

    @staticmethod
    def _account_after(after, account_id, *, seq):
        """The per-account keyset bound for a cross-account position ``after``.

        Cross-account order is (time desc, accountId asc, subjectId asc[, seq desc]);
        inside one account accountId is constant, so an earlier account only needs
        "older than t", a later one "t or older" and the same account the exact
        (subject, seq) position.
        """
        if after is None:
            return None
        if account_id < after["a"]:
            return {"timeMs": after["t"]}
        if account_id > after["a"]:
            return {"timeMs": after["t"], "sameTime": True}
        bound = {"timeMs": after["t"], "user": after["u"]}
        if seq:
            bound["seq"] = after["s"]
        return bound

    @staticmethod
    def _checked_after(after, *, seq):
        """Validate an opaque keyset position (``next`` of a previous page)."""
        if after is None:
            return None
        if not isinstance(after, dict) or type(after.get("t")) is not int or after["t"] < 0 or \
                not isinstance(after.get("a"), str) or not isinstance(after.get("u"), str) or \
                (seq and type(after.get("s")) is not int):
            raise McpError(400, "cursor-invalid", "游标无效或已失效")
        return after

    def find_contacts(self, allowed, *, query, limit):
        """Case-insensitive substring match on display names and the stable id.

        Matching runs in SQL (id, latest other-side name, plus the ids whose cached
        session name matches); only the matches come back and are ranked here by
        match quality (exact, prefix, substring, id) then recency.
        """
        needle = query.strip().casefold()

        def candidates(account_id, users):
            record = self.resolve(account_id)
            cached = self._cached_names(record["account"])
            keep = None if users is None else set(users)
            named = [user for user, name in cached.items()
                     if isinstance(name, str) and needle in name.casefold() and (keep is None or user in keep)]
            return self._conversations(account_id, users, match={"needle": needle, "users": named})[0]

        contacts, unavailable = self._each_account(allowed, candidates)
        ranked = []
        for contact in contacts:
            best = None
            for name in contact["names"] or [contact["name"]]:
                folded = name.casefold()
                rank = 0 if folded == needle else 1 if folded.startswith(needle) else \
                    2 if needle in folded else None
                if rank is not None and (best is None or rank < best):
                    best = rank
            if best is None and needle in contact["subjectId"].casefold():
                best = 3
            if best is not None:
                ranked.append((best, -contact["sortTime"], contact["accountId"], contact["subjectId"], contact))
        ranked.sort(key=lambda item: item[:4])
        items = [{key: contact[key] for key in ("accountId", "subjectId", "name", "nameSource", "isGroup",
                                                "lastMessageTime", "savedMessages")}
                 for *_rank, contact in ranked[:limit]]
        return {"contacts": items, "total": len(ranked), "truncated": len(ranked) > limit,
                "unavailableAccounts": unavailable}

    def recent_conversations(self, allowed, *, limit, offset=0, after=None, keyset=False):
        """Saved conversations newest first, with a short preview of the last saved message.

        Order: last saved message time desc, accountId asc, subjectId asc -- one
        complete key, applied in SQL before each account's truncation and again in
        the cross-account merge, so pages never reorder on ties.

        Paging: ``after`` is the opaque ``next`` of the previous page (keyset:
        ``{"t": time, "a": accountId, "u": subjectId}``) and is preferred over
        ``offset``; ``keyset=True`` adds that ``next`` to the result. With keyset
        paging a conversation never repeats and is never skipped while the data is
        unchanged; when a new message arrives between pages
        its conversation moves above the watermark, so it is not shown again (if
        already seen) or not shown at all in this pass (if not yet seen) -- restart
        from the first page to see it. Offset paging shifts by one for every
        conversation that moves to the top between pages.
        """
        after = self._checked_after(after, seq=False)
        totals = []

        def page(account_id, users):
            contacts, total = self._conversations(account_id, users, limit=offset + limit + 1,
                                                  after=self._account_after(after, account_id, seq=False))
            totals.append(total)
            return contacts

        contacts, unavailable = self._each_account(allowed, page)
        contacts.sort(key=lambda item: (-item["sortTime"], item["accountId"], item["subjectId"]))
        chosen = contacts[offset:offset + limit]
        items = []
        for contact in chosen:
            last = contact["last"] or {}
            preview = last.get("text") or ("[媒体或非文字消息]" if last.get("hasMedia") else "")
            items.append({"accountId": contact["accountId"], "subjectId": contact["subjectId"],
                          "name": contact["name"], "isGroup": contact["isGroup"],
                          "lastMessageTime": contact["lastMessageTime"],
                          "lastMessagePreview": preview[:80], "lastSide": last.get("side")})
        more = len(contacts) > offset + limit
        tail = chosen[-1] if more and chosen else None
        result = {"conversations": items, "total": sum(totals), "hasMore": more,
                  "unavailableAccounts": unavailable}
        if keyset:
            result["next"] = {"t": tail["sortTime"], "a": tail["accountId"], "u": tail["subjectId"]}                 if tail else None
        return result

    def contact(self, account_id, subject_id):
        """One saved contact: name, saved counts and data readiness; 404 when nothing is saved."""
        found = self._contacts(account_id, [subject_id])
        if not found:
            raise McpError(404, "not-found", "该联系人在本机已保存资料中不存在")
        return found[0]

    def contact_stats(self, account_id, subject_id):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_contact_stats(record["account"], subject_id)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def contact_records(self, account_id, subject_id):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_contact_records(record["account"], subject_id)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def recent_messages(self, account_id, subject_id, *, limit, before_seq=None, before_time_ms=None):
        record = self.resolve(account_id)
        try:
            return self.friends.saved_recent_messages(record["account"], subject_id, limit=limit,
                                                      before_seq=before_seq, before_time_ms=before_time_ms)
        except AdvisorError as exc:
            raise _from_advisor(exc) from None

    def search_all(self, allowed, *, query, limit, offset=0, scan_max, after=None, keyset=False):
        """Cross-contact search: each readable account's newest matches, merged newest first.

        Order: message time desc, accountId asc, subjectId asc, seq desc -- one
        complete key, applied in SQL before each account's truncation and again in
        the merge, so equal timestamps never reorder between pages.

        Paging: ``after`` is the opaque ``next`` of the previous page (keyset:
        ``{"t": time, "a": accountId, "u": subjectId, "s": seq}``) and is preferred
        over ``offset``; ``keyset=True`` adds that ``next`` to the result. With
        keyset paging a hit never repeats and is never skipped while the data is
        unchanged; messages that arrive between pages are newer than the watermark
        and are not part of this pass. Each account scans only its ``scan_max`` most
        recently written rows at the time of each page, so a long pass during heavy
        ingestion can stop reaching the oldest matches (``scanLimitedAccounts``).
        """
        after = self._checked_after(after, seq=True)
        names = {}

        def search(account_id, users):
            record = self.resolve(account_id)
            try:
                result = self.friends.saved_search_all(record["account"], query=query, users=users,
                                                       limit=offset + limit + 1, scan_max=scan_max,
                                                       after=self._account_after(after, account_id, seq=True))
            except AdvisorError as exc:
                raise _from_advisor(exc) from None
            if result["scanLimited"]:
                limited.append(account_id)
            return [{"accountId": account_id, **hit} for hit in result["hits"]]

        limited = []
        hits, unavailable = self._each_account(allowed, search)
        hits.sort(key=lambda hit: (-hit["sortTime"], hit["accountId"], hit["user"], -hit["seq"]))
        chosen = hits[offset:offset + limit]
        for account_id in sorted({hit["accountId"] for hit in chosen}):
            hit_users = sorted({hit["user"] for hit in chosen if hit["accountId"] == account_id})
            try:
                found = self._contacts(account_id, hit_users)
            except McpError as exc:
                if exc.code != "data-busy":
                    raise
                found = []
            for contact in found:
                names[(account_id, contact["subjectId"])] = (contact["name"], contact["isGroup"])
        page = []
        for hit in chosen:
            name, group = names.get((hit["accountId"], hit["user"]), (hit["user"], hit["user"].endswith("@chatroom")))
            page.append({"accountId": hit["accountId"], "subjectId": hit["user"], "name": name, "isGroup": group,
                         "message": hit["message"]})
        more = len(hits) > offset + limit
        result = {"hits": page, "hasMore": more, "scanLimitedAccounts": limited,
                  "unavailableAccounts": unavailable}
        if keyset:
            tail = chosen[-1] if more and chosen else None
            result["next"] = {"t": tail["sortTime"], "a": tail["accountId"], "u": tail["user"],
                              "s": tail["seq"]} if tail else None
        return result


def _from_advisor(exc):
    """Map a scoped AdvisorError onto the stable codes the MCP route reports."""
    mapping = {"invalid-request": (400, "invalid-request"),
               "account-closed": (404, "cache-gap"),
               "account-paused": (409, "account-paused"),
               "not-found": (404, "not-found"),
               "data-busy": (503, "data-busy"),
               # 版本条件或状态机不满足：稳定的 version-conflict，客户端重读后再试。
               "request-conflict": (409, "version-conflict")}
    status, code = mapping.get(exc.code, (503, "account-unavailable"))
    return McpError(status, code, exc.message)
