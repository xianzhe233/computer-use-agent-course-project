from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path


class TerminalRunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass(slots=True)
class TaskState:
    user_request: str
    task_type: str = "terminal"
    goal_summary: str = ""
    completion_hints: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    sensitive_data_present: bool = False


@dataclass(slots=True)
class RunState:
    run_id: str
    created_at: str
    root_dir: str
    platform: str = "windows"
    status: TerminalRunStatus = TerminalRunStatus.RUNNING
    current_step: int = 0
    current_phase: str = "main_loop"
    terminated_reason: str = ""


@dataclass(slots=True)
class ControlState:
    max_steps: int = 0
    step_timeout_seconds: int = 10
    allowed_tools: list[str] = field(default_factory=list)
    terminated_reason: str = ""


@dataclass(slots=True)
class ObservationState:
    latest_screenshot_id: str = ""
    latest_screenshot_path: str = ""
    latest_command_result_id: str = ""
    active_window_title: str = ""
    desktop_resolution: dict[str, int] = field(default_factory=dict)
    last_observation_summary: str = ""


@dataclass(slots=True)
class ActionRecord:
    action_id: str
    action_type: str
    action_args: dict[str, object]
    result_status: str
    result_summary: str
    artifact_refs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LastActionState:
    action_id: str = ""
    actor: str = ""
    action_type: str = ""
    action_args: dict[str, object] = field(default_factory=dict)
    result_status: str = ""
    result_summary: str = ""
    artifact_refs: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MetricsState:
    step_count: int = 0
    tool_call_count: int = 0
    screenshot_count: int = 0
    command_count: int = 0
    rework_count: int = 0
    consecutive_failures: int = 0
    runtime_seconds: float = 0.0


@dataclass(slots=True)
class ErrorState:
    last_error_code: str = ""
    last_error_message: str = ""
    last_failed_tool: str = ""
    blocked: bool = False
    block_reason: str = ""


@dataclass(slots=True)
class RuntimeState:
    run: RunState
    task: TaskState
    control: ControlState = field(default_factory=ControlState)
    observation: ObservationState = field(default_factory=ObservationState)
    last_action: LastActionState = field(default_factory=LastActionState)
    metrics: MetricsState = field(default_factory=MetricsState)
    errors: ErrorState = field(default_factory=ErrorState)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def create_runtime_state(
    user_request: str,
    run_id: str,
    root_dir: Path,
    *,
    task_type: str = "terminal",
    allowed_tools: list[str] | None = None,
    max_steps: int = 0,
    step_timeout_seconds: int = 10,
) -> RuntimeState:
    created_at = datetime.now(UTC).isoformat()
    task = TaskState(user_request=user_request, task_type=task_type, goal_summary=user_request.strip())
    run = RunState(run_id=run_id, created_at=created_at, root_dir=str(root_dir))
    control = ControlState(
        max_steps=max_steps,
        step_timeout_seconds=step_timeout_seconds,
        allowed_tools=allowed_tools or [],
    )
    return RuntimeState(run=run, task=task, control=control)
