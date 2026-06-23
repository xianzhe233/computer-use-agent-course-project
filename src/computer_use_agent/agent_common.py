from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, Sequence, TypeAlias, cast

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel

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


def compact_history_item(item: dict[str, object], output_char_limit: int) -> dict[str, object]:
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
