"""Core's account data service: per-account databases, the chat projection and 关系记忆.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
Split out of advisor_service.py, 2026-10-13 (plugin architecture): the Core half of the old
AdvisorService. It keeps the same attribute names (``stores``, ``context``, ``friends``,
``source``) so FriendService, HistoryService and HumanService work unchanged. Model runs,
the assistant catalog and the engine belong to the kyf.advisor plugin.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from backend_contracts import AccountChangedError
from core_account_db import AccountStores
from core_contracts import AdvisorError, valid_scope_value
from core_projection import ContextProjection


class AccountDataService:
    def __init__(self, source, root):
        self.source = source
        self.root = Path(os.path.abspath(root))
        self.stores = AccountStores(self.root)
        self.context = ContextProjection(self.stores, source)
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self._paused = set()
        self._closing = False
        self._cleared = set()
        self._drained = set()
        from friend_service import FriendService
        self.friends = FriendService(self)

    # ------------------------------------------------------------------ scope
    def _require_scope(self, account, with_fingerprint=False):
        account = valid_scope_value(account, "account")
        with self.lock:
            if self._closing:
                raise AdvisorError("account-closed", "账号数据服务已关闭")
            if account in self._paused:
                raise AdvisorError("account-paused", "账号正在清理中")
        fingerprint = self.context.source_scope(account)
        return (account, fingerprint) if with_fingerprint else account

    # ---------------------------------------------------------------- context
    def prepare_context(self, account, user):
        account = self._require_scope(account)
        user = valid_scope_value(user, "user")
        snapshot = self.context.prepare(account, user)
        result = {"revision": snapshot["revision"]}
        if snapshot.get("error"):
            result["error"] = snapshot["error"]
        return {"context": result}

    def observe_message_window(self, account, workdir, user, messages, has_more_before):
        """Fold a chat window the backend already read and verified into the projection."""
        account = valid_scope_value(account, "account")
        user = valid_scope_value(user, "user")
        with self.condition:
            if self._closing or account in self._paused or account in self._cleared:
                return False
        try:
            observed = self.context.scope_fingerprint(account, workdir)
            if self.context.source_scope(account) != observed:
                raise AccountChangedError()
            return self.context.observe_window(account, workdir, user, messages, has_more_before)
        except Exception:
            self.context.invalidate_window(account, user)
            return False

    # ------------------------------------------------------------- lifecycle
    def pause_for_account_clear(self, account):
        """Drain every writer of one account and close its database."""
        account = valid_scope_value(account, "account")
        self.friends.pause_account(account)
        with self.condition:
            if self._closing:
                raise RuntimeError("account data service is closing")
            self._paused.add(account)
        if not self.context.cancel_account(account):
            raise RuntimeError("context import did not finish")
        self.stores.block_account(account)
        self.stores.release_account(account)
        with self.condition:
            self._drained.add(account)

    def resume_after_failed_account_clear(self):
        with self.condition:
            if self._closing:
                raise RuntimeError("account data service is closing")
            resumed = (self._paused & self._drained) - self._cleared
            for account in resumed:
                self.stores.unblock_account(account)
                self.context.resume_account(account)
            self._paused.difference_update(resumed)
            self._drained.difference_update(resumed)
        self.friends.resume_accounts(resumed)

    def clear_account(self, account):
        """Remove this account's database directory after draining its writers."""
        account = valid_scope_value(account, "account")
        if account in self._cleared:
            return {"deleted": False}
        if account not in self._paused or account not in self.stores._blocked:
            self.pause_for_account_clear(account)
        result = self.stores.remove_account(account)
        with self.condition:
            self._cleared.add(account)
        return result

    def shutdown(self, account=None):
        """Stop imports and close every account database."""
        if account is not None:
            valid_scope_value(account, "account")
        self.friends.close()
        with self.condition:
            if self.stores._closed:
                return
            self._closing = True
            self.condition.notify_all()
        self.context.close()
        self.stores.close()
