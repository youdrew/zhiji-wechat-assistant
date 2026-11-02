"""Event delivery to plugins: new messages in subscribed chats, opened friends, human-data changes.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Only subscribed conversations are polled. A ``message.new`` event carries the
messages after the plugin's acknowledged position; the position advances only
when the plugin acknowledges. Imported history never produces events.
"""
from __future__ import annotations

import threading
import time
import uuid

from core_plugin_process import PluginUnavailable

POLL_SECONDS = 3.0
WINDOW = 40
MAX_EVENT_MESSAGES = 20
ACK_TIMEOUT = 60.0


def _sort(message):
    value = message.get("_sort")
    return list(value) if isinstance(value, (list, tuple)) and len(value) == 3 else None


def _brief(message):
    return {"id": message.get("id"), "side": message.get("side"), "kind": message.get("kind"),
            "text": (message.get("text") or "")[:2000], "time": message.get("time"),
            "senderName": message.get("senderName") or "", "senderId": message.get("senderId"),
            "senderResolved": message.get("_senderResolved") is True, "sortKey": _sort(message)}


class EventService:
    def __init__(self, core, poll_seconds=POLL_SECONDS):
        self.core = core
        self.poll_seconds = poll_seconds
        # The plugin may be started lazily by its first event. Give it the host's
        # live boundary instead of the later delivery time; WeChat timestamps
        # have whole-second precision.
        self.started_at_ms = int(time.time()) * 1000
        self.lock = threading.RLock()
        self.plugin_live_since = {}
        self.in_flight = {}     # event id -> (plugin, account, user, position, deadline)
        self.busy = {}          # (plugin, account, user) -> event id
        self.closed = threading.Event()
        self.wake = threading.Event()
        core.supervisor.on("event.ack", self._ack)
        self.thread = threading.Thread(target=self._loop, daemon=True, name="plugin-events")
        self.thread.start()

    def close(self):
        self.closed.set()
        self.wake.set()

    def reenabled(self, plugin):
        """A disabled plugin resumes from this activation, not the host's old start."""
        with self.lock:
            self.plugin_live_since[plugin] = max(self.started_at_ms, int(time.time()) * 1000,
                                                 self.plugin_live_since.get(plugin, 0))
            for event_id, entry in list(self.in_flight.items()):
                if entry[0] == plugin:
                    self.in_flight.pop(event_id, None)
                    self.busy.pop(entry[:3], None)
        self.wake.set()

    def subscribed(self, plugin, account, user, *, replay_live=False):
        """Initialize once, optionally including messages since this host activation."""
        with self.lock:
            if self.core.store.position(plugin, account, user) is not None:
                return
        # Source I/O must not hold the acknowledgment/activation lock. Recheck
        # after reading so a concurrent subscribe or ACK can never be rewound.
        window = self._windows(account, [user], required=True)[user]
        with self.lock:
            if self.core.store.position(plugin, account, user) is not None:
                return
            latest = _sort(window[-1]) if window else [0, "", 0]
            if replay_live:
                live_since = self.plugin_live_since.get(plugin, self.started_at_ms)
                for message in window:
                    if isinstance(message.get("time"), (int, float)) and message["time"] >= live_since:
                        first = _sort(message)
                        if first is None or type(first[2]) is not int:
                            raise PluginUnavailable("messages-unavailable", "当前微信消息尚未就绪，请稍后重试")
                        # The cursor is exclusive. Retain the first live row even
                        # when it is also the first row in this bounded window.
                        latest = [first[0], first[1], first[2] - 1]
                        break
            if latest is None:
                raise PluginUnavailable("messages-unavailable", "当前微信消息尚未就绪，请稍后重试")
            self.core.store.set_position(plugin, account, user, latest)
        self.wake.set()

    # Polling ----------------------------------------------------------------------------------------------------------
    def _windows(self, account, users, *, required=False):
        try:
            batch = self.core.source.message_windows(users, WINDOW, expected_account=account)
            if not isinstance(batch, dict) or any(not isinstance(batch.get(user), (list, tuple)) for user in users):
                raise ValueError("incomplete message windows")
            return {user: list(batch[user]) for user in users}
        except Exception:
            if required:
                raise PluginUnavailable("messages-unavailable", "当前微信消息尚未就绪，请稍后重试") from None
            return {}

    def _loop(self):
        while not self.closed.is_set():
            self.wake.wait(self.poll_seconds)
            self.wake.clear()
            if self.closed.is_set():
                return
            try:
                self.poll()
            except Exception:
                pass  # Event delivery never disturbs data maintenance.

    def poll(self):
        account = self.core.scope().get("account")
        if not account:
            return
        now = time.monotonic()
        with self.lock:
            for event_id, (plugin, owner, user, _position, deadline) in list(self.in_flight.items()):
                if deadline <= now:
                    self.in_flight.pop(event_id, None)
                    self.busy.pop((plugin, owner, user), None)
        subscriptions = [item for item in self.core.store.subscriptions(account=account, event="message.new")
                         if self.core.registry.enabled(item["plugin"])]
        if not subscriptions:
            return
        windows = self._windows(account, sorted({item["user"] for item in subscriptions}))
        for item in subscriptions:
            plugin, user = item["plugin"], item["user"]
            with self.lock:
                if (plugin, account, user) in self.busy:
                    continue
            window = windows.get(user) or []
            position = self.core.store.position(plugin, account, user)
            if position is None:
                # Subscription initialization owns its boundary; polling must
                # not guess a newer cursor while initialization is in progress.
                continue
            fresh = [message for message in window if _sort(message) is not None and _sort(message) > position]
            if not fresh:
                continue
            fresh = fresh[-MAX_EVENT_MESSAGES:]
            self._deliver(plugin, account, user, "message.new",
                          {"messages": [_brief(message) for message in fresh], "position": _sort(fresh[-1])},
                          position=_sort(fresh[-1]))

    def _deliver(self, plugin, account, user, event, data, position=None):
        event_id = "e" + uuid.uuid4().hex
        if position is not None:
            with self.lock:
                self.in_flight[event_id] = (plugin, account, user, position, time.monotonic() + ACK_TIMEOUT)
                self.busy[(plugin, account, user)] = event_id
        try:
            self.core.require_plugin(plugin, account)
            message = {"type": "event", "id": event_id, "event": event, "account": account,
                       "user": user, "data": data}
            if event == "message.new":
                # Host metadata stays outside the untrusted message payload.
                with self.lock:
                    if position is not None and event_id not in self.in_flight:
                        return False  # The plugin was re-enabled while preparing this delivery.
                    message["liveSince"] = self.plugin_live_since.get(plugin, self.started_at_ms)
            self.core.supervisor.send(plugin, message)
        except PluginUnavailable:
            with self.lock:
                self.in_flight.pop(event_id, None)
                if self.busy.get((plugin, account, user)) == event_id:
                    self.busy.pop((plugin, account, user), None)
            return False
        return True

    def _ack(self, plugin, message):
        with self.lock:
            entry = self.in_flight.pop(message.get("id"), None)
            if entry is None or entry[0] != plugin:
                return
            self.busy.pop((entry[0], entry[1], entry[2]), None)
            self.core.store.set_position(entry[0], entry[1], entry[2], entry[3])

    # Host-originated events ---------------------------------------------------------------------------------------------
    def friend_opened(self, plugin, account, user, scheme=None):
        manifest = self.core.registry.get(plugin)
        if manifest is None or "friend.opened" not in manifest["events"] or not self.core.registry.enabled(plugin):
            return False
        return self._deliver(plugin, account, user, "friend.opened", {"scheme": scheme})

    def human_changed(self, account, user, version=None):
        delivered = []
        for manifest in self.core.registry.active():
            if "human.changed" in manifest["events"] and self.core.supervisor.running(manifest["id"]) is not None:
                if self._deliver(manifest["id"], account, user, "human.changed", {"version": version}):
                    delivered.append(manifest["id"])
        return delivered
