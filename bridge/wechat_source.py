"""Read-only WeChat adapter: account identity, snapshots, messages, and media.

No inference scheduling or analysis-cache writes belong in this module.
Modified by Know Your Friends, 2026-10-10: safe live startup diagnostics.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree

from backend_contracts import (
    AccountChangedError, AccountPreparationPending, AccountUnavailableError, MAX_ISSUED_IMAGES,
    MessageWindow, MessageWindowBatch, MessagesUnavailableError,
    PROFILE_METADATA_CACHE_LIMIT, QUOTED_REPLY_TYPE, ROOT, SESSION_PREVIEWS, SYSTEM_NAMES,
)
from history_browser import encode_cursor
from media_assets import (
    LocalMedia, MediaUnavailable, clear_owned_media, media_kind, media_metadata, sticker_identity,
)
from message_links import link_metadata

# Official accounts, service notifications and fold placeholders carry no conversation
# worth analysing or listing, so a whole-account sidebar leaves them out. Real contacts,
# group chats and filehelper/openim are kept.
SERVICE_ACCOUNT_IDS = frozenset({
    "brandsessionholder", "brandservicesessionholder", "notifymessage", "weixin",
    "opencustomerservicemsg", "newsapp", "weixinliteservice", "weixinreminder",
    "fmessage", "floatbottle", "medianote", "qqmail", "voiceinput", "exmail_tool",
})
SERVICE_ACCOUNT_SUFFIXES = frozenset({"placeholder_foldgroup", "weclaw", "kefu.openim"})
ADVISOR_IDENTITY_LEASE_SECONDS = 2.0


class _SourceLock:
    """Reentrant source lock that remembers when each thread asked for its outermost hold.

    Know Your Friends, 2026-10-23 (Apache-2.0): the first reader check of a hold
    may share an ownership query that started after that request. None of this
    thread's source reads can be newer than the request, so queued callers share
    one Windows query instead of each paying for their own.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._local = threading.local()

    def acquire(self, blocking=True, timeout=-1):
        local = self._local
        depth = getattr(local, "depth", 0)
        requested = time.monotonic()
        if not self._lock.acquire(blocking, timeout):
            return False
        if depth == 0:
            local.requested, local.resolved = requested, False
        local.depth = depth + 1
        return True

    def release(self):
        self._local.depth -= 1
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()

    def _is_owned(self):
        return self._lock._is_owned()

    def first_check_since(self):
        """The request time for this hold's first reader check, then None for later (post-read) checks."""
        local = self._local
        if getattr(local, "depth", 0) and not local.resolved:
            local.resolved = True
            return local.requested
        return None


def _wechat_process_running():
    """Read process names only; an incomplete query cannot prove WeChat is closed."""
    try:
        import psutil
        unknown = False
        for process in psutil.process_iter(["name"]):
            name = process.info.get("name")
            if not isinstance(name, str) or not name:
                unknown = True
            elif name.lower() in ("weixin.exe", "wechat.exe"):
                return True
        return None if unknown else False
    except Exception:
        # Optional dependency, permissions and process-query failures are not login state.
        return None


def is_service_account(user):
    """Official accounts and system rows are not conversations."""
    if user.startswith("gh_"):
        return True
    base, _, suffix = user.partition("@")
    return base.lower() in SERVICE_ACCOUNT_IDS or suffix.lower() in SERVICE_ACCOUNT_SUFFIXES


def active_account_dir():
    """Resolve a unique live account from WeChat's open-file metadata, never DB mtimes."""
    native_reader = str(ROOT / "native-reader")
    if native_reader not in sys.path:
        sys.path.insert(0, native_reader)
    from wr import discovery

    accounts = discovery.discover_account_dirs()
    roots = [(account, os.path.normcase(os.path.realpath(account.path))) for account in accounts]
    matches = set()
    for process in discovery.find_weixin_processes():
        for path in discovery.process_open_file_paths(process.pid):
            opened = os.path.normcase(os.path.realpath(path))
            for account, root in roots:
                if opened == root or opened.startswith(root + os.sep):
                    matches.add(account.id)
    match = next((account for account in accounts if account.id in matches), None)
    return Path(match.path).parent.resolve() if len(matches) == 1 and match else None


def positive_timestamp(value):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return (number * 1000 if number < 10_000_000_000 else number) if number > 0 else None


def session_preview(summary, message_type, sub_type=None):
    if isinstance(message_type, int):
        kind = message_type & 0xFFFFFFFF if message_type > 0xFFFF else message_type
        if kind == 49 and sub_type in (5, 6):
            return "[链接]" if sub_type == 5 else "[文件]"
        if kind in SESSION_PREVIEWS:
            return SESSION_PREVIEWS[kind]
    content = summary.strip() if isinstance(summary, str) else ""
    return "[消息]" if content.startswith("<") else content


def avatar_candidates(*urls):
    candidates = []
    for url in urls:
        if not isinstance(url, str) or not url or url != url.strip():
            continue
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if (parsed.scheme.lower() in ("http", "https") and parsed.hostname and
                not parsed.username and not parsed.password and url not in candidates):
            candidates.append(url)
    return candidates


def contact_display(contacts, user):
    contact = contacts.get(user)
    if contact:
        return {**contact, "name": SYSTEM_NAMES.get(user, user) if contact["name"] == user else contact["name"]}
    return {"name": SYSTEM_NAMES.get(user, user), "avatar": "", "avatarCandidates": []}


def message_id(account, user, shard, row):
    identity = json.dumps([account, user, shard, row["local_id"], row["sort_seq"],
                           row["server_id"]], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class WeChatSource:
    def __init__(self, factory=None, classifier=None, media_factory=None, active_account_locator=None,
                 media_storage_root=None):
        self.factory = factory
        self.classifier = classifier
        self.media_factory = media_factory
        # Injected legacy factories remain fixed for synthetic tests. Production always resolves
        # the unique live account before accessing a cached reader.
        self.live_account = factory is None and active_account_locator is None
        self.dynamic_account = factory is None or active_account_locator is not None
        self._ownership_cache_enabled = self.live_account
        if self.live_account:
            from live_source import active_account_snapshot
            self.active_account_locator = active_account_snapshot
        else:
            self.active_account_locator = active_account_locator or active_account_dir
        self.lock = _SourceLock()
        self._proved_at = None
        self.closed = False
        self.db = None
        self._account_dir = None
        self._active_checked_at = 0.0
        self._request_context = threading.local()
        self._advisor_identity_lock = threading.Lock()
        self._advisor_verification_lock = threading.Lock()
        self._advisor_binding = None
        self._advisor_binding_generation = 0
        self._self_username = None
        self.issued_images = OrderedDict()
        self.window_images = {}
        self.profile_metadata_cache = OrderedDict()
        self.profile_overview_counts_cache = OrderedDict()
        self.media_reason = threading.local()
        self._local_media_state = None
        self._media_storage_root = Path(media_storage_root) if media_storage_root else ROOT / ".local" / "media-data"
        self._media_engines = {}
        self._blocked_media_accounts = set()

    def preparation_status(self, account=None):
        """Explain an unavailable live account without starting ownership or DB scans."""
        running = _wechat_process_running() if self.live_account else None
        if running is False:
            return {"reason": "wechat_not_running", "state": "unavailable",
                    "message": "未检测到正在运行的微信，请先打开电脑版微信并登录。"}
        status = getattr(self.factory, "preparation_status", None)
        if callable(status):
            try:
                result = status(account)
            except Exception:
                result = None
            if isinstance(result, dict) and result:
                return result.copy()
        if not self.live_account:
            return None
        message = ("微信已运行，但暂时无法确认当前账号，请确认电脑版微信已完成登录。"
                   if running is True else
                   "暂时无法确认当前微信账号，请确认电脑版微信已打开并完成登录。")
        return {"reason": "active_account_unavailable", "state": "unavailable", "message": message}

    def _release_db(self, *, reset_ownership=True):
        self.invalidate_advisor_identity(reset_ownership=reset_ownership)
        self.db = None
        self._account_dir = None
        self._active_checked_at = 0.0
        self._self_username = None
        self.issued_images.clear()
        self.window_images.clear()
        self._local_media_state = None
        self.profile_metadata_cache.clear()

    def _active_selection(self, *, fresh=True, since=None):
        from live_source import active_account_snapshot
        if self.live_account and self.active_account_locator is active_account_snapshot:
            return self.active_account_locator(fresh=fresh, **({"since": since} if since is not None else {}))
        return self.active_account_locator()

    def _ordinary_db(self):
        if getattr(self, "_ownership_cache_enabled", False):
            return self._db(fresh=True, reuse_ownership=True)
        return self._db(fresh=True)

    @contextmanager
    def request_scope(self):
        """Keep one reader for an HTTP request, including its final scope checks."""
        context = self._request_context
        previous = (getattr(context, "active", False), getattr(context, "reader", None))
        if not previous[0]:
            context.active, context.reader = True, None
        try:
            yield
        finally:
            context.active, context.reader = previous

    def _live_reader_valid(self, db, selection):
        from live_source import _pages, _readiness, _token, _valid_page_key
        from cache_source import resolve_anchor_rels

        pages = _pages(selection.account_dir)
        sessions_ready, messages_ready = _readiness(pages, getattr(db, "_keys", {}))
        token = _token(selection, pages, anchors_only=not db.messages_ready)
        validated = {rel for rel, raw in getattr(db, "_keys", {}).items()
                     if rel in pages and _valid_page_key(raw, pages[rel][2])}
        valid = (getattr(db, "_live_selection", None) == selection and
                 getattr(db, "_live_token", None) == token and
                 getattr(db, "anchor_rels", None) == resolve_anchor_rels(pages, validated) and
                 sessions_ready and (not db.messages_ready or messages_ready))
        return valid, token

    def require_messages_ready(self):
        if not self.live_account:
            return
        with self.lock:
            if not self._db(fresh=True).messages_ready:
                raise MessagesUnavailableError()

    def forget_account(self, account):
        """Drop only this account's open reader and volatile key references."""
        with self._advisor_identity_lock:
            binding = self._advisor_binding
            if binding is not None and binding[0] == str(account):
                self._advisor_binding = None
                self._advisor_binding_generation += 1
        with self.lock:
            for key in list(self.profile_overview_counts_cache):
                if key[0] == account:
                    del self.profile_overview_counts_cache[key]
            if self.db is not None and str(self.db.account) == account:
                for name in ("_keys", "_volatile_keys"):
                    value = getattr(self.db, name, None)
                    if isinstance(value, dict):
                        value.clear()
                if hasattr(self.db, "master_key"):
                    self.db.master_key = None
                self._release_db()
            forget = getattr(self.factory, "forget_account", None)
            if callable(forget):
                forget(account)

    def close(self):
        """Stop this bridge instance from reopening any WeChat-derived snapshot."""
        with self._advisor_identity_lock:
            self.closed = True
            self._advisor_binding = None
            self._advisor_binding_generation += 1
        with self.lock:
            self.closed = True
            if self.db is not None:
                self.forget_account(str(self.db.account))
            self._release_db()
            self.profile_overview_counts_cache.clear()

    def _db(self, fresh=False, reuse_ownership=False):
        with self.lock:
            # Only a hold's first check may share a query started after the hold was
            # requested; a later check in the same hold follows reads and stays strict.
            since = self.lock.first_check_since() if isinstance(self.lock, _SourceLock) else None
            self._proved_at = None
            if self.closed:
                raise AccountUnavailableError()
            if not self.dynamic_account and self.db is not None:
                return self.db
            if self.dynamic_account:
                if (self.db is not None and not self.live_account and not fresh and
                        time.monotonic() - self._active_checked_at < 0.5):
                    return self.db
                try:
                    selection = self._active_selection(fresh=not reuse_ownership, since=since)
                    self._proved_at = getattr(selection, "proved_at", None)
                    location = (selection.account_dir if self.live_account and selection is not None
                                else Path(selection).resolve() if selection is not None else None)
                except Exception as exc:
                    self._release_db()
                    raise AccountUnavailableError() from exc
                if location is None or not (location / "db_storage").is_dir():
                    self._release_db()
                    raise AccountUnavailableError()
                pinned = getattr(self._request_context, "reader", None)
                if self.live_account and pinned is not None:
                    try:
                        valid, _token_now = self._live_reader_valid(pinned, selection)
                    except Exception as exc:
                        raise AccountUnavailableError() from exc
                    if not valid or Path(pinned.account_dir).resolve() != location:
                        raise AccountUnavailableError()
                    return pinned
                if self.db is not None and self._account_dir != location:
                    self._release_db()
                if self.live_account and self.db is not None:
                    try:
                        valid, _token_now = self._live_reader_valid(self.db, selection)
                    except Exception:
                        valid = False
                    if not valid:
                        self._release_db()
                if self.factory is None:
                    if self.live_account:
                        from live_source import LiveWeChatFactory
                        self.factory = LiveWeChatFactory()
                    else:
                        from cache_source import CacheOnlyWeChatDB
                        self.factory = CacheOnlyWeChatDB
                if self.db is not None:
                    if self.live_account and not self.db.messages_ready:
                        try:
                            candidate = self.factory(db_dir=str(location.parent), account=location.name,
                                                     selection=selection)
                        except Exception:
                            candidate = None
                        if candidate is not None and candidate.messages_ready:
                            if self._active_selection() != selection:
                                raise AccountUnavailableError()
                            self._release_db()
                            self.db = candidate
                            self._account_dir = location
                            from live_source import _pages, _token
                            candidate._live_selection = selection
                            candidate._live_token = _token(selection, _pages(selection.account_dir),
                                                           anchors_only=not candidate.messages_ready)
                            valid, _current_token = self._live_reader_valid(candidate, selection)
                            if not valid:
                                self._release_db()
                                raise AccountUnavailableError()
                    self._active_checked_at = time.monotonic()
                    if self.live_account and getattr(self._request_context, "active", False):
                        self._request_context.reader = self.db
                    return self.db
            if self.factory is None:
                if self.live_account:
                    from live_source import LiveWeChatFactory
                    self.factory = LiveWeChatFactory()
                else:
                    from cache_source import CacheOnlyWeChatDB
                    self.factory = CacheOnlyWeChatDB
            try:
                if self.live_account:
                    db = self.factory(db_dir=str(location.parent), account=location.name,
                                      selection=selection)
                elif self.dynamic_account:
                    db = self.factory(db_dir=str(location.parent), account=location.name)
                else:
                    db = self.factory()
                if self.dynamic_account and (str(db.account) != location.name or
                                             Path(db.account_dir).resolve() != location):
                    raise AccountUnavailableError()
                if self.live_account and self._active_selection() != selection:
                    raise AccountUnavailableError()
                if self.live_account:
                    from live_source import _pages, _readiness, _token
                    pages = _pages(selection.account_dir)
                    sessions_ready, messages_ready = _readiness(pages, db._keys)
                    if not sessions_ready or db.messages_ready and not messages_ready:
                        raise AccountUnavailableError()
                    page_token = _token(selection, pages, anchors_only=not db.messages_ready)
            except Exception as exc:
                # Waiting for the scanner is not an account change. Invalidating its
                # in-flight ownership check here would discard fully prepared keys.
                self._release_db(reset_ownership=not isinstance(exc, AccountPreparationPending))
                if self.dynamic_account:
                    raise AccountUnavailableError() from exc
                raise
            self.db = db
            self._account_dir = location if self.dynamic_account else None
            self._active_checked_at = time.monotonic()
            if self.live_account:
                db._live_selection = selection
                db._live_token = page_token
                if getattr(self._request_context, "active", False):
                    self._request_context.reader = db
            return db

    def identity(self):
        return self.verified_identity()

    def verified_identity(self, *, messages=False):
        with self.lock:
            try:
                db = self._db(fresh=True)
                proved_at = self._proved_at
                ready = not self.live_account or bool(db.messages_ready)
                if messages and not ready:
                    raise MessagesUnavailableError()
                account = str(db.account)
                if not account or not getattr(db, "workdir", None):
                    raise RuntimeError("WeChat account/workdir unavailable")
                workdir = Path(db.workdir).resolve()
            except Exception as exc:
                # A session-only reader still proves the current account. Message
                # readiness polling must not cancel its background scan's final check.
                pending = (isinstance(exc, MessagesUnavailableError) or
                           isinstance(exc, AccountUnavailableError) and
                           isinstance(exc.__cause__, AccountPreparationPending))
                self.invalidate_advisor_identity(reset_ownership=not pending)
                raise
            self._remember_advisor_binding(db, account, workdir, ready, proved_at)
            return account, workdir

    def _remember_advisor_binding(self, db, account, workdir, ready, proved_at=None):
        # A shared ownership answer starts the lease when it finished, never later,
        # so sharing a query cannot stretch how long one proof is trusted.
        now = time.monotonic()
        stamp = now if proved_at is None else min(now, proved_at)
        with self._advisor_identity_lock:
            if not self.closed and self.db is db:
                previous = self._advisor_binding
                if previous is None or previous[:4] != (account, workdir, db, ready):
                    self._advisor_binding_generation += 1
                self._advisor_binding = (account, workdir, db, ready, stamp)

    def invalidate_advisor_identity(self, *, reset_ownership=True):
        """Invalidate the Advisor lease before releasing or reconfiguring this reader."""
        with self._advisor_identity_lock:
            self._advisor_binding = None
            self._advisor_binding_generation += 1
        if reset_ownership:
            from live_source import reset_ownership_cache
            reset_ownership_cache()

    @property
    def advisor_binding_generation(self):
        with self._advisor_identity_lock:
            return self._advisor_binding_generation

    def _leased_advisor_identity(self, messages):
        with self._advisor_identity_lock:
            if self.closed:
                raise AccountUnavailableError()
            binding = self._advisor_binding
            if binding is None:
                return None
            account, workdir, reader, ready, completed_at = binding
            if self.db is not reader or (self.live_account and ready != bool(getattr(reader, "messages_ready", False))):
                self._advisor_binding = None
                self._advisor_binding_generation += 1
                return None
            if time.monotonic() - completed_at >= ADVISOR_IDENTITY_LEASE_SECONDS:
                return None
            if messages and not ready:
                raise MessagesUnavailableError()
            return account, workdir

    def advisor_identity(self, *, messages=True):
        """Reuse only a recently verified binding; the default identity API stays fresh."""
        cached = self._leased_advisor_identity(messages)
        if cached is not None:
            return cached
        # Modified by Know Your Friends, 2026-10-10: fixed lock order.
        # Lock order is single-flight lock, then source lock. A caller already
        # holding the reentrant source lock (context import) verifies directly;
        # taking the single-flight lock here would deadlock against a thread
        # that holds it and waits for the source lock in verified_identity.
        if self.lock._is_owned():
            return self.verified_identity(messages=messages)
        # Single-flight expired bindings without holding the small lease lock during I/O.
        with self._advisor_verification_lock:
            cached = self._leased_advisor_identity(messages)
            if cached is not None:
                return cached
            return self.verified_identity(messages=messages)

    def self_user(self, db=None):
        with self.lock:
            standalone = db is None
            db = self._db(fresh=True) if standalone else db
            if self._self_username is None:
                self._self_username = db.get_self_info().get("username") or db.wxid
            if standalone and self._db(fresh=True) is not db:
                raise AccountChangedError()
            return self._self_username

    def _contacts(self, db):
        for rel, path, _ in db._db_files:
            if self.live_account and rel != db.anchor_rels["contact"]:
                continue
            if not self.live_account and Path(path).name != "contact.db":
                continue
            conn = db._open(rel)
            try:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(contact)")}
                fields = [field if field in columns else "''" for field in
                          ("username", "nick_name", "remark", "small_head_url", "big_head_url")]
                rows = conn.execute("SELECT " + ",".join(fields) + " FROM contact")
                contacts = {}
                for user, nick, remark, small, big in rows:
                    if user:
                        candidates = avatar_candidates(small, big)
                        name = (remark if remark and remark != user else
                                nick if nick and nick != user else SYSTEM_NAMES.get(user, user))
                        contacts[str(user)] = {"name": name,
                                               "avatar": candidates[0] if candidates else "",
                                               "avatarCandidates": candidates}
                return contacts
            finally:
                conn.close()
        raise RuntimeError("contact.db unavailable")

    def sessions(self):
        with self.lock:
            db = self._ordinary_db()
            contacts = self._contacts(db)
            self_user = self.self_user(db)
            self_contact = contact_display(contacts, self_user)
            items = []
            for rel, path, _ in db._db_files:
                if self.live_account and rel != db.anchor_rels["session"]:
                    continue
                if not self.live_account and Path(path).name != "session.db":
                    continue
                conn = db._open(rel)
                try:
                    columns = {row[1] for row in conn.execute("PRAGMA table_info(SessionTable)")}
                    required = {"username", "unread_count", "summary", "last_timestamp", "last_msg_sender",
                                "last_sender_display_name", "sort_timestamp", "is_hidden"}
                    if not required <= columns:
                        raise RuntimeError("unsupported session table schema")
                    type_column = next((name for name in ("last_msg_type", "last_message_type") if name in columns), None)
                    subtype_column = "last_msg_sub_type" if "last_msg_sub_type" in columns else None
                    fields = ("username,unread_count,summary,last_timestamp,last_msg_sender,"
                              "last_sender_display_name,sort_timestamp," +
                              (type_column if type_column else "NULL") + "," +
                              (subtype_column if subtype_column else "NULL") + "," +
                              ("is_top" if "is_top" in columns else "NULL"))
                    rows = conn.execute("SELECT " + fields + " FROM SessionTable WHERE is_hidden=0 "
                                        "ORDER BY sort_timestamp DESC,rowid DESC")
                    while batch := rows.fetchmany(256):
                        for user, unread, summary, last_time, sender, sender_name, sort_time, message_type, sub_type, is_top in batch:
                            if not isinstance(user, str) or not user or is_service_account(user):
                                continue
                            contact = contact_display(contacts, user)
                            try:
                                unread_count = max(0, int(unread or 0))
                            except (TypeError, ValueError, OverflowError):
                                unread_count = 0
                            items.append({"username": user, **contact,
                                          "preview": session_preview(summary, message_type, sub_type),
                                          "time": positive_timestamp(last_time),
                                          "sortTimestamp": int(sort_time) if isinstance(sort_time, (int, float)) and sort_time > 0 else None,
                                          "unreadCount": unread_count, "lastMsgType": message_type,
                                          "lastMsgSubType": sub_type,
                                          "pinned": bool(is_top) if is_top in (0, 1) else None,
                                          "lastSender": sender_name or sender or "",
                                          "lastMessageFromSelf": sender == self_user if isinstance(sender, str) and sender else None,
                                          "isGroup": user.endswith("@chatroom")})
                finally:
                    conn.close()
                break
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
            return {"self": {"username": self_user, **self_contact}, "sessions": items,
                    "account": str(db.account), "messagesReady": bool(getattr(db, "messages_ready", True))}

    def history_conversations(self):
        """Discover message-backed private and group chats, including hidden/unselected ones.

        SessionTable is a UI view, not an inventory of retained history. Resolve
        actual Msg tables through Name2Id and contact metadata instead; report
        unreadable shards and unresolved table names rather than claiming coverage.
        No message body is read by this discovery operation. Each chat carries
        ``latest``, its newest row position in the same form as ``history_highwater``,
        so a caller can tell an unchanged chat without preparing it.
        """
        with self.lock:
            db = self._ordinary_db()
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            contacts = self._contacts(db)
            own_user = self.self_user(db)
            names = set(contacts)
            shards, failed, readable = [], [], 0
            for rel, _path, _size in db._db_files:
                normalized = rel.replace("\\", "/")
                if not re.fullmatch(r"message/message_\d+\.db", normalized):
                    continue
                connection = None
                try:
                    connection = db._open(rel)
                    tables = {row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")
                        if isinstance(row[0], str) and re.fullmatch(r"Msg_[0-9a-fA-F]{32}", row[0])}
                    names.update(row[0] for row in connection.execute("SELECT user_name FROM Name2Id")
                                 if isinstance(row[0], str))
                    # Positions are named by snapshot file, exactly like history highwaters.
                    shard = Path(connection.execute("PRAGMA database_list").fetchone()[2]).name
                    if not re.fullmatch(r"message__message_\d+\.db", shard):
                        shard = None
                    nonempty = {}
                    for table in tables:
                        try:
                            row = connection.execute(f'SELECT sort_seq,local_id FROM "{table}" '
                                                     "ORDER BY sort_seq DESC,local_id DESC LIMIT 1").fetchone()
                        except sqlite3.OperationalError:
                            row = connection.execute(f'SELECT NULL,NULL FROM "{table}" LIMIT 1').fetchone()
                        if row is not None:
                            nonempty[table.lower()] = ((int(row[0]), shard, int(row[1])) if shard and
                                                       type(row[0]) is int and type(row[1]) is int else None)
                    shards.append((normalized, nonempty))
                    readable += 1
                except (AccountChangedError, AccountUnavailableError, MessagesUnavailableError):
                    raise
                except Exception:
                    failed.append({"shard": normalized, "error": "unreadable"})
                finally:
                    if connection is not None:
                        connection.close()
            mapping = {"msg_" + hashlib.md5(user.encode("utf-8")).hexdigest(): user for user in names
                       if user and len(user) <= 128 and not re.search(r"[\x00-\x1f/\\]", user)}
            found, latest, unresolved = set(), {}, 0
            for _rel, tables in shards:
                for table, position in tables.items():
                    user = mapping.get(table)
                    if user is None:
                        unresolved += 1
                    elif user != own_user and user != "filehelper" and not is_service_account(user):
                        found.add(user)
                        # Newest across shards in history-page order; unknown in any shard stays unknown.
                        previous = latest.get(user, ())
                        latest[user] = None if position is None or previous is None else max(previous, position)
            if failed:
                latest = {}  # an unreadable shard may hold a newer row
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
            return {"account": str(db.account), "conversations": [
                {"user": user, "name": contact_display(contacts, user)["name"], "isGroup": user.endswith("@chatroom"),
                 "latest": list(latest[user]) if latest.get(user) else None}
                for user in sorted(found)], "coverage": {
                    "state": "partial" if failed or unresolved or not readable else "complete",
                    "readableShards": readable, "failedShards": failed, "unresolvedTables": unresolved}}

    def _shard_rows(self, db, user, limit, offset=0):
        found = db._msg_conns(user)
        rows = []
        try:
            for conn, table in found:
                if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                    raise RuntimeError("invalid message table")
                source_path = conn.execute("PRAGMA database_list").fetchone()[2]
                shard = Path(source_path).name
                if not shard.startswith("message__message_") or not shard.endswith(".db"):
                    raise RuntimeError("unidentified message shard")
                sender_index = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                records = conn.execute(
                    f"SELECT local_id,local_type,real_sender_id,create_time,message_content,compress_content,server_id,sort_seq FROM {table} "
                    "ORDER BY sort_seq DESC,local_id DESC LIMIT ?", (limit + offset,))
                for record in records:
                    rows.append((record, shard, sender_index))
        finally:
            for conn in {id(conn): conn for conn, _ in found}.values():
                conn.close()
        rows.sort(key=lambda item: (int(item[0][7]), item[1], int(item[0][0])), reverse=True)
        return rows[offset:offset + limit]

    @staticmethod
    def _quoted_reply_title(content):
        if not isinstance(content, str):
            return None
        start = content.find("<msg")
        if start > 0:
            content = content[start:]
        try:
            root = ElementTree.fromstring(content.strip())
        except ElementTree.ParseError:
            return None
        appmsg = root if root.tag == "appmsg" else root.find("./appmsg")
        if appmsg is None or appmsg.find("./refermsg") is None:
            return None
        subtype = appmsg.find("./type")
        if subtype is not None and (subtype.text or "").strip() != "57":
            return None
        title = appmsg.find("./title")
        text = "".join(title.itertext()).strip() if title is not None else ""
        return text if text and text not in ("[应用消息]", "[消息]", "[图片]", "[表情]") else None

    @staticmethod
    def _decoded_content(db, local_type, content, compressed):
        kind_name = db._msg_type_name(local_type)
        if isinstance(content, bytes):
            content = db._friendly_content(content, kind_name)
        if not content or content == f"[{kind_name}]":
            if isinstance(compressed, bytes) and compressed:
                content = db._friendly_content(compressed, kind_name)
        return kind_name, content

    def _classified_content(self, db, local_type, content, compressed):
        kind_name, content = self._decoded_content(db, local_type, content, compressed)
        quote = self._quoted_reply_title(content) if local_type == QUOTED_REPLY_TYPE else None
        kind, text = ("text", quote) if quote is not None else self.classifier(
            kind_name, content or f"[{kind_name}]")
        return kind_name, kind, text

    def _analyzable_counts(self, db, conn, table, sender_ids=None):
        """Count exactly the rendered text that portrait scanning can consume."""
        where = "local_type IN (?,?)"
        args = [1, QUOTED_REPLY_TYPE]
        if sender_ids is not None:
            if not sender_ids:
                return {}
            where += " AND real_sender_id IN (" + ",".join("?" for _ in sender_ids) + ")"
            args.extend(sender_ids)
        counts = {}
        for sender_id, local_type, content, compressed in conn.execute(
                f"SELECT real_sender_id,local_type,message_content,compress_content FROM {table} WHERE {where}", args):
            _name, kind, text = self._classified_content(db, local_type, content, compressed)
            if kind == "text" and isinstance(text, str) and text.strip():
                counts[sender_id] = counts.get(sender_id, 0) + 1
        return counts

    def _render_row(self, db, user, item, contacts, own_user):
        record, shard, senders = item
        local_id, local_type, sender_id, created, content, compressed, server_id, seq = record
        kind_name, kind, text = self._classified_content(db, local_type, content, compressed)
        if kind == "system":
            return None
        # Modified by Know Your Friends, 2026-10-09: distinguish verified senders
        # from display-only fallbacks before triggering automatic suggestions.
        sender = senders.get(int(sender_id or 0), "")
        sender_resolved = bool(sender)
        if not sender and not user.endswith("@chatroom"):
            sender = user
        contact = contact_display(contacts, sender)
        timestamp = int(created or 0)
        message = {"id": message_id(str(db.account), user, shard,
                                 {"local_id": local_id, "sort_seq": seq, "server_id": server_id}),
                "historyCursor": encode_cursor(str(db.account), user, (int(seq), shard, int(local_id))),
                "side": "self" if sender and sender == own_user else "other", "text": text,
                "kind": "text" if kind == "text" else "image" if kind == "image" else "other",
                "time": timestamp * 1000 if timestamp < 10_000_000_000 else timestamp,
                "type": kind_name, "senderId": sender, "senderName": contact["name"],
                "senderAvatar": contact["avatar"], "senderAvatarCandidates": contact["avatarCandidates"],
                "_sort": [int(seq), shard, int(local_id)], "_senderResolved": sender_resolved}
        if isinstance(local_type, int) and local_type & 0xFFFFFFFF == 49:
            _name, decoded = self._decoded_content(db, local_type, content, compressed)
            link = link_metadata(local_type, decoded)
            if link is not None:
                message["link"] = link
                message["kind"] = "other"
                message["text"] = link["title"] or text
        # Modified by Know Your Friends, 2026-10-10: retain local-media metadata,
        # while keeping the legacy text/image/other contract for existing analysis.
        if media_kind(local_type):
            _name, decoded = self._decoded_content(db, local_type, content, compressed)
            message["media"] = media_metadata(local_type, decoded)
            key = (str(db.account), user, message["id"])
            self.issued_images[key] = tuple(message["_sort"]) + (server_id,)
            self.issued_images.move_to_end(key)
            if len(self.issued_images) > MAX_ISSUED_IMAGES:
                self.issued_images.popitem(last=False)
        return message

    def messages(self, user, limit, offset=0):
        with self.lock:
            db = self._ordinary_db()
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            contacts = self._contacts(db)
            own_user = self.self_user(db)
            result = []
            raw = self._shard_rows(db, user, limit + 1, offset)
            for item in reversed(raw[:limit]):
                message = self._render_row(db, user, item, contacts, own_user)
                if message:
                    result.append(message)
                    if message.get("media") or message["kind"] == "image" and item[0][1] == 3:
                        key = (str(db.account), user, message["id"])
                        self.issued_images[key] = tuple(message["_sort"]) + (item[0][6],)
                        self.issued_images.move_to_end(key)
                        if len(self.issued_images) > MAX_ISSUED_IMAGES:
                            self.issued_images.popitem(last=False)
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
            return MessageWindow(result, len(raw) > limit)

    def message_windows(self, users, limit=80, expected_account=None):
        """Prepare first screens together, opening each existing snapshot shard once.

        This reads a bounded latest window per conversation, never its full history and
        never invokes inference. Account verification brackets the complete batch.
        """
        with self.lock:
            db = self._ordinary_db()
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            if expected_account is not None and str(db.account) != expected_account:
                raise AccountChangedError()
            contacts = self._contacts(db)
            own_user = self.self_user(db)
            tables = {user: "Msg_" + hashlib.md5(user.encode("utf-8")).hexdigest() for user in users}
            rows = {user: [] for user in users}
            for rel in db._message_dbs():
                conn = db._open(rel)
                try:
                    shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
                    if not re.fullmatch(r"message__message_\d+\.db", shard):
                        raise RuntimeError("unidentified message shard")
                    available = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    found = [(user, table) for user, table in tables.items() if table in available]
                    if not found:
                        continue
                    senders = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                    for user, table in found:
                        records = conn.execute(
                            f"SELECT local_id,local_type,real_sender_id,create_time,message_content,"
                            f"compress_content,server_id,sort_seq FROM {table} "
                            "ORDER BY sort_seq DESC,local_id DESC LIMIT ?", (limit + 1,))
                        rows[user].extend((record, shard, senders) for record in records)
                finally:
                    conn.close()
            windows, images, has_more_before = {}, {}, {}
            for user, records in rows.items():
                records.sort(key=lambda item: (int(item[0][7]), item[1], int(item[0][0])), reverse=True)
                has_more_before[user] = len(records) > limit
                windows[user], images[user] = [], {}
                for item in reversed(records[:limit]):
                    message = self._render_row(db, user, item, contacts, own_user)
                    if message:
                        windows[user].append(message)
                        if message.get("media") or message["kind"] == "image" and item[0][1] == 3:
                            images[user][message["id"]] = tuple(message["_sort"]) + (item[0][6],)
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
            proved_at = self._proved_at
            # Each prepared conversation retains only the image capabilities belonging to
            # its current window, so preloading later chats cannot evict earlier images.
            for user in users:
                self.window_images[(str(db.account), user)] = images[user]
            batch = MessageWindowBatch(windows, has_more_before)
            batch.verified_scope = (str(db.account), str(Path(db.workdir).resolve()))
            self._remember_advisor_binding(db, batch.verified_scope[0], Path(batch.verified_scope[1]), True, proved_at)
            return batch

    def texts_for_refs(self, user, refs, with_ids=False):
        """Read only analyzed message rows by their stored shard and local primary key."""
        self.require_messages_ready()
        if not refs:
            return []
        with self.lock:
            db = self._db(fresh=True)
            by_shard = {}
            for shard, local_id, stable_id in refs:
                by_shard.setdefault(shard, {}).setdefault(int(local_id), set()).add(stable_id)
            table = "Msg_" + hashlib.md5(user.encode("utf-8")).hexdigest()
            texts = []
            for rel, _path, _size in db._db_files:
                shard = rel.replace(os.sep, "__")
                selected = by_shard.get(shard)
                if not selected:
                    continue
                conn = db._open(rel)
                try:
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
                    ).fetchone()
                    if not exists:
                        continue
                    ids = list(selected)
                    for start in range(0, len(ids), 400):
                        batch = ids[start:start + 400]
                        placeholders = ",".join("?" for _ in batch)
                        rows = conn.execute(
                            f"SELECT local_id,local_type,message_content,compress_content,server_id,sort_seq "
                            f"FROM {table} WHERE local_id IN ({placeholders})", batch
                        )
                        for local_id, local_type, content, compressed, server_id, sort_seq in rows:
                            stable_id = message_id(str(db.account), user, shard,
                                                   {"local_id": local_id, "sort_seq": sort_seq,
                                                    "server_id": server_id})
                            if stable_id not in selected.get(int(local_id), ()):
                                continue
                            kind_name = db._msg_type_name(local_type)
                            if isinstance(content, bytes):
                                content = db._friendly_content(content, kind_name)
                            placeholder = f"[{kind_name}]"
                            if (not content or content == placeholder) and isinstance(compressed, bytes):
                                content = db._friendly_content(compressed, kind_name)
                            kind, text = self.classifier(kind_name, content or placeholder)
                            if kind == "text" and isinstance(text, str) and text.strip():
                                texts.append((stable_id, text) if with_ids else text)
                finally:
                    conn.close()
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
            return texts

    def sticker_details(self, user, refs):
        """Identity, store pack and caption of sticker messages, by their stored shard and local id.

        Added by Know Your Friends, 2026-10-14 (Apache-2.0). Returns ``{stable_id: {"id", "pack"?,
        "caption"?, "packName"?}}`` for the refs that are sticker messages with a readable content MD5.
        """
        self.require_messages_ready()
        if not refs:
            return {}
        found = {}
        with self.lock:
            db = self._db(fresh=True)
            by_shard = {}
            for shard, local_id, stable_id in refs:
                by_shard.setdefault(shard, {}).setdefault(int(local_id), set()).add(stable_id)
            table = "Msg_" + hashlib.md5(user.encode("utf-8")).hexdigest()
            for rel, _path, _size in db._db_files:
                shard = rel.replace(os.sep, "__")
                selected = by_shard.get(shard)
                if not selected:
                    continue
                conn = db._open(rel)
                try:
                    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                                        (table,)).fetchone():
                        continue
                    ids = list(selected)
                    for start in range(0, len(ids), 400):
                        batch = ids[start:start + 400]
                        rows = conn.execute(
                            f"SELECT local_id,local_type,message_content,compress_content,server_id,sort_seq "
                            f"FROM {table} WHERE local_id IN ({','.join('?' for _ in batch)})", batch)
                        for local_id, local_type, content, compressed, server_id, sort_seq in rows:
                            stable_id = message_id(str(db.account), user, shard,
                                                   {"local_id": local_id, "sort_seq": sort_seq, "server_id": server_id})
                            if stable_id not in selected.get(int(local_id), ()) or media_kind(local_type) != "sticker":
                                continue
                            _name, decoded = self._decoded_content(db, local_type, content, compressed)
                            identity = sticker_identity(decoded)
                            if identity is not None:
                                found[stable_id] = identity
                finally:
                    conn.close()
        if found:
            # The account's emoticon index names installed store stickers; its absence only means no text.
            try:
                with self._local_media(db).operation() as store:
                    captions, names = store.sticker_texts(list(found.values()))
            except MediaUnavailable:
                captions, names = {}, {}
            for identity in found.values():
                if captions.get(identity["id"]):
                    identity["caption"] = captions[identity["id"]]
                if names.get(identity.get("pack")):
                    identity["packName"] = names[identity["pack"]]
        self._verify_media_reader(db)
        return found

    def _verified_media_row(self, user, stable_id, expected_account=None):
        self.require_messages_ready()
        if not isinstance(stable_id, str) or not re.fullmatch(r"[0-9a-f]{64}", stable_id):
            raise MediaUnavailable("invalid-id")
        with self.lock:
            db = self._db(fresh=True)
            account = str(db.account)
            if expected_account is not None and account != expected_account:
                raise AccountChangedError()
            issued = (self.issued_images.get((account, user, stable_id)) or
                      self.window_images.get((account, user), {}).get(stable_id))
            if issued is None:
                raise MediaUnavailable("not-issued")
            seq, shard, local_id, server_id = issued
            if stable_id != message_id(account, user, shard,
                    {"local_id": local_id, "sort_seq": seq, "server_id": server_id}):
                raise MediaUnavailable("identity-mismatch")
            found = db._msg_conns(user)
            row = None
            try:
                for conn, table in found:
                    if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                        continue
                    path = conn.execute("PRAGMA database_list").fetchone()[2]
                    if Path(path).name != shard:
                        continue
                    matches = conn.execute(
                        f"SELECT local_type,create_time,message_content,packed_info_data,compress_content FROM {table} "
                        "WHERE local_id=? AND sort_seq=? AND server_id=? LIMIT 2", (local_id, seq, server_id)).fetchall()
                    if len(matches) == 1 and media_kind(matches[0][0]):
                        local_type, created, content, packed, compressed = matches[0]
                        _name, decoded = self._decoded_content(db, local_type, content, compressed)
                        row = {"local_type": local_type, "create_time": created, "content": content,
                               "packed_info": packed, "compress_content": compressed,
                               "decoded_content": decoded, "server_id": server_id}
                    break
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
            if row is None:
                raise MediaUnavailable("row-unavailable")
            return db, row

    def _local_media(self, db):
        # Only the short reader/config lookup holds the source lock. Decoding and
        # transcoding run outside it so they do not block live message polling.
        with self.lock:
            account = str(db.account)
            if account in self._blocked_media_accounts:
                raise MediaUnavailable("account-clearing")
            key = (db, str(db.account), getattr(db, "workdir", None))
            if self._local_media_state is None or self._local_media_state[0] != key:
                if self.media_factory is None:
                    from wechatauto.media import MediaDownloader
                    downloader = MediaDownloader(db)
                else:
                    downloader = self.media_factory(db)
                store = LocalMedia(db, downloader, self._media_account_root(account))
                self._media_engines.setdefault(account, set()).add(store)
                self._local_media_state = (key, store)
            return self._local_media_state[1]

    def _media_account_root(self, account):
        return self._media_storage_root / hashlib.sha256(account.encode("utf-8")).hexdigest()

    def clear_media_account(self, account):
        with self.lock:
            self._blocked_media_accounts.add(account)
            stores = list(self._media_engines.get(account, ()))
        for store in stores:
            store.close()
        clear_owned_media(self._media_account_root(account))
        with self.lock:
            self._media_engines.pop(account, None)
            if self._local_media_state and self._local_media_state[0][1] == account:
                self._local_media_state = None

    def resume_media_after_failed_clear(self, account):
        with self.lock:
            self._blocked_media_accounts.discard(account)
            if self._local_media_state and self._local_media_state[0][1] == account:
                self._local_media_state = None

    def _verify_media_reader(self, db):
        with self.lock:
            if self._db(fresh=True) is not db:
                raise AccountChangedError()

    def media_asset(self, user, stable_id, variant="original", expected_account=None):
        self.media_reason.value = None
        try:
            db, row = self._verified_media_row(user, stable_id, expected_account)
            with self._local_media(db).operation() as store:
                asset = store.resolve(user, stable_id, row, variant)
        except MediaUnavailable as exc:
            self.media_reason.value = str(exc)
            return None
        except (OSError, ValueError):
            self.media_reason.value = "decode-failed"
            return None
        except RuntimeError as exc:
            if isinstance(exc, (AccountChangedError, AccountUnavailableError, MessagesUnavailableError)):
                raise
            self.media_reason.value = "decode-failed"
            return None
        self._verify_media_reader(db)
        return asset

    def media(self, user, stable_id):
        """Compatibility for image consumers; HTTP uses streamed media_asset."""
        asset = self.media_asset(user, stable_id)
        return (asset.read(), asset.mime) if asset else None

    def media_analysis(self, user, stable_id, cancel=None, frames=1):
        try:
            db, row = self._verified_media_row(user, stable_id)
            metadata = media_metadata(row["local_type"], row["decoded_content"])
            with self._local_media(db).operation() as store:
                result = store.analysis(user, stable_id, row, metadata, cancel, frames=frames)
        except MediaUnavailable as exc:
            return {"items": [], "error": str(exc)}
        except (OSError, ValueError):
            return {"items": [], "error": "decode-failed"}
        except RuntimeError as exc:
            if isinstance(exc, (AccountChangedError, AccountUnavailableError, MessagesUnavailableError)):
                raise
            return {"items": [], "error": "decode-failed"}
        self._verify_media_reader(db)
        return result

    def media_settings(self, expected_account, change=None, clear=False):
        with self.lock:
            db = self._db(fresh=True)
            if str(db.account) != expected_account:
                raise AccountChangedError()
        with self._local_media(db).operation() as store:
            result = store.clear_cache() if clear else store.settings(change)
        self._verify_media_reader(db)
        return result

    def media_archive(self, expected_account, user, stable_id):
        db, row = self._verified_media_row(user, stable_id, expected_account)
        with self._local_media(db).operation() as store:
            result = store.archive(user, stable_id, row)
        self._verify_media_reader(db)
        return result

    def history_highwater(self, user):
        with self.lock:
            db = self._ordinary_db()
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            newest = self._shard_rows(db, user, 1)
            if not newest:
                return None
            record, shard, _ = newest[0]
            return int(record[7]), shard, int(record[0])

    def history_page(self, user, highwater, after=None, page_size=256):
        with self.lock:
            # Resolve and validate one reader per page. Re-discovering the same
            # live account for readiness, reader access and self_user made every
            # page pay several Windows ownership queries before reading any rows.
            db = self._ordinary_db()
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            if highwater is None:
                return [], None
            contacts = self._contacts(db)
            own_user = self.self_user(db)
            found = db._msg_conns(user)
            rows = []
            try:
                for conn, table in found:
                    if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                        raise RuntimeError("invalid message table")
                    shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
                    if not re.fullmatch(r"message__message_\d+\.db", shard):
                        raise RuntimeError("unidentified message shard")
                    clauses, params = [], []
                    high_seq, high_shard, high_local = highwater
                    if shard == high_shard:
                        clauses.append("(sort_seq < ? OR (sort_seq = ? AND local_id <= ?))")
                        params.extend((high_seq, high_seq, high_local))
                    else:
                        clauses.append("sort_seq <= ?" if shard < high_shard else "sort_seq < ?")
                        params.append(high_seq)
                    if after is not None:
                        after_seq, after_shard, after_local = after
                        if shard == after_shard:
                            clauses.append("(sort_seq > ? OR (sort_seq = ? AND local_id > ?))")
                            params.extend((after_seq, after_seq, after_local))
                        else:
                            clauses.append("sort_seq >= ?" if shard > after_shard else "sort_seq > ?")
                            params.append(after_seq)
                    sql = (f"SELECT local_id,local_type,real_sender_id,create_time,message_content,compress_content,server_id,sort_seq "
                           f"FROM {table} WHERE {' AND '.join(clauses)} ORDER BY sort_seq ASC,local_id ASC LIMIT ?")
                    senders = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                    rows.extend((record, shard, senders) for record in conn.execute(sql, (*params, page_size)))
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
            rows.sort(key=lambda item: (int(item[0][7]), item[1], int(item[0][0])))
            page = rows[:page_size]
            if not page:
                return [], None
            last_record, last_shard, _ = page[-1]
            next_after = (int(last_record[7]), last_shard, int(last_record[0]))
            messages = [self._render_row(db, user, item, contacts, own_user) for item in page]
            return [message for message in messages if message], next_after

    def history_verify_rows(self, user, positions):
        """Re-read one contact's exact evidence locators in a fresh batch."""
        return self.history_verify_rows_many([{"user": user, "positions": positions}])[user]

    def history_verify_rows_many(self, groups):
        """Re-read evidence across contacts, opening each requested shard once.

        Nothing is cached between calls: edits/deletions are checked against a
        current snapshot, and account verification brackets the entire batch.
        Display names/avatars are omitted: evidence hashes use the immutable
        sender ID, side, kind, text and exact sort locator, never display data.
        """
        if not isinstance(groups, list) or len(groups) > 16:
            raise ValueError("invalid history groups")
        requested, by_shard, total = {}, {}, 0
        for group in groups:
            if not isinstance(group, dict) or set(group) != {"user", "positions"}:
                raise ValueError("invalid history group")
            user, positions = group["user"], group["positions"]
            if (not isinstance(user, str) or not 1 <= len(user) <= 256 or user in requested or
                    not isinstance(positions, list)):
                raise ValueError("invalid history locators")
            total += len(positions)
            if total > 500:
                raise ValueError("invalid history locators")
            requested[user] = set()
            for position in positions:
                if (not isinstance(position, (list, tuple)) or len(position) != 3 or
                        type(position[0]) is not int or not 0 <= position[0] <= 2**63 - 1 or
                        type(position[2]) is not int or not 0 <= position[2] <= 2**63 - 1 or
                        not isinstance(position[1], str) or
                        not re.fullmatch(r"message__message_\d+\.db", position[1])):
                    raise ValueError("invalid history locator")
                requested[user].add(tuple(position))
                by_shard.setdefault(position[1], {}).setdefault(user, set()).add(position[2])
        found = {user: {} for user in requested}
        if not by_shard:
            return found
        with self.lock:
            # Like message windows: a bounded-reuse reader before the reads and a strict
            # check after them, which is what proves the batch came from this account.
            db = self._ordinary_db()
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            identity = str(db.account), str(Path(db.workdir).resolve())
            inventory = {}
            for rel, _path, _size in db._db_files:
                normalized = rel.replace("\\", "/")
                if re.fullmatch(r"message/message_\d+\.db", normalized):
                    shard = normalized.replace("/", "__")
                    if shard in by_shard:
                        inventory[shard] = rel
            own_user = self.self_user(db)
            tables = {user: "Msg_" + hashlib.md5(user.encode("utf-8")).hexdigest() for user in requested}
            for shard, rel in inventory.items():
                conn = db._open(rel)
                try:
                    actual = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
                    if actual != shard:
                        raise ValueError("unidentified message shard")
                    available = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    users = [user for user in by_shard[shard] if tables[user] in available]
                    if not users:
                        continue
                    senders = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                    for user in users:
                        local_ids = sorted(by_shard[shard][user])
                        marks = ",".join("?" for _ in local_ids)
                        rows = conn.execute(
                            f"SELECT local_id,local_type,real_sender_id,create_time,message_content,compress_content,"
                            f"server_id,sort_seq FROM {tables[user]} WHERE local_id IN ({marks})", local_ids)
                        for row in rows:
                            position = int(row[7]), shard, int(row[0])
                            if position not in requested[user]:
                                continue
                            message = self._render_row(db, user, (row, shard, senders), {}, own_user)
                            if message:
                                if position in found[user]:
                                    raise ValueError("ambiguous history locator")
                                found[user][position] = message
                finally:
                    conn.close()
            if self._db(fresh=True) is not db or identity != (str(db.account), str(Path(db.workdir).resolve())):
                raise AccountChangedError()
            # This post-read check is as strong as a verified identity; callers'
            # following scope checks reuse it instead of querying Windows again.
            self._remember_advisor_binding(db, identity[0], Path(identity[1]), True, self._proved_at)
            return found

    def history_revision(self, user):
        """Opaque metadata revision for history change detection; never read message text."""
        with self.lock:
            db = self._ordinary_db()
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            records = []
            for rel, source_path, _size in getattr(db, "_db_files", ()):
                if not re.fullmatch(r"message/message_\d+\.db", rel.replace("\\", "/")):
                    continue
                destination = Path(db.workdir) / rel.replace(os.sep, "__")
                signatures = []
                for candidate in (Path(source_path), Path(str(source_path) + "-wal"), destination,
                                  Path(str(destination) + ".stamp")):
                    try:
                        details = candidate.stat()
                        signatures.append((details.st_mtime_ns, details.st_size, getattr(details, "st_ino", 0)))
                    except FileNotFoundError:
                        signatures.append(None)
                records.append((rel.replace("\\", "/"), signatures))
            storage = Path(getattr(db, "account_dir", "")) / "db_storage" / "message"
            try:
                inventory = sorted(path.name for path in storage.iterdir() if re.fullmatch(r"message_\d+\.db", path.name))
            except FileNotFoundError:
                inventory = []
            identity = [str(db.account), os.path.normcase(os.path.realpath(db.workdir)), user,
                        id(db), sorted(records), inventory]
            return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode("utf-8")).hexdigest()

    def history_prefix_signature(self, user, ceiling):
        """Numeric-only aggregates over one already-consumed cross-shard prefix."""
        if (not isinstance(ceiling, (list, tuple)) or len(ceiling) != 3 or
                type(ceiling[0]) is not int or type(ceiling[2]) is not int or
                not isinstance(ceiling[1], str) or not re.fullmatch(r"message__message_\d+\.db", ceiling[1])):
            raise ValueError("invalid history ceiling")
        with self.lock:
            db = self._ordinary_db()
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            found = db._msg_conns(user)
            signatures = []
            try:
                for connection, table in found:
                    if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                        raise RuntimeError("invalid message table")
                    shard = Path(connection.execute("PRAGMA database_list").fetchone()[2]).name
                    if not re.fullmatch(r"message__message_\d+\.db", shard):
                        raise RuntimeError("unidentified message shard")
                    high_seq, high_shard, high_local = ceiling
                    if shard == high_shard:
                        predicate = "(sort_seq < ? OR (sort_seq = ? AND local_id <= ?))"
                        params = (high_seq, high_seq, high_local)
                    else:
                        predicate = "sort_seq <= ?" if shard < high_shard else "sort_seq < ?"
                        params = (high_seq,)
                    aggregates = connection.execute(
                        "SELECT COUNT(*),COALESCE(MAX(local_id),0),COALESCE(SUM(local_id),0),"
                        "COALESCE(MAX(sort_seq),0) FROM " + table + " WHERE " + predicate, params).fetchone()
                    signatures.append((shard, list(aggregates)))
            finally:
                for connection in {id(connection): connection for connection, _table in found}.values():
                    connection.close()
            identity = [str(db.account), user, list(ceiling), sorted(signatures)]
            return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode("utf-8")).hexdigest()

    def quoted_history_page(self, user, ceiling, after=None, page_size=64, member=None):
        """Read only 49/57 candidates inside an already-consumed history prefix."""
        self.require_messages_ready()
        if ceiling is None:
            return [], None
        with self.lock:
            db = self._db()
            contacts = self._contacts(db)
            own_user = self.self_user()
            found = db._msg_conns(user)
            rows = []
            try:
                for conn, table in found:
                    if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                        raise RuntimeError("invalid message table")
                    shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
                    if not re.fullmatch(r"message__message_\d+\.db", shard):
                        raise RuntimeError("unidentified message shard")
                    clauses, params = ["local_type=?"], [QUOTED_REPLY_TYPE]
                    if member is not None:
                        selected_ids = [row[0] for row in conn.execute(
                            "SELECT rowid FROM Name2Id WHERE user_name=?", (member,))]
                        if not selected_ids:
                            continue
                        clauses.append("real_sender_id IN (" + ",".join("?" for _ in selected_ids) + ")")
                        params.extend(selected_ids)
                    high_seq, high_shard, high_local = ceiling
                    if shard == high_shard:
                        clauses.append("(sort_seq < ? OR (sort_seq = ? AND local_id <= ?))")
                        params.extend((high_seq, high_seq, high_local))
                    else:
                        clauses.append("sort_seq <= ?" if shard < high_shard else "sort_seq < ?")
                        params.append(high_seq)
                    if after is not None:
                        after_seq, after_shard, after_local = after
                        if shard == after_shard:
                            clauses.append("(sort_seq > ? OR (sort_seq = ? AND local_id > ?))")
                            params.extend((after_seq, after_seq, after_local))
                        else:
                            clauses.append("sort_seq >= ?" if shard > after_shard else "sort_seq > ?")
                            params.append(after_seq)
                    sql = ("SELECT local_id,local_type,real_sender_id,create_time,message_content,"
                           "compress_content,server_id,sort_seq "
                           f"FROM {table} WHERE {' AND '.join(clauses)} "
                           "ORDER BY sort_seq ASC,local_id ASC LIMIT ?")
                    senders = {int(row[0]): row[1] for row in conn.execute(
                        "SELECT rowid,user_name FROM Name2Id")}
                    rows.extend((record, shard, senders) for record in conn.execute(
                        sql, (*params, page_size)))
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
            rows.sort(key=lambda item: (int(item[0][7]), item[1], int(item[0][0])))
            page = rows[:page_size]
            if not page:
                return [], None
            last, shard, _senders = page[-1]
            next_after = (int(last[7]), shard, int(last[0]))
            rendered = [self._render_row(db, user, item, contacts, own_user) for item in page]
            return [item for item in rendered if item], next_after

    def preceding_text_context(self, user, before, limit=3):
        """Fetch the nearest earlier text context without replaying older history."""
        self.require_messages_ready()
        with self.lock:
            db = self._db()
            contacts = self._contacts(db)
            own_user = self.self_user()
            found = db._msg_conns(user)
            candidates = []
            try:
                for conn, table in found:
                    if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                        raise RuntimeError("invalid message table")
                    shard = Path(conn.execute("PRAGMA database_list").fetchone()[2]).name
                    if not re.fullmatch(r"message__message_\d+\.db", shard):
                        raise RuntimeError("unidentified message shard")
                    cursor = None
                    senders = {int(row[0]): row[1] for row in conn.execute(
                        "SELECT rowid,user_name FROM Name2Id")}
                    found_here = 0
                    while found_here < limit:
                        clauses = ["local_type IN (?,?)"]
                        params = [1, QUOTED_REPLY_TYPE]
                        if cursor is None:
                            seq, target_shard, local = before
                            if shard == target_shard:
                                clauses.append("(sort_seq < ? OR (sort_seq = ? AND local_id < ?))")
                                params.extend((seq, seq, local))
                            else:
                                clauses.append("sort_seq <= ?" if shard < target_shard else "sort_seq < ?")
                                params.append(seq)
                        else:
                            seq, local = cursor
                            clauses.append("(sort_seq < ? OR (sort_seq = ? AND local_id < ?))")
                            params.extend((seq, seq, local))
                        sql = ("SELECT local_id,local_type,real_sender_id,create_time,message_content,"
                               "compress_content,server_id,sort_seq "
                               f"FROM {table} WHERE {' AND '.join(clauses)} "
                               "ORDER BY sort_seq DESC,local_id DESC LIMIT ?")
                        rows = conn.execute(sql, (*params, 16)).fetchall()
                        if not rows:
                            break
                        for record in rows:
                            item = self._render_row(db, user, (record, shard, senders),
                                                    contacts, own_user)
                            if item and item["kind"] == "text" and item["text"].strip():
                                candidates.append(item)
                                found_here += 1
                                if found_here >= limit:
                                    break
                        cursor = int(rows[-1][7]), int(rows[-1][0])
                        if len(rows) < 16:
                            break
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
            newest = sorted(candidates, key=lambda item: tuple(item["_sort"]), reverse=True)[:limit]
            return [{"id": item["id"], "side": item["side"], "text": item["text"]}
                    for item in reversed(newest)]

    def stats(self, user, member=None):
        self.require_messages_ready()
        with self.lock:
            db = self._db()
            contacts = self._contacts(db)
            found = db._msg_conns(user)
            count = text_count = 0
            members = set()
            try:
                for conn, table in found:
                    sender_index = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                    selected_ids = [sender_id for sender_id, username in sender_index.items() if username == member] if member else []
                    if member and not selected_ids:
                        amount = 0
                    elif member:
                        placeholders = ",".join("?" for _ in selected_ids)
                        amount = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE real_sender_id IN ({placeholders})", selected_ids).fetchone()[0]
                    else:
                        amount = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    count += amount
                    text_count += sum(self._analyzable_counts(
                        db, conn, table, selected_ids if member else None).values())
                    for (sender_id,) in conn.execute(f"SELECT DISTINCT real_sender_id FROM {table}"):
                        sender = sender_index.get(int(sender_id or 0))
                        if sender:
                            members.add(sender)
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
            return count, text_count, [{"id": member, **contact_display(contacts, member)}
                                       for member in sorted(members)]

    def target_text_totals(self, users):
        """Messages the portrait scan can consume, per conversation.

        Same predicate as _analyzable_counts (plain text plus quoted replies) but without the
        per-row classification, which is what made a whole-account inventory expensive: these
        are plain SQL counts, milliseconds per conversation.
        """
        self.require_messages_ready()
        with self.lock:
            db = self._db()
            own = self.self_user(db)
            totals = {}
            for user in users:
                found = db._msg_conns(user)
                total = 0
                try:
                    for conn, table in found:
                        if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                            raise RuntimeError("invalid message table")
                        senders = {int(row[0]): row[1] for row in
                                   conn.execute("SELECT rowid,user_name FROM Name2Id")}
                        ids = [sender_id for sender_id, name in senders.items()
                               if name and name != own]
                        if not ids:
                            continue
                        placeholders = ",".join("?" for _ in ids)
                        total += conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE local_type IN (?,?) "
                            f"AND real_sender_id IN ({placeholders})",
                            (1, QUOTED_REPLY_TYPE, *ids)).fetchone()[0]
                finally:
                    for conn in {id(conn): conn for conn, _ in found}.values():
                        conn.close()
                totals[user] = total
            return totals


    def profile_metadata(self, user, member=None):
        """Read metadata once per snapshot revision; Backend brackets the account scope."""
        with self.lock:
            db = self._db(fresh=True)
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            contacts = self._contacts(db)
            found = db._msg_conns(user)
            try:
                signatures = []
                for conn, _table in found:
                    path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
                    stat = path.stat()
                    signatures.append((str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
                signature = tuple(signatures)
                key = (str(db.account), user)
                cached = self.profile_metadata_cache.get(key)
                if cached and cached[0] == signature:
                    counts, count, text_count = cached[1:]
                    self.profile_metadata_cache.move_to_end(key)
                else:
                    counts, count, text_count = {}, 0, 0
                    for conn, table in found:
                        if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                            raise RuntimeError("invalid message table")
                        senders = {int(row[0]): row[1] for row in conn.execute("SELECT rowid,user_name FROM Name2Id")}
                        for sender_id, amount in conn.execute(
                                f"SELECT real_sender_id,COUNT(*) FROM {table} GROUP BY real_sender_id"):
                            count += amount
                            sender = senders.get(int(sender_id or 0))
                            if sender:
                                previous = counts.get(sender, (0, 0))
                                counts[sender] = (previous[0] + amount, previous[1])
                        for sender_id, analyzed in self._analyzable_counts(db, conn, table).items():
                            text_count += analyzed
                            sender = senders.get(int(sender_id or 0))
                            if sender:
                                previous = counts.get(sender, (0, 0))
                                counts[sender] = (previous[0], previous[1] + analyzed)
                    self.profile_metadata_cache[key] = (signature, counts, count, text_count)
                    self.profile_metadata_cache.move_to_end(key)
                    if len(self.profile_metadata_cache) > PROFILE_METADATA_CACHE_LIMIT:
                        self.profile_metadata_cache.popitem(last=False)
            finally:
                for conn in {id(conn): conn for conn, _ in found}.values():
                    conn.close()
            group = user.endswith("@chatroom")
            if member and (not group or member not in counts):
                raise ValueError("unknown member")
            subject = member if group else user
            if subject:
                count, text_count = counts.get(subject, (0, 0))
            return {"contact": contact_display(contacts, member or user),
                    "members": [{"id": sender, **contact_display(contacts, sender)} for sender in sorted(counts)] if group else [],
                    "count": count, "textCount": text_count}

    def profile_overview(self, user, member=None, highwater=None):
        """Contact, member list, and per-sender message counts without classifying text.

        The API portrait read path must never re-render the analysis of every message
        just to show who is in a group: text totals come from the persisted portrait
        inventory/checkpoint instead.
        """
        with self.lock:
            db = self._db(fresh=True)
            if self.live_account and not db.messages_ready:
                raise MessagesUnavailableError()
            contacts = self._contacts(db)
            # A live reader may be replaced while the account and newest message
            # stay the same. Reuse the counts across that replacement, with a
            # bounded refresh window for older-message backfills.
            # The same account name can exist under different local WeChat roots.
            account_dir = getattr(db, "account_dir", None) or getattr(db, "workdir", "")
            account_dir = os.path.normcase(str(Path(account_dir).resolve())) if account_dir else ""
            cache_key = (str(db.account), account_dir, user, highwater)
            cached = self.profile_overview_counts_cache.get(cache_key)
            if cached is not None and cached[2] > time.monotonic():
                counts, total, _expires = cached
                self.profile_overview_counts_cache.move_to_end(cache_key)
            else:
                found = db._msg_conns(user)
                counts = {}
                total = 0
                try:
                    for conn, table in found:
                        if not re.fullmatch(r"Msg_[0-9a-fA-F]{32}", table):
                            raise RuntimeError("invalid message table")
                        senders = {int(row[0]): row[1] for row in
                                   conn.execute("SELECT rowid,user_name FROM Name2Id")}
                        for sender_id, amount in conn.execute(
                                f"SELECT real_sender_id,COUNT(*) FROM {table} GROUP BY real_sender_id"):
                            total += amount
                            sender = senders.get(int(sender_id or 0))
                            if sender:
                                counts[sender] = counts.get(sender, 0) + amount
                finally:
                    for conn in {id(conn): conn for conn, _ in found}.values():
                        conn.close()
                self.profile_overview_counts_cache[cache_key] = (counts, total, time.monotonic() + 60)
                while len(self.profile_overview_counts_cache) > PROFILE_METADATA_CACHE_LIMIT:
                    self.profile_overview_counts_cache.popitem(last=False)
            group = user.endswith("@chatroom")
            if member and (not group or member not in counts):
                raise ValueError("unknown member")
            return {"contact": contact_display(contacts, member or user),
                    "members": [{"id": sender, **contact_display(contacts, sender)}
                                for sender in sorted(counts)] if group else [],
                    "count": counts.get(member, 0) if member else
                             (total if group else counts.get(user, 0)),
                    "textCount": None}

    def contact(self, user):
        with self.lock:
            db = self._db(fresh=True)
            contact = contact_display(self._contacts(db), user)
            if self._db(fresh=True) is not db:
                raise AccountChangedError()
            return contact
