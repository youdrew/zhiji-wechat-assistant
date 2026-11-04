"""Window modules an external Agent may ask Core to open on the user's desktop (B1).

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

外部 Agent 只能按"模块 id"请求打开一个现成的知己窗口，绝不能传 URL、路径或
任意页面。本注册表是唯一来源：只有 ``agentOpenable`` 的条目能被打开，其余
（例如会展示模型密钥的模型设置页）只登记不开放。

每个条目：

- ``surface``：``"friend"``（独立好友窗 chatui/friend.html）、``"main"``（主窗口外壳）或
  ``"chat"``（受限宿主里独立的只读聊天记录窗口）。
- ``tab``：好友窗里要选中的页签，取值与 kyf.friend-analysis 方案页签一致
  （overview / memories / persona / values / chat / trend；memories 是宿主页签）。
- ``context``：``"contact"`` 需要 ``accountId`` + ``subjectId``（单个微信联系人，
  群聊 ``…@chatroom`` 一律拒绝）；``"none"`` 不接受任何上下文。
- ``plugins``：窗口里会出现的插件页面；这些插件必须启用，受限会话也只放行它们。
- ``capability``：打开窗口只需 ``"read"``。
- ``openModule`` / ``runStatus``：主窗外壳的 ``--open-module`` 参数 / 好友窗聚焦运行状态行。

``resolve(module_id, context)`` 返回校验后的规格，或抛出 :class:`UiError`
（``module-unknown`` / ``module-not-openable`` / ``context-invalid``）。
"""
from __future__ import annotations

import re

FRIEND_ANALYSIS = "kyf.friend-analysis"
CONTACT_SUBJECT_TYPE = "wechat_contact"
MODULE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}(\.[a-z][a-z0-9-]{0,31}){1,3}")
MAX_ACCOUNT_ID = 128
MAX_SUBJECT_ID = 256


class UiError(Exception):
    """A request-level refusal; ``status`` is the HTTP-style hint for the MCP proxy."""

    STATUS = {"module-unknown": 404, "module-not-openable": 403, "context-invalid": 400,
              "account-not-authorized": 403, "contact-not-authorized": 403, "account-unknown": 404}

    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.status = self.STATUS.get(code, 400)


def _contact(tab, **extra):
    return {"surface": "friend", "tab": tab, "context": "contact", "plugins": (FRIEND_ANALYSIS,),
            "capability": "read", "agentOpenable": True, **extra}


MODULES = {
    "contact.overview": _contact("overview"),
    "contact.memory": _contact("memories"),
    "contact.persona": _contact("persona"),
    "contact.values": _contact("values"),
    "contact.chat": _contact("chat"),
    "contact.trends": _contact("trend"),
    # 打开和该联系人的只读聊天记录窗口，供用户查看；Agent 只收到窗口状态。
    "contact.conversation": {"surface": "chat", "tab": None, "context": "contact", "plugins": (),
                             "capability": "read", "agentOpenable": True},
    # 好友窗头部的分析运行状态行；停在概述页并让窗口突出运行状态。
    "analysis.progress": _contact("overview", runStatus=True),
    "app.main": {"surface": "main", "tab": None, "context": "none", "plugins": (),
                 "capability": "read", "agentOpenable": True},
    "app.settings": {"surface": "main", "tab": None, "context": "none", "plugins": (),
                     "capability": "read", "agentOpenable": True, "openModule": "settings"},
    # 模型设置页能显示/揭示模型密钥：登记但永不对 Agent 开放。
    "app.settings.models": {"surface": "main", "tab": "models", "context": "none", "plugins": (),
                            "capability": "read", "agentOpenable": False, "openModule": "settings"},
}


def module(module_id):
    """The registry entry for a well-formed id, or None (never a fallback)."""
    if not isinstance(module_id, str) or not MODULE_ID.fullmatch(module_id):
        return None
    return MODULES.get(module_id)


def openable_modules():
    """Ids an Agent may request, in registry order (for tool descriptions/enums)."""
    return [name for name, entry in MODULES.items() if entry["agentOpenable"]]


def _text(value, maximum):
    return isinstance(value, str) and 1 <= len(value) <= maximum and \
        not any(ord(char) < 32 or ord(char) == 127 for char in value) and value == value.strip()


def resolve(module_id, context=None):
    """Validated spec ``{module, surface, tab, context, plugins, capability, accountId?, subjectId?, ...}``."""
    entry = module(module_id)
    if entry is None:
        raise UiError("module-unknown", "未知的窗口模块")
    if not entry["agentOpenable"]:
        raise UiError("module-not-openable", "该窗口模块不对外部 Agent 开放")
    context = {} if context is None else context
    if not isinstance(context, dict):
        raise UiError("context-invalid", "窗口上下文不正确")
    account_id, subject_id = context.get("accountId"), context.get("subjectId")
    if set(context) - {"accountId", "subjectId"}:
        raise UiError("context-invalid", "窗口上下文含有不支持的字段")
    spec = {"module": module_id, **{key: value for key, value in entry.items() if key != "agentOpenable"},
            "plugins": list(entry["plugins"])}
    if entry["context"] == "contact":
        if not _text(account_id, MAX_ACCOUNT_ID) or not _text(subject_id, MAX_SUBJECT_ID):
            raise UiError("context-invalid", "该窗口需要账号与联系人")
        if subject_id.endswith("@chatroom"):
            raise UiError("context-invalid", "好友窗口不接受群聊")
        spec["accountId"], spec["subjectId"] = account_id, subject_id
    elif account_id is not None or subject_id is not None:
        raise UiError("context-invalid", "该窗口不接受账号或联系人")
    return spec
