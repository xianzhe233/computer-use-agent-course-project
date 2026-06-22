# Computer Use Agent

本仓库当前实现 `tasks/prd-mvp-1-terminal-agent.md` 与 `tasks/prd-mvp-2-gui-basic-actions.md` 的 MVP 能力：除 terminal 工具外，已支持基础截图、GUI 动作与 GUI demo 任务。

工具层默认优先使用 vendored Windows-Use 风格 backend（PowerShell/UIA/ImageGrab），并保留 PyAutoGUI/ImageGrab fallback 以降低本机兼容风险。

## 开发

```bash
uv sync
uv run pytest
```

## 运行终端 MVP

```bash
uv run python -m computer_use_agent.cli "列出当前目录文件"
```

## 运行 GUI MVP

```bash
uv run python -m computer_use_agent.cli "打开记事本并输入一行文字"
```

当前内置 GUI demo 会执行：截图 -> 启动记事本 -> 显式等待 -> 输入 demo 文本 -> 再次截图。

## 实际 smoke 测试每个工具

```bash
uv run python -m computer_use_agent.tool_smoke --yes
```

该命令会实际调用 `run_command`、`wait`、`take_screenshot`、`click`、`type_text`、`hotkey`、`drag`。GUI 部分会通过显式路径启动 `%WINDIR%\System32\notepad.exe`，在窗口内点击/输入/快捷键/拖拽，并默认强制关闭该 Notepad 进程。报告与截图保存到 `runs/tool_smoke_*/`。

如只测试非 GUI 工具：

```bash
uv run python -m computer_use_agent.tool_smoke --no-gui --yes
```

运行产物会落到 `runs/<run_id>/`，包含：

- `trace.jsonl`
- `summary.json`
- `command_logs/`
- `screenshots/`
