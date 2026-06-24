from langchain_core.messages import AIMessage

from computer_use_agent.examiner_agent import parse_examiner_ai_message


def test_parse_examiner_view_screenshot_action_with_observation_summary() -> None:
    action = parse_examiner_ai_message(
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "view_screenshot",
                    "args": {
                        "screenshot_id": "ss_0003",
                        "note": "检查最终截图",
                        "observed_findings": ["已经看到了目标文本"],
                        "remaining_questions": ["是否还有保存成功证据"],
                    },
                    "id": "call_1",
                    "type": "tool_call",
                }
            ],
        )
    )

    assert action.kind == "view_screenshot"
    assert action.screenshot_ids == ["ss_0003"]
    assert action.observed_findings == ["已经看到了目标文本"]
    assert action.remaining_questions == ["是否还有保存成功证据"]
