import json

import pytest

from computer_use_agent.terminal_agent import (
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
