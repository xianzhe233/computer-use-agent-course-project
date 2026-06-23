from __future__ import annotations

import base64
import io
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Sequence, cast

from langchain_core.messages import AIMessage
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict, Field, create_model

from .agent_common import ChatContent, TerminalAgentDecision, TerminalAgentProtocolError


class _ToolCallEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thought_summary: str = Field(default="", description="Why this step is necessary right now.")
    expected_observation: str = Field(
        default="",
        description="What you expect to observe after this tool call completes.",
    )


class _FinishRequestArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completion_claim: str = Field(description="How the task has been completed.")
    supporting_evidence: list[str] = Field(
        default_factory=list,
        description="Artifact references supporting completion, such as command:cmd_0001 or screenshot:ss_0002.",
    )
    remaining_uncertainty: str = Field(
        default="",
        description="Any remaining uncertainty. Use an empty string if there is none.",
    )


def resolve_langchain_tools(allowed_tools: Sequence[str]) -> list[BaseTool]:
    registry = _tool_registry()
    resolved: list[BaseTool] = []
    seen: set[str] = set()
    for tool_name in allowed_tools:
        normalized = str(tool_name).strip()
        if not normalized or normalized in seen:
            continue
        tool = registry.get(normalized)
        if tool is None:
            raise ValueError(f"Unsupported tool for LangChain binding: {normalized}")
        resolved.append(tool)
        seen.add(normalized)
    resolved.append(_finish_request_tool())
    return resolved


@lru_cache(maxsize=1)
def _tool_registry() -> dict[str, BaseTool]:
    registry: dict[str, BaseTool] = {}
    for group in (
        _terminal_tool_definitions(),
        _observation_tool_definitions(),
        _pointer_tool_definitions(),
        _text_and_keyboard_tool_definitions(),
        _window_and_wait_tool_definitions(),
    ):
        registry.update(group)
    return registry


def _terminal_tool_definitions() -> dict[str, BaseTool]:
    return {
        "run_command": _schema_tool(
            name="run_command",
            description="Run a short, non-interactive PowerShell command inside the current workspace.",
            field_definitions={
                "command": (str, Field(description="The PowerShell command to execute.")),
            },
        )
    }


def _observation_tool_definitions() -> dict[str, BaseTool]:
    return {
        "take_screenshot": _schema_tool(
            name="take_screenshot",
            description="Capture a fresh screenshot for visual observation or evidence.",
            field_definitions={
                "description": (str, Field(description="Why this screenshot is needed or what it should prove.")),
            },
        ),
        "view_screenshot": _schema_tool(
            name="view_screenshot",
            description="Select one or more historical screenshots to review in the next multimodal turn.",
            field_definitions={
                "screenshot_id": (str | None, Field(default=None, description="A single screenshot id to review.")),
                "screenshot_ids": (
                    list[str],
                    Field(default_factory=list, description="One or more screenshot ids to review together."),
                ),
            },
        ),
    }


def _pointer_tool_definitions() -> dict[str, BaseTool]:
    common_target_fields = {
        "target_query": (str | None, Field(default=None, description="Semantic description of the target.")),
        "x": (int | None, Field(default=None, description="Screen x coordinate.")),
        "y": (int | None, Field(default=None, description="Screen y coordinate.")),
    }
    return {
        "click": _schema_tool(
            name="click",
            description="Click a UI target. Prefer semantic targeting with target_query over raw coordinates.",
            field_definitions={
                **common_target_fields,
                "button": (
                    Literal["left", "right", "middle"],
                    Field(default="left", description="Mouse button to click."),
                ),
                "clicks": (int, Field(default=1, description="Number of clicks. Use 1 or 2.")),
            },
        ),
        "double_click": _schema_tool(
            name="double_click",
            description="Double-click a UI target. Prefer semantic targeting with target_query.",
            field_definitions=dict(common_target_fields),
        ),
        "right_click": _schema_tool(
            name="right_click",
            description="Right-click a UI target. Prefer semantic targeting with target_query.",
            field_definitions=dict(common_target_fields),
        ),
        "move_mouse": _schema_tool(
            name="move_mouse",
            description="Move the mouse to a UI target. Prefer semantic targeting with target_query.",
            field_definitions=dict(common_target_fields),
        ),
        "hover": _schema_tool(
            name="hover",
            description="Move the mouse to a UI target and hover briefly. Prefer semantic targeting with target_query.",
            field_definitions={
                **common_target_fields,
                "duration_ms": (int, Field(default=500, description="Hover duration in milliseconds.")),
            },
        ),
        "scroll": _schema_tool(
            name="scroll",
            description="Scroll within the current UI, optionally targeting a semantic region first.",
            field_definitions={
                **common_target_fields,
                "direction": (
                    Literal["up", "down", "left", "right"],
                    Field(default="down", description="Scroll direction."),
                ),
                "amount": (int, Field(default=1, description="Scroll amount or wheel steps.")),
            },
        ),
        "drag": _schema_tool(
            name="drag",
            description="Drag from one point to another. Prefer semantic start_query/end_query over raw coordinates.",
            field_definitions={
                "start_query": (
                    str | None,
                    Field(default=None, description="Semantic description of the drag start target."),
                ),
                "end_query": (
                    str | None,
                    Field(default=None, description="Semantic description of the drag end target."),
                ),
                "x1": (int | None, Field(default=None, description="Drag start x coordinate.")),
                "y1": (int | None, Field(default=None, description="Drag start y coordinate.")),
                "x2": (int | None, Field(default=None, description="Drag end x coordinate.")),
                "y2": (int | None, Field(default=None, description="Drag end y coordinate.")),
            },
        ),
    }


def _text_and_keyboard_tool_definitions() -> dict[str, BaseTool]:
    return {
        "type_text": _schema_tool(
            name="type_text",
            description="Type text into the current focus or a semantically targeted input field.",
            field_definitions={
                "text": (str, Field(description="The text to type.")),
                "target_query": (
                    str | None,
                    Field(default=None, description="Semantic description of the target input field."),
                ),
                "x": (int | None, Field(default=None, description="Optional x coordinate for targeting.")),
                "y": (int | None, Field(default=None, description="Optional y coordinate for targeting.")),
                "clear": (bool, Field(default=False, description="Whether to clear existing text first.")),
                "caret_position": (
                    Literal["start", "idle", "end"],
                    Field(default="idle", description="Where to place the caret before typing."),
                ),
                "press_enter": (bool, Field(default=False, description="Whether to press Enter after typing.")),
            },
        ),
        "hotkey": _schema_tool(
            name="hotkey",
            description="Press a keyboard shortcut such as ctrl+s or alt+tab.",
            field_definitions={"shortcut": (str, Field(description="The keyboard shortcut to press."))},
        ),
    }


def _window_and_wait_tool_definitions() -> dict[str, BaseTool]:
    return {
        "open_app": _schema_tool(
            name="open_app",
            description="Open an application by its local Start menu name.",
            field_definitions={"name": (str, Field(description="The exact local app name to open."))},
        ),
        "switch_app": _schema_tool(
            name="switch_app",
            description="Switch to an existing application window by app name.",
            field_definitions={"name": (str, Field(description="The application name to switch to."))},
        ),
        "focus_window": _schema_tool(
            name="focus_window",
            description="Focus an existing window by title.",
            field_definitions={"title": (str, Field(description="The window title to focus."))},
        ),
        "wait": _schema_tool(
            name="wait",
            description="Wait for a short number of seconds to let the UI settle.",
            field_definitions={
                "seconds": (int, Field(description="How many seconds to wait.", ge=0, le=30)),
            },
        ),
    }


@lru_cache(maxsize=1)
def _finish_request_tool() -> BaseTool:
    return StructuredTool.from_function(
        func=_schema_only_tool_impl,
        name="finish_request",
        description="Finish the task and provide completion evidence instead of taking another action.",
        args_schema=_FinishRequestArgs,
        infer_schema=False,
    )


def _schema_tool(
    *,
    name: str,
    description: str,
    field_definitions: dict[str, tuple[Any, Any]],
) -> BaseTool:
    schema_name = "".join(part.capitalize() for part in name.split("_")) + "Args"
    args_schema = create_model(
        schema_name,
        __base__=_ToolCallEnvelope,
        **cast(Any, field_definitions),
    )
    return StructuredTool.from_function(
        func=_schema_only_tool_impl,
        name=name,
        description=description,
        args_schema=cast(Any, args_schema),
        infer_schema=False,
    )


def _schema_only_tool_impl(**_: Any) -> str:
    return "schema-only tool placeholder"


def parse_ai_message_decision(message: AIMessage) -> TerminalAgentDecision:
    tool_calls = list(message.tool_calls)
    if not tool_calls:
        raise TerminalAgentProtocolError("agent response did not contain a tool call")
    if len(tool_calls) != 1:
        raise TerminalAgentProtocolError("agent response must contain exactly one tool call")

    tool_call = tool_calls[0]
    tool_name = str(tool_call.get("name", "")).strip()
    if not tool_name:
        raise TerminalAgentProtocolError("tool call name must not be empty")

    raw_args = _coerce_tool_args(tool_call.get("args", {}))
    raw_response = json.dumps(
        {
            "content": message.content,
            "tool_calls": tool_calls,
        },
        ensure_ascii=False,
    )

    if tool_name == "finish_request":
        return TerminalAgentDecision(
            kind="finish_request",
            completion_claim=str(raw_args.get("completion_claim", "")),
            supporting_evidence=_string_list(raw_args.get("supporting_evidence", [])),
            remaining_uncertainty=str(raw_args.get("remaining_uncertainty", "")),
            raw_response=raw_response,
        )

    thought_summary = str(raw_args.pop("thought_summary", ""))
    expected_observation = str(raw_args.pop("expected_observation", ""))
    return TerminalAgentDecision(
        kind="tool_call",
        thought_summary=thought_summary,
        tool_name=tool_name,
        tool_args=dict(raw_args),
        expected_observation=expected_observation,
        raw_response=raw_response,
    )


def build_multimodal_user_content(
    *,
    text: str,
    screenshot_ids: Sequence[str],
    screenshot_paths: Sequence[str],
) -> ChatContent:
    selected_pairs = [
        (str(screenshot_id), Path(str(screenshot_path)))
        for screenshot_id, screenshot_path in zip(screenshot_ids, screenshot_paths, strict=False)
        if str(screenshot_id).strip() and str(screenshot_path).strip()
    ]
    if not selected_pairs:
        return text

    missing_images: list[str] = []
    content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": text
            + "\n\n本轮已附上你主动选择的一张或多张截图；请直接观察这些图片内容。",
        }
    ]
    for screenshot_id, path in selected_pairs:
        data_url = _screenshot_data_url(path)
        if not data_url:
            missing_images.append(f"{screenshot_id}:{path}")
            continue
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": data_url,
                    "detail": "low",
                },
            }
        )

    if len(content) == 1:
        return text + "\n\n[所选截图存在，但本轮未能附加图片内容：" + ", ".join(missing_images) + "]"
    if missing_images:
        content[0]["text"] += "\n未成功附加的截图：" + ", ".join(missing_images)
    return content


def _coerce_tool_args(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return {str(key): cast(object, item) for key, item in value.items()}
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise TerminalAgentProtocolError("tool call arguments must be a JSON object") from exc
        if isinstance(loaded, dict):
            return {str(key): cast(object, item) for key, item in loaded.items()}
    raise TerminalAgentProtocolError("tool call arguments must be an object")


def _screenshot_data_url(path: Path, *, max_side: int = 1280) -> str:
    if not path.exists():
        return ""

    try:
        from PIL import Image

        with Image.open(path) as image:
            image.thumbnail((max_side, max_side))
            output_image = image.convert("RGB") if image.mode not in {"RGB", "L"} else image
            buffer = io.BytesIO()
            output_image.save(buffer, format="JPEG", quality=85, optimize=True)
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]
