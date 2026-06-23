from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence, cast

from langchain_core.messages import BaseMessage, HumanMessage

from .agent_common import (
    AutonomousComputerAgent,
    AutonomousTerminalAgent,
    OpenAICompatibleChatClient,
    OpenAICompatibleModelConfig,
    TerminalAgentDecision,
    TerminalAgentProtocolError,
    compact_history_item,
    truncate_text,
)
from .agent_prompts import (
    COMPUTER_AGENT_PROMPT_TEMPLATE,
    COMPUTER_AGENT_SYSTEM_PROMPT,
    TERMINAL_AGENT_PROMPT_TEMPLATE,
    TERMINAL_AGENT_SYSTEM_PROMPT,
)
from .agent_tools import (
    build_multimodal_user_content,
    parse_ai_message_decision,
    resolve_langchain_tools,
)
from .runtime_state import RuntimeState


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
    ) -> "LLMTerminalAgent":
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
        recent_history = [compact_history_item(item, self.output_char_limit) for item in history]
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
    def _resolve_tools(allowed_tools: Sequence[str]):
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
    ) -> "LLMComputerAgent":
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
        recent_history = [compact_history_item(item, self.output_char_limit) for item in history]
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
        user_content = build_multimodal_user_content(
            text=user_text,
            screenshot_ids=state.observation.selected_screenshot_ids,
            screenshot_paths=state.observation.selected_screenshot_paths,
        )
        user_message = HumanMessage(content=cast(Any, user_content))
        return self.prompt.invoke({"input_messages": [user_message]}).to_messages()

    @staticmethod
    def _resolve_tools(allowed_tools: Sequence[str]):
        return resolve_langchain_tools(allowed_tools)


__all__ = [
    "AutonomousComputerAgent",
    "AutonomousTerminalAgent",
    "COMPUTER_AGENT_SYSTEM_PROMPT",
    "LLMComputerAgent",
    "LLMTerminalAgent",
    "OpenAICompatibleChatClient",
    "OpenAICompatibleModelConfig",
    "TERMINAL_AGENT_SYSTEM_PROMPT",
    "TerminalAgentDecision",
    "TerminalAgentProtocolError",
    "build_multimodal_user_content",
    "parse_ai_message_decision",
    "resolve_langchain_tools",
    "truncate_text",
]
