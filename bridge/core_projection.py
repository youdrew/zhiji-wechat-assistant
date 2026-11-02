"""Core's managed WeChat chat projection: one imported copy per real account + WeChat user.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Split out of advisor_context.py and advisor_media.py, 2026-10-13 (plugin architecture): the
import half stays in Core; model-input assembly and summaries moved to the plugins that use them.

The projection holds every readable message the source exposes. Imports freeze the source
highwater and walk forward with the source cursor, checking identity before and after every
page; a page is saved in one SQLite transaction together with the checkpoint, and a cancelled
or account-switched reader never commits its late page. Plugins read it through
``/plugin-api/v1/history/*`` (core_history.py). No generative model is ever called here.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import nullcontext

import message_input
from backend_contracts import AccountChangedError, AccountUnavailableError, MessagesUnavailableError
from core_contracts import AdvisorError, public_context
from history_browser import decode_cursor

IMPORT_PAGE_SIZE = 256
MAX_CONTEXT_MESSAGE_CHARS = 16000
MAX_CONTEXT_ERROR_CHARS = 300
MAX_RAW_FIELD_CHARS = 2048
VALID_SIDES = ("self", "other")


def _encode_position(value):
    if value is None:
        return None
    return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))


def _decode_position(value):
    if not value:
        return None
    try:
        parts = json.loads(value)
    except (TypeError, ValueError):
        return None
    if (not isinstance(parts, list) or len(parts) != 3 or type(parts[0]) is not int or
            not isinstance(parts[1], str) or type(parts[2]) is not int):
        return None
    return (parts[0], parts[1], parts[2])


def _friendly_error(exc):
    return "读取聊天记录失败，请重试"


class _Import:
    def __init__(self, cancel, thread):
        self.cancel = cancel
        self.thread = thread


class ContextProjection:
    """Owns the import workers and the per-account context projection."""

    def __init__(self, stores, source):
        self.stores = stores
        self.source = source
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self._imports = {}
        self._closed = False
        self._paused = set()
        self._observed = {}
        self._pending_windows = {}

    # -- identity ------------------------------------------------------------
    def _identity(self, fresh=False):
        advisor_identity = getattr(self.source, "advisor_identity", None)
        verified = getattr(self.source, "verified_identity", None)
        if not fresh and callable(advisor_identity):
            identity = advisor_identity(messages=True)
        else:
            identity = verified(messages=True) if callable(verified) else self.source.identity()
        if isinstance(identity, (tuple, list)) and len(identity) >= 2:
            return str(identity[0]), os.path.normcase(os.path.realpath(str(identity[1])))
        raise AdvisorError("account-changed", "无法确认当前微信账号")

    @staticmethod
    def scope_fingerprint(account, workdir):
        identity = (str(account), os.path.normcase(os.path.realpath(str(workdir))))
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()

    def source_scope(self, account, fresh=False):
        identity = self._identity(fresh=fresh)
        if identity[0] != account:
            raise AccountChangedError()
        return self.scope_fingerprint(*identity)

    def _binding_key(self, fingerprint):
        return fingerprint, getattr(self.source, "advisor_binding_generation", 0)

    def _check_identity(self, account, expected=None):
        current = self.source_scope(account)
        if expected is not None and current != expected:
            raise AccountChangedError()
        return current

    def _check_open(self, account, cancel=None, expected=None):
        if self._closed or account in self._paused or (cancel is not None and cancel.is_set()):
            raise AdvisorError("stopping", "已停止")
        return self._check_identity(account, expected)

    def highwater(self, account, user):
        """Read a fixed source highwater with identity checks on both sides."""
        self._check_identity(account)
        try:
            highwater = self.source.history_highwater(user)
        finally:
            self._check_identity(account)
        return highwater

    # -- public API ----------------------------------------------------------
    def observe_window(self, account, workdir, user, messages, has_more_before):
        """Accept only the backend's already-verified decoded latest message window."""
        if type(has_more_before) is not bool or not isinstance(messages, (list, tuple)):
            raise AdvisorError("invalid-request", "会话窗口结构无效")
        scope = (account, os.path.normcase(os.path.realpath(str(workdir))))
        fingerprint = self.scope_fingerprint(*scope)
        rows = self._page_rows(user, messages, 0, trusted_identity=scope)
        positions = [_decode_position(row["sort_key"]) for row in rows]
        if (len({row["msg_id"] for row in rows}) != len(rows) or
                any(previous >= current for previous, current in zip(positions, positions[1:]))):
            raise AdvisorError("context-error", "会话窗口顺序无效")
        observation = {"fingerprint": fingerprint, "rows": rows, "hasMoreBefore": has_more_before,
                       "latest": positions[-1] if positions else None}
        key = (account, user)
        with self.lock:
            if self._closed or account in self._paused:
                return False
            database = self.stores.account(account)
            entry = self._imports.get(key)
            if entry is not None and entry.thread.is_alive():
                self._pending_windows[key] = observation
                self._observed.pop(key, None)
                if database.context(user)["source_id"] != fingerprint:
                    entry.cancel.set()
                return True
            return self._apply_window_locked(account, user, observation)

    def _apply_window_locked(self, account, user, observation, after_import=False):
        key = (account, user)
        database = self.stores.account(account)
        row = database.context(user)
        fingerprint, rows, newest = observation["fingerprint"], observation["rows"], observation["latest"]
        scope_changed = row is not None and row["source_id"] != fingerprint
        previous = _decode_position(row["last_sort"]) if row is not None else None
        try:
            known = database.context_window_matches(user, rows) if row is not None and not scope_changed else set()
            changed_facts = False
        except AdvisorError:
            known, changed_facts = set(), True
        complete = not observation["hasMoreBefore"]
        missing_facts = complete and row is not None and not scope_changed and len(known) != row["read_count"]
        anchored = previous is not None and any(_decode_position(item["sort_key"]) == previous and
                                                item["msg_id"] in known for item in rows)
        prefix_unknown = row is not None and previous is not None and any(
            _decode_position(item["sort_key"]) <= previous and item["msg_id"] not in known for item in rows)
        older_pending = (after_import and not complete and row is not None and row["state"] == "ready" and
                         not scope_changed and not changed_facts and len(known) == len(rows) and
                         (newest is None or _decode_position(row["highwater"]) is not None and
                          newest <= _decode_position(row["highwater"])))
        if older_pending:
            self._observed[key] = self._binding_key(fingerprint)
            self._pending_windows.pop(key, None)
            return True
        rollback = row is not None and previous is not None and (newest is None or newest < previous)
        if complete and (row is None or scope_changed or changed_facts or missing_facts or rollback or prefix_unknown):
            database.context_begin(user, _encode_position(newest), fingerprint, reset=row is not None)
            database.context_append_page(user, rows, _encode_position(newest), _encode_position(newest))
            current = database.context(user)
            database.context_finish(user, current["read_count"])
            self._observed[key] = self._binding_key(fingerprint)
            self._pending_windows.pop(key, None)
            self.condition.notify_all()
            return True
        if (row is not None and row["state"] == "ready" and not scope_changed and not changed_facts and
                not rollback and not prefix_unknown and (anchored or complete)):
            delta = [item for item in rows if item["msg_id"] not in known]
            highwater = newest or _decode_position(row["highwater"])
            if delta:
                database.context_begin(user, _encode_position(highwater), fingerprint)
                database.context_append_page(user, delta, _encode_position(highwater), _encode_position(highwater))
                current = database.context(user)
                database.context_finish(user, current["read_count"])
            self._observed[key] = self._binding_key(fingerprint)
            self._pending_windows.pop(key, None)
            self.condition.notify_all()
            return True
        self._observed.pop(key, None)
        self._pending_windows[key] = observation
        if row is not None:
            # Only an already-managed conversation is repaired proactively; first windows are seeds.
            reset = scope_changed or changed_facts or rollback or prefix_unknown
            ceiling = newest or _decode_position(row["highwater"])
            database.context_begin(user, _encode_position(ceiling), fingerprint, reset=reset)
            self._start_worker(account, user)
        return True

    def invalidate_window(self, account, user):
        with self.lock:
            self._observed.pop((account, user), None)
            self._pending_windows.pop((account, user), None)

    def prepare(self, account, user, cancel=None, expected_scope=None, *, refresh=False, rebuild=False):
        """Start or continue the shared import; never calls a generative model."""
        with self.lock:
            if self._closed or account in self._paused or (cancel is not None and cancel.is_set()):
                raise AdvisorError("stopping", "已停止")
            key = (account, user)
            refresh = bool(refresh or rebuild)
            if refresh:
                # Explicit regeneration must discover its current source cursor,
                # even when the UI previously registered an older ready window.
                self._observed.pop(key, None)
                self._pending_windows.pop(key, None)
            database = self.stores.account(account)
            row = database.context(user)
            worker = self._imports.get(key)
            running = worker is not None and worker.thread.is_alive()
            while refresh and running:
                self.condition.wait(timeout=0.1)
                if self._closed or account in self._paused or (cancel is not None and cancel.is_set()):
                    raise AdvisorError("stopping", "已停止")
                row = database.context(user)
                worker = self._imports.get(key)
                running = worker is not None and worker.thread.is_alive()
            if running:
                return public_context(row)
            if (not refresh and row is not None and row["state"] == "ready" and
                    self._observed.get(key) == self._binding_key(row["source_id"]) and
                    (expected_scope is None or expected_scope == row["source_id"])):
                return public_context(row)
        # Source discovery may block; never hold the projection lock while it runs.
        fingerprint = self._check_open(account, cancel, expected_scope)
        with self.lock:
            pending = self._pending_windows.get(key)
        current = (pending["latest"] if not refresh and pending is not None and pending["fingerprint"] == fingerprint and
                   pending["latest"] is not None else self.highwater(account, user))
        self._check_open(account, cancel, fingerprint)
        with self.lock:
            if self._closed or account in self._paused or (cancel is not None and cancel.is_set()):
                raise AdvisorError("stopping", "已停止")
            row = database.context(user)
            worker = self._imports.get(key)
            if worker is not None and worker.thread.is_alive():
                return public_context(row)
            previous = _decode_position(row["highwater"]) if row else None
            reset = bool(row and (rebuild or row["source_id"] != fingerprint or
                                 (previous is not None and (current is None or current < previous))))
            if reset:
                row = database.context_begin(user, _encode_position(current), fingerprint, reset=True)
            if row is None:
                row = database.context_begin(user, _encode_position(current), fingerprint)
                if current is None:
                    row = database.context_finish(user, 0)
                    return public_context(row)
                self._start_worker(account, user)
                return public_context(database.context(user))
            if row["state"] == "error":
                row = database.context_begin(user, _encode_position(current), fingerprint)
                self._start_worker(account, user)
                return public_context(database.context(user))
            if row["state"] == "ready":
                if _encode_position(current) != row["highwater"]:
                    row = database.context_begin(user, _encode_position(current), fingerprint)
                    self._start_worker(account, user)
                return public_context(database.context(user))
            # reading: resume a worker that did not survive a restart
            if not running:
                self._start_worker(account, user)
            return public_context(database.context(user))

    def wait_ready(self, account, user, cancel, check, expected_scope=None, expected_epoch=None):
        while True:
            check()
            with self.lock:
                if self._closed or account in self._paused or cancel.is_set():
                    raise AdvisorError("stopping", "已停止")
                database = self.stores.account(account)
                row = database.context(user)
                if row is not None and ((expected_scope is not None and row["source_id"] != expected_scope) or
                                        (expected_epoch is not None and row["epoch"] != expected_epoch)):
                    raise AdvisorError("stopping", "聊天数据来源已经变化")
                if row is not None and row["state"] == "ready":
                    result = public_context(row)
                    result["sourceFingerprint"] = row["source_id"]
                    result["epoch"] = row["epoch"]
                    return result
                if row is not None and row["state"] == "error":
                    raise AdvisorError("context-error", "聊天记录读取失败，请重试")
                self.condition.wait(timeout=0.1)

    def importing(self, account, user):
        with self.lock:
            worker = self._imports.get((account, user))
            return worker is not None and worker.thread.is_alive()

    def recent_rows(self, account, user, limit, cancel=None, expected_scope=None):
        """The source's latest window with the same identity checks as the import; nothing is stored.

        Know Your Friends, 2026-10-12 (Apache-2.0): lets 知己分析 summarise recent
        conversations before a first full-history import has finished.
        """
        fingerprint = self._check_open(account, cancel, expected_scope)
        try:
            window = self.source.messages(user, limit)
        finally:
            self._check_identity(account, fingerprint)
        rows = self._page_rows(user, list(window), 0)
        positions = [_decode_position(row["sort_key"]) for row in rows]
        if any(previous >= current for previous, current in zip(positions, positions[1:])):
            raise AdvisorError("context-error", "会话窗口顺序无效")
        return fingerprint, rows, bool(getattr(window, "has_more_before", False))

    def snapshot(self, account, user):
        database = self.stores.account(account)
        return public_context(database.context(user))

    def messages(self, account, user, upto_seq=None):
        database = self.stores.account(account)
        return database.context_messages(user, upto_seq)

    def cancel_account(self, account):
        """Signal and join every import writer for one account; True when drained."""
        with self.lock:
            self._paused.add(account)
            for key in [key for key in self._observed if key[0] == account]:
                self._observed.pop(key, None)
            for key in [key for key in self._pending_windows if key[0] == account]:
                self._pending_windows.pop(key, None)
            entries = [entry for (owner, _user), entry in self._imports.items() if owner == account]
        for entry in entries:
            entry.cancel.set()
        with self.condition:
            self.condition.notify_all()
        deadline = time.monotonic() + 200
        drained = True
        for entry in entries:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                drained = False
                break
            entry.thread.join(remaining)
            if entry.thread.is_alive():
                drained = False
        return drained

    def resume_account(self, account):
        with self.lock:
            if not self._closed:
                self._paused.discard(account)

    def close(self):
        with self.lock:
            self._closed = True
            entries = list(self._imports.values())
        for entry in entries:
            entry.cancel.set()
        with self.condition:
            self.condition.notify_all()
        deadline = time.monotonic() + 200
        for entry in entries:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            entry.thread.join(remaining)
        if any(entry.thread.is_alive() for entry in entries):
            raise RuntimeError("context import did not finish")
        with self.lock:
            self._imports.clear()
            self._observed.clear()
            self._pending_windows.clear()

    # -- import worker -------------------------------------------------------
    def _start_worker(self, account, user):
        key = (account, user)
        worker = self._imports.get(key)
        if worker is not None and worker.thread.is_alive():
            return worker
        cancel = threading.Event()
        thread = threading.Thread(target=self._import_worker, args=(account, user, cancel),
                                  name="context-import", daemon=True)
        entry = _Import(cancel, thread)
        self._imports[key] = entry
        thread.start()
        return entry

    def _import_worker(self, account, user, cancel):
        key = (account, user)
        database = self.stores.account(account)
        try:
            while not cancel.is_set():
                row = database.context(user)
                if row is None or row["state"] != "reading":
                    return
                highwater = _decode_position(row["highwater"])
                expected = row["source_id"]
                if highwater is None:
                    with self.lock:
                        self._check_open(account, cancel, expected)
                        database.context_finish(user, row["read_count"])
                    return
                after = _decode_position(row["cursor"]) or _decode_position(row["last_sort"])
                self._check_open(account, cancel, expected)
                try:
                    page, next_after = self.source.history_page(
                        user, highwater, after, page_size=IMPORT_PAGE_SIZE)
                finally:
                    self._check_identity(account, expected)
                if cancel.is_set():
                    return
                if page:
                    rows = self._page_rows(user, page, row["read_count"])
                    last_sort = rows[-1]["sort_key"] if rows else row["last_sort"]
                else:
                    rows, last_sort = [], row["last_sort"]
                if next_after is not None and after is not None and tuple(next_after) <= tuple(after):
                    raise AdvisorError("context-error", "历史读取游标未推进")
                with self.lock:
                    source_lock = getattr(self.source, "lock", None)
                    with source_lock if source_lock is not None else nullcontext():
                        self._check_open(account, cancel, expected)
                        database.context_append_page(user, rows, _encode_position(next_after),
                                                     last_sort)
                        if next_after is None:
                            current = database.context(user)
                            database.context_finish(user, current["read_count"])
                            return
                        self.condition.notify_all()
        except AccountChangedError:
            if not cancel.is_set():
                database.context_error(user, "当前微信账号或数据来源已变化，读取已停止")
        except (AccountUnavailableError, MessagesUnavailableError):
            if not cancel.is_set():
                database.context_error(user, "当前微信记录暂不可用")
        except Exception as exc:  # noqa: BLE001 - the state must become visible, never silent
            if not cancel.is_set():
                database.context_error(user, _friendly_error(exc))
        finally:
            with self.lock:
                entry = self._imports.get(key)
                if entry is not None and entry.thread is threading.current_thread():
                    del self._imports[key]
                pending = self._pending_windows.get(key)
                current = database.context(user)
                if (pending is not None and not cancel.is_set() and not self._closed and account not in self._paused and
                        current is not None and current["state"] == "ready" and
                        current["source_id"] == pending["fingerprint"]):
                    self._apply_window_locked(account, user, pending, after_import=True)
                self.condition.notify_all()

    def _page_rows(self, user, page, read_count, trusted_identity=None):
        group_chat = user.endswith("@chatroom")
        rows = []
        scope = trusted_identity or self._identity()
        for index, message in enumerate(page):
            if not isinstance(message, dict):
                raise AdvisorError("context-error", "历史消息结构无效")
            sort = message.get("_sort")
            cursor = message.get("historyCursor")
            try:
                cursor_sort = decode_cursor(cursor, scope[0], user) if cursor is not None else None
            except ValueError as exc:
                raise AdvisorError("context-error", "历史消息游标不属于当前会话") from exc
            if sort is None and cursor_sort is not None:
                sort = list(cursor_sort)
            if (not isinstance(sort, (list, tuple)) or len(sort) != 3 or
                    type(sort[0]) is not int or not 0 <= sort[0] <= 2**63 - 1 or
                    not isinstance(sort[1], str) or type(sort[2]) is not int or not 0 <= sort[2] <= 2**63 - 1 or
                    not isinstance(message.get("id"), str) or not message.get("id") or
                    (cursor_sort is not None and tuple(sort) != cursor_sort)):
                raise AdvisorError("context-error", "历史消息身份无效")
            sort = list(sort)
            text = message.get("text")
            if not isinstance(text, str):
                raise AdvisorError("context-error", "历史文本结构无效")
            unified = message_input.prepare_item(message, account_id=scope[0], conversation_id=user,
                                                 source_kind="wechat")
            side = message["side"]
            sender_id = unified["inputMeta"].get("senderId") or ""
            sender_name = unified["inputMeta"].get("senderName") or ""
            kind = _bounded(message.get("kind"), 16)
            time_ms = unified["inputMeta"].get("sentAtMs")
            raw = {
                "_sort": sort,
                "historyCursor": _bounded(message.get("historyCursor"), MAX_RAW_FIELD_CHARS),
                "type": _bounded(message.get("type"), 64),
                "inputMeta": unified["inputMeta"],
            }
            media = media_metadata(message.get("media"))
            if media is not None:
                raw["media"] = media
            rows.append({
                "seq": read_count + index + 1,
                "msg_id": message["id"],
                "sort_key": json.dumps(sort, ensure_ascii=False, separators=(",", ":")),
                "group_chat": 1 if group_chat else 0,
                "side": side,
                "sender_id": sender_id,
                "sender_name": sender_name,
                "kind": kind,
                "text": text,
                "time_ms": time_ms,
                "raw": json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
            })
        return rows


def _bounded(value, maximum):
    if not isinstance(value, str):
        return ""
    return value[:maximum]


def media_metadata(value):
    """Persist only bounded, known metadata; never a local path or media bytes."""
    if not isinstance(value, dict) or value.get("kind") not in ("image", "sticker", "video", "voice", "audio"):
        return None
    result = {"kind": value["kind"]}
    for key in ("durationMs", "width", "height"):
        number = value.get(key)
        if type(number) in (int, float) and 0 <= number <= 86_400_000:
            result[key] = number
    if value.get("transcriptSource") == "wechat" and isinstance(value.get("transcript"), str):
        result.update(transcript=value["transcript"][:8000], transcriptSource="wechat")
    return result
