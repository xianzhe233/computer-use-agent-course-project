import json
from pathlib import Path

import pytest
from PIL import Image

from computer_use_agent.runtime_state import create_runtime_state
from computer_use_agent.terminal_agent import (
    COMPUTER_AGENT_SYSTEM_PROMPT,
    LLMComputerAgent,
    OpenAICompatibleChatClient,
    OpenAICompatibleModelConfig,
    TERMINAL_AGENT_SYSTEM_PROMPT,
    TerminalAgentProtocolError,
    parse_terminal_agent_decision,
    truncate_text,
)


def test_parse_terminal_agent_tool_call_json() -> None:
    decision = parse_terminal_agent_decision(
        json.dumps(
            {
                "kind": "tool_call",
                "thought_summary": "查看文件",
                "tool_name": "run_command",
                "tool_args": {"command": "Get-ChildItem"},
                "expected_observation": "文件列表",
            },
            ensure_ascii=False,
        )
    )

    assert decision.kind == "tool_call"
    assert decision.tool_name == "run_command"
    assert decision.tool_args == {"command": "Get-ChildItem"}
    assert decision.expected_observation == "文件列表"


def test_parse_terminal_agent_finish_request_from_markdown_json_block() -> None:
    decision = parse_terminal_agent_decision(
        """```json
        {
          "kind": "finish_request",
          "completion_claim": "任务已完成",
          "supporting_evidence": ["command:cmd_0001"],
          "remaining_uncertainty": ""
        }
        ```"""
    )

    assert decision.kind == "finish_request"
    assert decision.completion_claim == "任务已完成"
    assert decision.supporting_evidence == ["command:cmd_0001"]


def test_parse_terminal_agent_rejects_unknown_kind() -> None:
    with pytest.raises(TerminalAgentProtocolError):
        parse_terminal_agent_decision('{"kind": "click", "tool_name": "click"}')


def test_truncate_text_keeps_short_text_and_compacts_long_text() -> None:
    assert truncate_text("hello", 10) == "hello"

    truncated = truncate_text("a" * 200, 80)

    assert "truncated" in truncated
    assert len(truncated) <= 80
    assert truncated != "a" * 200


def test_computer_agent_prompt_mentions_new_gui_tools() -> None:
    assert "double_click" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "right_click" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "move_mouse" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "hover" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "scroll" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "open_app" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "switch_app" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "focus_window" in COMPUTER_AGENT_SYSTEM_PROMPT
    assert "double_click、right_click、move_mouse、hover、type_text、hotkey、scroll、drag、open_app、switch_app、focus_window" in TERMINAL_AGENT_SYSTEM_PROMPT


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

    content = messages[1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert "一张或多张截图" in content[0]["text"]
    image_parts = [part for part in content[1:] if part["type"] == "image_url"]
    assert len(image_parts) == 2
    assert all(part["image_url"]["url"].startswith("data:image/jpeg;base64,") for part in image_parts)
