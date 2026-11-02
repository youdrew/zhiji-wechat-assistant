"""The default scheme's task and automatic-update rules, shared by the plugin entry and its tests.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
"""
from __future__ import annotations

from analysis_store import FriendStore
from contracts import AdvisorError
from kyf_plugin import CoreError
from kyf_plugin.settings import read_settings

SCHEME = "default"


def run_scheme(analysis, accounts, task):
    """Run one Core scheme task to its end and report the final state to Core."""
    try:
        state = analysis.run_task(task)
    except AdvisorError as exc:
        task.finish("failed", {"code": exc.code, "message": exc.message})
        return
    run = analysis.status(task.account, task.user, FriendStore(accounts.account(task.account)))["run"] or {}
    if state in ("done", "partial"):
        task.finish(state)
    elif not task.cancelled.is_set():
        task.finish("failed", {"code": run.get("state") or "failed",
                               "message": run.get("error") or ("已中断，可继续" if run.get("state") == "interrupted"
                                                               else "分析未完成")})


def auto_update(plugin, analysis, accounts, account, user, start):
    """An analysed friend whose window opened, or whose human data changed: update when it is small.

    ``start(task, execution)`` runs the task this creates. Returns whether a task was created.
    """
    if not account or not user or user.endswith("@chatroom"):
        return False
    # An event sees one saved decision. Saving later does not interrupt a task
    # already accepted by this event, and manual scheme tasks bypass this gate.
    if not read_settings(plugin.core, {"autoUpdate": bool})["values"]["autoUpdate"]:
        return False
    try:
        database = accounts.account(account)
        database.forget_human(user)
        database.sync(user)
        if not analysis.maybe_auto(account, user, FriendStore(database)):
            return False
        created = plugin.core.post("tasks", {"kind": "scheme", "scheme": SCHEME, "account": account, "user": user,
                                             "title": "知己分析 · 自动更新",
                                             "params": {"mode": "update", "trigger": "auto"}})
    except (AdvisorError, CoreError):
        return False
    start(created["task"], created["execution"])
    return True
