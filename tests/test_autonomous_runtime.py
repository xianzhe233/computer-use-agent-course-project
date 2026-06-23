import json
from pathlib import Path
from typing import Sequence

from computer_use_agent.autonomous_runtime import AutonomousComputerRuntime
from computer_use_agent.runtime_state import RuntimeState
from computer_use_agent.terminal_agent import TerminalAgentDecision
from computer_use_agent.tools.element_location import ElementLocationCandidate
from computer_use_agent.tools.run_command import CommandResult, PowerShellBackend


class ScriptedComputerAgent:
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


class FakeScreenshotBackend:
    def __init__(self, *, width: int = 1280, height: int = 720) -> None:
        self.width = width
        self.height = height
        self.calls: list[Path] = []

    def capture(self, path: Path, target: str = "screen") -> tuple[int, int]:
        self.calls.append(path)
        path.write_bytes(b"fake-png")
        return self.width, self.height


class FakeGuiBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self.calls.append(("click", (x, y), {"button": button, "clicks": clicks}))

    def type_text(
        self,
        text: str,
        *,
        x: int | None = None,
        y: int | None = None,
        clear: bool = False,
        caret_position: str = "idle",
        press_enter: bool = False,
    ) -> None:
        self.calls.append(
            (
                "type_text",
                (text,),
                {
                    "x": x,
                    "y": y,
                    "clear": clear,
                    "caret_position": caret_position,
                    "press_enter": press_enter,
                },
            )
        )

    def hotkey(self, shortcut: str) -> None:
        self.calls.append(("hotkey", (shortcut,), {}))

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.calls.append(("drag", (x1, y1, x2, y2), {}))


class FakeElementLocatorBackend:
    def __init__(self, candidates: list[ElementLocationCandidate]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str, Path, str]] = []

    def locate(self, *, query: str, screenshot_path: Path, screenshot_id: str = "") -> list[ElementLocationCandidate]:
        self.calls.append((query, screenshot_path, screenshot_id))
        return self.candidates


class SequenceBackend(PowerShellBackend):
    def __init__(self, results: list[CommandResult]) -> None:
        super().__init__(executable="fake-powershell")
        self.results = results
        self.calls: list[tuple[str, int, Path | None]] = []

    def execute(self, command: str, timeout: int = 10, cwd: Path | None = None) -> CommandResult:
        self.calls.append((command, timeout, cwd))
        return self.results.pop(0)


def test_autonomous_computer_runtime_uses_gui_tools_then_finishes(tmp_path: Path) -> None:
    screenshot_backend = FakeScreenshotBackend()
    gui_backend = FakeGuiBackend()
    locator_backend = FakeElementLocatorBackend(
        [
            ElementLocationCandidate(
                bbox=(100, 200, 500, 400),
                confidence=0.94,
                source="uia",
                reason="matched editor area",
            )
        ]
    )
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="先观察桌面",
                tool_name="take_screenshot",
                tool_args={"description": "before locating editor"},
            ),
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="定位编辑区",
                tool_name="locate_element",
                tool_args={"query": "编辑区"},
            ),
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="点击定位结果中心",
                tool_name="click",
                tool_args={"target": "last_located"},
            ),
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="输入文本",
                tool_name="type_text",
                tool_args={"text": "hello gui", "press_enter": True},
            ),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已定位编辑区并输入文本",
                supporting_evidence=["screenshot:ss_0003", "location:loc_0002"],
            ),
        ]
    )
    runtime = AutonomousComputerRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        screenshot_backend=screenshot_backend,
        gui_backend=gui_backend,
        element_locator_backend=locator_backend,
        agent=agent,
    )

    state = runtime.run("在 GUI 中输入 hello gui")

    assert state.run.status == "success"
    assert state.task.task_type == "hybrid"
    assert "take_screenshot" in state.control.allowed_tools
    assert "run_command" in state.control.allowed_tools
    assert state.metrics.screenshot_count == 3
    assert state.metrics.command_count == 0
    assert state.observation.latest_location_bbox == (100, 200, 500, 400)
    assert state.observation.latest_screenshot_id == "ss_0003"
    assert gui_backend.calls == [
        ("click", (300, 300), {"button": "left", "clicks": 1}),
        (
            "type_text",
            ("hello gui",),
            {
                "x": None,
                "y": None,
                "clear": False,
                "caret_position": "idle",
                "press_enter": True,
            },
        ),
    ]

    run_dir = tmp_path / "runs" / state.run.run_id
    screenshot_index = (run_dir / "screenshots" / "index.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(screenshot_index) == 3
    assert (run_dir / "locations" / "loc_0002.json").exists()

    trace_records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    execution_results = [
        record["payload"]["result"]["tool_name"]
        for record in trace_records
        if record["event_type"] == "tool_execution"
    ]
    assert execution_results == ["take_screenshot", "locate_element", "click", "type_text"]
    click_record = next(
        record
        for record in trace_records
        if record["event_type"] == "tool_execution" and record["payload"]["result"]["tool_name"] == "click"
    )
    assert click_record["payload"]["result"]["result"]["resolved_from"] == "last_located"


def test_autonomous_computer_runtime_allows_recovery_after_validation_failure(tmp_path: Path) -> None:
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="错误地直接定位",
                tool_name="locate_element",
                tool_args={"query": "编辑区"},
            ),
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="补截图后再继续",
                tool_name="take_screenshot",
                tool_args={"description": "fallback observation"},
            ),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已在校验失败后恢复并补充截图",
                supporting_evidence=["screenshot:ss_0001"],
            ),
        ]
    )
    runtime = AutonomousComputerRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        screenshot_backend=FakeScreenshotBackend(),
        agent=agent,
        max_consecutive_failures=3,
    )

    state = runtime.run("先定位再截图的错误流程")

    assert state.run.status == "success"
    assert state.metrics.step_count == 2
    assert state.metrics.screenshot_count == 1
    assert state.errors.last_error_code == ""
    assert agent.calls[1][2][0]["validation_error"] == {
        "code": "SCREENSHOT_REQUIRED",
        "message": "locate_element requires an existing screenshot observation",
    }

    run_dir = tmp_path / "runs" / state.run.run_id
    trace_records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    failed_validation = next(record for record in trace_records if record["event_type"] == "tool_validation")
    assert failed_validation["status"] == "failed"
    assert failed_validation["payload"]["error"]["code"] == "SCREENSHOT_REQUIRED"


def test_autonomous_computer_runtime_can_view_selected_historical_screenshot(tmp_path: Path) -> None:
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(
                kind="tool_call",
                tool_name="take_screenshot",
                tool_args={"description": "first observation"},
            ),
            TerminalAgentDecision(
                kind="tool_call",
                tool_name="view_screenshot",
                tool_args={"screenshot_id": "ss_0001"},
            ),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已回看指定截图",
                supporting_evidence=["screenshot:ss_0001"],
            ),
        ]
    )
    runtime = AutonomousComputerRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        screenshot_backend=FakeScreenshotBackend(),
        agent=agent,
    )

    state = runtime.run("回看刚才的截图")

    assert state.run.status == "success"
    assert "view_screenshot" in state.control.allowed_tools
    assert state.metrics.screenshot_count == 1
    assert state.observation.latest_screenshot_id == "ss_0001"
    assert state.last_action.artifact_refs == ["screenshot:ss_0001"]

    run_dir = tmp_path / "runs" / state.run.run_id
    trace_records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    view_record = next(
        record
        for record in trace_records
        if record["event_type"] == "tool_execution" and record["payload"]["result"]["tool_name"] == "view_screenshot"
    )
    assert view_record["artifact_refs"] == ["screenshot:ss_0001"]
    assert view_record["payload"]["result"]["success"] is True


def test_autonomous_computer_runtime_blocks_risky_commands(tmp_path: Path) -> None:
    backend = SequenceBackend([])
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(
                kind="tool_call",
                tool_name="run_command",
                tool_args={"command": "Remove-Item -Recurse ."},
            )
        ]
    )
    runtime = AutonomousComputerRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        command_backend=backend,
        agent=agent,
    )

    state = runtime.run("删除所有文件")

    assert state.run.status == "failed"
    assert state.errors.last_error_code == "RISK_BLOCKED"
    assert state.metrics.command_count == 0
    assert backend.calls == []
