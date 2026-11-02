# 知己微信助手 · Zhiji WeChat Assistant

**看懂聊天，记住朋友，准备更合适的回复。**

知己微信助手是一款本地优先的 Windows 微信桌面助手。它帮你回看聊天、整理值得记住的事，并结合上下文提供交流建议。

本次发布的是**免费开源版**，一台 Windows 电脑即可运行；模型服务与所需 API Key 由用户自行提供。

[下载与安装](#下载与安装) · [第一次使用](#第一次使用) · [源码运行](#源码运行) · [问题反馈](https://github.com/youdrew/zhiji-wechat-assistant/issues)

## 下载与安装

适用于 **Windows 10/11 x64、Windows 微信 4.x**。

**安装包正在重新整理，暂时停止下载。** 新附件将通过[官方发布页](https://github.com/youdrew/zhiji-wechat-assistant/releases)提供；当前可以按下方步骤从源码运行。

下载安装：

1. 附件恢复后，从官方发布页下载 Windows x64 安装包。
2. 双击安装，完成后从桌面或开始菜单打开“知己微信助手”。
3. 按下面的步骤连接模型服务，开始使用。

安装包自带运行环境，无需另装 Node.js、Python 或 .NET。首版不使用应用内自动更新，后续版本到官方发布页下载。也可按下方步骤从源码运行。

AI 功能需要自行配置兼容的模型服务；软件不附带模型账号、API Key 或生成模型权重。

## 能帮你做什么

- **回看聊天**：查看单聊与群聊，按关键词和日期查找，浏览图片、语音与视频。
- **记住朋友**：整理偏好、边界和共同经历；记忆由你确认，可以编辑、删除并回查原文。
- **理解交流**：查看好友概述、聊天分析和关系趋势，结合依据了解互动方式。
- **准备回复**：向聊天助手提问，或按联系人开启“分析与建议”，获取解读和三条可复制回复。
- **整理其他聊天**：导入其他软件的单聊截图或录屏，经识别、核对后保存在本机。
- **了解自己**：整理本人资料、标签、经历时间线、性格与三观，建立可检索的表达知识库，并在聊天试用中核对效果。
- **连接本机 AI 客户端**：通过可撤销授权的 MCP 接口读取资料、运行分析和管理本地内容；模型设置、用量与调用记录也包含在免费版中。

AI 分析可能有误，请结合实际语境核对。回复建议可复制后自行使用；本版不操作微信发送。

## 第一次使用

1. 登录 Windows 微信，再打开知己微信助手。
2. 在“设置 → 模型设置”中填写模型服务地址、模型名称和所需凭据，测试连接后保存。也可连接你在本机或局域网运行的模型服务。
3. 在会话管理中添加想查看的好友或群聊。
4. 打开好友聊天，使用“知己分析”整理记忆，或向聊天助手提问。需要持续建议时，在好友姓名右侧开启“分析与建议”。

聊天助手需要模型支持工具调用；图片及录屏识别需要支持图片输入的模型。API 协议兼容不代表所有模型服务都已验证。

## 源码运行

需要 Node.js 24.11.1、Python 3.14 和 npm。在 PowerShell 中执行：

```powershell
git clone https://github.com/youdrew/zhiji-wechat-assistant.git
cd zhiji-wechat-assistant
py -3.14 -m venv .venv
$env:KYF_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
npm.cmd ci
& $env:KYF_PYTHON -m pip install --no-deps -r python-requirements.lock.txt
& $env:KYF_PYTHON -m pip install --no-deps PyYAML==6.0.3
& $env:KYF_PYTHON scripts/setup-advisor-engine.py
npm.cmd run build:advisor-ui
npm.cmd start
```

源码运行同样需要自己的模型服务。运行资料与开发文档留在本机；公开代码和安装包不包含项目 `docs/` 或生成的 benchmark 结果。打包还需按固定清单准备媒体与原生运行库，来源和许可见[第三方声明](THIRD_PARTY_NOTICES.md)。

## 数据会去哪里

- 聊天记录、关系记忆和个人设置保存在本机。
- 使用 API 模型时，所需聊天内容、摘要或图片会发往**你配置的模型服务**；本地优先不等于始终离线。
- 聊天助手默认不联网；主动启用搜索后，搜索关键词会发送到你配置的搜索源。

只处理你有权访问的记录。反馈问题前，请先去除聊天正文、账号标识和密钥。

## 联系与来源

- 作者博客：[EugenePage](https://eugenepage.com) · 邮箱：[youdrewxuan@icloud.com](mailto:youdrewxuan@icloud.com)
- 问题反馈：[GitHub Issues](https://github.com/youdrew/zhiji-wechat-assistant/issues)

本项目基于 [tswawa/WechatVibe](https://github.com/tswawa/WechatVibe) 独立开发，感谢原作者、贡献者及各上游项目。代码采用 [Apache-2.0](LICENSE)，第三方组件、模型和素材遵循各自许可；完整署名与许可入口见[第三方声明](THIRD_PARTY_NOTICES.md)。本项目独立维护，不代表腾讯、微信或上游项目，也没有其官方背书。
