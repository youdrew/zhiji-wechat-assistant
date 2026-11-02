"""Synthetic corpus and storage fixtures shared by period-analysis tests.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
import datetime
import tempfile
from pathlib import Path

from self_store import Store


def ms(year, month, day=10, hour=12):
    return int(datetime.datetime(year, month, day, hour).timestamp() * 1000)


class Corpus:
    def __init__(self, rows, chats):
        self.lines, self.chats = rows, chats

    def rows(self, query, values=()):
        if "min(time_ms)" in query:
            times = [row["time_ms"] for row in self.lines]
            return [{"first": min(times), "last": max(times)}]
        if "FROM profile_corpus_conversations" in query:
            group = 1 if "is_group=1" in query else 0
            return [{"user": user, "name": name} for user, (name, is_group) in self.chats.items() if is_group == group]
        raise AssertionError(f"unexpected synthetic corpus query: {query}")

    def messages_between(self, start, end):
        return [dict(row) for row in self.lines if start <= row["time_ms"] < end]

    def around(self, identifier, before=3, after=2):
        return {"before": [], "after": []}


def fresh_store(test):
    directory = tempfile.TemporaryDirectory()
    test.addCleanup(directory.cleanup)
    store = Store(Path(directory.name), "synthetic")
    test.addCleanup(store.close)
    return store
