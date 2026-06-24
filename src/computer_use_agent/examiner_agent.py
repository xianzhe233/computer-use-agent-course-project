from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence, cast

from langchain_core.messages import BaseMessage, HumanMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from .agent_common import OpenAICompatibleChatClient, TerminalAgentProtocolError
from .agent_prompts import EXAMINER_PROMPT_TEMPLATE
from .agent_tools import build_multimodal_user_content
from .runtime_state import RuntimeState

ExaminerActionKind = Literal["view_screenshot", "submit_decision"]
ExaminerDecisionLiteral = Literal["accept", "reject", "abort"]


@dataclass(slots=True)
class ExaminerAction:
    kind: ExaminerActionKind
    screenshot_ids: list[str] = field(default_factory=list)
    note: str = ""
    observed_findings: list[str] = field(default_factory=list)
    remaining_questions: list[str] = field(default_factory=list)
    decision: str = ""
    reason: str = ""
    missing_evidence: list[str] = field(default_factory=list)
    suggested_next_steps: list[str] = field(default_factory=list)
    raw_response: str = ""

    def to_trace_payload(self) -> dict[str, object]:
        payload = asdict(self)
        if not self.raw_response:
            payload.pop("raw_response", None)
        return payload


class ExaminerProtocol:
    def act(
        self,
        *,
        state: RuntimeState,
        review_payload: dict[str, object],
        history: Sequence[dict[str, object]],
    ) -> ExaminerAction: ...


class _ExaminerViewArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screenshot_id: str | None = Field(default=None, description="A single screenshot id to review.")
    screenshot_ids: list[str] = Field(default_factory=list, description="One or more screenshot ids to review together.")
    note: str = Field(default="", description="Why these screenshots should be inspected next.")
    observed_findings: list[str] = Field(default_factory=list, description="What you confirmed from these screenshots.")
    remaining_questions: list[str] = Field(default_factory=list, description="What is still uncertain after reviewing these screenshots.")


class _ExaminerDecisionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: ExaminerDecisionLiteral = Field(description="Final examiner decision.")
    reason: str = Field(description="Why this decision is appropriate.")
    missing_evidence: list[str] = Field(default_factory=list, description="What evidence is still missing, if any.")
    suggested_next_steps: list[str] = Field(default_factory=list, description="Concrete next steps for the main agent.")


class LLMExaminerAgent:
    def __init__(
        self,
        client: OpenAICompatibleChatClient,
        *,
        history_limit: int = 10,
        output_char_limit: int = 2000,
    ) -> None:
        self.client = client
        self.history_limit = history_limit
        self.output_char_limit = output_char_limit
        self.prompt = EXAMINER_PROMPT_TEMPLATE

    @classmethod
    def from_config_file(
        cls,
        config_path: Path,
        *,
        role: str = "examiner",
        timeout_s: int = 60,
    ) -> "LLMExaminerAgent":
        return cls(
            OpenAICompatibleChatClient.from_config_file(
                config_path=config_path,
                role=role,
                timeout_s=timeout_s,
            )
        )

    def act(
        self,
        *,
        state: RuntimeState,
        review_payload: dict[str, object],
        history: Sequence[dict[str, object]],
    ) -> ExaminerAction:
        messages = self._build_messages(state=state, review_payload=review_payload, history=history)
        response = self.client.invoke(messages, tools=_examiner_tools())
        return parse_examiner_ai_message(response)

    def _build_messages(
        self,
        *,
        state: RuntimeState,
        review_payload: dict[str, object],
        history: Sequence[dict[str, object]],
    ) -> list[BaseMessage]:
        recent_history = list(history[-self.history_limit :])
        context_payload = {
            "task": {
                "user_request": state.task.user_request,
                "goal_summary": state.task.goal_summary,
                "task_type": state.task.task_type,
                "constraints": state.task.constraints,
            },
            "review": review_payload,
            "examiner_state": asdict(state.examiner),
            "recent_examiner_history": recent_history,
        }
        user_text = "请基于以下 JSON 审阅上下文，选择恰好一个已绑定工具继续审阅；看够证据后调用 submit_examiner_decision：\n" + json.dumps(
            context_payload,
            ensure_ascii=False,
            indent=2,
        )
        user_content = build_multimodal_user_content(
            text=user_text,
            screenshot_ids=state.examiner.selected_screenshot_ids,
            screenshot_paths=state.examiner.selected_screenshot_paths,
        )
        user_message = HumanMessage(content=cast(Any, user_content))
        return self.prompt.invoke({"input_messages": [user_message]}).to_messages()


def parse_examiner_ai_message(message: Any) -> ExaminerAction:
    tool_calls = list(message.tool_calls)
    if not tool_calls:
        raise TerminalAgentProtocolError("examiner response did not contain a tool call")
    if len(tool_calls) != 1:
        raise TerminalAgentProtocolError("examiner response must contain exactly one tool call")

    tool_call = tool_calls[0]
    tool_name = str(tool_call.get("name", "")).strip()
    if not tool_name:
        raise TerminalAgentProtocolError("examiner tool call name must not be empty")

    raw_args = _coerce_tool_args(tool_call.get("args", {}))
    raw_response = json.dumps(
        {
            "content": message.content,
            "tool_calls": tool_calls,
        },
        ensure_ascii=False,
    )

    if tool_name == "submit_examiner_decision":
        return ExaminerAction(
            kind="submit_decision",
            decision=str(raw_args.get("decision", "")),
            reason=str(raw_args.get("reason", "")),
            missing_evidence=_string_list(raw_args.get("missing_evidence", [])),
            suggested_next_steps=_string_list(raw_args.get("suggested_next_steps", [])),
            raw_response=raw_response,
        )
    if tool_name == "view_screenshot":
        screenshot_ids = _string_list(raw_args.get("screenshot_ids", []))
        screenshot_id = str(raw_args.get("screenshot_id", "")).strip()
        if screenshot_id and screenshot_id not in screenshot_ids:
            screenshot_ids = [screenshot_id, *screenshot_ids]
        return ExaminerAction(
            kind="view_screenshot",
            screenshot_ids=screenshot_ids,
            note=str(raw_args.get("note", "")),
            observed_findings=_string_list(raw_args.get("observed_findings", [])),
            remaining_questions=_string_list(raw_args.get("remaining_questions", [])),
            raw_response=raw_response,
        )
    raise TerminalAgentProtocolError(f"unsupported examiner tool: {tool_name}")


def _examiner_tools() -> list[BaseTool]:
    return [
        StructuredTool.from_function(
            func=_schema_only_tool_impl,
            name="view_screenshot",
            description="Select one or more historical screenshots to review in the next multimodal turn.",
            args_schema=_ExaminerViewArgs,
            infer_schema=False,
        ),
        StructuredTool.from_function(
            func=_schema_only_tool_impl,
            name="submit_examiner_decision",
            description="Submit the final examiner decision after reviewing enough evidence.",
            args_schema=_ExaminerDecisionArgs,
            infer_schema=False,
        ),
    ]


def _schema_only_tool_impl(**_: Any) -> str:
    return "schema-only tool placeholder"


def _coerce_tool_args(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): cast(object, item) for key, item in value.items()}
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise TerminalAgentProtocolError("examiner tool arguments must be a JSON object") from exc
        if isinstance(loaded, dict):
            return {str(key): cast(object, item) for key, item in loaded.items()}
    raise TerminalAgentProtocolError("examiner tool arguments must be an object")


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
