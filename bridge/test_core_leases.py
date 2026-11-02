"""Lease semantics of the shared Core, on synthetic data: kinds never kill each other,
dead holders are reclaimed and the idle exit fires only after the last lease ended."""

import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_leases import DEFAULT_IDLE_EXIT_SECONDS, LeaseService, idle_exit_seconds, pid_alive


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_leases(idle=DEFAULT_IDLE_EXIT_SECONDS):
    clock = Clock()
    return LeaseService(idle_seconds=idle, monotonic=clock), clock


class KindIsolationTests(unittest.TestCase):
    def test_kinds_never_kill_each_other(self):
        leases, _clock = make_leases()
        leases.acquire("gui", "window-1", pid=4321)
        leases.acquire("mcp", "conn-a", pid=4322)
        leases.acquire("task", "task-1")
        self.assertEqual((leases.count(), leases.count("gui"), leases.count("mcp"), leases.count("task")),
                         (3, 1, 1, 1))
        # A GUI exit releases only the GUI lease; MCP activity and tasks keep Core alive.
        leases.release("gui", "window-1")
        self.assertFalse(leases.should_exit())
        self.assertEqual(leases.count("mcp"), 1)
        # An MCP disconnect does not stop a GUI that is still attached either.
        leases.release("mcp", "conn-a")
        self.assertFalse(leases.should_exit())
        leases.release("task", "task-1")
        # The last lease ended; the idle period must pass before the exit is due.
        self.assertFalse(leases.should_exit())

    def test_reacquire_refreshes_instead_of_duplicating(self):
        leases, clock = make_leases()
        leases.acquire("mcp", "conn-a", pid=10)
        clock.advance(60)
        leases.acquire("mcp", "conn-a", pid=10)
        self.assertEqual(leases.count("mcp"), 1)
        leases.refresh("mcp", "conn-a")
        leases.release("mcp", "conn-a")
        self.assertEqual(leases.idle_deadline(), clock.now + DEFAULT_IDLE_EXIT_SECONDS)

    def test_unknown_kind_or_key_is_rejected(self):
        leases, _clock = make_leases()
        with self.assertRaises(ValueError):
            leases.acquire("everything", "key")
        with self.assertRaises(ValueError):
            leases.acquire("gui", "")
        with self.assertRaises(ValueError):
            leases.acquire("gui", "x" * 257)


class DeathReclaimTests(unittest.TestCase):
    def test_dead_holder_process_is_reclaimed(self):
        leases, _clock = make_leases()
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            leases.acquire("mcp", "conn-a", pid=holder.pid)
            self.assertEqual(leases.reap_dead(), [])
            self.assertEqual(leases.count("mcp"), 1)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not holder.poll() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(leases.reap_dead(), [("mcp", "conn-a")])
        self.assertEqual(leases.count("mcp"), 0)

    def test_pid_alive_rejects_gone_and_invalid_pids(self):
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.assertTrue(pid_alive(holder.pid))
        finally:
            holder.terminate()
            holder.wait(timeout=10)
        deadline = time.monotonic() + 10
        while holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(pid_alive(holder.pid))
        self.assertFalse(pid_alive(None))
        self.assertFalse(pid_alive(0))
        self.assertFalse(pid_alive(-5))


class IdleExitTests(unittest.TestCase):
    def test_exit_only_after_idle_period_following_the_last_release(self):
        leases, clock = make_leases(idle=300)
        leases.acquire("gui", "window-1", pid=10)
        clock.advance(60)
        leases.refresh("gui", "window-1")
        clock.advance(400)
        self.assertFalse(leases.should_exit())  # Still held: activity does not matter.
        leases.release("gui", "window-1")
        self.assertFalse(leases.should_exit())
        clock.advance(299)
        self.assertFalse(leases.should_exit())
        clock.advance(2)
        self.assertTrue(leases.should_exit())
        # A new holder cancels the pending exit.
        leases.acquire("mcp", "conn-a", pid=11)
        self.assertFalse(leases.should_exit())

    def test_never_held_leases_keep_core_running(self):
        leases, clock = make_leases(idle=300)
        clock.advance(100000)
        self.assertIsNone(leases.idle_deadline())
        self.assertFalse(leases.should_exit())

    def test_idle_limit_is_configurable_and_can_be_disabled(self):
        self.assertEqual(idle_exit_seconds({"KYF_CORE_IDLE_EXIT_SECONDS": "120"}), 120)
        self.assertEqual(idle_exit_seconds({"KYF_CORE_IDLE_EXIT_SECONDS": "junk"}),
                         DEFAULT_IDLE_EXIT_SECONDS)
        self.assertEqual(idle_exit_seconds({"KYF_CORE_IDLE_EXIT_SECONDS": "0"}), 0)
        self.assertEqual(idle_exit_seconds({}), DEFAULT_IDLE_EXIT_SECONDS)
        leases, clock = make_leases(idle=0)
        leases.acquire("gui", "window-1")
        leases.release("gui", "window-1")
        clock.advance(100000)
        self.assertFalse(leases.should_exit())  # Disabled: never exit on idle.


if __name__ == "__main__":
    unittest.main()
