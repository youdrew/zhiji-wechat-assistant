# 知己微信助手 · Zhiji WeChat Assistant 修改记录与来源说明

本文件记录 Zhiji WeChat Assistant 相对其公开基线的修改，保留来源与验证范围。

## 2026-10-09：独立项目文档

日期采用 Asia/Taipei 时区。

### 公开基线

- 上游项目：[tswawa/WechatVibe](https://github.com/tswawa/WechatVibe)。
- 本轮公开基线：提交 `b35efa5ec`。
- 产品名：知己簿 · Know Your Friends。
- 公开仓库：[youdrew/know-your-friends](https://github.com/youdrew/know-your-friends)。

公开仓库保留继承的源码历史和上游标签，以便核对来源。上游标签、版本号、历史发布说明与验证记录不等于 Know Your Friends 的产品发布或独立验收。

### 本轮变更

- 重写 README，增加中英文产品介绍，准确描述微信文本分析、人物与群聊画像、历史持久化与分层摘要记忆、聊天助手等继承功能。
- 明确源码开发版状态，说明界面名称、打包标识和更新入口尚保留上游设置。
- 补充完整的源码运行步骤，包括单独安装当前锁文件缺少的 PyYAML，以及助手引擎与界面构建步骤。
- 写明平台、媒体、模型接入和数据流向的限制；把多平台、可编辑事实记忆强化和媒体理解列为规划。
- 新增公开开发与 release PR 流程。
- 保留 Apache-2.0、第三方来源文件、Laya NOTICE、OpenCode MIT 许可，以及上游 README 中的具体贡献者署名。

本轮只修改 README 和这两份开发文档，**不修改功能源码、依赖锁文件或模型行为**。文档中的安装补充不代表根 Python 锁文件已经修复。

### 现有能力与后续工作

当前公开基线可以读取本机已登录微信中的可用文本，展示分析与画像，持久化保存按账号和会话隔离的可读历史，并在模型容量不足时生成分层摘要，供配置的生成式聊天助手复用。它没有因此新增其他聊天平台适配、媒体原件归档、语音转写、多模态理解或自动发送能力。

本轮未建立 Know Your Friends 独立安装包、更新服务或新的产品发布。新增功能及其测试范围将在之后的公开 PR 和修改记录中逐项说明。

### 许可与来源保留

项目代码采用 [Apache-2.0](../LICENSE)，Know Your Friends 新增代码沿用该许可。依赖、模型、内嵌源码和演示素材继续遵循各自许可，详见：

- [第三方来源与许可证](../THIRD_PARTY_NOTICES.md)
- [Laya LICENSE](../electron/laya/LICENSE) 与 [NOTICE](../electron/laya/NOTICE)
- [OpenCode MIT 许可证](../licenses/OpenCode-MIT.txt)
- [本机微信读取层来源](../native-reader/THIRD_PARTY_NOTICES.md)

继承文件中的 WechatVibe 名称、上游版权与贡献说明用于记录来源，不应改写成 Know Your Friends 原创声明。Know Your Friends 独立维护，不代表上游项目，也不承诺获得其认可或背书。


## 2026-10-22 产品命名

平台品牌确定为“知己”，客户端名称为“知己微信助手”，英文为 Zhiji WeChat Assistant。公开仓库地址更新为 https://github.com/youdrew/zhiji-wechat-assistant 。

本次公开改动只更新产品文档名称和仓库链接，不引入未公开的新功能，不改变当前源码开发版和上游溯源说明。上述早期记录保留当时名称与来源；原 Apache-2.0 许可、原作者及贡献者署名继续保留。
