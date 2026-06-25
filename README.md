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

## Chainlit 前端

安装依赖：

```bash
uv sync
```

启动前端：

```bash
uv run chainlit run src/computer_use_agent/ui_chainlit.py
```

如果不是通过 uv 管理环境，也可以在安装依赖后运行：

```bash
chainlit run src/computer_use_agent/ui_chainlit.py
```

打开浏览器中的本地地址后，输入任务即可启动 agent。页面会在运行过程中直接解析 `runs/<run_id>/` 下的 `trace.jsonl`、`command_logs/`、`screenshots/` 与最终 `summary.json`，以中文摘要时间线动态展示本次 run 的状态、agent 计划、工具执行结果、examiner 检查和截图；默认不展示原始 payload/trace JSON，也不再显示单独的实时终端黑框。

前端会将每次 agent 任务作为独立子进程启动；点击页面停止按钮时，会终止后台 agent 进程树，避免 agent 在前端停止后继续执行。

试用前端时不建议加 `-w/--watch`，因为 agent 运行时会持续写入 `runs/`，监听模式可能触发 Chainlit reload，干扰长任务执行。

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
