import json
from pathlib import Path
from typing import Sequence
from unittest.mock import patch

from computer_use_agent.autonomous_runtime import AutonomousComputerRuntime
from computer_use_agent.computer_agent import TerminalAgentDecision
from computer_use_agent.examiner_agent import ExaminerAction
from computer_use_agent.runtime_state import RuntimeState
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


class AssertSelectedScreenshotAgent:
    def __init__(self) -> None:
        self.call_count = 0

    def decide(
        self,
        *,
        state: RuntimeState,
        workspace: Path,
        history: Sequence[dict[str, object]],
    ) -> TerminalAgentDecision:
        self.call_count += 1
        if self.call_count == 1:
            assert state.observation.latest_screenshot_id == "ss_0001"
            assert state.observation.selected_screenshot_ids == ["ss_0001"]
            assert len(state.observation.selected_screenshot_paths) == 1
            assert state.observation.selected_screenshot_paths[0].endswith("ss_0001.png")
            return TerminalAgentDecision(kind="tool_call", tool_name="open_app", tool_args={"name": "notepad"})
        assert state.observation.latest_screenshot_id == "ss_0002"
        assert state.observation.selected_screenshot_ids == ["ss_0002"]
        assert len(state.observation.selected_screenshot_paths) == 1
        assert state.observation.selected_screenshot_paths[0].endswith("ss_0002.png")
        return TerminalAgentDecision(
            kind="finish_request",
            completion_claim="GUI 动作后的自动截图已自动进入下一轮上下文",
            supporting_evidence=["screenshot:ss_0002"],
        )


class ScriptedExaminerAgent:
    def __init__(self, actions: list[ExaminerAction]) -> None:
        self.actions = actions
        self.calls: list[tuple[RuntimeState, dict[str, object], list[dict[str, object]]]] = []

    def act(
        self,
        *,
        state: RuntimeState,
        review_payload: dict[str, object],
        history: Sequence[dict[str, object]],
    ) -> ExaminerAction:
        self.calls.append((state, review_payload, list(history)))
        return self.actions.pop(0)


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

    def move(self, x: int, y: int) -> None:
        self.calls.append(("move", (x, y), {}))

    def scroll(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        axis: str = "vertical",
        direction: str = "down",
        amount: int = 1,
    ) -> None:
        self.calls.append(("scroll", (), {"x": x, "y": y, "axis": axis, "direction": direction, "amount": amount}))

    def open_app(self, name: str) -> str:
        self.calls.append(("open_app", (name,), {}))
        return f"opened {name}"

    def switch_app(self, name: str) -> str:
        self.calls.append(("switch_app", (name,), {}))
        return f"switched {name}"

    def focus_window(self, title: str) -> str:
        self.calls.append(("focus_window", (title,), {}))
        return f"focused {title}"


class FakeElementLocatorBackend:
    def __init__(self, candidates: list[ElementLocationCandidate] | dict[str, list[ElementLocationCandidate]]) -> None:
        self.candidates = candidates
        self.calls: list[tuple[str, Path, str]] = []

    def locate(self, *, query: str, screenshot_path: Path, screenshot_id: str = "") -> list[ElementLocationCandidate]:
        self.calls.append((query, screenshot_path, screenshot_id))
        if isinstance(self.candidates, dict):
            return list(self.candidates.get(query, []))
        return list(self.candidates)


class SequenceBackend(PowerShellBackend):
    def __init__(self, results: list[CommandResult]) -> None:
        super().__init__(executable="fake-powershell")
        self.results = results
        self.calls: list[tuple[str, int, Path | None]] = []

    def execute(self, command: str, timeout: int = 10, cwd: Path | None = None) -> CommandResult:
        self.calls.append((command, timeout, cwd))
        return self.results.pop(0)


def _accept_examiner(reason: str) -> ScriptedExaminerAgent:
    return ScriptedExaminerAgent([ExaminerAction(kind="submit_decision", decision="accept", reason=reason)])


def test_autonomous_computer_runtime_uses_gui_tools_then_finishes(tmp_path: Path) -> None:
    screenshot_backend = FakeScreenshotBackend()
    gui_backend = FakeGuiBackend()
    locator_backend = FakeElementLocatorBackend(
        [
            ElementLocationCandidate(
                point=(300, 300),
                confidence=0.94,
                source="vision",
                reason="matched editor point",
            )
        ]
    )
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="先观察桌面",
                tool_name="take_screenshot",
                tool_args={"description": "before clicking editor"},
            ),
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="点击编辑区",
                tool_name="click",
                tool_args={"target_query": "编辑区"},
            ),
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="输入文本",
                tool_name="type_text",
                tool_args={"text": "hello gui", "press_enter": True},
            ),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已点击编辑区并输入文本",
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
        examiner_agent=_accept_examiner("已点击编辑区并输入文本"),
    )

    state = runtime.run("在 GUI 中输入 hello gui")

    assert state.run.status == "success"
    assert state.task.task_type == "hybrid"
    assert state.examiner.review_count == 1
    assert state.examiner.last_decision == "accept"
    assert "take_screenshot" in state.control.allowed_tools
    assert "run_command" in state.control.allowed_tools
    assert state.metrics.screenshot_count == 4
    assert state.metrics.command_count == 0
    assert state.observation.latest_location_point == (300, 300)
    assert state.observation.latest_screenshot_id == "ss_0004"
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
    assert len(screenshot_index) == 4
    assert (run_dir / "locations" / "loc_0002.json").exists()
    assert (run_dir / "examiner" / "review_0001_input.json").exists()
    trace_records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    execution_results = [
        record["payload"]["result"]["tool_name"]
        for record in trace_records
        if record["event_type"] == "tool_execution"
    ]
    assert execution_results == ["take_screenshot", "click", "type_text"]
    initial_observation = next(record for record in trace_records if record["event_type"] == "initial_observation")
    assert initial_observation["payload"]["description"] == "初始截图"
    click_record = next(
        record
        for record in trace_records
        if record["event_type"] == "tool_execution" and record["payload"]["result"]["tool_name"] == "click"
    )
    assert click_record["payload"]["result"]["result"]["resolved_from"] == "target_query"


def test_autonomous_runtime_auto_locates_semantic_click_and_type_text(tmp_path: Path) -> None:
    screenshot_backend = FakeScreenshotBackend()
    gui_backend = FakeGuiBackend()
    locator_backend = FakeElementLocatorBackend(
        {
            "搜索框": [
                ElementLocationCandidate(
                    point=(150, 85),
                    confidence=0.93,
                    source="vision",
                    reason="matched search input",
                )
            ],
            "提交按钮": [
                ElementLocationCandidate(
                    point=(460, 330),
                    confidence=0.91,
                    source="vision",
                    reason="matched submit button",
                )
            ],
        }
    )
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(kind="tool_call", tool_name="take_screenshot", tool_args={"description": "observe the app"}),
            TerminalAgentDecision(
                kind="tool_call",
                tool_name="type_text",
                tool_args={"text": "hello", "target_query": "搜索框", "press_enter": False},
            ),
            TerminalAgentDecision(kind="tool_call", tool_name="click", tool_args={"target_query": "提交按钮"}),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已通过语义定位完成输入和点击",
                supporting_evidence=["location:loc_0002", "location:loc_0003", "screenshot:ss_0003"],
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
        examiner_agent=_accept_examiner("已通过语义定位完成输入和点击"),
    )

    state = runtime.run("在搜索框输入 hello 然后点击提交按钮")

    assert state.run.status == "success"
    assert locator_backend.calls[0][0] == "搜索框"
    assert locator_backend.calls[1][0] == "提交按钮"
    assert gui_backend.calls == [
        (
            "type_text",
            ("hello",),
            {
                "x": 150,
                "y": 85,
                "clear": False,
                "caret_position": "idle",
                "press_enter": False,
            },
        ),
        ("click", (460, 330), {"button": "left", "clicks": 1}),
    ]
    assert state.metrics.screenshot_count == 4
    assert state.observation.latest_location_query == "提交按钮"


def test_autonomous_runtime_auto_locates_drag_start_and_end(tmp_path: Path) -> None:
    screenshot_backend = FakeScreenshotBackend()
    gui_backend = FakeGuiBackend()
    locator_backend = FakeElementLocatorBackend(
        {
            "开始滑块": [
                ElementLocationCandidate(
                    point=(120, 420),
                    confidence=0.9,
                    source="vision",
                    reason="matched drag start",
                )
            ],
            "结束滑块": [
                ElementLocationCandidate(
                    point=(520, 420),
                    confidence=0.92,
                    source="vision",
                    reason="matched drag end",
                )
            ],
        }
    )
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(kind="tool_call", tool_name="take_screenshot", tool_args={"description": "before drag"}),
            TerminalAgentDecision(kind="tool_call", tool_name="drag", tool_args={"start_query": "开始滑块", "end_query": "结束滑块"}),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已通过语义定位完成拖拽",
                supporting_evidence=["location:loc_0002", "screenshot:ss_0002"],
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
        examiner_agent=_accept_examiner("已通过语义定位完成拖拽"),
    )

    state = runtime.run("把滑块从开始拖到结束")

    assert state.run.status == "success"
    assert [call[0] for call in locator_backend.calls] == ["开始滑块", "结束滑块"]
    assert gui_backend.calls == [("drag", (120, 420, 520, 420), {})]
    assert state.metrics.screenshot_count == 3


def test_autonomous_computer_runtime_allows_recovery_after_validation_failure(tmp_path: Path) -> None:
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(
                kind="tool_call",
                thought_summary="错误地直接语义点击",
                tool_name="click",
                tool_args={"target_query": "编辑区"},
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
        examiner_agent=_accept_examiner("已在校验失败后恢复并补充截图"),
        max_consecutive_failures=3,
        capture_initial_screenshot=False,
    )

    state = runtime.run("先定位再截图的错误流程")

    assert state.run.status == "success"
    assert state.metrics.step_count == 2
    assert state.metrics.screenshot_count == 1
    assert state.errors.last_error_code == ""
    assert agent.calls[1][2][0]["validation_error"] == {
        "code": "SCREENSHOT_REQUIRED",
        "message": "semantic click requires an existing screenshot observation",
    }

    run_dir = tmp_path / "runs" / state.run.run_id
    trace_records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    failed_validation = next(record for record in trace_records if record["event_type"] == "tool_validation")
    assert failed_validation["status"] == "failed"
    assert failed_validation["payload"]["error"]["code"] == "SCREENSHOT_REQUIRED"


def test_autonomous_computer_runtime_rejects_once_then_returns_to_main_loop(tmp_path: Path) -> None:
    backend = SequenceBackend(
        [
            CommandResult(
                command="Get-ChildItem",
                stdout="demo.txt",
                stderr="",
                exit_code=0,
                success=True,
                duration_ms=12,
            ),
            CommandResult(
                command="Get-Content demo.txt",
                stdout="hello",
                stderr="",
                exit_code=0,
                success=True,
                duration_ms=9,
            ),
        ]
    )
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(kind="tool_call", tool_name="run_command", tool_args={"command": "Get-ChildItem"}),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="目录已经列出，应当完成",
                supporting_evidence=["command:cmd_0001"],
            ),
            TerminalAgentDecision(kind="tool_call", tool_name="run_command", tool_args={"command": "Get-Content demo.txt"}),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已补充读取文件内容作为证据",
                supporting_evidence=["command:cmd_0002"],
            ),
        ]
    )
    examiner = ScriptedExaminerAgent(
        [
            ExaminerAction(
                kind="submit_decision",
                decision="reject",
                reason="缺少直接读取目标文件内容的证据",
                missing_evidence=["缺少文件内容验证"],
                suggested_next_steps=["执行 Get-Content demo.txt 并再次 finish_request"],
            ),
            ExaminerAction(kind="submit_decision", decision="accept", reason="已补充读取文件内容，证据充分"),
        ]
    )
    runtime = AutonomousComputerRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        command_backend=backend,
        screenshot_backend=FakeScreenshotBackend(),
        agent=agent,
        examiner_agent=examiner,
    )

    state = runtime.run("验证 demo.txt 内容")

    assert state.run.status == "success"
    assert state.metrics.rework_count == 1
    assert state.examiner.review_count == 2
    assert state.examiner.last_decision == "accept"
    run_dir = tmp_path / "runs" / state.run.run_id
    review_one = json.loads((run_dir / "examiner" / "review_0001_output.json").read_text(encoding="utf-8"))
    assert review_one["decision"] == "reject"
    assert review_one["suggested_next_steps"] == ["执行 Get-Content demo.txt 并再次 finish_request"]


def test_open_and_switch_app_support_discovery_first_turn(tmp_path: Path) -> None:
    screenshot_backend = FakeScreenshotBackend()
    gui_backend = FakeGuiBackend()
    command_backend = SequenceBackend(
        [
            CommandResult(
                command="discover apps",
                stdout="Name AppID\n记事本 Microsoft.WindowsNotepad_8wekyb3d8bbwe!App\n",
                stderr="",
                exit_code=0,
                success=True,
                duration_ms=10,
            ),
            CommandResult(
                command="discover windows",
                stdout="ProcessName MainWindowTitle\nCode agent.py - Visual Studio Code\nmsedge Assistant - Microsoft Edge\n",
                stderr="",
                exit_code=0,
                success=True,
                duration_ms=10,
            ),
        ]
    )
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(kind="tool_call", tool_name="open_app", tool_args={}),
            TerminalAgentDecision(kind="tool_call", tool_name="switch_app", tool_args={"name": None}),
            TerminalAgentDecision(kind="tool_call", tool_name="open_app", tool_args={"name": "记事本"}),
            TerminalAgentDecision(kind="tool_call", tool_name="switch_app", tool_args={"name": "Visual Studio Code"}),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已完成两步候选发现和执行",
                supporting_evidence=["command:cmd_0001", "command:cmd_0002", "screenshot:ss_0003"],
            ),
        ]
    )
    runtime = AutonomousComputerRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        command_backend=command_backend,
        screenshot_backend=screenshot_backend,
        gui_backend=gui_backend,
        agent=agent,
        examiner_agent=_accept_examiner("已完成两步候选发现和执行"),
        max_steps=8,
    )

    state = runtime.run("先发现应用和窗口，再选择执行")

    assert state.run.status == "success"
    assert command_backend.calls[0][0].startswith("Get-StartApps")
    assert "MainWindowTitle" in command_backend.calls[1][0]
    assert gui_backend.calls == [
        ("open_app", ("记事本",), {}),
        ("switch_app", ("Visual Studio Code",), {}),
    ]
    assert state.metrics.command_count == 2
    trace = (tmp_path / "runs" / state.run.run_id / "trace.jsonl").read_text(encoding="utf-8")
    assert "start_menu_apps" in trace
    assert "current_windows" in trace



def test_autonomous_computer_runtime_supports_new_gui_tools(tmp_path: Path) -> None:
    screenshot_backend = FakeScreenshotBackend()
    gui_backend = FakeGuiBackend()
    locator_backend = FakeElementLocatorBackend(
        {
            "文件图标": [
                ElementLocationCandidate(point=(100, 120), confidence=0.9, source="vision", reason="icon")
            ],
            "列表区域": [
                ElementLocationCandidate(point=(320, 420), confidence=0.88, source="vision", reason="list")
            ],
            "提示按钮": [
                ElementLocationCandidate(point=(480, 200), confidence=0.91, source="vision", reason="hint")
            ],
        }
    )
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(kind="tool_call", tool_name="take_screenshot", tool_args={"description": "observe"}),
            TerminalAgentDecision(kind="tool_call", tool_name="double_click", tool_args={"target_query": "文件图标"}),
            TerminalAgentDecision(kind="tool_call", tool_name="right_click", tool_args={"x": 200, "y": 210}),
            TerminalAgentDecision(kind="tool_call", tool_name="move_mouse", tool_args={"target_query": "提示按钮"}),
            TerminalAgentDecision(kind="tool_call", tool_name="hover", tool_args={"x": 510, "y": 220, "duration_ms": 0}),
            TerminalAgentDecision(kind="tool_call", tool_name="scroll", tool_args={"target_query": "列表区域", "direction": "down", "amount": 2}),
            TerminalAgentDecision(kind="tool_call", tool_name="open_app", tool_args={"name": "notepad"}),
            TerminalAgentDecision(kind="tool_call", tool_name="switch_app", tool_args={"name": "notepad"}),
            TerminalAgentDecision(kind="tool_call", tool_name="focus_window", tool_args={"title": "Untitled - Notepad"}),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已执行新增 GUI 工具",
                supporting_evidence=["screenshot:ss_0009"],
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
        examiner_agent=_accept_examiner("已执行新增 GUI 工具"),
        max_steps=12,
    )

    state = runtime.run("依次测试新增 GUI 工具")

    assert state.run.status == "success"
    assert all(
        tool in state.control.allowed_tools
        for tool in [
            "double_click",
            "right_click",
            "move_mouse",
            "hover",
            "scroll",
            "open_app",
            "switch_app",
            "focus_window",
        ]
    )
    assert gui_backend.calls == [
        ("click", (100, 120), {"button": "left", "clicks": 2}),
        ("click", (200, 210), {"button": "right", "clicks": 1}),
        ("move", (480, 200), {}),
        ("move", (510, 220), {}),
        ("scroll", (), {"x": 320, "y": 420, "axis": "vertical", "direction": "down", "amount": 2}),
        ("open_app", ("notepad",), {}),
        ("switch_app", ("notepad",), {}),
        ("focus_window", ("Untitled - Notepad",), {}),
    ]
    assert state.observation.active_window_title == "Untitled - Notepad"
    assert state.metrics.screenshot_count == 10


def test_gui_action_auto_screenshot_is_selected_for_next_turn(tmp_path: Path) -> None:
    runtime = AutonomousComputerRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        screenshot_backend=FakeScreenshotBackend(),
        gui_backend=FakeGuiBackend(),
        agent=AssertSelectedScreenshotAgent(),
        examiner_agent=_accept_examiner("GUI 动作后的自动截图已自动进入下一轮上下文"),
    )

    state = runtime.run("打开记事本后，下一轮自动带上刚产生的截图")

    assert state.run.status == "success"
    assert state.metrics.screenshot_count == 2
    assert state.observation.latest_screenshot_id == "ss_0002"
    assert state.observation.selected_screenshot_ids == ["ss_0002"]
    assert len(state.observation.selected_screenshot_paths) == 1
    assert state.observation.selected_screenshot_paths[0].endswith("ss_0002.png")


def test_gui_action_auto_screenshot_waits_before_capture(tmp_path: Path) -> None:
    runtime = AutonomousComputerRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        screenshot_backend=FakeScreenshotBackend(),
        gui_backend=FakeGuiBackend(),
        agent=AssertSelectedScreenshotAgent(),
        examiner_agent=_accept_examiner("GUI 动作后的自动截图已自动进入下一轮上下文"),
        post_gui_screenshot_delay_seconds=0.25,
    )

    with patch("computer_use_agent.autonomous_runtime.time.sleep") as sleep_mock:
        state = runtime.run("GUI 动作后等待片刻再截图")

    assert state.run.status == "success"
    sleep_mock.assert_called_with(0.25)


def test_autonomous_computer_runtime_can_view_multiple_historical_screenshots(tmp_path: Path) -> None:
    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(kind="tool_call", tool_name="take_screenshot", tool_args={"description": "first observation"}),
            TerminalAgentDecision(kind="tool_call", tool_name="take_screenshot", tool_args={"description": "second observation"}),
            TerminalAgentDecision(kind="tool_call", tool_name="view_screenshot", tool_args={"screenshot_ids": ["ss_0002", "ss_0003"]}),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已回看多张指定截图",
                supporting_evidence=["screenshot:ss_0002", "screenshot:ss_0003"],
            ),
        ]
    )
    runtime = AutonomousComputerRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        screenshot_backend=FakeScreenshotBackend(),
        agent=agent,
        examiner_agent=_accept_examiner("已回看多张指定截图"),
    )

    state = runtime.run("回看刚才的截图")

    assert state.run.status == "success"
    assert "view_screenshot" in state.control.allowed_tools
    assert state.metrics.screenshot_count == 3
    assert state.observation.latest_screenshot_id == "ss_0003"
    assert state.observation.selected_screenshot_ids == ["ss_0002", "ss_0003"]
    assert state.examiner.last_decision == "accept"

    run_dir = tmp_path / "runs" / state.run.run_id
    trace_records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    initial_observation = next(record for record in trace_records if record["event_type"] == "initial_observation")
    assert initial_observation["payload"]["description"] == "初始截图"
    view_record = next(
        record
        for record in trace_records
        if record["event_type"] == "tool_execution" and record["payload"]["result"]["tool_name"] == "view_screenshot"
    )
    assert view_record["artifact_refs"] == ["screenshot:ss_0002", "screenshot:ss_0003"]
    assert view_record["payload"]["result"]["success"] is True


def test_examiner_observation_summary_is_carried_across_review_steps(tmp_path: Path) -> None:
    class ObservationTrackingExaminer:
        def __init__(self) -> None:
            self.call_count = 0

        def act(
            self,
            *,
            state: RuntimeState,
            review_payload: dict[str, object],
            history: Sequence[dict[str, object]],
        ) -> ExaminerAction:
            self.call_count += 1
            if self.call_count == 1:
                return ExaminerAction(
                    kind="view_screenshot",
                    screenshot_ids=["ss_0002"],
                    note="检查输入后的最终截图",
                    observed_findings=["截图中已经能看到 hello examiner 文本"],
                    remaining_questions=["还需要确认这是否就是最终证据图"],
                )
            assert history[0]["observed_findings"] == ["截图中已经能看到 hello examiner 文本"]
            assert state.examiner.observed_findings == ["截图中已经能看到 hello examiner 文本"]
            assert state.examiner.remaining_questions == ["还需要确认这是否就是最终证据图"]
            assert state.examiner.observation_log[0]["screenshot_ids"] == ["ss_0002"]
            return ExaminerAction(kind="submit_decision", decision="accept", reason="观察摘要已成功回传")

    agent = ScriptedComputerAgent(
        [
            TerminalAgentDecision(kind="tool_call", tool_name="take_screenshot", tool_args={"description": "collect evidence"}),
            TerminalAgentDecision(
                kind="finish_request",
                completion_claim="已收集截图证据",
                supporting_evidence=["screenshot:ss_0002"],
            ),
        ]
    )
    runtime = AutonomousComputerRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        screenshot_backend=FakeScreenshotBackend(),
        agent=agent,
        examiner_agent=ObservationTrackingExaminer(),
    )

    state = runtime.run("采集截图后由 examiner 审阅")

    assert state.run.status == "success"
    assert state.examiner.observed_findings == ["截图中已经能看到 hello examiner 文本"]
    assert state.examiner.remaining_questions == ["还需要确认这是否就是最终证据图"]
    assert state.examiner.reviewed_screenshot_ids == ["ss_0002"]


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
