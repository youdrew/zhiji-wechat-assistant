# Windows 原生运行库交付

核查日期：2026-11-02（Asia/Tokyo）。本页记录应用私有 Microsoft Visual C++ 运行库的来源、许可分层和验收方式。它不替代微软原始许可，也不把某个安装包的通过结果推广为所有 Windows 环境均已验收。

## 来源与范围

本机语义检索的 ONNX 原生模块及判断运行时需要 Microsoft Visual C++ v14 release 运行库。发行版将经过核对的 Windows x64 DLL 放入应用自己的相应运行目录，普通用户无需另装 Visual Studio。它不安装系统级 Redistributable、不改写 System32，也不把开发机恰好已有的系统 DLL 当成随包依赖。

| 项目 | 固定内容 |
| --- | --- |
| 产品系列 | Visual C++ 2015–2022 Runtime，Windows x64 release |
| 安装器版本 | `14.44.35211.0` |
| 官方入口 | [VS2022 x64 Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe)；[微软下载说明](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist) |
| 原安装器 SHA-256 | `cc0ff0eb1dc3f5188ae6300faef32bf5beeba4bdd6e8e445a9184072096b713b` |
| 签名 | 原安装器 Authenticode 核对为 Microsoft Corporation、状态 Valid；具体 DLL 也须逐项核对原始字节、版本及签名 |
| 原始许可 | 安装器 `UX/license.rtf`；随源码保留为 [Microsoft 运行库许可](../licenses/Microsoft-Visual-Cpp-Runtime-2015-2022.rtf) |
| 许可 SHA-256 | `8099dc3cf9502c335da829e5c755948a12e3e6de490eb492a99deb673d883d8b`，9,235 字节 |

下载入口可能随微软维护更新；构建以项目固定清单中的版本、来源及 SHA-256 为准，不能因为入口仍可下载就接受不同版本。此处不将 VS2022 的许可自动解释为对后续产品系列的授权。

## 源码构建前准备

在运行 `build:portable` 或 `build:installer` 前，先按[源码运行步骤](getting-started.md#从源码运行)准备项目环境，再用项目 Python 执行下列命令。默认通过 `vswhere` 查找本机已具备相应许可的 VS2022 Community 17.x release CRT，只接受与 [`scripts/msvc-runtime.json`](../scripts/msvc-runtime.json) 固定版本和哈希一致的五个 x64 DLL；也可显式提供从同一官方安装器静态解包的 CRT 目录。

```powershell
.venv\Scripts\python.exe scripts/setup-msvc-runtime.py
# 已有官方静态解包文件时，改用此命令：
.venv\Scripts\python.exe scripts/setup-msvc-runtime.py --source-dir "<官方解包CRT目录>"
```

工具接受原始 `.dll` 文件名或 MSI 中的 `.dll_amd64` 文件名，将 DLL、原始许可和说明固定到被 Git 忽略的 `.local/tools/msvc-runtime/dist/`；不执行 Redistributable、不修改系统目录，也不接受 System32、SysWOW64 或 WinSxS 作为来源。正常使用已安装的 VS2022 CRT，或已经解包的输入目录时，不需要 WiX。

若自行从清单固定的微软安装器提取输入，可使用 WiX 3.14.1 portable 的 `dark.exe` 先执行 `dark.exe -x <Burn输出目录> <固定的VC_redist.x64.exe>`，再对其中的 `vc_runtimeMinimum_x64.msi` 执行 `dark.exe -x <MSI输出目录> -o <输出.wxs> <MSI文件>`。这两步仅静态解包，不运行安装器或 `msiexec`；将提取出的 release CRT 目录传给 `--source-dir`，仍须通过上述固定字节校验。

## 再分发依据与许可分层

[微软再分发文档](https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files)明确支持将运行库 DLL 放入应用本地目录，并说明再分发受适用 Visual Studio 许可约束。[VS2022 REDIST 列表](https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution)允许具备相应有效许可的使用者随自己的应用提供列出的原样 release 文件，也说明了从官方下载入口取得的单独二进制适用的条件；debug runtime 不在应用发行范围内。

[Visual Studio Community 2022 原许可](https://visualstudio.microsoft.com/license-terms/vs2022-ga-community/)的 Distributable Code 部分要求应用提供主要功能，并要求分发者及外部终端用户接受相应保护条款。单独的 Runtime 终端用户许可并不是开发者的再分发授权；只复制一份 EULA 也不能替代适用的开发者权利与文件范围核对。

本项目自有源码继续采用 Apache-2.0；微软 DLL 按其原条款提供，其他组件保留各自许可。安装器单独展示微软运行库条款并要求接受；不将 Apache 源码改为受微软 EULA 约束。包内同时保留完整原始 RTF 和[组件说明](../licenses/Microsoft-Visual-Cpp-Runtime-NOTICE.md)，而非只有一个网页链接或本项目摘要。

微软更推荐可独立维护的系统级 Redistributable 部署。采用应用私有运行库时，运行库的后续安全更新需随应用版本重新交付；不能把应用私有 DLL 描述为已经接入系统级运行库的独立更新机制。

## 验收与证据

- 固定原安装器、每个 DLL、许可和说明文件的字节数、版本、SHA-256；提取和复制不得改写微软二进制。
- 只在隔离构建目录解包所需 release 文件，不执行系统安装；不取 System32 文件代替对应来源。
- 递归核对 ONNX 与判断进程的 PE 导入，并在最终运行目录验证模块实际加载路径；仅在开发机能够 import 不能证明包内闭包完整。
- DLL、许可及来源清单进入正式 staging 与解包清单。安装、重装及卸载的数据保留检查仍独立执行。
- 原隐私报告只证明它对应的具体文件集；加入运行库后，必须将新文件、修改和移除集与新源码提交重新绑定，不能直接沿用旧安装包的验收结论。
