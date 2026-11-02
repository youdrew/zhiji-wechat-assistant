# 聊天助手会话管理验证记录

2026-10-10，需求版本 v0.3。以下检查只使用临时数据库、合成联系人、合成对话及本机模拟服务；未调用真实聊天数据或用户的模型网关。

## 验收对应

| 需求 | 主要验证位置 | 已验证行为 |
| --- | --- | --- |
| CTX-01～03、UI-01～02 | `tests/advisor-ui.test.cjs`、`bridge/test_advisor_sessions_service.py`、`tests/advisor-runtime.test.ts` | 无可靠读数隐藏数字；单次输入含缓存；统一窗口；模型变化后旧读数失效；常驻压缩入口与三行详情 |
| CTX-02、CTX-04 | `scripts/advisor-native-smoke.ts` | 固定 OpenCode 1.18.34 对五种协议的非零缓存、多步最后一次输入；十轮有效资料去重与预算；真实原生自动压缩 |
| CMP-01～06 | Runtime、会话服务、UI 测试及原生 smoke | 手动压缩不续写；回答与压缩互斥；请求去重；停止/超时先核对；失败保留原记录；过大输入有界退出 |
| SES-01～04 | `bridge/test_advisor_sessions_service.py`、`tests/advisor-ui.test.cjs` | 新建与切换不停止旧任务；按 thread 隔离选择、草稿、滚动、事件；旧 `/reset` 为新建别名 |
| SES-05～08 | `bridge/test_advisor_sessions_store.py`、会话服务与 UI 测试 | v7 升级备份、草稿迁移、失效助手历史、删除与任务互斥；删除失败可重试，包含历代原生会话，保留微信和好友资料 |
| REC-01～02、REC-05～06 | `bridge/test_advisor_recovery.py`、会话服务测试、原生 smoke | 缺失原生状态恢复；小窗口分块与归因；失败保留原问题；检查点复用；已完成回答不重复；缺失旧问答停止恢复 |
| REC-03～04 | 会话服务、原有好友服务、边界与共享资料测试 | 记忆/目标换代排除旧建议与旧摘要；迟到结果拒绝；普通微信追加不强制更换原生会话 |
| SCP-01、AUTO-01 | 原有账号清理/边界/自动建议测试、新会话服务测试 | 账号与联系人归属、迟到事件、自动三候选和手动讨论隔离；联系人级忙碌覆盖压缩待核对状态 |
| CMD-01 | `tests/advisor-ui.test.cjs` | 完整命令匹配、中文输入法、引用与普通斜杠文本、按钮同一操作 |

## 本机界面验证

`tests/advisor-session-frame-smoke.cjs` 使用隐藏 Electron 窗口与合成快照：440px 浅色/深色、历史分组、压缩浮层、实际改名及删除确认、340×400 只读布局。检查无横向溢出、输入与操作按钮在可视区域内、无渲染错误。截图保存在被忽略的 `.local/tmp/advisor-session-ui-smoke/`。

## 可复现命令

在项目目录的 PowerShell 中先运行 `. .\.local\setup\Enter-Dev.ps1`，再运行：

```powershell
npm run typecheck
npm run test:node
npm run test:scripts
npm run test:python
npm run build:advisor-ui
node --import tsx scripts/advisor-native-smoke.ts
.\node_modules\electron\dist\electron.exe tests/advisor-session-frame-smoke.cjs
```

原生 smoke 使用项目已安装的固定引擎和本机合成 HTTP 服务；缺少该引擎时须先完成项目的引擎安装步骤。

## 本次执行结果

- TypeScript 类型检查、助手 UI 构建通过。
- Node 完整测试 687/687；Node 脚本测试 11 组通过。
- Python 完整测试 65 组、875/875 通过。
- 固定原生引擎 smoke 通过，包含 HTTP 等待者已断开、原生压缩仍运行时的独立停止、重复停止、原讨论保留和继续回答。
- 新增恢复测试 8/8；会话服务边界测试 25/25。包括来源换代、资料变化后拒绝旧压缩、多窗口草稿冲突及操作重试。
- 隐藏 Electron 界面验证通过，0 渲染错误；`git diff --check` 通过。

## 保留与限制

- 实施基于私有 `dev` 的既有提交，未提交、推送或重启正在运行的用户应用；两份原需求文档与实施前备份的 SHA-256 一致。
- 升级前数据库自动备份不会随单会话删除消失；当前工作区备份位于 `.local/backups/advisor-sessions-20261010-0625/`。
- 合成服务验证协议和状态机，不能证明真实网关的 token 统计准确度、摘要保真度或实际延迟。摘要仍可能损失细节。
- 无法核验原生摘要的业务覆盖范围时，仅供回看；恢复改用可验证的本地讨论记录。无法恢复的记录缺口会明确阻止继续回答。
- 应用主进程重启后加载新的 Python 服务与桌面桥接代码；刷新面板本身不足以加载全部修改。
