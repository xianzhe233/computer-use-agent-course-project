# Computer Use Agent

本仓库当前实现 `tasks/prd-mvp-1-terminal-agent.md` 的 terminal-only MVP。

## 开发

```bash
uv sync
uv run pytest
```

## 运行终端 MVP

```bash
uv run python -m computer_use_agent.cli "列出当前目录文件"
```

运行产物会落到 `runs/<run_id>/`，包含：

- `trace.jsonl`
- `summary.json`
- `command_logs/`
