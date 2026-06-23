from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .autonomous_runtime import AutonomousComputerRuntime
from .autonomous_terminal_runtime import AutonomousTerminalRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the computer use agent MVP")
    parser.add_argument("task", help="自然语言任务")
    parser.add_argument(
        "--mode",
        choices=("autonomous", "autonomous-terminal"),
        default="autonomous",
        help=(
            "默认 autonomous 使用模型逐步决策并开放 terminal+GUI 工具；"
            "autonomous-terminal 仅开放 run_command"
        ),
    )
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
    parser.add_argument(
        "--max-steps",
        type=int,
        default=50,
        help="最大自主执行步数，默认 50",
    )
    parser.add_argument(
        "--step-timeout",
        type=int,
        default=180,
        help="自主模式下单条命令/工具超时时间，默认 180 秒",
    )
    parser.add_argument(
        "--model-config",
        default="config/models.local.json",
        help="自主模式读取的模型配置文件，默认 config/models.local.json",
    )
    parser.add_argument(
        "--model-role",
        default="mainAgent",
        help="自主模式使用的模型角色，默认 mainAgent",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="自主模式下不实时打印 agent 决策、命令和命令输出摘要",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    runtime: AutonomousComputerRuntime | AutonomousTerminalRuntime
    try:
        if args.mode == "autonomous-terminal":
            runtime = AutonomousTerminalRuntime(
                workspace=Path(args.workspace).resolve(),
                runs_root=Path(args.runs_root).resolve(),
                max_steps=args.max_steps,
                step_timeout_seconds=args.step_timeout,
                model_config_path=Path(args.model_config).resolve(),
                model_role=args.model_role,
                progress_callback=None if args.quiet else _print_progress,
            )
        else:
            runtime = AutonomousComputerRuntime(
                workspace=Path(args.workspace).resolve(),
                runs_root=Path(args.runs_root).resolve(),
                max_steps=args.max_steps,
                step_timeout_seconds=args.step_timeout,
                model_config_path=Path(args.model_config).resolve(),
                model_role=args.model_role,
                progress_callback=None if args.quiet else _print_progress,
            )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"failed to initialize runtime: {exc}", file=sys.stderr)
        return 2

    state = runtime.run(args.task)
    print(f"run_id={state.run.run_id}")
    print(f"status={state.run.status}")
    print(f"reason={state.run.terminated_reason}")
    return 0 if state.run.status == "success" else 1


def _print_progress(message: str) -> None:
    print(message, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
