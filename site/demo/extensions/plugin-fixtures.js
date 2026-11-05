// Copyright 2026 Know Your Friends contributors. SPDX-License-Identifier: Apache-2.0
// Public v2.1.0 plugin definitions; in-page synthetic configuration only.
window.ZhijiDemoPlugins = [
  {
    "id": "kyf.advisor",
    "name": "聊天助手",
    "version": "0.2.0",
    "description": "依据当前好友的聊天资料进行独立讨论，管理助手模板、技能包和历史会话",
    "contributes": {
      "settings": {
        "schema": 1,
        "description": "控制后续讨论直接加入的好友资料；保存后从下一个问题生效，已有讨论记录保留。联网方式在模型设置中统一配置。",
        "groups": [
          {
            "id": "context",
            "title": "讨论资料",
            "fields": [
              {
                "key": "includeMemories",
                "title": "使用关系记忆",
                "type": "boolean",
                "default": true,
                "description": "加入已确认和待确认的关系记忆，并保留来源标记。关闭后，已有讨论中提及的内容仍会保留。"
              },
              {
                "key": "includeObservations",
                "title": "使用用户观察",
                "type": "boolean",
                "default": true,
                "description": "加入你填写的主观观察，始终标明它们是个人看法。"
              }
            ]
          }
        ],
        "links": [
          {
            "label": "管理助手模板与技能",
            "nav": "repository"
          }
        ]
      },
      "chatToolbar": [
        {
          "id": "advisor",
          "label": "聊天助手",
          "page": "ui/panel.html"
        }
      ],
      "nav": [
        {
          "id": "repository",
          "label": "助手仓库",
          "page": "ui/repository.html",
          "icon": "assistant"
        }
      ]
    },
    "source": "bundled",
    "enabled": true
  },
  {
    "id": "kyf.message-insights",
    "name": "分析与建议",
    "version": "0.2.0",
    "description": "为开启的好友的每条新消息生成解读和三条回复建议；结果默认收起，不会自动发送",
    "contributes": {
      "settings": {
        "schema": 1,
        "description": "仅对你在聊天中手动开启的好友生效，仍为每条消息生成三条可复制建议；不会自动发送。",
        "groups": [
          {
            "id": "replies",
            "title": "回复建议",
            "fields": [
              {
                "key": "replyStyle",
                "title": "默认回复语气",
                "type": "select",
                "default": "natural",
                "description": "后续排队的消息与重试采用这个偏好，已排队的任务和已有结果保留。始终尊重最近对话、表达习惯与对方边界。",
                "options": [
                  {
                    "value": "natural",
                    "label": "自然口语"
                  },
                  {
                    "value": "brief",
                    "label": "简短直接"
                  },
                  {
                    "value": "polite",
                    "label": "礼貌温和"
                  }
                ]
              }
            ]
          }
        ]
      },
      "chatHeader": [
        {
          "id": "insights",
          "label": "分析与建议",
          "kind": "toggle"
        }
      ],
      "messageAnnotations": true
    },
    "source": "bundled",
    "enabled": true
  },
  {
    "id": "kyf.friend-analysis",
    "name": "知己分析",
    "version": "0.2.0",
    "description": "按好友分析近期概述、关系记忆候选、性格与三观、聊天互动与关系趋势",
    "contributes": {
      "settings": {
        "schema": 1,
        "description": "设置知己分析的后续自动更新行为；保存不会立即发起分析。",
        "groups": [
          {
            "id": "automation",
            "title": "更新方式",
            "fields": [
              {
                "key": "autoUpdate",
                "title": "自动增量更新",
                "type": "boolean",
                "default": true,
                "description": "打开已经分析过的好友，或修改其关系记忆与资料时，符合小量更新条件才自动分析。关闭后仍可手动分析；正在运行的任务继续完成。"
              }
            ]
          }
        ]
      },
      "chatHeader": [
        {
          "id": "analysis",
          "label": "知己分析",
          "kind": "friend-window"
        }
      ],
      "schemes": [
        {
          "id": "default",
          "name": "默认方案",
          "summary": "近期概述、关系记忆候选、性格分析、三观分析、聊天分析与关系趋势；增量更新，已有结果保留",
          "page": "ui/index.html",
          "tabs": [
            {
              "id": "overview",
              "title": "好友概述"
            },
            {
              "host": "memories"
            },
            {
              "id": "persona",
              "title": "性格分析"
            },
            {
              "id": "values",
              "title": "三观分析"
            },
            {
              "id": "chat",
              "title": "聊天分析"
            },
            {
              "id": "trend",
              "title": "关系趋势"
            }
          ]
        }
      ]
    },
    "source": "bundled",
    "enabled": true
  },
  {
    "id": "kyf.self-analysis",
    "name": "自我剖析",
    "version": "0.9.0",
    "description": "统一同步本人聊天，增量更新资料、标签、性格倾向、性格时间线、三观、三观时间线和表达知识库；保留人工修正与原文依据",
    "contributes": {
      "settings": {
        "schema": 1,
        "groups": [
          {
            "id": "analysis",
            "title": "自我剖析",
            "fields": [
              {
                "key": "analysisDepth",
                "title": "分析深度",
                "type": "select",
                "default": "deep",
                "options": [
                  {
                    "value": "basic",
                    "label": "基础"
                  },
                  {
                    "value": "standard",
                    "label": "标准"
                  },
                  {
                    "value": "deep",
                    "label": "深度"
                  }
                ]
              },
              {
                "key": "analysisGrain",
                "title": "分析时间颗粒度",
                "type": "select",
                "default": "6",
                "options": [
                  {
                    "value": "3",
                    "label": "3 个月"
                  },
                  {
                    "value": "6",
                    "label": "6 个月"
                  },
                  {
                    "value": "12",
                    "label": "1 年"
                  },
                  {
                    "value": "24",
                    "label": "2 年"
                  },
                  {
                    "value": "60",
                    "label": "5 年"
                  }
                ]
              }
            ]
          },
          {
            "id": "trial",
            "title": "聊天试用",
            "fields": [
              {
                "key": "replyDepth",
                "title": "回答检索深度",
                "type": "select",
                "default": "auto",
                "options": [
                  {
                    "value": "auto",
                    "label": "自动（先判断再回）"
                  },
                  {
                    "value": "basic",
                    "label": "基础"
                  },
                  {
                    "value": "standard",
                    "label": "标准"
                  },
                  {
                    "value": "deep",
                    "label": "深度"
                  }
                ]
              }
            ]
          }
        ]
      },
      "nav": [
        {
          "id": "self-analysis",
          "label": "自我剖析",
          "page": "ui/index.html",
          "icon": "self"
        }
      ]
    },
    "source": "bundled",
    "enabled": true
  },
  {
    "id": "kyf.external-chat-import",
    "name": "外部聊天识别",
    "version": "0.1.0",
    "description": "将聊天截图和录屏画面转录为可核对的消息候选；由应用确认保存，不启动分析或发送消息",
    "contributes": {},
    "source": "bundled",
    "enabled": true
  },
  {
    "id": "kyf.mcp",
    "name": "MCP",
    "version": "0.1.0",
    "description": "配对本机 AI 客户端，管理 MCP 授权范围、调用记录与总开关；含安全与数据说明",
    "contributes": {
      "nav": [
        {
          "id": "mcp-admin",
          "label": "MCP 管理",
          "page": "ui/index.html",
          "icon": "generic"
        }
      ],
      "settings": {
        "schema": 1,
        "description": "授权连接、数据范围、账号与联系人只在 MCP 管理页更改；这里仅保存新建客户端的默认数据范围。",
        "groups": [
          {
            "id": "pairing",
            "title": "配对",
            "fields": [
              {
                "key": "defaultMode",
                "title": "新建客户端默认数据范围",
                "type": "select",
                "default": "accounts",
                "options": [
                  {
                    "value": "accounts",
                    "label": "按账号"
                  },
                  {
                    "value": "per_contact",
                    "label": "逐联系人"
                  },
                  {
                    "value": "full",
                    "label": "全部账号（旧）"
                  }
                ],
                "description": "仅作为配对时的预选项；已创建客户端的数据范围随时可在管理页修改。"
              }
            ]
          }
        ],
        "links": [
          {
            "label": "打开 MCP 管理",
            "nav": "mcp-admin"
          }
        ]
      }
    },
    "source": "bundled",
    "enabled": true
  }
];
