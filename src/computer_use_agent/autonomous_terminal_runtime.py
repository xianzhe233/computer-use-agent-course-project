from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from .examiner_agent import ExaminerProtocol
from .examiner_runtime import RuntimeExaminerLoop
from .graph_runtime import AgentGraphState, compile_linear_agent_graph
from .run_store import RunStore, mark_run_finished
from .runtime_state import RuntimeState, TerminalRunStatus, create_runtime_state
from .computer_agent import (
    AutonomousTerminalAgent,
    LLMTerminalAgent,
    TerminalAgentDecision,
    TerminalAgentProtocolError,
    truncate_text,
)
from .tools.run_command import CommandResult, PowerShellBackend, run_command

ProgressCallback = Callable[[str], None]


class AutonomousTerminalRuntime:
    """Terminal-only autonomous runtime driven by a LangGraph loop."""

    def __init__(
        self,
        workspace: Path,
        runs_root: Path,
        *,
        command_backend: PowerShellBackend | None = None,
        agent: AutonomousTerminalAgent | None = None,
        examiner_agent: ExaminerProtocol | None = None,
        max_steps: int = 50,
        step_timeout_seconds: int = 180,
        max_consecutive_failures: int = 3,
        max_rework_rounds: int = 2,
        max_examiner_steps: int = 20,
        model_config_path: Path = Path("config/models.local.json"),
        model_role: str = "mainAgent",
        examiner_role: str = "examiner",
        progress_callback: ProgressCallback | None = None,
        progress_output_char_limit: int = 1200,
    ) -> None:
        self.workspace = workspace
        self.runs_root = runs_root
        self.command_backend = command_backend or PowerShellBackend()
        self.agent = agent
        self.examiner_agent = examiner_agent
        self.model_config_path = model_config_path
        self.model_role = model_role
        self.examiner_role = examiner_role
        self.max_steps = max_steps
        self.step_timeout_seconds = step_timeout_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.max_rework_rounds = max_rework_rounds
        self.max_examiner_steps = max_examiner_steps
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
            task_type="terminal",
            allowed_tools=["run_command"],
            max_steps=self.max_steps,
            step_timeout_seconds=self.step_timeout_seconds,
            max_rework_rounds=self.max_rework_rounds,
            max_examiner_steps=self.max_examiner_steps,
            examiner_enabled=True,
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
                "mode": "autonomous_terminal",
                "allowed_tools": state.control.allowed_tools,
                "examiner_enabled": state.control.examiner_enabled,
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
            state.run.current_phase = "main_loop"
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
            self._emit_tool_call(step_id=next_step, decision=decision)
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
                mark_run_finished(
                    state,
                    TerminalRunStatus.FAILED,
                    f"Agent emitted invalid terminal action: {validation_error['message']}",
                )
                return {"step_id": step_id, "terminated": True, "route": "terminated"}

            store.append_trace(
                step_id=step_id,
                actor="runtime",
                event_type="tool_validation",
                payload={"tool_name": "run_command", "tool_args": decision.tool_args},
                status="success",
            )
            return {"step_id": step_id, "route": "execute"}

        def execute_node(graph_state: AgentGraphState) -> AgentGraphState:
            decision = cast(TerminalAgentDecision, graph_state["decision"])
            step_id = int(graph_state["step_id"])
            action_id = str(graph_state["action_id"])
            self._emit(f"Running   : timeout={self.step_timeout_seconds}s")
            result, artifact_refs, command_result_id = self._execute_command_step(
                store=store,
                step_id=step_id,
                decision=decision,
            )
            self._apply_command_result(
                state=state,
                step_id=step_id,
                action_id=action_id,
                decision=decision,
                result=result,
                artifact_refs=artifact_refs,
                command_result_id=command_result_id,
            )
            history.append(
                self._history_item(
                    step_id=step_id,
                    decision=decision,
                    result=result,
                    artifact_refs=artifact_refs,
                )
            )
            result_payload = asdict(result)
            result_payload["tool_name"] = "run_command"
            self._emit_command_result(step_id=step_id, result=result, artifact_refs=artifact_refs)
            store.append_trace(
                step_id=step_id,
                actor="tool_runtime",
                event_type="tool_execution",
                payload={"result": result_payload},
                status="success" if result.success else "failed",
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
                status="success" if result.success else "failed",
                artifact_refs=artifact_refs,
            )
            if state.metrics.consecutive_failures >= self.max_consecutive_failures:
                state.errors.blocked = True
                state.errors.block_reason = "连续命令失败次数达到上限，停止继续自主执行。"
                mark_run_finished(
                    state,
                    TerminalRunStatus.ABORTED,
                    "Maximum consecutive command failures reached",
                )
                self._emit("Abort     : maximum consecutive command failures reached")
                return {"step_id": step_id, "terminated": True, "route": "terminated"}
            return {
                "step_id": step_id,
                "artifact_refs": artifact_refs,
                "command_result_id": command_result_id,
                "route": "loop",
            }

        def finish_node(graph_state: AgentGraphState) -> AgentGraphState:
            decision = cast(TerminalAgentDecision, graph_state["decision"])
            step_id = int(graph_state["step_id"])
            action_id = str(graph_state["action_id"])
            completion_claim = decision.completion_claim.strip() or "Agent requested finish"
            self._emit_finish_request(decision=decision, completion_claim=completion_claim)
            state.pending_finish.requested = True
            state.pending_finish.request_step = step_id
            state.pending_finish.completion_claim = completion_claim
            state.pending_finish.supporting_evidence = list(decision.supporting_evidence)
            state.pending_finish.remaining_uncertainty = decision.remaining_uncertainty
            state.last_action.action_id = action_id
            state.last_action.actor = "main_agent"
            state.last_action.action_type = "finish_request"
            state.last_action.action_args = {
                "completion_claim": completion_claim,
                "supporting_evidence": decision.supporting_evidence,
                "remaining_uncertainty": decision.remaining_uncertainty,
            }
            state.last_action.result_status = "pending"
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

            review_result = self._examiner_loop().review(state=state, store=store, history=history)
            decision_name = str(review_result["decision"])
            if decision_name == "accept":
                mark_run_finished(state, TerminalRunStatus.SUCCESS, str(review_result["reason"]))
                state.pending_finish.requested = False
                state.last_action.actor = "examiner"
                state.last_action.action_type = "examiner_accept"
                state.last_action.action_args = {
                    "completion_claim": completion_claim,
                    "supporting_evidence": decision.supporting_evidence,
                }
                state.last_action.result_status = "success"
                state.last_action.result_summary = str(review_result["reason"])
                state.last_action.artifact_refs = list(review_result.get("artifact_refs", []))
                return {"step_id": step_id, "terminated": True, "route": "end"}

            if decision_name == "reject":
                state.metrics.rework_count += 1
                state.pending_finish.requested = False
                state.run.current_phase = "main_loop"
                state.last_action.actor = "examiner"
                state.last_action.action_type = "examiner_reject"
                state.last_action.action_args = {
                    "reason": review_result["reason"],
                    "missing_evidence": review_result["missing_evidence"],
                    "suggested_next_steps": review_result["suggested_next_steps"],
                }
                state.last_action.result_status = "failed"
                state.last_action.result_summary = str(review_result["reason"])
                state.last_action.artifact_refs = list(review_result.get("artifact_refs", []))
                if state.metrics.rework_count > state.control.max_rework_rounds:
                    mark_run_finished(state, TerminalRunStatus.ABORTED, "Maximum examiner rework rounds reached")
                    self._emit("Abort     : maximum examiner rework rounds reached")
                    return {"step_id": step_id, "terminated": True, "route": "terminated"}
                self._emit("Examiner  : rejected finish request; returning to main loop")
                return {"step_id": step_id, "route": "loop"}

            mark_run_finished(state, TerminalRunStatus.ABORTED, str(review_result["reason"]))
            state.last_action.actor = "examiner"
            state.last_action.action_type = "examiner_abort"
            state.last_action.action_args = {
                "reason": review_result["reason"],
                "missing_evidence": review_result["missing_evidence"],
                "suggested_next_steps": review_result["suggested_next_steps"],
            }
            state.last_action.result_status = "failed"
            state.last_action.result_summary = str(review_result["reason"])
            state.last_action.artifact_refs = list(review_result.get("artifact_refs", []))
            return {"step_id": step_id, "terminated": True, "route": "terminated"}

        return compile_linear_agent_graph(
            plan_node=plan_node,
            validate_node=validate_node,
            execute_node=execute_node,
            finish_node=finish_node,
            route_after_plan=lambda graph_state: cast(Any, graph_state["route"]),
            route_after_validate=lambda graph_state: cast(Any, graph_state["route"]),
            route_after_execute=lambda graph_state: cast(Any, graph_state["route"]),
            route_after_finish=lambda graph_state: cast(Any, graph_state["route"]),
        )

    def _examiner_loop(self) -> RuntimeExaminerLoop:
        return RuntimeExaminerLoop(
            model_config_path=self.model_config_path,
            examiner_role=self.examiner_role,
            step_timeout_seconds=self.step_timeout_seconds,
            progress_callback=self.progress_callback,
            examiner_agent=self.examiner_agent,
        )

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
            f"Limits    : max_steps={self.max_steps}, command_timeout={self.step_timeout_seconds}s, examiner=on"
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

    def _emit_tool_call(self, *, step_id: int, decision: TerminalAgentDecision) -> None:
        self._emit("Decision  : tool_call")
        self._emit(f"Tool      : {decision.tool_name or '<empty>'}")
        self._emit(f"Thought   : {decision.thought_summary or '-'}")
        command = decision.tool_args.get("command")
        if isinstance(command, str):
            self._emit("Command   :")
            self._emit(
                _indent_block(
                    truncate_text(command, self.progress_output_char_limit),
                    prefix="  PS> ",
                )
            )

    def _emit_command_result(
        self,
        *,
        step_id: int,
        result: CommandResult,
        artifact_refs: list[str],
    ) -> None:
        status = "OK" if result.success else "FAILED"
        self._emit(
            f"Result    : {status} | exit_code={result.exit_code} | "
            f"duration={result.duration_ms}ms | artifacts={', '.join(artifact_refs)}"
        )
        stdout = _format_terminal_output(result.stdout, limit=self.progress_output_char_limit)
        stderr = _format_terminal_output(result.stderr, limit=self.progress_output_char_limit)
        self._emit("Stdout    :" if stdout else "Stdout    : <empty>")
        if stdout:
            self._emit(_indent_block(stdout))
        self._emit("Stderr    :" if stderr else "Stderr    : <empty>")
        if stderr:
            self._emit(_indent_block(stderr))

    def _emit_run_footer(self, *, state: RuntimeState, run_dir: Path) -> None:
        self._emit("")
        self._emit("=" * 80)
        self._emit(f"DONE      : {state.run.status}")
        self._emit(f"Reason    : {state.run.terminated_reason}")
        self._emit(
            f"Metrics   : steps={state.metrics.step_count}, "
            f"commands={state.metrics.command_count}, reworks={state.metrics.rework_count}, runtime={state.metrics.runtime_seconds}s"
        )
        self._emit(f"Artifacts : {run_dir}")
        self._emit("=" * 80)

    def _agent(self) -> AutonomousTerminalAgent:
        if self.agent is None:
            self.agent = LLMTerminalAgent.from_config_file(
                config_path=self.model_config_path,
                role=self.model_role,
            )
        return self.agent

    def _validate_tool_call(
        self,
        *,
        state: RuntimeState,
        decision: TerminalAgentDecision,
    ) -> dict[str, str] | None:
        if decision.tool_name not in state.control.allowed_tools:
            return {
                "code": "TOOL_NOT_ALLOWED",
                "message": f"Only run_command is allowed, got {decision.tool_name or '<empty>'}",
            }

        command = decision.tool_args.get("command")
        if not isinstance(command, str) or not command.strip():
            return {"code": "INVALID_TOOL_ARGS", "message": "run_command requires command"}

        blocked_reason = _blocked_command_reason(command)
        if blocked_reason:
            return {"code": "RISK_BLOCKED", "message": blocked_reason}

        return None

    def _execute_command_step(
        self,
        *,
        store: RunStore,
        step_id: int,
        decision: TerminalAgentDecision,
    ) -> tuple[CommandResult, list[str], str]:
        command = str(decision.tool_args["command"])
        result = run_command(
            command=command,
            timeout_s=self.step_timeout_seconds,
            cwd=self.workspace,
            backend=self.command_backend,
        )
        artifact_paths = store.write_command_result(step_id=step_id, result=result)
        command_result_id = artifact_paths["command_result_id"]
        return result, [f"command:{command_result_id}"], command_result_id

    def _apply_command_result(
        self,
        *,
        state: RuntimeState,
        step_id: int,
        action_id: str,
        decision: TerminalAgentDecision,
        result: CommandResult,
        artifact_refs: list[str],
        command_result_id: str,
    ) -> None:
        state.metrics.step_count += 1
        state.metrics.tool_call_count += 1
        state.metrics.command_count += 1
        if result.success:
            state.metrics.consecutive_failures = 0
        else:
            state.metrics.consecutive_failures += 1

        result_summary = _summarize_command_result(result)
        state.observation.latest_command_result_id = command_result_id
        state.observation.last_observation_summary = result_summary
        state.last_action.action_id = action_id
        state.last_action.actor = "tool_runtime"
        state.last_action.action_type = "run_command"
        state.last_action.action_args = {
            "command": result.command,
            "thought_summary": decision.thought_summary,
            "expected_observation": decision.expected_observation,
        }
        state.last_action.result_status = "success" if result.success else "failed"
        state.last_action.result_summary = result_summary
        state.last_action.artifact_refs = artifact_refs

        if result.success:
            state.errors.last_error_code = ""
            state.errors.last_error_message = ""
            state.errors.last_failed_tool = ""
            state.errors.blocked = False
            state.errors.block_reason = ""
        else:
            state.errors.last_error_code = "COMMAND_FAILED"
            state.errors.last_error_message = result_summary
            state.errors.last_failed_tool = "run_command"

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

    @staticmethod
    def _history_item(
        *,
        step_id: int,
        decision: TerminalAgentDecision,
        result: CommandResult,
        artifact_refs: list[str],
    ) -> dict[str, object]:
        return {
            "step_id": step_id,
            "thought_summary": decision.thought_summary,
            "command": result.command,
            "expected_observation": decision.expected_observation,
            "exit_code": result.exit_code,
            "success": result.success,
            "stdout": truncate_text(result.stdout),
            "stderr": truncate_text(result.stderr),
            "timed_out": result.timed_out,
            "artifact_refs": artifact_refs,
        }


def _indent_block(text: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line if line else prefix.rstrip() for line in text.splitlines())


def _format_terminal_output(text: str, *, limit: int) -> str:
    stripped = text.strip()
    if not stripped:
        return ""

    if stripped.startswith("#< CLIXML") and "Preparing modules for first use" in stripped:
        return "<PowerShell progress output suppressed>"

    normalized = stripped.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n") if line.strip()]
    cleaned = "\n".join(lines)
    return truncate_text(cleaned, limit)


_BLOCKED_COMMAND_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bRemove-Item\b", re.IGNORECASE), "Remove-Item is blocked"),
    (re.compile(r"\brm\b", re.IGNORECASE), "rm is blocked"),
    (re.compile(r"\bdel\b", re.IGNORECASE), "del is blocked"),
    (re.compile(r"\berase\b", re.IGNORECASE), "erase is blocked"),
    (re.compile(r"\brmdir\b", re.IGNORECASE), "rmdir is blocked"),
    (re.compile(r"\bFormat-Volume\b", re.IGNORECASE), "format commands are blocked"),
    (re.compile(r"\bshutdown\b", re.IGNORECASE), "shutdown is blocked"),
    (re.compile(r"\bRestart-Computer\b", re.IGNORECASE), "restart is blocked"),
    (re.compile(r"\bStop-Computer\b", re.IGNORECASE), "power-off commands are blocked"),
)


def _blocked_command_reason(command: str) -> str:
    for pattern, reason in _BLOCKED_COMMAND_PATTERNS:
        if pattern.search(command):
            return reason
    return ""


def _summarize_command_result(result: CommandResult) -> str:
    stream = result.stdout.strip() if result.stdout.strip() else result.stderr.strip()
    if not stream:
        stream = result.note or f"exit_code={result.exit_code}"
    status = "success" if result.success else "failed"
    return f"{status}, exit_code={result.exit_code}, output={truncate_text(stream, 500)}"
