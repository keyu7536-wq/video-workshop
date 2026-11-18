# 视频工坊配置说明

## 应用配置

把根目录 `app.example.json` 复制为 `app.local.json`，然后修改需要的字段：

```json
{
  "name": "视频工坊",
  "default_voice": "zh-CN-YunyangNeural",
  "default_rate": "+0%",
  "port": 8780
}
```

| 字段 | 含义 |
| --- | --- |
| `name` | 界面显示名称，1–40 个字符 |
| `default_voice` | 新任务默认音色；应使用程序支持的音色标识 |
| `default_rate` | 新任务默认语速；如 `+0%`、`+10%`、`+25%`、`+50%` 或 `auto` |
| `port` | 本机工作台端口，1024–65535 |

此文件不接受 API Key 等额外字段。`VIDEO_WORKSHOP_CONFIG` 环境变量可指向其他配置 JSON；不设置时读取根目录 `app.local.json`。应用配置用于初始默认值，浏览器已保存设置或已打开任务的实际设置可能覆盖默认值。打开已有任务时会恢复该任务的音色、语速与画面设置。

修改配置后重新启动工作台。已有旧服务占用端口时，先正常关闭对应服务，或选择另一个空闲端口；启动器会检查协议版本，不会把新版页面静默连接到不兼容的旧服务。

可直接运行服务，关闭自动打开浏览器，或显式选择端口：

```bash
python studio_server.py --no-browser
python studio_server.py --port 8781
```

## 语言模型

界面的“AI 与配音设置”提供两种模式：

| 模式 | 需要准备 |
| --- | --- |
| 本机 Codex | 可在命令行找到的 Codex CLI，以及当前用户自己的有效登录 |
| 兼容模型 API | Base URL、模型名称和 API Key；模型必须支持图片理解与所需结构化响应 |

API 路径使用兼容 Chat Completions 的接口。Base URL 可以是服务根路径，也可以以 `/chat/completions` 结尾。远程服务要求 HTTPS，本机服务允许 HTTP。使用界面的连接与图片理解测试检查实际服务能力；仅仅地址格式兼容并不保证模型能力兼容。

界面输入的 API Key 仅用于当前会话，通过子进程环境传递，不写入任务配置或浏览器持久化设置。命令行部署也可使用以下环境变量：

| 变量 | 用途 |
| --- | --- |
| `PAPER_VIDEO_API_BASE` | API 服务地址的兼容变量 |
| `PAPER_VIDEO_MODEL` | API 模型名称的兼容变量 |
| `PAPER_VIDEO_API_KEY` | API Key，优先于 `OPENAI_API_KEY` |
| `OPENAI_API_KEY` | 未设置专用 Key 时使用的兼容变量 |

界面明确传入的地址和模型优先于环境变量的默认值。不要把真实凭据加入 `app.local.json`、示例文件或公开 Issue。日志和提示词可能含上传材料，即使没有 Key，也不宜直接公开。

## 配音与画面

Edge 音色使用在线语音服务。界面中的云扬、云希、云健、云哲、云夏及其他可选音色均应按实际服务身份理解；它们不是其他平台的官方配音承诺。固定语速不按段落自动变换，`auto` 模式则会根据句子中的数字和术语分段调整节奏。普通录音缓存由文字、音色和语速共同决定。

可选画幅包括 1080×1920、1920×1080、1080×1080、720×1280 与自定义尺寸。自定义宽高须为 320–3840 的偶数，总像素不超过 8,294,400，长宽比例不超过 3:1。

- **完整显示（contain）：** 保留原素材边缘，可能出现留白。
- **居中铺满（cover）：** 放大并居中裁切，可能裁掉边缘主体；不会自动追踪机器人或人物。
- 图表和被标为需保护的内容保留完整上下文，避免裁掉坐标轴、图例和比较口径。

画面来源说明、章节标题与少文字选项按任务保存。来源与审核记录始终可保存在随片文件；关闭画面标注不等于删除研究依据。

## FFmpeg 与字体

程序优先查找系统 `ffmpeg`，找不到时尝试 `imageio-ffmpeg` 的可执行文件。实际版本必须含 `ass` 字幕滤镜，可自行检查：

```bash
ffmpeg -hide_banner -filters
```

输出中应有 `ass`。缺少时请安装带 `libass` 的完整 FFmpeg。中文字体默认查找 Windows 的微软雅黑、macOS 的苹方/黑体，以及 Linux 常见路径下的 Noto Sans CJK。字体不会随源码包分发。

Linux 可安装发行版提供的 FFmpeg、Python venv 支持和 Noto CJK 字体，例如 Debian/Ubuntu：

```bash
sudo apt install ffmpeg fonts-noto-cjk python3-venv
```

独立渲染器和离线检查允许指定字体文件：

```bash
python scripts/smoke_render.py --font /path/to/chinese-font.ttf
python make_video.py --project /path/to/project.json --font /path/to/chinese-font.ttf
```

这些跨平台命令是安装方式说明；Linux/macOS 尚未完成与 Windows 相同范围的项目验收。

## 预览有画面但没有声音

首先看播放器是否静音或音量为零，再核对浏览器标签页和系统输出设备。如果在 VS Code 内置浏览器里播放，可能是音频编码兼容性：它支持 H.264 画面，但不支持常见 MP4 使用的 AAC 音频。参考 [VS Code 官方媒体格式说明](https://code.visualstudio.com/api/extension-guides/webview#supported-media-formats)。

将当前任务地址复制到独立 Edge 或 Chrome 的地址栏打开，或下载成片后用本地播放器播放。工作台会在检测到相关音频能力缺失时提供提示和当前任务地址；检测到浏览器不支持某种编码，并不代表文件没有音轨。

`output/verification.json` 中的非静音检查用于验证文件音轨。技术检测通过不能保证系统扬声器已出声，也不能代替人工听审。若下载后的文件仍无声，应保留任务日志和检查结果继续定位，无需先删除任务或缓存。

## 可选本地配音

基础程序不包含语音模型权重。当前自动安装脚本针对 Windows、Python 3.12 与 NVIDIA CUDA 环境，需先准备 `uv`：

```powershell
powershell -File scripts/install_voice.ps1
```

脚本创建 `.venv-voice`、安装独立语音依赖、下载模型并检测 CUDA。具体依赖和固定模型版本见 `requirements-voice.txt`、`scripts/voice-constraints.txt` 与 `scripts/voice-manifests/`。下载占用较多空间；普通 CPU 环境不能视为已满足该安装器要求。

本地语音工作进程可通过 `VIDEO_WORKSHOP_VOICE_PYTHON` 指定 Python 可执行文件；未设置时，程序查找 `.venv-voice` 中相应平台的 `Scripts/python.exe` 或 `bin/python`。Linux/macOS 的语音环境需手动准备，自动发现可执行文件不代表模型、硬件或语音依赖已经验证可用。

本地 Qwen 音色使用其独立生成配置，不使用 Edge 语速选项。不要为了重试而默默更换说话人或情绪配置；生成后仍需检查实际内容与听感。

## 数据与源码交付

本地配置、虚拟环境、模型、任务、缓存和输出不应进入源码发布包。使用 `scripts/build_release.py --check` 检查允许发布的文件，再构建源码 ZIP。程序的任务 ZIP 用于继续编辑具体作品，可能包含私人原稿、下载素材、来源说明和日志，与开源源码包用途不同。

如需报告错误，请提供版本、操作步骤、脱敏后的错误信息和可公开复现的素材。不要提交账户凭据或未授权的论文、视频、录音。
