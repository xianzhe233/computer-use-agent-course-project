import json
from pathlib import Path

from computer_use_agent.runtime import TerminalRuntime
from computer_use_agent.tools.run_command import CommandResult, PowerShellBackend


class SequenceBackend(PowerShellBackend):
    def __init__(self, results: list[CommandResult]) -> None:
        super().__init__(executable="fake-powershell")
        self.results = results
        self.calls: list[str] = []

    def execute(self, command: str, timeout: int = 10, cwd: Path | None = None) -> CommandResult:
        self.calls.append(command)
        return self.results.pop(0)


def test_runtime_persists_command_logs_and_trace(tmp_path: Path) -> None:
    backend = SequenceBackend(
        [
            CommandResult(
                command="Get-ChildItem",
                stdout="README.md\npyproject.toml",
                stderr="",
                exit_code=0,
                success=True,
                duration_ms=18,
            )
        ]
    )
    runtime = TerminalRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        command_backend=backend,
    )

    state = runtime.run("列出当前目录文件")

    assert state.run.status == "success"
    run_dir = tmp_path / "runs" / state.run.run_id
    assert run_dir.exists()
    assert (run_dir / "trace.jsonl").exists()
    assert (run_dir / "summary.json").exists()
    assert (run_dir / "command_logs" / "cmd_0001.stdout.log").read_text(encoding="utf-8") == "README.md\npyproject.toml"

    trace_lines = (run_dir / "trace.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(trace_lines) >= 3
    last_trace = json.loads(trace_lines[-1])
    assert last_trace["event_type"] == "finish_request"


def test_runtime_marks_failed_when_task_not_supported(tmp_path: Path) -> None:
    runtime = TerminalRuntime(workspace=tmp_path, runs_root=tmp_path / "runs")

    state = runtime.run("做一个这里暂不支持的任务")

    assert state.run.status == "failed"
    assert state.errors.last_error_code == "TASK_NOT_SUPPORTED"
