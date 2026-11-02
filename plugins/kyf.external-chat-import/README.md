# 外部聊天识别插件

这是内置的识别业务插件。核心负责素材文件、抽帧操作、后台任务、候选核对和正式记录；插件负责识别参数、提示词、模型调用和输出校验。只调用 SDK 的统一模型服务，不读取模型密钥，不直接请求外部服务，不启动分析，不发送聊天消息。

## RPC

`transcription.plan` 返回机械预处理参数：长截图按 1600 像素高切片、400 像素重叠；录屏保留每 750 毫秒的覆盖点，并在该点前后 100 毫秒内本地择优，最多 240 帧且最多 180 秒。图像优先使用 PNG；小画面尽量放大至 660 像素宽，但最多三倍、宽度最多 1600 像素、每张最多 2.5 MiB。放大不代表恢复不存在的细节。核心应拒绝超限录屏并说明原因，不把截取到的前半段展示成完整转录。采样仍可能漏掉快速滑过的文字，需要保留全部素材回查。

`refinement` 参数限制每次局部复核最多三帧、每批最多 40 个目标、每目标一次；格式重试独立限制为一次。插件定义业务策略，核心执行抽帧、保留证据并安排后台任务。

`transcription.recognize` 参数：

```json
{"image":"data:image/png;base64,...","platform":"用户填写的来源","previous":[{"id":"candidate1","segmentId":"frame1","version":1,"text":"上一个画面的候选","sender":"other","kind":"text","issues":[],"bbox":[0.1,0.2,0.6,0.1]}],"overlap":{"reliable":true,"offsetY":-0.2,"scaleY":1}}
```

`platform` 是用户填写的来源名称，去除首尾空白后须非空、最多 80 个字符，不映射为软件枚举或界面模板。识别仍以实际图像为依据，来源名称只是待处理资料。

核心控制通道上下文提供 `task:{id,account,user,kind:"background"}` 和 `execution:{id,token}`。插件会在当前 RPC 期间注册 `TaskContext`，支持标准 `task.cancel`，并把执行凭据交给 SDK 模型调用。执行由核心管理，插件不创建、完成或自动重试整个批次。每个执行同时只能进行一个识别或复核 RPC；请求超时由核心模型设置和共享 SDK 管理，插件不另设短超时。

结果：

```json
{"schema":1,"messages":[{"text":"画面中的正文","sender":"other","kind":"text","timeLabel":null,"timestamp":null,"issues":[],"bbox":[0.1,0.2,0.6,0.1]}],"warnings":[]}
```

`sender` 为 `self | other | unknown`；`kind` 为 `text | image | voice | video | system`。可选 `quote` 独立保留实际可见引用；可选 `bbox` 是相对于传入图像的 `[x,y,width,height]`。图片、语音、视频只保存可见标签，不代表拥有原件。首版保留 `timeLabel` 原样并始终输出 `timestamp:null`，不推断年份、时区或把录屏时间当成消息发送时间。

日期分隔条、会话标题、输入框等界面不生成聊天候选。解析器只对明确标为日期分隔条或系统类别的完整日期文字作额外过滤。本人／对方气泡中的“10:20”等真实文字仍保留；未明确分类且发送者未知的时间文字也保留 `unknown_sender` 疑点，避免误删人类消息。真正聊天内系统事件使用 `kind:system,sender:unknown`，无需伪造人类发言人，也不会因没有人类发言人产生 `unknown_sender`。普通省略号不再自动视为截断，`truncated` 必须来自可见的裁切或遮挡。

稳定疑点值：`unclear_text`、`unknown_sender`、`truncated`、`incomplete_time`、`possible_duplicate`、`uncertain_order`、`missing_region`、`multiple_conversations`、`missing_original`。`incomplete_time` 和 `missing_original` 是信息限制，正文与发言人明确时可由核心允许保存；其余疑点需要核对。缺少正文、未知发言人、无法识别字符会强制加入疑点，模型不能仅靠遗漏 `issues` 将这些内容标为明确。

结果不按正文去重。相邻画面提示最多 20 条，每条最多 20000 字；当前画面的每个实际可见气泡都保留，同样的话可以出现多次。结果另含 `supportsTracking:true` 和 `links`，仅在相邻帧有可靠位移、几何位置和气泡内容相符时建议复用；截断补全和文字冲突还要求独立气泡锚点。`transcription.match` 用 `{previous,current,overlap}` 执行同一纯关联逻辑，不调用模型；`currentIndex` 使用候选原有 `order`，没有时使用列表下标。核心校验记录归属并保护人工修改后，决定是否接受建议。

`transcription.review` 接收 `{platform,frames:[{id,image,videoMs?}],target:{id,segmentId,text,sender,kind,issues,bbox?,quote?}}`。图像最多三张，只针对一个已存在目标补看。模型必须确认 `matchesTarget:true`、`fullyVisible:true`，明确选择一张目标完全可见的证据帧，并提供无剩余阻断疑点的正文和发送者，才返回：

```json
{"schema":1,"resolved":true,"message":{"text":"完整原文","sender":"other","kind":"text","timeLabel":null,"timestamp":null,"issues":[],"bbox":[0.1,0.3,0.6,0.1]},"evidenceSegmentIds":["frame2"],"preferredEvidenceSegmentId":"frame2","warnings":[]}
```

证据编号必须全部来自本次提供的图像，`bbox` 对应明确选择的那张完整证据图。不能把不同帧缺失的半句拼猜成“完整正文”。系统事件没有人类发言人是例外；不明确的正文、发送者、引用、目标身份或完整性继续保留待核对。无法确定或第二次仍格式错误时返回 `resolved:false`、空证据列表和安全警告，不返回可覆盖原候选的正文。插件不覆盖人工修改，也不自动确认正式保存。

空画面返回 `messages:[]` 与 `no_visible_messages` 警告，不能被当成成功获取完整聊天；只有响应中的观察全部被明确过滤为日期／界面时才附 `uiOnly:true`，空输出本身不能证明是纯界面。已知不支持视觉的模型返回 `vision-required`；能力元数据缺失时允许尝试，并返回 `vision_unconfirmed`。模型输出结构错误时，使用相同图像再请求一次完整、规范的输出，不回传错误响应、不删除实际气泡来凑格式；取消、超时和提供方错误不会触发此重试。仍失败则不返回部分候选，核心保留已完成画面和待核对内容。

运行合成测试：在项目开发环境中执行 `python -m unittest discover -s plugins/kyf.external-chat-import/tests -v`。这些测试验证边界和调用协议，不代替不同来源真实素材上的识别质量验收。
