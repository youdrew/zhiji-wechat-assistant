"""kyf.friend-analysis plugin entry: the default 知己分析 scheme.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Host calls: ``scheme.status`` / ``scheme.estimate`` for the window's run controls; the scheme
page calls ``page.*`` through the host bridge. Runs are Core scheme tasks; an analysed friend
is updated automatically when its window opens (``friend.opened``) if the update is small.
"""
import threading
import time

import friend_pages
import scheme
import status_view
from account import Accounts
from contracts import AdvisorError
from friend_analysis import FriendAnalysis
from analysis_store import FriendStore
from friend_trends import relationship_trends
from kyf_plugin import CoreError, Plugin, RpcError

OPEN = ("queued", "running")

plugin = Plugin()
accounts = Accounts(plugin)
analysis = FriendAnalysis(plugin, accounts)
estimates = {}


class PageService:
    """What the page builders expect: scope checks over the mirror and the analysis engine."""

    def __init__(self, engine):
        self.analysis = engine

    def _scope(self, account, user):
        database = accounts.account(account)
        context = database.sync(user)
        return account, user, (context or {}).get("source_id"), database, FriendStore(database)


pages = PageService(analysis)


def scoped(context):
    account, user = context.get("account"), context.get("user")
    if not isinstance(account, str) or not account or not isinstance(user, str) or not user:
        raise RpcError("invalid-request", "缺少好友")
    if user.endswith("@chatroom"):
        raise RpcError("invalid-request", "群聊暂不提供知己分析")
    return account, user


def call(action):
    try:
        return action()
    except AdvisorError as exc:
        raise RpcError(exc.code, exc.message) from None
    except CoreError as exc:
        raise RpcError(exc.code, exc.message) from None


def _status(account, user):
    database = accounts.account(account)
    store = FriendStore(database)
    history = plugin.core.history_status(account, user)
    status = analysis.status(account, user, store)
    run = status["run"]
    estimate = None
    if not (run and run["state"] in OPEN):
        human = database.human(user)["state"]
        key = (history.get("sourceFingerprint"), history.get("epoch"), history.get("readCount"),
               status["configFingerprint"], human.get("memory_version"), run and run["id"], run and run["state"])
        cached = estimates.get((account, user))
        if cached and cached[0] == key:
            estimate = cached[1]
        else:
            estimate = analysis.estimate(account, user, store, "update")
            # An unreachable model is often temporary; ask again on the next status rather than keep the error.
            if estimate.get("configured"):
                estimates[(account, user)] = (key, estimate)
    return {"account": account, "user": user, **status_view.view(status, estimate, history), "run": run}


@plugin.method("scheme.status")
def scheme_status(params, context):
    account, user = scoped(context)
    return call(lambda: _status(account, user))


@plugin.method("scheme.estimate")
def scheme_estimate(params, context):
    account, user = scoped(context)
    mode = params.get("mode") if params.get("mode") in ("update", "rebuild") else "update"
    estimate = call(lambda: analysis.estimate(account, user, FriendStore(accounts.account(account)), mode))
    return {"line": status_view.estimate_line(estimate), "estimate": estimate}


@plugin.method("page.overview")
def page_overview(params, context):
    account, user = scoped(context)
    return call(lambda: friend_pages.overview(pages, account, user))


@plugin.method("page.persona")
def page_persona(params, context):
    account, user = scoped(context)
    return call(lambda: friend_pages.persona(pages, account, user))


@plugin.method("page.chat")
def page_chat(params, context):
    account, user = scoped(context)
    return call(lambda: friend_pages.chat(pages, account, user))


@plugin.method("page.personaEvidence")
def page_persona_evidence(params, context):
    account, user = scoped(context)
    return call(lambda: friend_pages.persona_evidence(pages, account, user, list(params.get("ids") or [])[:41]))


@plugin.method("page.message")
def page_message(params, context):
    account, user = scoped(context)
    return call(lambda: friend_pages.message_context(pages, account, user, params.get("id")))


@plugin.method("page.round")
def page_round(params, context):
    account, user = scoped(context)
    return call(lambda: friend_pages.chat_round(pages, account, user, params.get("id")))


@plugin.method("page.trend")
def page_trend(params, context):
    account, user = scoped(context)

    def build():
        _account, _user, _fp, database, _store = pages._scope(account, user)
        state = database.context(user)
        rows = database._rows("SELECT seq,side,time_ms,kind,text,CASE WHEN json_valid(raw) THEN "
                              "json_extract(raw,'$.type') END AS type FROM context_messages WHERE user=? ORDER BY seq",
                              (user,))
        summary = {"state": state["state"] if state else "empty", "readCount": state["read_count"] if state else 0}
        result = relationship_trends(rows, context_state=summary, now_ms=int(time.time() * 1000))
        return {"account": account, "user": user, "dataRevision": state["revision"] if state else 0, **result}
    return call(build)


@plugin.task("scheme")
def run_scheme(task):
    scheme.run_scheme(analysis, accounts, task)
    estimates.pop((task.account, task.user), None)


def start_auto(task, execution):
    threading.Thread(target=plugin.run_task, args=(task, execution, run_scheme), daemon=True,
                     name="friend-analysis-auto").start()


@plugin.event("friend.opened")
def friend_opened(event):
    scheme.auto_update(plugin, analysis, accounts, event.get("account"), event.get("user"), start_auto)


@plugin.event("human.changed")
def human_changed(event):
    """A changed goal or memory makes dependent results stale; the same small-update rule applies."""
    account, user = event.get("account"), event.get("user")
    estimates.pop((account, user), None)
    # Invalidating read caches is independent of whether automatic inference is
    # enabled: pages and an immediate manual analysis must see the edited data.
    if account and user:
        accounts.account(account).forget_human(user)
    scheme.auto_update(plugin, analysis, accounts, account, user, start_auto)


plugin.on_shutdown.append(analysis.close)
plugin.on_shutdown.append(accounts.close)
plugin.run()
