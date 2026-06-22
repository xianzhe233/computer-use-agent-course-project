import json
from pathlib import Path

from computer_use_agent.runtime import TerminalRuntime
from computer_use_agent.sample_tasks import NOTEPAD_START_COMMAND
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
