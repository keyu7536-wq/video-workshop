# 参与开发

先看 [使用流程](docs/production-workflow.md)，再按 [架构与代码导航](docs/architecture.md) 找到要修改的模块。前端位于 `studio/`，后端按职责拆成根目录的 Python 模块，安装和发布工具位于 `scripts/`。

## 准备开发环境

从仓库根目录创建并激活虚拟环境，再安装依赖：

```bash
python -m venv .venv
```

Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1`；Linux / macOS 使用 `source .venv/bin/activate`。也可以直接使用虚拟环境里的 Python，无需激活。随后执行：

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m unittest discover -s tests -p "test_*.py"
python scripts/smoke_render.py
python scripts/build_release.py --check
```

## 修改与验证

修改应说明具体输入、原来行为、改后行为与验证结果。涉及时间线、字幕、裁切或配音时，检查实际编码输出，不只检查计划 JSON。论文事实要能回到来源，无法访问的材料应记录缺口。

新增模块、测试或公开文档时，同时检查 `scripts/build_release.py` 的发布清单；新增 `docs/` 文件还需加入 `.gitignore` 中的文档白名单。CI 和源码 ZIP 应覆盖同一套可发布文件。版本号在 `app_config.py` 中维护，打包工具默认读取该版本。

测试使用自造文本、图案和短音频，不提交个人任务、研究论文、账号配置、日志、下载模型或未授权媒体。接口密钥只在本地配置，示例使用明显无效占位值。新依赖同时更新依赖文件及第三方说明。

提交内容应拥有可贡献的权利，并同意以项目 MIT 许可发布。请保持独立模型适配、证据记录和渲染模块之间的边界；不要把某次样片的音色、人物、论文结论写成全局默认。

## 提交与 Pull Request

从最新 `main` 建立独立分支，一次解决一个问题。提交标题可使用 `fix:`、`feat:`、`docs:` 或 `test:`，后接具体行为；不要把格式调整、私人样片和无关重构混入修复。

PR 说明应让没有看过讨论的人理解问题、改后行为、验证方式和实际限制。复现涉及网页或视频时，提供可公开的最小示例，并写明联网、模型调用或人工观看的范围。完整验证说明见 [验证与发布](docs/testing.md)。
