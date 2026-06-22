from __future__ import annotations

import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .run_store import RunStore, mark_run_finished
from .runtime_state import RuntimeState, TerminalRunStatus, create_runtime_state
from .sample_tasks import build_completion_hint, find_demo_task
from .tools.run_command import CommandResult, PowerShellBackend, run_command


class TerminalMainAgent:
    def plan(self, user_request: str) -> tuple[str, ...]:
        matched = find_demo_task(user_request)
        if matched is None:
            return tuple()
        return matched.command_plan


class TerminalRuntime:
    def __init__(
        self,
        workspace: Path,
        runs_root: Path,
        command_backend: PowerShellBackend | None = None,
        agent: TerminalMainAgent | None = None,
        max_steps: int = 5,
    ) -> None:
        self.workspace = workspace
        self.runs_root = runs_root
        self.command_backend = command_backend or PowerShellBackend()
        self.agent = agent or TerminalMainAgent()
        self.max_steps = max_steps

    def run(self, user_request: str) -> RuntimeState:
        run_id = datetime.now(UTC).strftime("run_%Y%m%d_%H%M%S")
        run_dir = self.runs_root / run_id
        store = RunStore(run_dir)
        store.prepare()

        state = create_runtime_state(user_request=user_request, run_id=run_id, root_dir=run_dir)
        started_at = time.perf_counter()
        commands = self.agent.plan(user_request)

        store.append_trace(
            step_id=0,
            actor="runtime",
            event_type="run_initialized",
            payload={"task": user_request, "workspace": str(self.workspace)},
            status="success",
        )

        if not commands:
            state.errors.last_error_code = "TASK_NOT_SUPPORTED"
            state.errors.last_error_message = "No demo task matched the user request"
            mark_run_finished(state, TerminalRunStatus.FAILED, "No supported terminal demo task matched")
            state.metrics.runtime_seconds = round(time.perf_counter() - started_at, 3)
            store.append_trace(
                step_id=0,
                actor="main_agent",
                event_type="finish_request",
                payload={"reason": state.run.terminated_reason},
                status="failed",
            )
            store.write_summary(state)
            return state

        for step_id, command in enumerate(commands, start=1):
            if step_id > self.max_steps:
                state.errors.last_error_code = "MAX_STEPS_EXCEEDED"
                state.errors.last_error_message = "Reached max steps before completing task"
                mark_run_finished(state, TerminalRunStatus.ABORTED, "Maximum step limit reached")
                break

            state.run.current_step = step_id
            store.append_trace(
                step_id=step_id,
                actor="main_agent",
                event_type="agent_decision",
                payload={
                    "kind": "tool_call",
                    "tool_name": "run_command",
                    "tool_args": {"command": command},
                    "expected_observation": "command output available",
                },
                status="pending",
            )

            result = run_command(
                command=command,
                timeout_s=10,
                cwd=self.workspace,
                backend=self.command_backend,
            )
            artifact_paths = store.write_command_result(step_id=step_id, result=result)
            self._apply_command_result(state=state, step_id=step_id, result=result, artifact_paths=artifact_paths)
            store.append_trace(
                step_id=step_id,
                actor="tool_runtime",
                event_type="tool_execution",
                payload={"result": asdict(result)},
                status="success" if result.success else "failed",
                artifact_refs=[artifact_paths["command_result_id"]],
            )

            if not result.success:
                mark_run_finished(state, TerminalRunStatus.FAILED, f"Command failed at step {step_id}")
                break
        else:
            completion_hint = build_completion_hint(self.workspace, len(commands))
            mark_run_finished(state, TerminalRunStatus.SUCCESS, completion_hint)

        state.metrics.runtime_seconds = round(time.perf_counter() - started_at, 3)
        store.append_trace(
            step_id=state.run.current_step,
            actor="main_agent",
            event_type="finish_request",
            payload={
                "completion_claim": state.run.terminated_reason,
                "status": state.run.status,
            },
            status="success" if state.run.status == TerminalRunStatus.SUCCESS else "failed",
        )
        store.write_summary(state)
        return state

    def _apply_command_result(
        self,
        *,
        state: RuntimeState,
        step_id: int,
        result: CommandResult,
        artifact_paths: dict[str, str],
    ) -> None:
        state.metrics.step_count += 1
        state.metrics.tool_call_count += 1
        state.metrics.command_count += 1
        state.last_action.action_id = f"act_{step_id:04d}"
        state.last_action.actor = "tool_runtime"
        state.last_action.action_type = "run_command"
        state.last_action.action_args = {"command": result.command}
        state.last_action.result_status = "success" if result.success else "failed"
        state.last_action.result_summary = result.stdout.strip() or result.stderr.strip() or result.note
        state.last_action.artifact_refs = [artifact_paths["command_result_id"]]

        if result.success:
            state.errors.last_error_code = ""
            state.errors.last_error_message = ""
            state.errors.last_failed_tool = ""
        else:
            state.errors.last_error_code = "COMMAND_FAILED"
            state.errors.last_error_message = result.stderr.strip() or result.note
            state.errors.last_failed_tool = "run_command"
