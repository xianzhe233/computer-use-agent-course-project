from pathlib import Path

from computer_use_agent.runtime_state import TerminalRunStatus, create_runtime_state


def test_create_runtime_state_has_mvp1_fields() -> None:
    state = create_runtime_state(
        user_request="列出当前目录文件",
        run_id="run_0001",
        root_dir=Path("runs/run_0001"),
    )

    assert state.task.user_request == "列出当前目录文件"
    assert state.task.task_type == "terminal"
    assert state.run.run_id == "run_0001"
    assert Path(state.run.root_dir) == Path("runs/run_0001")
    assert state.run.status == TerminalRunStatus.RUNNING
    assert state.last_action.action_id == ""
    assert state.metrics.step_count == 0
    assert state.errors.last_error_code == ""


def test_terminal_run_status_contains_terminal_end_states() -> None:
    assert TerminalRunStatus.SUCCESS == "success"
    assert TerminalRunStatus.FAILED == "failed"
    assert TerminalRunStatus.ABORTED == "aborted"
