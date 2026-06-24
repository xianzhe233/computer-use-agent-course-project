# OSWorld 本机测试用例

本目录用于让队友在自己的 Windows 机器上直接运行 10 个 OSWorld 风格测试：Python 脚本会把指定用例的初始文件恢复到本机真实路径，按需打开对应应用，然后启动本项目的 agent。测试完成后由人工打开结果文件和 reference/gold 文件进行核对。

## 前置条件

在仓库根目录完成依赖和配置：

```bash
uv sync
```

并确保 `config/models.local.json` 已配置好本机可用模型。Office 类任务需要本机安装 Microsoft Word、Excel、PowerPoint；PDF 表单任务还需要本机可用的 PDF 查看/编辑工具或浏览器 PDF 支持；Scholar 任务需要浏览器和网络访问。

## 查看可运行用例

```bash
uv run python OSWorld_examples/run_case.py --list-cases
```

当前 10 个 case：

| Case ID | 应用 | 任务 |
|---|---|---|
| `h2o_subscript` | Word | 把 H2O 中的 2 改为下标 |
| `excel_freeze_headers` | Excel | 冻结 A1:B1 表头 |
| `ppt_blue_background` | PowerPoint | 所有幻灯片背景改蓝色 |
| `excel_sort_amounts` | Excel | 按金额升序排序记录 |
| `excel_highlight_weekends` | Excel | 周末日期单元格标红 |
| `ppt_strikethrough_todo` | PowerPoint | 第 5 页前两行加删除线 |
| `word_export_pdf` | Word | 导出同名 PDF |
| `ppt_columbus_cover` | PowerPoint | Columbus 图片铺满首页并居中 |
| `multiapp_performance_pdfs` | Excel + PDF | 从 Excel 数据填写 7 份 PDF 表单 |
| `scholar_yann_lecun_researcher` | Chrome/Edge + Excel | 从 Google Scholar 追加 Yann LeCun 研究者记录 |

## 只初始化，不启动 agent

用于确认文件会放到哪里：

```bash
uv run python OSWorld_examples/run_case.py --case h2o_subscript --prepare-only --force-close-apps
```

把 `h2o_subscript` 替换成任意 case id 即可。

## 初始化并运行 agent

```bash
uv run python OSWorld_examples/run_case.py --case h2o_subscript --force-close-apps
```

脚本会：

1. 按 case 配置关闭可能冲突的 Office/PDF 应用进程（仅当传入 `--force-close-apps` 时；运行前请保存相关应用中的工作）。
2. 用 `assets/initial/` 中的初始文件覆盖本机真实测试路径。
3. 打开配置中标记的 Word/Excel/PPT/PDF 文件。
4. 调用 `uv run python -m computer_use_agent.cli ...` 启动 agent。
5. agent 结束后打印人工校验提示、reference 文件、expected output 路径和外部 URL。

默认情况下，`run_case.py` 不用 agent 的退出码判定任务对错；它只负责跑完流程，正确性由人工检查。如果你想让脚本透传 agent 退出码，可加：

```bash
--pass-through-agent-exit-code
```

## 示例

H2O Word 任务：

```bash
uv run python OSWorld_examples/run_case.py --case h2o_subscript --force-close-apps
```

Excel 冻结表头任务：

```bash
uv run python OSWorld_examples/run_case.py --case excel_freeze_headers --force-close-apps
```

PDF 表单任务：

```bash
uv run python OSWorld_examples/run_case.py --case multiapp_performance_pdfs --force-close-apps
```

Google Scholar + Excel 任务：

```bash
uv run python OSWorld_examples/run_case.py --case scholar_yann_lecun_researcher --force-close-apps
```

## 人工校验

运行结束后，根据脚本输出的：

- `Prepared files`
- `Reference files`
- `Expected output paths`
- `External targets`
- `Manual check guidance`

逐项打开文件人工检查。没有 gold 文件的任务按脚本输出的人工说明检查。

## 目录说明

- `cases/`：单用例配置，每个 JSON 描述初始文件、参考文件、预期输出、外部目标、任务提示词和运行参数。
- `run_case.py`：按用例初始化本机真实环境，并启动 agent。
- `assets/initial/`：各用例初始文件缓存。
- `assets/reference/`：人工核对用 reference/gold 文件。
- `assets/source_json/`：原 OSWorld 任务 JSON，便于追溯。
- `manifest.json`：从原始表格生成的资源总清单。
- `prepare_osworld_assets.py`：当素材缺失或表格变化时，用于重新生成 `manifest.json` 并补齐下载。

## 重新补齐素材

一般队友不需要运行。只有素材缺失时执行：

```bash
uv run python OSWorld_examples/prepare_osworld_assets.py
```
