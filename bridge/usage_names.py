"""Account-scoped display names learned from ordinary session reads.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Usage statistics never open WeChat data to populate this small memory cache.
Only usernames and display names survive the session response; a generation
ticket prevents reads started before account clearing or shutdown from restoring
discarded names.
"""
from collections import OrderedDict
import threading


class UsageNameCache:
    def __init__(self, account_limit=8):
        self.account_limit = account_limit
        self.lock = threading.Lock()
        self.names = OrderedDict()
        self.generation = 0
        self.closed = False

    def ticket(self):
        with self.lock:
            return None if self.closed else self.generation

    def observe(self, payload, ticket):
        if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
            return
        account = payload.get("account")
        if not isinstance(account, str) or not account:
            return
        names = {item["username"]: item["name"] for item in payload["sessions"]
                 if isinstance(item, dict) and isinstance(item.get("username"), str) and item["username"]
                 and isinstance(item.get("name"), str) and item["name"]}
        with self.lock:
            if self.closed or type(ticket) is not int or ticket != self.generation:
                return
            self.names[account] = names
            self.names.move_to_end(account)
            while len(self.names) > self.account_limit:
                self.names.popitem(last=False)

    def get(self, account):
        with self.lock:
            names = self.names.get(account)
            if names is None:
                return {}
            self.names.move_to_end(account)
            return dict(names)

    def clear(self, account):
        with self.lock:
            self.generation += 1
            self.names.pop(account, None)

    def close(self):
        with self.lock:
            self.closed = True
            self.generation += 1
            self.names.clear()
