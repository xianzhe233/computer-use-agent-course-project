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

    thought_summary: str = Field(
        default="",
        description=(
            "Briefly state why this exact tool is the best next step, including any "
            "optional parameter choices that make the action safer or more efficient."
        ),
    )
    expected_observation: str = Field(
        default="",
        description=(
            "State the concrete signal you expect from this call, such as a file listing, "
            "a visible dialog state, a changed field value, or evidence for finish_request."
        ),
    )


class _FinishRequestArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completion_claim: str = Field(
        description=(
            "Concise claim of the completed result. Mention the user-visible or file-system "
            "state that now satisfies the request, not just that actions were attempted."
        )
    )
    supporting_evidence: list[str] = Field(
        default_factory=list,
        description=(
            "Evidence references that prove completion, such as command:cmd_0001 for "
            "terminal output or screenshot:ss_0002 for visual confirmation. Include the "
            "strongest recent artifacts before finishing."
        ),
    )
    remaining_uncertainty: str = Field(
        default="",
        description=(
            "Any remaining uncertainty or missing evidence. Use an empty string only when "
            "the cited command output or screenshots are enough for examiner review."
        ),
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
            description=(
                "Run a short, non-interactive PowerShell command inside the current workspace. "
                "Prefer this for file checks, data processing, app discovery, and other tasks "
                "that are faster or more reliable than GUI interaction."
            ),
            field_definitions={
                "command": (
                    str,
                    Field(
                        description=(
                            "PowerShell command to execute. Keep it deterministic and "
                            "non-interactive; combine simple inspection steps when that reduces "
                            "round trips without hiding important evidence."
                        )
                    ),
                ),
            },
        )
    }


def _observation_tool_definitions() -> dict[str, BaseTool]:
    return {
        "take_screenshot": _schema_tool(
            name="take_screenshot",
            description=(
                "Capture a fresh screenshot for visual observation or completion evidence. "
                "Use it before semantic GUI targeting, after visible UI changes, or before "
                "finish_request when visual state matters."
            ),
            field_definitions={
                "description": (
                    str,
                    Field(
                        description=(
                            "Purpose of the screenshot, including the UI state, target, or "
                            "completion evidence it should confirm."
                        )
                    ),
                ),
            },
        ),
        "view_screenshot": _schema_tool(
            name="view_screenshot",
            description=(
                "Select one or more historical screenshots to review in the next multimodal "
                "turn. Use screenshot_ids to compare states or gather visual evidence without "
                "capturing another screenshot."
            ),
            field_definitions={
                "screenshot_id": (
                    str | None,
                    Field(default=None, description="Single screenshot id to review when only one is needed."),
                ),
                "screenshot_ids": (
                    list[str],
                    Field(
                        default_factory=list,
                        description=(
                            "Multiple screenshot ids to review together, ordered by relevance "
                            "or time, when comparison is useful."
                        ),
                    ),
                ),
            },
        ),
    }


def _pointer_tool_definitions() -> dict[str, BaseTool]:
    common_target_fields = {
        "target_query": (
            str | None,
            Field(
                default=None,
                description=(
                    "Semantic description of the target. Prefer this over coordinates; include "
                    "visible text/control type, window or panel, and nearby layout clues."
                ),
            ),
        ),
        "x": (
            int | None,
            Field(
                default=None,
                description=(
                    "Screen x coordinate. Use only when semantic targeting is unavailable, "
                    "failed, or the target is a precise coordinate-based location."
                ),
            ),
        ),
        "y": (
            int | None,
            Field(
                default=None,
                description=(
                    "Screen y coordinate. Pair with x only when semantic targeting is not the "
                    "best option or a prior observation gives reliable coordinates."
                ),
            ),
        ),
    }
    return {
        "click": _schema_tool(
            name="click",
            description=(
                "Click a UI target. Prefer target_query for visible controls because it lets "
                "the runtime locate the element from the current screenshot; use coordinates "
                "only for known exact positions."
            ),
            field_definitions={
                **common_target_fields,
                "button": (
                    Literal["left", "right", "middle"],
                    Field(
                        default="left",
                        description=(
                            "Mouse button to click. Keep left for normal activation; use right "
                            "only when opening a context menu."
                        ),
                    ),
                ),
                "clicks": (
                    int,
                    Field(
                        default=1,
                        description=(
                            "Number of clicks. Use 1 for buttons, links, and fields; use 2 "
                            "only for items that conventionally require opening/selection."
                        ),
                    ),
                ),
            },
        ),
        "double_click": _schema_tool(
            name="double_click",
            description=(
                "Double-click a UI target that requires open/activate behavior. Prefer "
                "target_query with a specific visible label or item context instead of raw coordinates."
            ),
            field_definitions=dict(common_target_fields),
        ),
        "right_click": _schema_tool(
            name="right_click",
            description=(
                "Right-click a UI target to open a context menu. Prefer target_query so the "
                "runtime can locate the intended item before showing the menu."
            ),
            field_definitions=dict(common_target_fields),
        ),
        "move_mouse": _schema_tool(
            name="move_mouse",
            description=(
                "Move the mouse to a UI target without clicking. Prefer target_query when "
                "positioning over a visible control, menu, icon, or list item."
            ),
            field_definitions=dict(common_target_fields),
        ),
        "hover": _schema_tool(
            name="hover",
            description=(
                "Move the mouse to a UI target and hover briefly, useful for tooltips, hover "
                "menus, and revealing hidden controls. Prefer target_query over coordinates."
            ),
            field_definitions={
                **common_target_fields,
                "duration_ms": (
                    int,
                    Field(
                        default=500,
                        description=(
                            "Hover duration in milliseconds. Use 500-1000 for normal tooltips; "
                            "increase only when a UI is slow to reveal hover content."
                        ),
                    ),
                ),
            },
        ),
        "scroll": _schema_tool(
            name="scroll",
            description=(
                "Scroll within the current UI. Provide target_query when a specific pane, list, "
                "editor, or page region should receive the scroll instead of the active window."
            ),
            field_definitions={
                **common_target_fields,
                "direction": (
                    Literal["up", "down", "left", "right"],
                    Field(
                        default="down",
                        description="Scroll direction relative to the content you want to reveal.",
                    ),
                ),
                "amount": (
                    int,
                    Field(
                        default=1,
                        description=(
                            "Scroll wheel steps. Use small values (1-3) for controlled search; "
                            "larger values only when moving through long content."
                        ),
                    ),
                ),
            },
        ),
        "drag": _schema_tool(
            name="drag",
            description=(
                "Drag from one target to another. Prefer semantic start_query/end_query for "
                "sliders, resize handles, selected items, and drop targets; use coordinates only "
                "for precise canvas-style interactions."
            ),
            field_definitions={
                "start_query": (
                    str | None,
                    Field(
                        default=None,
                        description=(
                            "Semantic description of the drag start target, with visible text, "
                            "control type, or nearby layout clues."
                        ),
                    ),
                ),
                "end_query": (
                    str | None,
                    Field(
                        default=None,
                        description=(
                            "Semantic description of the drop/end target or final handle "
                            "position when it can be described visually."
                        ),
                    ),
                ),
                "x1": (
                    int | None,
                    Field(default=None, description="Drag start x coordinate when not using start_query."),
                ),
                "y1": (
                    int | None,
                    Field(default=None, description="Drag start y coordinate when not using start_query."),
                ),
                "x2": (
                    int | None,
                    Field(default=None, description="Drag end x coordinate when not using end_query."),
                ),
                "y2": (
                    int | None,
                    Field(default=None, description="Drag end y coordinate when not using end_query."),
                ),
            },
        ),
    }


def _text_and_keyboard_tool_definitions() -> dict[str, BaseTool]:
    return {
        "type_text": _schema_tool(
            name="type_text",
            description=(
                "Type text into the current focus or a semantically targeted input field. "
                "Use target_query when focus is uncertain; set clear, caret_position, and "
                "press_enter deliberately to avoid extra click/hotkey steps."
            ),
            field_definitions={
                "text": (
                    str,
                    Field(
                        description=(
                            "Exact text to type. Include newlines only when the target field or "
                            "editor should receive multi-line input."
                        )
                    ),
                ),
                "target_query": (
                    str | None,
                    Field(
                        default=None,
                        description=(
                            "Semantic description of the target input field. Prefer this when "
                            "focus may be wrong; include label, placeholder, role, and nearby UI."
                        ),
                    ),
                ),
                "x": (
                    int | None,
                    Field(default=None, description="Optional x coordinate only when target_query is unsuitable."),
                ),
                "y": (
                    int | None,
                    Field(default=None, description="Optional y coordinate only when target_query is unsuitable."),
                ),
                "clear": (
                    bool,
                    Field(
                        default=False,
                        description=(
                            "Whether to clear existing text first. Set true for replacing field "
                            "contents; leave false when appending or typing into an empty field."
                        ),
                    ),
                ),
                "caret_position": (
                    Literal["start", "idle", "end"],
                    Field(
                        default="idle",
                        description=(
                            "Where to place the caret before typing: start to prepend, end to "
                            "append, idle to keep the current caret/focus behavior."
                        ),
                    ),
                ),
                "press_enter": (
                    bool,
                    Field(
                        default=False,
                        description=(
                            "Whether to press Enter after typing. Set true for searches, chat "
                            "submission, or confirming a field; otherwise leave false."
                        ),
                    ),
                ),
            },
        ),
        "hotkey": _schema_tool(
            name="hotkey",
            description=(
                "Press a keyboard shortcut such as ctrl+s, ctrl+l, enter, escape, or alt+tab. "
                "Use this for reliable app commands when a shortcut is clearer than clicking."
            ),
            field_definitions={
                "shortcut": (
                    str,
                    Field(
                        description=(
                            "Keyboard shortcut to press, written in a pyautogui-compatible form "
                            "such as ctrl+s, alt+tab, enter, or escape."
                        )
                    ),
                )
            },
        ),
    }


def _window_and_wait_tool_definitions() -> dict[str, BaseTool]:
    return {
        "open_app": _schema_tool(
            name="open_app",
            description=(
                "Open an application by its local Start menu name. If the exact name is "
                "unknown, first use run_command with Get-StartApps to discover it."
            ),
            field_definitions={
                "name": (
                    str,
                    Field(
                        description=(
                            "Exact local Start menu app name to open, preferably copied from "
                            "Get-StartApps output instead of guessed from an English alias."
                        )
                    ),
                )
            },
        ),
        "switch_app": _schema_tool(
            name="switch_app",
            description=(
                "Switch to an existing application window by app name. Use this when the app "
                "is already open and you need to bring it forward before acting."
            ),
            field_definitions={
                "name": (
                    str,
                    Field(
                        description=(
                            "Application name to switch to, using the visible app or process name "
                            "rather than a vague description."
                        )
                    ),
                )
            },
        ),
        "focus_window": _schema_tool(
            name="focus_window",
            description=(
                "Focus an existing window by title. Prefer this over switch_app when several "
                "windows from the same app are open or the visible title is known."
            ),
            field_definitions={
                "title": (
                    str,
                    Field(
                        description=(
                            "Visible window title or distinctive title fragment to focus, useful "
                            "when multiple windows share the same app name."
                        )
                    ),
                )
            },
        ),
        "wait": _schema_tool(
            name="wait",
            description=(
                "Wait briefly for the UI, process, animation, or page load to settle. Use a "
                "small explicit wait instead of repeated screenshots when timing is the only uncertainty."
            ),
            field_definitions={
                "seconds": (
                    int,
                    Field(
                        description=(
                            "Seconds to wait, from 0 to 30. Use 1-3 for normal UI settling and "
                            "longer only for known slow app launches or loads."
                        ),
                        ge=0,
                        le=30,
                    ),
                ),
            },
        ),
    }


@lru_cache(maxsize=1)
def _finish_request_tool() -> BaseTool:
    return StructuredTool.from_function(
        func=_schema_only_tool_impl,
        name="finish_request",
        description=(
            "Finish the task only after the current evidence supports completion. Provide a "
            "clear claim, cite recent command/screenshot artifacts as completion evidence, "
            "and disclose uncertainty instead of taking another action."
        ),
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


def _screenshot_data_url(path: Path) -> str:
    if not path.exists():
        return ""

    try:
        from PIL import Image

        with Image.open(path) as image:
            output_image = image.convert("RGB") if image.mode not in {"RGB", "L"} else image.copy()
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
