"""Synthetic Restart Manager cache checks, with no Windows ownership or process access."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import live_source


class OwnershipCacheTests(unittest.TestCase):
    def setUp(self):
        live_source.reset_ownership_cache()
        self.addCleanup(live_source.reset_ownership_cache)

    def test_only_explicit_ordinary_queries_reuse_answers(self):
        with patch("live_source.file_owners", return_value=((42, 10.0),)) as query:
            self.assertEqual(live_source._owned_processes(["synthetic-a.db"], fresh=False), ((42, 10.0),))
            live_source._owned_processes(["synthetic-a.db"], fresh=False)
            self.assertEqual(query.call_count, 1)
            live_source._owned_processes(["synthetic-a.db"])
            live_source._owned_processes(["synthetic-a.db"])
            self.assertEqual(query.call_count, 3)
            live_source._owned_processes(["synthetic-b.db"], fresh=False)
            self.assertEqual(query.call_count, 4)

    def test_ttl_starts_before_query_not_on_completion(self):
        clock = [100.0]
        def slow(_paths):
            clock[0] += 2.0
            return ((42, 10.0),)
        with patch("live_source.time.monotonic", side_effect=lambda: clock[0]), \
                patch("live_source.file_owners", side_effect=slow) as query:
            live_source._owned_processes(["synthetic-a.db"], fresh=False)
            live_source._owned_processes(["synthetic-a.db"], fresh=False)
            self.assertEqual(query.call_count, 2)

    def test_same_key_concurrent_ordinary_queries_are_single_flight(self):
        entered, release = threading.Event(), threading.Event()
        results, failures = [], []
        def query(_paths):
            entered.set()
            self.assertTrue(release.wait(2))
            return ((42, 10.0),)
        def call():
            try:
                results.append(live_source._owned_processes(["synthetic-a.db"], fresh=False))
            except Exception as exc:
                failures.append(exc)
        with patch("live_source.file_owners", side_effect=query) as probe:
            threads = [threading.Thread(target=call) for _ in range(6)]
            for thread in threads:
                thread.start()
            self.assertTrue(entered.wait(1))
            release.set()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
            self.assertEqual(probe.call_count, 1)
        self.assertFalse(failures)
        self.assertEqual(results, [((42, 10.0),)] * 6)

    def test_reset_during_query_blocks_old_answer_return_and_cache_fill(self):
        entered, release = threading.Event(), threading.Event()
        results, failures = [], []
        def query(_paths):
            entered.set()
            self.assertTrue(release.wait(2))
            return ((42, 10.0),)
        def call():
            try:
                results.append(live_source._owned_processes(["synthetic-a.db"], fresh=False))
            except OSError as exc:
                failures.append(str(exc))
        with patch("live_source.file_owners", side_effect=query):
            thread = threading.Thread(target=call)
            thread.start()
            self.assertTrue(entered.wait(1))
            live_source.reset_ownership_cache()
            release.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(failures, ["ownership query was invalidated"])
        with patch("live_source.file_owners", return_value=((43, 20.0),)) as probe:
            self.assertEqual(live_source._owned_processes(["synthetic-a.db"], fresh=False), ((43, 20.0),))
            self.assertEqual(probe.call_count, 1)

    def test_fresh_waiter_never_accepts_the_earlier_in_flight_answer(self):
        entered, release = threading.Event(), threading.Event()
        ordinary, verified = [], []
        calls = []
        def query(_paths):
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                entered.set()
                self.assertTrue(release.wait(2))
            return ((42, float(len(calls))),)
        with patch("live_source.file_owners", side_effect=query):
            one = threading.Thread(target=lambda: ordinary.append(live_source._owned_processes(["synthetic-a.db"], fresh=False)))
            two = threading.Thread(target=lambda: verified.append(live_source._owned_processes(["synthetic-a.db"])))
            one.start()
            self.assertTrue(entered.wait(1))
            two.start()
            release.set()
            one.join(2)
            two.join(2)
        self.assertEqual(ordinary, [((42, 1.0),)])
        self.assertEqual(verified, [((42, 2.0),)])

    def test_failed_fresh_query_drops_prior_cached_success(self):
        with patch("live_source.file_owners", return_value=((42, 10.0),)):
            live_source._owned_processes(["synthetic-a.db"], fresh=False)
        with patch("live_source.file_owners", side_effect=OSError("synthetic denied")):
            with self.assertRaises(OSError):
                live_source._owned_processes(["synthetic-a.db"])
        with patch("live_source.file_owners", return_value=()) as probe:
            self.assertEqual(live_source._owned_processes(["synthetic-a.db"], fresh=False), ())
            self.assertEqual(probe.call_count, 1)

    def test_resource_order_and_duplicates_do_not_create_extra_queries(self):
        with patch("live_source.file_owners", return_value=((42, 10.0),)) as query:
            live_source._owned_processes(["synthetic-b.db", "synthetic-a.db", "synthetic-a.db"], fresh=False)
            live_source._owned_processes(["synthetic-a.db", "synthetic-b.db"], fresh=False)
            self.assertEqual(query.call_count, 1)

    def test_ttl_expiry_queries_actual_owner_again(self):
        clock = [100.0]
        with patch("live_source.time.monotonic", side_effect=lambda: clock[0]), \
                patch("live_source.file_owners", side_effect=[((42, 10.0),), ()]) as query:
            self.assertEqual(live_source._owned_processes(["synthetic-a.db"], fresh=False), ((42, 10.0),))
            clock[0] += live_source.OWNERSHIP_TTL_SECONDS + .01
            self.assertEqual(live_source._owned_processes(["synthetic-a.db"], fresh=False), ())
            self.assertEqual(query.call_count, 2)

    def test_default_fresh_sees_same_pid_account_switch_and_reset_invalidates_reuse(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        accounts = []
        for name in ("account-a", "account-b"):
            storage = root / name / "db_storage"
            storage.mkdir(parents=True)
            (storage / "session.db").write_bytes(b"synthetic file")
            accounts.append(SimpleNamespace(path=storage))
        selected = ["account-a"]
        def query(paths):
            return ((42, 10.0),) if Path(paths[0]).parent.parent.name == selected[0] else ()
        psutil = SimpleNamespace(Process=lambda _pid: SimpleNamespace(name=lambda: "Weixin.exe", create_time=lambda: 10.0),
                                 NoSuchProcess=RuntimeError, AccessDenied=PermissionError)
        processes = [SimpleNamespace(pid=42)]
        with patch("live_source.file_owners", side_effect=query), patch.object(live_source.discovery, "psutil", psutil):
            first = live_source._account_from_file_owners(accounts, processes, fresh=False)
            self.assertEqual(first.account_dir, root / "account-a")
            selected[0] = "account-b"
            self.assertEqual(live_source._account_from_file_owners(accounts, processes).account_dir, root / "account-b")
            self.assertEqual(live_source._account_from_file_owners(accounts, processes, fresh=False).account_dir, root / "account-b")
            selected[0] = "account-a"
            live_source.reset_ownership_cache()
            self.assertEqual(live_source._account_from_file_owners(accounts, processes, fresh=False).account_dir, root / "account-a")

    def test_cached_pid_creation_and_name_are_rechecked(self):
        account = SimpleNamespace(path="C:/synthetic/account-a/db_storage")
        current = {"created": 10.0, "name": "Weixin.exe"}
        psutil = SimpleNamespace(Process=lambda _pid: SimpleNamespace(name=lambda: current["name"], create_time=lambda: current["created"]),
                                 NoSuchProcess=RuntimeError, AccessDenied=PermissionError)
        with patch("live_source._account_database_files", return_value=["synthetic-a.db"]), \
                patch("live_source.file_owners", return_value=((42, 10.0),)), \
                patch.object(live_source.discovery, "psutil", psutil):
            self.assertIsNotNone(live_source._account_from_file_owners([account], [SimpleNamespace(pid=42)], fresh=False))
            current["created"] = 20.0
            self.assertIsNone(live_source._account_from_file_owners([account], [SimpleNamespace(pid=42)], fresh=False))
            current["created"], current["name"] = 10.0, "not-wechat.exe"
            self.assertIsNone(live_source._account_from_file_owners([account], [SimpleNamespace(pid=42)], fresh=False))


if __name__ == "__main__":
    unittest.main()
