"""Chat history as plugins see it: coverage, bounded pages, snapshots and evidence checks.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The projection (``context`` and ``context_messages`` in each account database)
is maintained by Core's import; plugins only read it through these views.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time

from backend_contracts import AccountChangedError, AccountPreparationPending, AccountUnavailableError, MessagesUnavailableError
from core_projection import _decode_position, _encode_position

PAGE_LIMIT = 500
STICKER_LIMIT = 1000
MAX_FRAMES = 4
CONTEXT_LIMIT = 50
VERIFY_LIMIT = 500
VERIFY_GROUP_LIMIT = 16
STATUS_LIMIT = 500
RECENT_LIMIT = 2000
MEDIA_KINDS = ("image", "sticker", "video")
DIRECTORY_TTL = 300
DIRECTORY_SNAPSHOTS = 8


def content_hash(row):
    """Stable digest of one message's immutable facts (same rule as relationship-memory evidence)."""
    facts = [row.get("sort_key"), row.get("side"), row.get("sender_id") or "", row.get("kind") or "",
             row.get("text") or ""]
    return hashlib.sha256(json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def source_position(value):
    """A source row position in the projection's ``highwater`` encoding, or None."""
    if (not isinstance(value, (list, tuple)) or len(value) != 3 or type(value[0]) is not int or
            not isinstance(value[1], str) or type(value[2]) is not int):
        return None
    return _encode_position(value)


def _metadata_value(read, *args):
    """Unknown metadata is never evidence that a cached history is unchanged."""
    if not callable(read):
        return None
    try:
        value = read(*args)
    except (AccountChangedError, AccountPreparationPending, AccountUnavailableError, MessagesUnavailableError):
        raise
    except Exception as exc:
        if getattr(exc, "code", None) in ("account-changed", "account-unavailable", "source-changed", "core-unavailable"):
            raise
        return None
    return value if isinstance(value, str) and value else None


def message_view(row):
    try:
        raw = json.loads(row.get("raw") or "{}")
    except ValueError:
        raw = {}
    item = {"id": row["msg_id"], "seq": row["seq"], "sortKey": row["sort_key"], "side": row.get("side"),
            "senderId": row.get("sender_id") or "", "senderName": row.get("sender_name") or "",
            "kind": row.get("kind"), "type": raw.get("type"), "text": row.get("text") or "",
            "time": row.get("time_ms"), "group": bool(row.get("group_chat")), "hash": content_hash(row),
            "raw": row.get("raw") or "{}"}
    if raw.get("media") is not None:
        item["media"] = raw["media"]
    return item


class HistoryService:
    def __init__(self, stores, context):
        self.stores = stores
        self.context = context
        self._directories = {}
        self._directory_lock = threading.Lock()

    def conversations(self, account, after=None, limit=PAGE_LIMIT):
        """A bounded page of one immutable, account/source-bound discovery snapshot."""
        limit = max(1, min(int(limit), PAGE_LIMIT))
        fingerprint = self.context.source_scope(account, fresh=True)
        now = time.monotonic()
        with self._directory_lock:
            self._directories = {key: value for key, value in self._directories.items()
                                 if value["expires"] > now}
            if after:
                try:
                    token, position = after.split(":", 1)
                    offset = int(position)
                    snapshot = self._directories[token]
                    if offset < 0 or snapshot["account"] != account or snapshot["sourceFingerprint"] != fingerprint:
                        raise ValueError()
                except (ValueError, KeyError, AttributeError):
                    raise ValueError("directory-expired") from None
            else:
                snapshot = None
        if snapshot is None:
            read = getattr(self.context.source, "history_conversations", None)
            if not callable(read):
                return {"account": account, "sourceFingerprint": fingerprint, "revision": None,
                        "conversations": [], "next": None, "total": 0,
                        "coverage": {"state": "unsupported", "readableShards": 0,
                                     "failedShards": [], "unresolvedTables": 0}}
            value = read()
            if value.get("account") != account or self.context.source_scope(account, fresh=True) != fingerprint:
                raise ValueError("source-changed")
            rows = [{**row, "latest": source_position(row.get("latest")), "sourceFingerprint": fingerprint}
                    for row in value["conversations"]]
            revision = hashlib.sha256(json.dumps([fingerprint, rows, value["coverage"]],
                                                 sort_keys=True).encode()).hexdigest()
            token, offset = secrets.token_hex(16), 0
            snapshot = {"account": account, "sourceFingerprint": fingerprint, "revision": revision,
                        "conversations": rows, "coverage": value["coverage"], "expires": now + DIRECTORY_TTL}
            with self._directory_lock:
                while len(self._directories) >= DIRECTORY_SNAPSHOTS:
                    self._directories.pop(next(iter(self._directories)))
                self._directories[token] = snapshot
        if self.context.source_scope(account, fresh=True) != fingerprint:
            raise ValueError("source-changed")
        rows = snapshot["conversations"]
        end = offset + limit
        return {key: snapshot[key] for key in ("account", "sourceFingerprint", "revision", "coverage")} | {
            "conversations": rows[offset:end], "total": len(rows),
            "next": f"{token}:{end}" if end < len(rows) else None}

    def _database(self, account):
        return self.stores.account(account)

    def _watermark(self, database, user):
        row = database.read_row("SELECT seq, msg_id FROM context_messages WHERE user=? ORDER BY seq DESC LIMIT 1", (user,))
        return {"seq": row["seq"], "id": row["msg_id"]} if row else None

    def status(self, account, user, fresh=False, audit=False, audit_highwater=None):
        if audit_highwater is not None and (not audit or not isinstance(audit_highwater, str) or
                len(audit_highwater) > 512 or _decode_position(audit_highwater) is None):
            raise ValueError("invalid audit ceiling")
        database = self._database(account)
        current_source = self.context.source_scope(account, fresh=fresh)
        row = database.context(user)
        source = getattr(self.context, "source", None)
        revision = getattr(source, "history_revision", None)
        metadata = {"sourceRevision": _metadata_value(revision, user)}
        prefix = getattr(source, "history_prefix_signature", None)
        if audit:
            ceiling = audit_highwater if audit_highwater is not None else row.get("highwater") if row else None
            position = _decode_position(ceiling)
            metadata.update(sourcePrefixCeiling=ceiling, sourcePrefixSignature=(
                _metadata_value(prefix, user, position) if position is not None else None))
        if (callable(revision) or audit and callable(prefix)) and self.context.source_scope(account, fresh=fresh) != current_source:
            raise ValueError("source changed")
        return self._status_view(database, account, user, row, current_source, metadata)

    def status_many(self, account, users):
        """Ordinary status of many chats under one source identity check.

        No source metadata is read: ``sourceRevision`` and prefix audits stay
        per-chat, so a whole-account sweep costs local reads only.
        """
        if (not isinstance(users, list) or len(users) > STATUS_LIMIT or len(set(users)) != len(users) or
                any(not isinstance(user, str) or not 1 <= len(user) <= 128 or
                    any(ord(char) < 32 or char in "/\\" for char in user) for user in users)):
            raise ValueError("invalid users")
        database = self._database(account)
        current_source = self.context.source_scope(account)
        results = [{"user": user, **self._status_view(database, account, user, database.context(user), current_source, {})}
                   for user in users]
        if self.context.source_scope(account) != current_source:
            raise ValueError("source changed")
        return {"currentSourceFingerprint": current_source, "results": results}

    def _status_view(self, database, account, user, row, current_source, metadata):
        if row is None:
            return {"state": "idle", "readCount": 0, "watermark": None,
                    "currentSourceFingerprint": current_source, **metadata}
        status = {"state": row["state"], "readCount": row["read_count"], "revision": row["revision"],
                  "sourceFingerprint": row.get("source_id"), "epoch": row.get("epoch"),
                  "currentSourceFingerprint": current_source,
                  "watermark": self._watermark(database, user), "importing": self.context.importing(account, user),
                  # The source position this projection was read up to; compare with a directory ``latest``.
                  "highwater": row.get("highwater"), **metadata}
        if row.get("total_count") is not None:
            status["totalCount"] = row["total_count"]
        if row.get("error"):
            status["error"] = row["error"]
        return status

    def prepare(self, account, user, refresh=False, rebuild=False):
        options = {"refresh": bool(refresh or rebuild)}
        if rebuild:
            options["rebuild"] = True
        self.context.prepare(account, user, **options)
        return self.status(account, user)

    def messages(self, account, user, after=0, limit=PAGE_LIMIT, upto=None):
        after = max(0, int(after or 0))
        limit = max(1, min(int(limit or PAGE_LIMIT), PAGE_LIMIT))
        database = self._database(account)
        clauses, values = ["user=?", "seq>?"], [user, after]
        if upto is not None:
            clauses.append("seq<=?")
            values.append(int(upto))
        with database.read_snapshot() as reader:
            rows = reader.read_rows(f"SELECT * FROM context_messages WHERE {' AND '.join(clauses)} "
                                   "ORDER BY seq ASC LIMIT ?", (*values, limit + 1))
            state = reader.read_row("SELECT source_id,epoch,revision FROM context WHERE user=?", (user,)) or {}
            watermark = self._watermark(reader, user)
        more = len(rows) > limit
        rows = rows[:limit]
        return {"messages": [message_view(row) for row in rows],
                "next": rows[-1]["seq"] if more and rows else None,
                "watermark": watermark, "sourceFingerprint": state.get("source_id"),
                "epoch": state.get("epoch"), "revision": state.get("revision")}

    def rows(self, account, user, ids, *, verify_sender=False):
        """Projected messages by id, in conversation order; unknown ids are left out."""
        if not isinstance(ids, list) or len(ids) > PAGE_LIMIT or not all(isinstance(item, str) for item in ids):
            raise ValueError("invalid ids")
        if type(verify_sender) is not bool or (verify_sender and len(ids) != 1):
            raise ValueError("sender verification requires one message")
        database = self._database(account)
        found = []
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            found.extend(database.read_rows("SELECT * FROM context_messages WHERE user=? AND msg_id IN (" +
                                        ",".join("?" for _ in batch) + ")", (user, *batch)))
        messages = [message_view(row) for row in sorted(found, key=lambda row: row["seq"])]
        if verify_sender:
            scope = self.context.source_scope(account)
            for view in messages:
                view["senderResolved"] = self._verified_sender(user, view)
            if self.context.source_scope(account) != scope:
                raise ValueError("source changed")
        return {"messages": messages}

    def _verified_sender(self, user, view):
        """Re-read one original row; old projections need no schema/hash changes."""
        try:
            position = json.loads(view["sortKey"])
            if (not isinstance(position, list) or len(position) != 3 or
                    type(position[0]) is not int or type(position[2]) is not int or
                    min(position[0], position[2]) < 0 or not isinstance(position[1], str)):
                return False
            page = getattr(self.context.source, "history_page", None)
            live, _next = page(user, tuple(position), after=(position[0], position[1], position[2] - 1),
                               page_size=1) if callable(page) else ([], None)
            if len(live) != 1 or not isinstance(live[0], dict):
                return False
            current = live[0]
            return (current.get("id") == view["id"] and current.get("_senderResolved") is True and
                    current.get("senderId") == view["senderId"] and current.get("side") == view["side"] and
                    current.get("kind") == view["kind"])
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def recent(self, account, user, limit=200):
        """The live conversation end from WeChat, with its position cursor; nothing is stored."""
        source = self.context.source
        limit = max(1, min(int(limit), RECENT_LIMIT))
        scope = self.context.source_scope(account)
        highwater = source.history_highwater(user)
        window = source.messages(user, limit) if highwater is not None else []
        revision = getattr(source, "history_revision", None)
        if self.context.source_scope(account) != scope:
            raise ValueError("source changed")
        messages = []
        for item in window:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            sort = item.get("_sort")
            messages.append({"id": item["id"], "sortKey": list(sort) if isinstance(sort, (list, tuple)) else None,
                             "side": item.get("side"), "senderId": item.get("senderId"),
                             "senderResolved": item.get("_senderResolved") is True, "kind": item.get("kind"),
                             "time": item.get("time"), "text": item.get("text") or ""})
        return {"cursor": list(highwater) if highwater is not None else None,
                "revision": revision(user) if callable(revision) else None,
                "hasMoreBefore": getattr(window, "has_more_before", len(window) >= limit), "messages": messages}

    def recent_rows(self, account, user, limit=400):
        """The live conversation end in projection form (``seq`` counts within this window); nothing is stored."""
        _fingerprint, rows, more = self.context.recent_rows(account, user, max(1, min(int(limit), RECENT_LIMIT)))
        return {"messages": [message_view(row) for row in rows], "hasMoreBefore": bool(more)}

    def media(self, account, user, message_id, recent_messages=100, frames=1):
        """Bounded images of one projected image, sticker or video message for a vision model.

        ``frames`` above 1 asks for that many stills spread over an animated sticker (Know Your Friends,
        2026-10-14); other media and the default keep their single first frame.
        """
        source = self.context.source
        database = self._database(account)
        row = database.read_row("SELECT * FROM context_messages WHERE user=? AND msg_id=?", (user, message_id))
        if row is None:
            return {"error": "not-found"}
        scope = self.context.source_scope(account)
        current = {item.get("id") for item in source.messages(user, max(1, min(int(recent_messages), 100)))
                   if isinstance(item, dict)}
        if message_id not in current:
            page = getattr(source, "history_page", None)
            try:
                position = json.loads(row["sort_key"])
                live, _next = page(user, tuple(position), after=(position[0], position[1], position[2] - 1),
                                   page_size=1) if callable(page) else ([], None)
            except Exception:
                live = []
            if not (len(live) == 1 and live[0].get("id") == message_id):
                return {"error": "not-current"}
        read = getattr(source, "media_analysis", None)
        if not callable(read):
            return {"error": "unsupported"}
        frames = max(1, min(int(frames), MAX_FRAMES))
        payload = read(user, message_id, frames=frames) if frames > 1 else read(user, message_id)
        if self.context.source_scope(account) != scope:
            raise ValueError("source changed")
        if not isinstance(payload, dict) or payload.get("error") or payload.get("kind") not in MEDIA_KINDS:
            return {"error": "unavailable"}
        items = []
        for item in payload.get("items") or []:
            body = item.get("bytes") if isinstance(item, dict) else None
            if not isinstance(body, bytes) or not 0 < len(body) <= 2 * 1024 * 1024:
                return {"error": "unavailable"}
            items.append({"mime": item.get("mime"), "data": base64.b64encode(body).decode("ascii"),
                          **({"timeMs": item["timeMs"]} if "timeMs" in item else {})})
        result = {"kind": payload["kind"], "sampling": payload.get("sampling"), "items": items}
        if type(payload.get("durationMs")) is int and 0 <= payload["durationMs"] <= 86_400_000:
            result["durationMs"] = payload["durationMs"]
        return result

    def stickers(self, account, user, ids):
        """Identity, store pack and caption of projected sticker messages, read from the WeChat source.

        Added by Know Your Friends, 2026-10-14 (Apache-2.0). Ids that are not sticker messages of this
        conversation, or whose identity is unreadable, are absent from ``stickers``.
        """
        if (not isinstance(ids, list) or not 1 <= len(ids) <= STICKER_LIMIT or
                any(not isinstance(item, str) or not 1 <= len(item) <= 512 for item in ids)):
            raise ValueError("invalid sticker ids")
        read = getattr(self.context.source, "sticker_details", None)
        if not callable(read):
            return {"stickers": {}, "error": "unsupported"}
        database = self._database(account)
        refs = []
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            rows = database.read_rows("SELECT msg_id,sort_key,raw FROM context_messages WHERE user=? AND msg_id IN "
                                      f"({','.join('?' for _ in batch)})", (user, *batch))
            for row in rows:
                try:
                    raw, sort = json.loads(row["raw"] or "{}"), json.loads(row["sort_key"])
                except ValueError:
                    continue
                if (isinstance(raw.get("media"), dict) and raw["media"].get("kind") == "sticker" and
                        isinstance(sort, list) and len(sort) == 3 and isinstance(sort[1], str) and type(sort[2]) is int):
                    refs.append((sort[1], sort[2], row["msg_id"]))
        if not refs:
            return {"stickers": {}}
        scope = self.context.source_scope(account)
        details = read(user, refs)
        if self.context.source_scope(account) != scope:
            raise ValueError("source changed")
        return {"stickers": details}

    def export(self, account, user):
        """NDJSON lines: every message up to the watermark at the start, then one trailer."""
        database = self._database(account)
        with database.read_snapshot() as reader:
            watermark = self._watermark(reader, user)
            upto = watermark["seq"] if watermark else 0
            after, count = 0, 0
            while after < upto:
                rows = reader.read_rows("SELECT * FROM context_messages WHERE user=? AND seq>? AND seq<=? "
                                       "ORDER BY seq ASC LIMIT ?", (user, after, upto, PAGE_LIMIT))
                if not rows:
                    break
                for row in rows:
                    count += 1
                    yield json.dumps({"record": "message", **message_view(row)}, ensure_ascii=False) + "\n"
                after = rows[-1]["seq"]
            yield json.dumps({"record": "end", "watermark": watermark, "count": count}, ensure_ascii=False) + "\n"

    def around(self, account, user, message_id, before=10, after=10):
        before = max(0, min(int(before), CONTEXT_LIMIT))
        after = max(0, min(int(after), CONTEXT_LIMIT))
        database = self._database(account)
        target = database.read_row("SELECT * FROM context_messages WHERE user=? AND msg_id=?", (user, message_id))
        if target is None:
            return {"target": None, "messages": []}
        rows = database.read_rows("SELECT * FROM context_messages WHERE user=? AND seq BETWEEN ? AND ? ORDER BY seq ASC",
                              (user, target["seq"] - before, target["seq"] + after))
        return {"target": message_view(target), "messages": [message_view(row) for row in rows]}

    def verify(self, account, user, items, fresh=False):
        """Check evidence references: ``same`` / ``changed`` / ``missing`` for each ``{id, hash}``."""
        if not self._valid_verify_items(items) or type(fresh) is not bool:
            raise ValueError("invalid items")
        if fresh and callable(getattr(self.context.source, "history_verify_rows_many", None)):
            grouped = self.verify_many(account, [{"user": user, "items": items}])
            return {"results": grouped["results"][0]["results"]}
        fingerprint = self.context.source_scope(account, fresh=True) if fresh else None
        database = self._database(account)
        rows = self._verification_rows(database, user, items)
        batch = fresh and callable(getattr(self.context.source, "history_verify_rows", None))
        originals = None
        if batch:
            originals = self._verify_originals(user, rows) if rows else {}
        results = self._verification_results(user, items, rows, fresh, originals)
        if fresh and self.context.source_scope(account, fresh=True) != fingerprint:
            raise ValueError("source changed")
        return {"results": results}

    def verify_many(self, account, groups):
        """Freshly verify bounded contacts under one shared source identity check."""
        if not isinstance(groups, list) or len(groups) > VERIFY_GROUP_LIMIT:
            raise ValueError("invalid verification groups")
        users, total = set(), 0
        for group in groups:
            if not isinstance(group, dict) or set(group) != {"user", "items"}:
                raise ValueError("invalid verification group")
            user = group["user"]
            if (not isinstance(user, str) or not 1 <= len(user) <= 128 or
                    any(ord(char) < 32 or char in "/\\" for char in user) or user in users or
                    not self._valid_verify_items(group["items"])):
                raise ValueError("invalid verification group")
            users.add(user)
            total += len(group["items"])
        if total > VERIFY_LIMIT:
            raise ValueError("too many verification items")
        if not groups:
            return {"results": []}
        read = getattr(self.context.source, "history_verify_rows_many", None)
        if not callable(read):
            return {"results": [{"user": group["user"], **self.verify(
                account, group["user"], group["items"], fresh=True)} for group in groups]}
        # The source read brackets itself with a strict ownership check after the
        # reads and refreshes the identity lease, so both scope checks here use the
        # lease rather than querying Windows ownership again.
        fingerprint = self.context.source_scope(account)
        database = self._database(account)
        saved, positions, requested = {}, {}, []
        for group in groups:
            user = group["user"]
            rows = saved[user] = self._verification_rows(database, user, group["items"])
            positions[user] = self._original_positions(rows)
            if positions[user]:
                requested.append({"user": user, "positions": list(dict.fromkeys(positions[user].values()))})
        try:
            originals = read(requested) if requested else {}
        except (ValueError, TypeError, KeyError):
            originals = None
        results = []
        for group in groups:
            user = group["user"]
            current = self._project_originals(user, saved[user], positions[user],
                originals.get(user) if isinstance(originals, dict) else None)
            results.append({"user": user, "results": self._verification_results(
                user, group["items"], saved[user], True, current)})
        if self.context.source_scope(account) != fingerprint:
            raise ValueError("source changed")
        return {"results": results}

    @staticmethod
    def _valid_verify_items(items):
        return (isinstance(items, list) and len(items) <= VERIFY_LIMIT and
                all(isinstance(item, dict) and isinstance(item.get("id"), str) for item in items))

    @staticmethod
    def _verification_rows(database, user, items):
        identifiers = list(dict.fromkeys(item["id"] for item in items))
        return database.read_rows("SELECT * FROM context_messages WHERE user=? AND msg_id IN (" +
                                  ",".join("?" for _ in identifiers) + ")", (user, *identifiers)) if identifiers else []

    def _verification_results(self, user, items, rows, fresh, originals=None):
        saved = {row["msg_id"]: row for row in rows}
        results = []
        for item in items:
            row = saved.get(item["id"])
            state = "missing" if row is None else "same" if content_hash(row) == item.get("hash") else "changed"
            if fresh and row is not None:
                if originals is not None:
                    original = originals[row["msg_id"]]
                    state = original if isinstance(original, str) else (
                        "same" if content_hash(original) == item.get("hash") else "changed")
                else:
                    state = self._verify_original(user, row, item.get("hash"))
            results.append({"id": item["id"], "state": state})
        return results

    def _verify_originals(self, user, rows):
        """Read exact original locators together; never validate against the saved projection."""
        positions = self._original_positions(rows)
        if not positions:
            return {row["msg_id"]: "unready" for row in rows}
        try:
            originals = self.context.source.history_verify_rows(user, list(dict.fromkeys(positions.values())))
        except (ValueError, TypeError, KeyError):
            originals = None
        return self._project_originals(user, rows, positions, originals)

    @staticmethod
    def _original_positions(rows):
        positions = {}
        for row in rows:
            try:
                position = json.loads(row["sort_key"])
                if (not isinstance(position, list) or len(position) != 3 or
                        type(position[0]) is not int or not 0 <= position[0] <= 2**63 - 1 or
                        type(position[2]) is not int or not 0 <= position[2] <= 2**63 - 1 or
                        not isinstance(position[1], str)):
                    continue
                positions[row["msg_id"]] = tuple(position)
            except (ValueError, TypeError, KeyError):
                continue
        return positions

    def _project_originals(self, user, rows, positions, originals):
        result = {row["msg_id"]: "unready" for row in rows}
        if not positions or not isinstance(originals, dict):
            return result
        try:
            messages = []
            for identifier, position in positions.items():
                message = originals.get(position)
                if message is None:
                    result[identifier] = "missing"
                elif (isinstance(message, dict) and message.get("id") == identifier and
                      isinstance(message.get("_sort"), (list, tuple)) and tuple(message["_sort"]) == position):
                    messages.append(message)
                else:
                    result[identifier] = "changed"
            if messages:
                result.update({row["msg_id"]: row for row in self.context._page_rows(user, messages, 0)})
        except (ValueError, TypeError, KeyError):
            # An invalid batch never falls back to old projected content.
            return {row["msg_id"]: "unready" for row in rows}
        return result

    def _verify_original(self, user, row, expected_hash):
        """Read exactly the original locator; a stale projection cannot validate deleted text."""
        read = getattr(self.context.source, "history_page", None)
        if not callable(read):
            return "unready"
        try:
            position = json.loads(row["sort_key"])
            if (not isinstance(position, list) or len(position) != 3 or
                    type(position[0]) is not int or type(position[2]) is not int or
                    not isinstance(position[1], str)):
                return "unready"
            page, _next = read(user, tuple(position), after=(position[0], position[1], position[2] - 1), page_size=1)
            if not page or page[0].get("id") != row["msg_id"]:
                return "missing"
            current = self.context._page_rows(user, page, 0)
            return "same" if current and content_hash(current[0]) == expected_hash else "changed"
        except (ValueError, TypeError, KeyError):
            return "unready"
