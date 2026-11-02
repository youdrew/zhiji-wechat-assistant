"""Synthetic Restart Manager calls; no process or private file access."""
import ctypes
import unittest
from ctypes import wintypes
from unittest.mock import patch

import windows_file_owners as owners


def uint(pointer):
    return ctypes.cast(pointer, ctypes.POINTER(wintypes.UINT)).contents


class RestartManager:
    def __init__(self, replies=(), start_status=0, register_status=0):
        self.replies = iter(replies)
        self.start_status = start_status
        self.register_status = register_status
        self.capacities = []
        self.ended = 0
        self.paths = ()

    def RmStartSession(self, _session, _flags, _key):
        return self.start_status

    def RmRegisterResources(self, _session, count, paths, *_unused):
        self.paths = tuple(paths[:count])
        return self.register_status

    def RmGetList(self, _session, needed, count, entries, _reasons):
        capacity = uint(count).value
        assert entries is not None and len(entries) == capacity
        self.capacities.append(capacity)
        status, required, result_count, processes = next(self.replies)
        uint(needed).value = required
        uint(count).value = result_count
        for index, (pid, created) in enumerate(processes):
            entries[index].process.pid = pid
            ticks = int((created + 11_644_473_600) * 10_000_000)
            entries[index].process.started.dwLowDateTime = ticks & 0xffffffff
            entries[index].process.started.dwHighDateTime = ticks >> 32
        return status

    def RmEndSession(self, _session):
        self.ended += 1
        return 0


class FileOwnerTests(unittest.TestCase):
    def test_preallocated_query_returns_all_process_generations_in_one_fresh_call(self):
        api = RestartManager([(0, 2, 2, [(42, 1000), (43, 2000)])])
        with patch.object(owners, "_api", return_value=api):
            self.assertEqual(owners.file_owners(["synthetic.db", "synthetic.db"]),
                             ((42, 1000.0), (43, 2000.0)))
        self.assertEqual(api.capacities, [64])
        self.assertEqual(api.paths, ("synthetic.db",))
        self.assertEqual(api.ended, 1)

    def test_empty_owner_result_needs_one_query_and_no_paths_needs_none(self):
        api = RestartManager([(0, 0, 0, [])])
        with patch.object(owners, "_api", return_value=api) as factory:
            self.assertEqual(owners.file_owners([]), ())
            factory.assert_not_called()
            self.assertEqual(owners.file_owners(["synthetic.db"]), ())
        self.assertEqual(api.capacities, [64])
        self.assertEqual(api.ended, 1)

    def test_owner_population_growth_requires_new_query_and_discards_partial_answer(self):
        api = RestartManager([(234, 65, 64, [(999, 1000)]),
                              (234, 300, 128, [(998, 1000)]),
                              (0, 1, 1, [(42, 2000)])])
        with patch.object(owners, "_api", return_value=api):
            self.assertEqual(owners.file_owners(["synthetic.db"]), ((42, 2000.0),))
        self.assertEqual(api.capacities, [64, 128, 300])
        self.assertEqual(api.ended, 1)

    def test_bad_or_unbounded_responses_fail_closed_and_end_session(self):
        for reply in [(5, 0, 0, []), (234, 4097, 0, []), (234, 64, 0, []),
                      (0, 65, 0, []), (0, 0, 65, [])]:
            with self.subTest(reply=reply):
                api = RestartManager([reply])
                with patch.object(owners, "_api", return_value=api), self.assertRaises(OSError):
                    owners.file_owners(["synthetic.db"])
                self.assertEqual(api.capacities, [64])
                self.assertEqual(api.ended, 1)

    def test_repeated_growth_has_bounded_attempts(self):
        api = RestartManager([(234, capacity + 1, 0, []) for capacity in (64, 128, 256, 512)])
        with patch.object(owners, "_api", return_value=api), self.assertRaisesRegex(OSError, "changed repeatedly"):
            owners.file_owners(["synthetic.db"])
        self.assertEqual(api.capacities, [64, 128, 256, 512])
        self.assertEqual(api.ended, 1)

    def test_failed_registration_ends_session_and_failed_start_does_not(self):
        for start, register, ended in ((5, 0, 0), (0, 5, 1)):
            with self.subTest(start=start, register=register):
                api = RestartManager(start_status=start, register_status=register)
                with patch.object(owners, "_api", return_value=api), self.assertRaises(OSError):
                    owners.file_owners(["synthetic.db"])
                self.assertEqual(api.capacities, [])
                self.assertEqual(api.ended, ended)


if __name__ == "__main__":
    unittest.main()
