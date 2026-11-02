"""MCP stdio adapter: the entry a local AI client launches to reach one Zhiji installation.

Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0

The client starts this script over stdio. The installation is identified only by
the explicit ``--root`` path (no registry or fixed-path probing); the adapter
reuses a healthy Core of that installation or starts the headless one through
the existing launcher, refusing the automatic path when account clearing has
latched ``no-auto-recovery`` and never clearing that latch itself. Concurrent
cold starts serialize on an installation lock file, then retry the reuse.
stdout carries only MCP messages (newline-delimited JSON); diagnostics go to
stderr and never contain chat text or credentials.

Credentials come from ``KYF_MCP_CLIENT_ID`` / ``KYF_MCP_CREDENTIAL`` environment
variables or a ``--config`` JSON file (``{"clientId": ..., "credential": ...}``),
never from argv, logs or diagnostics. Tool calls map onto the Core capability
proxy's ``{action, params, account?, subjects?}`` envelope at ``/api/mcp/rpc``.

Protocol: MCP 2025-06-18. A client-requested version is echoed when known,
otherwise the adapter answers with its own latest supported version.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

# The launcher ships as a hyphenated script; load it as a module from beside this file.
import importlib.util  # noqa: E402

_LAUNCHER_SPEC = importlib.util.spec_from_file_location("real_client_launcher",
                                                        SCRIPTS / "start-real-client.py")
launcher = importlib.util.module_from_spec(_LAUNCHER_SPEC)
sys.modules[_LAUNCHER_SPEC.name] = launcher
_LAUNCHER_SPEC.loader.exec_module(launcher)  # noqa: E402  (identity, health, headless launch)

PROTOCOL_VERSION = "2025-06-18"
KNOWN_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2026-07-28")
SERVER_NAME = "zhiji-mcp"
SERVER_VERSION = "phase3b"
KEEPALIVE_SECONDS = 240.0
TOOLCHANGE_POLL_SECONDS = 30.0
HANDSHAKE_TIMEOUT = 10.0
CALL_TIMEOUT = 60.0
# High-impact deletion drains running work before cleanup.
LONG_CALL_TIMEOUT = 300.0
LONG_CALL_TOOLS = frozenset(("zhiji_delete_account", "zhiji_clear_local_data"))
START_LOCK_TIMEOUT = 60.0

CLIENT_ID_ENV = "KYF_MCP_CLIENT_ID"
CREDENTIAL_ENV = "KYF_MCP_CREDENTIAL"

# Tools are exactly the registered capabilities; subjects are always WeChat
# contacts addressed by stable id. Names in parameters are never authorization
# input -- Core re-checks every call server-side.
TOOL_CATALOG = {
    "zhiji_ping": {
        "description": "连通性与本客户端权限摘要探针；不读取任何资料。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "zhiji_list_accounts": {
        "description": "列出本机已保存微信账号及其缓存/在线状态；逐联系人模式只列已授权账号。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "zhiji_search_messages": {
        "description": "在一个已保存账号的一个联系人投影中做有界文本搜索，返回来源与覆盖标记及分页游标。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "query": {"type": "string", "description": "搜索文本"},
                                       "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                       "cursor": {"type": "string", "description": "上一次返回的游标"}},
                        "required": ["accountId", "subjectId", "query"],
                        "additionalProperties": False},
    },
    "zhiji_get_message_context": {
        "description": "读取一条消息前后各五条的上下文（已保存账号的投影）。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "messageId": {"type": "string", "description": "消息标识"}},
                        "required": ["accountId", "subjectId", "messageId"],
                        "additionalProperties": False},
    },
    "zhiji_list_memories": {
        "description": "分页读取一个联系人的关系记忆（含证据引用与版本）。状态筛选：active=待确认+已确认、"
                       "pending=待确认、confirmed=已确认、user_observed=用户观察（用户亲自记录或修正）、"
                       "rejected=已忽略；每页最多 100 条，游标失效后需重新读取。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "status": {"type": "string", "enum": ["active", "pending", "confirmed",
                                                                             "user_observed", "rejected"],
                                                  "description": "状态筛选，默认 active"},
                                       "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                       "cursor": {"type": "string", "description": "上一次返回的游标"}},
                        "required": ["accountId", "subjectId"],
                        "additionalProperties": False},
    },
    "zhiji_get_memory_evidence": {
        "description": "读取一条记忆的证据：摘录、版本历史及每条证据前后各五条的聊天窗口；"
                       "证据状态按当前投影重新核对，离线时如实报告不可核验。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "memoryId": {"type": "string", "description": "记忆标识"},
                                       "ordinal": {"type": "integer", "minimum": 0,
                                                   "description": "只取指定编号的证据；缺省返回全部"}},
                        "required": ["accountId", "subjectId", "memoryId"],
                        "additionalProperties": False},
    },
    "zhiji_get_media": {
        "description": "读取一条媒体消息的受控内容。只返回有界内嵌静帧（image/sticker/video，"
                       "每帧不超过 2 MiB、最多 4 帧），不返回音视频原件，也不暴露本机磁盘路径；"
                       "微信离线或账号非当前会话时如实报告媒体缺失（available=false 及原因），"
                       "语音仅可能带缓存转写文本。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "messageId": {"type": "string", "description": "消息标识"},
                                       "frames": {"type": "integer", "minimum": 1, "maximum": 4,
                                                  "description": "动图/视频抽帧数，默认 1"}},
                        "required": ["accountId", "subjectId", "messageId"],
                        "additionalProperties": False},
    },
    # 阶段 3b：版本化人工资料写入。所有写操作必须携带 requestId（幂等）；同一
    # 请求标识重复调用返回原结果，不同参数被拒绝；版本冲突返回 version-conflict。
    "zhiji_create_memory": {
        "description": "为一位联系人新增一条人工记忆（创建即已确认）。text 不超过 300 字，"
                       "tags 为固定标签集合中的一到多个；可附 eventTime（YYYY、YYYY-MM 或 YYYY-MM-DD）。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "text": {"type": "string", "description": "记忆内容"},
                                       "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                                                "description": "固定标签集合中的标签"},
                                       "eventTime": {"type": ["string", "null"],
                                                     "description": "事件时间，可省略"}},
                        "required": ["accountId", "subjectId", "requestId", "text", "tags"],
                        "additionalProperties": False},
    },
    "zhiji_update_memory": {
        "description": "整体替换一条记忆的内容、标签与事件时间（先读取当前内容与版本；"
                       "eventTime 传 null 表示清除）。expectedVersion 与当前版本不符时返回 version-conflict，"
                       "不会写入。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "memoryId": {"type": "string", "description": "记忆标识"},
                                       "text": {"type": "string", "description": "新的记忆内容"},
                                       "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                                                "description": "新的标签集合"},
                                       "eventTime": {"type": ["string", "null"], "description": "新的事件时间"},
                                       "expectedVersion": {"type": "integer", "minimum": 0,
                                                           "description": "当前版本"}},
                        "required": ["accountId", "subjectId", "requestId", "memoryId", "text", "tags",
                                     "eventTime", "expectedVersion"],
                        "additionalProperties": False},
    },
    "zhiji_confirm_memory": {
        "description": "确认一位联系人的待确认候选记忆。只有待确认状态可确认；冲突候选需附 choice："
                       "keep=保留原记录、adopt=采用新说法、both=分别注明时期。返回新版本与状态。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "memoryId": {"type": "string", "description": "候选记忆标识"},
                                       "expectedVersion": {"type": "integer", "minimum": 0,
                                                           "description": "当前版本"},
                                       "choice": {"type": "string", "enum": ["keep", "adopt", "both"],
                                                  "description": "冲突候选的处理方式，普通候选可省略"}},
                        "required": ["accountId", "subjectId", "requestId", "memoryId", "expectedVersion"],
                        "additionalProperties": False},
    },
    "zhiji_delete_memory": {
        "description": "删除一条记忆及其证据。普通删除不触发全量清除确认；expectedVersion 不符时返回 "
                       "version-conflict。同版本号下记忆可能已因确认合并而消失，此时返回 merged 标记。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "memoryId": {"type": "string", "description": "记忆标识"},
                                       "expectedVersion": {"type": "integer", "minimum": 0,
                                                           "description": "当前版本"}},
                        "required": ["accountId", "subjectId", "requestId", "memoryId", "expectedVersion"],
                        "additionalProperties": False},
    },
    "zhiji_get_contact_goal": {
        "description": "读取为一位联系人设置的联系目标及其版本。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"}},
                        "required": ["accountId", "subjectId"],
                        "additionalProperties": False},
    },
    "zhiji_save_contact_goal": {
        "description": "整体替换一位联系人的联系目标（可为空字符串）。expectedVersion 不符时返回 "
                       "version-conflict。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "text": {"type": ["string", "null"], "description": "联系目标文本"},
                                       "expectedVersion": {"type": "integer", "minimum": 0,
                                                           "description": "当前版本"}},
                        "required": ["accountId", "subjectId", "requestId", "text", "expectedVersion"],
                        "additionalProperties": False},
    },
    "zhiji_get_self_profile": {
        "description": "读取账号级自身资料（自身背景与表达风格）。逐联系人授权模式不提供该账号级能力。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"}},
                        "required": ["accountId"],
                        "additionalProperties": False},
    },
    "zhiji_save_self_profile": {
        "description": "整体替换账号级自身资料（自身背景与表达风格都必填）。expectedVersion 不符时返回 "
                       "version-conflict。逐联系人授权模式不提供该账号级能力。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "background": {"type": ["string", "null"], "description": "自身背景"},
                                       "style": {"type": ["string", "null"], "description": "表达风格"},
                                       "expectedVersion": {"type": "integer", "minimum": 0,
                                                           "description": "当前版本"}},
                        "required": ["accountId", "requestId", "background", "style", "expectedVersion"],
                        "additionalProperties": False},
    },
    "zhiji_list_observations": {
        "description": "分页读取一位联系人的手工观察记录（一句话判断、依据备注与关联维度）及各自版本。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                       "cursor": {"type": "string", "description": "上一次返回的游标"}},
                        "required": ["accountId", "subjectId"],
                        "additionalProperties": False},
    },
    "zhiji_save_observation": {
        "description": "新增或更新一条手工观察记录。observationId 缺省为新增；提供时为更新，需附 "
                       "expectedVersion，不符时返回 version-conflict。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "observationId": {"type": "string",
                                                         "description": "观察记录标识，缺省为新增"},
                                       "judgement": {"type": "string", "description": "一句话判断"},
                                       "note": {"type": "string", "description": "依据备注，可省略"},
                                       "dimension": {"type": ["string", "null"],
                                                     "description": "关联维度，可省略"},
                                       "expectedVersion": {"type": "integer", "minimum": 0,
                                                           "description": "更新时的当前版本"}},
                        "required": ["accountId", "subjectId", "requestId", "judgement"],
                        "additionalProperties": False},
    },
    "zhiji_delete_observation": {
        "description": "删除一条手工观察记录。expectedVersion 不符时返回 version-conflict。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "observationId": {"type": "string", "description": "观察记录标识"},
                                       "expectedVersion": {"type": "integer", "minimum": 0,
                                                           "description": "当前版本"}},
                        "required": ["accountId", "subjectId", "requestId", "observationId",
                                     "expectedVersion"],
                        "additionalProperties": False},
    },
    # 阶段 3b 第二批：分析任务。开始/停止是写操作（requestId 幂等）；长任务返回
    # 任务 ID，用 zhiji_get_task 轮询状态；逐句分析与回复建议按消息读取已保存结果；
    # 自我剖析是账号级汇总，逐联系人授权模式不提供。
    "zhiji_start_friend_analysis": {
        "description": "为一位联系人开始知己好友分析（长任务）。返回任务 ID，用 zhiji_get_task 轮询；"
                       "已有分析运行时默认拒绝，conflict=queue 排队或 stop-other 停止原任务。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "scheme": {"type": "string",
                                                  "description": "分析方案，缺省为默认方案"},
                                       "conflict": {"type": "string", "enum": ["queue", "stop-other"],
                                                    "description": "冲突处理方式，缺省拒绝并提示"}},
                        "required": ["accountId", "subjectId", "requestId"],
                        "additionalProperties": False},
    },
    "zhiji_get_friend_analysis": {
        "description": "读取一位联系人已保存的好友分析结果。section 可选：status=运行状态、"
                       "overview=好友概述、persona=性格分析、chat=聊天分析、trend=关系趋势。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "section": {"type": "string",
                                                   "enum": ["status", "overview", "persona", "chat",
                                                            "trend"],
                                                   "description": "结果分节，缺省 status"}},
                        "required": ["accountId", "subjectId"],
                        "additionalProperties": False},
    },
    "zhiji_get_task": {
        "description": "查询本客户端发起的一个分析任务的状态与进度（含排队、运行、终态）。",
        "inputSchema": {"type": "object",
                        "properties": {"taskId": {"type": "string", "description": "任务标识"}},
                        "required": ["taskId"],
                        "additionalProperties": False},
    },
    "zhiji_stop_task": {
        "description": "停止本客户端发起的一个分析任务；停止后迟到的结果不会再写回。",
        "inputSchema": {"type": "object",
                        "properties": {"taskId": {"type": "string", "description": "任务标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"}},
                        "required": ["taskId", "requestId"],
                        "additionalProperties": False},
    },
    "zhiji_start_message_insights": {
        "description": "为一位联系人的一条消息发起逐句解读与三条回复建议（长任务）。"
                       "返回该消息的排队/运行状态；需要该好友已在知己中开启分析与建议。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "messageId": {"type": "string", "description": "消息标识"}},
                        "required": ["accountId", "subjectId", "requestId", "messageId"],
                        "additionalProperties": False},
    },
    "zhiji_get_message_insights": {
        "description": "读取一位联系人若干消息的已保存逐句解读与回复建议（一次最多 20 条）。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "messageIds": {"type": "array", "items": {"type": "string"},
                                                      "minItems": 1, "maxItems": 20,
                                                      "description": "消息标识列表"}},
                        "required": ["accountId", "subjectId", "messageIds"],
                        "additionalProperties": False},
    },
    "zhiji_start_self_analysis": {
        "description": "开始账号级自我剖析（长任务）。返回运行状态；用 zhiji_get_self_analysis 轮询。"
                       "逐联系人授权模式不提供该账号级能力。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "depth": {"type": "string", "enum": ["basic", "standard", "deep"],
                                                 "description": "分析深度，缺省沿用现有设置"},
                                       "outputs": {"type": "array",
                                                   "items": {"type": "string",
                                                             "enum": ["profile", "tags", "timeline",
                                                                      "persona", "periods", "values",
                                                                      "library"]},
                                                   "description": "要更新的分析项目，缺省沿用现有设置"}},
                        "required": ["accountId", "requestId"],
                        "additionalProperties": False},
    },
    "zhiji_get_self_analysis": {
        "description": "读取自我剖析的运行状态或已保存结果。section 可选：status=运行状态、"
                       "persona=性格倾向、values=三观、tags=本人标签、tagEvidence=一个标签的依据（需 name）、"
                       "periods=分时期性格、periodDetail=某时期某一项的依据（需 period 和 key）、"
                       "timeline=时间线（事件分页，nextOffset 非空时用 offset 续读；version 用作修改的 "
                       "expectedVersion）、timelineEvidence=一个事件的依据和前后对话（需 id）。"
                       "逐联系人授权模式不提供该账号级能力。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "section": {"type": "string",
                                                   "enum": ["status", "persona", "values", "tags",
                                                            "tagEvidence", "periods", "periodDetail",
                                                            "timeline", "timelineEvidence"],
                                                   "description": "结果分节，缺省 status"},
                                       "offset": {"type": "integer", "minimum": 0,
                                                  "description": "timeline 的续读位置"},
                                       "id": {"type": "string", "description": "timelineEvidence 的事件 id"},
                                       "name": {"type": "string", "description": "tagEvidence 的标签名"},
                                       "period": {"type": "string",
                                                  "description": "periodDetail 的时期，如 2024H1"},
                                       "key": {"type": "string", "description": "periodDetail 的维度"}},
                        "required": ["accountId"],
                        "additionalProperties": False},
    },
    "zhiji_edit_self_timeline": {
        "description": "修改自我剖析时间线，一次一项，和界面上的修改等价，以这里写入的内容为准。"
                       "target=event 时 action 为 add、edit、confirm、unconfirm、delete、restore；"
                       "target=period 时为 add、edit、confirm、unconfirm、delete；"
                       "target=track 时为 add、edit、visible、delete，id 是主线名。"
                       "除 add 外都要 id；edit 只需给要改的字段，其余保留。"
                       "返回新的 version，继续修改时作为 expectedVersion；add 返回新条目的 id。"
                       "expectedVersion 不符时返回 version-conflict。可用 zhiji_undo_self_timeline 撤销。"
                       "逐联系人授权模式不提供该账号级能力。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "target": {"type": "string", "enum": ["event", "period", "track"],
                                                  "description": "修改对象：事件、时期或主线"},
                                       "action": {"type": "string",
                                                  "enum": ["add", "edit", "confirm", "unconfirm", "delete",
                                                           "restore", "visible"],
                                                  "description": "操作"},
                                       "id": {"type": "string", "description": "事件或时期 id，或主线名"},
                                       "fields": {
                                           "type": "object",
                                           "description": "event：title、track、status（occurred/planned/"
                                                          "cancelled/uncertain）、importance（important/"
                                                          "general）、when、phrase、place、people。"
                                                          "period：title、track、when、ongoing。"
                                                          "track：title（add 时的主线名）、description、"
                                                          "color、visible。when 可写 2024、2024.05、"
                                                          "2024-05-03、2024春或 2024.03-2024.06，空字符串"
                                                          "清空时间。"},
                                       "expectedVersion": {"type": "integer", "minimum": 0,
                                                           "description": "当前版本"}},
                        "required": ["accountId", "requestId", "target", "action", "expectedVersion"],
                        "additionalProperties": False},
    },
    "zhiji_undo_self_timeline": {
        "description": "撤销时间线最近一次修改，和界面上按 Ctrl+Z 共用一份记录，可连续撤销；"
                       "undone 是被撤销操作的说明，没有可撤销的修改时为 null。"
                       "expectedVersion 不符时返回 version-conflict。逐联系人授权模式不提供该账号级能力。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "expectedVersion": {"type": "integer", "minimum": 0,
                                                           "description": "当前版本"}},
                        "required": ["accountId", "requestId", "expectedVersion"],
                        "additionalProperties": False},
    },
    "zhiji_stop_self_analysis": {
        "description": "暂停正在进行的自我剖析；已保存结果保留。逐联系人授权模式不提供。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"}},
                        "required": ["accountId", "requestId"],
                        "additionalProperties": False},
    },
    # 阶段 3b 第三批：聊天助手。模板与技能管理（读+改）；讨论、草稿与排队消息按
    # 联系人主体读取；提交提问逐个核验 @ 的每位好友参与者（逐联系人模式任一未授
    # 权即整体拒绝）；模型调用留在 Core 网关，助手不代写草稿或排队消息。
    "zhiji_get_assistant_catalog": {
        "description": "读取聊天助手的模板与技能目录、引擎可用状态。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "zhiji_save_assistant_template": {
        "description": "新增或更新一个助手模板。agent 含 name、description、prompt、skillIds，"
                       "更新时另附 id；welcome 可省略（缺省沿用默认欢迎语）。",
        "inputSchema": {"type": "object",
                        "properties": {"requestId": {"type": "string", "description": "幂等请求标识"},
                                       "agent": {"type": "object", "additionalProperties": False,
                                                 "properties": {"id": {"type": "string"},
                                                                "name": {"type": "string"},
                                                                "description": {"type": "string"},
                                                                "prompt": {"type": "string"},
                                                                "skillIds": {"type": "array",
                                                                             "items": {"type": "string"}},
                                                                "welcome": {"type": ["string", "null"]}}}},
                        "required": ["requestId", "agent"],
                        "additionalProperties": False},
    },
    "zhiji_enable_assistant_template": {
        "description": "启用或停用一个助手模板。",
        "inputSchema": {"type": "object",
                        "properties": {"requestId": {"type": "string", "description": "幂等请求标识"},
                                       "id": {"type": "string", "description": "助手模板标识"},
                                       "enabled": {"type": "boolean"}},
                        "required": ["requestId", "id", "enabled"],
                        "additionalProperties": False},
    },
    "zhiji_delete_assistant_template": {
        "description": "删除一个助手模板；其历史讨论仍可在其他助手历史中只读查看。",
        "inputSchema": {"type": "object",
                        "properties": {"requestId": {"type": "string", "description": "幂等请求标识"},
                                       "id": {"type": "string", "description": "助手模板标识"}},
                        "required": ["requestId", "id"],
                        "additionalProperties": False},
    },
    "zhiji_import_assistant_skill": {
        "description": "向技能库导入一个技能（名称、描述与正文）。",
        "inputSchema": {"type": "object",
                        "properties": {"requestId": {"type": "string", "description": "幂等请求标识"},
                                       "name": {"type": "string"},
                                       "description": {"type": "string"},
                                       "content": {"type": "string"}},
                        "required": ["requestId", "name", "description", "content"],
                        "additionalProperties": False},
    },
    "zhiji_delete_assistant_skill": {
        "description": "从技能库删除一个技能。",
        "inputSchema": {"type": "object",
                        "properties": {"requestId": {"type": "string", "description": "幂等请求标识"},
                                       "id": {"type": "string", "description": "技能标识"}},
                        "required": ["requestId", "id"],
                        "additionalProperties": False},
    },
    "zhiji_list_assistant_friends": {
        "description": "列出一位联系人的讨论里可以 @ 的好友（已添加的一对一会话）。"
                       "逐联系人授权模式只列出已授权联系人。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"}},
                        "required": ["accountId", "subjectId"],
                        "additionalProperties": False},
    },
    "zhiji_list_assistant_threads": {
        "description": "列出一位联系人的助手讨论（含标题、状态与当前选择）。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "agentId": {"type": "string", "description": "助手模板标识，可省略"}},
                        "required": ["accountId", "subjectId"],
                        "additionalProperties": False},
    },
    "zhiji_get_assistant_thread": {
        "description": "读取一段助手讨论：消息分页、该讨论的草稿与排队消息、运行状态。"
                       "需要显式 threadId（读取不新建讨论）。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "agentId": {"type": "string", "description": "助手模板标识"},
                                       "threadId": {"type": "string", "description": "讨论标识"},
                                       "beforeSeq": {"type": "integer", "minimum": 0,
                                                     "description": "读取该序号之前的消息"}},
                        "required": ["accountId", "subjectId", "agentId", "threadId"],
                        "additionalProperties": False},
    },
    "zhiji_new_assistant_thread": {
        "description": "为一位联系人和一个助手模板新建一段讨论。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "agentId": {"type": "string", "description": "助手模板标识"}},
                        "required": ["accountId", "subjectId", "requestId", "agentId"],
                        "additionalProperties": False},
    },
    "zhiji_submit_assistant_question": {
        "description": "在一段讨论中提交提问（长任务）。返回运行标识，用 zhiji_get_assistant_run 轮询；"
                       "mentions 为 @ 的好友标识列表，逐联系人授权模式要求每位参与者均已授权。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "agentId": {"type": "string", "description": "助手模板标识"},
                                       "threadId": {"type": "string", "description": "讨论标识"},
                                       "message": {"type": "string", "description": "提问内容"},
                                       "thinking": {"type": "boolean",
                                                    "description": "深度思考开关，缺省开启"},
                                       "mentions": {"type": "array", "items": {"type": "string"},
                                                    "description": "@ 的好友标识列表"}},
                        "required": ["accountId", "subjectId", "requestId", "agentId", "threadId",
                                     "message"],
                        "additionalProperties": False},
    },
    "zhiji_get_assistant_run": {
        "description": "读取一次提问的事件流与运行状态；after 为已读到的最后一个事件序号。"
                       "结束后返回该讨论的完整消息页。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "runId": {"type": "string", "description": "运行标识"},
                                       "after": {"type": "integer", "minimum": 0,
                                                 "description": "已读事件序号"}},
                        "required": ["accountId", "subjectId", "runId"],
                        "additionalProperties": False},
    },
    "zhiji_stop_assistant_run": {
        "description": "停止一段讨论中正在进行的提问；已生成片段保留。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "subjectId": {"type": "string", "description": "稳定联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "runId": {"type": "string", "description": "运行标识"}},
                        "required": ["accountId", "subjectId", "requestId", "runId"],
                        "additionalProperties": False},
    },
    # 阶段 3b 第三批：外部聊天导入。外部联系人按 external_contact 主体类型独立
    # 授权（friendId 为素材库稳定 ID，与显示名无关）；没有文件输入工具，只管理
    # 已有素材；转录与核对记录是 Core 自有资料。
    "zhiji_list_import_contacts": {
        "description": "列出外部聊天导入素材库中的联系人（含是否已绑定微信）。"
                       "逐联系人授权模式只列出已授权的外部联系人。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "zhiji_list_import_batches": {
        "description": "列出一位外部联系人的导入批次（状态、进度与计数摘要）。",
        "inputSchema": {"type": "object",
                        "properties": {"friendId": {"type": "string",
                                                    "description": "外部联系人标识"}},
                        "required": ["friendId"],
                        "additionalProperties": False},
    },
    "zhiji_get_import_batch": {
        "description": "读取一个导入批次的详情：素材清单、识别进度与计数。",
        "inputSchema": {"type": "object",
                        "properties": {"friendId": {"type": "string",
                                                    "description": "外部联系人标识"},
                                       "batchId": {"type": "string", "description": "导入批次标识"}},
                        "required": ["friendId", "batchId"],
                        "additionalProperties": False},
    },
    "zhiji_list_import_candidates": {
        "description": "分页读取一个导入批次的转录候选与核对记录（文本、发言人、问题标记、复核状态）。",
        "inputSchema": {"type": "object",
                        "properties": {"friendId": {"type": "string",
                                                    "description": "外部联系人标识"},
                                       "batchId": {"type": "string", "description": "导入批次标识"},
                                       "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                       "cursor": {"type": "string",
                                                  "description": "上一次返回的游标"}},
                        "required": ["friendId", "batchId"],
                        "additionalProperties": False},
    },
    "zhiji_get_import_messages": {
        "description": "分页读取一位外部联系人已保存的导入结果（含来源、覆盖与告警）。",
        "inputSchema": {"type": "object",
                        "properties": {"friendId": {"type": "string",
                                                    "description": "外部联系人标识"},
                                       "platform": {"type": "string",
                                                    "description": "来源平台，可省略"},
                                       "batchId": {"type": "string",
                                                   "description": "限定导入批次，可省略"},
                                       "query": {"type": "string", "description": "文本筛选"},
                                       "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                                       "cursor": {"type": "string",
                                                  "description": "上一次返回的游标"}},
                        "required": ["friendId"],
                        "additionalProperties": False},
    },
    "zhiji_start_import_recognition": {
        "description": "开始一个导入批次的识别转录（长任务）。reprocess=true 时丢弃已有识别重来。",
        "inputSchema": {"type": "object",
                        "properties": {"friendId": {"type": "string",
                                                    "description": "外部联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "batchId": {"type": "string", "description": "导入批次标识"},
                                       "reprocess": {"type": "boolean",
                                                     "description": "重新识别，缺省 false"}},
                        "required": ["friendId", "requestId", "batchId"],
                        "additionalProperties": False},
    },
    "zhiji_stop_import_recognition": {
        "description": "停止一个导入批次的识别转录；已完成画面保留。",
        "inputSchema": {"type": "object",
                        "properties": {"friendId": {"type": "string",
                                                    "description": "外部联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "batchId": {"type": "string", "description": "导入批次标识"}},
                        "required": ["friendId", "requestId", "batchId"],
                        "additionalProperties": False},
    },
    "zhiji_save_import_batch": {
        "description": "确认保存一个导入批次中核对完成的记录；candidateIds 缺省为全部可保存候选。",
        "inputSchema": {"type": "object",
                        "properties": {"friendId": {"type": "string",
                                                    "description": "外部联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "batchId": {"type": "string", "description": "导入批次标识"},
                                       "candidateIds": {"type": "array", "items": {"type": "string"},
                                                        "description": "要保存的候选记录标识"}},
                        "required": ["friendId", "requestId", "batchId"],
                        "additionalProperties": False},
    },
    "zhiji_remove_import_material": {
        "description": "从一个导入批次移除一份已有素材；已用于识别的素材会被拒绝。",
        "inputSchema": {"type": "object",
                        "properties": {"friendId": {"type": "string",
                                                    "description": "外部联系人标识"},
                                       "requestId": {"type": "string", "description": "幂等请求标识"},
                                       "batchId": {"type": "string", "description": "导入批次标识"},
                                       "materialId": {"type": "string", "description": "素材标识"}},
                        "required": ["friendId", "requestId", "batchId", "materialId"],
                        "additionalProperties": False},
    },
    # 阶段 5：应用管理。完整模式开放；逐联系人模式需“全局管理”开关。模型密钥只
    # 写不读；删除整个账号/清空本机数据先返回待确认意图，确认只能在本机确认界面
    # 完成，之后携带 confirmationId 重新调用（一次性、短时）。
    "zhiji_get_model_settings": {
        "description": "查询核心模型设置：调度与联网（并发、SearXNG 等）及当前模型连接；"
                       "密钥只返回“是否已配置”，永远不含明文。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "zhiji_save_model_settings": {
        "description": "修改核心模型设置。scheduling 为完整调度/联网设置（revision 乐观锁）；"
                       "connection 为模型连接（protocol/baseUrl/model/contextTokens/timeoutMs，"
                       "apiKey 只接受设置或替换、绝不回显）。两者至少提供一个。",
        "inputSchema": {"type": "object",
                        "properties": {"requestId": {"type": "string", "description": "幂等请求标识"},
                                       "scheduling": {"type": "object",
                                                      "description": "完整调度设置 {revision, values}"},
                                       "connection": {"type": "object",
                                                      "description": "模型连接字段；apiKey 可省略以复用已存密钥"}},
                        "required": ["requestId"],
                        "additionalProperties": False},
    },
    "zhiji_get_plugins": {
        "description": "查询插件列表与启停状态（有界视图，不含插件设置内容）。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "zhiji_set_plugin_enabled": {
        "description": "启用或停用一个插件；MCP 自身（kyf.mcp）不能由本工具停用。",
        "inputSchema": {"type": "object",
                        "properties": {"requestId": {"type": "string", "description": "幂等请求标识"},
                                       "pluginId": {"type": "string", "description": "插件标识"},
                                       "enabled": {"type": "boolean", "description": "启用为 true"}},
                        "required": ["requestId", "pluginId", "enabled"],
                        "additionalProperties": False},
    },
    "zhiji_get_usage": {
        "description": "查询一个已保存账号的模型网关用量（可按 plugin/model/task/时间筛选）。"
                       "逐联系人模式只返回能从已授权联系人重算的子集并标注 scope。",
        "inputSchema": {"type": "object",
                        "properties": {"accountId": {"type": "string", "description": "已保存账号标识"},
                                       "plugin": {"type": "string", "description": "按插件筛选"},
                                       "model": {"type": "string", "description": "按模型筛选"},
                                       "task": {"type": "string", "description": "按任务筛选"},
                                       "since": {"type": "integer", "description": "起始时间（毫秒）"},
                                       "until": {"type": "integer", "description": "截止时间（毫秒）"},
                                       "includeContacts": {"type": "boolean",
                                                           "description": "完整模式下附逐联系人用量"}},
                        "required": ["accountId"],
                        "additionalProperties": False},
    },
    "zhiji_delete_account": {
        "description": "删除一个已保存账号的全部本机资料（高影响、不可恢复）。第一次调用（不带 "
                       "confirmationId）登记待确认意图并在本机确认界面请求确认；用户确认后携带返回的 "
                       "confirmationId 重新调用才会执行。确认一次性、短时，绑定本次参数与授权版本。",
        "inputSchema": {"type": "object",
                        "properties": {"requestId": {"type": "string", "description": "幂等请求标识"},
                                       "accountId": {"type": "string", "description": "已保存账号标识"},
                                       "confirmationId": {"type": "string",
                                                          "description": "本机确认界面确认后回传的确认标识"}},
                        "required": ["requestId", "accountId"],
                        "additionalProperties": False},
    },
    "zhiji_clear_local_data": {
        "description": "清空本机所有已保存账号的资料、分析与用量数据（高影响、不可恢复）。流程同"
                       "删除账号：先不带 confirmationId 发起，在本机确认界面确认后携带 confirmationId 重新调用。",
        "inputSchema": {"type": "object",
                        "properties": {"requestId": {"type": "string", "description": "幂等请求标识"},
                                       "confirmationId": {"type": "string",
                                                          "description": "本机确认界面确认后回传的确认标识"}},
                        "required": ["requestId"],
                        "additionalProperties": False},
    },
}
SUBJECT_TOOLS = {"zhiji_search_messages", "zhiji_get_message_context", "zhiji_list_memories",
                 "zhiji_get_memory_evidence", "zhiji_get_media", "zhiji_create_memory",
                 "zhiji_update_memory", "zhiji_confirm_memory", "zhiji_delete_memory",
                 "zhiji_get_contact_goal", "zhiji_save_contact_goal", "zhiji_list_observations",
                 "zhiji_save_observation", "zhiji_delete_observation",
                 "zhiji_start_friend_analysis", "zhiji_get_friend_analysis",
                 "zhiji_start_message_insights", "zhiji_get_message_insights",
                 "zhiji_list_assistant_friends", "zhiji_list_assistant_threads",
                 "zhiji_get_assistant_thread", "zhiji_new_assistant_thread",
                 "zhiji_submit_assistant_question", "zhiji_get_assistant_run",
                 "zhiji_stop_assistant_run"}
# 外部聊天导入工具按 external_contact 主体寻址：friendId 是素材库里的稳定好友
# ID，授权键与显示名无关；素材库不属于任何已保存微信账号，用固定伪账号区分。
EXTERNAL_SUBJECT_TOOLS = {"zhiji_list_import_batches", "zhiji_get_import_batch",
                          "zhiji_list_import_candidates", "zhiji_get_import_messages",
                          "zhiji_start_import_recognition", "zhiji_stop_import_recognition",
                          "zhiji_save_import_batch", "zhiji_remove_import_material"}
EXTERNAL_ACCOUNT = "external-chat"
# requestId 属于调用信封（幂等），不属于业务参数；适配器负责把它移到信封顶层。
ENVELOPE_KEYS = ("accountId", "subjectId", "requestId")


class AdapterError(Exception):
    """A startup or call failure reported to the AI client as a stable JSON-RPC error."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def diagnostic(message):
    """Diagnostics carry states and codes only -- never chat text or credentials."""
    sys.stderr.write(f"[zhiji-mcp] {message}\n")
    sys.stderr.flush()


# -- installation lock --------------------------------------------------------

def _lock_region(handle):
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_region(handle):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def install_start_lock(runtime_dir, timeout=START_LOCK_TIMEOUT):
    """Serialize concurrent cold starts of one installation, then retry the reuse."""
    runtime_dir = Path(runtime_dir)

    class _Context:
        def __enter__(self):
            runtime_dir.mkdir(parents=True, exist_ok=True)
            self.handle = (runtime_dir / "mcp-start.lock").open("a+")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    _lock_region(self.handle)
                    return self
                except OSError:
                    if time.monotonic() >= deadline:
                        self.handle.close()
                        raise AdapterError("start-lock-timeout",
                                           "另一个启动过程长时间占用本安装的启动锁") from None
                    time.sleep(0.2)

        def __exit__(self, *_exc):
            try:
                _unlock_region(self.handle)
            finally:
                self.handle.close()
            return False

    return _Context()


# -- Core connection ----------------------------------------------------------

class CoreConnection:
    """One installation's loopback Core: reuse or headless start, then authenticated calls."""

    def __init__(self, root, client_id, credential, *,
                 ensure=None, health=None):
        self.root = Path(root)
        self.client_id = client_id
        self.credential = credential
        self.token = None
        self.auth_version = None
        self.profile = None
        self.url = None
        self._ensure = ensure  # test seam
        self._health = health
        self._http_lock = threading.Lock()

    # -- resolution -----------------------------------------------------------
    def config(self, port):
        portable = self.root / "runtime" / "python" / "python.exe"
        python_exe = portable if portable.is_file() else Path(sys.executable)
        return launcher.Config(self.root, python_exe, port)

    def resolve(self):
        """Health-check the installation's Core; cold-start it under the install lock if needed."""
        base = self.config(launcher.default_port(self.root))
        port = launcher.selected_port(base, allow_fallback=True)
        config = self.config(port)
        if self._health is not None:
            state = self._health(config)
        else:
            state = launcher.health(config)
        if state != "ready":
            try:
                runtime_dir = launcher.checked_runtime_dir(config)
            except launcher.LauncherError as exc:
                raise AdapterError("core-start-refused", str(exc)) from None
            with install_start_lock(runtime_dir):
                # Another starter may have won the race while we waited.
                if self._health is not None:
                    state = self._health(config)
                else:
                    state = launcher.health(config)
                if state != "ready":
                    try:
                        if self._ensure is not None:
                            self._ensure(config)
                        else:
                            # 自动 MCP 启动路径：账号清理后的闩锁生效，且不清除闩锁。
                            launcher.ensure_service(config, recovery=True)
                    except launcher.LauncherError as exc:
                        raise AdapterError("core-start-refused", str(exc)) from None
        if self._health is not None:
            state = self._health(config)
        else:
            state = launcher.health(config)
        if state != "ready":
            raise AdapterError("core-unavailable",
                               f"本安装的本地服务暂不可用（{state}）；请显式启动一次知己后再试")
        diagnostic(f"使用本机服务 {config.url}（{state}）")
        return config.url

    # -- HTTP -----------------------------------------------------------------
    def _post(self, url, path, body, token=None, timeout=CALL_TIMEOUT):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        req = urlrequest.Request(url + path, data=payload, headers=headers, method="POST")
        with urlrequest.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def handshake(self, url):
        result = self._post(url, "/api/mcp/handshake",
                            {"clientId": self.client_id, "credential": self.credential,
                             "pid": os.getpid()}, timeout=HANDSHAKE_TIMEOUT)
        self.token = result["token"]
        self.auth_version = result["authVersion"]
        return result

    def connect(self):
        url = self.resolve()
        try:
            self.handshake(url)
        except HTTPError as exc:
            detail = _http_error(exc)
            raise AdapterError(detail[0], f"配对失败：{detail[1]}") from None
        except (URLError, socket.timeout, OSError) as exc:
            raise AdapterError("core-unavailable", f"本地服务无法连接：{exc}") from None
        self.url = url
        return url

    def ensure_connected(self):
        """A connection usable for calls; a lost token or a restarted Core reconnects."""
        with self._http_lock:
            if self.url is None or self.token is None:
                self.connect()
            return self.url

    def call(self, action, params, account=None, subjects=None, request_id=None, timeout=CALL_TIMEOUT):
        url = self.ensure_connected()
        body = {"action": action, "params": params}
        if account is not None:
            body["account"] = account
        if subjects is not None:
            body["subjects"] = subjects
        if request_id is not None:
            body["requestId"] = request_id
        for attempt in (1, 2):
            try:
                result = self._post(url, "/api/mcp/rpc", body, token=self.token, timeout=timeout)
                return result.get("result", result)
            except HTTPError as exc:
                if exc.code == 401 and attempt == 1:
                    # 短时令牌过期：重新配对一次后重试同一调用。
                    diagnostic("连接令牌已过期，正在重新配对")
                    try:
                        self.handshake(url)
                    except HTTPError as reconnect:
                        raise AdapterError(*_http_error(reconnect)) from None
                    continue
                code, message = _http_error(exc)
                raise AdapterError(code, message) from None
            except (URLError, socket.timeout, OSError) as exc:
                if attempt == 1:
                    # The Core may have exited since; resolve and pair again once.
                    diagnostic(f"本地服务暂时不可用（{exc}），正在重新连接")
                    self.url = None
                    url = self.ensure_connected()
                    continue
                raise AdapterError("core-unavailable", f"本地服务无法连接：{exc}") from None

    def probe(self):
        """The profile's open capabilities and own-permission summary, via the real proxy."""
        return self.call("zhiji_ping", {})


def _http_error(exc):
    try:
        payload = json.loads(exc.read().decode("utf-8"))
        return payload.get("error", "core-error"), payload.get("message", "本地服务返回错误")
    except Exception:
        return "core-error", f"本地服务返回 HTTP {exc.code}"


# -- envelope mapping ---------------------------------------------------------

def envelope(action, arguments):
    """Map flat tool arguments onto the capability proxy's envelope.

    ``requestId`` moves to the envelope top level (write idempotency), never into
    the business params; account and subject flatten into Core's subjects shape.
    External-import tools address an external contact by its stable friendId.
    """
    arguments = dict(arguments or {})
    unknown = set(arguments) - set(TOOL_CATALOG.get(action, {}).get("inputSchema", {})
                                    .get("properties", {}))
    if unknown:
        raise AdapterError("invalid-params", f"工具不接受参数：{', '.join(sorted(unknown))}")
    request_id = arguments.pop("requestId", None)
    params = {key: value for key, value in arguments.items()
              if key not in ("accountId", "subjectId", "friendId")}
    account = arguments.get("accountId")
    subjects = None
    if action in SUBJECT_TOOLS:
        if not account or not arguments.get("subjectId"):
            raise AdapterError("invalid-params", "该工具需要 accountId 与 subjectId")
        subjects = [{"accountId": account, "subjectType": "wechat_contact",
                     "subjectId": arguments["subjectId"]}]
    elif action in EXTERNAL_SUBJECT_TOOLS:
        friend_id = arguments.get("friendId")
        if not friend_id:
            raise AdapterError("invalid-params", "该工具需要 friendId")
        account = EXTERNAL_ACCOUNT
        subjects = [{"accountId": EXTERNAL_ACCOUNT, "subjectType": "external_contact",
                     "subjectId": friend_id}]
    return action, params, account, subjects, request_id


# -- stdio transport ----------------------------------------------------------

class StdioAdapter:
    def __init__(self, connection, *, stdin=None, stdout=None, protocol_version=PROTOCOL_VERSION):
        self.connection = connection
        self.stdin = stdin if stdin is not None else sys.stdin
        self.stdout = stdout if stdout is not None else sys.stdout
        # MCP stdio is UTF-8 regardless of the console codepage.
        for stream in (self.stdin, self.stdout):
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                try:
                    reconfigure(encoding="utf-8")
                except (OSError, ValueError):
                    pass
        self.protocol_version = protocol_version
        self._write_lock = threading.Lock()
        self._capabilities = None
        self._poll_stop = threading.Event()
        self._poll_thread = None

    # -- wire -----------------------------------------------------------------
    def send(self, message):
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            self.stdout.write(line + "\n")
            self.stdout.flush()

    def reply(self, request_id, result):
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def error(self, request_id, code, message):
        self.send({"jsonrpc": "2.0", "id": request_id,
                   "error": {"code": code, "message": message}})

    # -- capability state -----------------------------------------------------
    def refresh_capabilities(self):
        self._capabilities = self.connection.probe()

    def open_capabilities(self):
        server = (self._capabilities or {}).get("server") or {}
        return set(server.get("openCapabilities") or [])

    def tool_list(self):
        return [{"name": name, "description": catalog["description"],
                 "inputSchema": catalog["inputSchema"]}
                for name, catalog in TOOL_CATALOG.items() if name in self.open_capabilities()]

    def start_polling(self):
        """Keepalive pings slide the token TTL and the lease's idle clock; capability
        or permission changes emit notifications/tools/list_changed once."""
        def poll():
            last = (frozenset(self.open_capabilities()), self._capabilities or {})
            while not self._poll_stop.wait(TOOLCHANGE_POLL_SECONDS):
                try:
                    # The probe doubles as the keepalive: it slides the token TTL
                    # and the connection lease's idle clock at the Core.
                    self.refresh_capabilities()
                    current = (frozenset(self.open_capabilities()), self._capabilities or {})
                    if current != last:
                        last = current
                        self.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                except AdapterError as exc:
                    diagnostic(f"后台轮询失败：{exc.code}")
                except Exception as exc:  # never kill the adapter from the poll thread
                    diagnostic(f"后台轮询失败：{type(exc).__name__}")
        self._poll_thread = threading.Thread(target=poll, name="zhiji-mcp-poll", daemon=True)
        self._poll_thread.start()

    def stop_polling(self):
        self._poll_stop.set()

    # -- dispatch -------------------------------------------------------------
    def handle_message(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            return  # client notifications need no reply
        try:
            if method == "initialize":
                requested = message.get("params", {}).get("protocolVersion")
                version = requested if requested in KNOWN_PROTOCOL_VERSIONS else self.protocol_version
                self.reply(request_id, {"protocolVersion": version,
                                        "capabilities": {"tools": {"listChanged": True}},
                                        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}})
            elif method == "tools/list":
                self.reply(request_id, {"tools": self.tool_list()})
            elif method == "tools/call":
                self.reply(request_id, self.tool_call(message.get("params") or {}))
            elif method == "ping":
                self.reply(request_id, {})
            else:
                self.error(request_id, -32601, f"未知方法：{method}")
        except AdapterError as exc:
            self.error(request_id, -32000, f"{exc.code}: {exc.message}")
        except Exception:
            diagnostic("处理请求时发生内部错误")
            self.error(request_id, -32603, "适配器内部错误")

    def tool_call(self, params):
        name = params.get("name")
        if name not in TOOL_CATALOG:
            raise AdapterError("unknown-tool", f"未知工具：{name}")
        if name not in self.open_capabilities():
            raise AdapterError("capability-not-open", "该能力未对当前客户端开放")
        action, call_params, account, subjects, request_id = envelope(name, params.get("arguments"))
        result = self.connection.call(action, call_params, account=account, subjects=subjects,
                                      request_id=request_id,
                                      timeout=LONG_CALL_TIMEOUT if name in LONG_CALL_TOOLS else CALL_TIMEOUT)
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                "structuredContent": result}

    # -- loop -----------------------------------------------------------------
    def serve(self):
        while True:
            line = self.stdin.readline()
            if not line:
                return
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.error(None, -32700, "无法解析的消息")
                continue
            self.handle_message(message)


# -- entry --------------------------------------------------------------------

def load_credentials(config_path):
    """Config file first, then environment; values never reach argv, logs or diagnostics."""
    if config_path is not None:
        try:
            document = json.loads(Path(config_path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AdapterError("invalid-config", f"凭据配置文件不可用：{exc}") from None
        client_id = document.get("clientId") if isinstance(document, dict) else None
        credential = document.get("credential") if isinstance(document, dict) else None
        if not isinstance(client_id, str) or not client_id or not isinstance(credential, str) or not credential:
            raise AdapterError("invalid-config", "配置文件需要 clientId 与 credential")
        return client_id, credential
    client_id, credential = os.environ.get(CLIENT_ID_ENV), os.environ.get(CREDENTIAL_ENV)
    if not client_id or not credential:
        raise AdapterError("missing-credentials",
                           f"需要 --config 配置文件或 {CLIENT_ID_ENV}/{CREDENTIAL_ENV} 环境变量")
    return client_id, credential


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    root = None
    config_path = None
    index = 0
    while index < len(argv):
        option = argv[index]
        if option == "--root" and index + 1 < len(argv):
            root = Path(argv[index + 1])
            index += 2
        elif option == "--config" and index + 1 < len(argv):
            config_path = argv[index + 1]
            index += 2
        else:
            diagnostic(f"未知参数被忽略：{option}")
            index += 1
    try:
        client_id, credential = load_credentials(config_path)
        if root is None:
            # 默认为本适配器所在的安装；显式 --root 永远优先。
            root = SCRIPTS.parent
        connection = CoreConnection(root, client_id, credential)
        connection.ensure_connected()
        adapter = StdioAdapter(connection)
        try:
            adapter.refresh_capabilities()
        except AdapterError as exc:
            diagnostic(f"能力目录读取失败：{exc.code}")
        adapter.start_polling()
        diagnostic(f"就绪：{len(adapter.tool_list())} 个工具，协议 {adapter.protocol_version}")
        adapter.serve()
        return 0
    except AdapterError as exc:
        diagnostic(f"启动失败：{exc.code}: {exc.message}")
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
