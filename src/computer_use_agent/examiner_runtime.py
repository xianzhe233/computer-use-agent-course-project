from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .agent_common import truncate_text
from .examiner_agent import ExaminerAction, ExaminerProtocol, LLMExaminerAgent
from .run_store import RunStore, mark_run_finished
from .runtime_state import RuntimeState, TerminalRunStatus
from .tools import view_screenshot


class RuntimeExaminerLoop:
    def __init__(
        self,
        *,
        model_config_path: Path,
        examiner_role: str,
        step_timeout_seconds: int,
        progress_callback: Callable[[str], None] | None = None,
        examiner_agent: ExaminerProtocol | None = None,
    ) -> None:
        self.model_config_path = model_config_path
        self.examiner_role = examiner_role
        self.step_timeout_seconds = step_timeout_seconds
        self.progress_callback = progress_callback
        self.examiner_agent = examiner_agent

    def review(
        self,
        *,
        state: RuntimeState,
        store: RunStore,
        history: Sequence[dict[str, object]],
    ) -> dict[str, Any]:
        if not state.control.examiner_enabled:
            completion_claim = state.pending_finish.completion_claim.strip() or "Agent requested finish"
            mark_run_finished(state, TerminalRunStatus.SUCCESS, completion_claim)
            return {
                "decision": "accept",
                "reason": completion_claim,
                "missing_evidence": [],
                "suggested_next_steps": [],
                "artifact_refs": list(state.pending_finish.supporting_evidence),
            }

        review_count = state.examiner.review_count + 1
        state.examiner.review_count = review_count
        state.run.current_phase = "examiner_review"
        state.examiner.selected_screenshot_ids = []
        state.examiner.selected_screenshot_paths = []
        state.examiner.reviewed_screenshot_ids = []
        state.examiner.observed_findings = []
        state.examiner.remaining_questions = []
        state.examiner.observation_log = []
        review_payload = self._build_review_payload(state=state, store=store, history=history)
        input_path = store.write_examiner_review_input(review_count, review_payload)
        examiner_history: list[dict[str, object]] = []
        self._emit(f"Examiner  : review #{review_count} started")

        for examiner_step in range(1, state.control.max_examiner_steps + 1):
            action = self._agent().act(
                state=state,
                review_payload=review_payload,
                history=examiner_history,
            )
            if action.kind == "view_screenshot":
                result, artifact_refs = self._apply_view_screenshot_action(state=state, action=action)
                examiner_history.append(
                    {
                        "step": examiner_step,
                        "kind": action.kind,
                        "screenshot_ids": list(action.screenshot_ids),
                        "note": action.note,
                        "observed_findings": list(action.observed_findings),
                        "remaining_questions": list(action.remaining_questions),
                        "success": result["success"],
                        "error": result.get("error"),
                    }
                )
                store.append_trace(
                    step_id=state.run.current_step,
                    actor="examiner",
                    phase="examiner_review",
                    event_type="examiner_review",
                    payload={
                        "review_count": review_count,
                        "examiner_step": examiner_step,
                        "action": action.to_trace_payload(),
                        "result": result,
                    },
                    status="success" if result["success"] else "failed",
                    artifact_refs=artifact_refs,
                )
                ids_text = ", ".join(action.screenshot_ids) or "<empty>"
                self._emit(f"Examiner  : view_screenshot [{ids_text}]")
                continue

            output_payload = {
                "review_count": review_count,
                "examiner_step": examiner_step,
                "decision": action.decision,
                "reason": action.reason,
                "missing_evidence": list(action.missing_evidence),
                "suggested_next_steps": list(action.suggested_next_steps),
            }
            output_path = store.write_examiner_review_output(review_count, output_payload)
            state.examiner.last_decision = action.decision
            state.examiner.last_reason = action.reason
            state.examiner.missing_evidence = list(action.missing_evidence)
            state.examiner.suggested_next_steps = list(action.suggested_next_steps)
            state.examiner.review_trace_refs = [str(input_path), str(output_path)]
            artifact_refs = list(state.examiner.selected_screenshot_ids)
            screenshot_artifacts = [f"screenshot:{item}" for item in artifact_refs]
            store.append_trace(
                step_id=state.run.current_step,
                actor="examiner",
                phase="examiner_review",
                event_type="examiner_review",
                payload={
                    "review_count": review_count,
                    "examiner_step": examiner_step,
                    "action": action.to_trace_payload(),
                    "output_path": str(output_path),
                },
                status=action.decision,
                artifact_refs=screenshot_artifacts,
            )
            self._emit(f"Examiner  : {action.decision} - {truncate_text(action.reason, 300)}")
            return {
                "decision": action.decision,
                "reason": action.reason,
                "missing_evidence": list(action.missing_evidence),
                "suggested_next_steps": list(action.suggested_next_steps),
                "artifact_refs": screenshot_artifacts,
            }

        output_payload = {
            "review_count": review_count,
            "examiner_step": state.control.max_examiner_steps,
            "decision": "abort",
            "reason": "Examiner reached max review steps before submitting a decision",
            "missing_evidence": [],
            "suggested_next_steps": [],
        }
        output_path = store.write_examiner_review_output(review_count, output_payload)
        state.examiner.last_decision = "abort"
        state.examiner.last_reason = output_payload["reason"]
        state.examiner.missing_evidence = []
        state.examiner.suggested_next_steps = []
        state.examiner.review_trace_refs = [str(input_path), str(output_path)]
        store.append_trace(
            step_id=state.run.current_step,
            actor="examiner",
            phase="examiner_review",
            event_type="examiner_review",
            payload=output_payload,
            status="abort",
            artifact_refs=[],
        )
        self._emit("Examiner  : abort - review step limit reached")
        return {
            "decision": "abort",
            "reason": output_payload["reason"],
            "missing_evidence": [],
            "suggested_next_steps": [],
            "artifact_refs": [],
        }

    def _build_review_payload(
        self,
        *,
        state: RuntimeState,
        store: RunStore,
        history: Sequence[dict[str, object]],
    ) -> dict[str, object]:
        screenshot_index = store.load_screenshot_index()
        command_index = store.load_command_index()
        location_index = store.load_location_index()
        return {
            "task": {
                "user_request": state.task.user_request,
                "goal_summary": state.task.goal_summary,
                "task_type": state.task.task_type,
                "constraints": state.task.constraints,
            },
            "finish_request": {
                "completion_claim": state.pending_finish.completion_claim,
                "supporting_evidence": list(state.pending_finish.supporting_evidence),
                "remaining_uncertainty": state.pending_finish.remaining_uncertainty,
                "request_step": state.pending_finish.request_step,
            },
            "run_summary": {
                "current_step": state.run.current_step,
                "step_count": state.metrics.step_count,
                "tool_call_count": state.metrics.tool_call_count,
                "command_count": state.metrics.command_count,
                "screenshot_count": state.metrics.screenshot_count,
                "rework_count": state.metrics.rework_count,
            },
            "last_action": asdict(state.last_action),
            "latest_observation": asdict(state.observation),
            "recent_main_history": list(history[-12:]),
            "available_screenshots": screenshot_index,
            "available_commands": command_index,
            "available_locations": location_index,
            "previous_examiner_feedback": {
                "last_decision": state.examiner.last_decision,
                "last_reason": state.examiner.last_reason,
                "missing_evidence": state.examiner.missing_evidence,
                "suggested_next_steps": state.examiner.suggested_next_steps,
            },
        }

    def _apply_view_screenshot_action(
        self,
        *,
        state: RuntimeState,
        action: ExaminerAction,
    ) -> tuple[dict[str, Any], list[str]]:
        screenshot_ids = [item.strip() for item in action.screenshot_ids if item.strip()]
        if not screenshot_ids:
            return {
                "success": False,
                "error": {
                    "code": "INVALID_SCREENSHOT_ID",
                    "message": "view_screenshot requires non-empty screenshot_ids",
                },
            }, []

        result = view_screenshot(
            screenshot_ids=screenshot_ids,
            screenshots_dir=Path(state.run.root_dir) / "screenshots",
        )
        if not result.success:
            return {
                "success": False,
                "error": result.error or {"code": "SCREENSHOT_NOT_FOUND", "message": "screenshot not found"},
            }, []

        selected_paths: list[str] = [
            str(item.get("path", "")) for item in result.screenshots if isinstance(item, dict) and item.get("path")
        ]

        state.examiner.selected_screenshot_ids = screenshot_ids
        state.examiner.selected_screenshot_paths = selected_paths
        for screenshot_id in screenshot_ids:
            if screenshot_id not in state.examiner.reviewed_screenshot_ids:
                state.examiner.reviewed_screenshot_ids.append(screenshot_id)
        state.examiner.observed_findings.extend(
            item for item in action.observed_findings if item and item not in state.examiner.observed_findings
        )
        state.examiner.remaining_questions = [item for item in action.remaining_questions if item]
        state.examiner.observation_log.append(
            {
                "screenshot_ids": list(screenshot_ids),
                "note": action.note,
                "observed_findings": list(action.observed_findings),
                "remaining_questions": list(action.remaining_questions),
            }
        )

        return {
            "success": True,
            "note": action.note or "selected screenshots for examiner review",
            "observed_findings": list(action.observed_findings),
            "remaining_questions": list(action.remaining_questions),
            "screenshots": result.screenshots,
        }, [f"screenshot:{item}" for item in screenshot_ids]

    def _agent(self) -> ExaminerProtocol:
        if self.examiner_agent is None:
            self.examiner_agent = LLMExaminerAgent.from_config_file(
                config_path=self.model_config_path,
                role=self.examiner_role,
                timeout_s=self.step_timeout_seconds,
            )
        return self.examiner_agent

    def _emit(self, message: str) -> None:
        if self.progress_callback is not None:
            self.progress_callback(message)
