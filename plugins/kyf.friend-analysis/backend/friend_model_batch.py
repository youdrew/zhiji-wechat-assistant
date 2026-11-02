"""One window's independent model requests; no business state or database writes.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Worker width follows the three evidence channels. Core alone admits model work.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from dataclasses import dataclass
import threading
import time

from kyf_plugin import Cancelled


@dataclass(frozen=True)
class Generation:
    result: dict
    seconds: float
    chars: int


def generate(models, task, request, step):
    began = time.monotonic()
    result = models.generate(task, request, step=step)
    return Generation(result, time.monotonic() - began, len(request["system"]) + len(request["prompt"]))


class _Cancellation:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event.is_set() for event in self.events)

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            self.events[-1].wait(.05 if remaining is None else min(.05, remaining))
        return True


class _Task:
    """Same execution token; stopping this window never stops its parent task."""
    def __init__(self, parent, entry_cancel, local_cancel):
        self.parent = parent
        self.cancelled = _Cancellation(parent.cancelled, entry_cancel, local_cancel)

    def __getattr__(self, name):
        return getattr(self.parent, name)

    def check(self):
        if self.cancelled.is_set():
            raise Cancelled()
        self.parent.check()


class ModelBatch:
    def __init__(self, models, task, entry_cancel, calls):
        if not 1 <= len(calls) <= 3:
            raise ValueError("invalid-evidence-channel-count")
        self.cancel = threading.Event()
        self.task = _Task(task, entry_cancel, self.cancel)
        self.pool = ThreadPoolExecutor(max_workers=len(calls), thread_name_prefix="friend-evidence")
        self.futures = []
        try:
            for request, step in calls:
                self.futures.append(self.pool.submit(generate, models, self.task, request, step))
        except BaseException:
            self.close()
            raise

    def result(self, index):
        # Keep channel-order error precedence: a later truncation must not replace
        # an earlier in-flight channel's error and change the existing split policy.
        while True:
            self.task.check()
            try:
                return self.futures[index].result(timeout=.2)
            except TimeoutError:
                if self.futures[index].done():
                    return self.futures[index].result()

    def close(self):
        self.cancel.set()
        for future in self.futures:
            future.cancel()
        # SDK waiters observe the facade and abort only their own request IDs.
        # Join all SDK callers before retry/split/return. Core keeps admission
        # slots until its asynchronous transport-abort cleanup has completed.
        self.pool.shutdown(wait=True, cancel_futures=True)

    def completed(self):
        """Successful calls, including speculative results discarded by a failure."""
        return [(index, future.result()) for index, future in enumerate(self.futures)
                if future.done() and not future.cancelled() and future.exception() is None]
