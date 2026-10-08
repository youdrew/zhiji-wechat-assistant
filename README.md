# 知己簿 · Know Your Friends

**本地优先的好友理解与关系记忆助手**

**A local-first assistant for understanding friends and maintaining relationship memory.**

知己簿帮助你从聊天记录中整理交流线索：回看消息情绪与意图、查看人物和群聊画像，并结合当前会话生成交流建议与回复草稿。分析结果是模型对聊天文本的推测，应结合实际语境判断。

Know Your Friends helps you review emotions and interaction patterns in WeChat conversations, explore contact and group profiles, and draft replies with context. Its assessments are model-generated interpretations, not verified facts about a person's thoughts or personality.

本项目基于 [WechatVibe](https://github.com/tswawa/WechatVibe) 衍生开发，作为独立仓库维护。当前提供微信文本分析、会话记忆与聊天助手功能，帮助你回顾共同经历、整理交流线索并准备回复。

**当前状态：源码开发版。** 本仓库尚未发布 Know Your Friends 自有正式安装包。界面名称、打包标识和应用内更新地址仍保留上游设置，请使用下方源码步骤运行。独立发布机制完成前，不要使用应用内更新来更新 Know Your Friends。继承的上游 Git 标签用于代码溯源，不代表本项目的正式产品发布。

[当前功能](#当前功能) · [源码运行](#源码运行) · [使用边界](#使用边界) · [开发与贡献](#开发与贡献) · [来源与许可](#来源与许可)

## 当前功能

| 能力 | 当前实现 |
| --- | --- |
| 微信会话查看 | 只读读取本机已登录账号的单聊与群聊，按需选择会话；支持历史分页、关键词和日期查找、定位上下文。 |
| 消息分析 | 显示情绪和意图短标签，结合上下文提供交流线索；可开启或关闭。 |
| 人物与群聊画像 | 展示互动风格、常聊词、摘要，以及模型推测的好感度和 MBTI 倾向；群聊可切换整体与成员视图。 |
| 保存与续算 | 保存已分析结果和处理进度，按账号与模型来源隔离；在可用历史基础上继续处理新消息。 |
| 关系记忆 | 持久化保存可读历史文本及上下文，按需生成分段和分层摘要；资料按账号、会话隔离，供后续交流复用。 |
| 聊天助手 | 在当前会话中整理内容、讨论交流方式、拟写回复；保留助手对话与草稿，不自动发送微信消息。 |
| 助手与技能 | 可导入 GitHub 或本地的 Skill 文本资料，编辑提示词与助手配置；导入的脚本和外部工具不会执行。 |
| 模型选择 | 消息与画像分析可使用本地 Laya 或配置的 API；聊天助手使用支持工具调用的生成式模型。 |

好感度数值和 MBTI 倾向是继承自上游的分析展示，不是心理测评或对真实关系的客观定论。依据不足时，应保留不确定性。

### 关系记忆

当前已经具备基于聊天历史的关系记忆基础：

- **可读历史持久化**：聊天助手按需整理当前会话可读取的历史文本，保留发送者、时间和可用引用，在本地保存，供后续对话复用。
- **持续更新**：已整理的会话可以追加新消息；遇到历史缺口、内容变化或来源变化时，会重新核对或整理，不能保证所有情况都只做增量处理。
- **分层摘要**：模型上下文容量不足时，将较早的内容分段并生成分层摘要，保留覆盖范围；摘要用于模型输入，不替代已保存的聊天文本。
- **范围隔离**：资料按账号和会话隔离，同一会话中的不同助手共享托管资料。助手生成的建议与推测不写回公共事实摘要。

这部分是可复用的会话资料和摘要记忆。目前尚未提供完整的、可由用户逐条编辑与纠错的结构化事实记忆库，相关强化列在后续规划中。

### 模型接入

- **本地分析**：Laya 多语言 ONNX 模型在本机运行，可使用 CPU；适合消息与画像分析，不负责生成聊天助手回答。
- **生成式 API**：支持 Anthropic、OpenAI Responses、Chat Completions、Gemini 和 Ollama 兼容协议。用户主动填写服务地址、凭据、模型和上下文容量。
- **聊天助手**：需要模型支持工具调用，允许读取本轮提供的当前会话文本资料。协议兼容不代表所有服务商与模型都已经过实际验证。
- **连接限制**：当前代码仅允许回环地址使用 HTTP，其他模型地址要求 HTTPS。局域网模型服务可能需要 HTTPS 网关或本机代理，不能直接假定任意 HTTP 端点可用。

API 画像的完整判断至少需要 12,288 tokens 上下文。模型回答质量、速度与实际容量仍由所选服务决定。

## 源码运行

当前面向 **Windows 10/11 x64**，使用 **Windows 微信 4.x**。上游记录的实测版本为 **4.1.15.13**；不支持 3.x，也不承诺兼容所有后续微信版本。

开发环境需要 **Node.js 24.11.1、Python 3.14 和 npm**。在 PowerShell 中执行：

```powershell
git clone https://github.com/youdrew/know-your-friends.git
cd know-your-friends

py -3.14 -m venv .venv
$env:WECHATVIBE_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path

npm.cmd ci
& $env:WECHATVIBE_PYTHON -m pip install --no-deps -r python-requirements.lock.txt
& $env:WECHATVIBE_PYTHON -m pip install --no-deps PyYAML==6.0.3

& $env:WECHATVIBE_PYTHON scripts/setup-advisor-engine.py
npm.cmd run build:advisor-ui
npm.cmd start
```

- 当前 Python 锁文件缺少技能解析器需要的 PyYAML，因此上面单独补装固定版本；保留 `--no-deps`，不自动扩展上游可选依赖。
- `npm ci` 会安装 Electron 桌面运行时。若此前跳过安装脚本，先执行 `npm.cmd run setup:electron`。
- 助手安装脚本下载固定版本的 OpenCode 引擎并校验完整性，不使用全局 OpenCode 配置。
- 只用 API 时可以跳过 Laya 下载。需要本地分析时，运行 `npm.cmd run setup:models`，或在应用中下载/选择已有模型目录。源码模型文件约 681 MB，模型权重不包含在仓库中。
- `WECHATVIBE_PYTHON` 等名称是保留的上游兼容配置项；当前窗口中的设置不会修改系统级环境变量。

### 首次使用

1. 登录你有权读取的 Windows 微信账号。
2. 启动应用，在设置中选择本地模型，或主动配置并测试外部模型连接。
3. 在会话管理中添加要查看的联系人或群聊。
4. 在聊天页开启消息标签，或进入人物画像页面查看分析。
5. 打开聊天助手询问交流建议。检查生成的草稿后，自行决定是否使用。

更多继承功能的使用细节见[聊天助手说明](docs/advisor.md)。其中仍可能出现上游界面名称。

## 使用边界

- **读取范围**：读取本机微信已有且当前可访问的数据。界面轮询更新不等于手机端完整实时同步，也不能恢复本地不存在的聊天。
- **媒体**：当前聊天界面以 `[图片]` 等占位展示图片，语音和视频也作为非文本消息处理。没有完整的音视频归档、语音转写或多模态理解流程。
- **平台**：当前没有“她说”“橙”等其他聊天平台的适配器，也没有通用聊天文件导入或聊天记录导出入口。
- **自动化**：不自动回复、不操作微信发送、不执行导入技能中的脚本。窗口读取相关代码不代表已经实现 GUI 自动聊天。
- **上下文**：助手会整理可读历史，在容量不足时压缩模型输入。资料可用不保证模型每次实际读完全部内容，回答仍需核对。
- **发布**：上游安装包、版本号、截图和更新记录不构成本项目的独立发布或验收声明。

## 数据与隐私

“本地优先”指本地读取、保存和可选的本机推理，**不代表使用 API 时数据不会离开设备**。

- 本地 Laya 模式在本机完成消息与画像分析。
- 启用 API 分析时，所需聊天文本和画像参考会发送到你配置的模型服务。
- 提交聊天助手问题时，当前会话资料、必要摘要、选定技能与助手对话会按需发送到配置的生成式模型；即使消息分析使用本地 Laya，助手也可能使用 API。
- API 凭据由用户主动配置，当前 Windows 实现使用系统 DPAPI 加密保存。仓库不提供预置的个人账号、模型凭据或聊天数据。
- 清除应用内账号数据不会删除微信源数据库中的记录。

只处理你有权访问和分析的记录。提交 Issue、截图或日志时，先移除姓名、聊天正文、账号标识、密钥和其他私人信息。

## 规划

以下是待评估与实现的方向，不是当前功能或交付时间承诺：

- [ ] 完成独立应用标识、打包与发布渠道。
- [ ] 在现有会话记忆之上，增加可编辑的结构化事实与偏好记录，并关联可核对的来源。
- [ ] 强化事实记忆的纠错、矛盾处理及覆盖范围展示，区分已确认事实与待确认推测。
- [ ] 设计多平台数据接入与身份映射，再逐步增加适配器。
- [ ] 评估媒体保存、语音转写和图像/视频理解。
- [ ] 提升本地模型与自部署网关的接入体验和可验证性。

## 开发与贡献

公开 `main` 维护经过审核的代码与文档。开发在功能分支进行，通过 Pull Request 合入；实验性或非公开环境中的改动按成熟程度选择发布，不将个人数据和配置同步到公开仓库。

```powershell
npm.cmd test
```

该命令运行类型检查、Node、桌面脚本与 Python 回归。需要实际加载 Laya 的 `npm.cmd run test:model` 单独执行，要求模型已经下载。

- [开发与公开发布流程](docs/development-workflow.md)
- [修改记录与来源说明](docs/modifications.md)
- [后端结构说明](docs/backend-architecture.md)
- [问题反馈](https://github.com/youdrew/know-your-friends/issues)

## 来源与许可

本项目基于 [tswawa/WechatVibe](https://github.com/tswawa/WechatVibe)，保留其源码历史与第三方来源说明。Know Your Friends 独立维护，不代表 WechatVibe、腾讯、微信或其他上游项目，也没有这些项目的官方背书。

代码采用 [Apache-2.0](LICENSE)；本项目新增代码同样采用该许可。第三方组件、模型与素材遵循各自的许可证：

- [第三方来源与许可证](THIRD_PARTY_NOTICES.md)
- [Laya LICENSE](electron/laya/LICENSE) 与 [NOTICE](electron/laya/NOTICE)
- [OpenCode MIT 许可证](licenses/OpenCode-MIT.txt)
- [本机微信读取层来源](native-reader/THIRD_PARTY_NOTICES.md)

继承的第三方说明保留原项目名称与固定来源版本。演示头像采用 Lisa Wischofsky 的 [Adventurer](https://www.dicebear.com/styles/adventurer/) 插画，经 DiceBear 组合与配色，适用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)，不属于代码的 Apache-2.0 许可。

### 致谢

感谢 WechatVibe 的作者、贡献者，以及 Laya、OpenCode、wechatauto-replica 等上游项目。

<details>
<summary>继承的上游贡献者署名</summary>

以下保留上游 README 的具体贡献说明。链接中的 Issue 和 PR 均属于 WechatVibe；其中“本版”指所引用的上游版本，不表示这些贡献者参与了 Know Your Friends 的独立维护。

- 感谢 [china-luo](https://github.com/china-luo) 在 [Issue #1](https://github.com/tswawa/WechatVibe/issues/1) 中反馈 Windows 微信 4.1.15.13 的聊天记录读取异常，并提出对非字符串消息类型进行兼容转换的建议。
- 感谢 [Li Xiang](https://github.com/Misaka1008611) 反馈微信数据库密钥不完整、反复读取的问题，并提供排查信息，帮助定位读取兼容性问题。
- 感谢 [luo785859020（小闹一起）](https://github.com/luo785859020) 通过 [PR #3](https://github.com/tswawa/WechatVibe/pull/3) 和 [PR #4](https://github.com/tswawa/WechatVibe/pull/4) 改进源码模型下载、Python 环境选择与验证流程，并完成后端职责分层。
- 感谢 [morticuke](https://github.com/morticuke) 在 [Issue #5](https://github.com/tswawa/WechatVibe/issues/5) 中提供 Windows MIME 映射异常的排查过程和修复建议。
- 感谢 [Dl1447（Geekline）](https://github.com/Dl1447) 通过 [PR #12](https://github.com/tswawa/WechatVibe/pull/12) 修复选中首个会话后无法继续添加会话的问题。
- 感谢 [silicon-sbt](https://github.com/silicon-sbt) 通过 [PR #13](https://github.com/tswawa/WechatVibe/pull/13) 改进 API 消息标签的批量结果对应。
- 感谢 [silicon-sbt](https://github.com/silicon-sbt) 通过 [PR #10](https://github.com/tswawa/WechatVibe/pull/10)、[PR #18](https://github.com/tswawa/WechatVibe/pull/18)、[PR #19](https://github.com/tswawa/WechatVibe/pull/19)、[PR #20](https://github.com/tswawa/WechatVibe/pull/20) 和 [PR #21](https://github.com/tswawa/WechatVibe/pull/21) 加入本地多路并行分析、过滤服务号与系统会话、一键添加全部会话、只读的整账号分析进度与耗时，以及后台分析开关。
- 感谢 [Ch1cken-1145（Ch1cken_#）](https://github.com/Ch1cken-1145) 通过 [PR #16](https://github.com/tswawa/WechatVibe/pull/16) 加入手动设置微信聊天记录路径的功能。
- 感谢 [LianYu-Ya](https://github.com/LianYu-Ya) 在 [Issue #14](https://github.com/tswawa/WechatVibe/issues/14) 中反馈自部署模型批量标签格式错误、只返回一条的问题。
- 感谢 [nizitao](https://github.com/nizitao) 通过 [PR #26](https://github.com/tswawa/WechatVibe/pull/26) 贡献重复分词优化，并在 [PR #33](https://github.com/tswawa/WechatVibe/pull/33) 中继续整理改进建议。本版采用此前 #26 中的编码缓存、共享状态编码及等价文本处理，减少重复分词计算。
- 感谢 [wzzzzzzzh](https://github.com/wzzzzzzzh) 通过 [PR #27](https://github.com/tswawa/WechatVibe/pull/27)、[PR #28](https://github.com/tswawa/WechatVibe/pull/28)、[Issue #30](https://github.com/tswawa/WechatVibe/issues/30) 和 [PR #32](https://github.com/tswawa/WechatVibe/pull/32) 贡献换行整理、账号查询提速及读取诊断。本版以等价实现采用扫描预算调整方案，改善部分较大微信进程因旧预算不足而持续未就绪的问题。
- 感谢 [silicon-sbt](https://github.com/silicon-sbt) 通过 [PR #31](https://github.com/tswawa/WechatVibe/pull/31) 提供累计分析状态异常的定位、最小复现和回归测试，帮助修复情绪证据缺失导致的分析中断，并提供读取问题的独立核对与测试反馈。
- 感谢 [Berge520](https://github.com/Berge520) 通过 [Issue #24](https://github.com/tswawa/WechatVibe/issues/24) 反馈会话管理与分析恢复问题。

</details>

---

**文档修改日期：2026-10-09（Asia/Taipei）。** 本次调整项目品牌、功能说明、源码使用步骤和开发流程；基线与变更范围见[修改说明](docs/modifications.md)。
