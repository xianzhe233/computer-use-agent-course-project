import json
from pathlib import Path

from computer_use_agent.runtime import TerminalRuntime, TerminalMainAgent
from computer_use_agent.sample_tasks import NOTEPAD_START_COMMAND, DemoTask, PlannedAction
from computer_use_agent.tools.element_location import ElementLocationCandidate
from computer_use_agent.tools.run_command import CommandResult, PowerShellBackend


class SequenceBackend(PowerShellBackend):
    def __init__(self, results: list[CommandResult]) -> None:
        super().__init__(executable="fake-powershell")
        self.results = results
        self.calls: list[str] = []

    def execute(self, command: str, timeout: int = 10, cwd: Path | None = None) -> CommandResult:
        self.calls.append(command)
        return self.results.pop(0)


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


class SequenceElementLocatorBackend:
    def __init__(self, results: list[list[ElementLocationCandidate]]) -> None:
        self.results = results
        self.calls: list[tuple[str, Path, str]] = []

    def locate(self, *, query: str, screenshot_path: Path, screenshot_id: str = "") -> list[ElementLocationCandidate]:
        self.calls.append((query, screenshot_path, screenshot_id))
        index = min(len(self.calls) - 1, len(self.results) - 1)
        return self.results[index]


class LocateThenClickAgent(TerminalMainAgent):
    def plan(self, user_request: str) -> DemoTask | None:
        if user_request == "定位并点击记事本输入区":
            return DemoTask(
                name="locate_then_click_notepad",
                description="先截图并定位输入区，再点击定位结果中心",
                user_requests=("定位并点击记事本输入区",),
                task_type="gui",
                action_plan=(
                    PlannedAction(tool_name="take_screenshot", tool_args={"description": "before locating notepad editor"}),
                    PlannedAction(tool_name="locate_element", tool_args={"query": "编辑区"}),
                    PlannedAction(tool_name="click", tool_args={"target": "last_located"}),
                ),
                success_hint="已根据描述定位并点击目标区域",
            )
        if user_request == "定位失败后重新截图再重试":
            return DemoTask(
                name="locate_retry_after_screenshot",
                description="首次定位失败后重新截图并重试，再点击定位结果中心",
                user_requests=("定位失败后重新截图再重试",),
                task_type="gui",
                action_plan=(
                    PlannedAction(tool_name="take_screenshot", tool_args={"description": "first screenshot"}),
                    PlannedAction(tool_name="locate_element", tool_args={"query": "编辑区"}),
                    PlannedAction(tool_name="take_screenshot", tool_args={"description": "fallback screenshot"}),
                    PlannedAction(tool_name="locate_element", tool_args={"query": "编辑区"}),
                    PlannedAction(tool_name="click", tool_args={"target": "last_located"}),
                ),
                success_hint="定位失败后通过重新截图重试成功",
            )
        return super().plan(user_request)


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
    assert len(trace_lines) >= 4
    event_types = [json.loads(line)["event_type"] for line in trace_lines]
    assert "tool_validation" in event_types
    assert "tool_execution" in event_types
    assert event_types[-1] == "finish_request"


def test_runtime_marks_failed_when_task_not_supported(tmp_path: Path) -> None:
    runtime = TerminalRuntime(workspace=tmp_path, runs_root=tmp_path / "runs")

    state = runtime.run("做一个这里暂不支持的任务")

    assert state.run.status == "failed"
    assert state.errors.last_error_code == "TASK_NOT_SUPPORTED"


def test_runtime_persists_gui_artifacts_and_trace(tmp_path: Path) -> None:
    command_backend = SequenceBackend(
        [
            CommandResult(
                command=NOTEPAD_START_COMMAND,
                stdout="",
                stderr="",
                exit_code=0,
                success=True,
                duration_ms=22,
            )
        ]
    )
    screenshot_backend = FakeScreenshotBackend()
    gui_backend = FakeGuiBackend()
    runtime = TerminalRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        command_backend=command_backend,
        screenshot_backend=screenshot_backend,
        gui_backend=gui_backend,
    )

    state = runtime.run("打开记事本并输入一行文字")

    assert state.run.status == "success"
    assert state.task.task_type == "gui"
    assert state.metrics.screenshot_count == 3
    assert state.observation.latest_screenshot_id == "ss_0003"
    assert gui_backend.calls == [
        (
            "type_text",
            ("computer use agent gui demo",),
            {
                "x": None,
                "y": None,
                "clear": False,
                "caret_position": "idle",
                "press_enter": True,
            },
        )
    ]

    run_dir = tmp_path / "runs" / state.run.run_id
    screenshot_index = (run_dir / "screenshots" / "index.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(screenshot_index) == 3

    trace_records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    state_update_events = [record for record in trace_records if record["event_type"] == "state_update"]
    assert state_update_events
    type_execution = next(
        record
        for record in trace_records
        if record["event_type"] == "tool_execution" and record["payload"]["result"]["tool_name"] == "type_text"
    )
    assert type_execution["artifact_refs"] == ["screenshot:ss_0002"]


def test_runtime_separates_locate_and_click_steps_in_trace(tmp_path: Path) -> None:
    screenshot_backend = FakeScreenshotBackend()
    gui_backend = FakeGuiBackend()
    locator_backend = FakeElementLocatorBackend(
        [
            ElementLocationCandidate(
                bbox=(10, 20, 210, 120),
                confidence=0.93,
                source="uia",
                reason="matched notepad editor area",
            )
        ]
    )
    runtime = TerminalRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        screenshot_backend=screenshot_backend,
        gui_backend=gui_backend,
        element_locator_backend=locator_backend,
        agent=LocateThenClickAgent(),
    )

    state = runtime.run("定位并点击记事本输入区")

    assert state.run.status == "success"
    assert state.observation.latest_location_bbox == (10, 20, 210, 120)
    assert state.observation.latest_location_query == "编辑区"
    assert state.observation.latest_location_source == "uia"
    assert gui_backend.calls == [("click", (110, 70), {"button": "left", "clicks": 1})]

    run_dir = tmp_path / "runs" / state.run.run_id
    location_index_lines = (run_dir / "locations" / "index.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(location_index_lines) == 1
    location_index = json.loads(location_index_lines[0])
    assert location_index["bbox"] == [10, 20, 210, 120]
    assert location_index["reason"] == "matched notepad editor area"

    trace_records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    execution_results = [record["payload"]["result"]["tool_name"] for record in trace_records if record["event_type"] == "tool_execution"]
    assert execution_results == ["take_screenshot", "locate_element", "click"]

    locate_record = next(
        record
        for record in trace_records
        if record["event_type"] == "tool_execution" and record["payload"]["result"]["tool_name"] == "locate_element"
    )
    click_record = next(
        record
        for record in trace_records
        if record["event_type"] == "tool_execution" and record["payload"]["result"]["tool_name"] == "click"
    )
    assert locate_record["artifact_refs"] == ["screenshot:ss_0001", "location:loc_0002"]
    assert click_record["payload"]["result"]["result"]["resolved_from"] == "last_located"


def test_official_demo_uses_description_bbox_click_flow(tmp_path: Path) -> None:
    command_backend = SequenceBackend(
        [
            CommandResult(
                command=NOTEPAD_START_COMMAND,
                stdout="",
                stderr="",
                exit_code=0,
                success=True,
                duration_ms=20,
            )
        ]
    )
    gui_backend = FakeGuiBackend()
    locator_backend = FakeElementLocatorBackend(
        [
            ElementLocationCandidate(
                bbox=(100, 200, 500, 400),
                confidence=0.94,
                source="uia",
                reason="matched official notepad editor demo target",
            )
        ]
    )
    runtime = TerminalRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        command_backend=command_backend,
        screenshot_backend=FakeScreenshotBackend(),
        gui_backend=gui_backend,
        element_locator_backend=locator_backend,
    )

    state = runtime.run("用描述定位记事本编辑区并输入文字")

    assert state.run.status == "success"
    assert state.run.terminated_reason == "已通过描述定位记事本编辑区，点击定位结果中心并输入 demo 文本"
    assert state.observation.latest_location_bbox == (100, 200, 500, 400)
    assert state.metrics.screenshot_count == 5
    assert gui_backend.calls == [
        ("click", (300, 300), {"button": "left", "clicks": 1}),
        (
            "type_text",
            ("computer use agent locate element demo",),
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
    trace_records = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    execution_results = [record["payload"]["result"]["tool_name"] for record in trace_records if record["event_type"] == "tool_execution"]
    assert execution_results == [
        "take_screenshot",
        "run_command",
        "wait",
        "take_screenshot",
        "locate_element",
        "click",
        "type_text",
        "take_screenshot",
    ]
    assert (run_dir / "locations" / "loc_0005.json").exists()


def test_runtime_can_retry_after_locate_failure_with_new_screenshot(tmp_path: Path) -> None:
    gui_backend = FakeGuiBackend()
    locator_backend = SequenceElementLocatorBackend(
        [
            [],
            [
                ElementLocationCandidate(
                    bbox=(20, 30, 80, 90),
                    confidence=0.88,
                    source="uia",
                    reason="matched after fallback screenshot",
                )
            ],
        ]
    )
    runtime = TerminalRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        screenshot_backend=FakeScreenshotBackend(),
        gui_backend=gui_backend,
        element_locator_backend=locator_backend,
        agent=LocateThenClickAgent(),
    )

    state = runtime.run("定位失败后重新截图再重试")

    assert state.run.status == "success"
    assert state.metrics.consecutive_location_failures == 0
    assert state.observation.latest_location_suggested_next_steps == []
    assert gui_backend.calls == [("click", (50, 60), {"button": "left", "clicks": 1})]

    run_dir = tmp_path / "runs" / state.run.run_id
    location_records = (run_dir / "locations" / "index.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(location_records) == 2
    assert json.loads(location_records[0])["success"] is False
    assert json.loads(location_records[1])["success"] is True


def test_runtime_blocks_click_after_failed_location_without_blind_click(tmp_path: Path) -> None:
    gui_backend = FakeGuiBackend()
    runtime = TerminalRuntime(
        workspace=tmp_path,
        runs_root=tmp_path / "runs",
        screenshot_backend=FakeScreenshotBackend(),
        gui_backend=gui_backend,
        element_locator_backend=FakeElementLocatorBackend([]),
        agent=LocateThenClickAgent(),
    )

    state = runtime.run("定位并点击记事本输入区")

    assert state.run.status == "failed"
    assert state.errors.last_error_code == "LOCATION_REQUIRED"
    assert state.errors.last_failed_tool == "click"
    assert state.observation.latest_location_error_code == "ELEMENT_NOT_FOUND"
    assert state.observation.latest_location_suggested_next_steps == [
        "take_screenshot",
        "scroll or reveal more UI",
        "retry locate_element",
    ]
    assert gui_backend.calls == []
