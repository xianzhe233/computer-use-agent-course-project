import json
from dataclasses import dataclass
from pathlib import Path

from computer_use_agent.tool_smoke import (
    NOTEPAD_START_FOR_PID,
    SmokeRecorder,
    parse_first_int,
    points_from_rect,
)


@dataclass(slots=True)
class FakeResult:
    success: bool
    value: str


def test_notepad_start_command_uses_explicit_exe_path() -> None:
    assert "Start-Process notepad" not in NOTEPAD_START_FOR_PID
    assert "notepad.exe" in NOTEPAD_START_FOR_PID
    assert "-FilePath" in NOTEPAD_START_FOR_PID


def test_parse_first_int() -> None:
    assert parse_first_int("\r\n1234\r\n") == 1234
    assert parse_first_int("pid=5678 status=ok") == 5678
    assert parse_first_int("no pid") is None


def test_points_from_rect_stays_inside_rect() -> None:
    points = points_from_rect(100, 50, 700, 450)

    for x in (points.click_x, points.drag_start_x, points.drag_end_x):
        assert 100 <= x < 700
    for y in (points.click_y, points.drag_start_y, points.drag_end_y):
        assert 50 <= y < 450
    assert points.drag_start_x <= points.drag_end_x


def test_points_from_tiny_rect_are_stable() -> None:
    points = points_from_rect(10, 20, 11, 21)

    assert points.click_x == 10
    assert points.click_y == 20
    assert points.drag_start_x == 10
    assert points.drag_end_x == 10


def test_smoke_recorder_writes_report(tmp_path: Path) -> None:
    recorder = SmokeRecorder(tmp_path / "smoke")
    recorder.record("fake.ok", FakeResult(success=True, value="done"))
    report_path = recorder.write_report()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["success"] is True
    assert report["records"][0]["name"] == "fake.ok"
    assert report["records"][0]["result"] == {"success": True, "value": "done"}
