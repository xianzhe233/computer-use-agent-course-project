from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .runtime_state import RuntimeState, TerminalRunStatus
from .tools.element_location import ElementLocationResult
from .tools.run_command import CommandResult
from .tools.screenshot import ScreenshotResult


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.command_logs_dir = root / "command_logs"
        self.screenshots_dir = root / "screenshots"
        self.trace_path = root / "trace.jsonl"
        self.summary_path = root / "summary.json"
        self.command_index_path = self.command_logs_dir / "index.jsonl"
        self.screenshot_index_path = self.screenshots_dir / "index.jsonl"
        self.location_results_dir = root / "locations"
        self.location_index_path = self.location_results_dir / "index.jsonl"

    def prepare(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.command_logs_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        self.location_results_dir.mkdir(parents=True, exist_ok=True)

    def write_command_result(self, step_id: int, result: CommandResult) -> dict[str, str]:
        command_result_id = f"cmd_{step_id:04d}"
        stdout_path = self.command_logs_dir / f"{command_result_id}.stdout.log"
        stderr_path = self.command_logs_dir / f"{command_result_id}.stderr.log"
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")

        index_record = {
            "command_result_id": command_result_id,
            "step_id": step_id,
            "command": result.command,
            "exit_code": result.exit_code,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
            "duration_ms": result.duration_ms,
        }
        with self.command_index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(index_record, ensure_ascii=False) + "\n")

        return {
            "command_result_id": command_result_id,
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        }

    def write_screenshot_result(self, result: ScreenshotResult, description: str = "") -> dict[str, str]:
        screenshot_path = Path(result.path)
        screenshot_ref = f"screenshot:{result.screenshot_id}"
        index_record = {
            "screenshot_id": result.screenshot_id,
            "path": str(screenshot_path),
            "step_id": result.step_id,
            "timestamp": result.timestamp,
            "source_action_id": result.source_action_id,
            "description": description,
            "resolution": {"width": result.width, "height": result.height},
            "success": result.success,
        }
        with self.screenshot_index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(index_record, ensure_ascii=False) + "\n")

        return {
            "screenshot_id": result.screenshot_id,
            "screenshot_path": str(screenshot_path),
            "artifact_ref": screenshot_ref,
        }

    def write_location_result(self, step_id: int, result: ElementLocationResult) -> dict[str, str]:
        location_result_id = f"loc_{step_id:04d}"
        result_path = self.location_results_dir / f"{location_result_id}.json"
        result_path.write_text(json.dumps(asdict(result), ensure_ascii=False, indent=2), encoding="utf-8")

        index_record = {
            "location_result_id": location_result_id,
            "step_id": step_id,
            "query": result.query,
            "screenshot_id": result.screenshot_id,
            "screenshot_path": result.screenshot_path,
            "success": result.success,
            "point": list(result.point) if result.point else None,
            "confidence": result.confidence,
            "source": result.source,
            "reason": result.reason,
            "result_path": str(result_path),
            "timestamp": result.timestamp,
        }
        with self.location_index_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(index_record, ensure_ascii=False) + "\n")

        return {
            "location_result_id": location_result_id,
            "result_path": str(result_path),
            "artifact_ref": f"location:{location_result_id}",
        }

    def append_trace(
        self,
        *,
        step_id: int,
        actor: str,
        event_type: str,
        payload: dict[str, Any],
        status: str,
        artifact_refs: list[str] | None = None,
    ) -> None:
        event = {
            "event_id": f"evt_{step_id:04d}_{event_type}",
            "step_id": step_id,
            "timestamp": datetime.now(UTC).isoformat(),
            "actor": actor,
            "phase": "main_loop",
            "event_type": event_type,
            "payload": payload,
            "status": status,
            "artifact_refs": artifact_refs or [],
        }
        with self.trace_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def write_summary(self, state: RuntimeState) -> None:
        summary = {
            "run_id": state.run.run_id,
            "task": state.task.user_request,
            "final_status": state.run.status,
            "final_reason": state.run.terminated_reason,
            "step_count": state.metrics.step_count,
            "tool_call_count": state.metrics.tool_call_count,
            "screenshot_count": state.metrics.screenshot_count,
            "command_count": state.metrics.command_count,
            "started_at": state.run.created_at,
            "ended_at": datetime.now(UTC).isoformat(),
            "runtime_seconds": state.metrics.runtime_seconds,
            "state": asdict(state),
        }
        self.summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_run_finished(state: RuntimeState, status: TerminalRunStatus, reason: str) -> None:
    state.run.status = status
    state.run.current_phase = "terminated"
    state.run.terminated_reason = reason
