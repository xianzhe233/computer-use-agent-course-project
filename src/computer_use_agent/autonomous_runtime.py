from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from .autonomous_terminal_runtime import (
    _blocked_command_reason,
    _format_terminal_output,
    _indent_block,
    _summarize_command_result,
)
from .graph_runtime import AgentGraphState, compile_linear_agent_graph
from .run_store import RunStore, mark_run_finished
from .runtime_state import RuntimeState, TerminalRunStatus, create_runtime_state
from .computer_agent import (
    AutonomousComputerAgent,
    LLMComputerAgent,
    TerminalAgentDecision,
    TerminalAgentProtocolError,
    truncate_text,
)
from .tools import (
    CommandResult,
    ElementLocationResult,
    ElementLocatorBackend,
    GuiActionResult,
    GuiAutomationBackend,
    PowerShellBackend,
    ScreenshotBackend,
    ScreenshotResult,
    click,
    create_default_element_locator_backend,
    double_click,
    drag,
    focus_window,
    hotkey,
    hover,
    locate_element,
    move_mouse,
    open_app,
    right_click,
    run_command,
    scroll,
    switch_app,
    take_screenshot,
    type_text,
    wait,
)

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

AUTO_LOCATE_TOOL_NAMES = {"click", "double_click", "right_click", "move_mouse", "hover", "type_text", "scroll"}


class AutonomousComputerRuntime:
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
        self.element_locator_backend = element_locator_backend
        self.agent = agent
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

    def _agent(self) -> AutonomousComputerAgent:
        if self.agent is None:
            self.agent = LLMComputerAgent.from_config_file(
                config_path=self.model_config_path,
                role=self.model_role,
            )
        return self.agent

    def _locator_backend(self) -> ElementLocatorBackend:
        if self.element_locator_backend is None:
            self.element_locator_backend = create_default_element_locator_backend(
                model_config_path=self.model_config_path,
                role=self.locator_role,
                timeout_s=self.step_timeout_seconds,
            )
        return self.element_locator_backend

    def _execute_tool_step(
        self,
        *,
        state: RuntimeState,
        store: RunStore,
        step_id: int,
        action_id: str,
        decision: TerminalAgentDecision,
    ) -> tuple[dict[str, Any], list[str]]:
        tool_name = decision.tool_name
        tool_args = decision.tool_args
        artifact_refs: list[str] = []

        if tool_name == "run_command":
            result = run_command(
                command=str(tool_args["command"]),
                timeout_s=self.step_timeout_seconds,
                cwd=self.workspace,
                backend=self.command_backend,
            )
            artifact_paths = store.write_command_result(step_id=step_id, result=result)
            command_result_id = artifact_paths["command_result_id"]
            artifact_refs.append(f"command:{command_result_id}")
            result_dict = asdict(result)
            result_dict["tool_name"] = "run_command"
            return result_dict, artifact_refs

        if tool_name == "take_screenshot":
            screenshot_result = self._capture_and_record_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                description=str(tool_args.get("description", "manual observation")),
            )
            artifact_refs.append(f"screenshot:{screenshot_result.screenshot_id}")
            result_dict = asdict(screenshot_result)
            result_dict["tool_name"] = "take_screenshot"
            return result_dict, artifact_refs

        if tool_name == "view_screenshot":
            result_dict = self._view_screenshot_result(
                state=state,
                screenshot_ids=self._view_screenshot_ids(tool_args),
            )
            if result_dict["success"]:
                artifact_refs.extend(
                    f"screenshot:{screenshot_id}" for screenshot_id in result_dict.get("screenshot_ids", [])
                )
            return result_dict, artifact_refs

        auto_locate_result: ElementLocationResult | None = None
        if tool_name in AUTO_LOCATE_TOOL_NAMES:
            auto_locate_result = self._maybe_auto_locate(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                query=self._auto_locate_query(decision.tool_args),
                tool_name=tool_name,
            )
            if auto_locate_result is not None:
                location_artifacts = store.write_location_result(step_id=step_id, result=auto_locate_result)
                artifact_refs.extend(auto_locate_result.artifacts)
                artifact_refs.append(location_artifacts["artifact_ref"])
                if not auto_locate_result.success:
                    failure_result = asdict(auto_locate_result)
                    failure_result["tool_name"] = tool_name
                    failure_result["result"] = {"auto_locate_query": self._auto_locate_query(decision.tool_args)}
                    return failure_result, artifact_refs

        if tool_name in {"click", "double_click", "right_click"}:
            click_x, click_y, resolved_from = self._resolve_click_coordinates(
                state=state,
                tool_args=tool_args,
                auto_locate_result=auto_locate_result,
            )
            if tool_name == "double_click":
                gui_result = double_click(click_x, click_y, backend=self.gui_backend)
            elif tool_name == "right_click":
                gui_result = right_click(click_x, click_y, backend=self.gui_backend)
            else:
                gui_result = click(
                    click_x,
                    click_y,
                    button=self._button_value(tool_args.get("button", "left")),
                    clicks=self._optional_int(tool_args.get("clicks"), default=1) or 1,
                    backend=self.gui_backend,
                )
            gui_result.result["x"] = click_x
            gui_result.result["y"] = click_y
            if resolved_from:
                gui_result.result["resolved_from"] = resolved_from
            result_dict, artifact_refs = self._attach_post_action_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                tool_name=tool_name,
                gui_result=gui_result,
                artifact_refs=artifact_refs,
            )
            return self._attach_location_metadata(result_dict, auto_locate_result), artifact_refs

        if tool_name in {"move_mouse", "hover"}:
            move_x, move_y, resolved_from = self._resolve_optional_point(
                state=state,
                tool_args=tool_args,
                keys=("x", "y"),
                query_keys=("target_query", "query"),
                auto_locate_result=auto_locate_result,
            )
            if move_x is None or move_y is None:
                raise ValueError(f"{tool_name} requires x/y or target_query")
            gui_result = (
                hover(
                    move_x,
                    move_y,
                    duration_ms=self._optional_int(tool_args.get("duration_ms"), default=500) or 500,
                    backend=self.gui_backend,
                )
                if tool_name == "hover"
                else move_mouse(move_x, move_y, backend=self.gui_backend)
            )
            if resolved_from:
                gui_result.result["resolved_from"] = resolved_from
            result_dict, artifact_refs = self._attach_post_action_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                tool_name=tool_name,
                gui_result=gui_result,
                artifact_refs=artifact_refs,
            )
            return self._attach_location_metadata(result_dict, auto_locate_result), artifact_refs

        if tool_name == "type_text":
            type_x, type_y, resolved_from = self._resolve_optional_point(
                state=state,
                tool_args=tool_args,
                keys=("x", "y"),
                query_keys=("target_query", "query"),
                auto_locate_result=auto_locate_result,
            )
            gui_result = type_text(
                str(tool_args["text"]),
                x=type_x,
                y=type_y,
                clear=bool(tool_args.get("clear", False)),
                caret_position=self._caret_position(tool_args.get("caret_position", "idle")),
                press_enter=bool(tool_args.get("press_enter", False)),
                backend=self.gui_backend,
            )
            if resolved_from:
                gui_result.result["resolved_from"] = resolved_from
            result_dict, artifact_refs = self._attach_post_action_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                tool_name=tool_name,
                gui_result=gui_result,
                artifact_refs=artifact_refs,
            )
            return self._attach_location_metadata(result_dict, auto_locate_result), artifact_refs

        if tool_name == "hotkey":
            gui_result = hotkey(str(tool_args["shortcut"]), backend=self.gui_backend)
            return self._attach_post_action_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                tool_name=tool_name,
                gui_result=gui_result,
                artifact_refs=artifact_refs,
            )

        if tool_name == "scroll":
            scroll_x, scroll_y, resolved_from = self._resolve_optional_point(
                state=state,
                tool_args=tool_args,
                keys=("x", "y"),
                query_keys=("target_query", "query"),
                auto_locate_result=auto_locate_result,
            )
            gui_result = scroll(
                direction=self._scroll_direction(tool_args.get("direction", "down")),
                amount=self._optional_int(tool_args.get("amount", tool_args.get("wheel_times")), default=1) or 1,
                x=scroll_x,
                y=scroll_y,
                backend=self.gui_backend,
            )
            if resolved_from:
                gui_result.result["resolved_from"] = resolved_from
            result_dict, artifact_refs = self._attach_post_action_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                tool_name=tool_name,
                gui_result=gui_result,
                artifact_refs=artifact_refs,
            )
            return self._attach_location_metadata(result_dict, auto_locate_result), artifact_refs

        if tool_name == "drag":
            start_query = str(tool_args.get("start_query", "")).strip()
            end_query = str(tool_args.get("target_query", tool_args.get("end_query", tool_args.get("query", "")))).strip()
            start_location = self._maybe_auto_locate(
                state=state,
                store=store,
                step_id=step_id,
                action_id=f"{action_id}_drag_start",
                query=start_query,
                tool_name=tool_name,
            )
            if start_location is not None:
                start_artifacts = store.write_location_result(step_id=step_id, result=start_location)
                artifact_refs.extend(start_location.artifacts)
                artifact_refs.append(start_artifacts["artifact_ref"])
                if not start_location.success:
                    failure_result = asdict(start_location)
                    failure_result["tool_name"] = tool_name
                    failure_result["result"] = {"auto_locate_query": start_query}
                    return failure_result, artifact_refs

            end_location = self._maybe_auto_locate(
                state=state,
                store=store,
                step_id=step_id,
                action_id=f"{action_id}_drag_end",
                query=end_query,
                tool_name=tool_name,
            )
            if end_location is not None:
                end_artifacts = store.write_location_result(step_id=step_id, result=end_location)
                artifact_refs.extend(end_location.artifacts)
                artifact_refs.append(end_artifacts["artifact_ref"])
                if not end_location.success:
                    failure_result = asdict(end_location)
                    failure_result["tool_name"] = tool_name
                    failure_result["result"] = {"auto_locate_query": end_query}
                    return failure_result, artifact_refs

            start_x, start_y, start_source = self._resolve_drag_point(
                state=state,
                tool_args=tool_args,
                x_key="x1",
                y_key="y1",
                query=start_query,
                location_result=start_location,
                source_name="start_query",
            )
            end_x, end_y, end_source = self._resolve_drag_point(
                state=state,
                tool_args=tool_args,
                x_key="x2",
                y_key="y2",
                query=end_query,
                location_result=end_location,
                source_name="target_query",
            )
            gui_result = drag(
                start_x,
                start_y,
                end_x,
                end_y,
                backend=self.gui_backend,
            )
            if start_source:
                gui_result.result["start_resolved_from"] = start_source
            if end_source:
                gui_result.result["end_resolved_from"] = end_source
            result_dict, artifact_refs = self._attach_post_action_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                tool_name=tool_name,
                gui_result=gui_result,
                artifact_refs=artifact_refs,
            )
            return self._attach_location_metadata(result_dict, end_location or start_location), artifact_refs

        if tool_name in {"open_app", "switch_app", "focus_window"}:
            if tool_name == "open_app":
                gui_result = open_app(str(tool_args["name"]), backend=self.gui_backend)
                state.observation.active_window_title = str(tool_args["name"])
            elif tool_name == "switch_app":
                gui_result = switch_app(str(tool_args["name"]), backend=self.gui_backend)
                state.observation.active_window_title = str(tool_args["name"])
            else:
                window_title = str(tool_args.get("title", tool_args.get("name", "")))
                gui_result = focus_window(window_title, backend=self.gui_backend)
                state.observation.active_window_title = window_title
            return self._attach_post_action_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                tool_name=tool_name,
                gui_result=gui_result,
                artifact_refs=artifact_refs,
            )

        if tool_name == "wait":
            wait_result = wait(self._required_int(tool_args, "seconds"))
            return asdict(wait_result), artifact_refs

        raise ValueError(f"Unsupported autonomous action: {tool_name}")

    def _attach_post_action_screenshot(
        self,
        *,
        state: RuntimeState,
        store: RunStore,
        step_id: int,
        action_id: str,
        tool_name: str,
        gui_result: GuiActionResult,
        artifact_refs: list[str],
    ) -> tuple[dict[str, Any], list[str]]:
        result_dict = asdict(gui_result)
        if self.screenshot_after_gui_action and gui_result.success:
            if self.post_gui_screenshot_delay_seconds > 0:
                time.sleep(self.post_gui_screenshot_delay_seconds)
            screenshot_result = self._capture_and_record_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                description=f"after {tool_name}",
            )
            screenshot_ref = f"screenshot:{screenshot_result.screenshot_id}"
            artifact_refs.append(screenshot_ref)
            result_dict["artifacts"] = [screenshot_ref]
        return result_dict, artifact_refs

    def _capture_and_record_screenshot(
        self,
        *,
        state: RuntimeState,
        store: RunStore,
        step_id: int,
        action_id: str,
        description: str,
    ) -> ScreenshotResult:
        screenshot_id = f"ss_{state.metrics.screenshot_count + 1:04d}"
        screenshot_path = store.screenshots_dir / f"{screenshot_id}.png"
        screenshot_result = take_screenshot(
            screenshot_id=screenshot_id,
            path=screenshot_path,
            step_id=step_id,
            source_action_id=action_id,
            backend=self.screenshot_backend,
        )
        store.write_screenshot_result(screenshot_result, description=description)
        return screenshot_result

    def _view_screenshot_result(self, *, state: RuntimeState, screenshot_ids: list[str]) -> dict[str, Any]:
        started_at = time.perf_counter()
        viewed_at = datetime.now(UTC).isoformat()
        normalized_ids = [screenshot_id.strip() for screenshot_id in screenshot_ids if screenshot_id.strip()]
        if not normalized_ids:
            return {
                "tool_name": "view_screenshot",
                "screenshot_ids": [],
                "screenshots": [],
                "success": False,
                "duration_ms": int((time.perf_counter() - started_at) * 1000),
                "timestamp": viewed_at,
                "note": "screenshot_ids must not be empty",
                "error": {"code": "INVALID_SCREENSHOT_ID", "message": "view_screenshot requires non-empty screenshot_ids"},
            }

        screenshots: list[dict[str, Any]] = []
        for screenshot_id in normalized_ids:
            screenshot_path = Path(state.run.root_dir) / "screenshots" / f"{screenshot_id}.png"
            if not screenshot_path.exists():
                return {
                    "tool_name": "view_screenshot",
                    "screenshot_ids": normalized_ids,
                    "screenshots": screenshots,
                    "success": False,
                    "duration_ms": int((time.perf_counter() - started_at) * 1000),
                    "timestamp": viewed_at,
                    "note": "screenshot not found",
                    "error": {
                        "code": "SCREENSHOT_NOT_FOUND",
                        "message": f"Screenshot not found: {screenshot_path}",
                    },
                }
            width, height = _read_image_size(screenshot_path)
            screenshots.append(
                {
                    "screenshot_id": screenshot_id,
                    "path": str(screenshot_path),
                    "width": width,
                    "height": height,
                }
            )

        return {
            "tool_name": "view_screenshot",
            "screenshot_ids": normalized_ids,
            "screenshots": screenshots,
            "success": True,
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
            "timestamp": viewed_at,
            "note": "selected screenshots for visual context",
            "error": None,
        }

    def _apply_tool_result(
        self,
        *,
        state: RuntimeState,
        action_id: str,
        decision: TerminalAgentDecision,
        result: dict[str, Any],
        artifact_refs: list[str],
    ) -> None:
        success = bool(result["success"])
        previous_latest_screenshot_id = state.observation.latest_screenshot_id
        state.metrics.step_count += 1
        state.metrics.tool_call_count += 1
        if success:
            state.metrics.consecutive_failures = 0
        else:
            state.metrics.consecutive_failures += 1

        if decision.tool_name == "run_command":
            state.metrics.command_count += 1
            state.observation.latest_command_result_id = self._artifact_id(
                artifact_refs,
                prefix="command:",
            )
            command_result = CommandResult(
                command=str(result.get("command", "")),
                stdout=str(result.get("stdout", "")),
                stderr=str(result.get("stderr", "")),
                exit_code=int(result.get("exit_code", 0)),
                success=success,
                duration_ms=int(result.get("duration_ms", 0)),
                timed_out=bool(result.get("timed_out", False)),
                note=str(result.get("note", "")),
            )
            result_summary = _summarize_command_result(command_result)
        elif decision.tool_name == "take_screenshot":
            state.metrics.screenshot_count += 1
            state.observation.latest_screenshot_id = str(result["screenshot_id"])
            state.observation.latest_screenshot_path = str(result["path"])
            state.observation.selected_screenshot_ids = [state.observation.latest_screenshot_id]
            state.observation.selected_screenshot_paths = [state.observation.latest_screenshot_path]
            state.observation.desktop_resolution = {
                "width": int(result.get("width", 0)),
                "height": int(result.get("height", 0)),
            }
            result_summary = str(result.get("note") or result.get("path"))
        elif decision.tool_name == "view_screenshot":
            if success:
                screenshots = result.get("screenshots", [])
                state.observation.selected_screenshot_ids = self._string_list(result.get("screenshot_ids", []))
                state.observation.selected_screenshot_paths = [
                    str(item.get("path", "")) for item in screenshots if isinstance(item, dict) and item.get("path")
                ]
            result_summary = str(result.get("note") or "selected screenshots for visual context")
        elif self._artifact_ref(artifact_refs, prefix="location:") is not None:
            self._update_location_observation_from_result(state=state, result=result, artifact_refs=artifact_refs)
            if decision.tool_name == "locate_element":
                result_summary = str(result.get("reason") or "located element candidate")
            else:
                tool_error = result.get("error") or {}
                if result.get("success"):
                    result_summary = str(result.get("note") or result.get("reason") or f"{decision.tool_name} executed")
                else:
                    result_summary = str(tool_error.get("message") or result.get("reason") or "auto locate failed")
        else:
            result_summary = self._generic_gui_result_summary(decision=decision, result=result)

        latest_screenshot_ref = self._artifact_ref(artifact_refs, prefix="screenshot:")
        if latest_screenshot_ref is not None and decision.tool_name not in {"take_screenshot", "view_screenshot"}:
            latest_screenshot_id = latest_screenshot_ref.split(":", 1)[1]
            if latest_screenshot_id != previous_latest_screenshot_id:
                state.metrics.screenshot_count += 1
            latest_screenshot_path = str(
                Path(state.run.root_dir)
                / "screenshots"
                / f"{latest_screenshot_id}.png"
            )
            state.observation.latest_screenshot_id = latest_screenshot_id
            state.observation.latest_screenshot_path = latest_screenshot_path
            state.observation.selected_screenshot_ids = [latest_screenshot_id]
            state.observation.selected_screenshot_paths = [latest_screenshot_path]

        state.observation.last_observation_summary = result_summary
        state.last_action.action_id = action_id
        state.last_action.actor = "tool_runtime"
        state.last_action.action_type = decision.tool_name
        state.last_action.action_args = decision.tool_args
        state.last_action.result_status = "success" if success else "failed"
        state.last_action.result_summary = result_summary
        state.last_action.artifact_refs = artifact_refs

        if success:
            state.errors.last_error_code = ""
            state.errors.last_error_message = ""
            state.errors.last_failed_tool = ""
            state.errors.blocked = False
            state.errors.block_reason = ""
        else:
            error = result.get("error") or {}
            state.errors.last_error_code = str(error.get("code", "TOOL_FAILED"))
            state.errors.last_error_message = str(error.get("message", result_summary))
            state.errors.last_failed_tool = decision.tool_name
            if self._artifact_ref(artifact_refs, prefix="location:") is not None and state.metrics.consecutive_location_failures >= 2:
                state.errors.blocked = True
                state.errors.block_reason = "连续定位失败，禁止继续盲点；请重新截图、等待或放弃当前方案。"

    def _validate_tool_call(
        self,
        *,
        state: RuntimeState,
        decision: TerminalAgentDecision,
    ) -> dict[str, str] | None:
        if decision.tool_name not in state.control.allowed_tools:
            return {
                "code": "TOOL_NOT_ALLOWED",
                "message": f"Tool {decision.tool_name or '<empty>'} is not allowed for this run",
            }

        tool_args = decision.tool_args
        match decision.tool_name:
            case "run_command":
                command = tool_args.get("command")
                if not isinstance(command, str) or not command.strip():
                    return {"code": "INVALID_TOOL_ARGS", "message": "run_command requires command"}
                blocked_reason = _blocked_command_reason(command)
                if blocked_reason:
                    return {"code": "RISK_BLOCKED", "message": blocked_reason}
            case "take_screenshot":
                return None
            case "view_screenshot":
                screenshot_ids = self._view_screenshot_ids(tool_args)
                if not screenshot_ids:
                    return {"code": "INVALID_SCREENSHOT_ID", "message": "view_screenshot requires non-empty screenshot_ids"}
                for screenshot_id in screenshot_ids:
                    screenshot_path = Path(state.run.root_dir) / "screenshots" / f"{screenshot_id}.png"
                    if not screenshot_path.exists():
                        return {
                            "code": "SCREENSHOT_NOT_FOUND",
                            "message": f"Screenshot not found: {screenshot_path}",
                        }
            case "click" | "double_click" | "right_click":
                target_query = str(tool_args.get("target_query", tool_args.get("query", ""))).strip()
                if target_query:
                    if not state.observation.latest_screenshot_path:
                        return {
                            "code": "SCREENSHOT_REQUIRED",
                            "message": f"semantic {decision.tool_name} requires an existing screenshot observation",
                        }
                elif not self._has_coordinates(tool_args, keys=("x", "y")):
                    return {"code": "INVALID_COORDINATES", "message": f"{decision.tool_name} requires x/y or target_query"}
                elif not self._coordinates_in_bounds(state, self._required_int(tool_args, "x"), self._required_int(tool_args, "y")):
                    return {"code": "INVALID_COORDINATES", "message": f"{decision.tool_name} coordinates are outside screen bounds"}
                if decision.tool_name == "click":
                    clicks = self._optional_int(tool_args.get("clicks"), default=1) or 1
                    if clicks < 1 or clicks > 2:
                        return {"code": "INVALID_TOOL_ARGS", "message": "clicks must be 1 or 2"}
                    if tool_args.get("button", "left") not in {"left", "right", "middle"}:
                        return {"code": "INVALID_TOOL_ARGS", "message": "button must be left, right or middle"}
            case "move_mouse" | "hover":
                x_value = tool_args.get("x")
                y_value = tool_args.get("y")
                target_query = str(tool_args.get("target_query", tool_args.get("query", ""))).strip()
                if target_query and not state.observation.latest_screenshot_path:
                    return {
                        "code": "SCREENSHOT_REQUIRED",
                        "message": f"semantic {decision.tool_name} requires an existing screenshot observation",
                    }
                if target_query:
                    return None
                if x_value is None or y_value is None:
                    return {"code": "INVALID_COORDINATES", "message": f"{decision.tool_name} requires x/y or target_query"}
                if not isinstance(x_value, int | float) or not isinstance(y_value, int | float):
                    return {"code": "INVALID_COORDINATES", "message": f"{decision.tool_name} x/y must be numeric"}
                if not self._coordinates_in_bounds(state, int(x_value), int(y_value)):
                    return {"code": "INVALID_COORDINATES", "message": f"{decision.tool_name} coordinates are outside screen bounds"}
                if decision.tool_name == "hover":
                    duration_ms = self._optional_int(tool_args.get("duration_ms"), default=500) or 500
                    if duration_ms < 0 or duration_ms > 30000:
                        return {"code": "INVALID_TOOL_ARGS", "message": "hover duration_ms must be between 0 and 30000"}
            case "type_text":
                text = tool_args.get("text")
                if not isinstance(text, str) or not text:
                    return {"code": "INVALID_TOOL_ARGS", "message": "type_text requires text"}
                x_value = tool_args.get("x")
                y_value = tool_args.get("y")
                target_query = str(tool_args.get("target_query", tool_args.get("query", ""))).strip()
                if target_query and not state.observation.latest_screenshot_path:
                    return {
                        "code": "SCREENSHOT_REQUIRED",
                        "message": "semantic type_text requires an existing screenshot observation",
                    }
                if (x_value is None) ^ (y_value is None):
                    return {"code": "INVALID_COORDINATES", "message": "type_text x/y must be provided together"}
                if x_value is not None and y_value is not None:
                    if not isinstance(x_value, int | float) or not isinstance(y_value, int | float):
                        return {"code": "INVALID_COORDINATES", "message": "type_text x/y must be numeric"}
                    if not self._coordinates_in_bounds(state, int(x_value), int(y_value)):
                        return {
                            "code": "INVALID_COORDINATES",
                            "message": "type_text coordinates are outside screen bounds",
                        }
                if tool_args.get("caret_position", "idle") not in {"start", "idle", "end"}:
                    return {"code": "INVALID_TOOL_ARGS", "message": "caret_position must be start, idle or end"}
            case "hotkey":
                shortcut = str(tool_args.get("shortcut", "")).strip()
                if not shortcut:
                    return {"code": "INVALID_TOOL_ARGS", "message": "hotkey requires shortcut"}
            case "scroll":
                x_value = tool_args.get("x")
                y_value = tool_args.get("y")
                target_query = str(tool_args.get("target_query", tool_args.get("query", ""))).strip()
                if target_query and not state.observation.latest_screenshot_path:
                    return {
                        "code": "SCREENSHOT_REQUIRED",
                        "message": "semantic scroll requires an existing screenshot observation",
                    }
                if (x_value is None) ^ (y_value is None):
                    return {"code": "INVALID_COORDINATES", "message": "scroll x/y must be provided together"}
                if x_value is not None and y_value is not None:
                    if not isinstance(x_value, int | float) or not isinstance(y_value, int | float):
                        return {"code": "INVALID_COORDINATES", "message": "scroll x/y must be numeric"}
                    if not self._coordinates_in_bounds(state, int(x_value), int(y_value)):
                        return {"code": "INVALID_COORDINATES", "message": "scroll coordinates are outside screen bounds"}
                direction = tool_args.get("direction", "down")
                if direction not in {"up", "down", "left", "right"}:
                    return {"code": "INVALID_TOOL_ARGS", "message": "scroll direction must be up, down, left or right"}
                amount_value = tool_args.get("amount", tool_args.get("wheel_times", 1))
                if not isinstance(amount_value, int | float) or int(amount_value) < 1 or int(amount_value) > 100:
                    return {"code": "INVALID_TOOL_ARGS", "message": "scroll amount must be between 1 and 100"}
            case "drag":
                has_numeric_coords = self._has_coordinates(tool_args, keys=("x1", "y1", "x2", "y2"))
                has_semantic_drag = any(str(tool_args.get(key, "")).strip() for key in ("start_query", "target_query", "end_query", "query"))
                if not has_numeric_coords and not has_semantic_drag:
                    return {
                        "code": "INVALID_COORDINATES",
                        "message": "drag requires numeric coordinates or semantic start/target queries",
                    }
                if has_numeric_coords:
                    if not self._coordinates_in_bounds(
                        state,
                        self._required_int(tool_args, "x1"),
                        self._required_int(tool_args, "y1"),
                    ) or not self._coordinates_in_bounds(
                        state,
                        self._required_int(tool_args, "x2"),
                        self._required_int(tool_args, "y2"),
                    ):
                        return {"code": "INVALID_COORDINATES", "message": "drag coordinates are outside screen bounds"}
                elif not state.observation.latest_screenshot_path:
                    return {
                        "code": "SCREENSHOT_REQUIRED",
                        "message": "semantic drag requires an existing screenshot observation",
                    }
            case "open_app" | "switch_app":
                name = str(tool_args.get("name", "")).strip()
                if not name:
                    return {"code": "INVALID_TOOL_ARGS", "message": f"{decision.tool_name} requires name"}
            case "focus_window":
                title = str(tool_args.get("title", tool_args.get("name", ""))).strip()
                if not title:
                    return {"code": "INVALID_TOOL_ARGS", "message": "focus_window requires title or name"}
            case "wait":
                seconds_value = tool_args.get("seconds", 0)
                if not isinstance(seconds_value, int | float):
                    return {"code": "INVALID_TOOL_ARGS", "message": "wait seconds must be numeric"}
                seconds = int(seconds_value)
                if seconds < 0 or seconds > 30:
                    return {"code": "INVALID_TOOL_ARGS", "message": "wait seconds must be between 0 and 30"}
        return None

    def _record_agent_error(
        self,
        *,
        state: RuntimeState,
        store: RunStore,
        step_id: int,
        action_id: str,
        error: Exception,
    ) -> None:
        message = f"{type(error).__name__}: {error}"
        state.metrics.step_count += 1
        state.metrics.consecutive_failures += 1
        state.errors.last_error_code = "AGENT_DECISION_FAILED"
        state.errors.last_error_message = message
        state.errors.last_failed_tool = "main_agent"
        state.last_action.action_id = action_id
        state.last_action.actor = "main_agent"
        state.last_action.action_type = "agent_decision"
        state.last_action.action_args = {}
        state.last_action.result_status = "failed"
        state.last_action.result_summary = message
        store.append_trace(
            step_id=step_id,
            actor="main_agent",
            event_type="agent_decision",
            payload={"error": message},
            status="failed",
        )

    def _record_validation_error(
        self,
        *,
        state: RuntimeState,
        store: RunStore,
        step_id: int,
        action_id: str,
        decision: TerminalAgentDecision,
        validation_error: dict[str, str],
    ) -> None:
        state.metrics.step_count += 1
        state.metrics.tool_call_count += 1
        state.metrics.consecutive_failures += 1
        state.errors.last_error_code = validation_error["code"]
        state.errors.last_error_message = validation_error["message"]
        state.errors.last_failed_tool = decision.tool_name or "unknown"
        state.last_action.action_id = action_id
        state.last_action.actor = "main_agent"
        state.last_action.action_type = decision.tool_name or decision.kind
        state.last_action.action_args = decision.tool_args
        state.last_action.result_status = "failed"
        state.last_action.result_summary = validation_error["message"]
        state.last_action.artifact_refs = []
        store.append_trace(
            step_id=step_id,
            actor="runtime",
            event_type="tool_validation",
            payload={"tool_name": decision.tool_name, "error": validation_error},
            status="failed",
        )
        store.append_trace(
            step_id=step_id,
            actor="runtime",
            event_type="state_update",
            payload={
                "last_action": asdict(state.last_action),
                "metrics": asdict(state.metrics),
                "errors": asdict(state.errors),
            },
            status="failed",
        )

    def _history_item(
        self,
        *,
        step_id: int,
        decision: TerminalAgentDecision,
        result: dict[str, Any],
        artifact_refs: list[str],
    ) -> dict[str, object]:
        history_item: dict[str, object] = {
            "step_id": step_id,
            "tool_name": decision.tool_name,
            "thought_summary": decision.thought_summary,
            "expected_observation": decision.expected_observation,
            "success": bool(result.get("success", False)),
            "artifact_refs": artifact_refs,
            "result_summary": truncate_text(str(result.get("note") or result.get("reason") or ""), 500),
        }
        if decision.tool_name == "run_command":
            history_item.update(
                {
                    "command": result.get("command", ""),
                    "exit_code": result.get("exit_code", 0),
                    "stdout": truncate_text(str(result.get("stdout", ""))),
                    "stderr": truncate_text(str(result.get("stderr", ""))),
                    "timed_out": bool(result.get("timed_out", False)),
                }
            )
        elif decision.tool_name in {"take_screenshot", "view_screenshot"}:
            history_item.update(
                {
                    "screenshot_id": result.get("screenshot_id", ""),
                    "path": result.get("path", ""),
                    "resolution": {
                        "width": result.get("width", 0),
                        "height": result.get("height", 0),
                    },
                }
            )
        elif decision.tool_name == "locate_element" or self._artifact_ref(artifact_refs, prefix="location:") is not None:
            history_item.update(
                {
                    "query": result.get("query", "") or self._auto_locate_query(decision.tool_args),
                    "point": result.get("point"),
                    "confidence": result.get("confidence", 0.0),
                    "source": result.get("source", ""),
                    "suggested_next_steps": result.get("suggested_next_steps", []),
                    "error": result.get("error"),
                }
            )
        else:
            history_item.update(
                {
                    "result": result.get("result", {}),
                    "error": result.get("error"),
                }
            )
        return history_item

    @staticmethod
    def _validation_history_item(
        *,
        step_id: int,
        decision: TerminalAgentDecision,
        validation_error: dict[str, str],
    ) -> dict[str, object]:
        return {
            "step_id": step_id,
            "tool_name": decision.tool_name,
            "thought_summary": decision.thought_summary,
            "expected_observation": decision.expected_observation,
            "success": False,
            "validation_error": validation_error,
            "artifact_refs": [],
        }

    def _emit(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)

    def _emit_run_header(self, *, run_id: str, user_request: str, state: RuntimeState) -> None:
        self._emit("=" * 80)
        self._emit(f"RUN       : {run_id}")
        self._emit(f"Task      : {user_request}")
        self._emit(f"Workspace : {self.workspace}")
        self._emit(f"Tools     : {', '.join(state.control.allowed_tools)}")
        self._emit(
            f"Limits    : max_steps={self.max_steps}, tool_timeout={self.step_timeout_seconds}s, "
            f"examiner=off"
        )
        self._emit("=" * 80)

    def _emit_step_header(self, *, step_id: int, title: str) -> None:
        self._emit("")
        self._emit("-" * 80)
        self._emit(f"STEP {step_id}/{self.max_steps} · {title}")
        self._emit("-" * 80)

    def _emit_finish_request(
        self,
        *,
        decision: TerminalAgentDecision,
        completion_claim: str,
    ) -> None:
        self._emit("Decision  : finish_request")
        self._emit(f"Claim     : {completion_claim}")
        if decision.supporting_evidence:
            self._emit(f"Evidence  : {', '.join(decision.supporting_evidence)}")
        if decision.remaining_uncertainty:
            self._emit(f"Uncertain : {decision.remaining_uncertainty}")

    def _emit_tool_call(self, *, decision: TerminalAgentDecision) -> None:
        self._emit("Decision  : tool_call")
        self._emit(f"Tool      : {decision.tool_name or '<empty>'}")
        self._emit(f"Thought   : {decision.thought_summary or '-'}")
        if decision.tool_name == "run_command" and isinstance(decision.tool_args.get("command"), str):
            self._emit("Command   :")
            self._emit(
                _indent_block(
                    truncate_text(str(decision.tool_args["command"]), self.progress_output_char_limit),
                    prefix="  PS> ",
                )
            )
        else:
            self._emit("Args      :")
            self._emit(
                _indent_block(
                    truncate_text(
                        json.dumps(decision.tool_args, ensure_ascii=False),
                        self.progress_output_char_limit,
                    )
                )
            )

    def _emit_tool_result(self, *, result: dict[str, Any], artifact_refs: list[str]) -> None:
        status = "OK" if result["success"] else "FAILED"
        self._emit(
            f"Result    : {status} | tool={result.get('tool_name')} | "
            f"duration={result.get('duration_ms', 0)}ms | artifacts={', '.join(artifact_refs)}"
        )
        tool_name = str(result.get("tool_name", ""))
        if tool_name == "run_command":
            stdout = _format_terminal_output(str(result.get("stdout", "")), limit=self.progress_output_char_limit)
            stderr = _format_terminal_output(str(result.get("stderr", "")), limit=self.progress_output_char_limit)
            self._emit("Stdout    :" if stdout else "Stdout    : <empty>")
            if stdout:
                self._emit(_indent_block(stdout))
            self._emit("Stderr    :" if stderr else "Stderr    : <empty>")
            if stderr:
                self._emit(_indent_block(stderr))
        elif tool_name == "take_screenshot":
            self._emit(f"Screenshot: {result.get('screenshot_id')} {result.get('path')}")
        elif tool_name == "view_screenshot":
            screenshot_ids = ', '.join(self._string_list(result.get('screenshot_ids', [])))
            self._emit(f"Screenshots: {screenshot_ids}")
        elif result.get("point") is not None:
            self._emit(
                f"Location  : point={result.get('point')} confidence={result.get('confidence')} "
                f"source={result.get('source')} reason={result.get('reason')}"
            )
        else:
            preview = json.dumps(result.get("result", {}), ensure_ascii=False)
            self._emit("Output    :")
            self._emit(_indent_block(truncate_text(preview, self.progress_output_char_limit)))
        if result.get("error"):
            self._emit(f"Error     : {result['error']}")

    def _emit_run_footer(self, *, state: RuntimeState, run_dir: Path) -> None:
        self._emit("")
        self._emit("=" * 80)
        self._emit(f"DONE      : {state.run.status}")
        self._emit(f"Reason    : {state.run.terminated_reason}")
        self._emit(
            f"Metrics   : steps={state.metrics.step_count}, commands={state.metrics.command_count}, "
            f"screenshots={state.metrics.screenshot_count}, runtime={state.metrics.runtime_seconds}s"
        )
        self._emit(f"Artifacts : {run_dir}")
        self._emit("=" * 80)

    def _maybe_auto_locate(
        self,
        *,
        state: RuntimeState,
        store: RunStore,
        step_id: int,
        action_id: str,
        query: str,
        tool_name: str,
    ) -> ElementLocationResult | None:
        if not query:
            return None
        if not state.observation.latest_screenshot_path:
            screenshot_result = self._capture_and_record_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=f"{action_id}_auto_locate",
                description=f"auto locate before {tool_name}",
            )
            state.observation.latest_screenshot_id = screenshot_result.screenshot_id
            state.observation.latest_screenshot_path = screenshot_result.path
            state.observation.desktop_resolution = {"width": screenshot_result.width, "height": screenshot_result.height}
            state.metrics.screenshot_count += 1
        screenshot_path = Path(state.observation.latest_screenshot_path)
        screenshot_id = state.observation.latest_screenshot_id
        return locate_element(
            query=query,
            screenshot_path=screenshot_path,
            screenshot_id=screenshot_id,
            backend=self._locator_backend(),
        )

    @staticmethod
    def _auto_locate_query(tool_args: dict[str, object]) -> str:
        for key in ("target_query", "query", "target_description"):
            value = str(tool_args.get(key, "")).strip()
            if value:
                return value
        return ""

    def _update_location_observation_from_result(
        self,
        *,
        state: RuntimeState,
        result: dict[str, Any],
        artifact_refs: list[str],
    ) -> None:
        state.observation.latest_location_result_id = self._artifact_id(
            artifact_refs,
            prefix="location:",
        )
        state.observation.latest_location_query = str(result.get("query", ""))
        point = result.get("point")
        state.observation.latest_location_point = tuple(point) if isinstance(point, list | tuple) else None
        state.observation.latest_location_confidence = float(result.get("confidence", 0.0))
        state.observation.latest_location_source = str(result.get("source") or "")
        state.observation.latest_location_suggested_next_steps = self._string_list(
            result.get("suggested_next_steps", [])
        )
        if bool(result.get("success", False)):
            state.metrics.consecutive_location_failures = 0
            state.observation.latest_location_error_code = ""
        else:
            state.metrics.consecutive_location_failures += 1
            error = result.get("error") or {}
            state.observation.latest_location_error_code = str(
                error.get("code", "LOCATE_ELEMENT_FAILED")
            )
            state.observation.latest_location_point = None

    @staticmethod
    def _attach_location_metadata(
        result: dict[str, Any],
        location_result: ElementLocationResult | None,
    ) -> dict[str, Any]:
        if location_result is None:
            return result
        result["query"] = location_result.query
        result["point"] = list(location_result.point) if location_result.point is not None else None
        result["confidence"] = location_result.confidence
        result["source"] = location_result.source
        result["reason"] = location_result.reason
        result["error"] = location_result.error if location_result.error is not None else result.get("error")
        result["suggested_next_steps"] = list(location_result.suggested_next_steps)
        return result

    def _generic_gui_result_summary(
        self,
        *,
        decision: TerminalAgentDecision,
        result: dict[str, Any],
    ) -> str:
        if decision.tool_name == "type_text":
            typed_length = int(result.get("result", {}).get("typed_length", 0))
            return f"typed {typed_length} characters"
        if decision.tool_name == "wait":
            return f"waited {result.get('result', {}).get('seconds', 0)} seconds"
        if decision.tool_name in {"click", "double_click", "right_click", "move_mouse", "hover"}:
            click_result = result.get("result", {})
            action = {
                "click": "clicked",
                "double_click": "double-clicked",
                "right_click": "right-clicked",
                "move_mouse": "moved mouse to",
                "hover": "hovered at",
            }[decision.tool_name]
            return f"{action} ({click_result.get('x')}, {click_result.get('y')})"
        if decision.tool_name == "scroll":
            scroll_result = result.get("result", {})
            return (
                f"scrolled {scroll_result.get('axis')} {scroll_result.get('direction')} "
                f"by {scroll_result.get('amount')}"
            )
        if decision.tool_name == "drag":
            drag_result = result.get("result", {})
            return (
                f"dragged from ({drag_result.get('x1')}, {drag_result.get('y1')}) "
                f"to ({drag_result.get('x2')}, {drag_result.get('y2')})"
            )
        if decision.tool_name in {"open_app", "switch_app", "focus_window"}:
            return str(result.get("result", {}).get("message", "")).strip() or f"{decision.tool_name} executed"
        return f"{decision.tool_name} executed"

    def _resolve_click_coordinates(
        self,
        *,
        state: RuntimeState,
        tool_args: dict[str, object],
        auto_locate_result: ElementLocationResult | None,
    ) -> tuple[int, int, str]:
        if auto_locate_result is not None and auto_locate_result.success and auto_locate_result.point is not None:
            x, y = auto_locate_result.point
            return x, y, "target_query"
        return self._required_int(tool_args, "x"), self._required_int(tool_args, "y"), ""

    def _resolve_optional_point(
        self,
        *,
        state: RuntimeState,
        tool_args: dict[str, object],
        keys: tuple[str, str],
        query_keys: tuple[str, ...],
        auto_locate_result: ElementLocationResult | None,
    ) -> tuple[int | None, int | None, str]:
        x_key, y_key = keys
        query = next((str(tool_args.get(key, "")).strip() for key in query_keys if str(tool_args.get(key, "")).strip()), "")
        if auto_locate_result is not None and auto_locate_result.success and auto_locate_result.point is not None and query:
            x, y = auto_locate_result.point
            return x, y, query_keys[0]
        x_value = tool_args.get(x_key)
        y_value = tool_args.get(y_key)
        if x_value is None or y_value is None:
            return None, None, ""
        if not isinstance(x_value, int | float) or not isinstance(y_value, int | float):
            raise ValueError("coordinates must be numeric")
        return int(x_value), int(y_value), ""

    def _resolve_drag_point(
        self,
        *,
        state: RuntimeState,
        tool_args: dict[str, object],
        x_key: str,
        y_key: str,
        query: str,
        location_result: ElementLocationResult | None,
        source_name: str,
    ) -> tuple[int, int, str]:
        if location_result is not None and location_result.success and location_result.point is not None and query:
            x, y = location_result.point
            return x, y, source_name
        return self._required_int(tool_args, x_key), self._required_int(tool_args, y_key), ""

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    @staticmethod
    def _view_screenshot_ids(tool_args: dict[str, object]) -> list[str]:
        screenshot_ids = tool_args.get("screenshot_ids")
        if isinstance(screenshot_ids, list):
            return [str(item).strip() for item in screenshot_ids if str(item).strip()]
        screenshot_id = str(tool_args.get("screenshot_id", "")).strip()
        return [screenshot_id] if screenshot_id else []

    @staticmethod
    def _artifact_ref(artifact_refs: list[str], *, prefix: str) -> str | None:
        for artifact_ref in reversed(artifact_refs):
            if artifact_ref.startswith(prefix):
                return artifact_ref
        return None

    @staticmethod
    def _artifact_id(artifact_refs: list[str], *, prefix: str) -> str:
        artifact_ref = AutonomousComputerRuntime._artifact_ref(artifact_refs, prefix=prefix)
        if artifact_ref is None:
            return ""
        return artifact_ref.split(":", 1)[1]

    @staticmethod
    def _has_coordinates(tool_args: dict[str, object], *, keys: tuple[str, ...]) -> bool:
        return all(key in tool_args and isinstance(tool_args[key], int | float) for key in keys)

    @staticmethod
    def _required_int(tool_args: dict[str, object], key: str) -> int:
        value = tool_args[key]
        if not isinstance(value, int | float):
            raise ValueError(f"{key} must be numeric")
        return int(value)

    @staticmethod
    def _optional_int(value: object, default: int | None = None) -> int | None:
        if value is None:
            return default
        if not isinstance(value, int | float):
            raise ValueError("value must be numeric")
        return int(value)

    @staticmethod
    def _button_value(value: object) -> Literal["left", "right", "middle"]:
        if value not in {"left", "right", "middle"}:
            raise ValueError("button must be left, right or middle")
        return cast(Literal["left", "right", "middle"], value)

    @staticmethod
    def _caret_position(value: object) -> Literal["start", "idle", "end"]:
        if value not in {"start", "idle", "end"}:
            raise ValueError("caret_position must be start, idle or end")
        return cast(Literal["start", "idle", "end"], value)

    @staticmethod
    def _scroll_direction(value: object) -> Literal["up", "down", "left", "right"]:
        if value not in {"up", "down", "left", "right"}:
            raise ValueError("scroll direction must be up, down, left or right")
        return cast(Literal["up", "down", "left", "right"], value)

    @staticmethod
    def _coordinates_in_bounds(state: RuntimeState, x: int, y: int) -> bool:
        if x < 0 or y < 0:
            return False
        width = state.observation.desktop_resolution.get("width")
        height = state.observation.desktop_resolution.get("height")
        if not width or not height:
            return True
        return x <= width and y <= height


def _read_image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:
        return 0, 0
