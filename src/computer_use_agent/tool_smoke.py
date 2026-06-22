from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .tools import click, drag, hotkey, run_command, take_screenshot, type_text, wait

NOTEPAD_EXE = '"$env:WINDIR\\System32\\notepad.exe"'
NOTEPAD_START_FOR_PID = (
    f"Start-Process -FilePath {NOTEPAD_EXE} -PassThru | Select-Object -ExpandProperty Id"
)


@dataclass(frozen=True, slots=True)
class SmokePoints:
    click_x: int
    click_y: int
    drag_start_x: int
    drag_start_y: int
    drag_end_x: int
    drag_end_y: int


class SmokeRecorder:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.records: list[dict[str, Any]] = []

    def record(self, name: str, result: object, extra: dict[str, Any] | None = None) -> None:
        payload = _to_payload(result)
        self.records.append(
            {
                "name": name,
                "timestamp": datetime.now(UTC).isoformat(),
                "success": _payload_success(payload),
                "result": payload,
                "extra": extra or {},
            }
        )
        status = "OK" if self.records[-1]["success"] else "FAIL"
        print(f"[{status}] {name}")

    def record_error(self, name: str, exc: BaseException, extra: dict[str, Any] | None = None) -> None:
        self.records.append(
            {
                "name": name,
                "timestamp": datetime.now(UTC).isoformat(),
                "success": False,
                "result": {
                    "error": {
                        "code": type(exc).__name__,
                        "message": str(exc),
                    }
                },
                "extra": extra or {},
            }
        )
        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")

    def write_report(self) -> Path:
        report = {
            "created_at": datetime.now(UTC).isoformat(),
            "root": str(self.root),
            "success": all(record["success"] for record in self.records),
            "records": self.records,
        }
        path = self.root / "tool_smoke_report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Actually smoke-test computer-use-agent tools. GUI mode opens Notepad and performs real mouse/keyboard actions.",
    )
    parser.add_argument("--runs-root", default="runs", help="Root directory for smoke reports.")
    parser.add_argument("--yes", action="store_true", help="Skip the interactive safety confirmation.")
    parser.add_argument("--no-gui", action="store_true", help="Only test non-GUI tools.")
    parser.add_argument("--keep-open", action="store_true", help="Keep the Notepad process open after GUI smoke test.")
    parser.add_argument("--wait-seconds", type=int, default=1, help="Seconds for the wait tool smoke test.")
    parser.add_argument(
        "--text",
        default="computer use agent tool smoke test",
        help="Text to type into Notepad during GUI smoke test.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.no_gui and sys.platform != "win32":
        print("GUI smoke test requires Windows. Use --no-gui to test only non-GUI tools.")
        return 2
    if not args.no_gui and not args.yes and not _confirm_gui_smoke():
        print("Aborted.")
        return 2

    run_dir = Path(args.runs_root).resolve() / datetime.now(UTC).strftime("tool_smoke_%Y%m%d_%H%M%S")
    recorder = SmokeRecorder(run_dir)

    _run_non_gui_smoke(recorder=recorder, wait_seconds=args.wait_seconds)
    if not args.no_gui:
        _run_gui_smoke(
            recorder=recorder,
            text=args.text,
            keep_open=args.keep_open,
        )

    report_path = recorder.write_report()
    print(f"Report: {report_path}")
    return 0 if all(record["success"] for record in recorder.records) else 1


def _run_non_gui_smoke(*, recorder: SmokeRecorder, wait_seconds: int) -> None:
    recorder.record("run_command.get_date", run_command("Get-Date"))
    recorder.record("wait", wait(wait_seconds))
    recorder.record(
        "take_screenshot.before_gui",
        take_screenshot(
            screenshot_id="smoke_before_gui",
            path=recorder.root / "smoke_before_gui.png",
            step_id=1,
            source_action_id="tool_smoke_non_gui",
        ),
    )


def _run_gui_smoke(*, recorder: SmokeRecorder, text: str, keep_open: bool) -> None:
    pid: int | None = None
    try:
        start_result = run_command(NOTEPAD_START_FOR_PID)
        recorder.record("run_command.open_notepad", start_result)
        pid = parse_first_int(start_result.stdout or start_result.stderr)

        recorder.record("wait.notepad_launch", wait(2))
        rect = _find_window_rect(pid=pid)
        points = points_from_rect(rect.left, rect.top, rect.right, rect.bottom)
        rect_payload = {
            "pid": pid,
            "rect": {
                "left": rect.left,
                "top": rect.top,
                "right": rect.right,
                "bottom": rect.bottom,
            },
            "points": asdict(points),
        }

        recorder.record("click.notepad_body", click(points.click_x, points.click_y), rect_payload)
        recorder.record("type_text.initial", type_text(text + "\n"), rect_payload)
        recorder.record("hotkey.select_all", hotkey("ctrl+a"), rect_payload)
        recorder.record("type_text.replace", type_text(text + " - replace path ok\n"), rect_payload)
        recorder.record(
            "drag.notepad_body",
            drag(points.drag_start_x, points.drag_start_y, points.drag_end_x, points.drag_end_y),
            rect_payload,
        )
        recorder.record(
            "take_screenshot.after_gui",
            take_screenshot(
                screenshot_id="smoke_after_gui",
                path=recorder.root / "smoke_after_gui.png",
                step_id=2,
                source_action_id="tool_smoke_gui",
            ),
            rect_payload,
        )
    except Exception as exc:
        recorder.record_error("gui_smoke", exc, {"pid": pid})
    finally:
        if pid is not None and not keep_open:
            recorder.record("run_command.cleanup_notepad", run_command(f"Stop-Process -Id {pid} -Force"))


def parse_first_int(text: str) -> int | None:
    match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


def points_from_rect(left: int, top: int, right: int, bottom: int) -> SmokePoints:
    width = max(right - left, 1)
    height = max(bottom - top, 1)
    click_x = left + width // 2
    click_y = top + height // 2
    drag_start_offset = _clamp(width // 3, 0, width - 1)
    drag_end_offset = _clamp((width * 2) // 3, drag_start_offset, width - 1)
    drag_y = top + height // 2
    return SmokePoints(
        click_x=click_x,
        click_y=click_y,
        drag_start_x=left + drag_start_offset,
        drag_start_y=drag_y,
        drag_end_x=left + drag_end_offset,
        drag_end_y=drag_y,
    )


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))


def _find_window_rect(*, pid: int | None):
    from computer_use_agent._vendor.windows_use import uia

    if pid is not None:
        window = uia.WindowControl(ProcessId=pid)
        if window.Exists(maxSearchSeconds=8):
            return window.BoundingRectangle

    window = uia.WindowControl(RegexName="(?i).*notepad.*")
    if window.Exists(maxSearchSeconds=8):
        return window.BoundingRectangle

    raise RuntimeError("Notepad window was not detected")


def _confirm_gui_smoke() -> bool:
    print(
        "This will open Notepad, take screenshots, click, type text, press Ctrl+A, drag the mouse, "
        "and then force-close the Notepad process."
    )
    print("Save any active work and avoid showing sensitive windows before continuing.")
    answer = input("Continue? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _to_payload(result: object) -> dict[str, Any]:
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(cast(Any, result))
    if isinstance(result, dict):
        return result
    return {"value": repr(result)}


def _payload_success(payload: dict[str, Any]) -> bool:
    value = payload.get("success")
    return bool(value) if isinstance(value, bool) else False


if __name__ == "__main__":
    raise SystemExit(main())
