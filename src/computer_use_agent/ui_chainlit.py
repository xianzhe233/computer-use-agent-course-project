from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path
from collections.abc import Awaitable
from typing import Any, cast

import chainlit as cl

from computer_use_agent.autonomous_runtime import AutonomousComputerRuntime

WORKSPACE = Path(".").resolve()
RUNS_ROOT = Path("runs").resolve()
MODEL_CONFIG_CANDIDATES = [
    Path("config/models.local.json").resolve(),
    Path("config/models.json").resolve(),
    Path("config/models.example.json").resolve(),
]

MAX_PAYLOAD_CHARS = 6000
MAX_LOG_CHARS = 8000
MAX_PROGRESS_CHARS = 8000
SENSITIVE_REASONING_KEYS = {"thought", "chain_of_thought", "cot", "raw_reasoning", "raw_response"}
SUMMARY_REASONING_KEYS = ("thought_summary", "reasoning_summary", "summary", "rationale")


def truncate_text(value: Any, limit: int = MAX_PAYLOAD_CHARS) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2)
        except TypeError:
            text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n... [truncated, total {len(text)} chars]"


def safe_read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        print(f"Failed to parse JSON: {path}", flush=True)
        traceback.print_exc()
        return {"_error": f"无法解析 {path.name}"}
    return loaded if isinstance(loaded, dict) else {"_error": f"{path.name} 不是 JSON object"}


def safe_read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            loaded = json.loads(stripped)
        except Exception:
            print(f"Failed to parse JSONL line {line_no}: {path}", flush=True)
            traceback.print_exc()
            rows.append({"_warning": f"跳过无法解析的 JSONL 行：{path.name}:{line_no}"})
            continue
        if isinstance(loaded, dict):
            rows.append(loaded)
    return rows


def resolve_model_config() -> Path | None:
    for path in MODEL_CONFIG_CANDIDATES:
        if path.exists():
            return path
    return None


def resolve_artifact_path(raw_path: Any, run_dir: Path, default_dir: str | None = None) -> Path | None:
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if path.is_absolute():
        return path
    candidates = [run_dir / path]
    if default_dir is not None:
        candidates.append(run_dir / default_dir / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def load_screenshot_index(run_dir: Path) -> dict[str, dict[str, Any]]:
    screenshots: dict[str, dict[str, Any]] = {}
    for item in safe_read_jsonl(run_dir / "screenshots" / "index.jsonl"):
        screenshot_id = item.get("screenshot_id") or item.get("id")
        if screenshot_id:
            screenshots[str(screenshot_id)] = item
    return screenshots


def load_command_index(run_dir: Path) -> dict[str, dict[str, Any]]:
    commands: dict[str, dict[str, Any]] = {}
    for item in safe_read_jsonl(run_dir / "command_logs" / "index.jsonl"):
        command_id = item.get("command_result_id") or item.get("id")
        if command_id:
            commands[str(command_id)] = item
    return commands


def load_run_artifacts(run_dir: Path) -> dict[str, Any]:
    return {
        "summary": safe_read_json(run_dir / "summary.json"),
        "trace": safe_read_jsonl(run_dir / "trace.jsonl"),
        "screenshots": load_screenshot_index(run_dir),
        "commands": load_command_index(run_dir),
    }


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in SENSITIVE_REASONING_KEYS:
                sanitized[key] = "[内部推理或原始模型响应已省略]"
            else:
                sanitized[key] = sanitize_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


def extract_thought_summary(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in SUMMARY_REASONING_KEYS:
        value = payload.get(key)
        if value:
            return truncate_text(value, 1200)
    action = payload.get("action")
    if isinstance(action, dict):
        for key in SUMMARY_REASONING_KEYS:
            value = action.get(key)
            if value:
                return truncate_text(value, 1200)
    return ""


def screenshot_path_from_item(item: dict[str, Any], run_dir: Path) -> Path | None:
    path = resolve_artifact_path(item.get("path"), run_dir, "screenshots")
    if path is not None and path.exists():
        return path
    return None


def screenshot_caption(screenshot_id: str, item: dict[str, Any]) -> str:
    resolution = item.get("resolution") or {}
    if isinstance(resolution, dict):
        size = f"{resolution.get('width', 'N/A')}x{resolution.get('height', 'N/A')}"
    else:
        size = "N/A"
    parts = [
        f"screenshot {screenshot_id}",
        f"step={item.get('step_id', 'N/A')}",
        f"resolution={size}",
    ]
    description = item.get("description")
    if description:
        parts.append(str(description))
    return " · ".join(parts)


def images_for_event(
    event: dict[str, Any],
    run_dir: Path,
    screenshots: dict[str, dict[str, Any]],
) -> tuple[list[cl.Image], list[str]]:
    images: list[cl.Image] = []
    warnings: list[str] = []
    seen: set[str] = set()

    def add_screenshot(screenshot_id: str) -> None:
        if screenshot_id in seen:
            return
        seen.add(screenshot_id)
        item = screenshots.get(screenshot_id)
        if not item:
            warnings.append(f"screenshot `{screenshot_id}` 未在 index 中找到。")
            return
        path = screenshot_path_from_item(item, run_dir)
        if path is None:
            warnings.append(f"screenshot `{screenshot_id}` 文件缺失：`{item.get('path', 'N/A')}`")
            return
        images.append(
            cl.Image(
                path=str(path),
                name=screenshot_caption(screenshot_id, item),
                display="inline",
            )
        )

    artifact_refs = event.get("artifact_refs") or []
    if isinstance(artifact_refs, list):
        for ref in artifact_refs:
            if isinstance(ref, str) and ref.startswith("screenshot:"):
                add_screenshot(ref.split(":", 1)[1])

    if images:
        return images, warnings

    step_id = event.get("step_id")
    if step_id is not None and event.get("event_type") in {"initial_observation", "tool_execution", "examiner_review"}:
        for screenshot_id, item in screenshots.items():
            if item.get("step_id") == step_id:
                add_screenshot(screenshot_id)
    return images, warnings


def commands_for_event(event: dict[str, Any], commands: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    if event.get("event_type") != "tool_execution":
        return []

    attached_commands: list[dict[str, Any]] = []
    seen: set[str] = set()
    artifact_refs = event.get("artifact_refs") or []
    if isinstance(artifact_refs, list):
        for ref in artifact_refs:
            if isinstance(ref, str) and ref.startswith("command:"):
                command_id = ref.split(":", 1)[1]
                seen.add(command_id)
                command = commands.get(command_id)
                if command is not None:
                    attached_commands.append(command)

    if attached_commands:
        return attached_commands

    step_id = event.get("step_id")
    for command_id, command in commands.items():
        if command_id not in seen and command.get("step_id") == step_id:
            attached_commands.append(command)
    return attached_commands


def read_text_artifact(path: Path | None, limit: int = MAX_LOG_CHARS) -> str:
    if path is None:
        return "N/A"
    if not path.exists():
        return f"command log file missing: {path}"
    try:
        return truncate_text(path.read_text(encoding="utf-8", errors="replace"), limit)
    except Exception as exc:
        print(f"Failed to read artifact text: {path}", flush=True)
        traceback.print_exc()
        return f"无法读取文件：{path} ({type(exc).__name__}: {exc})"


def format_command_log(command: dict[str, Any], run_dir: Path) -> str:
    stdout_path = resolve_artifact_path(command.get("stdout_path"), run_dir, "command_logs")
    stderr_path = resolve_artifact_path(command.get("stderr_path"), run_dir, "command_logs")
    return "\n".join(
        [
            "#### Command Log",
            "",
            "```bash",
            truncate_text(str(command.get("command", "N/A")), 2000),
            "```",
            "",
            "#### Exit code",
            "",
            "```text",
            str(command.get("exit_code", "N/A")),
            "```",
            "",
            "#### Stdout",
            "",
            "```text",
            read_text_artifact(stdout_path),
            "```",
            "",
            "#### Stderr",
            "",
            "```text",
            read_text_artifact(stderr_path),
            "```",
        ]
    )


def format_summary(run_id: str, run_dir: Path, summary: dict[str, Any]) -> str:
    if not summary:
        return f"## Run Summary\n\n未找到 `{run_dir / 'summary.json'}`。"
    if summary.get("_error"):
        return f"## Run Summary\n\n{summary['_error']}"

    def get(name: str) -> Any:
        return summary.get(name, "N/A")

    duration = summary.get("duration", summary.get("runtime_seconds", "N/A"))
    return f"""## Run Summary

- Run ID: `{get("run_id") or run_id}`
- Status: `{get("final_status")}`
- Reason: {get("final_reason")}
- Steps: `{get("step_count")}`
- Commands: `{get("command_count")}`
- Screenshots: `{get("screenshot_count")}`
- Examiner reviews: `{get("examiner_review_count")}`
- Started at: `{get("started_at")}`
- Ended at: `{get("ended_at")}`
- Duration: `{duration}`
"""


def format_examiner_event(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    action = payload.get("action") if isinstance(payload, dict) else {}
    result = payload.get("result") if isinstance(payload, dict) else {}
    action = action if isinstance(action, dict) else {}
    result = result if isinstance(result, dict) else {}
    decision = action.get("decision") or payload.get("decision") or event.get("status", "N/A")
    reason = action.get("reason") or payload.get("reason") or result.get("note") or "N/A"
    missing = action.get("missing_evidence") or payload.get("missing_evidence") or []
    next_steps = action.get("suggested_next_steps") or payload.get("suggested_next_steps") or []
    findings = action.get("observed_findings") or result.get("observed_findings") or []
    questions = action.get("remaining_questions") or result.get("remaining_questions") or []
    return "\n".join(
        [
            "### Examiner Review",
            "",
            f"- Step: `{event.get('step_id', 'N/A')}`",
            f"- Examiner step: `{payload.get('examiner_step', 'N/A') if isinstance(payload, dict) else 'N/A'}`",
            f"- Decision: `{decision}`",
            f"- Status: `{event.get('status', 'N/A')}`",
            f"- Time: `{event.get('timestamp', 'N/A')}`",
            f"- Reason: {reason}",
            f"- Missing evidence: {truncate_text(missing, 1200)}",
            f"- Suggested next step: {truncate_text(next_steps, 1200)}",
            f"- Observed findings: {truncate_text(findings, 1200)}",
            f"- Remaining questions: {truncate_text(questions, 1200)}",
            "",
            "#### Payload",
            "",
            "```json",
            truncate_text(sanitize_payload(payload), MAX_PAYLOAD_CHARS),
            "```",
        ]
    )


def format_event(event: dict[str, Any], commands: dict[str, dict[str, Any]], run_dir: Path) -> str:
    if event.get("_warning"):
        return f"### Trace Warning\n\n{event['_warning']}"

    actor = event.get("actor", "unknown")
    event_type = event.get("event_type", "event")
    if str(actor).lower() == "examiner" or "examiner" in str(event_type).lower():
        content = format_examiner_event(event)
    else:
        payload = event.get("payload", {})
        thought_summary = extract_thought_summary(payload)
        parts = [
            f"### Step {event.get('step_id', 'N/A')} · `{actor}` · `{event_type}`",
            "",
            f"- Phase: `{event.get('phase', 'N/A')}`",
            f"- Status: `{event.get('status', 'N/A')}`",
            f"- Time: `{event.get('timestamp', 'N/A')}`",
        ]
        if thought_summary:
            parts.extend(["", "#### Thought Summary", "", thought_summary])
        parts.extend(
            [
                "",
                "#### Payload",
                "",
                "```json",
                truncate_text(sanitize_payload(payload), MAX_PAYLOAD_CHARS),
                "```",
            ]
        )
        content = "\n".join(parts)

    for command in commands_for_event(event, commands):
        content += "\n\n" + format_command_log(command, run_dir)
    return content


async def render_run(run_id: str, runs_root: Path = RUNS_ROOT) -> None:
    run_dir = runs_root / run_id
    if not run_dir.exists():
        await cl.Message(content=f"找不到运行目录：`{run_dir}`").send()
        return

    artifacts = load_run_artifacts(run_dir)
    summary = artifacts["summary"]
    trace = artifacts["trace"]
    screenshots = artifacts["screenshots"]
    commands = artifacts["commands"]

    await cl.Message(content=format_summary(run_id, run_dir, summary)).send()

    trace_path = run_dir / "trace.jsonl"
    if not trace:
        await cl.Message(content=f"无法找到可用 trace：`{trace_path}`，因此只能展示 summary。").send()
        return

    for event in trace:
        content = format_event(event, commands, run_dir)
        images, warnings = images_for_event(event, run_dir, screenshots)
        if warnings:
            content += "\n\n#### Artifact Warnings\n\n" + "\n".join(f"- {item}" for item in warnings)
        await cl.Message(content=content, elements=images).send()

    final_reason = summary.get("final_reason") if isinstance(summary, dict) else "N/A"
    final_status = summary.get("final_status") if isinstance(summary, dict) else "N/A"
    await cl.Message(content=f"## Final Result\n\n- Status: `{final_status or 'N/A'}`\n- Summary: {final_reason or 'N/A'}").send()


def build_runtime(progress_callback=None) -> AutonomousComputerRuntime:
    model_config = resolve_model_config()
    if model_config is None:
        tried = ", ".join(str(path) for path in MODEL_CONFIG_CANDIDATES)
        raise FileNotFoundError(f"找不到模型配置文件，已尝试：{tried}")
    return AutonomousComputerRuntime(
        workspace=WORKSPACE,
        runs_root=RUNS_ROOT,
        max_steps=50,
        step_timeout_seconds=180,
        model_config_path=model_config,
        model_role="mainAgent",
        progress_callback=progress_callback,
    )


def extract_run_id(state: Any) -> str:
    run = getattr(state, "run", None)
    run_id = getattr(run, "run_id", None) if run is not None else None
    if not run_id:
        run_id = getattr(state, "run_id", None)
    return str(run_id or "")


@cl.on_chat_start
async def on_chat_start() -> None:
    await cl.Message(
        content=(
            "输入一个电脑使用任务，系统会启动 agent 执行，"
            "并在结束后展示 summary、trace、命令日志、examiner 检查和截图。\n\n"
            "提示：前端试用时请不要使用 `-w/--watch` 启动，"
            "否则 `runs/` 目录写入可能触发页面 reload。"
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    task = message.content.strip()
    if not task:
        await cl.Message(content="任务不能为空，请输入一个具体的电脑使用任务。").send()
        return

    progress_lines: list[str] = []
    progress_message = cl.Message(content=f"任务已启动：\n\n> {task}\n\nAgent 正在运行，请等待...")
    await progress_message.send()

    def progress_callback(text: str) -> None:
        line = str(text)
        progress_lines.append(line)
        print(line, flush=True)

    async def refresh_progress_until_done(task_future: asyncio.Future[Any]) -> None:
        last_rendered = ""
        while not task_future.done():
            if progress_lines:
                recent_progress = truncate_text("\n".join(progress_lines[-30:]), MAX_PROGRESS_CHARS)
                content = (
                    f"任务运行中：\n\n> {task}\n\n"
                    "最近进度：\n\n```text\n"
                    f"{recent_progress}\n```"
                )
                if content != last_rendered:
                    progress_message.content = content
                    await progress_message.update()
                    last_rendered = content
            await asyncio.sleep(1)

    try:
        runtime = build_runtime(progress_callback=progress_callback)
        run_future: asyncio.Future[Any] = asyncio.ensure_future(
            cast(Awaitable[Any], cl.make_async(runtime.run)(task))
        )
        await refresh_progress_until_done(run_future)
        state = await run_future
        run_id = extract_run_id(state)
        if not run_id:
            await cl.Message(content="Agent 运行结束，但无法从返回 state 中找到 run_id。请检查 runtime.run() 返回结构。").send()
            return

        progress_message.content = f"Agent 运行结束。Run ID: `{run_id}`"
        await progress_message.update()

        if progress_lines:
            await cl.Message(
                content="## Runtime Progress\n\n```text\n"
                + truncate_text("\n".join(progress_lines), MAX_PROGRESS_CHARS)
                + "\n```"
            ).send()

        await render_run(run_id)
    except Exception as exc:
        traceback.print_exc()
        await cl.Message(
            content=(
                "Agent 运行失败。\n\n"
                f"- 错误类型：`{type(exc).__name__}`\n"
                f"- 错误信息：`{str(exc)}`\n\n"
                "请检查模型配置、依赖安装和运行环境。"
            )
        ).send()
