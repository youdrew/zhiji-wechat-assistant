# 插件化重构代码审查记录

更新时间：2026-10-14。审查对象：dev 分支上的插件化重构及其后续提交。

| 范围 | 提交 |
| --- | --- |
| 聊天助手迁入 `kyf.advisor` 插件（P7） | `18a48a3`、`7efb7e8`，经 `ca1aaf0` 合入 dev |
| 合入后的功能提交 | `9ce9366` 会话列表宽度、`2a589b3` 插件版本 0.1.0、`9552f37` 加密表情与气泡颜色、`25476e0` 独立聊天记录窗口、`63bc8c6` 聊天布局与启动可靠性 |
| 合并提交本身 | `2bd3fba`、`59dc052`、`85c34be`、`ca1aaf0`：除 dev 自身两个提交外只有空白行整理，无夹带改动 |

审查分五块：Core（Python）、`kyf.advisor` 插件后端、宿主界面与 Electron 及打包、TypeScript（中继、引擎、网关）、合入后的功能提交。只记录读代码并经测试或复现确认的问题；每个修复都带有在旧代码上失败的回归测试。修复后 `npm run typecheck`、`test:node`（591 通过）、`test:scripts`（11 个）与 `scripts/run-python-tests.py`（76 个文件）全部通过。

## 已修复

| 严重性 | 位置 | 问题 | 处理 |
| --- | --- | --- | --- |
| 高 | `bridge/core_migration.py` | 迁移把旧 opencode 运行目录（实测 4198 个文件、约 247 MB，npm 缓存路径超过 260 字符）整体复制。未开启 Windows 长路径的电脑上每次失败，`kyf.advisor` 永远停在“资料迁移未完成”，且每 10 秒在请求线程中重新复制。复制过去的目录其实从未被引擎使用；若改为使用，旧会话绑定旧工作目录，会被判为 `scope-mismatch` 而失败 | 不再复制 opencode 目录，只迁移表和助手模板；旧会话按“原生会话丢失”由插件从已迁移的讨论记录重建 |
| 高 | `plugin-sdk/web/kyf-host.js` | 首次用“继续聊这句”打开聊天助手时，宿主在页面注册监听前送达引用，消息被丢弃，输入框为空（真实 Electron 中 5/5 复现） | 无监听时暂存最多 32 条消息，注册后按序送达 |
| 中 | `electron/model-gateway-queue.ts` | 排队上限 120 秒，而转发请求可等首字节 10 分钟；单并发或较慢的本地模型被后台分析占用时，聊天助手排队 2 分钟即报“排队超时” | 排队上限改为 10 分钟，与首字节期限一致 |
| 中 | `bridge/core_account_db.py`、`core_services.py`、`account_api.py`、`plugins/kyf.advisor/backend/store.py` | 清除账号时，旧账号目录与插件引擎目录中超过 260 字符的路径删除失败 | 使用 Windows 扩展长度路径删除 |
| 中 | `plugin-sdk/python/kyf_plugin/core.py` | Python 插件访问本机 Core 时沿用 Windows 系统代理；代理例外未含 `127.*` 时所有 Python 插件“无法连接 Core” | 本机请求不经代理 |
| 中 | `bridge/core_services.py` | 插件接口 `scope` 每次调用都做完整的微信账号检查；聊天助手每次请求（包括输入时保存草稿）都会调用 | 复用 2 秒内的账号核验，读取器更换后不复用 |
| 中 | `plugins/kyf.advisor/backend/accounts.py` | 等待同一好友另一个运行的首次历史复制时察觉不到停止；停止超过 10 秒会被 Core 强停整个插件进程 | 等待期间持续检查停止 |
| 中 | `plugins/kyf.advisor/backend/service.py` | Core 导入历史期间，每 50 毫秒轮询两次完整账号检查 | 轮询普通状态，每 2 秒核验一次账号 |
| 中 | `bridge/media_keys.py` | 无法推导图片／表情密钥时，每个首块不同的文件都重新读取一次微信进程内存（数百 MB） | 每 30 秒最多读取一次，期间复用候选密钥 |
| 中 | `chatui/history-panel.js` | 主窗口短暂重读时广播空账号，独立聊天记录窗口误判为换账号并隐藏记录 | 仅在收到另一个账号时挂起 |
| 中 | `plugins/kyf.friend-analysis/ui/page-core.js` | 知己分析 → 人格分析删除观察使用 `confirm()`，沙箱框架中被忽略，删除永远不生效（P6 遗留） | 页面内确认框，Esc、点背景或“取消”为否 |
| 低 | `plugins/kyf.advisor/backend/main.py` | RPC 未按原接口要求必填字段，例如缺 `requestId` 的 `run.start` 可重复开跑 | 恢复原接口的必填校验 |
| 低 | `plugins/kyf.advisor/plugin.json` | 助手仓库导航图标退化为通用四宫格 | 恢复原机器人图标 |
| 低 | `chatui/message-analysis.js`、`plugin-slots.js` | 未启用聊天面板插件时仍显示“继续聊这句”；导航页无需用户操作即可写剪贴板 | 无面板时隐藏；复制需刚有用户操作 |
| 低 | `chatui/model-usage.js` | 模型调用窗口在切换筛选后保留之前的模型选项 | 只显示匹配项 |

## 未修复，建议

| 严重性 | 位置 | 问题 | 建议 |
| --- | --- | --- | --- |
| 低 | `plugins/kyf.advisor` 压缩会话 | 停止压缩最长约 20 秒，超过 Core 的 10 秒宽限，可能导致整个插件进程被强停、其他进行中的回答一起中断 | 引擎侧把压缩停止限制在 10 秒内，或对该插件的交互任务放宽宽限 |
| 低 | `plugins/kyf.advisor` 压缩会话 | 停止压缩时即使摘要已生效也记为“已中断” | 停止后先核对原生摘要再定状态 |
| 低 | `plugins/kyf.advisor` 任务 | 任务名“保存回答”在恢复讨论和人物资料变化时也显示；失败任务结束时不带错误原因 | 按用途命名任务；`finish` 附上错误码与说明 |
| 低 | `plugins/kyf.advisor/ui` | “深度思考”开关与所选助手在重启后复位（沙箱页面无本地存储） | 由插件后端保存面板偏好 |
| 低 | `plugins/kyf.advisor/ui` | 输入后约半秒内退出会丢失最后几个字；面板加载中关闭时页面仍以为可见 | 退出前同步保存草稿；关闭时补发可见性事件 |
| 低 | `bridge/real_http.py` | 用量账本忙时界面收到通用 503，而不是 `usage-busy` | 透传错误码 |
| 低 | `bridge/core_migration.py` | 非系统错误时 `ensure()` 仍可能抛出 | 统一记为迁移失败、按 10 秒重试 |
| 低 | `bridge/media_database.py` | 语音数据库也做重解析点检查，微信目录或上级是联接点时语音无法播放 | 与表情、视频加固一致，暂按设计保留 |
| 低 | `chatui/history-panel.js` | 对已打开的聊天记录窗口再点“聊天记录”会回到第一页 | 微信只聚焦已有窗口，可不刷新 |

## 审查后确认无问题的部分

- 执行令牌中继：只接受本机、带密钥、非浏览器来源的请求；路径先规范化再校验前缀；密钥头被剥离，令牌放回原头；解绑时切断进行中的请求。
- 引擎：opencode 配置只有本机中继地址与随机密钥，不含 API key 和执行令牌；配置变化导致的重启已串行化。
- Core 任务生命周期与插件接口安全：任务停止、文件授权与领取、请求大小、插件页面 CSP；宿主隔离、IPC、导航限制与打包清单。
- 合入后的会话列表／输入框宽度记忆、独立聊天记录窗口的隔离与 IPC 校验、启动可靠性改动、插件版本改为 0.1.0（无按版本记录的状态）。

## 本机数据提示

本机 `.local/plugin-data/kyf.advisor/accounts/<账号>/engine/` 下有一份早先迁移复制的 opencode 目录（约 244 MB：cache、config、data、home、managed、state、tmp、workspace），引擎不会使用它（引擎状态在同目录下新建的 `engine/engine`）。确认聊天助手工作正常后可以手动删除这些子目录以释放空间；未自动删除。

## 仍需实测

- 真实旧账号的资料迁移（聊天助手会话重建、模板、知己分析结果）。
- 安装包内的插件进程、网关与 opencode 实际运行。
