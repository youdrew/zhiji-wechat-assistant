"""New-message notifications for MCP clients ("B" outlet, G11): metadata-only events.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

This outlet is separate from ``core_events.EventService`` (which only serves
plugin-subscribed chats). A daemon thread looks at the live account's session list
every ``POLL_SECONDS`` and turns inbound activity into ``message.new`` rows that
MCP clients long-poll with signed cursors. Events never carry names or message
text -- only ids, counts and times -- so an Agent must still call the read tools
(which re-check authorization) to see content.

Wiring (done by the integrator; nothing here imports the proxy or services):

* ``McpEventStore(path)`` -- own database, normally ``.local/core/mcp/events.sqlite3``.
* ``McpEventOutlet(source_provider, store, registry_path, fingerprint, cursor_key,
  active, clock=time.time, poll_seconds=POLL_SECONDS)``

  - ``source_provider()`` -> the chat source (``core.source``) or ``None``. Only
    ``sessions()`` and ``message_windows(users, 40, expected_account=...)`` are used;
    the live account is ``sessions()["account"]``.
  - ``registry_path`` -- ``.local/real-client-data/accounts.json``; accounts missing
    from it never produce events (state ``account-not-saved``).
  - ``fingerprint`` / ``cursor_key`` -- a string or a zero-argument callable (e.g.
    ``McpServices.source_fingerprint`` and ``McpStore.cursor_key``). Rotating the
    cursor key invalidates outstanding cursors (``cursor-invalid``).
  - ``active()`` -> truthy when an MCP consumer called within the last 30 min
    (``ACTIVE_WINDOW_SECONDS``); otherwise a cycle is skipped (state ``idle``) and the
    next active cycle is a catch-up cycle.
  - ``start()`` / ``close()`` with Core start/stop; ``close()`` wakes long-pollers.
  - proxy action ``zhiji_poll_events``: ``outlet.read(principal_info, scope, cursor,
    wait, consumer, resume, limit)`` where ``principal_info`` is ``{clientId,
    authVersion, fingerprint}`` and ``scope`` is ``McpServices.readable_scope(principal)``
    (duck-typed: ``accounts()`` -> ``None`` for all or a collection of accountIds;
    ``allows(account_id, subject_type, subject_id)``). ``EventError.code`` maps to the
    proxy error (``invalid-params`` -> 400, ``cursor-invalid`` / ``cursor-stale`` -> 409).
  - account clearing -> ``store.forget_account(account)``; authorization version
    bump or profile removal (``McpStore.add_version_listener``) ->
    ``store.forget_client(client_id)``.

Detection: a conversation changed when its ``time``/``sortTimestamp`` advanced or its
``unreadCount`` rose -- also when the newest row is our own reply, because inbound rows
may precede it in the same cycle. Only changed conversations are read (bounded
40-message window) and inbound (``side == "other"``) rows past the stored position are
counted; own rows only move the position. When the window cannot prove it covers the
position the count falls back to the unread delta with ``countExact: false``. Until a
conversation has a precise position (silent baseline, or a conversation that appeared
later) only its session time is known; inbound rows of that same time are ambiguous
and are counted as far as the unread delta says they are new.
The first sighting of an account is a silent baseline. After a restart or an idle gap,
new rows are emitted with ``catchUp: true`` only when ``time >= last heartbeat`` and
within 24 h; older ones are absorbed. Events are deduplicated by
``sha256(account|user|lastSortKey)`` and kept 7 days / 5000 rows; a cursor behind the
pruned range resumes from the oldest kept row with ``gap: true``.

Schema note: besides the agreed columns, ``events.state`` holds the new state of a
``source.state`` row and ``positions.sort_ts`` the last seen ``sortTimestamp``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

from backend_contracts import AccountChangedError, MessagesUnavailableError

_log = logging.getLogger(__name__)

POLL_SECONDS = 5.0
WINDOW = 40
ACTIVE_WINDOW_SECONDS = 30 * 60
MAX_WAIT_SECONDS = 25
MAX_LIMIT = 100
DEFAULT_LIMIT = 50
RETENTION_MS = 7 * 24 * 60 * 60 * 1000
RETENTION_ROWS = 5000
CATCH_UP_HORIZON_MS = 24 * 60 * 60 * 1000
# A heartbeat older than this means the outlet did not watch (restart or idle).
CATCH_UP_GAP_MS = 60 * 1000
MAX_CHANGED_PER_CYCLE = 64
# After ``forget_account`` the outlet stores nothing for that account until it sees the
# account gone from the saved-account registry (clearing finished) or this hold expires
# (clearing failed and the account stays saved): a cycle racing the clear never re-adds it.
CLEAR_HOLD_SECONDS = 600.0
# One warning per failure kind per interval; repeats in between are only counted.
LOG_INTERVAL_SECONDS = 300.0
CONSUMER_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
SUBJECT_TYPE = "wechat_contact"
STATES = ("live", "wechat-unavailable", "messages-not-ready", "account-not-saved", "idle")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL UNIQUE,
  type TEXT NOT NULL,
  account TEXT,
  account_id TEXT,
  subject_id TEXT,
  is_group INTEGER,
  new_count INTEGER,
  count_exact INTEGER,
  first_time_ms INTEGER,
  last_time_ms INTEGER,
  observed_at_ms INTEGER NOT NULL,
  catch_up INTEGER NOT NULL DEFAULT 0,
  fingerprint TEXT,
  state TEXT
);
CREATE INDEX IF NOT EXISTS events_observed ON events(observed_at_ms);
CREATE TABLE IF NOT EXISTS positions (
  account TEXT NOT NULL,
  user TEXT NOT NULL,
  sort_key TEXT,
  last_time_ms INTEGER,
  unread INTEGER,
  sort_ts INTEGER,
  PRIMARY KEY (account, user)
);
CREATE TABLE IF NOT EXISTS outlet_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumer_cursors (
  client_id TEXT NOT NULL,
  consumer TEXT NOT NULL,
  auth_version INTEGER NOT NULL,
  fingerprint TEXT NOT NULL,
  seq INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  PRIMARY KEY (client_id, consumer)
);
"""


class EventError(Exception):
    """A rejected poll: ``invalid-params``, ``cursor-invalid`` or ``cursor-stale``."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _value(item):
    return item() if callable(item) else item


def _ms(clock):
    return int(clock() * 1000)


def _registry_accounts(path):
    """``{account: accountId}`` from the saved-account registry; broken file -> none.

    Mirrors ``core_mcp_offline.registry_records`` without importing the proxy.
    """
    try:
        registry = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    records = registry.get("accounts") if isinstance(registry, dict) else None
    result = {}
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        account, identifier = record.get("account"), record.get("accountId")
        if isinstance(account, str) and account and isinstance(identifier, str) and identifier:
            result[account] = identifier
    return result


def event_key(account, user, last_sort_key):
    raw = f"{account}|{user}|{last_sort_key}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class McpEventStore:
    """The outlet's own SQLite file (WAL, synchronous NORMAL): rows, positions, cursors."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=NORMAL")
            self.connection.execute("PRAGMA busy_timeout=5000")
            self.connection.executescript(SCHEMA)
        except BaseException:
            self.close()
            raise
        self._clearing = {}  # account -> monotonic time of its forget_account

    def close(self):
        with self.lock:
            try:
                self.connection.close()
            except sqlite3.Error:
                pass

    def _write(self, apply):
        with self.lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                result = apply(self.connection)
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            self.connection.execute("COMMIT")
            return result

    # -- outlet_state ----------------------------------------------------------------
    def get_state(self, key, default=None):
        with self.lock:
            row = self.connection.execute("SELECT value FROM outlet_state WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def set_state(self, key, value):
        self._write(lambda db: db.execute("INSERT OR REPLACE INTO outlet_state(key,value) VALUES(?,?)",
                                          (key, json.dumps(value, separators=(",", ":")))))

    # -- positions -------------------------------------------------------------------
    def positions(self, account):
        with self.lock:
            rows = self.connection.execute(
                "SELECT user,sort_key,last_time_ms,unread,sort_ts FROM positions WHERE account=?",
                (account,)).fetchall()
        result = {}
        for row in rows:
            try:
                sort_key = json.loads(row["sort_key"]) if row["sort_key"] else None
            except ValueError:
                sort_key = None
            result[row["user"]] = {"sortKey": sort_key, "time": row["last_time_ms"] or 0,
                                   "unread": row["unread"] or 0, "sortTs": row["sort_ts"] or 0}
        return result

    def save_cycle(self, account, positions, events, heartbeat_ms, now_ms, still_saved=None):
        """One transaction: position updates, deduplicated rows, heartbeat and retention.

        Returns the number of newly inserted rows, or ``None`` when nothing was stored
        because the account is being cleared (``forget_account``) or ``still_saved()`` --
        checked under the store lock right before persisting -- says it left the registry.
        """
        def apply(db):
            if self._held(account) or (still_saved is not None and not still_saved()):
                return None
            for user, item in positions.items():
                db.execute("INSERT OR REPLACE INTO positions(account,user,sort_key,last_time_ms,unread,sort_ts)"
                           " VALUES(?,?,?,?,?,?)",
                           (account, user, json.dumps(item["sortKey"]) if item["sortKey"] is not None else None,
                            item["time"], item["unread"], item["sortTs"]))
            inserted = self._insert(db, events)
            if heartbeat_ms is not None:
                db.execute("INSERT OR REPLACE INTO outlet_state(key,value) VALUES(?,?)",
                           ("heartbeat:" + account, json.dumps(heartbeat_ms)))
            self._prune(db, now_ms)
            return inserted

        return self._write(apply)

    def add_events(self, events, now_ms):
        def apply(db):
            inserted = self._insert(db, [event for event in events
                                         if not event.get("account") or not self._held(event["account"])])
            self._prune(db, now_ms)
            return inserted

        return self._write(apply)

    @staticmethod
    def _insert(db, events):
        inserted = 0
        for event in events:
            cursor = db.execute(
                "INSERT OR IGNORE INTO events(event_key,type,account,account_id,subject_id,is_group,new_count,"
                "count_exact,first_time_ms,last_time_ms,observed_at_ms,catch_up,fingerprint,state)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event["key"], event["type"], event.get("account"), event.get("accountId"),
                 event.get("subjectId"), int(bool(event.get("isGroup"))), event.get("newCount"),
                 int(bool(event.get("countExact"))), event.get("firstTime"), event.get("lastTime"),
                 event["observedAt"], int(bool(event.get("catchUp"))), event.get("fingerprint"),
                 event.get("state")))
            inserted += cursor.rowcount
        return inserted

    @staticmethod
    def _prune(db, now_ms):
        """Drop rows past 7 days or beyond the newest 5000; remember the pruned edge."""
        edge = db.execute(
            "SELECT MAX(seq) FROM events WHERE observed_at_ms < ? OR seq <= "
            "(SELECT COALESCE(MAX(seq),0) - ? FROM events)", (now_ms - RETENTION_MS, RETENTION_ROWS)).fetchone()[0]
        if edge is None:
            return
        db.execute("DELETE FROM events WHERE seq <= ?", (edge,))
        row = db.execute("SELECT value FROM outlet_state WHERE key='pruned_through'").fetchone()
        previous = int(json.loads(row[0])) if row is not None else 0
        db.execute("INSERT OR REPLACE INTO outlet_state(key,value) VALUES('pruned_through',?)",
                   (json.dumps(max(previous, int(edge))),))

    # -- reading ---------------------------------------------------------------------
    def head_seq(self):
        with self.lock:
            row = self.connection.execute("SELECT seq FROM sqlite_sequence WHERE name='events'").fetchone()
        return int(row[0]) if row is not None else 0

    def pruned_through(self):
        return int(self.get_state("pruned_through", 0) or 0)

    def rows_after(self, seq, limit):
        with self.lock:
            return [dict(row) for row in self.connection.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?", (seq, limit))]

    # -- consumer cursors ------------------------------------------------------------
    def consumer_cursor(self, client_id, consumer):
        with self.lock:
            row = self.connection.execute(
                "SELECT auth_version,fingerprint,seq FROM consumer_cursors WHERE client_id=? AND consumer=?",
                (client_id, consumer)).fetchone()
        return dict(row) if row is not None else None

    def save_consumer_cursor(self, client_id, consumer, auth_version, fingerprint, seq, now_ms):
        """Acknowledge ``seq``. Within one authorization version and source fingerprint the
        saved position only moves forward, so a late request carrying an older cursor
        cannot roll the acknowledgement back; a new version or fingerprint replaces it."""
        self._write(lambda db: db.execute(
            "INSERT INTO consumer_cursors(client_id,consumer,auth_version,fingerprint,seq,updated_at_ms)"
            " VALUES(?,?,?,?,?,?) ON CONFLICT(client_id,consumer) DO UPDATE SET"
            " seq=CASE WHEN auth_version=excluded.auth_version AND fingerprint=excluded.fingerprint"
            " THEN MAX(seq,excluded.seq) ELSE excluded.seq END,"
            " auth_version=excluded.auth_version, fingerprint=excluded.fingerprint,"
            " updated_at_ms=excluded.updated_at_ms",
            (client_id, consumer, int(auth_version), fingerprint, int(seq), now_ms)))

    # -- forgetting ------------------------------------------------------------------
    def forget_account(self, account):
        """Purge every row, position and heartbeat of one account (internal id or accountId)
        and hold it: until :meth:`release_account` (or ``CLEAR_HOLD_SECONDS``) no cycle may
        store anything for it again, so an outlet cycle racing the clear cannot re-add it."""
        def apply(db):
            db.execute("DELETE FROM events WHERE account=? OR account_id=?", (account, account))
            db.execute("DELETE FROM positions WHERE account=?", (account,))
            db.execute("DELETE FROM outlet_state WHERE key=?", ("heartbeat:" + account,))

        with self.lock:
            self._clearing[account] = time.monotonic()
            self._write(apply)

    def release_account(self, account):
        """The account left the saved-account registry: clearing is over, lift the hold."""
        with self.lock:
            self._clearing.pop(account, None)

    def _held(self, account):
        with self.lock:
            since = self._clearing.get(account)
            if since is None:
                return False
            if time.monotonic() - since > CLEAR_HOLD_SECONDS:
                del self._clearing[account]
                return False
            return True

    def forget_client(self, client_id):
        """Drop a client's server-side consumer cursors (authorization changed or removed)."""
        self._write(lambda db: db.execute("DELETE FROM consumer_cursors WHERE client_id=?", (client_id,)))


def _sort_of(message):
    value = message.get("_sort") if isinstance(message, dict) else None
    return list(value) if isinstance(value, (list, tuple)) and len(value) == 3 else None


def _time_of(message):
    value = message.get("time") if isinstance(message, dict) else None
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _int(value, default=0):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


class McpEventOutlet:
    """The polling thread plus the long-poll reader. See the module docstring for wiring."""

    def __init__(self, source_provider, store, registry_path, fingerprint, cursor_key, active,
                 clock=time.time, poll_seconds=POLL_SECONDS):
        self.source_provider = source_provider
        self.store = store
        self.registry_path = registry_path
        self._fingerprint = fingerprint
        self._cursor_key = cursor_key
        self.active = active
        self.clock = clock
        self.poll_seconds = poll_seconds
        self.condition = threading.Condition()
        self.closed = threading.Event()
        self.thread = None
        self._cycle_lock = threading.Lock()
        self._watched = set()   # accounts observed by this process (restart detection)
        self._log_times = {}    # failure kind -> monotonic time of its last warning
        self._log_suppressed = {}
        self._fingerprint_digest = None
        self._state = store.get_state("source_state") or {"state": "idle", "checkedAt": None}

    # -- lifecycle -------------------------------------------------------------------
    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, daemon=True, name="mcp-events")
            self.thread.start()

    def close(self):
        self.closed.set()
        with self.condition:
            self.condition.notify_all()
        thread, self.thread = self.thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.poll_seconds + 5)

    def _loop(self):
        while not self.closed.is_set():
            try:
                self.poll_once()
            except Exception as exc:
                # The outlet never disturbs Core; the next cycle retries.
                self._warn("cycle", exc)
            self.closed.wait(self.poll_seconds)

    def _warn(self, kind, exc):
        """Rate-limited warning: one per ``kind`` per ``LOG_INTERVAL_SECONDS``."""
        moment = time.monotonic()
        last = self._log_times.get(kind)
        if last is not None and moment - last < LOG_INTERVAL_SECONDS:
            self._log_suppressed[kind] = self._log_suppressed.get(kind, 0) + 1
            return
        self._log_times[kind] = moment
        suppressed = self._log_suppressed.pop(kind, 0)
        _log.warning("mcp events: %s failed (%s; %d similar since last report)", kind,
                     type(exc).__name__, suppressed, exc_info=exc)

    def _observe_fingerprint(self, fingerprint, now):
        """Make a changing source fingerprint observable: rows stored under the old value
        become invisible to principals on the new one. Nothing is dropped; only a digest
        of the value is recorded (``source_fingerprint_change``) and logged."""
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:12]
        if digest == self._fingerprint_digest:
            return
        self._fingerprint_digest = digest
        previous = self.store.get_state("source_fingerprint")
        if previous == digest:
            return
        self.store.set_state("source_fingerprint", digest)
        if previous is None:
            return
        self.store.set_state("source_fingerprint_change", {"from": previous, "to": digest, "changedAt": now})
        _log.warning("mcp events: source fingerprint changed (%s -> %s); earlier rows stay under the old one",
                     previous, digest)

    def fingerprint(self):
        return str(_value(self._fingerprint) or "")

    def _notify(self):
        with self.condition:
            self.condition.notify_all()

    # -- source state ----------------------------------------------------------------
    def source_state(self):
        """``{state, accountId?, checkedAt}`` of the most recent cycle (``idle`` when skipped)."""
        return dict(self._state)

    def _set_state(self, state, now, account_id=None, account=None):
        snapshot = {"state": state, "checkedAt": now}
        if account_id:
            snapshot["accountId"] = account_id
        self._state = snapshot
        self.store.set_state("source_state", snapshot)
        if state == "idle":
            return
        previous = self.store.get_state("source_state_emitted")
        current = {"state": state, "accountId": account_id}
        if previous == current:
            return
        self.store.set_state("source_state_emitted", current)
        if previous is None:
            return  # The first observation is a baseline, not a transition.
        key = hashlib.sha256(json.dumps([previous, current, now]).encode("utf-8")).hexdigest()
        if self.store.add_events([{"key": key, "type": "source.state", "account": account,
                                   "accountId": account_id, "observedAt": now, "state": state,
                                   "fingerprint": self.fingerprint()}], now):
            self._notify()

    # -- polling ---------------------------------------------------------------------
    def poll_once(self):
        """One detection cycle; returns the number of newly stored ``message.new`` rows."""
        with self._cycle_lock:
            return self._poll()

    def _poll(self):
        now = _ms(self.clock)
        if self.active is not None and not self.active():
            self._set_state("idle", now)
            return 0
        fingerprint = self.fingerprint()  # one value per cycle
        self._observe_fingerprint(fingerprint, now)
        source = self.source_provider() if self.source_provider is not None else None
        try:
            metadata = source.sessions() if source is not None else None
        except Exception:
            metadata = None
        if not isinstance(metadata, dict) or not isinstance(metadata.get("sessions"), list):
            self._set_state("wechat-unavailable", now)
            return 0
        account = metadata.get("account")
        if not isinstance(account, str) or not account:
            self._set_state("wechat-unavailable", now)
            return 0
        account_id = _registry_accounts(self.registry_path).get(account)
        if account_id is None:
            self.store.release_account(account)
            self._set_state("account-not-saved", now)
            return 0

        def still_saved():
            # Re-read right before persisting: a clear may have finished since the check above.
            return _registry_accounts(self.registry_path).get(account) == account_id
        if metadata.get("messagesReady") is False:
            self._set_state("messages-not-ready", now, account_id, account)
            return 0
        sessions = [item for item in metadata["sessions"]
                    if isinstance(item, dict) and isinstance(item.get("username"), str) and item["username"]]
        stored = self.store.positions(account)
        heartbeat = self.store.get_state("heartbeat:" + account)
        if heartbeat is None and not stored:
            # First sighting: record where every conversation stands, emit nothing.
            positions = {item["username"]: self._snapshot(item, None) for item in sessions}
            if self.store.save_cycle(account, positions, [], now, now, still_saved) is None:
                self._set_state("account-not-saved", now)
                return 0
            self._watched.add(account)
            self._set_state("live", now, account_id, account)
            return 0
        heartbeat = _int(heartbeat, now)
        catch_up = account not in self._watched or now - heartbeat > CATCH_UP_GAP_MS
        updates, changed = {}, []
        for item in sessions:
            user = item["username"]
            seen = stored.get(user)
            current = (_int(item.get("time")), _int(item.get("sortTimestamp")), max(0, _int(item.get("unreadCount"))))
            if seen is None:
                if current[0] <= heartbeat and current[2] == 0:
                    updates[user] = self._snapshot(item, None)
                    continue
                # A conversation that appeared since the last cycle: new rows are those after it.
                seen = stored[user] = {"sortKey": None, "time": heartbeat, "unread": 0, "sortTs": 0}
            if current[0] > seen["time"] or current[1] > seen["sortTs"] or current[2] > seen["unread"]:
                if len(changed) < MAX_CHANGED_PER_CYCLE:
                    changed.append(item)
                # Over the cap the stored row stays untouched, so a later cycle still sees the change.
            elif current != (seen["time"], seen["sortTs"], seen["unread"]):
                updates[user] = dict(seen, time=current[0], sortTs=current[1], unread=current[2])
        events = []
        if changed:
            users = [item["username"] for item in changed]
            try:
                batch = source.message_windows(users, WINDOW, expected_account=account)
            except Exception as exc:
                if isinstance(exc, MessagesUnavailableError):
                    self._set_state("messages-not-ready", now, account_id, account)
                elif not isinstance(exc, AccountChangedError):
                    self._warn("message-windows", exc)
                return 0  # Account switch or transient failure: nothing advanced, retry next cycle.
            more = getattr(batch, "has_more_before", None) or {}
            for item in changed:
                user = item["username"]
                window = batch.get(user) if isinstance(batch, dict) else None
                window = [m for m in window if isinstance(m, dict)] if isinstance(window, (list, tuple)) else []
                seen = stored[user]
                has_more = more.get(user, len(window) >= WINDOW)
                event, newest = self._detect(account, account_id, item, seen, window, has_more,
                                             catch_up, heartbeat, now, fingerprint)
                updates[user] = self._snapshot(item, newest if newest is not None else seen["sortKey"])
                if event is not None:
                    events.append(event)
        inserted = self.store.save_cycle(account, updates, events, now, now, still_saved)
        if inserted is None:
            self._set_state("account-not-saved", now)
            return 0
        self._watched.add(account)
        self._set_state("live", now, account_id, account)
        if inserted:
            self._notify()
        return inserted

    @staticmethod
    def _snapshot(item, sort_key):
        return {"sortKey": sort_key, "time": _int(item.get("time")),
                "unread": max(0, _int(item.get("unreadCount"))), "sortTs": _int(item.get("sortTimestamp"))}

    def _detect(self, account, account_id, item, seen, window, has_more, catch_up, heartbeat, now, fingerprint):
        """The event of one changed conversation (or ``None``) and its newest window position.

        Rows are judged one by one against the stored position, so an own reply at the end
        of the window only moves the position and never hides inbound rows before it.
        """
        position, boundary = seen["sortKey"], seen["time"]
        rows = sorted((m for m in window if _sort_of(m) is not None), key=_sort_of)
        newest = _sort_of(rows[-1]) if rows else None
        ties = []
        if position is not None:
            fresh = [m for m in rows if _sort_of(m) > position]
        else:
            # Only the session time is known: later rows are new; inbound rows of exactly
            # that time are either the baseline's last row or same-second arrivals.
            fresh = [m for m in rows if (_time_of(m) or 0) > boundary]
            ties = [m for m in rows if _time_of(m) == boundary and m.get("side") == "other"]
        # The window proves it reaches back to the stored position.
        covered = bool(rows) and (not has_more or len(fresh) < len(rows))
        counted = [m for m in fresh if m.get("side") == "other"]
        delta = max(0, _int(item.get("unreadCount")) - seen["unread"])
        extra = min(len(ties), delta - len(counted))
        if extra > 0:
            # The unread delta decides how many same-second rows are new: the newest ones.
            counted = ties[len(ties) - extra:] + counted
        horizon = max(heartbeat, now - CATCH_UP_HORIZON_MS + 1)
        if catch_up:
            counted = [m for m in counted if (_time_of(m) or 0) >= horizon]
        session_time = _int(item.get("time")) or now
        if covered:
            if not counted:
                return None, newest
            new_count, exact = len(counted), extra <= 0
        else:
            new_count, exact = max(len(counted), delta), False
            if new_count == 0 or (catch_up and not counted and session_time < horizon):
                return None, newest
        user = item["username"]
        times = [_time_of(m) for m in counted if _time_of(m) is not None]
        if counted:
            last_key = json.dumps(max(_sort_of(m) for m in counted), separators=(",", ":"))
        else:
            last_key = f"t:{session_time}:{_int(item.get('sortTimestamp'))}:{_int(item.get('unreadCount'))}"
        return ({"key": event_key(account, user, last_key), "type": "message.new", "account": account,
                 "accountId": account_id, "subjectId": user,
                 "isGroup": bool(item.get("isGroup", user.endswith("@chatroom"))),
                 "newCount": new_count, "countExact": exact,
                 "firstTime": min(times) if times else session_time,
                 "lastTime": max(times) if times else session_time,
                 "observedAt": now, "catchUp": catch_up, "fingerprint": fingerprint}, newest)

    # -- cursors ---------------------------------------------------------------------
    def _key(self):
        key = _value(self._cursor_key)
        return key.encode("utf-8") if isinstance(key, str) else bytes(key)

    def encode_cursor(self, client_id, auth_version, fingerprint, consumer, seq):
        body = json.dumps({"k": "ev", "c": client_id, "v": auth_version, "f": fingerprint, "n": consumer,
                           "s": int(seq)}, separators=(",", ":"), sort_keys=True).encode("utf-8")
        signature = hmac.new(self._key(), body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + signature).decode("ascii").rstrip("=")

    def decode_cursor(self, cursor, client_id, auth_version, fingerprint, consumer):
        """The seq of a cursor this principal issued; ``cursor-invalid`` / ``cursor-stale`` otherwise."""
        invalid = EventError("cursor-invalid", "事件游标无效，请不带游标重新开始")
        if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
            raise invalid
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        except (ValueError, TypeError):
            raise invalid from None
        # The signature is the fixed-length SHA-256 tail; it may contain any byte.
        body, signature = raw[:-32], raw[-32:]
        if len(raw) <= 32 or not hmac.compare_digest(hmac.new(self._key(), body, hashlib.sha256).digest(),
                                                     signature):
            raise invalid
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            raise invalid from None
        if (not isinstance(payload, dict) or payload.get("k") != "ev" or payload.get("c") != client_id
                or payload.get("f") != fingerprint or payload.get("n") != consumer
                or type(payload.get("s")) is not int or payload["s"] < 0):
            raise invalid
        if payload.get("v") != auth_version:
            raise EventError("cursor-stale", "授权已变更，事件游标失效；请不带游标重新开始")
        return payload["s"]

    # -- reading ---------------------------------------------------------------------
    def read(self, principal_info, scope, cursor=None, wait=0, consumer="default", resume=False,
             limit=DEFAULT_LIMIT):
        """Long-poll ``message.new`` / ``source.state`` rows visible to ``scope``.

        ``principal_info``: ``{clientId, authVersion, fingerprint}``. ``wait`` seconds 0-25,
        ``limit`` 1-100. The incoming cursor is the acknowledgement: its seq is saved as the
        consumer's server-side position, which ``resume=True`` (without a cursor) picks up
        only while the authorization version and source fingerprint are unchanged.
        """
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not 0 <= wait <= MAX_WAIT_SECONDS:
            raise EventError("invalid-params", f"waitSeconds 须在 0-{MAX_WAIT_SECONDS} 之间")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
            raise EventError("invalid-params", f"limit 须在 1-{MAX_LIMIT} 之间")
        if not isinstance(consumer, str) or not CONSUMER_PATTERN.fullmatch(consumer):
            raise EventError("invalid-params", "consumer 须为 1-64 位字母、数字或 _.:-")
        if not isinstance(resume, bool):
            raise EventError("invalid-params", "resume 须为布尔值")
        info = principal_info or {}
        client_id, auth_version = info.get("clientId"), info.get("authVersion")
        fingerprint = info.get("fingerprint")
        if fingerprint is None:
            fingerprint = self.fingerprint()
        if not isinstance(client_id, str) or not client_id:
            raise EventError("invalid-params", "缺少客户端身份")
        now = _ms(self.clock)
        start = None
        if cursor is not None:
            start = self.decode_cursor(cursor, client_id, auth_version, fingerprint, consumer)
        elif resume:
            saved = self.store.consumer_cursor(client_id, consumer)
            if saved is not None and saved["auth_version"] == auth_version and saved["fingerprint"] == fingerprint:
                start = saved["seq"]
        if start is None:
            head = self.store.head_seq()
            self.store.save_consumer_cursor(client_id, consumer, auth_version, fingerprint, head, now)
            return {"events": [], "cursor": self.encode_cursor(client_id, auth_version, fingerprint, consumer, head),
                    "baseline": True, "gap": False, "source": self._visible_source(scope)}
        self.store.save_consumer_cursor(client_id, consumer, auth_version, fingerprint, start, now)
        gap = start < self.store.pruned_through()
        deadline = time.monotonic() + float(wait)
        position = start
        while True:
            events, position = self._collect(position, scope, fingerprint, limit)
            if events or self.closed.is_set():
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            with self.condition:
                if self.store.head_seq() <= position and not self.closed.is_set():
                    self.condition.wait(min(1.0, remaining))
        return {"events": events,
                "cursor": self.encode_cursor(client_id, auth_version, fingerprint, consumer, position),
                "baseline": False, "gap": gap, "source": self._visible_source(scope)}

    def _collect(self, position, scope, fingerprint, limit):
        """Visible rows after ``position``; the returned position passes skipped rows too."""
        events = []
        while len(events) < limit:
            rows = self.store.rows_after(position, 200)
            if not rows:
                break
            for row in rows:
                position = row["seq"]
                if row["fingerprint"] == fingerprint:
                    event = self._present(row, scope)
                    if event is not None:
                        events.append(event)
                        if len(events) >= limit:
                            break
        return events, position

    @staticmethod
    def _account_visible(scope, account_id):
        accounts = scope.accounts()
        return accounts is None or account_id in accounts

    def _present(self, row, scope):
        if row["type"] == "message.new":
            account_id, subject_id = row["account_id"], row["subject_id"]
            if not account_id or not subject_id or not scope.allows(account_id, SUBJECT_TYPE, subject_id):
                return None
            return {"eventId": "ev_" + row["event_key"][:24], "seq": row["seq"], "type": "message.new",
                    "accountId": account_id,
                    "subject": {"accountId": account_id, "subjectType": SUBJECT_TYPE, "subjectId": subject_id},
                    "isGroup": bool(row["is_group"]), "newCount": row["new_count"],
                    "countExact": bool(row["count_exact"]), "firstTime": row["first_time_ms"],
                    "lastTime": row["last_time_ms"], "observedAt": row["observed_at_ms"],
                    "catchUp": bool(row["catch_up"])}
        if row["type"] == "source.state":
            event = {"eventId": "ev_" + row["event_key"][:24], "seq": row["seq"], "type": "source.state",
                     "state": row["state"], "observedAt": row["observed_at_ms"]}
            if row["account_id"] and self._account_visible(scope, row["account_id"]):
                event["accountId"] = row["account_id"]
            return event
        return None

    def _visible_source(self, scope):
        state = self.source_state()
        account_id = state.pop("accountId", None)
        if account_id and self._account_visible(scope, account_id):
            state["accountId"] = account_id
        return state
