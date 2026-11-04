"""MCP new-message outlet (G11 "B") on synthetic sessions: detection, cursors, scope, retention."""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core_mcp_events as events_module
from core_mcp_events import EventError, McpEventOutlet, McpEventStore

FINGERPRINT = "synthetic-install-fingerprint"
CURSOR_KEY = "c" * 64
BASE_MS = 1_800_000_000_000


class Clock:
    def __init__(self):
        self.now = BASE_MS / 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MessagesUnavailableError(RuntimeError):
    pass


class FakeSource:
    """Synthetic WeChat source: sessions + message windows of one live account."""

    def __init__(self, account="wxid_self"):
        self.account = account
        self.chats = {}          # user -> list of messages
        self.unread = {}
        self.fail = None         # "raise" | "none" | "not-ready"
        self.window_calls = []
        self._seq = 0

    def add(self, user, side, time_ms, unread_delta=None):
        self._seq += 1
        self.chats.setdefault(user, []).append(
            {"id": f"m{self._seq}", "side": side, "time": time_ms, "text": "合成",
             "_sort": [self._seq, "message__message_0.db", self._seq]})
        if side == "other":
            self.unread[user] = self.unread.get(user, 0) + (1 if unread_delta is None else unread_delta)
        else:
            self.unread[user] = 0

    def sessions(self):
        if self.fail == "raise":
            raise RuntimeError("wechat closed")
        if self.fail == "none":
            return None
        items = []
        for user, messages in self.chats.items():
            last = messages[-1]
            items.append({"username": user, "name": "不应出现的名字", "time": last["time"],
                          "sortTimestamp": last["time"] // 1000, "unreadCount": self.unread.get(user, 0),
                          "lastMessageFromSelf": last["side"] == "self", "isGroup": user.endswith("@chatroom")})
        return {"account": self.account, "sessions": items, "messagesReady": self.fail != "not-ready"}

    def message_windows(self, users, limit=80, expected_account=None):
        if expected_account != self.account:
            raise RuntimeError("account changed")
        self.window_calls.append(list(users))
        return {user: list(self.chats.get(user, []))[-limit:] for user in users}


class Scope:
    def __init__(self, accounts=None, contacts=None):
        self._accounts, self.contacts = accounts, contacts

    def accounts(self):
        return self._accounts

    def allows(self, account_id, subject_type, subject_id):
        if subject_type != "wechat_contact":
            return False
        if self._accounts is not None and account_id not in self._accounts:
            return False
        return self.contacts is None or (account_id, subject_id) in self.contacts


ALL = Scope()


def principal(client="mcp_a", version=1, fingerprint=FINGERPRINT):
    return {"clientId": client, "authVersion": version, "fingerprint": fingerprint}


class OutletTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.registry = root / "accounts.json"
        self.registry.write_text(json.dumps({"accounts": [
            {"account": "wxid_self", "accountId": "acc_1", "nickname": "我"},
            {"account": "wxid_other", "accountId": "acc_2"}]}), encoding="utf-8")
        self.db_path = root / "mcp" / "events.sqlite3"
        self.clock = Clock()
        self.source = FakeSource()
        self.active = True
        self.store = None
        self.outlet = self.make_outlet()

    def make_outlet(self):
        if self.store is not None:
            self.store.close()
        self.store = McpEventStore(self.db_path)
        self.addCleanup(self.store.close)
        outlet = McpEventOutlet(lambda: self.source, self.store, self.registry, lambda: FINGERPRINT,
                                lambda: CURSOR_KEY, lambda: self.active, clock=self.clock, poll_seconds=0.05)
        self.addCleanup(outlet.close)
        return outlet

    def now_ms(self):
        return int(self.clock() * 1000)

    def step(self, seconds=5):
        self.clock.advance(seconds)
        return self.outlet.poll_once()

    def baseline(self, consumer="default", client="mcp_a"):
        result = self.outlet.read(principal(client), ALL, consumer=consumer)
        self.assertTrue(result["baseline"])
        return result["cursor"]

    # -- detection -------------------------------------------------------------------
    def test_first_poll_is_a_silent_baseline_even_with_unread(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.source.add("friend_a", "other", self.now_ms() - 30_000)
        self.assertEqual(self.outlet.poll_once(), 0)
        result = self.outlet.read(principal(), ALL)
        self.assertEqual((result["events"], result["baseline"]), ([], True))
        self.assertEqual(self.store.head_seq(), 0)
        self.assertEqual(result["source"]["state"], "live")
        self.assertEqual(result["source"]["accountId"], "acc_1")

    def test_new_inbound_message_gives_one_exact_metadata_event(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        cursor = self.baseline()
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.source.add("friend_a", "other", self.now_ms())
        self.assertEqual(self.outlet.poll_once(), 1)
        result = self.outlet.read(principal(), ALL, cursor=cursor)
        self.assertFalse(result["baseline"])
        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["type"], "message.new")
        self.assertEqual(event["subject"], {"accountId": "acc_1", "subjectType": "wechat_contact",
                                            "subjectId": "friend_a"})
        self.assertEqual((event["newCount"], event["countExact"], event["catchUp"]), (2, True, False))
        self.assertEqual((event["firstTime"], event["lastTime"]), (self.now_ms(), self.now_ms()))
        self.assertTrue(event["eventId"].startswith("ev_") and len(event["eventId"]) == 27)
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("合成", text)
        self.assertNotIn("不应出现的名字", text)
        # The next poll from the returned cursor sees nothing new.
        again = self.outlet.read(principal(), ALL, cursor=result["cursor"])
        self.assertEqual(again["events"], [])

    def test_outgoing_only_changes_produce_nothing(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        self.clock.advance(5)
        self.source.add("friend_a", "self", self.now_ms())
        self.assertEqual(self.outlet.poll_once(), 0)
        # A later inbound message counts only itself.
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.assertEqual(self.outlet.poll_once(), 1)
        row = self.store.rows_after(0, 10)[-1]
        self.assertEqual((row["new_count"], row["count_exact"]), (1, 1))

    # R2-01: a trailing own reply must not swallow earlier inbound rows of the same window.
    def _baseline_one_old_message(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        self.clock.advance(5)

    def test_inbound_then_own_reply_in_one_cycle_still_reports_the_inbound(self):
        self._baseline_one_old_message()
        self.source.add("friend_a", "other", self.now_ms())
        self.source.add("friend_a", "self", self.now_ms() + 1000)
        self.assertEqual(self.outlet.poll_once(), 1)
        self.assertEqual(self.step(), 0)
        rows = self.store.rows_after(0, 10)
        self.assertEqual([(r["subject_id"], r["new_count"], r["count_exact"]) for r in rows], [("friend_a", 1, 1)])

    def test_inbound_reply_inbound_counts_both_inbound_rows(self):
        self._baseline_one_old_message()
        self.source.add("friend_a", "other", self.now_ms())
        self.source.add("friend_a", "self", self.now_ms() + 1000)
        self.source.add("friend_a", "other", self.now_ms() + 2000)
        self.assertEqual(self.outlet.poll_once(), 1)
        row = self.store.rows_after(0, 10)[-1]
        self.assertEqual((row["new_count"], row["count_exact"]), (2, 1))

    def test_pure_own_replies_produce_nothing(self):
        self._baseline_one_old_message()
        self.source.add("friend_a", "self", self.now_ms())
        self.source.add("friend_a", "self", self.now_ms() + 1000)
        self.assertEqual(self.outlet.poll_once(), 0)
        self.assertEqual(self.step(), 0)
        self.assertEqual(self.store.rows_after(0, 10), [])

    # R2-05: the time-only baseline must not absorb an inbound row of the same second.
    def test_same_second_inbound_after_baseline_is_reported(self):
        moment = self.now_ms() - 60_000
        self.source.add("friend_a", "other", moment)
        self.outlet.poll_once()
        self.clock.advance(5)
        self.source.add("friend_a", "other", moment)
        self.assertEqual(self.outlet.poll_once(), 1)
        row = self.store.rows_after(0, 10)[-1]
        self.assertEqual((row["subject_id"], row["new_count"], row["count_exact"]), ("friend_a", 1, 0))
        self.assertEqual(self.step(), 0)

    def test_same_second_inbound_after_own_baseline_message_is_reported(self):
        moment = self.now_ms() - 60_000
        self.source.add("friend_a", "self", moment)
        self.outlet.poll_once()
        self.clock.advance(5)
        self.source.add("friend_a", "other", moment)
        self.assertEqual(self.outlet.poll_once(), 1)

    def test_window_that_misses_the_position_falls_back_to_unread_delta(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        self.clock.advance(5)
        for _ in range(45):
            self.source.add("friend_a", "other", self.now_ms())
        self.outlet.poll_once()
        row = self.store.rows_after(0, 10)[-1]
        self.assertEqual((row["new_count"], row["count_exact"]), (45, 0))

    def test_new_conversation_after_baseline_is_reported(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        self.clock.advance(5)
        self.source.add("group_1@chatroom", "other", self.now_ms())
        self.outlet.poll_once()
        cursor = self.outlet.encode_cursor("mcp_a", 1, FINGERPRINT, "default", 0)
        event = self.outlet.read(principal(), ALL, cursor=cursor)["events"][0]
        self.assertEqual((event["subject"]["subjectId"], event["isGroup"], event["newCount"]),
                         ("group_1@chatroom", True, 1))

    def test_restart_catches_up_recent_messages_and_absorbs_older_ones(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.source.add("friend_b", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        heartbeat = self.now_ms()
        self.outlet.close()
        # While Core was down: friend_a wrote after the heartbeat; friend_b's new row is
        # older than the heartbeat (it was not seen before, but predates the outage).
        self.source.add("friend_b", "other", heartbeat - 1000)
        self.clock.advance(3600)
        self.source.add("friend_a", "other", self.now_ms() - 600_000)
        self.outlet = self.make_outlet()
        self.assertEqual(self.outlet.poll_once(), 1)
        rows = self.store.rows_after(0, 10)
        self.assertEqual([(r["subject_id"], r["catch_up"], r["new_count"]) for r in rows], [("friend_a", 1, 1)])
        # Absorbed: friend_b's position advanced, so nothing more later.
        self.assertEqual(self.step(), 0)

    def test_catch_up_ignores_rows_older_than_a_day(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        self.outlet.close()
        self.clock.advance(3 * 24 * 3600)
        self.source.add("friend_a", "other", self.now_ms() - 30 * 3600 * 1000)
        self.outlet = self.make_outlet()
        self.assertEqual(self.outlet.poll_once(), 0)

    def test_replay_of_a_cycle_does_not_duplicate_keys(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        stored = self.store.positions("wxid_self")
        self.assertEqual(self.outlet.poll_once(), 1)
        # Simulate a crash before positions persisted: rewind them and poll again.
        self.store.save_cycle("wxid_self", stored, [], None, self.now_ms())
        self.clock.advance(5)
        self.assertEqual(self.outlet.poll_once(), 0)
        self.assertEqual(len(self.store.rows_after(0, 10)), 1)

    def test_unsaved_account_produces_no_events(self):
        self.source.account = "wxid_unsaved"
        self.source.add("friend_a", "other", self.now_ms())
        self.outlet.poll_once()
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.assertEqual(self.outlet.poll_once(), 0)
        self.assertEqual(self.outlet.source_state()["state"], "account-not-saved")
        self.assertNotIn("accountId", self.outlet.source_state())
        self.assertEqual(self.store.positions("wxid_unsaved"), {})

    def test_inactive_consumers_skip_cycles(self):
        self.source.add("friend_a", "other", self.now_ms())
        self.active = False
        self.outlet.poll_once()
        self.assertEqual(self.source.window_calls, [])
        self.assertEqual(self.outlet.source_state()["state"], "idle")
        self.assertEqual(self.store.positions("wxid_self"), {})

    # -- source state ----------------------------------------------------------------
    def test_source_state_transitions(self):
        self.source.add("friend_a", "other", self.now_ms())
        self.outlet.poll_once()
        cursor = self.baseline()
        self.source.fail = "raise"
        self.step()
        self.assertEqual(self.outlet.source_state()["state"], "wechat-unavailable")
        self.source.fail = "none"
        self.step()
        self.assertEqual(self.outlet.source_state()["state"], "wechat-unavailable")
        self.source.fail = "not-ready"
        self.step()
        self.assertEqual(self.outlet.source_state()["state"], "messages-not-ready")
        self.source.fail = None
        self.step()
        result = self.outlet.read(principal(), ALL, cursor=cursor)
        states = [(e["type"], e["state"], e.get("accountId")) for e in result["events"]]
        self.assertEqual(states, [("source.state", "wechat-unavailable", None),
                                  ("source.state", "messages-not-ready", "acc_1"),
                                  ("source.state", "live", "acc_1")])
        # Another account's scope sees the transitions without the accountId.
        hidden = self.outlet.read(principal("mcp_b"), Scope(accounts={"acc_2"}),
                                  cursor=self.outlet.encode_cursor("mcp_b", 1, FINGERPRINT, "default", 0))
        self.assertEqual([e.get("accountId") for e in hidden["events"]], [None, None, None])
        self.assertNotIn("accountId", hidden["source"])

    # -- scope ---------------------------------------------------------------------
    def _two_events(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.source.add("friend_b", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.source.add("friend_b", "other", self.now_ms())
        self.assertEqual(self.outlet.poll_once(), 2)

    def test_scope_filters_accounts_and_contacts(self):
        self._two_events()
        start = lambda client: self.outlet.encode_cursor(client, 1, FINGERPRINT, "default", 0)
        other = self.outlet.read(principal("mcp_b"), Scope(accounts={"acc_2"}), cursor=start("mcp_b"))
        self.assertEqual(other["events"], [])
        self.assertNotIn("accountId", other["source"])
        # The cursor still passes the invisible rows.
        self.assertEqual(self.outlet.decode_cursor(other["cursor"], "mcp_b", 1, FINGERPRINT, "default"),
                         self.store.head_seq())
        one = self.outlet.read(principal("mcp_c"), Scope(contacts={("acc_1", "friend_b")}), cursor=start("mcp_c"))
        self.assertEqual([e["subject"]["subjectId"] for e in one["events"]], ["friend_b"])

    def test_rows_of_another_source_fingerprint_are_invisible(self):
        self._two_events()
        result = self.outlet.read(principal(fingerprint="other-fp"), ALL,
                                  cursor=self.outlet.encode_cursor("mcp_a", 1, "other-fp", "default", 0))
        self.assertEqual(result["events"], [])

    # -- cursors ---------------------------------------------------------------------
    def test_cursor_bound_to_client_fingerprint_and_signature(self):
        cursor = self.baseline()
        for info in (principal("mcp_b"), principal(fingerprint="other-fp")):
            with self.assertRaises(EventError) as caught:
                self.outlet.read(info, ALL, cursor=cursor)
            self.assertEqual(caught.exception.code, "cursor-invalid")
        with self.assertRaises(EventError) as caught:
            self.outlet.read(principal(), ALL, cursor=cursor[:-2] + ("AA" if cursor[-2:] != "AA" else "BB"))
        self.assertEqual(caught.exception.code, "cursor-invalid")
        with self.assertRaises(EventError) as caught:
            self.outlet.read(principal(), ALL, cursor=cursor, consumer="other")
        self.assertEqual(caught.exception.code, "cursor-invalid")

    def test_version_bump_makes_cursor_stale_and_resume_rebaselines(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        cursor = self.baseline()
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.outlet.poll_once()
        with self.assertRaises(EventError) as caught:
            self.outlet.read(principal(version=2), ALL, cursor=cursor)
        self.assertEqual(caught.exception.code, "cursor-stale")
        resumed = self.outlet.read(principal(version=2), ALL, resume=True)
        self.assertTrue(resumed["baseline"])
        self.assertEqual(resumed["events"], [])  # Events before the revocation are never re-sent.

    def test_resume_uses_server_side_consumer_cursor(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        self.baseline()
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.outlet.poll_once()
        first = self.outlet.read(principal(), ALL, resume=True)
        self.assertEqual((first["baseline"], len(first["events"])), (False, 1))
        # Not acknowledged yet (no cursor sent back): resume re-delivers.
        self.assertEqual(len(self.outlet.read(principal(), ALL, resume=True)["events"]), 1)
        self.outlet.read(principal(), ALL, cursor=first["cursor"])  # ack
        self.assertEqual(self.outlet.read(principal(), ALL, resume=True)["events"], [])
        self.store.forget_client("mcp_a")
        self.assertTrue(self.outlet.read(principal(), ALL, resume=True)["baseline"])

    # R2-06: a late request carrying an older cursor must not roll the acknowledgement back.
    def test_late_stale_cursor_does_not_roll_back_the_acknowledgement(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        cursor0 = self.baseline()
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.outlet.poll_once()
        first = self.outlet.read(principal(), ALL, cursor=cursor0)
        self.assertEqual(len(first["events"]), 1)
        self.outlet.read(principal(), ALL, cursor=first["cursor"])        # ack seq 1
        self.outlet.read(principal(), ALL, cursor=cursor0)                # late retry of an old request
        self.assertEqual(self.outlet.read(principal(), ALL, resume=True)["events"], [])
        # A new authorization version still replaces the saved position.
        self.assertTrue(self.outlet.read(principal(version=2), ALL)["baseline"])
        self.assertEqual(self.store.consumer_cursor("mcp_a", "default")["auth_version"], 2)

    def test_two_consumers_are_independent(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        hook = self.baseline("hook")
        self.baseline("cli")
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.outlet.poll_once()
        got = self.outlet.read(principal(), ALL, cursor=hook, consumer="hook")
        self.outlet.read(principal(), ALL, cursor=got["cursor"], consumer="hook")
        self.assertEqual(len(got["events"]), 1)
        self.assertEqual(len(self.outlet.read(principal(), ALL, consumer="cli", resume=True)["events"]), 1)
        self.assertEqual(self.outlet.read(principal(), ALL, consumer="hook", resume=True)["events"], [])

    # -- long poll -------------------------------------------------------------------
    def test_long_poll_returns_early_on_new_event(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        cursor = self.baseline()

        def produce():
            time.sleep(0.3)
            self.clock.advance(5)
            self.source.add("friend_a", "other", self.now_ms())
            self.outlet.poll_once()

        threading.Thread(target=produce, daemon=True).start()
        started = time.monotonic()
        result = self.outlet.read(principal(), ALL, cursor=cursor, wait=10)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(result["events"]), 1)

    def test_long_poll_wakes_on_close(self):
        cursor = self.baseline()
        threading.Timer(0.3, self.outlet.close).start()
        started = time.monotonic()
        result = self.outlet.read(principal(), ALL, cursor=cursor, wait=20)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result["events"], [])

    def test_background_thread_polls(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        cursor = self.baseline()
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.outlet.start()
        result = self.outlet.read(principal(), ALL, cursor=cursor, wait=5)
        self.assertEqual(len(result["events"]), 1)

    # P2-8: background failures are logged (rate limited); exceptions are matched by type.
    def test_loop_failures_are_logged_and_rate_limited(self):
        calls = []

        def broken():
            calls.append(1)
            raise ValueError("synthetic failure")

        self.outlet.poll_once = broken
        with self.assertLogs("core_mcp_events", level="WARNING") as captured:
            self.outlet.start()
            deadline = time.monotonic() + 5
            while len(calls) < 4 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.outlet.close()
        self.assertGreaterEqual(len(calls), 4)
        self.assertEqual(len(captured.records), 1)
        self.assertIn("ValueError", captured.output[0])

    def test_messages_unavailable_is_recognised_by_type(self):
        from backend_contracts import MessagesUnavailableError as RealMessagesUnavailable
        self._baseline_one_old_message()
        self.source.add("friend_a", "other", self.now_ms())
        original = self.source.message_windows

        def unavailable(*_args, **_kwargs):
            raise RealMessagesUnavailable()

        self.source.message_windows = unavailable
        self.assertEqual(self.outlet.poll_once(), 0)
        self.assertEqual(self.outlet.source_state()["state"], "messages-not-ready")
        # A look-alike class name is not the contract error: logged, state unchanged.
        def look_alike(*_args, **_kwargs):
            raise MessagesUnavailableError("same name, other type")

        self.source.message_windows = look_alike
        with self.assertLogs("core_mcp_events", level="WARNING"):
            self.step()
        self.assertEqual(self.outlet.source_state()["state"], "messages-not-ready")
        self.source.message_windows = original
        self.assertEqual(self.step(), 1)

    # P2-6: a changing source fingerprint is observable and keeps detecting.
    def test_fingerprint_change_is_logged_and_recorded(self):
        current = {"fp": FINGERPRINT}
        self.outlet._fingerprint = lambda: current["fp"]
        self._baseline_one_old_message()
        self.outlet.poll_once()
        current["fp"] = "flapped-fingerprint"
        self.source.add("friend_a", "other", self.now_ms())
        with self.assertLogs("core_mcp_events", level="WARNING") as captured:
            self.assertEqual(self.step(), 1)
        self.assertIn("fingerprint", captured.output[0])
        change = self.store.get_state("source_fingerprint_change")
        self.assertEqual(change["changedAt"], self.now_ms())
        self.assertNotIn("flapped-fingerprint", json.dumps(change))
        self.assertEqual(self.store.rows_after(0, 10)[-1]["fingerprint"], "flapped-fingerprint")

    def test_parameter_limits(self):
        for kwargs in ({"wait": 26}, {"wait": -1}, {"wait": True}, {"limit": 101}, {"limit": 0},
                       {"consumer": ""}, {"consumer": "a b"}, {"resume": "yes"}):
            with self.assertRaises(EventError) as caught:
                self.outlet.read(principal(), ALL, **kwargs)
            self.assertEqual(caught.exception.code, "invalid-params")
        self.outlet.read(principal(), ALL, wait=25)  # baseline returns at once

    def test_limit_pages_events(self):
        for index in range(5):
            self.source.add(f"friend_{index}", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        cursor = self.baseline()
        self.clock.advance(5)
        for index in range(5):
            self.source.add(f"friend_{index}", "other", self.now_ms())
        self.outlet.poll_once()
        page = self.outlet.read(principal(), ALL, cursor=cursor, limit=3)
        rest = self.outlet.read(principal(), ALL, cursor=page["cursor"], limit=3)
        self.assertEqual((len(page["events"]), len(rest["events"])), (3, 2))

    # -- retention / forgetting ------------------------------------------------------
    def test_retention_prunes_and_reports_gap(self):
        self.source.add("friend_a", "other", self.now_ms() - 60_000)
        self.outlet.poll_once()
        cursor = self.baseline()
        old = events_module.RETENTION_ROWS
        events_module.RETENTION_ROWS = 2
        self.addCleanup(setattr, events_module, "RETENTION_ROWS", old)
        for _ in range(4):
            self.clock.advance(5)
            self.source.add("friend_a", "other", self.now_ms())
            self.outlet.poll_once()
        result = self.outlet.read(principal(), ALL, cursor=cursor)
        self.assertTrue(result["gap"])
        self.assertEqual([e["seq"] for e in result["events"]], [3, 4])
        self.assertFalse(self.outlet.read(principal(), ALL, cursor=result["cursor"])["gap"])

    def test_age_retention(self):
        self._two_events()
        self.clock.advance(8 * 24 * 3600)
        self.source.add("friend_a", "other", self.now_ms())
        self.outlet.poll_once()
        self.assertEqual([r["seq"] for r in self.store.rows_after(0, 10)], [3])

    def test_forget_account_purges(self):
        self._two_events()
        self.store.forget_account("wxid_self")
        self.assertEqual(self.store.rows_after(0, 10), [])
        self.assertEqual(self.store.positions("wxid_self"), {})
        self.assertIsNone(self.store.get_state("heartbeat:wxid_self"))
        # The next sighting is a fresh silent baseline.
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.assertEqual(self.outlet.poll_once(), 0)

    def account_rows(self):
        """Rows tied to the cleared account (a state-only ``source.state`` row carries none)."""
        return [row for row in self.store.rows_after(0, 50)
                if row["account"] == "wxid_self" or row["account_id"] == "acc_1"]

    def test_a_cycle_racing_an_account_clear_never_re_adds_it(self):
        self._two_events()
        # The clear purges the account while a cycle is between reading the registry and
        # persisting: the cycle's positions, heartbeat and rows must not come back.
        original = self.store.save_cycle

        def racing_save(*args, **kwargs):
            self.store.forget_account("wxid_self")
            return original(*args, **kwargs)

        self.store.save_cycle = racing_save
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.assertEqual(self.outlet.poll_once(), 0)
        self.store.save_cycle = original
        self.assertEqual(self.account_rows(), [])
        self.assertEqual(self.store.positions("wxid_self"), {})
        self.assertIsNone(self.store.get_state("heartbeat:wxid_self"))
        # Further cycles while the clear is still in progress (account not yet out of the
        # registry) store nothing either, not even a silent baseline or a source.state row.
        self.step()
        self.assertEqual(self.store.positions("wxid_self"), {})
        self.assertEqual(self.account_rows(), [])
        # The registry write completes the clear; a later re-save starts from a fresh baseline.
        self.registry.write_text(json.dumps({"accounts": [{"account": "wxid_other", "accountId": "acc_2"}]}),
                                 encoding="utf-8")
        self.step()
        self.registry.write_text(json.dumps({"accounts": [
            {"account": "wxid_self", "accountId": "acc_1"}]}), encoding="utf-8")
        self.step()
        self.assertTrue(self.store.positions("wxid_self"))

    def test_registry_is_rechecked_right_before_persisting(self):
        self._two_events()
        original = self.store.save_cycle

        def cleared_meanwhile(*args, **kwargs):
            self.registry.write_text(json.dumps({"accounts": []}), encoding="utf-8")
            return original(*args, **kwargs)

        self.store.save_cycle = cleared_meanwhile
        before = self.store.positions("wxid_self")
        self.clock.advance(5)
        self.source.add("friend_a", "other", self.now_ms())
        self.assertEqual(self.outlet.poll_once(), 0)
        self.assertEqual(self.store.positions("wxid_self"), before)
        self.assertEqual(len([row for row in self.store.rows_after(0, 10) if row["type"] == "message.new"]), 2)


if __name__ == "__main__":
    unittest.main()
