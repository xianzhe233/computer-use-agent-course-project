from __future__ import annotations

import argparse
from pathlib import Path

from .runtime import TerminalRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the terminal-only computer use agent MVP")
    parser.add_argument("task", help="自然语言终端任务")
    parser.add_argument(
        "--workspace",
        default=".",
        help="命令执行工作目录，默认当前目录",
    )
    parser.add_argument(
        "--runs-root",
        default="runs",
        help="运行产物根目录，默认 runs/",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runtime = TerminalRuntime(
        workspace=Path(args.workspace).resolve(),
        runs_root=Path(args.runs_root).resolve(),
    )
    state = runtime.run(args.task)
    print(f"run_id={state.run.run_id}")
    print(f"status={state.run.status}")
    print(f"reason={state.run.terminated_reason}")
    return 0 if state.run.status == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
