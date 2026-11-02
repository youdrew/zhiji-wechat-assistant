# 中文私聊情境检索 benchmark

固定中文聊天合成集，对比 BGE-small-zh-v1.5、BGE-base-zh-v1.5、Qwen3-Embedding-0.6B 的词项、纯语义与混合检索。最终结果见 `docs/self-analysis-benchmark-2026-10-18.md`。

## 评测对象

- 比较的是“找到可参考的历史回复情境”，不评价最终回复生成，也不证明像某个真实用户。
- `dataset.json` 是 AI 编写并交叉复核的合成数据，不含真实聊天、联系人或本人资料。数据卡说明标签与困难负例，`dataset-lock.json` 固定实验输入及源码哈希。未标注候选按零评分，不能当作穷尽的人类相关性标注。
- `protocol.json` 在正式结果前固定参数、指标、性能条件和默认模型决策规则；所有候选都可检索，不按 family 缩小候选池。不拿 test 集调融合权重或指令。
- 词项与混合排序调用生产代码，语义向量离线注入。前文输入不含候选的待学习回复；回复只用于去重和相关性复核。主指标 nDCG@4 对应线上最多四条参考，@10 是诊断扩展。
- 主测 `raw` 按当前回复建议的输入格式去掉 query 每行开头的说话人标签；候选保持建库时的标签。`role-explicit` 单独消融，保留 query 标签，使用同一语料及标签，不参与默认模型选择。

## 复现

在仓库根目录运行项目开发环境。需要约 3 GB 模型资源、额外临时空间和足够内存；权重及原始输出保存在被 Git 忽略的 `.local/`。下载脚本固定国内 ModelScope revision、长度与 SHA-256，不回退至 Hugging Face。运行编码与评测不联外。

```powershell
. .local/setup/Enter-Dev.ps1
python benchmarks/self-analysis-chat/download-models.py
python benchmarks/self-analysis-chat/download-models.py --verify-only
python tests/test_self_analysis_benchmark.py
python benchmarks/self-analysis-chat/run.py
$env:OPENBLAS_NUM_THREADS = '2'
$env:OMP_NUM_THREADS = '2'
$env:MKL_NUM_THREADS = '2'
python benchmarks/self-analysis-chat/evaluate.py --dataset benchmarks/self-analysis-chat/dataset.json --vectors bge-small-zh-v1.5=.local/benchmarks/self-analysis/2026-10-18-v1/bge-small-zh-v1.5.npz --vectors bge-base-zh-v1.5=.local/benchmarks/self-analysis/2026-10-18-v1/bge-base-zh-v1.5.npz --vectors qwen3-embedding-0.6b=.local/benchmarks/self-analysis/2026-10-18-v1/qwen3-embedding-0.6b.npz --output .local/benchmarks/self-analysis/2026-10-18-v1/quality --bootstrap 10000
python benchmarks/self-analysis-chat/summarize.py
python benchmarks/self-analysis-chat/stress.py
python benchmarks/self-analysis-chat/memory.py
```

保留说话人标签的消融使用同一个 `evaluate.py` 命令，增加 `--representation role-explicit`，三份向量文件改为 `<模型>-role-explicit.npz`，输出目录改为 `quality-role-explicit`。向量元数据的表示类型必须与评测参数一致。

`run.py` 默认三轮新进程并轮换模型顺序，每轮查询使用相同随机顺序。候选 batch=8，交互查询 batch=1，CPU FP32、ORT 两线程、最多 512 tokens；保留尾部情境及模型所需特殊 token，BGE 用 CLS，Qwen 用最后有效 token，均 L2 归一化。BGE 使用官方中文检索指令，Qwen 使用预先固定的英文聊天情境检索指令。

`performance.json` 逐请求记录 IPC 时间、编码器内部时间、截断数和进程内存。`quality/` 保存逐查询排名、所有指标、分类切片、配对 family bootstrap 和误例。`latency-and-resources.json` 合成查询编码与排序耗时；延迟不含 Core HTTP、源消息校验、队列等待或回复生成。`scan-scaling.json` 在重复向量构造的负载库上测 480/5,000/50,000 条索引扫描成本，不能用来评价 50,000 条独立聊天的召回质量。

合成延迟是独立编码测量与排名测量按 query 配对相加的估计，不是一次应用请求的实测端到端延迟。扩容实验继承生产 SQLite 分页与过滤，只把固定的 512 维字节长度校验泛化为各模型维度；产品本身仍只支持当前 512 维模型。工具断言未超时的扫描必须覆盖全部行。

主表语义相似度来自预载 RAM 矩阵，没有计入生产 SQLite 语义分页、BLOB 读取和逐块归一化；另见 `scan-scaling.json`，不把这段独立扫描时间静默加回延迟主表。

工作集 RSS 用独立编码进程的 Windows `peak_wset` 加 10ms 采样；private memory 仅采样峰值，不能当作 OS 保证的精确峰值。加载是进程冷启动，未清空系统文件缓存。保留环境信息及三轮差异，避免将一次开发机测量外推到所有电脑。

`method-memory.json` 另测七种检索方式在同一组 16 个热查询期间，同时存活的 Python 排序子进程及可选 Node 编码子进程的 RSS/private 总和。报告 10ms 采样峰值，包含 NumPy、冻结数据及向量等测量程序开销，不含协调进程或完整应用；共享页可能重复计数。不能把不同进程各自的历史最高值相加来声称同时占用。

内存采集包括完整的受控进程树与 Windows 虚拟环境启动器/控制台辅助进程。首版仅采 `Popen.pid` 漏掉实际 Python 子进程，已废弃并重测；[measurement-amendments.json](measurement-amendments.json)记录唯一测量修复的前后哈希。原 `dataset-lock.json` 保留评分前源码身份，该修复不改变数据、检索成绩、编码性能或决策规则。核验 `memory.py` 时应同时核对修订记录，其余锁定源码保持一致。

本次可核对结果保存在 [results/](results/)，包括完整指标、逐查询排名、配对差异、延迟、内存、扫描与运行清单。模型权重、原始向量及逐批日志留在 `.local/`。

## 文件

- `models.json` / `download-models.py`：固定国内公开资源、下载与完整性校验。
- `encoder.cjs`：独立离线编码器及 NDJSON 进程协议。
- `run.py`：编码质量输入、三轮时延/内存采集。
- `evaluate.py`：生产排序、graded relevance、分组配对置信区间。
- `summarize.py` / `stress.py` / `memory.py`：排序耗时、资源汇总、有界 SQLite 扫描扩容及同期检索进程内存诊断。

池化与指令依据：[BGE 官方说明](https://bge-model.com/bge/bge_v1_v1.5.html)、[Qwen3-Embedding 官方说明](https://github.com/QwenLM/Qwen3-Embedding)。本 benchmark 不重新发布模型权重，模型许可、来源及模型卡保留在清单和本机缓存中。
