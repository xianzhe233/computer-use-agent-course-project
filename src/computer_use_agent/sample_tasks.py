from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

TaskActionKind = Literal["run_command", "take_screenshot", "click", "type_text", "hotkey", "drag", "wait"]
NOTEPAD_START_COMMAND = 'Start-Process -FilePath "$env:WINDIR\\System32\\notepad.exe"'


@dataclass(slots=True)
class PlannedAction:
    tool_name: TaskActionKind
    tool_args: dict[str, object] = field(default_factory=dict)
    expected_observation: str = ""


@dataclass(slots=True)
class DemoTask:
    name: str
    description: str
    user_requests: tuple[str, ...]
    task_type: Literal["terminal", "gui", "hybrid"]
    action_plan: tuple[PlannedAction, ...]
    success_hint: str


DEMO_TASKS: tuple[DemoTask, ...] = (
    DemoTask(
        name="list_workspace_files",
        description="列出当前工作目录文件，用于演示 terminal-only 闭环",
        user_requests=(
            "列出当前目录文件",
            "帮我查看当前工作目录有哪些文件",
            "列出项目根目录文件",
        ),
        task_type="terminal",
        action_plan=(
            PlannedAction(
                tool_name="run_command",
                tool_args={"command": "Get-ChildItem"},
                expected_observation="command output available",
            ),
        ),
        success_hint="命令成功输出当前目录文件列表",
    ),
    DemoTask(
        name="create_demo_note",
        description="创建并读取一个 demo 文本文件，用于演示多步 terminal 动作",
        user_requests=(
            "创建一个 demo 文本文件并读取内容",
            "在当前目录写入 demo.txt 然后显示它的内容",
        ),
        task_type="terminal",
        action_plan=(
            PlannedAction(
                tool_name="run_command",
                tool_args={"command": "Set-Content -Path demo.txt -Value 'terminal agent demo'"},
                expected_observation="demo.txt created",
            ),
            PlannedAction(
                tool_name="run_command",
                tool_args={"command": "Get-Content -Path demo.txt"},
                expected_observation="demo.txt content displayed",
            ),
        ),
        success_hint="demo.txt 被成功写入并读取",
    ),
    DemoTask(
        name="open_notepad_and_type",
        description="打开记事本并输入一行文本，演示 GUI 基础动作与截图证据链",
        user_requests=(
            "打开记事本并输入一行文字",
            "演示一个简单的 gui 任务",
            "打开记事本输入 demo 文本",
        ),
        task_type="gui",
        action_plan=(
            PlannedAction(
                tool_name="take_screenshot",
                tool_args={"description": "before launching notepad"},
                expected_observation="desktop screenshot captured before gui actions",
            ),
            PlannedAction(
                tool_name="run_command",
                tool_args={"command": NOTEPAD_START_COMMAND},
                expected_observation="notepad launch requested",
            ),
            PlannedAction(
                tool_name="wait",
                tool_args={"seconds": 2},
                expected_observation="notepad window should become active",
            ),
            PlannedAction(
                tool_name="type_text",
                tool_args={"text": "computer use agent gui demo", "press_enter": True},
                expected_observation="demo text typed into notepad",
            ),
            PlannedAction(
                tool_name="take_screenshot",
                tool_args={"description": "after typing demo text in notepad"},
                expected_observation="notepad content visible in screenshot",
            ),
        ),
        success_hint="记事本已打开且输入了 demo 文本",
    ),
)


def find_demo_task(user_request: str) -> DemoTask | None:
    normalized = user_request.strip()
    for task in DEMO_TASKS:
        if normalized in task.user_requests:
            return task
    return None


def build_completion_hint(workspace: Path, command_count: int) -> str:
    return f"在 {workspace} 中完成了 {command_count} 条终端命令"
