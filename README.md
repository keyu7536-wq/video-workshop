# 视频工坊

把文案、文章、论文和已有素材做成带中文配音与字幕的解说视频。提供本地浏览器界面，文稿、分镜、音色和画幅都可以修改。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Offline checks](https://github.com/keyu7536-wq/video-workshop/actions/workflows/tests.yml/badge.svg)](https://github.com/keyu7536-wq/video-workshop/actions/workflows/tests.yml)

当前版本 **0.6.1** · [下载源码包](https://github.com/keyu7536-wq/video-workshop/releases) · [更新记录](CHANGELOG.md)

## 主要功能

- 输入文案、文章链接、论文 PDF、图片或视频，生成可编辑的文稿与分镜。
- 查找论文的作者项目及相关素材，保留来源、阅读笔记和未获取的材料记录。
- 配置配音、语速及横竖屏；按实际录音长度安排镜头和字幕。
- 导出视频、音频、字幕及可继续编辑的任务包。

## 安装与打开

需要 **Python 3.11+**，首次安装需要联网。下载源码包并解压，或克隆仓库：

```bash
git clone https://github.com/keyu7536-wq/video-workshop.git
cd video-workshop
```

- **Windows：** 双击 `start.cmd`。
- **Linux / macOS：** 在项目目录运行 `bash start.sh`。

启动脚本会创建环境并安装基础依赖。默认地址：**http://127.0.0.1:8780**，建议用独立的 Chrome 或 Edge 打开。

模型可选已登录的 **Codex CLI**，或支持图片理解与结构化输出的 **模型 API**。默认 Edge 配音需要联网。FFmpeg、中文字体和可选本地配音的配置见 [配置与排错](docs/configuration.md)。

## 三步使用

1. **添加资料**：上传文件或粘贴文字、链接，设置声音与画幅。
2. **文稿与分镜**：检查并修改讲稿、素材、裁切和视频片段，再生成视频。
3. **预览与导出**：播放成片、查看复核意见，按需修改或导出。

默认保留人工编辑步骤，也可选择自动生成和最多三次自动返修。成片位于 `jobs/<任务编号>/output/video.mp4`；文稿、来源与复核记录保存在同一任务目录。

## 文档与开发

| 内容 | 入口 |
| --- | --- |
| 输入、制作步骤与输出文件 | [制作流程](docs/production-workflow.md) |
| 模型、声音、画幅与常见问题 | [配置与排错](docs/configuration.md) |
| 作者项目、视频与补充材料 | [素材来源](docs/media-sources.md) |
| 模块职责、调用关系与目录 | [代码导航](docs/architecture.md) |
| 测试、打包与参与开发 | [验证与发布](docs/testing.md) · [贡献指南](CONTRIBUTING.md) |

前端位于 `studio/`，Python 后端按职责拆分在根目录；`tests/`、`scripts/`、`skills/` 分别存放测试、开发工具和制作规范。

本项目面向本机使用，利用已有素材和程序绘制的示意画面制作视频。模型复核不能代替事实核查、完整观看与试听。公开源码不包含个人任务、论文、素材、账号或模型权重。

源码采用 [MIT 许可证](LICENSE)，第三方素材与依赖见 [许可说明](THIRD_PARTY_NOTICES.md)。
