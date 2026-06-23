from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence

from .runtime_state import RuntimeState

TerminalDecisionKind = Literal["tool_call", "finish_request"]


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
    ) -> None:
        self.config = config
        self.timeout_s = timeout_s

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

    def complete(self, messages: Sequence[dict[str, str]], *, temperature: float = 0.0) -> str:
        endpoint = self.config.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.config.model,
            "messages": list(messages),
            "temperature": temperature,
        }
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"chat completion failed: HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"chat completion failed: {exc.reason}") from exc

        choices = response_payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("chat completion response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            raise RuntimeError("chat completion choice has no message")
        content = message.get("content", "")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("chat completion message content is empty")
        return content


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
    ) -> list[dict[str, str]]:
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
