# 知己微信助手 · Zhiji WeChat Assistant 第三方来源与许可证

本项目本体采用 [Apache-2.0](LICENSE)。知己微信助手基于 [WechatVibe](https://github.com/tswawa/WechatVibe) 修改开发；两者均为独立项目，与微信、腾讯、Laya 或以下上游项目不存在官方隶属或背书关系。本文件说明当前使用的组件及保留的历史来源；传递依赖仍以各自附带的许可证为准。

## 内嵌 Laya 源码

历史版本 `electron/laya/` 中的 `types.ts`、`pyjson.ts`、`tokenizer.ts`、`questions.ts`、`prompt.ts`、`calibration.ts` 和 `agent.ts` 来自 [mizchi/laya-mlx](https://github.com/mizchi/laya-mlx) 的 `web/packages/laya-web/src`，提交 `dc3aa6b150cb861d0788fbd421cfd1303de4ed57`。上游采用 Apache-2.0；原有完整条款和声明保留为 [Laya LICENSE](licenses/Laya-Apache-2.0.txt) 与 [Laya NOTICE](licenses/Laya-NOTICE.txt)。历史适配吸收了 nizitao 的 [PR #26](https://github.com/tswawa/WechatVibe/pull/26)，固定提交 `bca5c5ab4258e16340b5a4ecea94d070011853a9` 中的分词缓存与共享 state 编码优化，并补充不可变返回、原文长度限制和生命周期清理。

当前版本已移除上述源码和旧 Laya 分析流水线；此节保留来源与贡献说明，不表示安装包仍提供 Laya 本地分析。

## Agent 会话引擎与界面组件

Agent 使用 OpenCode `v1.18.34` 的官方 SDK 和独立 Windows 引擎。来源为 [anomalyco/opencode](https://github.com/anomalyco/opencode)，固定提交 `e9f8a210b9e2b1e13d375b84906069886eb3b767`，MIT 完整许可证见 [licenses/OpenCode-MIT.txt](licenses/OpenCode-MIT.txt)。

`plugins/kyf.advisor/ui-src/vendor/opencode/` 的 Markdown 流式投影、消息文本读取、TextShimmer 与 SessionRetry 来自该提交的 `packages/session-ui` 和 `packages/ui`；SessionRetry 的主题与翻译依赖经过局部适配。它们编译为独立面板组件，不引入上游全局主题或编程工作区。编译依赖 Solid、Marked、Remend 和 DOMPurify，实际许可证保留在对应 npm 包及构建输出中。

## 分析模型

源码和首版安装包**不含生成模型权重**。历史版本曾使用 [mizchi/laya-multilingual-onnx](https://huggingface.co/mizchi/laya-multilingual-onnx) revision `d9d003d543e63d6d3375c21d44624136bd1e0bad`；该模型声明 Apache-2.0，模型卡说明它是独立移植，并非 Convai Innovations 官方发布。当前版本不再附带其下载脚本或模型文件，生成式分析由用户配置的模型服务提供。

可选的本机判断使用 [Supersonic Labs 的 Julia-1](https://huggingface.co/SupersonicLabs/Julia-1)，原作者模型卡明确声明模型产物采用 Apache-2.0；完整条款保留为 [Julia-1 Apache-2.0](licenses/Julia-1-Apache-2.0.txt)。实际下载为 `ggml-org/Julia-1-GGUF` 的 `Julia-1-Q8_0.gguf`，ModelScope revision `ff2a69b1baac01bbac5b46a2f377103c07fd8cc3`，来源、大小和 SHA-256 由 `scripts/judge-model.json` 固定；权重不随源码或安装包分发，用户按需下载。

判断运行时为 [llama.cpp b12032](https://github.com/ggml-org/llama.cpp/tree/b12032) 的 Windows x64 CPU 构建，固定归档与文件哈希见 `scripts/judge-runtime.json`。本项目构建时仅复制已核对的运行时闭包，并保留 [llama.cpp MIT](licenses/llama.cpp-MIT.txt) 与 [LLVM OpenMP Apache-2.0 及例外](licenses/LLVM-OpenMP-Apache-2.0.txt)；安装版中实际包含的文件仍须与构建清单一致。此判断资源不提供生成式聊天，也不构成其对私人聊天判断质量已经验收的声明。

## 运行依赖

自我剖析的本地语义检索按需下载 **BAAI BGE-small-zh-v1.5**（MIT，约 24M 参数、512 维）。权重来自 [Xenova ONNX 国内镜像](https://modelscope.cn/models/Xenova/bge-small-zh-v1.5)，固定 ModelScope revision `313386ad7ffad360a4870fd1910f8ecaf1a05151`；原模型与使用方式见 [BGE 官方文档](https://bge-model.com/bge/bge_v1_v1.5.html)。来源、字节数及 SHA-256 固定于 `scripts/embedding-model.json`。源码和便携包不含模型权重，首次建库将其下载到本机 `.local/core/embeddings/`，保留模型卡；上游 MIT 原文与署名另见 [licenses/BGE-MIT.txt](licenses/BGE-MIT.txt)。下载和重定向只允许指定的国内 ModelScope 地址，不回退至 Hugging Face。推理复用下列 ONNX Runtime 与分词器依赖，分词器从本机文件初始化。

| 组件 | 锁定版本 | 许可证与保留位置 |
| --- | --- | --- |
| `@huggingface/tokenizers` | 0.2.0 | Apache-2.0；安装包内 `LICENSE` |
| `@anthropic-ai/sdk` | 0.128.0 | MIT；安装包内 `LICENSE`，用于 Anthropic 兼容接口 |
| `@google/genai` | 2.24.0 | Apache-2.0；安装包内 `LICENSE`，用于 Gemini 兼容接口 |
| `openai` | 7.23.0 | Apache-2.0；安装包内 `LICENSE`，用于 Responses 和 Chat Completions 接口 |
| `ollama` | 0.6.3 | MIT；安装包内 `LICENSE`，用于 Ollama 接口 |
| `onnxruntime-node` / `onnxruntime-common` | 1.30.0 | MIT；上游 [LICENSE](https://github.com/microsoft/onnxruntime/blob/v1.30.0/LICENSE)，全文见下文 |
| `tsx` | 4.23.15 | MIT；安装包内 `LICENSE` |
| `undici` | 7.29.1 | MIT；安装包内 `LICENSE`，用于应用内更新的 HTTP 代理连接 |
| Electron | 44.4.3 | MIT 及 Chromium 第三方条款；便携目录内 `LICENSE.electron.txt`、`LICENSES.chromium.html` |
| Electron Builder | 26.15.3 | MIT；仅构建期使用，安装包内 `LICENSE` |
| TypeScript / `@types/node` | 7.0.2 / 26.6.2 | Apache-2.0 / MIT；仅开发期使用，安装包内许可证 |
| Node.js | 24.11.1 | Node.js 及其第三方条款；源码保留 [完整许可证](licenses/node-LICENSE-24.11.1.txt)，便携版复制到 `resources/client/runtime/node/LICENSE` |
| Python | 3.14 | PSF License；便携版复制基础运行时的 `LICENSE.txt` |
| `wechatauto-replica` | 1.2.2.6 | Apache-2.0；安装发行包 `wechatauto_replica-1.2.2.6.dist-info/licenses/LICENSE` |

完整 Python 版本集合见 [python-requirements.lock.txt](python-requirements.lock.txt)，Node 直接及传递版本见 [package-lock.json](package-lock.json)。读取层的来源说明见 [native-reader/THIRD_PARTY_NOTICES.md](native-reader/THIRD_PARTY_NOTICES.md)。便携构建只复制所需的 Python 包与 Node 包闭包，并保留包内实际附带的许可文件；它不复制整个开发环境。

`wechatauto-replica` 声明的 `winsdk`、`pyautogui` 未安装，属于未启用的可选 GUI 路径；读取依赖与来源声明继续保留。免费版不含微信发送插件、发送 SDK 或执行器，不会操作微信发送。当前生成式分析使用用户配置的模型服务，所需聊天片段及有限媒体图像会发送到该地址；旧 Laya 分析已停用，详见[媒体说明](docs/chat-media.md)。

## 本地媒体解码（Know Your Friends，2026-10-10 新增）

- `Pillow 12.0.0` 用于图像缩放，MIT-CMU，保留发行包中的许可。
- `opencv-python 4.13.0.90` 是既有锁定依赖，外部聊天导入使用其 `cv2` 接口读取录屏和提取证据画面。发行元数据声明 Apache 2.0；实际发行包中，OpenCV 二进制采用 Apache-2.0，Python 包装器的 `LICENSE.txt` 为 MIT。须保留 `opencv_python-4.13.0.90.dist-info/LICENSE.txt` 与 `opencv_python-4.13.0.90.dist-info/LICENSE-3RD-PARTY.txt`；后者还包含二进制所用其他组件的条款，不能全部用包装器或本项目许可证替代。上述文件名与内容已核对本机安装发行包，版本记录见 [python-requirements.lock.txt](python-requirements.lock.txt)。 其中 `opencv_videoio_ffmpeg4130_64.dll` 有独立 FFmpeg／编解码依赖；官方对应源归档、脚本与许可闭包按精确提交和哈希随包提供，见[FFmpeg 与 OpenCV 插件交付](docs/ffmpeg-source-distribution.md#opencv-的独立-ffmpeg-插件)。
- [`silk-python 0.2.8`](https://pypi.org/project/silk-python/0.2.8/) 提供 `pysilk` 流式音频解码接口；发行元数据声明 BSD，包装项目来源为 [synodriver/pysilk](https://github.com/synodriver/pysilk)。其微信 SILK 适配来源为 `kn007/silk-v3-decoder`，上游子模块固定提交 `507be6bca8ce1fb977a061481f1d79e8c610e309`；MIT 原文保留在 [silk-v3-decoder-MIT.txt](licenses/silk-v3-decoder-MIT.txt)，Skype SILK 源码中的版权和 BSD 条款保留在 [Skype-SILK-BSD.txt](licenses/Skype-SILK-BSD.txt)。包装发行包未附独立 LICENSE，发布时仍须核对上游完整授权与二进制组成，不以本项目 Apache-2.0 覆盖第三方代码。
- [`imageio-ffmpeg 0.6.0`](https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0) 是 BSD-2-Clause 包装器，条款见 [imageio-ffmpeg-BSD.txt](licenses/imageio-ffmpeg-BSD.txt)。免费发行包不复制其 wheel 内的 Gyan GPL 程序；改用本项目从官方签名 FFmpeg 7.1 源码编译的独立 LGPL 2.1-or-later 程序。完整条款见 [FFmpeg-LGPL-2.1.txt](licenses/FFmpeg-LGPL-2.1.txt)，固定源码、二进制与构建脚本及运行库许可随包交付，准确身份与文件位置见 [FFmpeg 交付说明](docs/ffmpeg-source-distribution.md)。包装器的安装副本仅调整程序映射并附修改记录。源码开发者自行安装的原 wheel 仍按其实际程序与各自许可使用，不等同于本项目发行包。

`bridge/media_keys.py` 的限定配置指针读取方法适配自 [julibeian/wechat-local-exporter](https://github.com/julibeian/wechat-local-exporter) 1.6.0 的 `src/wechat_exporter/media.py`；表情缓存的完整 UIN 读取与密钥派生规则参考其 [1.6.1 源码](https://github.com/julibeian/wechat-local-exporter/blob/50b79147a16f3d3510c07bc54da1bba5387152a9/src/wechat_exporter/media.py)。原作者 Copyright (c) 2026 julibeian，MIT 原文保留在 [wechat-exporter-MIT.txt](licenses/wechat-exporter-MIT.txt)。适配增加当前账号和进程归属验证、读取范围限制及内存缓存；运行时不依赖旧导出工程。

本项目媒体适配、缓存、界面和 AI 上下文新增代码仍采用 Apache-2.0，并保留上述第三方许可。真实聊天和私人媒体不随源码分发。

## 量表与题库

自我剖析使用的 IPIP 题项由[上游许可说明](https://ipip.ori.org/newPermission.htm)置于公有领域，可用于商业或非商业目的。本项目的译文、检索提示与基于聊天的适配不代表上游验证了这些使用方式。8values 的固定来源、MIT 许可、翻译和计分适配见下文。

ECR-RS 官方页面允许翻译，但本次未确认将其题目随软件转载、再许可或商业分发的授权；首版暂不交付该问卷分支。不可将本项目 Apache-2.0 许可覆盖到授权未闭环的题库。具体来源与复核依据见[量表来源与许可](docs/scale-sources-and-permissions.md)。

## README 演示头像

README 两张演示图中的头像来自 Lisa Wischofsky 的 [Adventurer](https://www.dicebear.com/styles/adventurer/) 插画，采用 [Creative Commons Attribution 4.0 International](https://creativecommons.org/licenses/by/4.0/)（CC BY 4.0）。演示通过 DiceBear 组合角色、调整配色，并在应用截图中缩放展示。人物、对白及分析数值为虚构演示；头像素材不适用本项目代码的 Apache-2.0 许可，继续分发时须保留上述署名和许可说明。

## ONNX Runtime 的 MIT 许可证

`onnxruntime-node` 与 `onnxruntime-common` 的 npm 包内未见独立 LICENSE 文件，故保留其上游 MIT 全文：

```text
MIT License

Copyright (c) Microsoft Corporation

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Skill Package Metadata Parser

PyYAML is used only for safe parsing of skill metadata. It is distributed under
the MIT license. Python-tag constructors, YAML anchors and aliases are rejected.
Imported skill package licenses are retained with each fixed package version.

## 8values political questionnaire

The self-analysis political timeline uses 54 questions affecting the economic or civil-liberty
axes from [8values](https://github.com/8values/8values.github.io), revision
`3971ed8bc0c3dda18f9b001013327f382343b74f`. Original English items, numbering and weights are
retained in `plugins/kyf.self-analysis/backend/8values.json`. Chinese translations and search
terms were added by this project. Scores with missing evidence use the documented coverage
threshold and answered-weight normalization; no diplomacy/society score or ideology match
from 8values is generated.

Copyright (c) 2020 8values. Distributed under the MIT License; the complete notice is retained
in [licenses/8values-LICENSE.txt](licenses/8values-LICENSE.txt).
