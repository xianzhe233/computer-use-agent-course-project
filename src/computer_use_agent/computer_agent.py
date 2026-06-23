from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence, TypeAlias, cast

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel, ConfigDict, Field, create_model
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import BaseTool, StructuredTool
from langchain_openai import ChatOpenAI

from .runtime_state import RuntimeState

TerminalDecisionKind = Literal["tool_call", "finish_request"]
ChatContent: TypeAlias = str | list[dict[str, Any]]


class TerminalAgentProtocolError(RuntimeError):
    pass


@dataclass(slots=True)
class TerminalAgentDecision:
    kind: TerminalDecisionKind
    thought_summary: str = ""
    tool_name: str = ""
    tool_args: dict[str, object] = field(default_factory=dict)
    expected_observation: str = ""
    completion_claim: str = ""
    supporting_evidence: list[str] = field(default_factory=list)
    remaining_uncertainty: str = ""
    raw_response: str = ""

    def to_trace_payload(self) -> dict[str, object]:
        payload = asdict(self)
        if not self.raw_response:
            payload.pop("raw_response", None)
        return payload


class AutonomousTerminalAgent(Protocol):
    def decide(
        self,
        *,
        state: RuntimeState,
        workspace: Path,
        history: Sequence[dict[str, object]],
    ) -> TerminalAgentDecision: ...


class AutonomousComputerAgent(Protocol):
    def decide(
        self,
        *,
        state: RuntimeState,
        workspace: Path,
        history: Sequence[dict[str, object]],
    ) -> TerminalAgentDecision: ...


@dataclass(slots=True)
class OpenAICompatibleModelConfig:
    provider: str
    base_url: str
    api_key: str
    model: str


class OpenAICompatibleChatClient:
    def __init__(
        self,
        *,
        config: OpenAICompatibleModelConfig,
        timeout_s: int = 60,
        max_retries: int = 2,
    ) -> None:
        self.config = config
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self._model = ChatOpenAI(
            model=config.model,
            api_key=cast(Any, config.api_key),
            base_url=config.base_url,
            timeout=timeout_s,
            max_retries=max_retries,
            temperature=0,
        )

    @classmethod
    def from_config_file(
        cls,
        config_path: Path,
        *,
        role: str = "mainAgent",
        timeout_s: int = 60,
    ) -> OpenAICompatibleChatClient:
        if not config_path.exists():
            raise FileNotFoundError(f"model config not found: {config_path}")

        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        roles = raw_config.get("roles", {})
        providers = raw_config.get("providers", {})
        role_config = roles.get(role)
        if not isinstance(role_config, dict):
            raise ValueError(f"model role not found in config: {role}")

        provider_name = str(role_config.get("provider", ""))
        provider_config = providers.get(provider_name)
        if not isinstance(provider_config, dict):
            raise ValueError(f"provider not found in config: {provider_name}")

        base_url = str(provider_config.get("baseUrl", "")).strip()
        model = str(role_config.get("model", "")).strip()
        api_key = _resolve_api_key(
            configured_key=str(provider_config.get("apiKey", "")).strip(),
            provider_name=provider_name,
        )
        if not base_url:
            raise ValueError(f"provider {provider_name} has empty baseUrl")
        if not model:
            raise ValueError(f"role {role} has empty model")
        if not api_key:
            raise ValueError(
                f"provider {provider_name} has empty apiKey; set it in config or environment"
            )

        return cls(
            config=OpenAICompatibleModelConfig(
                provider=provider_name,
                base_url=base_url,
                api_key=api_key,
                model=model,
            ),
            timeout_s=timeout_s,
        )

    def invoke(
        self,
        messages: Sequence[BaseMessage],
        *,
        tools: Sequence[BaseTool],
        temperature: float = 0.0,
    ) -> AIMessage:
        model = self._model.bind(temperature=temperature) if temperature != 0 else self._model
        runnable = model.bind_tools(list(tools), tool_choice="required")
        try:
            response = runnable.invoke(list(messages))
        except Exception as exc:
            raise RuntimeError(f"chat completion failed: {type(exc).__name__}: {exc}") from exc

        if not isinstance(response, AIMessage):
            raise RuntimeError("chat completion did not return an AIMessage")
        return response

    def invoke_structured(
        self,
        messages: Sequence[BaseMessage],
        *,
        schema: type[BaseModel],
        temperature: float = 0.0,
    ) -> BaseModel:
        model = self._model.bind(temperature=temperature) if temperature != 0 else self._model
        runnable = model.with_structured_output(schema)
        try:
            response = runnable.invoke(list(messages))
        except Exception as exc:
            raise RuntimeError(f"structured chat completion failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(response, schema):
            raise RuntimeError("structured chat completion returned unexpected payload type")
        return response


class LLMTerminalAgent:
    def __init__(
        self,
        client: OpenAICompatibleChatClient,
        *,
        history_limit: int = 8,
        output_char_limit: int = 2000,
    ) -> None:
        self.client = client
        self.history_limit = history_limit
        self.output_char_limit = output_char_limit
        self.prompt = TERMINAL_AGENT_PROMPT_TEMPLATE

    @classmethod
    def from_config_file(
        cls,
        config_path: Path,
        *,
        role: str = "mainAgent",
        timeout_s: int = 60,
    ) -> LLMTerminalAgent:
        return cls(
            OpenAICompatibleChatClient.from_config_file(
                config_path=config_path,
                role=role,
                timeout_s=timeout_s,
            )
        )

    def decide(
        self,
        *,
        state: RuntimeState,
        workspace: Path,
        history: Sequence[dict[str, object]],
    ) -> TerminalAgentDecision:
        messages = self._build_messages(state=state, workspace=workspace, history=history)
        response = self.client.invoke(messages, tools=self._resolve_tools(state.control.allowed_tools))
        return parse_ai_message_decision(response)

    def _build_messages(
        self,
        *,
        state: RuntimeState,
        workspace: Path,
        history: Sequence[dict[str, object]],
    ) -> list[BaseMessage]:
        recent_history = [_compact_history_item(item, self.output_char_limit) for item in history]
        context_payload = {
            "task": {
                "user_request": state.task.user_request,
                "goal_summary": state.task.goal_summary,
                "constraints": [
                    "当前是 terminal-only 模式",
                    "每轮必须且只能通过一次工具调用返回下一步",
                    "任务完成时调用 finish_request，不要输出普通文本",
                    "当前命令工作目录就是 workspace",
                    "不需要 examiner",
                ],
            },
            "workspace": str(workspace),
            "control": {
                "current_step": state.run.current_step,
                "max_steps": state.control.max_steps,
                "allowed_tools": state.control.allowed_tools,
                "step_timeout_seconds": state.control.step_timeout_seconds,
            },
            "latest_observation": asdict(state.observation),
            "last_action": asdict(state.last_action),
            "recent_command_history": recent_history[-self.history_limit :],
        }
        user_message = HumanMessage(
            content="请根据以下 JSON 上下文，选择恰好一个已绑定工具来执行下一步；完成时调用 finish_request：\n"
            + json.dumps(context_payload, ensure_ascii=False, indent=2)
        )
        return self.prompt.invoke({"input_messages": [user_message]}).to_messages()

    @staticmethod
    def _resolve_tools(allowed_tools: Sequence[str]) -> list[BaseTool]:
        return resolve_langchain_tools(allowed_tools)


class LLMComputerAgent:
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
        self.prompt = COMPUTER_AGENT_PROMPT_TEMPLATE

    @classmethod
    def from_config_file(
        cls,
        config_path: Path,
        *,
        role: str = "mainAgent",
        timeout_s: int = 60,
    ) -> LLMComputerAgent:
        return cls(
            OpenAICompatibleChatClient.from_config_file(
                config_path=config_path,
                role=role,
                timeout_s=timeout_s,
            )
        )

    def decide(
        self,
        *,
        state: RuntimeState,
        workspace: Path,
        history: Sequence[dict[str, object]],
    ) -> TerminalAgentDecision:
        messages = self._build_messages(state=state, workspace=workspace, history=history)
        response = self.client.invoke(messages, tools=self._resolve_tools(state.control.allowed_tools))
        return parse_ai_message_decision(response)

    def _build_messages(
        self,
        *,
        state: RuntimeState,
        workspace: Path,
        history: Sequence[dict[str, object]],
    ) -> list[BaseMessage]:
        recent_history = [_compact_history_item(item, self.output_char_limit) for item in history]
        context_payload = {
            "task": {
                "user_request": state.task.user_request,
                "goal_summary": state.task.goal_summary,
                "task_type": state.task.task_type,
                "constraints": [
                    "可以使用终端与当前已绑定的 GUI 工具，但每轮必须且只能调用一个工具",
                    "任务完成时调用 finish_request，不要输出普通文本",
                    "优先使用 run_command 等命令工具完成任务；只有在 GUI 操作明显更快、命令难以完成，或任务明确要求 GUI 交互时，才优先使用 GUI 工具",
                    "GUI 操作优先遵循 主动观察截图 -> 语义目标/单步动作 -> 再观察 的节奏",
                    "如果需要知道当前屏幕状态，主动调用 take_screenshot；如果需要回看历史证据，主动调用 view_screenshot",
                    "你主动采集或回看的截图会作为多模态图片直接附在下一轮消息中；不要只根据文件路径臆测屏幕状态",
                    "命令成功不等于 GUI 状态正确；凡是窗口、弹窗、输入结果等视觉状态，都要用截图确认",
                    "需要坐标的 GUI 动作优先使用 target_query / start_query / end_query，让 runtime 在内部自动定位",
                    "只有在你拥有可靠坐标证据时才直接填写 x/y",
                    "语义定位失败后不要盲点，应重新截图、等待或换策略",
                    "如果需要打开应用但不确定本机应用名，先用 run_command 执行 Get-StartApps 查询开始菜单应用名称，再把查到的名称写入 open_app.name",
                    "当前命令工作目录就是 workspace",
                    "不需要 examiner",
                ],
            },
            "workspace": str(workspace),
            "control": {
                "current_step": state.run.current_step,
                "max_steps": state.control.max_steps,
                "allowed_tools": state.control.allowed_tools,
                "step_timeout_seconds": state.control.step_timeout_seconds,
            },
            "latest_observation": asdict(state.observation),
            "last_action": asdict(state.last_action),
            "recent_history": recent_history[-self.history_limit :],
        }
        user_text = "请根据以下 JSON 上下文和你已主动采集/回看的附带截图，选择恰好一个已绑定工具来执行下一步；完成时调用 finish_request：\n" + json.dumps(
            context_payload,
            ensure_ascii=False,
            indent=2,
        )
        user_content = _build_multimodal_user_content(
            text=user_text,
            screenshot_ids=state.observation.selected_screenshot_ids,
            screenshot_paths=state.observation.selected_screenshot_paths,
        )
        user_message = HumanMessage(content=cast(Any, user_content))
        return self.prompt.invoke({"input_messages": [user_message]}).to_messages()

    @staticmethod
    def _resolve_tools(allowed_tools: Sequence[str]) -> list[BaseTool]:
        return resolve_langchain_tools(allowed_tools)


COMPUTER_AGENT_SYSTEM_PROMPT = """
你是一个 Windows computer use 自主 agent。
你必须通过 LangChain 绑定给你的工具来行动，不能手写 JSON、不能输出 Markdown、不能输出解释性正文。

硬性规则：
1. 每轮必须且只能调用一个已绑定工具。
2. 若任务已完成，调用 finish_request；若还需要观察或操作环境，调用其他已绑定工具。
3. 只能使用当前真正绑定给你的工具；不要假设未绑定工具可用。
4. 对除 finish_request 以外的每次工具调用，都填写 thought_summary 与 expected_observation。
5. 视觉观察必须由你主动选择：需要看当前屏幕时调用 take_screenshot；需要回看历史截图时调用 view_screenshot。
6. take_screenshot/view_screenshot 执行后的下一轮会把你选中的一张或多张截图作为图片内容附上；你必须直接观察这些图片内容。
7. 优先使用 run_command 等命令工具完成任务；只有在 GUI 操作明显更快、命令难以完成，或任务明确要求 GUI 交互时，才优先使用 GUI 工具。
8. 命令成功不等于 GUI 状态正确；凡是窗口是否打开、弹窗是否出现、文本是否输入、保存是否完成等视觉状态，都要用截图确认，不能只看 exit_code。
9. GUI 任务优先遵循 take_screenshot -> semantic target -> 单步动作 -> take_screenshot 的节奏；语义定位失败后不要盲点，应重新截图、等待或换策略。
10. 对于 click、double_click、right_click、move_mouse、hover、type_text、scroll、drag 等需要坐标的 GUI 动作，优先填写 target_query / start_query / end_query，让运行时在内部自动定位；只有在坐标证据明确可靠时才直接填 x/y。
11. 命令应短小、非交互、可在 PowerShell 中执行；避免破坏性命令、系统级修改、无限等待和需要人工输入的命令。
12. GUI 动作应原子化；执行点击、输入、快捷键、拖拽后运行时会自动补截图证据。
13. 任务完成前尽量用命令输出、截图、定位结果或 GUI 后截图作为证据。
14. 打开应用时优先使用 open_app；如果你不确定 open_app.name 该填什么，先用 run_command 执行 Get-StartApps 查询本机开始菜单应用名称，再根据查询结果填写准确的应用名，不要盲猜英文名、可执行文件名或窗口标题。
""".strip()


TERMINAL_AGENT_SYSTEM_PROMPT = """
你是一个 Windows PowerShell 终端自主 agent。
你必须通过 LangChain 绑定给你的工具来行动，不能手写 JSON、不能输出 Markdown、不能输出解释性正文。

硬性规则：
1. 每轮必须且只能调用一个已绑定工具。
2. terminal-only 模式下只会绑定 run_command 与 finish_request；不要尝试任何 GUI 操作。
3. 若任务已完成，调用 finish_request；否则调用 run_command。
4. 对 run_command 的调用必须填写 thought_summary 与 expected_observation。
5. 命令应短小、非交互、可在 PowerShell 中执行；避免破坏性命令、系统级修改、无限等待和需要人工输入的命令。
6. 任务完成前优先用命令验证结果，例如读取文件、列目录、检查退出码或输出内容。
""".strip()


COMPUTER_AGENT_PROMPT_TEMPLATE = ChatPromptTemplate.from_messages(
    [
        ("system", COMPUTER_AGENT_SYSTEM_PROMPT),
        MessagesPlaceholder("input_messages"),
    ]
)

TERMINAL_AGENT_PROMPT_TEMPLATE = ChatPromptTemplate.from_messages(
    [
        ("system", TERMINAL_AGENT_SYSTEM_PROMPT),
        MessagesPlaceholder("input_messages"),
    ]
)


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
    return {
        "run_command": _schema_tool(
            name="run_command",
            description="Run a short, non-interactive PowerShell command inside the current workspace.",
            field_definitions={
                "command": (
                    str,
                    Field(description="The PowerShell command to execute."),
                )
            },
        ),
        "take_screenshot": _schema_tool(
            name="take_screenshot",
            description="Capture a fresh screenshot for visual observation or evidence.",
            field_definitions={
                "description": (
                    str,
                    Field(description="Why this screenshot is needed or what it should prove."),
                )
            },
        ),
        "view_screenshot": _schema_tool(
            name="view_screenshot",
            description="Select one or more historical screenshots to review in the next multimodal turn.",
            field_definitions={
                "screenshot_id": (
                    str | None,
                    Field(default=None, description="A single screenshot id to review."),
                ),
                "screenshot_ids": (
                    list[str],
                    Field(default_factory=list, description="One or more screenshot ids to review together."),
                ),
            },
        ),
        "click": _schema_tool(
            name="click",
            description="Click a UI target. Prefer semantic targeting with target_query over raw coordinates.",
            field_definitions={
                "target_query": (
                    str | None,
                    Field(default=None, description="Semantic description of the target to click."),
                ),
                "x": (int | None, Field(default=None, description="Screen x coordinate.")),
                "y": (int | None, Field(default=None, description="Screen y coordinate.")),
                "button": (
                    Literal["left", "right", "middle"],
                    Field(default="left", description="Mouse button to click."),
                ),
                "clicks": (
                    int,
                    Field(default=1, description="Number of clicks. Use 1 or 2."),
                ),
            },
        ),
        "double_click": _schema_tool(
            name="double_click",
            description="Double-click a UI target. Prefer semantic targeting with target_query.",
            field_definitions={
                "target_query": (
                    str | None,
                    Field(default=None, description="Semantic description of the target to double-click."),
                ),
                "x": (int | None, Field(default=None, description="Screen x coordinate.")),
                "y": (int | None, Field(default=None, description="Screen y coordinate.")),
            },
        ),
        "right_click": _schema_tool(
            name="right_click",
            description="Right-click a UI target. Prefer semantic targeting with target_query.",
            field_definitions={
                "target_query": (
                    str | None,
                    Field(default=None, description="Semantic description of the target to right-click."),
                ),
                "x": (int | None, Field(default=None, description="Screen x coordinate.")),
                "y": (int | None, Field(default=None, description="Screen y coordinate.")),
            },
        ),
        "move_mouse": _schema_tool(
            name="move_mouse",
            description="Move the mouse to a UI target. Prefer semantic targeting with target_query.",
            field_definitions={
                "target_query": (
                    str | None,
                    Field(default=None, description="Semantic description of the target location."),
                ),
                "x": (int | None, Field(default=None, description="Screen x coordinate.")),
                "y": (int | None, Field(default=None, description="Screen y coordinate.")),
            },
        ),
        "hover": _schema_tool(
            name="hover",
            description="Move the mouse to a UI target and hover briefly. Prefer semantic targeting with target_query.",
            field_definitions={
                "target_query": (
                    str | None,
                    Field(default=None, description="Semantic description of the target location."),
                ),
                "x": (int | None, Field(default=None, description="Screen x coordinate.")),
                "y": (int | None, Field(default=None, description="Screen y coordinate.")),
                "duration_ms": (
                    int,
                    Field(default=500, description="Hover duration in milliseconds."),
                ),
            },
        ),
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
            field_definitions={
                "shortcut": (str, Field(description="The keyboard shortcut to press."))
            },
        ),
        "scroll": _schema_tool(
            name="scroll",
            description="Scroll within the current UI, optionally targeting a semantic region first.",
            field_definitions={
                "target_query": (
                    str | None,
                    Field(default=None, description="Semantic description of the region to scroll within."),
                ),
                "x": (int | None, Field(default=None, description="Optional x coordinate for scrolling.")),
                "y": (int | None, Field(default=None, description="Optional y coordinate for scrolling.")),
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
        "open_app": _schema_tool(
            name="open_app",
            description="Open an application by its local Start menu name.",
            field_definitions={
                "name": (str, Field(description="The exact local app name to open."))
            },
        ),
        "switch_app": _schema_tool(
            name="switch_app",
            description="Switch to an existing application window by app name.",
            field_definitions={
                "name": (str, Field(description="The application name to switch to."))
            },
        ),
        "focus_window": _schema_tool(
            name="focus_window",
            description="Focus an existing window by title.",
            field_definitions={
                "title": (str, Field(description="The window title to focus."))
            },
        ),
        "wait": _schema_tool(
            name="wait",
            description="Wait for a short number of seconds to let the UI settle.",
            field_definitions={
                "seconds": (
                    int,
                    Field(description="How many seconds to wait.", ge=0, le=30),
                )
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


def _build_multimodal_user_content(
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


def truncate_text(text: str, limit: int = 2000) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text

    marker = "\n... <truncated> ...\n"
    if len(marker) >= limit:
        return marker.strip()[:limit]

    remaining = limit - len(marker)
    head = remaining // 2
    tail = remaining - head
    omitted = len(text) - head - tail
    marker = f"\n... <truncated {omitted} chars> ...\n"
    if len(marker) >= limit:
        return marker.strip()[:limit]

    remaining = limit - len(marker)
    head = remaining // 2
    tail = remaining - head
    suffix = text[-tail:] if tail else ""
    return text[:head] + marker + suffix


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _compact_history_item(item: dict[str, object], output_char_limit: int) -> dict[str, object]:
    compacted = dict(item)
    for key in ("stdout", "stderr"):
        value = compacted.get(key)
        if isinstance(value, str):
            compacted[key] = truncate_text(value, output_char_limit)
    return compacted


def _resolve_api_key(*, configured_key: str, provider_name: str) -> str:
    if configured_key and not configured_key.startswith("<"):
        return configured_key
    env_names = [
        re.sub(r"[^A-Za-z0-9]+", "_", provider_name).strip("_").upper() + "_API_KEY",
        "OPENAI_API_KEY",
    ]
    for env_name in env_names:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return ""
