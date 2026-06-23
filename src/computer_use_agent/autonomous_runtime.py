from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .autonomous_runtime_helpers import AutonomousComputerRuntimeHelpers
from .graph_runtime import AgentGraphState, compile_linear_agent_graph
from .run_store import RunStore, mark_run_finished
from .runtime_state import RuntimeState, TerminalRunStatus, create_runtime_state
from .computer_agent import AutonomousComputerAgent, TerminalAgentDecision, TerminalAgentProtocolError
from .tools import ElementLocatorBackend, GuiAutomationBackend, PowerShellBackend, ScreenshotBackend

ProgressCallback = Callable[[str], None]

AUTONOMOUS_COMPUTER_TOOLS: list[str] = [
    "run_command",
    "take_screenshot",
    "view_screenshot",
    "click",
    "double_click",
    "right_click",
    "move_mouse",
    "hover",
    "type_text",
    "hotkey",
    "scroll",
    "drag",
    "open_app",
    "switch_app",
    "focus_window",
    "wait",
]



class AutonomousComputerRuntime(AutonomousComputerRuntimeHelpers):
    """Autonomous runtime with terminal + GUI tools and no examiner loop, driven by LangGraph."""

    def __init__(
        self,
        workspace: Path,
        runs_root: Path,
        *,
        command_backend: PowerShellBackend | None = None,
        screenshot_backend: ScreenshotBackend | None = None,
        gui_backend: GuiAutomationBackend | None = None,
        element_locator_backend: ElementLocatorBackend | None = None,
        agent: AutonomousComputerAgent | None = None,
        max_steps: int = 50,
        step_timeout_seconds: int = 180,
        max_consecutive_failures: int = 4,
        model_config_path: Path = Path("config/models.local.json"),
        model_role: str = "mainAgent",
        locator_role: str = "locator",
        capture_initial_screenshot: bool = True,
        screenshot_after_gui_action: bool = True,
        post_gui_screenshot_delay_seconds: float = 0.6,
        progress_callback: ProgressCallback | None = None,
        progress_output_char_limit: int = 1200,
    ) -> None:
        self.workspace = workspace
        self.runs_root = runs_root
        self.command_backend = command_backend or PowerShellBackend()
        self.screenshot_backend = screenshot_backend
        self.gui_backend = gui_backend
        self.element_locator_backend: ElementLocatorBackend | None = element_locator_backend
        self.agent: AutonomousComputerAgent | None = agent
        self.model_config_path = model_config_path
        self.model_role = model_role
        self.locator_role = locator_role
        self.max_steps = max_steps
        self.step_timeout_seconds = step_timeout_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.capture_initial_screenshot = capture_initial_screenshot
        self.screenshot_after_gui_action = screenshot_after_gui_action
        self.post_gui_screenshot_delay_seconds = max(0.0, post_gui_screenshot_delay_seconds)
        self.progress_callback = progress_callback
        self.progress_output_char_limit = progress_output_char_limit

    def run(self, user_request: str) -> RuntimeState:
        run_id = datetime.now(UTC).strftime("run_%Y%m%d_%H%M%S_%f")
        run_dir = self.runs_root / run_id
        store = RunStore(run_dir)
        store.prepare()

        state = create_runtime_state(
            user_request=user_request,
            run_id=run_id,
            root_dir=run_dir,
            task_type="hybrid",
            allowed_tools=list(AUTONOMOUS_COMPUTER_TOOLS),
            max_steps=self.max_steps,
            step_timeout_seconds=self.step_timeout_seconds,
        )
        started_at = time.perf_counter()
        history: list[dict[str, object]] = []

        store.append_trace(
            step_id=0,
            actor="runtime",
            event_type="run_initialized",
            payload={
                "task": user_request,
                "workspace": str(self.workspace),
                "mode": "autonomous_computer",
                "allowed_tools": state.control.allowed_tools,
                "examiner_enabled": False,
                "locator_role": self.locator_role,
            },
            status="success",
        )
        self._emit_run_header(run_id=run_id, user_request=user_request, state=state)
        if self.capture_initial_screenshot:
            self._record_initial_screenshot(state=state, store=store)

        app = self._build_graph(state=state, store=store, history=history)
        app.invoke({"step_id": 0, "terminated": False, "artifact_refs": []})

        state.control.terminated_reason = state.run.terminated_reason
        state.metrics.runtime_seconds = round(time.perf_counter() - started_at, 3)
        if state.run.status != TerminalRunStatus.SUCCESS:
            store.append_trace(
                step_id=state.run.current_step,
                actor="runtime",
                event_type="termination",
                payload={
                    "status": state.run.status,
                    "reason": state.run.terminated_reason,
                    "errors": asdict(state.errors),
                },
                status="failed" if state.run.status == TerminalRunStatus.FAILED else "aborted",
            )
        store.write_summary(state)
        self._emit_run_footer(state=state, run_dir=run_dir)
        return state

    def _record_initial_screenshot(self, *, state: RuntimeState, store: RunStore) -> None:
        screenshot_result = self._capture_and_record_screenshot(
            state=state,
            store=store,
            step_id=0,
            action_id="runtime_initial_screenshot",
            description="初始截图",
        )
        screenshot_ref = f"screenshot:{screenshot_result.screenshot_id}"
        state.metrics.screenshot_count += 1
        state.observation.latest_screenshot_id = screenshot_result.screenshot_id
        state.observation.latest_screenshot_path = str(screenshot_result.path)
        state.observation.selected_screenshot_ids = [screenshot_result.screenshot_id]
        state.observation.selected_screenshot_paths = [str(screenshot_result.path)]
        state.observation.desktop_resolution = {
            "width": screenshot_result.width,
            "height": screenshot_result.height,
        }
        state.observation.last_observation_summary = "初始截图"
        store.append_trace(
            step_id=0,
            actor="runtime",
            event_type="initial_observation",
            payload={
                "description": "初始截图",
                "result": asdict(screenshot_result),
            },
            status="success",
            artifact_refs=[screenshot_ref],
        )
        self._emit(f"Observe   : captured initial screenshot ({screenshot_result.screenshot_id})")

    def _build_graph(
        self,
        *,
        state: RuntimeState,
        store: RunStore,
        history: list[dict[str, object]],
    ):
        def plan_node(graph_state: AgentGraphState) -> AgentGraphState:
            next_step = int(graph_state.get("step_id", 0)) + 1
            if next_step > self.max_steps:
                state.errors.last_error_code = "MAX_STEPS_EXCEEDED"
                state.errors.last_error_message = "Reached max steps before finish_request"
                mark_run_finished(state, TerminalRunStatus.ABORTED, "Maximum step limit reached")
                self._emit("Abort     : maximum step limit reached")
                return {"step_id": next_step - 1, "terminated": True, "route": "terminated"}

            state.run.current_step = next_step
            action_id = f"act_{next_step:04d}"
            self._emit_step_header(step_id=next_step, title="planning")
            self._emit("Planning  : asking model for the next action")
            try:
                decision = self._agent().decide(state=state, workspace=self.workspace, history=history)
            except (FileNotFoundError, TerminalAgentProtocolError, RuntimeError, ValueError) as exc:
                self._record_agent_error(
                    state=state,
                    store=store,
                    step_id=next_step,
                    action_id=action_id,
                    error=exc,
                )
                self._emit(f"Decision  : failed ({type(exc).__name__}: {exc})")
                mark_run_finished(state, TerminalRunStatus.FAILED, "Agent decision failed")
                return {
                    "step_id": next_step,
                    "action_id": action_id,
                    "terminated": True,
                    "route": "terminated",
                }

            store.append_trace(
                step_id=next_step,
                actor="main_agent",
                event_type="agent_decision",
                payload=decision.to_trace_payload(),
                status="success",
            )
            if decision.kind == "finish_request":
                return {
                    "step_id": next_step,
                    "action_id": action_id,
                    "decision": decision,
                    "decision_kind": decision.kind,
                    "route": "finish",
                }
            self._emit_tool_call(decision=decision)
            return {
                "step_id": next_step,
                "action_id": action_id,
                "decision": decision,
                "decision_kind": decision.kind,
                "route": "validate",
            }

        def validate_node(graph_state: AgentGraphState) -> AgentGraphState:
            decision = cast(TerminalAgentDecision, graph_state["decision"])
            step_id = int(graph_state["step_id"])
            action_id = str(graph_state["action_id"])
            validation_error = self._validate_tool_call(state=state, decision=decision)
            if validation_error is not None:
                self._record_validation_error(
                    state=state,
                    store=store,
                    step_id=step_id,
                    action_id=action_id,
                    decision=decision,
                    validation_error=validation_error,
                )
                self._emit(f"Validation: failed ({validation_error['message']})")
                history.append(
                    self._validation_history_item(
                        step_id=step_id,
                        decision=decision,
                        validation_error=validation_error,
                    )
                )
                if validation_error["code"] == "RISK_BLOCKED":
                    mark_run_finished(state, TerminalRunStatus.FAILED, validation_error["message"])
                    return {"step_id": step_id, "terminated": True, "route": "terminated"}
                if state.metrics.consecutive_failures >= self.max_consecutive_failures:
                    state.errors.blocked = True
                    state.errors.block_reason = "连续工具校验失败次数达到上限，停止继续自主执行。"
                    mark_run_finished(
                        state,
                        TerminalRunStatus.ABORTED,
                        "Maximum consecutive validation failures reached",
                    )
                    self._emit("Abort     : maximum consecutive validation failures reached")
                    return {"step_id": step_id, "terminated": True, "route": "terminated"}
                return {"step_id": step_id, "route": "loop"}

            store.append_trace(
                step_id=step_id,
                actor="runtime",
                event_type="tool_validation",
                payload={"tool_name": decision.tool_name, "tool_args": decision.tool_args},
                status="success",
            )
            return {"step_id": step_id, "route": "execute"}

        def execute_node(graph_state: AgentGraphState) -> AgentGraphState:
            decision = cast(TerminalAgentDecision, graph_state["decision"])
            step_id = int(graph_state["step_id"])
            action_id = str(graph_state["action_id"])
            self._emit("Running   : executing tool")
            result, artifact_refs = self._execute_tool_step(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                decision=decision,
            )
            self._apply_tool_result(
                state=state,
                action_id=action_id,
                decision=decision,
                result=result,
                artifact_refs=artifact_refs,
            )
            history.append(
                self._history_item(
                    step_id=step_id,
                    decision=decision,
                    result=result,
                    artifact_refs=artifact_refs,
                )
            )
            self._emit_tool_result(result=result, artifact_refs=artifact_refs)
            store.append_trace(
                step_id=step_id,
                actor="tool_runtime",
                event_type="tool_execution",
                payload={"result": result},
                status="success" if result["success"] else "failed",
                artifact_refs=artifact_refs,
            )
            store.append_trace(
                step_id=step_id,
                actor="runtime",
                event_type="state_update",
                payload={
                    "last_action": asdict(state.last_action),
                    "observation": asdict(state.observation),
                    "metrics": asdict(state.metrics),
                    "errors": asdict(state.errors),
                },
                status="success" if result["success"] else "failed",
                artifact_refs=artifact_refs,
            )
            if state.metrics.consecutive_failures >= self.max_consecutive_failures:
                state.errors.blocked = True
                state.errors.block_reason = "连续工具失败次数达到上限，停止继续自主执行。"
                mark_run_finished(
                    state,
                    TerminalRunStatus.ABORTED,
                    "Maximum consecutive tool failures reached",
                )
                self._emit("Abort     : maximum consecutive tool failures reached")
                return {"step_id": step_id, "terminated": True, "route": "terminated"}
            return {"step_id": step_id, "artifact_refs": artifact_refs, "route": "loop"}

        def finish_node(graph_state: AgentGraphState) -> AgentGraphState:
            decision = cast(TerminalAgentDecision, graph_state["decision"])
            step_id = int(graph_state["step_id"])
            action_id = str(graph_state["action_id"])
            completion_claim = decision.completion_claim.strip() or "Agent requested finish"
            self._emit_finish_request(decision=decision, completion_claim=completion_claim)
            mark_run_finished(state, TerminalRunStatus.SUCCESS, completion_claim)
            state.last_action.action_id = action_id
            state.last_action.actor = "main_agent"
            state.last_action.action_type = "finish_request"
            state.last_action.action_args = {
                "completion_claim": completion_claim,
                "supporting_evidence": decision.supporting_evidence,
                "remaining_uncertainty": decision.remaining_uncertainty,
            }
            state.last_action.result_status = "success"
            state.last_action.result_summary = completion_claim
            state.last_action.artifact_refs = decision.supporting_evidence
            store.append_trace(
                step_id=step_id,
                actor="main_agent",
                event_type="finish_request",
                payload=state.last_action.action_args,
                status="success",
                artifact_refs=decision.supporting_evidence,
            )
            return {"step_id": step_id, "terminated": True, "route": "end"}

        return compile_linear_agent_graph(
            plan_node=plan_node,
            validate_node=validate_node,
            execute_node=execute_node,
            finish_node=finish_node,
            route_after_plan=lambda graph_state: cast(Any, graph_state["route"]),
            route_after_validate=lambda graph_state: cast(Any, graph_state["route"]),
            route_after_execute=lambda graph_state: cast(Any, graph_state["route"]),
        )


