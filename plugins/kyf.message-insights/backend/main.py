"""kyf.message-insights plugin entry: 分析与建议 for the main chat.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

Host slots:
- chat header toggle ``insights``: ``chatHeader.state`` / ``chatHeader.toggle``;
- message annotations: ``annotations.status`` / ``annotations.generate`` / ``annotations.retry`` / ``annotations.copy``.
New messages of enabled friends arrive as ``message.new`` events.
"""
import threading

from kyf_plugin import CoreError, Plugin, RpcError

from insights import Insights
from prompts import InsightError

plugin = Plugin()
state = {}
state_lock = threading.Lock()


def insights():
    with state_lock:
        if "service" not in state:
            state["service"] = Insights(plugin)
        return state["service"]


def scoped(context):
    account, user = context.get("account"), context.get("user")
    if not isinstance(account, str) or not account or not isinstance(user, str) or not user:
        raise RpcError("invalid-request", "缺少好友")
    return account, user


def call(action):
    try:
        return action()
    except InsightError as exc:
        raise RpcError(exc.code, exc.message) from None
    except CoreError as exc:
        raise RpcError(exc.code, exc.message) from None


@plugin.method("chatHeader.state")
def header_state(params, context):
    account, user = scoped(context)
    return call(lambda: insights().status(account, user, []))


@plugin.method("chatHeader.toggle")
def header_toggle(params, context):
    account, user = scoped(context)
    return call(lambda: insights().set(account, user, params.get("enabled")))


@plugin.method("annotations.status")
def annotations_status(params, context):
    account, user = scoped(context)
    return call(lambda: insights().status(account, user, params.get("messageIds", [])))


@plugin.method("annotations.retry")
def annotations_retry(params, context):
    account, user = scoped(context)
    return call(lambda: insights().retry(account, user, params.get("messageId"), params.get("part"),
                                         params.get("expectedVersion")))


@plugin.method("annotations.generate")
def annotations_generate(params, context):
    account, user = scoped(context)
    return call(lambda: insights().generate(account, user, params.get("messageId")))


@plugin.method("annotations.copy")
def annotations_copy(params, context):
    account, user = scoped(context)
    return call(lambda: insights().copy(account, user, params.get("messageId"), params.get("version"),
                                        params.get("index")))


@plugin.event("message.new")
def on_message(event):
    insights().on_message(event)


plugin.run()
