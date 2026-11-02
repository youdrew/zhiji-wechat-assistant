# 多模型设置与判断模型：需求与实施方案

创建：2026-11-01。状态更新：2026-11-02，**阶段 1 已复核修复；阶段 2 判断接口、阶段 3 本地下载运行和阶段 4 影子评估已实现并通过相关回归，尚未提交。真实窗口质量、完整安装包和实际跳过仍待验收**。本文供实现者直接使用。

阶段 1 已包含多模型存储与迁移、用途路由、每模型并发、设置面板及图片用途；SDK 的 `service_for()` 已随之提供。本次接手补齐 `judge()` 两路协议、ModelScope 下载与恢复、本地进程管理和提取完成后的影子补判，验证了真实 Julia 运行与打包依赖闭包；未将这些验证写成筛选质量或完整安装版验收。2026-11-02 复核已撤回首轮“中文词表损坏”等结论：Julia-1 可运行，保留为原定候选；当前不自动分配本地判断用途，也不启用窗口跳过。见[阶段 0 复核记录](judge-model-benchmark-2026-11-01.md)与[需求待办清单](requirements-todo.md#model)。

实施前 Core 只保存一个大模型，所有任务都走它。本需求的三项目标及当前进展如下：

1. 用户可以保存多个模型，并给“主模型”“判断模型”“图片模型”三种用途各选一个，由 Core 路由。
2. 增加判断能力：插件用 `judge()` 问封闭问题，得到选择、打分或是/否概率，不生成文本。
3. 用户可以一键下载本地判断模型，电脑性能够用时直接在本机运行；保留原定 Julia-1 候选，下载、运行闭环与筛选质量分别验收。

同时把模型设置面板改得更简洁，保持微信式极简：不加多余元素，不做仪表盘。

文中“建议”表示未经实测的初始值，可以按实测调整；其余为已定方案，改动前先问用户。

## 1. 已定决策

| 事项 | 决定 |
| --- | --- |
| 设置形态 | “模型列表 + 用途分配”。保存模型和分配用途分开 |
| 用途 | 主模型、判断模型、图片模型三种，后两种默认跟随主模型 |
| 路由方式 | 静态对应：每个步骤属于哪种用途写在代码里。不让模型动态挑模型 |
| 故障切换 | 不做。调用失败就报错，不自动换模型 |
| 并发 | 从全局设置移到每个模型 |
| 判断接口 | 新增 `judge()`，不走 `generate()`，因为决策模型不生成文本 |
| 默认本地模型 | 保留原定 Julia-1 Q8（168 MB）候选，2026-11-02 已复核可运行；实际窗口筛选质量及启用条件仍待验收 |
| 运行时 | llama.cpp 的 `llama-server`，CPU 版随安装包发布 |
| 权重 | 不打包，用户点击后从 ModelScope 下载 |
| 平台 | 本地判断模型首版只做 Windows x64，其他平台隐藏下载入口 |

## 2. 用途路由与存储

### 2.1 各用途对应的步骤

| 用途 | 步骤 |
| --- | --- |
| 主模型 | 现有全部文本步骤，以及军师的 opencode 主循环 |
| 图片模型 | message-insights 的 `vision`、军师的 `media-understanding`、外部导入的 `transcription` 系列步骤 |
| 判断模型 | 只用于新增的 `judge()` 调用 |

图片步骤有两处不能只靠给 `generate()` 传 `role`：

- 军师的 `media-understanding` 在 `plugins/kyf.advisor/engine/runtime.ts` 里把 `service` 直接传给 `generate`，要改成传图片模型的服务。
- `plugins/kyf.message-insights/backend/worker.py`、`plugins/kyf.external-chat-import/backend/transcription.py` 先调 `models.service()` 读模型参数，图片调用要改成 `service_for("image")`。

`self-tags-merge`、`self-timeline-merge`、`self-timeline-assign` 要输出分组 JSON，决策模型做不了，继续走主模型。

军师的 `small_model` 不接判断模型：title、summary 代理已禁用，压缩走主模型，接上也没有收益（`plugins/kyf.advisor/engine/runtime.ts`）。

### 2.2 存储格式

`bridge/model_source.py` 的 `ModelSourceStore` 改为版本 2，文件仍是 `.local/real-client-runtime/api-model-source.json`：

```json
{
  "version": 2,
  "selectedMode": "api",
  "models": [
    {"id": "<32 位十六进制 sourceId>", "name": "", "protocol": "anthropic", "baseUrl": "https://…",
     "model": "…", "contextTokens": null, "timeoutMs": 3600000, "concurrency": 4,
     "encryptedKey": "…"},
    {"id": "<32 位十六进制 sourceId>", "name": "本地判断模型", "protocol": "systemone",
     "builtin": "julia-1", "model": "julia-1", "concurrency": 1}
  ],
  "roles": {"main": "<id>", "judge": null, "image": null},
  "sourceIds": {"<fingerprint>": "<id>"}
}
```

- `roles.judge`、`roles.image` 为 `null` 表示跟随主模型。
- 模型最多保存 16 个（建议）。
- `id` 就是现有的 `sourceId`。删除后再添加同一模型，仍通过 `sourceIds` 取回原编号。
- `protocol: "systemone"` 只用于内置本地模型，首版不开放给用户手动填写。
- 读取时兼容版本 1；第一次写入时升级，并把旧文件保存为 `api-model-source.v1.json`。
- 迁移规则：旧 `api` 成为 `models[0]`，`id` 取旧 `sourceId`，`encryptedKey` 原样搬过去，`concurrency` 取自 `maxConcurrentTasks`（超过 8 截断为 8），`roles.main` 指向它。旧文件未配置 `api` 时迁移为空列表、`roles.main` 为 `null`，等同未配置。
- `resolve_key` 改成按模型查：留空沿用密钥时，只能取同协议且同 Base URL 的已存模型，并复用其密文。安全规则不变。

### 2.3 下发给网关和插件的服务

`bridge/core_services.py` 的 `_gateway_services()` 改为：

- 只下发已分配用途的模型。同一模型有多种用途时只下发一条，`roles` 写全，例如 `["main", "judge", "image"]`。服务上的 `roles` 数组与 §2.2 的用途配置映射同名不同物。
- **主模型永远排第一**。`ModelClient.service()`、军师、知己分析、`message-insights` 都读第一个服务，旧插件要继续可用。
- 每个模型单独处理失败。判断或图片模型缺密钥、能力探测失败时，静默把它的用途退回主模型，不提示。下发的 `roles` 反映生效分配而非配置，这是 §4.3 判断逻辑的依赖，不是 bug。现在的 `return []` 和 `raise GatewayUnavailable` 只对主模型保留。
- 内置本地模型没有用户密钥，不走“缺密钥就跳过”和能力探测这两步。

`bridge/core_gateway.py` 的 `services()` 是发给插件的白名单，新增 `roles`。本地模型的启动参数放在 `local` 字段里，只走 Core 到网关的私有管道，不进白名单。

Python SDK（`plugin-sdk/python/kyf_plugin/models.py`）增加 `service_for(role)`：取第一个含该用途的服务，没有就取主模型。`generate()` 增加可选参数 `role=None`，默认行为不变。增加 `judge()`（§4.1）：Python 侧与 Node 插件共用同一个 Node 工作进程，协议转换只在 §4.2 写一份。

### 2.4 接口

`GET /api/model-source` 保持原有字段，继续表示主模型，新增 `models`（不含密钥）、`roles`、`localJudge`。`chatui/plugin-slots.js` 和 MCP 的 `_connection_view` 依赖原字段。

| 接口 | 作用 |
| --- | --- |
| `POST /api/model-source/models/save` | 新增或修改模型，保存前测试连接 |
| `POST /api/model-source/models/delete` | 删除模型 |
| `POST /api/model-source/roles` | 修改用途分配 |
| `POST /api/model-source/local-judge/prepare` | 开始或继续下载 |
| `POST /api/model-source/local-judge/cancel` | 取消下载，保留进度 |
| `POST /api/model-source/local-judge/delete` | 删除本地模型 |

- `/activate` 保留，含义是保存主模型，MCP 的 `zhiji_save_model_settings` 继续用它。
- `/list`、`/test`、`/reveal-key` 增加可选的 `id`，用于定位沿用哪个模型的密钥。`connection_values` 的白名单要同步放开。
- 新接口要加进 `bridge/real_http.py` 的 `model_endpoints`，以及 `chatui/settings-window.js` 的刷新路径集合。
- 主模型不可删除。删除已分配用途的模型时，该用途退回跟随主模型。
- 保存前测试连接对内置本地模型跳过。

### 2.5 并发

- 每个模型有自己的 `concurrency`，范围 1–8，网关已按服务排队。本地判断模型固定为 1，不可编辑。
- `maxConcurrentTasks` 从 `bridge/core_model_settings.py` 的 `FIELDS` 移除。读取旧文档时要忽略这个多出来的字段，不能报错。
- 同步修改读取它的位置：`bridge/core_services.py`、`bridge/core_external_import.py`、`plugins/kyf.message-insights/backend/insights.py`、`chatui/model-settings.js`，以及 MCP 的 `scheduling` 校验。
- 后两处 Python 调用方用这个值决定自己一次发多少任务，改为读主模型服务的 `concurrency`。`core_model_settings.py` 保存时对这个字段的特殊处理一并移除。

## 3. 设置面板

现在的 `panelModels`（`chatui/index.html`）把连接表单、调度联网、开发者模式堆在一页，还有两个保存按钮。改成主页和详情页两层。

主页单列展示，不加说明文字：

```text
模型设置

用途
  主模型        Claude Sonnet        ›
  判断模型      跟随主模型           ›
  图片模型      跟随主模型           ›

我的模型
  Claude Sonnet    Anthropic         ›
  本地判断模型     168 MB            ›
  添加模型

联网搜索        关闭                 ›
开发者模式                           ›
```

- 用途行点击后选择模型，选完立即保存，没有保存按钮。
- 判断模型的选项最后一项是“下载本地模型（168 MB）”，状态见 §5.4。
- 模型行显示名称和协议。名称留空时显示模型 ID。

详情页从上到下：

```text
接口协议
Base URL
API Key
模型            （可输入，也可点“获取列表”选择）
同时请求数
高级            （默认收起：上下文大小、生成超时）

[测试连接]  [保存]  [删除]
```

- 只有一个“保存”按钮。保存时先测试连接，通过才写入。
- 联网搜索和开发者模式各占一个子页，内容和逻辑沿用现有实现，只是搬过去。
- 删除“统一调度与联网”标题和页头的“生成模型 待读取”。
- 修改主模型会让运行中的任务按现有 stamp 机制停止，保存前提示一句。修改判断或图片模型不提示。

## 4. 判断接口

### 4.1 调用方式

```python
result = models.judge(task, state, {
    "has_content": {"type": "noul", "instructions": "这段聊天里有没有本人经历的具体事情？"},
}, step="judge-window-filter")
# {"answers": {...}, "engine": "systemone", "fingerprint": "..."}
```

问题和回答的格式与 llama.cpp 的 `/v1/systemone` 一致：

| 类型 | `criteria` | 回答字段 |
| --- | --- | --- |
| `choice` | 选项到说明的对象，说明可为 `null` | `choice`、`probabilities`、`confidence` |
| `score` | 2–10 个等级说明，从低到高 | `score`、`legend`、`probabilities`、`confidence` |
| `noul` | 可省略 | `noul`，即答案为真的概率 |

单次最多 16 题；每题必须提供 `instructions`。`state` 最多 6000 个 Unicode 字符，但这不能保证低于 8192 tokens；实际上下文超限同样报 `judge-state-too-long`。影子评估收到该错误会继续二分重试，不能截断窗口后当作完整判断。Julia-1 最长 8,192 tokens，选择题 2–20 个选项；`score` 是从 0 开始的等级加权期望，可以为小数，`legend` 和 `probabilities` 使用字符串索引对象。

错误码：`judge-not-configured`、`judge-unavailable`、`judge-state-too-long`（插件切分 `state` 后重试）、`judge-invalid-output`。判断只是优化手段，调用方遇到这些错误时必须按“不判断”继续，不能让主任务失败。

### 4.2 实现位置

网关继续不做协议转换，转换放在 SDK 的 Node 工作进程（`plugin-sdk/node/model-worker.ts`）：增加 `judge` 命令，按服务协议分两路。

- `protocol == "systemone"`：POST 到 `<服务地址>~<步骤>/v1/systemone`，校验回答字段，概率必须是 0–1 的有限数。
- 其他协议：用 `generateStructured` 发 JSON 提示词，解析后转成同样格式，`engine` 为 `generate`。这类概率未校准，不能和 `systemone` 共用阈值。

网关（`electron/model-gateway.ts`）要做的改动：

- 接受 `systemone` 协议，用 `Authorization: Bearer` 注入本地随机密钥。
- `UsageMeter` 能处理 `usage.input_tokens`，`output_tokens` 恒为 0，这类调用要正常进入用量和调用记录。
- 调用走现有排队、取消和令牌校验，不开旁路。

### 4.3 什么时候用

判断跟随主模型时，每次 `judge()` 都要花主模型的 token，筛选反而更贵。所以用于省钱的筛选步骤只在判断模型独立于主模型时运行：

```python
service = models.service_for("judge")
if "main" in service.get("roles", []):
    ...  # 判断模型跟随主模型，跳过筛选
```

## 5. 本地判断模型

### 5.1 候选

| 模型 | 体积 | 公开成绩 | 结论 |
| --- | --- | --- | --- |
| Julia-1 Q8 | 168 MB | 简中 18 类场景 83.6%，typed-decisions 73.15%，Banking77 64% | 默认 |
| Laya 多语言版 Q8 | 362 MB | 简中 20 类 65%，同一 typed-decisions 35.2% | 备选，要微调才好用，且 Q8 只在 HuggingFace 社区 |
| Qwen3.5-0.8B Q4 | 533 MB | 无中文判断成绩 | 只做对照，是生成模型 |

- 这些成绩来自作者或社区，都不是微信聊天，也没有在本机验证。Julia-1 的成绩是 BF16/FP32 测的，Q8 没有公开数据。
- 只有 typed-decisions 这一组能在 Julia-1 和 Laya 之间直接比较。
- 已排除：Kev-0.8B（只支持英文）、Kev-4B 和 lev（约 3 GB）、OpenJev（19 GB 起，CC-BY-NC，付费版不能用）、laya-zh-v2（1288 MB，换场景后 51.6%）。
- llama.cpp 当前 README 还列出 granite-switch、lfm2-d1、h2o-lightning、vllm-d3 等决策模型，方案讨论时没有评估，阶段 0 要补查。

### 5.2 固定资源

权重清单 `scripts/judge-model.json`，格式沿用 `scripts/embedding-model.json`：

| 字段 | 值 |
| --- | --- |
| 仓库 | ModelScope `ggml-org/Julia-1-GGUF` |
| 文件 | `Julia-1-Q8_0.gguf` |
| 大小 | 168,166,496 字节 |
| Revision | `ff2a69b1baac01bbac5b46a2f377103c07fd8cc3` |
| SHA-256 | `1ea6a7e87156eeeda88cb7a36a61265b37ba7b993897b7289b99aea5b5e47069` |
| URL | `https://modelscope.cn/api/v1/models/ggml-org/Julia-1-GGUF/repo?Revision=<Revision>&FilePath=Julia-1-Q8_0.gguf` |

`allowedHosts` 用 embedding 清单的同一组 ModelScope 域名。

运行时 `llama-b12032-bin-win-cpu-x64.zip`，来自 GitHub `ggml-org/llama.cpp` 的 `b12032`（2026-11-01 发布），24,582,754 字节，SHA-256 `5296f5d18bb7d422c7c26f4bb9bc796d9d4f41e1312a8c296cff475bdc60d0d4`。

- 构建时按固定地址和校验值取得，不提交进 git。运行时只在构建期获取，不面向用户下载。
- 解压后的文件必须加入 `scripts/stage-real-client.py` 的清单，否则开发环境能跑，安装包却缺文件。
- 这个版本发布不久，阶段 0 要确认它能跑 Julia-1。需要换版本时，同步更新版本号和校验值。
- 新增 `licenses/llama.cpp-MIT.txt`、`licenses/Julia-1-Apache-2.0.txt`，并更新 `docs/acknowledgements.md`。

### 5.3 下载与运行

下载：把 `bridge/core_embeddings.py` 的清单校验、域名白名单、`.part` 断点续传、SHA-256 校验抽到新文件 `bridge/core_model_assets.py`，`EmbeddingService` 和判断模型共用。

- 错误文案参数化，不再写死“检索模型”。
- `dimensions` 改成 embedding 专属字段。
- `scripts/embedding-model.json` 不改，`bridge/test_core_embeddings.py` 继续通过。
- 权重放到 `.local/core/judge/julia-1/`。

运行：`llama-server` 由网关进程管理，新文件 `electron/model-gateway-local.ts`，做法与 embedding 在网关内懒加载一致。

- 请求获得队列名额后才启动，等 `/health` 就绪再转发。
- 只监听 `127.0.0.1` 的空闲端口，带每次随机生成的 `--api-key`。
- 线程数不超过物理核心数的一半，用 `os.setPriority` 设为低于正常优先级。
- 空闲 5 分钟（建议）后退出，释放内存。
- 异常退出后重启一次，再失败报 `judge-unavailable`。
- 启动参数须按 `b12032` 实际确认，尤其是 `-c` 和 `--ubatch-size`。

### 5.4 用户流程

点击“下载本地模型（168 MB）”后，判断模型那一行依次显示进度，用户没有单独的部署步骤：

```text
检查电脑 → 下载中 45% → 校验 → 自测 → 已启用（约 0.3 秒/次）
```

1. **下载前检查**：只拦明显不够的情况。建议：可用内存少于 2 GB 或磁盘少于 500 MB 时不下载，并说明原因。
2. **下载**：可取消，下次继续。失败时保留进度。
3. **自测**：用固定中文样例（`scripts/judge-selftest.json`）跑一遍，检查回答格式，并测单题耗时。首次加载不计入。
4. **按耗时决定**：以下为质量通过后的启用策略。当前候选质量尚未验收，准备完成只登记模型，`qualityVerified=false`；用户可手动选作判断用途进行影子评估，不按短题耗时自动启用。

| 单题耗时（建议阈值） | 结果 |
| --- | --- |
| 不超过 0.5 秒 | 自动启用 |
| 0.5–2 秒 | 自动启用，标记为只跑后台任务。首版判断调用都在后台，暂无行为差别 |
| 超过 2 秒 | 不自动启用，行内状态只显示“电脑较慢”，不弹提示。文件保留，用户可手动选择 |

删除本地模型时同时清除文件；若它正担任判断模型，用途退回跟随主模型。

## 6. 第一个用途：窗口筛选

自我剖析的 `self-evidence-extract`、`self-timeline-extract` 调用量最大。在把窗口发给主模型之前，先用判断模型问一句“这段有没有值得提取的内容”，没有就跳过，从而节省 token。

规则：

- **先影子运行**：只记录判断结果，不跳过任何窗口。影子判断在提取全部完成后批量补跑，不占用提取步骤的串行时间；用主模型的提取结果作对照，算漏检率，就是主模型提取到内容、判断模型却判“没有”的比例。
- **达标后才开启跳过**：主要看漏检率。默认值由用户在看过结果后确定。
- **只跳过高置信结果**：低于阈值的窗口仍交给主模型。不同模型的阈值不能照搬。
- **被跳过不等于已提取**：跳过记录单独保存，并绑定判断模型的 `fingerprint` 和问题版本。关闭筛选或换模型后，这些窗口可以重新提取。
- **不改现有窗口的缓存身份**：见 §7 第 5 条。

否定句是已知风险：Laya 官方记录过置信度 0.9998 的否定句误判，Julia-1 是否有同样问题未知。阶段 0 的样例要覆盖否定句。

如果零样本效果不够，再考虑微调。对外分发的权重只能用公开或合成数据训练，不能用真实聊天。用户自己的微调结果只留在本机。

## 7. 不能破坏的约束

1. **主模型的 `stamp` 和 `fingerprint` 迁移前后必须完全相同**。`stamp` 的输入包含 `sourceId`、协议、地址、模型、`contextTokens`、`modelMetadata`、`encryptedKey`。密文不能重新加密，否则缓存失效，运行中的任务也会被停止。
2. **`services[0]` 永远是主模型**。
3. **`roles` 不能参与网关路由比较**。`ModelGateway.configure` 用路由字段判断服务是否变化，变化后排队中的请求会收到 409 `service-changed`。比较时要像 `concurrency`、`timeoutMs` 一样排除 `roles`，否则改判断模型会打断主模型任务。`roles` 同样不得进入任何 stamp、fingerprint 或缓存键的哈希输入。
4. **改判断或图片模型不能改变主模型 `stamp`**，也不能停止只用主模型的任务。
5. **不改现有结果的缓存身份**。`c07d903` 修过路由字段顶掉 27.7 万条时间线窗口缓存的问题。图片模型未单独设置时，图片步骤的身份也要保持不变。
6. **判断或图片模型出错不影响主模型**。
7. **插件拿不到密钥和本地启动参数**。
8. **新增的 Python 和 TS 文件要加入 `scripts/stage-real-client.py` 的清单**。
9. **面向用户的下载只走 ModelScope**。`allowedHosts` 不加入 HuggingFace、GitHub 或其他境外域名；GitHub 上的运行时只在构建期获取。镜像同步滞后由固定 Revision 和 SHA-256 兜底，不做静默 fallback。

## 8. 实施阶段

每个阶段单独提交。完成并通过测试后按项目约定合入 `dev`，不 push。

### 阶段 0：实测（合成冒烟已完成，真实窗口评测待做）

下载已获用户授权（2026-11-01），合计约 1.1 GB，放临时目录，只在本机运行：

| 文件 | 来源 | 大小 |
| --- | --- | --- |
| `Julia-1-Q8_0.gguf` | ModelScope `ggml-org/Julia-1-GGUF` | 168 MB |
| `laya_multilingual_q8_0.gguf` | hf-mirror.com（镜像 HuggingFace `mys/laya-multilingual-GGUF`） | 362 MB |
| `Qwen3.5-0.8B-Q4_K_M.gguf` | ModelScope `unsloth/Qwen3.5-0.8B-GGUF` | 533 MB |
| `llama-b12032-bin-win-cpu-x64.zip` | GitHub `ggml-org/llama.cpp` | 25 MB |

- 用真实聊天窗口比较延迟、内存、判断准确度和漏检率，以主模型结果为对照，并抽查。
- 补查 §5.1 提到的其他决策模型。
- 结果写成 `docs/judge-model-benchmark-<日期>.md`。如果 Julia-1 不达标，先和用户定下默认模型，再做阶段 3。

验收：有明确的默认模型，以及自测和筛选阈值的实测依据。

### 阶段 1：多模型设置（已实现）

存储 v2、迁移、路由、接口、并发、设置面板、图片用途。

验收：

- 版本 1 文件迁移后，主模型 `sourceId`、`stamp`、`fingerprint` 不变，有测试锁定。
- 只配一个模型的老用户，行为和现在一致。
- 配置独立图片模型后，§2.1 的图片步骤走到该模型，调用记录显示正确。
- 修改判断或图片模型时，主模型排队和运行中的请求不受影响。
- 判断或图片模型的密钥失效时，主模型继续工作。
- 用预览窗口实际走通添加、编辑、删除、分配用途、联网搜索和开发者模式。

### 阶段 2：判断接口（开发中，待验证）

补齐 SDK 的 `judge()`、网关 `systemone` 协议，以及普通大模型的 JSON 回退。`service_for()` 已在阶段 1 提供；当前工作区已有判断接口和测试代码，完成状态以验证结果为准。

验收：

- 模拟的 `/v1/systemone` 服务和普通大模型两路返回同样结构。
- Python SDK 的 `judge()` 与 Node 插件共用工作进程，返回同样结构。
- 无效输出、取消、任务停止都有测试。
- 调用记录和用量统计能看到判断调用。

### 阶段 3：本地判断模型（开发中，运行与发行闭环待完成）

通用下载器、运行时打包、进程管理、自测、下载界面。

验收：

- 全新环境点击一次就能完成下载、校验、自测和启用。
- 中断后能续传；校验失败不会启用；域名白名单外的跳转被拒绝。
- 空闲后进程退出，下次调用自动启动；退出应用后没有残留进程。
- 打包后的安装版也能运行。
- 超过 2 秒的情况用模拟耗时测试。

### 阶段 4：窗口筛选（影子实现开发中，跳过未启用）

先上线影子模式，把漏检率结果交给用户，用户同意后再开启跳过。

验收：

- 影子模式不改变任何提取结果，也不改变缓存命中。
- 开启跳过后，token 用量下降，漏检率不超过用户定的值。
- 关闭筛选后，被跳过的窗口能重新提取。

## 9. 需同步更新的文档

- `docs/analysis-plugins-requirements.md` 的 V7：写的是任务专用本地分类模型属于插件资源，现在判断模型归 Core。
- `docs/plugin-architecture.md` §7：补充多服务、用途和 `systemone`。第 11 行的 Laya 退役记录保留，它说的是旧分析链路。
- `docs/modifications.md`、`docs/acknowledgements.md`：按现有约定记录新增第三方组件。

## 10. 待用户确定

1. 阶段 0 之后的默认模型。
2. 窗口筛选开启跳过的漏检率上限。

已定（2026-11-01）：自测阈值沿用 §5.4 建议值（0.5 秒 / 2 秒），阶段 0 后按实测调整；影子判断在提取完成后批量补跑；提示文案只保留“修改主模型会停止运行中任务”一条，判断/图片模型失败静默回退、慢机器不弹提示；面向用户的下载只走国内镜像。

## 11. 来源

- [llama.cpp 服务器文档](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)、[PR #29818](https://github.com/ggml-org/llama.cpp/pull/29818)
- [Julia-1 模型卡](https://huggingface.co/SupersonicLabs/Julia-1)、[ModelScope 文件](https://modelscope.cn/models/ggml-org/Julia-1-GGUF)
- [Laya](https://github.com/NandhaKishorM/laya)
- [Kev-0.8B 模型卡](https://huggingface.co/jaredpalmer/kev-0.8b)
- [laya-zh-v2](https://modelscope.cn/models/zcgnull/laya-zh-v2)

## 12. 2026-11-02 接手复核与实现验收

复核并修正了首个模型保存后配置无法重读、保存接口遗漏 API Key、按模型 ID 留空密钥时缺少同协议／地址校验等问题。另补齐同模型多用途合并、私有密文参与服务 stamp、非主模型密钥与确认交互、真实图片请求路由、图片观察缓存和表情失败预算的模型身份隔离。迁移、空配置和重复身份边界有回归覆盖。

阶段 2 两路判断和 Python／Node 共用 worker 已验证。阶段 3 已实际通过隔离 Julia 自测、任务令牌调用、用量记录、删除和进程退出；下载取消、删除竞争、启动失败记录与崩溃重试脱敏也有专项测试。阶段 4 只记录影子结果，独立于提取缓存，整批提取完成后才补判；分析进度面板可按需读取按引擎分开的漏判分母与计数。全部实现尚未提交或推送。

构建前须运行 `python scripts/setup-judge-runtime.py`，或通过 `--archive <已有 ZIP>` 复用已下载的固定归档。该步骤只在开发／构建机执行，产物位于 `.local/tools/judge-runtime/b12032/`；stage 检查逐文件哈希，仅复制审核过的 exe／DLL／许可闭包到 `runtime/judge/`，缺失或篡改时拒绝打包，不在客户端执行 GitHub 下载。完整安装包的全新安装验收仍待进行。

验证记录：全量 Python 测试通过；最终 Node 测试 1165 通过、3 条条件跳过，typecheck 通过；模型设置真实 Electron 37 项检查通过；自我剖析真实 Electron 流程通过并包含影子统计与账号隔离的测试。相关 Core、SDK、下载、打包、图片插件和影子专项测试另行通过。真实筛选准确度未验收，本地候选准备后不自动分配用途，当前代码没有开启窗口跳过的开关。
