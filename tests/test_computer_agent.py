import base64
import io
from pathlib import Path

import pytest
from PIL import Image
from langchain_core.messages import AIMessage

from computer_use_agent.runtime_state import create_runtime_state
from computer_use_agent.computer_agent import (
    COMPUTER_AGENT_SYSTEM_PROMPT,
    LLMComputerAgent,
    OpenAICompatibleChatClient,
    OpenAICompatibleModelConfig,
    TERMINAL_AGENT_SYSTEM_PROMPT,
    TerminalAgentProtocolError,
    parse_ai_message_decision,
    resolve_langchain_tools,
    truncate_text,
)


def test_parse_ai_message_tool_call_decision() -> None:
    decision = parse_ai_message_decision(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "run_command",
                    "args": {
                        "thought_summary": "查看文件",
                        "command": "Get-ChildItem",
                        "expected_observation": "文件列表",
                    },
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        )
    )

    assert decision.kind == "tool_call"
    assert decision.tool_name == "run_command"
    assert decision.tool_args == {"command": "Get-ChildItem"}
    assert decision.expected_observation == "文件列表"
    assert decision.thought_summary == "查看文件"


def test_parse_ai_message_finish_request_decision() -> None:
    decision = parse_ai_message_decision(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "finish_request",
                    "args": {
                        "completion_claim": "任务已完成",
                        "supporting_evidence": ["command:cmd_0001"],
                        "remaining_uncertainty": "",
                    },
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        )
    )

    assert decision.kind == "finish_request"
    assert decision.completion_claim == "任务已完成"
    assert decision.supporting_evidence == ["command:cmd_0001"]


def test_parse_ai_message_rejects_missing_tool_calls() -> None:
    with pytest.raises(TerminalAgentProtocolError):
        parse_ai_message_decision(AIMessage(content="done"))


def test_parse_ai_message_rejects_multiple_tool_calls() -> None:
    with pytest.raises(TerminalAgentProtocolError):
        parse_ai_message_decision(
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "run_command", "args": {"command": "pwd"}, "id": "1", "type": "tool_call"},
                    {
                        "name": "finish_request",
                        "args": {"completion_claim": "done"},
                        "id": "2",
                        "type": "tool_call",
                    },
                ],
            )
        )


def test_truncate_text_keeps_short_text_and_compacts_long_text() -> None:
    assert truncate_text("hello", 10) == "hello"

    truncated = truncate_text("a" * 200, 80)

    assert "truncated" in truncated
    assert len(truncated) <= 80
    assert truncated != "a" * 200


def test_resolve_langchain_tools_adds_finish_request() -> None:
    tools = resolve_langchain_tools(["run_command", "take_screenshot"])
    names = [tool.name for tool in tools]

    assert names == ["run_command", "take_screenshot", "finish_request"]


def test_computer_agent_prompt_mentions_langchain_tool_calling_rules() -> None:
    assert "绑定给你的工具" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "finish_request" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "优先使用 run_command 等命令工具完成任务" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "take_screenshot -> semantic target -> 单步动作 -> take_screenshot" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "控件类型或可见文本 + 所在窗口/面板/区域 + 布局参照物" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "不要只写过短、孤立、缺少上下文的目标名称" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "Get-StartApps" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "只有 examiner accept 才算真正完成" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "只有 examiner accept 才算真正完成" in TERMINAL_AGENT_SYSTEM_PROMPT
    assert "terminal-only 模式下只会绑定 run_command 与 finish_request" in TERMINAL_AGENT_SYSTEM_PROMPT


def test_llm_computer_agent_includes_selected_screenshots_as_image_content(tmp_path: Path) -> None:
    screenshot_path_1 = tmp_path / "ss_0001.png"
    screenshot_path_2 = tmp_path / "ss_0002.png"
    Image.new("RGB", (32, 24), "white").save(screenshot_path_1)
    Image.new("RGB", (32, 24), "black").save(screenshot_path_2)
    state = create_runtime_state(
        user_request="观察截图",
        run_id="run_test",
        root_dir=tmp_path,
        task_type="hybrid",
        allowed_tools=["take_screenshot", "view_screenshot"],
        max_steps=3,
    )
    state.observation.latest_screenshot_id = "ss_0002"
    state.observation.latest_screenshot_path = str(screenshot_path_2)
    state.observation.selected_screenshot_ids = ["ss_0001", "ss_0002"]
    state.observation.selected_screenshot_paths = [str(screenshot_path_1), str(screenshot_path_2)]
    client = OpenAICompatibleChatClient(
        config=OpenAICompatibleModelConfig(
            provider="fake",
            base_url="https://example.invalid/v1",
            api_key="fake-key",
            model="fake-model",
        )
    )
    agent = LLMComputerAgent(client)

    messages = agent._build_messages(state=state, workspace=tmp_path, history=[])

    content = messages[1].content
    assert isinstance(content, list)
    first_part = content[0]
    assert isinstance(first_part, dict)
    assert first_part["type"] == "text"
    assert isinstance(first_part.get("text"), str)
    assert "一张或多张截图" in first_part["text"]
    image_parts = [part for part in content[1:] if isinstance(part, dict) and part.get("type") == "image_url"]
    assert len(image_parts) == 2
    assert all(
        isinstance(part.get("image_url"), dict)
        and str(part["image_url"].get("url", "")).startswith("data:image/jpeg;base64,")
        for part in image_parts
    )
    first_url = str(image_parts[0]["image_url"]["url"])
    encoded = first_url.split(",", 1)[1]
    decoded_image = Image.open(io.BytesIO(base64.b64decode(encoded)))
    assert decoded_image.size == (32, 24)
