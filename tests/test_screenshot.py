import json
from pathlib import Path

from computer_use_agent.run_store import RunStore
from computer_use_agent.tools.screenshot import ScreenshotExecutionError, ScreenshotResult, take_screenshot


class FakeScreenshotBackend:
    def __init__(self, *, width: int = 1600, height: int = 900, should_fail: bool = False) -> None:
        self.width = width
        self.height = height
        self.should_fail = should_fail
        self.calls: list[tuple[Path, str]] = []

    def capture(self, path: Path, target: str = "screen") -> tuple[int, int]:
        self.calls.append((path, target))
        if self.should_fail:
            raise ScreenshotExecutionError("backend failed")
        path.write_bytes(b"fake-png")
        return self.width, self.height


def test_take_screenshot_returns_structured_metadata(tmp_path: Path) -> None:
    backend = FakeScreenshotBackend()
    path = tmp_path / "shots" / "ss_0001.png"

    result = take_screenshot(
        screenshot_id="ss_0001",
        path=path,
        step_id=1,
        source_action_id="act_0001",
        backend=backend,
    )

    assert result.success is True
    assert result.screenshot_id == "ss_0001"
    assert result.width == 1600
    assert result.height == 900
    assert result.step_id == 1
    assert result.source_action_id == "act_0001"
    assert backend.calls == [(path, "screen")]
    assert path.exists()


def test_take_screenshot_returns_structured_error(tmp_path: Path) -> None:
    backend = FakeScreenshotBackend(should_fail=True)

    result = take_screenshot(
        screenshot_id="ss_0001",
        path=tmp_path / "shots" / "ss_0001.png",
        step_id=1,
        backend=backend,
    )

    assert result.success is False
    assert result.error == {
        "code": "SCREENSHOT_CAPTURE_FAILED",
        "message": "ScreenshotExecutionError: backend failed",
    }


def test_run_store_writes_screenshot_index(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs" / "run_0001")
    store.prepare()

    screenshot_path = store.screenshots_dir / "ss_0001.png"
    screenshot_path.write_bytes(b"fake-png")
    result = ScreenshotResult(
        screenshot_id="ss_0001",
        path=str(screenshot_path),
        width=1920,
        height=1080,
        success=True,
        duration_ms=12,
        timestamp="2026-06-22T18:30:00+00:00",
        step_id=3,
        source_action_id="act_0003",
    )

    artifacts = store.write_screenshot_result(result, description="after opening notepad")

    assert artifacts["artifact_ref"] == "screenshot:ss_0001"
    index_lines = store.screenshot_index_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(index_lines) == 1
    index_record = json.loads(index_lines[0])
    assert index_record["screenshot_id"] == "ss_0001"
    assert index_record["description"] == "after opening notepad"
    assert index_record["resolution"] == {"width": 1920, "height": 1080}
