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
from .terminal_agent import (
    AutonomousComputerAgent,
    LLMComputerAgent,
    TerminalAgentDecision,
    TerminalAgentProtocolError,
    truncate_text,
)
from .tools import (
    CommandResult,
    ElementLocatorBackend,
    GuiActionResult,
    GuiAutomationBackend,
    PowerShellBackend,
    ScreenshotBackend,
    ScreenshotResult,
    bbox_center,
    click,
    drag,
    hotkey,
    locate_element,
    run_command,
    take_screenshot,
    type_text,
    wait,
)

ProgressCallback = Callable[[str], None]

AUTONOMOUS_COMPUTER_TOOLS: list[str] = [
    "run_command",
    "take_screenshot",
    "view_screenshot",
    "locate_element",
    "click",
    "type_text",
    "hotkey",
    "drag",
    "wait",
]


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
        max_steps: int = 20,
        step_timeout_seconds: int = 180,
        max_consecutive_failures: int = 4,
        model_config_path: Path = Path("config/models.local.json"),
        model_role: str = "mainAgent",
        screenshot_after_gui_action: bool = True,
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
        self.max_steps = max_steps
        self.step_timeout_seconds = step_timeout_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.screenshot_after_gui_action = screenshot_after_gui_action
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
            },
            status="success",
        )
        self._emit_run_header(run_id=run_id, user_request=user_request, state=state)

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
                screenshot_id=str(tool_args["screenshot_id"]),
            )
            if result_dict["success"]:
                artifact_refs.append(f"screenshot:{result_dict['screenshot_id']}")
            return result_dict, artifact_refs

        if tool_name == "locate_element":
            screenshot_path = Path(state.observation.latest_screenshot_path)
            screenshot_id = state.observation.latest_screenshot_id
            location_result = locate_element(
                query=str(tool_args["query"]),
                screenshot_path=screenshot_path,
                screenshot_id=screenshot_id,
                backend=self.element_locator_backend,
            )
            location_artifacts = store.write_location_result(step_id=step_id, result=location_result)
            artifact_refs.extend(location_result.artifacts)
            artifact_refs.append(location_artifacts["artifact_ref"])
            return asdict(location_result), artifact_refs

        if tool_name == "click":
            click_x, click_y, resolved_from = self._resolve_click_coordinates(
                state=state,
                tool_args=tool_args,
            )
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
            return self._attach_post_action_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                tool_name=tool_name,
                gui_result=gui_result,
                artifact_refs=artifact_refs,
            )

        if tool_name == "type_text":
            gui_result = type_text(
                str(tool_args["text"]),
                x=self._optional_int(tool_args.get("x")),
                y=self._optional_int(tool_args.get("y")),
                clear=bool(tool_args.get("clear", False)),
                caret_position=self._caret_position(tool_args.get("caret_position", "idle")),
                press_enter=bool(tool_args.get("press_enter", False)),
                backend=self.gui_backend,
            )
            return self._attach_post_action_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                tool_name=tool_name,
                gui_result=gui_result,
                artifact_refs=artifact_refs,
            )

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

        if tool_name == "drag":
            gui_result = drag(
                self._required_int(tool_args, "x1"),
                self._required_int(tool_args, "y1"),
                self._required_int(tool_args, "x2"),
                self._required_int(tool_args, "y2"),
                backend=self.gui_backend,
            )
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

    def _view_screenshot_result(self, *, state: RuntimeState, screenshot_id: str) -> dict[str, Any]:
        started_at = time.perf_counter()
        viewed_at = datetime.now(UTC).isoformat()
        normalized_id = screenshot_id.strip()
        screenshot_path = Path(state.run.root_dir) / "screenshots" / f"{normalized_id}.png"
        if not normalized_id:
            return {
                "tool_name": "view_screenshot",
                "screenshot_id": screenshot_id,
                "path": "",
                "width": 0,
                "height": 0,
                "success": False,
                "duration_ms": int((time.perf_counter() - started_at) * 1000),
                "timestamp": viewed_at,
                "note": "screenshot_id must not be empty",
                "error": {"code": "INVALID_SCREENSHOT_ID", "message": "view_screenshot requires screenshot_id"},
            }
        if not screenshot_path.exists():
            return {
                "tool_name": "view_screenshot",
                "screenshot_id": normalized_id,
                "path": str(screenshot_path),
                "width": 0,
                "height": 0,
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
        return {
            "tool_name": "view_screenshot",
            "screenshot_id": normalized_id,
            "path": str(screenshot_path),
            "width": width,
            "height": height,
            "success": True,
            "duration_ms": int((time.perf_counter() - started_at) * 1000),
            "timestamp": viewed_at,
            "note": "selected screenshot for visual context",
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
            state.observation.desktop_resolution = {
                "width": int(result.get("width", 0)),
                "height": int(result.get("height", 0)),
            }
            result_summary = str(result.get("note") or result.get("path"))
        elif decision.tool_name == "view_screenshot":
            if success:
                state.observation.latest_screenshot_id = str(result["screenshot_id"])
                state.observation.latest_screenshot_path = str(result["path"])
                state.observation.desktop_resolution = {
                    "width": int(result.get("width", 0)),
                    "height": int(result.get("height", 0)),
                }
            result_summary = str(result.get("note") or result.get("path"))
        elif decision.tool_name == "locate_element":
            state.observation.latest_location_result_id = self._artifact_id(
                artifact_refs,
                prefix="location:",
            )
            state.observation.latest_location_query = str(result.get("query", ""))
            bbox = result.get("bbox")
            state.observation.latest_location_bbox = tuple(bbox) if isinstance(bbox, list | tuple) else None
            state.observation.latest_location_confidence = float(result.get("confidence", 0.0))
            state.observation.latest_location_source = str(result.get("source") or "")
            state.observation.latest_location_suggested_next_steps = self._string_list(
                result.get("suggested_next_steps", [])
            )
            if success:
                state.metrics.consecutive_location_failures = 0
                state.observation.latest_location_error_code = ""
            else:
                state.metrics.consecutive_location_failures += 1
                error = result.get("error") or {}
                state.observation.latest_location_error_code = str(
                    error.get("code", "LOCATE_ELEMENT_FAILED")
                )
                state.observation.latest_location_bbox = None
            result_summary = str(result.get("reason") or "located element candidate")
        else:
            latest_screenshot_ref = self._artifact_ref(artifact_refs, prefix="screenshot:")
            if latest_screenshot_ref is not None:
                state.metrics.screenshot_count += 1
                state.observation.latest_screenshot_id = latest_screenshot_ref.split(":", 1)[1]
                state.observation.latest_screenshot_path = str(
                    Path(state.run.root_dir)
                    / "screenshots"
                    / f"{state.observation.latest_screenshot_id}.png"
                )
            if decision.tool_name == "type_text":
                typed_length = int(result.get("result", {}).get("typed_length", 0))
                result_summary = f"typed {typed_length} characters"
            elif decision.tool_name == "wait":
                result_summary = f"waited {result.get('result', {}).get('seconds', 0)} seconds"
            elif decision.tool_name == "click":
                click_result = result.get("result", {})
                result_summary = f"clicked at ({click_result.get('x')}, {click_result.get('y')})"
            else:
                result_summary = f"{decision.tool_name} executed"

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
            if decision.tool_name == "locate_element" and state.metrics.consecutive_location_failures >= 2:
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
                screenshot_id = str(tool_args.get("screenshot_id", "")).strip()
                if not screenshot_id:
                    return {"code": "INVALID_SCREENSHOT_ID", "message": "view_screenshot requires screenshot_id"}
                screenshot_path = Path(state.run.root_dir) / "screenshots" / f"{screenshot_id}.png"
                if not screenshot_path.exists():
                    return {
                        "code": "SCREENSHOT_NOT_FOUND",
                        "message": f"Screenshot not found: {screenshot_path}",
                    }
            case "locate_element":
                query = str(tool_args.get("query", "")).strip()
                if not query:
                    return {"code": "INVALID_QUERY", "message": "locate_element requires query"}
                if not state.observation.latest_screenshot_path:
                    return {
                        "code": "SCREENSHOT_REQUIRED",
                        "message": "locate_element requires an existing screenshot observation",
                    }
            case "click":
                target = str(tool_args.get("target", "")).strip()
                if target == "last_located":
                    if state.observation.latest_location_bbox is None:
                        return {
                            "code": "LOCATION_REQUIRED",
                            "message": "click target=last_located requires a successful locate_element result",
                        }
                elif not self._has_coordinates(tool_args, keys=("x", "y")):
                    return {"code": "INVALID_COORDINATES", "message": "click requires x/y or target=last_located"}
                elif not self._coordinates_in_bounds(state, self._required_int(tool_args, "x"), self._required_int(tool_args, "y")):
                    return {"code": "INVALID_COORDINATES", "message": "click coordinates are outside screen bounds"}
                clicks = self._optional_int(tool_args.get("clicks"), default=1) or 1
                if clicks < 1 or clicks > 2:
                    return {"code": "INVALID_TOOL_ARGS", "message": "clicks must be 1 or 2"}
                if tool_args.get("button", "left") not in {"left", "right", "middle"}:
                    return {"code": "INVALID_TOOL_ARGS", "message": "button must be left, right or middle"}
            case "type_text":
                text = tool_args.get("text")
                if not isinstance(text, str) or not text:
                    return {"code": "INVALID_TOOL_ARGS", "message": "type_text requires text"}
                x_value = tool_args.get("x")
                y_value = tool_args.get("y")
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
            case "drag":
                if not self._has_coordinates(tool_args, keys=("x1", "y1", "x2", "y2")):
                    return {
                        "code": "INVALID_COORDINATES",
                        "message": "drag requires x1, y1, x2 and y2",
                    }
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
        elif decision.tool_name == "locate_element":
            history_item.update(
                {
                    "query": result.get("query", ""),
                    "bbox": result.get("bbox"),
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
        elif tool_name in {"take_screenshot", "view_screenshot"}:
            self._emit(f"Screenshot: {result.get('screenshot_id')} {result.get('path')}")
        elif tool_name == "locate_element":
            self._emit(
                f"Location  : bbox={result.get('bbox')} confidence={result.get('confidence')} "
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

    def _resolve_click_coordinates(
        self,
        *,
        state: RuntimeState,
        tool_args: dict[str, object],
    ) -> tuple[int, int, str]:
        target = str(tool_args.get("target", "")).strip()
        if target == "last_located":
            if state.observation.latest_location_bbox is None:
                raise ValueError("No latest located bbox available")
            x, y = bbox_center(state.observation.latest_location_bbox)
            return x, y, "last_located"
        return self._required_int(tool_args, "x"), self._required_int(tool_args, "y"), ""

    @staticmethod
    def _string_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

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
