import json
from pathlib import Path
from typing import Sequence

from computer_use_agent.autonomous_terminal_runtime import AutonomousTerminalRuntime
from computer_use_agent.runtime_state import RuntimeState
from computer_use_agent.computer_agent import TerminalAgentDecision
from computer_use_agent.tools.run_command import CommandResult, PowerShellBackend


class SequenceBackend(PowerShellBackend):
    def __init__(self, results: list[CommandResult]) -> None:
        super().__init__(executable="fake-powershell")
        self.results = results
        self.calls: list[tuple[str, int, Path | None]] = []

    def execute(self, command: str, timeout: int = 10, cwd: Path | None = None) -> CommandResult:
        self.calls.append((command, timeout, cwd))
        return self.results.pop(0)


class ScriptedTerminalAgent:
    def __init__(self, decisions: list[TerminalAgentDecision]) -> None:
        self.decisions = decisions
        self.calls: list[tuple[RuntimeState, Path, list[dict[str, object]]]] = []

    def decide(
        self,
        *,
        state: RuntimeState,
        workspace: Path,
        history: Sequence[dict[str, object]],
    ) -> TerminalAgentDecision:
        self.calls.append((state, workspace, list(history)))
        return self.decisions.pop(0)


def test_autonomous_terminal_runtime_executes_command_then_finishes(tmp_path: Path) -> None:
    backend = SequenceBackend(
        [
            CommandResult(
                command="Get-ChildItem",
                stdout="demo.txt\nREADME.md",
                stderr="",
                exit_code=0,
                success=True,
                duration_ms=12,
            )
        ]
    )
    agent = ScriptedTerminalAgent(
        [
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="查看当前目录",
                tool_name="run_command",
                tool_args={"command": "Get-ChildItem"},
                expected_observation="输出目录文件列表",
            ),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已列出当前目录文件",
                supporting_evidence=["command:cmd_0001"],
            ),
        ]
    )
    runtime = AutonomousTerminalRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        command_backend=backend,
        agent=agent,
    )

    state = runtime.run("列出当前目录文件")

    assert state.run.status == "success"
    assert state.run.run_id.startswith("run_")
    assert state.run.terminated_reason == "已列出当前目录文件"
    assert state.control.allowed_tools == ["run_command"]
    assert state.metrics.command_count == 1
    assert state.metrics.screenshot_count == 0
    assert state.observation.latest_command_result_id == "cmd_0001"
    assert backend.calls == [("Get-ChildItem", 180, tmp_path)]

    run_dir = tmp_path / "runs" / state.run.run_id
    assert (run_dir / "command_logs" / "cmd_0001.stdout.log").read_text(encoding="utf-8") == "demo.txt\nREADME.md"
    trace_records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    event_types = [record["event_type"] for record in trace_records]
    assert event_types.count("agent_decision") == 2
    assert "tool_execution" in event_types
    assert event_types[-1] == "finish_request"


def test_autonomous_terminal_runtime_emits_progress_messages(tmp_path: Path) -> None:
    progress_messages: list[str] = []
    backend = SequenceBackend(
        [
            CommandResult(
                command="Get-ChildItem",
                stdout="README.md",
                stderr="",
                exit_code=0,
                success=True,
                duration_ms=12,
            )
        ]
    )
    agent = ScriptedTerminalAgent(
        [
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="查看当前目录",
                tool_name="run_command",
                tool_args={"command": "Get-ChildItem"},
            ),
            TerminalAgentDecision(kind="finish_request", completion_claim="已完成"),
        ]
    )
    runtime = AutonomousTerminalRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        command_backend=backend,
        agent=agent,
        progress_callback=progress_messages.append,
    )

    state = runtime.run("列出当前目录文件")

    assert state.run.status == "success"
    joined_output = "\n".join(progress_messages)
    assert "RUN       : run_" in joined_output
    assert "STEP 1/20 · planning" in joined_output
    assert "Command   :" in joined_output
    assert "PS> Get-ChildItem" in joined_output
    assert "Stdout    :" in joined_output
    assert "DONE      : success" in joined_output


def test_autonomous_terminal_runtime_blocks_gui_tool_requests(tmp_path: Path) -> None:
    backend = SequenceBackend([])
    agent = ScriptedTerminalAgent(
        [
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="错误地尝试点击",
                tool_name="click",
                tool_args={"x": 10, "y": 10},
            )
        ]
    )
    runtime = AutonomousTerminalRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        command_backend=backend,
        agent=agent,
    )

    state = runtime.run("点击按钮")

    assert state.run.status == "failed"
    assert state.errors.last_error_code == "TOOL_NOT_ALLOWED"
    assert state.errors.last_failed_tool == "click"
    assert state.metrics.command_count == 0
    assert backend.calls == []

    run_dir = tmp_path / "runs" / state.run.run_id
    trace_records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    validation_record = next(record for record in trace_records if record["event_type"] == "tool_validation")
    assert validation_record["status"] == "failed"
    assert validation_record["payload"]["error"]["code"] == "TOOL_NOT_ALLOWED"


def test_autonomous_terminal_runtime_allows_recovery_after_command_failure(tmp_path: Path) -> None:
    backend = SequenceBackend(
        [
            CommandResult(
                command="Get-Content missing.txt",
                stdout="",
                stderr="Cannot find path missing.txt",
                exit_code=1,
                success=False,
                duration_ms=10,
            ),
            CommandResult(
                command="Get-ChildItem",
                stdout="README.md",
                stderr="",
                exit_code=0,
                success=True,
                duration_ms=10,
            ),
        ]
    )
    agent = ScriptedTerminalAgent(
        [
            TerminalAgentDecision(
                kind="tool_call",
                tool_name="run_command",
                tool_args={"command": "Get-Content missing.txt"},
            ),
            TerminalAgentDecision(
                kind="tool_call",
                tool_name="run_command",
                tool_args={"command": "Get-ChildItem"},
            ),
            TerminalAgentDecision(kind="finish_request", completion_claim="已恢复并完成目录检查"),
        ]
    )
    runtime = AutonomousTerminalRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        command_backend=backend,
        agent=agent,
        max_consecutive_failures=3,
    )

    state = runtime.run("先读文件，失败后查看目录")

    assert state.run.status == "success"
    assert state.metrics.command_count == 2
    assert state.metrics.consecutive_failures == 0
    assert state.errors.last_error_code == ""
    assert backend.calls == [
        ("Get-Content missing.txt", 180, tmp_path),
        ("Get-ChildItem", 180, tmp_path),
    ]
    assert agent.calls[1][2][0]["success"] is False
    assert "Cannot find path" in str(agent.calls[1][2][0]["stderr"])


def test_autonomous_terminal_runtime_blocks_risky_commands(tmp_path: Path) -> None:
    backend = SequenceBackend([])
    agent = ScriptedTerminalAgent(
        [
            TerminalAgentDecision(
                kind="tool_call",
                tool_name="run_command",
                tool_args={"command": "Remove-Item -Recurse ."},
            )
        ]
    )
    runtime = AutonomousTerminalRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        command_backend=backend,
        agent=agent,
    )

    state = runtime.run("清理目录")

    assert state.run.status == "failed"
    assert state.errors.last_error_code == "RISK_BLOCKED"
    assert state.metrics.command_count == 0
    assert backend.calls == []
