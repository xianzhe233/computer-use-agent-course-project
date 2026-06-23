from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence, TypeAlias, cast

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from .runtime_state import RuntimeState

TerminalDecisionKind = Literal["tool_call", "finish_request"]
ChatContent: TypeAlias = str | list[dict[str, Any]]
ChatMessage: TypeAlias = dict[str, ChatContent]


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

    def complete(self, messages: Sequence[ChatMessage], *, temperature: float = 0.0) -> str:
        model = self._model.bind(temperature=temperature) if temperature != 0 else self._model
        try:
            response = model.invoke(_to_langchain_messages(messages))
        except Exception as exc:
            raise RuntimeError(f"chat completion failed: {type(exc).__name__}: {exc}") from exc

        content = response.content
        if isinstance(content, str):
            if not content.strip():
                raise RuntimeError("chat completion message content is empty")
            return content
        if isinstance(content, list):
            text_parts: list[str] = []
            for part in content:
                if isinstance(part, str):
                    text_parts.append(part)
                elif isinstance(part, dict):
                    text_value = part.get("text")
                    if isinstance(text_value, str):
                        text_parts.append(text_value)
            text = "\n".join(part for part in text_parts if part.strip())
            if text.strip():
                return text
        raise RuntimeError("chat completion message content is empty")


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
        response = self.client.complete(messages)
        return parse_terminal_agent_decision(response)

    def _build_messages(
        self,
        *,
        state: RuntimeState,
        workspace: Path,
        history: Sequence[dict[str, object]],
    ) -> list[ChatMessage]:
        recent_history = [_compact_history_item(item, self.output_char_limit) for item in history]
        context_payload = {
            "task": {
                "user_request": state.task.user_request,
                "goal_summary": state.task.goal_summary,
                "constraints": [
                    "只能使用 run_command 工具",
                    "不能请求或使用 GUI 工具、截图工具、点击、输入、热键或元素定位",
                    "每轮只能输出一个动作",
                    "当前命令工作目录就是 workspace",
                    "不需要 examiner；认为任务完成时直接提交 finish_request",
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
        return [
            {"role": "system", "content": TERMINAL_AGENT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "请基于以下 JSON 上下文决定下一步动作，只输出一个 JSON 对象：\n"
                + json.dumps(context_payload, ensure_ascii=False, indent=2),
            },
        ]


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
        response = self.client.complete(messages)
        return parse_computer_agent_decision(response)

    def _build_messages(
        self,
        *,
        state: RuntimeState,
        workspace: Path,
        history: Sequence[dict[str, object]],
    ) -> list[ChatMessage]:
        recent_history = [_compact_history_item(item, self.output_char_limit) for item in history]
        context_payload = {
            "task": {
                "user_request": state.task.user_request,
                "goal_summary": state.task.goal_summary,
                "task_type": state.task.task_type,
                "constraints": [
                    "可以使用终端与已开放的 GUI 工具，但每轮只能输出一个动作",
                    "不需要 examiner；认为任务完成时直接提交 finish_request",
                    "GUI 操作优先遵循 主动观察截图 -> 定位/单步动作 -> 再观察 的节奏",
                    "如果需要知道当前屏幕状态，主动调用 take_screenshot；如果需要回看历史证据，主动调用 view_screenshot",
                    "你主动采集或回看的 latest_screenshot 会作为多模态图片直接附在下一轮消息中；不要只根据文件路径臆测屏幕状态",
                    "命令成功不等于 GUI 状态正确；凡是窗口、弹窗、输入结果等视觉状态，都要用截图确认",
                    "locate_element 依赖 latest_screenshot；如果没有截图或界面变化明显，先 take_screenshot",
                    "点击定位结果时优先使用 click {target: 'last_located'}，不要在定位失败后盲点",
                    "当前命令工作目录就是 workspace",
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
        user_text = "请基于以下 JSON 上下文和你已主动采集/回看的附带截图决定下一步动作，只输出一个 JSON 对象：\n" + json.dumps(
            context_payload,
            ensure_ascii=False,
            indent=2,
        )
        user_content = _build_multimodal_user_content(
            text=user_text,
            screenshot_path=state.observation.latest_screenshot_path,
            screenshot_id=state.observation.latest_screenshot_id,
        )
        return [
            {"role": "system", "content": COMPUTER_AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]


COMPUTER_AGENT_SYSTEM_PROMPT = """
你是一个 Windows computer use 自主 agent。你要根据用户任务、最近工具结果、截图/定位元信息和运行状态，逐步决定下一步。

硬性规则：
1. 每轮只能输出一个 JSON 对象；不要输出 Markdown、解释文字或代码块。
2. 若还需要观察或操作环境，输出 tool_call；若你认为任务已完成，输出 finish_request。
3. 只能调用 allowed_tools 中列出的工具；不要虚构工具或一次请求多个工具。
4. 视觉观察必须由你主动选择：需要看当前屏幕时调用 take_screenshot；需要回看历史截图时调用 view_screenshot。
5. take_screenshot/view_screenshot 执行后的下一轮会把对应截图作为图片内容附上；如果本轮有 latest_screenshot 图片，你必须直接观察图片内容。
6. 命令成功不等于 GUI 状态正确；凡是窗口是否打开、弹窗是否出现、文本是否输入、保存是否完成等视觉状态，都要用截图确认，不能只看 exit_code。
7. GUI 任务优先遵循 take_screenshot -> locate_element -> 单步动作 -> take_screenshot 的节奏；定位失败后不要盲点，应重新截图、等待或换策略。
8. 命令应短小、非交互、可在 PowerShell 中执行；避免破坏性命令、系统级修改、无限等待和需要人工输入的命令。
9. GUI 动作应原子化；执行点击、输入、快捷键、拖拽后运行时会自动补截图证据。
10. 任务完成前尽量用命令输出、截图、定位结果或 GUI 后截图作为证据。
11. 不要只根据截图路径、截图编号或 expected_observation 判断界面；没有图片内容就先请求截图。

可用工具 schema：
- run_command: {"command": "PowerShell 命令"}
- take_screenshot: {"description": "这张截图用于观察/证明什么"}
- view_screenshot: {"screenshot_id": "ss_0001"}
- locate_element: {"query": "要定位的控件或元素描述"}
- click: {"x": 100, "y": 200, "button": "left", "clicks": 1} 或 {"target": "last_located"}
- type_text: {"text": "要输入的文本", "x": 100, "y": 200, "clear": false, "caret_position": "idle", "press_enter": false}
- hotkey: {"shortcut": "ctrl+s"}
- drag: {"x1": 100, "y1": 100, "x2": 300, "y2": 300}
- wait: {"seconds": 1}

工具调用 JSON：
{
  "kind": "tool_call",
  "thought_summary": "简短说明为什么要执行这一步",
  "tool_name": "工具名",
  "tool_args": {"参数名": "参数值"},
  "expected_observation": "你期望执行后看到什么"
}

完成申请 JSON：
{
  "kind": "finish_request",
  "completion_claim": "说明任务已经如何完成",
  "supporting_evidence": ["command:cmd_0001", "screenshot:ss_0002", "location:loc_0003"],
  "remaining_uncertainty": "若无不确定性则写空字符串"
}
""".strip()


TERMINAL_AGENT_SYSTEM_PROMPT = """
你是一个 Windows PowerShell 终端自主 agent。你要根据用户任务、最近命令输出和运行状态，逐步决定下一步。

硬性规则：
1. 你只能使用一个工具：run_command。
2. 禁止请求 GUI 工具，包括 take_screenshot、click、type_text、hotkey、drag、locate_element 等。
3. 每轮只能输出一个 JSON 对象；不要输出 Markdown、解释文字或代码块。
4. 若还需要观察或操作环境，输出 tool_call；若你认为任务已完成，输出 finish_request。
5. 命令应短小、非交互、可在 PowerShell 中执行；避免破坏性命令、系统级修改、无限等待和需要人工输入的命令。
6. 任务完成前优先用命令验证结果，例如读取文件、列目录、检查退出码或输出内容。

可输出的 JSON schema 只有以下两种：

工具调用：
{
  "kind": "tool_call",
  "thought_summary": "简短说明为什么要执行这条命令",
  "tool_name": "run_command",
  "tool_args": {"command": "PowerShell 命令"},
  "expected_observation": "你期望从命令输出中看到什么"
}

完成申请：
{
  "kind": "finish_request",
  "completion_claim": "说明任务已经如何完成",
  "supporting_evidence": ["command:cmd_0001"],
  "remaining_uncertainty": "若无不确定性则写空字符串"
}
""".strip()


def _build_multimodal_user_content(
    *,
    text: str,
    screenshot_path: str,
    screenshot_id: str,
) -> ChatContent:
    if not screenshot_path:
        return text

    path = Path(screenshot_path)
    data_url = _screenshot_data_url(path)
    if not data_url:
        return text + f"\n\n[latest_screenshot:{screenshot_id} 存在于 {screenshot_path}，但本轮未能附加图片内容。]"

    return [
        {
            "type": "text",
            "text": text
            + f"\n\n本轮已附上 latest_screenshot:{screenshot_id} 的实际图片内容；请直接观察图片。",
        },
        {
            "type": "image_url",
            "image_url": {
                "url": data_url,
                "detail": "low",
            },
        },
    ]


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


def _to_langchain_messages(messages: Sequence[ChatMessage]) -> list[SystemMessage | HumanMessage]:
    converted: list[SystemMessage | HumanMessage] = []
    for message in messages:
        role = cast(str, message["role"])
        content = message["content"]
        if role == "system":
            converted.append(SystemMessage(content=cast(Any, content)))
        elif role == "user":
            converted.append(HumanMessage(content=cast(Any, content)))
        else:
            raise ValueError(f"Unsupported chat message role: {role}")
    return converted


def parse_computer_agent_decision(raw_response: str) -> TerminalAgentDecision:
    return parse_terminal_agent_decision(raw_response)


def parse_terminal_agent_decision(raw_response: str) -> TerminalAgentDecision:
    payload = _load_json_object(raw_response)
    kind = str(payload.get("kind", "")).strip()
    if kind not in {"tool_call", "finish_request"}:
        raise TerminalAgentProtocolError("agent decision kind must be tool_call or finish_request")

    if kind == "tool_call":
        tool_args = payload.get("tool_args", {})
        if not isinstance(tool_args, dict):
            raise TerminalAgentProtocolError("tool_args must be an object")
        return TerminalAgentDecision(
            kind="tool_call",
            thought_summary=str(payload.get("thought_summary", "")),
            tool_name=str(payload.get("tool_name", "")),
            tool_args=dict(tool_args),
            expected_observation=str(payload.get("expected_observation", "")),
            raw_response=raw_response,
        )

    return TerminalAgentDecision(
        kind="finish_request",
        completion_claim=str(payload.get("completion_claim", "")),
        supporting_evidence=_string_list(payload.get("supporting_evidence", [])),
        remaining_uncertainty=str(payload.get("remaining_uncertainty", "")),
        raw_response=raw_response,
    )


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


def _load_json_object(raw_response: str) -> dict[str, Any]:
    text = raw_response.strip()
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        loaded = json.loads(_extract_json_object(text))
    if not isinstance(loaded, dict):
        raise TerminalAgentProtocolError("agent response must be a JSON object")
    return loaded


def _extract_json_object(text: str) -> str:
    code_block_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if code_block_match:
        return code_block_match.group(1)

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise TerminalAgentProtocolError("agent response does not contain a JSON object")
    return text[start : end + 1]


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
