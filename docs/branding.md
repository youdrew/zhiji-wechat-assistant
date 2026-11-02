# 品牌与兼容约定

更新日期：2026-11-02。

| 对象 | 当前名称与入口 |
| --- | --- |
| 本地客户端 | 知己微信助手 · Zhiji WeChat Assistant |
| Agent 社交平台 | 知己（规划中的独立平台） |
| 客户端公开仓库 | [youdrew/zhiji-wechat-assistant](https://github.com/youdrew/zhiji-wechat-assistant) |
| 问题反馈 | [Issues](https://github.com/youdrew/zhiji-wechat-assistant/issues) |
| 官方发布页 | [Releases](https://github.com/youdrew/zhiji-wechat-assistant/releases) |

客户端此前名为“知己簿 · Know Your Friends”。当前界面和使用文档采用新名称；已注明日期的修改记录、调研结论、上游来源、版权与贡献署名保留当时名称，便于核对历史。

首版公开交付为免费开源版，一台 Windows 电脑即可运行；微信发送、朋友圈抓取和社交媒体抓取归后续 Pro 外部插件规划，双机 Pro 尚未交付。两个版本均由用户自行配置模型服务和所需 API Key。

本次变更调整展示品牌与项目入口，继续沿用现有的应用 ID、数据目录和插件 ID。`KnowYourFriends.exe`、ZIP 与诊断文件名、更新清单的 `KnowYourFriends` 产品身份等内部兼容标识保持不变。现有本机源码工作目录无需因品牌更新而改名，用户数据不会因此迁移或清理。

**2.0.0 为首个免费开源安装版本**，下载以[官方发布页](https://github.com/youdrew/zhiji-wechat-assistant/releases)实际公布的附件、兼容说明与更新公告为准。Windows x64 安装包文件名为 `Zhiji-WeChat-Assistant-2.0.0-Setup-x64.exe`，自带运行环境，无需用户另装 Node.js、Python 或 .NET；不附带模型账号、API Key 或生成模型权重。

首版禁用应用内自动更新，后续版本请到官方发布页下载。旧版客户端用户同样应在发布页核对可用版本与操作说明。当前仍可从源码运行；品牌与链接更新本身不代表下载附件已经可用，也不表示“知己”平台已经上线。新的安装包展示名称不改变上述内部兼容身份。

知己微信助手独立维护，保留 WechatVibe、微信读取层、OpenCode、Laya 及其他第三方来源和许可，不代表腾讯、微信或上游项目，也没有这些项目的官方背书。
