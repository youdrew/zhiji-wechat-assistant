"""One Core per installation: a second process cannot take the Core lock until the holder exits."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BRIDGE = Path(__file__).resolve().parent
sys.path.insert(0, str(BRIDGE))

import core_instance_lock

HOLDER = """
import sys
sys.path.insert(0, sys.argv[1])
import core_instance_lock
handle = core_instance_lock.acquire(sys.argv[2])
print("held" if handle else "refused", flush=True)
sys.stdin.read()
"""


class CoreInstanceLockTests(unittest.TestCase):
    def test_second_core_is_refused_while_the_first_runs_and_admitted_after_it_exits(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / ".local" / "real-client-runtime"
            holder = subprocess.Popen([sys.executable, "-c", HOLDER, str(BRIDGE), str(runtime)],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(holder.stdout.readline().strip(), "held")
                self.assertIsNone(core_instance_lock.acquire(runtime))
            finally:
                holder.communicate(input="", timeout=10)
            # The operating system drops the lock with the holder process, however it ended.
            handle = core_instance_lock.acquire(runtime)
            self.assertIsNotNone(handle)
            handle.close()


if __name__ == "__main__":
    unittest.main()
