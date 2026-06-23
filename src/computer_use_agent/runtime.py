from __future__ import annotations

import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from .run_store import RunStore, mark_run_finished
from .runtime_state import RuntimeState, TerminalRunStatus, create_runtime_state
from .sample_tasks import DemoTask, PlannedAction, build_completion_hint, find_demo_task
from .tools import (
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


class TerminalMainAgent:
    def plan(self, user_request: str) -> DemoTask | None:
        return find_demo_task(user_request)


class TerminalRuntime:
    def __init__(
        self,
        workspace: Path,
        runs_root: Path,
        command_backend: PowerShellBackend | None = None,
        screenshot_backend: ScreenshotBackend | None = None,
        gui_backend: GuiAutomationBackend | None = None,
        element_locator_backend: ElementLocatorBackend | None = None,
        agent: TerminalMainAgent | None = None,
        max_steps: int = 8,
        screenshot_after_gui_action: bool = True,
    ) -> None:
        self.workspace = workspace
        self.runs_root = runs_root
        self.command_backend = command_backend or PowerShellBackend()
        self.screenshot_backend = screenshot_backend
        self.gui_backend = gui_backend
        self.element_locator_backend = element_locator_backend
        self.agent = agent or TerminalMainAgent()
        self.max_steps = max_steps
        self.screenshot_after_gui_action = screenshot_after_gui_action

    def run(self, user_request: str) -> RuntimeState:
        run_id = datetime.now(UTC).strftime("run_%Y%m%d_%H%M%S")
        run_dir = self.runs_root / run_id
        store = RunStore(run_dir)
        store.prepare()

        task = self.agent.plan(user_request)
        state = create_runtime_state(
            user_request=user_request,
            run_id=run_id,
            root_dir=run_dir,
            task_type=task.task_type if task else "terminal",
            allowed_tools=self._allowed_tools(task),
            max_steps=self.max_steps,
            step_timeout_seconds=180,
        )
        started_at = time.perf_counter()

        store.append_trace(
            step_id=0,
            actor="runtime",
            event_type="run_initialized",
            payload={"task": user_request, "workspace": str(self.workspace)},
            status="success",
        )

        if task is None:
            state.errors.last_error_code = "TASK_NOT_SUPPORTED"
            state.errors.last_error_message = "No demo task matched the user request"
            mark_run_finished(state, TerminalRunStatus.FAILED, "No supported demo task matched")
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

        for step_id, planned_action in enumerate(task.action_plan, start=1):
            if step_id > self.max_steps:
                state.errors.last_error_code = "MAX_STEPS_EXCEEDED"
                state.errors.last_error_message = "Reached max steps before completing task"
                mark_run_finished(state, TerminalRunStatus.ABORTED, "Maximum step limit reached")
                break

            state.run.current_step = step_id
            action_id = f"act_{step_id:04d}"
            store.append_trace(
                step_id=step_id,
                actor="main_agent",
                event_type="agent_decision",
                payload={
                    "kind": "tool_call",
                    "tool_name": planned_action.tool_name,
                    "tool_args": planned_action.tool_args,
                    "expected_observation": planned_action.expected_observation,
                },
                status="pending",
            )

            validation_error = self._validate_action(state=state, planned_action=planned_action)
            if validation_error is not None:
                state.metrics.step_count += 1
                state.metrics.tool_call_count += 1
                state.metrics.consecutive_failures += 1
                state.errors.last_error_code = validation_error["code"]
                state.errors.last_error_message = validation_error["message"]
                state.errors.last_failed_tool = planned_action.tool_name
                state.last_action.action_id = action_id
                state.last_action.actor = "tool_runtime"
                state.last_action.action_type = planned_action.tool_name
                state.last_action.action_args = planned_action.tool_args
                state.last_action.result_status = "failed"
                state.last_action.result_summary = validation_error["message"]
                state.last_action.artifact_refs = []
                store.append_trace(
                    step_id=step_id,
                    actor="runtime",
                    event_type="tool_validation",
                    payload={"tool_name": planned_action.tool_name, "error": validation_error},
                    status="failed",
                )
                store.append_trace(
                    step_id=step_id,
                    actor="runtime",
                    event_type="state_update",
                    payload={"last_action": asdict(state.last_action), "errors": asdict(state.errors)},
                    status="failed",
                )
                mark_run_finished(state, TerminalRunStatus.FAILED, f"{planned_action.tool_name} validation failed")
                break

            store.append_trace(
                step_id=step_id,
                actor="runtime",
                event_type="tool_validation",
                payload={"tool_name": planned_action.tool_name, "tool_args": planned_action.tool_args},
                status="success",
            )

            result, artifact_refs = self._execute_planned_action(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                planned_action=planned_action,
            )
            self._apply_tool_result(
                state=state,
                step_id=step_id,
                action_id=action_id,
                planned_action=planned_action,
                result=result,
                artifact_refs=artifact_refs,
            )
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

            if not result["success"]:
                if self._can_continue_after_tool_failure(state=state, planned_action=planned_action):
                    continue
                mark_run_finished(state, TerminalRunStatus.FAILED, f"{planned_action.tool_name} failed at step {step_id}")
                break
        else:
            if task.task_type == "terminal":
                completion_hint = build_completion_hint(self.workspace, state.metrics.command_count)
            else:
                completion_hint = task.success_hint
            mark_run_finished(state, TerminalRunStatus.SUCCESS, completion_hint)

        state.control.terminated_reason = state.run.terminated_reason
        state.metrics.runtime_seconds = round(time.perf_counter() - started_at, 3)
        store.append_trace(
            step_id=state.run.current_step,
            actor="main_agent",
            event_type="finish_request",
            payload={
                "completion_claim": state.run.terminated_reason,
                "status": state.run.status,
                "task_type": state.task.task_type,
            },
            status="success" if state.run.status == TerminalRunStatus.SUCCESS else "failed",
            artifact_refs=state.last_action.artifact_refs,
        )
        store.write_summary(state)
        return state

    def _allowed_tools(self, task: DemoTask | None) -> list[str]:
        if task is None:
            return ["run_command"]
        return sorted({action.tool_name for action in task.action_plan})

    def _validate_action(
        self,
        *,
        state: RuntimeState,
        planned_action: PlannedAction,
    ) -> dict[str, str] | None:
        if planned_action.tool_name not in state.control.allowed_tools:
            return {
                "code": "TOOL_NOT_ALLOWED",
                "message": f"Tool {planned_action.tool_name} is not allowed for this run",
            }

        match planned_action.tool_name:
            case "run_command":
                command = str(planned_action.tool_args.get("command", "")).strip()
                if not command:
                    return {"code": "INVALID_TOOL_ARGS", "message": "command must not be empty"}
            case "take_screenshot":
                return None
            case "locate_element":
                query = str(planned_action.tool_args.get("query", "")).strip()
                if not query:
                    return {"code": "INVALID_QUERY", "message": "locate_element requires query"}
                if not state.observation.latest_screenshot_path:
                    return {
                        "code": "SCREENSHOT_REQUIRED",
                        "message": "locate_element requires an existing screenshot observation",
                    }
            case "click":
                target = str(planned_action.tool_args.get("target", "")).strip()
                if target == "last_located":
                    if state.observation.latest_location_bbox is None:
                        return {
                            "code": "LOCATION_REQUIRED",
                            "message": "click target=last_located requires a successful prior locate_element result",
                        }
                elif not self._has_coordinates(planned_action.tool_args, keys=("x", "y")):
                    return {"code": "INVALID_COORDINATES", "message": "click requires x/y or target=last_located"}
            case "drag":
                if not self._has_coordinates(planned_action.tool_args, keys=("x1", "y1", "x2", "y2")):
                    return {
                        "code": "INVALID_COORDINATES",
                        "message": "drag requires x1, y1, x2 and y2",
                    }
            case "type_text":
                text = str(planned_action.tool_args.get("text", ""))
                if not text:
                    return {"code": "INVALID_TOOL_ARGS", "message": "type_text requires text"}
            case "hotkey":
                shortcut = str(planned_action.tool_args.get("shortcut", "")).strip()
                if not shortcut:
                    return {"code": "INVALID_TOOL_ARGS", "message": "hotkey requires shortcut"}
            case "wait":
                seconds_value = planned_action.tool_args.get("seconds", 0)
                if not isinstance(seconds_value, int | float):
                    return {"code": "INVALID_TOOL_ARGS", "message": "wait seconds must be numeric"}
                if int(seconds_value) < 0:
                    return {"code": "INVALID_TOOL_ARGS", "message": "wait seconds must be >= 0"}
        return None

    def _execute_planned_action(
        self,
        *,
        state: RuntimeState,
        store: RunStore,
        step_id: int,
        action_id: str,
        planned_action: PlannedAction,
    ) -> tuple[dict[str, Any], list[str]]:
        artifact_refs: list[str] = []
        tool_name = planned_action.tool_name
        tool_args = planned_action.tool_args

        if tool_name == "run_command":
            result = run_command(
                command=str(tool_args["command"]),
                timeout_s=180,
                cwd=self.workspace,
                backend=self.command_backend,
            )
            artifact_paths = store.write_command_result(step_id=step_id, result=result)
            artifact_refs.append(artifact_paths["command_result_id"])
            result_dict = asdict(result)
            result_dict["tool_name"] = "run_command"
            return result_dict, artifact_refs

        if tool_name == "take_screenshot":
            screenshot_result = self._capture_and_record_screenshot(
                state=state,
                store=store,
                step_id=step_id,
                action_id=action_id,
                description=str(tool_args.get("description", "")),
            )
            artifact_refs.append(f"screenshot:{screenshot_result.screenshot_id}")
            result_dict = asdict(screenshot_result)
            result_dict["tool_name"] = "take_screenshot"
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
            result_dict = asdict(location_result)
            return result_dict, artifact_refs

        if tool_name == "click":
            click_x, click_y, resolved_from = self._resolve_click_coordinates(state=state, tool_args=tool_args)
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

        raise ValueError(f"Unsupported planned action: {tool_name}")

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

    def _apply_tool_result(
        self,
        *,
        state: RuntimeState,
        step_id: int,
        action_id: str,
        planned_action: PlannedAction,
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

        if planned_action.tool_name == "run_command":
            state.metrics.command_count += 1
            state.observation.latest_command_result_id = artifact_refs[0] if artifact_refs else ""
            result_summary = str(result.get("stdout") or result.get("stderr") or result.get("note", ""))
        elif planned_action.tool_name == "take_screenshot":
            state.metrics.screenshot_count += 1
            state.observation.latest_screenshot_id = str(result["screenshot_id"])
            state.observation.latest_screenshot_path = str(result["path"])
            state.observation.desktop_resolution = {
                "width": int(result.get("width", 0)),
                "height": int(result.get("height", 0)),
            }
            result_summary = str(result.get("note") or result.get("path"))
        elif planned_action.tool_name == "locate_element":
            state.observation.latest_location_result_id = self._artifact_id(artifact_refs, prefix="location:")
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
                state.observation.latest_location_error_code = str(error.get("code", "LOCATE_ELEMENT_FAILED"))
                state.observation.latest_location_bbox = None
            result_summary = str(result.get("reason") or "located element candidate")
        else:
            latest_screenshot_ref = self._artifact_ref(artifact_refs, prefix="screenshot:")
            if latest_screenshot_ref is not None:
                state.metrics.screenshot_count += 1
                state.observation.latest_screenshot_id = latest_screenshot_ref.split(":", 1)[1]
                state.observation.latest_screenshot_path = str(
                    Path(state.run.root_dir) / "screenshots" / f"{state.observation.latest_screenshot_id}.png"
                )
            if planned_action.tool_name == "type_text":
                typed_length = int(result.get("result", {}).get("typed_length", 0))
                result_summary = f"typed {typed_length} characters"
            elif planned_action.tool_name == "wait":
                result_summary = f"waited {result.get('result', {}).get('seconds', 0)} seconds"
            elif planned_action.tool_name == "click":
                click_result = result.get("result", {})
                result_summary = f"clicked at ({click_result.get('x')}, {click_result.get('y')})"
            else:
                result_summary = f"{planned_action.tool_name} executed"

        state.observation.last_observation_summary = result_summary
        state.last_action.action_id = action_id
        state.last_action.actor = "tool_runtime"
        state.last_action.action_type = planned_action.tool_name
        state.last_action.action_args = planned_action.tool_args
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
            state.errors.last_failed_tool = planned_action.tool_name
            if planned_action.tool_name == "locate_element" and state.metrics.consecutive_location_failures >= 2:
                state.errors.blocked = True
                state.errors.block_reason = "连续定位失败，禁止继续盲点；请重新截图、滚动/展开界面或放弃当前方案。"

    def _can_continue_after_tool_failure(self, *, state: RuntimeState, planned_action: PlannedAction) -> bool:
        if planned_action.tool_name != "locate_element":
            return False
        return state.metrics.consecutive_location_failures < 2

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
        artifact_ref = TerminalRuntime._artifact_ref(artifact_refs, prefix=prefix)
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
