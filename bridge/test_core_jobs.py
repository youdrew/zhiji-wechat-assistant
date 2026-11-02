"""Windows Job Object cleanup closes descendants and their files before returning.

All processes and files are synthetic. The child is spawned only after its
leader has been adopted, exactly as the plugin control-channel handshake does.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core_jobs import ProcessGroup  # noqa: E402

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    WINDOWS = ctypes.WinDLL("kernel32", use_last_error=True)
    WINDOWS.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    WINDOWS.OpenProcess.restype = wintypes.HANDLE
    WINDOWS.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    WINDOWS.WaitForSingleObject.restype = wintypes.DWORD
    WINDOWS.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    WINDOWS.TerminateProcess.restype = wintypes.BOOL
    WINDOWS.CloseHandle.argtypes = (wintypes.HANDLE,)
    WINDOWS.CloseHandle.restype = wintypes.BOOL

CHILD = """
import sys, time
held = open(sys.argv[1], "wb")
held.write(b"synthetic child owns this file")
held.flush()
print("child-ready", flush=True)
time.sleep(60)
"""

LEADER = """
import json, os, subprocess, sys, time
sys.stdin.readline()
held = open(sys.argv[1], "wb")
held.write(b"synthetic leader owns this file")
held.flush()
child = subprocess.Popen([sys.executable, "-u", "-c", sys.argv[3], sys.argv[2]],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
ready = child.stdout.readline().strip()
print(json.dumps({"child": child.pid, "ready": ready}), flush=True)
time.sleep(60)
"""


@unittest.skipUnless(os.name == "nt", "Windows Job Object and Windows file-sharing semantics")
class WindowsProcessGroupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "synthetic-owned"
        self.directory.mkdir()
        self.files = [self.directory / "leader-open.bin", self.directory / "child-open.bin"]
        self.group = ProcessGroup()
        self.child_pid = None
        self.child_handle = None
        self.process = subprocess.Popen([sys.executable, "-u", "-c", LEADER,
            str(self.files[0]), str(self.files[1]), CHILD], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, **ProcessGroup.popen_options())
        self.addCleanup(self.cleanup_processes)
        self.group.adopt(self.process)
        self.process.stdin.write("start owned child\n")
        self.process.stdin.flush()
        output = []
        ready = threading.Event()

        def read_ready():
            output.append(self.process.stdout.readline())
            ready.set()

        reader = threading.Thread(target=read_ready, daemon=True)
        reader.start()
        self.assertTrue(ready.wait(10), "synthetic leader and child did not start")
        reader.join(1)
        self.assertTrue(output[0].strip(), "synthetic leader exited before child became ready")
        state = json.loads(output[0])
        self.assertEqual(state["ready"], "child-ready")
        self.child_pid = state["child"]
        self.child_handle = WINDOWS.OpenProcess(0x100001, False, self.child_pid)  # SYNCHRONIZE | TERMINATE
        self.assertTrue(self.child_handle)
        self.assertIsNone(self.process.poll())
        # Establish that these are real open handles, not merely marker files.
        for file in self.files:
            with self.assertRaises(PermissionError):
                file.unlink()

    def cleanup_processes(self):
        try:
            self.group.terminate()
        finally:
            self.group.close()
        if self.child_handle is not None:
            # An owned handle remains tied to this process even if its PID is reused.
            if WINDOWS.WaitForSingleObject(self.child_handle, 0) != 0:
                WINDOWS.TerminateProcess(self.child_handle, 1)
                WINDOWS.WaitForSingleObject(self.child_handle, 5000)
            WINDOWS.CloseHandle(self.child_handle)
            self.child_handle = None
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()

    def assert_immediately_reclaimable(self):
        # No sleeps, process.wait(), or retry loop may hide a cleanup race.
        self.assertIsNotNone(self.process.poll(), "the group returned before its leader exited")
        self.assertEqual(WINDOWS.WaitForSingleObject(self.child_handle, 0), 0,
                         "the group returned before its child exited")
        for file in self.files:
            file.unlink()
        self.directory.rmdir()
        self.assertFalse(self.directory.exists())

    def test_terminate_waits_for_leader_and_child_handles_to_close(self):
        self.group.terminate()
        self.assert_immediately_reclaimable()
        self.group.terminate()  # Idempotent after all members have exited.

    def test_close_waits_for_the_whole_job_before_releasing_its_handle(self):
        self.group.close()
        self.assert_immediately_reclaimable()
        self.group.close()

    def test_concurrent_terminate_and_close_are_safe_and_joined(self):
        barrier = threading.Barrier(3)
        errors = []

        def stop(action):
            try:
                barrier.wait(timeout=5)
                action()
            except BaseException as exc:
                errors.append(exc)

        workers = [threading.Thread(target=stop, args=(action,), daemon=True)
                   for action in (self.group.terminate, self.group.close)]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=5)
        for worker in workers:
            worker.join(timeout=10)
        self.assertFalse(any(worker.is_alive() for worker in workers), "concurrent group shutdown deadlocked")
        self.assertEqual(errors, [])
        self.assert_immediately_reclaimable()
        self.group.terminate()
        self.group.close()


if __name__ == "__main__":
    unittest.main()
