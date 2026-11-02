"""Cursor-based reads of one conversation in the existing WeChat snapshot.

Modified by Know Your Friends, 2026-10-11: forward paging retains raw scan
progress even when an entire page contains no displayable messages.
"""
from __future__ import annotations

import base64
import binascii
import heapq
import json
import re
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


SHARD_NAME = re.compile(r"message__message_\d+\.db\Z")
TABLE_NAME = re.compile(r"Msg_[0-9a-fA-F]{32}\Z")
CURSOR_TEXT = re.compile(r"[A-Za-z0-9_-]{1,1024}\Z")
SEARCH_SCAN_LIMIT = 2048
HISTORY_SCAN_LIMIT = 4096
HISTORY_CATEGORIES = frozenset({"all", "files", "media", "links", "music", "mini-programs", "channels"})
APP_CATEGORIES = {3: "music", 5: "links", 6: "files", 33: "mini-programs",
                  36: "mini-programs", 51: "channels", 76: "music"}
MEDIA_MESSAGE_TYPES = frozenset({3, 34, 43, 47, 11000})
TEXT_LINK = re.compile(r"https?://[^\s<>]+", re.I)
MESSAGE_FIELDS = ("local_id,local_type,real_sender_id,create_time,message_content,"
                  "compress_content,server_id,sort_seq")


def encode_cursor(account, user, position):
    seq, shard, local_id = position
    raw = json.dumps([1, account, user, int(seq), shard, int(local_id)],
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_cursor(value, account, user):
    if not isinstance(value, str) or not CURSOR_TEXT.fullmatch(value):
        raise ValueError("invalid history cursor")
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        parts = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeError, TypeError, binascii.Error) as exc:
        raise ValueError("invalid history cursor") from exc
    if (not isinstance(parts, list) or len(parts) != 6 or type(parts[0]) is not int or parts[0] != 1 or
            parts[1] != account or parts[2] != user or
            type(parts[3]) is not int or type(parts[5]) is not int or
            not 0 <= parts[3] <= 2**63 - 1 or not 0 <= parts[5] <= 2**63 - 1 or
            not isinstance(parts[4], str) or not SHARD_NAME.fullmatch(parts[4])):
        raise ValueError("invalid history cursor")
    return parts[3], parts[4], parts[5]


def date_bounds(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("invalid history date")
    try:
        selected = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("invalid history date") from exc
    timezone = ZoneInfo("Asia/Shanghai")
    begin = int(datetime(selected.year, selected.month, selected.day, tzinfo=timezone).timestamp())
    end = begin + 86400
    return begin, end, begin * 1000, end * 1000


def _position(item):
    record, shard, _senders = item
    return int(record[7]), shard, int(record[0])


def _condition(shard, bound, descending):
    if bound is None:
        return None, ()
    seq, bound_shard, local_id = bound
    if shard == bound_shard:
        op = "<" if descending else ">"
        return f"(sort_seq,local_id) {op} (?,?)", (seq, local_id)
    if descending:
        return ("sort_seq <= ?" if shard < bound_shard else "sort_seq < ?"), (seq,)
    return ("sort_seq >= ?" if shard > bound_shard else "sort_seq > ?"), (seq,)


def _merged_rows(db, user, bound=None, descending=True, bounds=None, max_rows=HISTORY_SCAN_LIMIT):
    """Merge keyset SQL cursors; each shard reads only rows reached by this page."""
    found = db._msg_conns(user)
    try:
        streams = []
        for conn, table in found:
            if not TABLE_NAME.fullmatch(table):
                raise RuntimeError("invalid message table")
            shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
            if not SHARD_NAME.fullmatch(shard):
                raise RuntimeError("unidentified message shard")
            streams.append((conn, table, shard))
        names = [entry[2] for entry in streams]
        if len(names) != len(set(names)):
            raise RuntimeError("duplicate message shard")
        ranks = {name: rank for rank, name in enumerate(sorted(names))}
        heap = []
        for conn, table, shard in streams:
            clauses, params = [], []
            clause, values = _condition(shard, bound, descending)
            if clause:
                clauses.append(clause)
                params.extend(values)
            if bounds:
                clauses.append("((create_time >= ? AND create_time < ?) OR "
                               "(create_time >= ? AND create_time < ?))")
                params.extend(bounds)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            direction = "DESC" if descending else "ASC"
            sql = (f"SELECT {MESSAGE_FIELDS} FROM {table}{where} "
                   f"ORDER BY sort_seq {direction},local_id {direction} LIMIT ?")
            cursor = conn.execute(sql, (*params, max_rows + 1))
            senders = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
            record = cursor.fetchone()
            if record is not None:
                seq, _name, local_id = _position((record, shard, senders))
                key = ((-seq, -ranks[shard], -local_id) if descending else
                       (seq, ranks[shard], local_id))
                heapq.heappush(heap, (key, record, shard, senders, cursor))
        while heap:
            _key, record, shard, senders, cursor = heapq.heappop(heap)
            item = (record, shard, senders)
            following = cursor.fetchone()
            if following is not None:
                seq, _name, local_id = _position((following, shard, senders))
                key = ((-seq, -ranks[shard], -local_id) if descending else
                       (seq, ranks[shard], local_id))
                heapq.heappush(heap, (key, following, shard, senders, cursor))
            yield item
    finally:
        for conn in {id(conn): conn for conn, _table in found}.values():
            conn.close()


def _issue_image(source, account, user, message, item, max_issued_images):
    # Modified by Know Your Friends, 2026-10-10: issue scoped voice/video references too.
    if message.get("media") or message["kind"] == "image" and item[0][1] == 3:
        key = (account, user, message["id"])
        source.issued_images[key] = tuple(message["_sort"]) + (item[0][6],)
        source.issued_images.move_to_end(key)
        if len(source.issued_images) > max_issued_images:
            source.issued_images.popitem(last=False)


def _history_category(source, db, item, message):
    """Classify original message types; a quoted app card is still a reply."""
    record = item[0]
    local_type = record[1]
    base_type = local_type & 0xFFFFFFFF if isinstance(local_type, int) else local_type
    if base_type in MEDIA_MESSAGE_TYPES:
        return "media"
    if base_type == 1:
        return "links" if TEXT_LINK.search(str(message.get("text") or "")) else None
    if base_type != 49:
        return None
    subtype = local_type >> 32 if isinstance(local_type, int) else 0
    if subtype:
        return APP_CATEGORIES.get(subtype)
    # Old snapshots keep the subtype only in appmsg XML. Use the same decoder
    # as the source, including its compressed-content fallback, and never return
    # source XML or external resource URLs in the search payload.
    _kind, content = source._decoded_content(db, local_type, record[4], record[5])
    if not isinstance(content, str) or len(content) > 128000 or "<!DOCTYPE" in content.upper():
        return None
    start = content.find("<")
    if start < 0:
        return None
    try:
        root = ElementTree.fromstring(content[start:].strip())
    except ElementTree.ParseError:
        return None
    app = root if root.tag == "appmsg" else root.find("./appmsg")
    value = app.findtext("./type", "").strip() if app is not None else ""
    return APP_CATEGORIES.get(int(value)) if re.fullmatch(r"[0-9]{1,10}", value) else None


def _collect(source, db, user, account, contacts, own_user, *, bound, descending,
             limit, scan_limit, bounds=None, query=None, early_match_scan=None,
             max_issued_images=2048, category=None):
    matched, consumed, last = [], 0, None
    with closing(_merged_rows(db, user, bound, descending, bounds, scan_limit)) as rows:
        while consumed < scan_limit and len(matched) < limit:
            if early_match_scan is not None and matched and consumed >= early_match_scan:
                break
            item = next(rows, None)
            if item is None:
                break
            consumed += 1
            last = _position(item)
            message = source._render_row(db, user, item, contacts, own_user)
            if message is None:
                continue
            if category is not None:
                selected_category = _history_category(source, db, item, message)
                if category != "all" and selected_category != category:
                    continue
                if selected_category:
                    message["historyCategory"] = selected_category
            if query is not None and query not in str(message.get("text") or "").casefold():
                continue
            _issue_image(source, account, user, message, item, max_issued_images)
            matched.append(message)
        more = next(rows, None) is not None
    return matched, last, more


def _target(source, db, user, position, contacts, own_user, max_issued_images):
    seq, shard, local_id = position
    found = db._msg_conns(user)
    try:
        for conn, table in found:
            if not TABLE_NAME.fullmatch(table):
                raise RuntimeError("invalid message table")
            actual_shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
            if not SHARD_NAME.fullmatch(actual_shard):
                raise RuntimeError("unidentified message shard")
            if actual_shard != shard:
                continue
            records = conn.execute(
                f"SELECT {MESSAGE_FIELDS} FROM {table} WHERE sort_seq=? AND local_id=? LIMIT 2",
                (seq, local_id)).fetchall()
            if len(records) != 1:
                break
            senders = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
            item = (records[0], shard, senders)
            message = source._render_row(db, user, item, contacts, own_user)
            if message is not None:
                _issue_image(source, str(db.account), user, message, item, max_issued_images)
                return message
            break
    finally:
        for conn in {id(conn): conn for conn, _table in found}.values():
            conn.close()
    raise ValueError("invalid history cursor")


def browse(source, account, user, *, before=None, around=None, after=None, limit=80,
           max_issued_images=2048):
    if sum(value is not None for value in (before, around, after)) > 1:
        raise ValueError("before, around and after cannot be combined")
    before_position = decode_cursor(before, account, user) if before is not None else None
    around_position = decode_cursor(around, account, user) if around is not None else None
    after_position = decode_cursor(after, account, user) if after is not None else None
    with source.lock:
        db = source._db(fresh=True)
        if str(db.account) != account:
            raise ValueError("account mismatch")
        contacts, own_user = source._contacts(db), source.self_user(db)
        if around_position is not None:
            target = _target(source, db, user, around_position, contacts, own_user,
                             max_issued_images)
            older, older_last, more_before = _collect(
                source, db, user, account, contacts, own_user, bound=around_position,
                descending=True, limit=limit // 2, scan_limit=HISTORY_SCAN_LIMIT,
                max_issued_images=max_issued_images)
            newer, _newer_last, more_after = _collect(
                source, db, user, account, contacts, own_user, bound=around_position,
                descending=False, limit=limit - 1 - len(older), scan_limit=HISTORY_SCAN_LIMIT,
                max_issued_images=max_issued_images)
            messages = list(reversed(older)) + [target] + newer
            next_position = older_last or around_position
        elif after_position is not None:
            messages, next_position, more_after = _collect(
                source, db, user, account, contacts, own_user, bound=after_position,
                descending=False, limit=limit, scan_limit=HISTORY_SCAN_LIMIT,
                max_issued_images=max_issued_images)
            more_before = True
        else:
            selected, next_position, more_before = _collect(
                source, db, user, account, contacts, own_user, bound=before_position,
                descending=True, limit=limit, scan_limit=HISTORY_SCAN_LIMIT,
                max_issued_images=max_issued_images)
            messages = list(reversed(selected))
            more_after = before_position is not None
        return {"messages": messages, "hasMoreBefore": more_before,
                "hasMoreAfter": more_after,
                "nextCursor": encode_cursor(account, user, next_position) if next_position else None,
                "oldestCursor": messages[0]["historyCursor"] if messages else None,
                "newestCursor": messages[-1]["historyCursor"] if messages else None,
                "focusId": target["id"] if around_position is not None else None}


def search(source, account, user, *, query=None, day=None, category="all", before=None, limit=50,
           max_issued_images=2048):
    if query is not None and (not isinstance(query, str) or len(query) > 256 or
                              any(ord(char) < 32 for char in query)):
        raise ValueError("invalid history query")
    query = (query or "").strip().casefold()
    if not isinstance(category, str) or category not in HISTORY_CATEGORIES:
        raise ValueError("invalid history category")
    bounds = date_bounds(None if day == "" else day)
    before_position = decode_cursor(before, account, user) if before is not None else None
    with source.lock:
        db = source._db(fresh=True)
        if str(db.account) != account:
            raise ValueError("account mismatch")
        contacts, own_user = source._contacts(db), source.self_user(db)
        messages, last, more = _collect(
            source, db, user, account, contacts, own_user, bound=before_position,
            descending=True, limit=limit, scan_limit=SEARCH_SCAN_LIMIT,
            bounds=bounds, query=query if query else None,
            early_match_scan=256 if query else None,
            max_issued_images=max_issued_images, category=category)
        return {"messages": messages, "hasMore": more,
                "nextCursor": encode_cursor(account, user, last) if more and last else None}
