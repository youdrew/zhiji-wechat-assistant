# FFmpeg 二进制与对应源码交付

核查日期：2026-11-02（Asia/Tokyo）。免费版采用从官方 FFmpeg 7.1 源码编译的独立 LGPL 可执行程序，保留媒体解码、抽帧与音频转换。下列闭包已在本机构建和合成媒体上验证；最终安装包中的文件一致性与安装验收仍须另行核对。

## 二进制与来源

| 项目 | 固定内容 |
| --- | --- |
| Python 包装器 | `imageio-ffmpeg 0.6.0`，BSD-2-Clause；[官方源码](https://github.com/imageio/imageio-ffmpeg/tree/v0.6.0)与[完整条款](../licenses/imageio-ffmpeg-BSD.txt) |
| 本项目编译程序 | FFmpeg `7.1`，20,995,072 字节 |
| 二进制 SHA-256 | `9dac1399ec43497e2560e067cdbe13f85134df2d75069de9fab7c19e5516a581` |
| 程序报告许可 | LGPL 2.1 或更高版本；[完整条款](../licenses/FFmpeg-LGPL-2.1.txt) |
| 官方源码归档 | [ffmpeg-7.1.tar.xz](https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz)，11,011,364 字节 |
| 源码 SHA-256 | `40973d44970dbc83ef302b0609f2e74982be2d85916dd2ee7472d30678a7abe6` |
| 源码真实性 | [官方分离签名](https://ffmpeg.org/releases/ffmpeg-7.1.tar.xz.asc)已用[官方公钥](https://ffmpeg.org/ffmpeg-devel.asc)验证；签名指纹 `FCF986EA15E6E293A5644F10B4322F04D67658D8` |
| 构建工具 | [w64devkit 2.11.0](https://github.com/skeeto/w64devkit/releases/tag/v2.11.0)，GCC 16.2.0、GNU Make 4.4.1 |
| 工具链归档 SHA-256 | `54c2bb9962e8a30c8270e34c5efc3d5fee579f53087fa7d435b95fbba2af7b2b`，与上游不可变发布的摘要一致 |

实际清单与每个文件的字节数、哈希固定在 [`scripts/ffmpeg-runtime.json`](../scripts/ffmpeg-runtime.json)。

## 构建边界

源码归档未经修改。随附 `build-ffmpeg.sh` 使用全新源目录构建，关闭 GPL、nonfree、version3、外部库自动探测、网络协议、硬件加速、采集设备、postproc、ffplay 和 ffprobe；不静态引入 Gyan 构建的 x264、x265、GnuTLS 等外部库。保留适用的内置 CPU 编解码器、封装格式、过滤器及本地 file／pipe 协议。H.264／HEVC 解码、MJPEG 与 PCM 输出、scale／crop 均已核对。

编译在源目录中使用相对路径，中立安装前缀为 `/usr`，关闭调试符号并 strip。实际 `-version`、`-L` 与 `-buildconf` 输出随包保存；二进制的 UTF-8／UTF-16 本机目录、用户名扫描没有命中，导入仅包含 Windows 的 bcrypt、KERNEL32、msvcrt、SHELL32 系统 DLL。

## 随包交付

- 可执行程序位于 `runtime/python/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-zhiji-lgpl.exe`。构建时不复制 wheel 内的旧 Gyan GPL 程序。
- 包装器的安装副本只调整 Windows x64 可执行程序映射，保留 BSD 许可，附修改说明并重建文件记录；开发机安装的 Python 包不被改写。
- `runtime/ffmpeg/` 保存精确源码归档、分离签名、公钥、构建脚本、构建说明、版本／配置／许可输出及文件清单。
- 同目录保留 FFmpeg LGPL 与源码许可说明、MinGW-w64 运行库声明，以及 GCC Runtime Library Exception 和对应 GPLv3 文本。编译工具链本身不随应用分发；构建说明给出其确切版本、下载地址与哈希。

用户可以从同一安装包取得对应 FFmpeg 源码与构建脚本，不需要依赖未来的源码索取承诺。项目 Apache-2.0 不替代 FFmpeg、Python 包装器或工具链运行库各自的条款。

## OpenCV 的独立 FFmpeg 插件

录屏导入还使用 `opencv-python 4.13.0.90` 的 `opencv_videoio_ffmpeg4130_64.dll`，这是另一条依赖，不能用上面的 FFmpeg 7.1 源码替代它的对应来源。已核对的 DLL 为 28,578,304 字节，SHA-256 `fcc614672159094a35815b7ea2819a648cbf86c30d90c57e8c6ba1432f594548`。

其官方二进制提交为 [`d82ad9a54a7b42a1648a9cae8fed5c2f20ea396c`](https://github.com/opencv/opencv_3rdparty/tree/d82ad9a54a7b42a1648a9cae8fed5c2f20ea396c)，对应源码由官方 source 提交 [`62bd53c24feadd92af0146462317fcd4d64fd842`](https://github.com/opencv/opencv_3rdparty/tree/62bd53c24feadd92af0146462317fcd4d64fd842)提供。交付闭包保留 FFmpeg n4.4.6、AOM v3.13.1、libvpx v1.15.2、OpenH264 API headers v1.8.0 的精确归档、OpenCV 插件源码与构建脚本、原始 readme 和许可文件；每项固定大小、SHA-256 与 Git blob 身份。

这些文件随 OpenCV 插件的源码／许可清单交付，原 wheel 的 `LICENSE.txt`、`LICENSE-3RD-PARTY.txt` 和 DLL 均保留。二者分别核对各自的清单，不能将 Python 包装器、OpenCV 主项目、独立 CLI 和视频插件的许可统一写成 Apache-2.0。

## 已验证与剩余范围

合成验证覆盖 MPEG-4 视频编码、按时间定位并缩放取 JPEG、imageio 风格原始帧输出、48 kHz 双声道转 16 kHz 单声道 WAV，以及 H.264／HEVC 裁剪和解帧。测试素材全部合成，未使用真实聊天或媒体。

这些检查验证当前程序和来源闭包；不表示全部媒体格式、长录屏性能或最终安装版已经验收。发布前仍需核对安装包内 `manifest.json`、可执行程序及源码文件的哈希，并通过实际应用的媒体读取与导入流程。
