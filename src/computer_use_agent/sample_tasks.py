from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class DemoTask:
    name: str
    description: str
    user_requests: tuple[str, ...]
    command_plan: tuple[str, ...]
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
        command_plan=("Get-ChildItem",),
        success_hint="命令成功输出当前目录文件列表",
    ),
    DemoTask(
        name="create_demo_note",
        description="创建并读取一个 demo 文本文件，用于演示多步 terminal 动作",
        user_requests=(
            "创建一个 demo 文本文件并读取内容",
            "在当前目录写入 demo.txt 然后显示它的内容",
        ),
        command_plan=(
            "Set-Content -Path demo.txt -Value 'terminal agent demo'",
            "Get-Content -Path demo.txt",
        ),
        success_hint="demo.txt 被成功写入并读取",
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
