# 来源、许可与致谢

[返回项目首页](../README.md)

本项目基于 [tswawa/WechatVibe](https://github.com/tswawa/WechatVibe)，保留其源码历史与第三方来源说明。知己微信助手独立维护，不代表 WechatVibe、腾讯、微信或其他上游项目，也没有这些项目的官方背书。

代码采用 [Apache-2.0](../LICENSE)；本项目新增代码同样采用该许可。第三方组件、模型与素材遵循各自的许可证：

- [第三方来源与许可证](../THIRD_PARTY_NOTICES.md)
- [Laya 历史来源与许可说明](../THIRD_PARTY_NOTICES.md)
- [OpenCode MIT 许可证](../licenses/OpenCode-MIT.txt)
- [llama.cpp MIT 许可证](../licenses/llama.cpp-MIT.txt)、[LLVM OpenMP 许可证与例外](../licenses/LLVM-OpenMP-Apache-2.0.txt)
- [Julia-1 模型 Apache-2.0 许可证](../licenses/Julia-1-Apache-2.0.txt)
- [本机微信读取层来源](../native-reader/THIRD_PARTY_NOTICES.md)

继承的第三方说明保留原项目名称与固定来源版本。演示头像采用 Lisa Wischofsky 的 [Adventurer](https://www.dicebear.com/styles/adventurer/) 插画，经 DiceBear 组合与配色，适用 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)，不属于代码的 Apache-2.0 许可。

## 致谢

感谢 WechatVibe 的作者、贡献者，以及 Laya、OpenCode、wechatauto-replica 等上游项目。

旧版 Laya/API 消息标签与画像流水线已经停用，保留的旧分析结果仅供查阅。这里继续保留 Laya 及相关上游贡献的历史署名；当前生成式分析通过用户配置的模型服务运行。

可选的本地判断使用 [Supersonic Labs 的 Julia-1](https://huggingface.co/SupersonicLabs/Julia-1) 与 [llama.cpp](https://github.com/ggml-org/llama.cpp/tree/b12032) CPU 运行时。Julia-1 权重按固定版本与 SHA-256 从 ModelScope 下载，不随源码提交；llama.cpp `b12032` 及其 LLVM OpenMP 依赖在构建机校验后随安装包提供，用户端无需下载运行时。判断模型的适用性仍需针对具体任务验证。

免费开源版保留微信只读数据源及其上游来源，不包含微信发送插件、发送 SDK 或原生发送执行器。各读取依赖的来源与许可继续见[本机微信读取层来源](../native-reader/THIRD_PARTY_NOTICES.md)及[第三方声明](../THIRD_PARTY_NOTICES.md)。

<details>
<summary>继承的上游贡献者署名</summary>

以下保留上游 README 的具体贡献说明。链接中的 Issue 和 PR 均属于 WechatVibe；其中“本版”指所引用的上游版本，不表示这些贡献者参与了知己微信助手的独立维护。

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
