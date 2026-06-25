from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any

import chainlit as cl

WORKSPACE = Path(".").resolve()
RUNS_ROOT = Path("runs").resolve()
MODEL_CONFIG_CANDIDATES = [
    Path("config/models.local.json").resolve(),
    Path("config/models.json").resolve(),
    Path("config/models.example.json").resolve(),
]

MAX_PAYLOAD_CHARS = 6000
MAX_LOG_CHARS = 8000
SENSITIVE_REASONING_KEYS = {"thought", "chain_of_thought", "cot", "raw_reasoning", "raw_response"}
SUMMARY_REASONING_KEYS = ("thought_summary", "reasoning_summary", "summary", "rationale")
MAIN_AGENT_AUTHOR = "mainAgent"
EXAMINER_AUTHOR = "examiner"


def author_for_event(event: dict[str, Any]) -> str:
    actor = str(event.get("actor", ""))
    event_type = str(event.get("event_type", ""))
    if actor.lower() == EXAMINER_AUTHOR or EXAMINER_AUTHOR in event_type.lower():
        return EXAMINER_AUTHOR
    return MAIN_AGENT_AUTHOR


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
        return ""
    if not path.exists():
        return f"文件缺失：{path}"
    try:
        return truncate_text(path.read_text(encoding="utf-8", errors="replace"), limit)
    except Exception as exc:
        print(f"Failed to read artifact text: {path}", flush=True)
        traceback.print_exc()
        return f"无法读取文件：{path} ({type(exc).__name__}: {exc})"


def normalize_inline_text(value: Any, limit: int = 800) -> str:
    text = truncate_text(value, limit)
    return " ".join(text.replace("\r", "\n").split()) or "无"


def bullet(label: str, value: Any, *, limit: int = 800) -> str:
    return f"- {label}: {normalize_inline_text(value, limit)}"


def format_command_log(command: dict[str, Any], run_dir: Path) -> str:
    stdout_path = resolve_artifact_path(command.get("stdout_path"), run_dir, "command_logs")
    stderr_path = resolve_artifact_path(command.get("stderr_path"), run_dir, "command_logs")
    stdout = read_text_artifact(stdout_path, 1000)
    stderr = read_text_artifact(stderr_path, 1000)
    parts = [
        "#### 命令结果",
        bullet("命令", command.get("command", "N/A"), limit=1200),
        bullet("退出码", command.get("exit_code", "N/A"), limit=80),
        bullet("耗时", f"{command.get('duration_ms', 'N/A')} ms", limit=80),
    ]
    if stdout:
        parts.append(bullet("输出", stdout, limit=1000))
    if stderr:
        parts.append(bullet("错误输出", stderr, limit=1000))
    return "\n".join(parts)


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


def format_tool_args(args: Any) -> list[str]:
    if not isinstance(args, dict) or not args:
        return []
    labels = {
        "command": "命令",
        "name": "名称",
        "text": "文本",
        "target_query": "目标",
        "screenshot_ids": "截图",
        "keys": "按键",
        "seconds": "等待",
        "direction": "方向",
        "amount": "距离",
        "x": "X",
        "y": "Y",
    }
    lines: list[str] = []
    for key, value in args.items():
        if key in {"thought_summary", "expected_observation"}:
            continue
        label = labels.get(str(key), str(key))
        limit = 1200 if key in {"command", "text"} else 500
        lines.append(bullet(label, value, limit=limit))
    return lines


def format_tool_result(result: dict[str, Any]) -> list[str]:
    lines = [bullet("结果", "成功" if result.get("success") else "失败", limit=80)]
    tool_name = result.get("tool_name")
    if tool_name:
        lines.append(bullet("工具", tool_name, limit=120))
    detail = result.get("result") if isinstance(result.get("result"), dict) else {}
    if isinstance(detail, dict) and detail.get("mode") == "discovery":
        lines.append(bullet("模式", "候选发现", limit=120))
        lines.append(bullet("候选类型", detail.get("candidate_type", "N/A"), limit=120))
        if detail.get("candidates_text"):
            lines.append(bullet("候选列表", detail.get("candidates_text"), limit=2000))
        if detail.get("next_step"):
            lines.append(bullet("下一步", detail.get("next_step"), limit=1000))
    elif isinstance(detail, dict) and detail.get("message"):
        lines.append(bullet("说明", detail.get("message"), limit=1000))
    if result.get("command"):
        lines.append(bullet("命令", result.get("command"), limit=1200))
    if "exit_code" in result:
        lines.append(bullet("退出码", result.get("exit_code"), limit=80))
    if "duration_ms" in result:
        lines.append(bullet("耗时", f"{result.get('duration_ms')} ms", limit=80))
    if result.get("note"):
        lines.append(bullet("说明", result.get("note"), limit=800))
    if result.get("stdout"):
        lines.append(bullet("输出", result.get("stdout"), limit=1000))
    if result.get("stderr"):
        lines.append(bullet("错误输出", result.get("stderr"), limit=1000))
    if result.get("path"):
        lines.append(bullet("文件", result.get("path"), limit=1000))
    if result.get("error"):
        lines.append(bullet("错误", result.get("error"), limit=1000))
    return lines


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
            f"### Step {event.get('step_id', 'N/A')} · 验收检查",
            "",
            bullet("结论", decision, limit=120),
            bullet("状态", event.get("status", "N/A"), limit=120),
            bullet("原因", reason, limit=1200),
            bullet("缺少证据", missing, limit=1200),
            bullet("下一步建议", next_steps, limit=1200),
            bullet("已确认", findings, limit=1200),
            bullet("仍有疑问", questions, limit=1200),
        ]
    )


def should_render_event(event: dict[str, Any]) -> bool:
    event_type = str(event.get("event_type", ""))
    return event_type not in {"tool_validation", "state_update"}


def format_event(event: dict[str, Any], commands: dict[str, dict[str, Any]], run_dir: Path) -> str:
    if event.get("_warning"):
        return f"### Trace Warning\n\n{event['_warning']}"
    if not should_render_event(event):
        return ""

    actor = str(event.get("actor", "unknown"))
    event_type = str(event.get("event_type", "event"))
    payload = event.get("payload", {})
    payload = payload if isinstance(payload, dict) else {}

    if actor.lower() == "examiner" or "examiner" in event_type.lower():
        return format_examiner_event(event)

    if event_type == "run_initialized":
        return "\n".join(
            [
                "### 运行开始",
                "",
                bullet("任务", payload.get("task", "N/A"), limit=1200),
                bullet("工作目录", payload.get("workspace", "N/A"), limit=1000),
                bullet("模式", payload.get("mode", "N/A"), limit=120),
            ]
        )

    if event_type == "initial_observation":
        raw_result = payload.get("result")
        result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
        return "\n".join(
            [
                f"### Step {event.get('step_id', 'N/A')} · 初始观察",
                "",
                bullet("说明", payload.get("description", "初始截图"), limit=500),
                bullet("截图", result.get("screenshot_id", "N/A"), limit=120),
                bullet("分辨率", f"{result.get('width', 'N/A')}x{result.get('height', 'N/A')}", limit=120),
            ]
        )

    if event_type == "agent_decision":
        tool_name = payload.get("tool_name") or payload.get("kind") or "N/A"
        parts = [
            f"### Step {event.get('step_id', 'N/A')} · Agent 计划",
            "",
            bullet("意图", extract_thought_summary(payload) or "未提供", limit=1200),
            bullet("工具", tool_name, limit=120),
        ]
        parts.extend(format_tool_args(payload.get("tool_args")))
        expected = payload.get("expected_observation")
        if expected:
            parts.append(bullet("预期观察", expected, limit=1000))
        return "\n".join(parts)

    if event_type == "tool_execution":
        raw_result = payload.get("result")
        result = raw_result if isinstance(raw_result, dict) else {}
        tool_name = result.get("tool_name") or result.get("action_type") or "工具"
        parts = [f"### Step {event.get('step_id', 'N/A')} · {tool_name} 执行结果", ""]
        parts.extend(format_tool_result(result))
        if not result:
            for command in commands_for_event(event, commands):
                parts.extend(["", format_command_log(command, run_dir)])
        return "\n".join(parts)

    if event_type == "finish_request":
        return "\n".join(
            [
                f"### Step {event.get('step_id', 'N/A')} · 请求验收",
                "",
                bullet("完成说明", payload.get("completion_claim", "N/A"), limit=1200),
                bullet("支持证据", payload.get("supporting_evidence", []), limit=1200),
                bullet("不确定点", payload.get("remaining_uncertainty", "无"), limit=1200),
            ]
        )

    if event_type == "termination":
        return "\n".join(
            [
                "### 运行终止",
                "",
                bullet("状态", payload.get("status", event.get("status", "N/A")), limit=120),
                bullet("原因", payload.get("reason", "N/A"), limit=1200),
            ]
        )

    return "\n".join(
        [
            f"### Step {event.get('step_id', 'N/A')} · {event_type}",
            "",
            bullet("角色", actor, limit=120),
            bullet("状态", event.get("status", "N/A"), limit=120),
        ]
    )


def iter_run_directories(runs_root: Path) -> list[Path]:
    if not runs_root.exists():
        return []
    try:
        directories = [path for path in runs_root.iterdir() if path.is_dir() and path.name.startswith("run_")]
    except OSError:
        return []
    return sorted(directories, key=lambda path: path.name)



def discover_run_id_from_files(runs_root: Path, known_run_ids: set[str], started_at: float) -> str | None:
    candidates: list[tuple[float, str]] = []
    for path in iter_run_directories(runs_root):
        if path.name in known_run_ids:
            continue
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            continue
        if modified_at + 1 < started_at:
            continue
        candidates.append((modified_at, path.name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]



def event_render_key(event: dict[str, Any]) -> str:
    event_id = event.get("event_id")
    if event_id:
        return str(event_id)
    return json.dumps(event, ensure_ascii=False, sort_keys=True)



def format_live_status(
    task: str,
    run_id: str,
    run_dir: Path,
    artifacts: dict[str, Any],
    *,
    is_done: bool,
) -> str:
    summary = artifacts["summary"]
    trace = artifacts["trace"]
    screenshots = artifacts["screenshots"]
    commands = artifacts["commands"]

    latest_event: dict[str, Any] | None = None
    current_step = 0
    examiner_reviews = 0
    for item in trace:
        if not isinstance(item, dict) or item.get("_warning"):
            continue
        latest_event = item
        if str(item.get("actor", "")).lower() == "examiner" or "examiner" in str(item.get("event_type", "")).lower():
            examiner_reviews += 1
        try:
            current_step = max(current_step, int(item.get("step_id", 0) or 0))
        except (TypeError, ValueError):
            continue

    final_status = None
    final_reason = None
    if isinstance(summary, dict) and summary and not summary.get("_error"):
        final_status = summary.get("final_status")
        final_reason = summary.get("final_reason")

    latest_actor = latest_event.get("actor", "N/A") if latest_event else "N/A"
    latest_event_type = latest_event.get("event_type", "N/A") if latest_event else "N/A"
    latest_phase = latest_event.get("phase", "N/A") if latest_event else "N/A"
    latest_time = latest_event.get("timestamp", "N/A") if latest_event else "N/A"
    live_status = str(final_status or ("completed" if is_done else "running"))

    parts = [
        "## Run Status",
        "",
        f"- Task: {truncate_text(task, 600)}",
        f"- Run ID: `{run_id}`",
        f"- Run dir: `{run_dir}`",
        f"- Status: `{live_status}`",
        f"- Current step: `{current_step}`",
        f"- Trace events: `{len(trace)}`",
        f"- Commands: `{len(commands)}`",
        f"- Screenshots: `{len(screenshots)}`",
        f"- Examiner reviews: `{examiner_reviews}`",
        f"- Latest event: `{latest_actor}` · `{latest_event_type}`",
        f"- Latest phase: `{latest_phase}`",
        f"- Latest update: `{latest_time}`",
    ]
    if final_reason:
        parts.append(f"- Summary: {final_reason}")
    elif is_done:
        parts.append("- Summary: 运行已结束，等待最终汇总落盘。")
    return "\n".join(parts)


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
        if not content:
            continue
        images, warnings = images_for_event(event, run_dir, screenshots)
        if warnings:
            content += "\n\n#### Artifact Warnings\n\n" + "\n".join(f"- {item}" for item in warnings)
        await cl.Message(content=content, elements=images, author=author_for_event(event)).send()

    final_reason = summary.get("final_reason") if isinstance(summary, dict) else "N/A"
    final_status = summary.get("final_status") if isinstance(summary, dict) else "N/A"
    await cl.Message(
        content=f"## Final Result\n\n- Status: `{final_status or 'N/A'}`\n- Summary: {final_reason or 'N/A'}",
    ).send()


async def stream_run_updates(
    task: str,
    run_future: asyncio.Future[Any],
    status_message: cl.Message,
    *,
    known_run_ids: set[str],
    started_at: float,
) -> str | None:
    run_id: str | None = None
    overview_message: cl.Message | None = None
    final_message: cl.Message | None = None
    last_overview_content = ""
    sent_event_ids: set[str] = set()

    while True:
        if run_id is None:
            run_id = discover_run_id_from_files(RUNS_ROOT, known_run_ids, started_at)
            if run_id is not None:
                status_message.content = (
                    "任务运行中，正在根据运行文件实时渲染。\n\n"
                    f"- Run ID: `{run_id}`"
                )
                await status_message.update()

        if run_id is not None:
            run_dir = RUNS_ROOT / run_id
            artifacts = load_run_artifacts(run_dir)
            overview_content = format_live_status(
                task,
                run_id,
                run_dir,
                artifacts,
                is_done=run_future.done(),
            )
            if overview_message is None:
                overview_message = cl.Message(content=overview_content)
                await overview_message.send()
                last_overview_content = overview_content
            elif overview_content != last_overview_content:
                overview_message.content = overview_content
                await overview_message.update()
                last_overview_content = overview_content

            for event in artifacts["trace"]:
                event_key = event_render_key(event)
                if event_key in sent_event_ids:
                    continue
                content = format_event(event, artifacts["commands"], run_dir)
                if not content:
                    sent_event_ids.add(event_key)
                    continue
                images, warnings = images_for_event(event, run_dir, artifacts["screenshots"])
                if warnings:
                    content += "\n\n#### Artifact Warnings\n\n" + "\n".join(f"- {item}" for item in warnings)
                await cl.Message(content=content, elements=images, author=author_for_event(event)).send()
                sent_event_ids.add(event_key)

            if run_future.done():
                if cl.user_session.get("agent_stop_requested"):
                    status_message.content = f"任务已停止，后台 agent 进程已终止。Run ID: `{run_id}`"
                    await status_message.update()
                    return run_id
                summary = artifacts["summary"]
                if overview_message is not None:
                    final_overview = format_summary(run_id, run_dir, summary)
                    if final_overview != last_overview_content:
                        overview_message.content = final_overview
                        await overview_message.update()
                final_reason = summary.get("final_reason") if isinstance(summary, dict) else "N/A"
                final_status = summary.get("final_status") if isinstance(summary, dict) else "N/A"
                final_content = (
                    "## Final Result\n\n"
                    f"- Status: `{final_status or 'N/A'}`\n"
                    f"- Summary: {final_reason or 'N/A'}"
                )
                if final_message is None:
                    final_message = cl.Message(content=final_content)
                    await final_message.send()
                else:
                    final_message.content = final_content
                    await final_message.update()
                status_message.content = f"Agent 运行结束。Run ID: `{run_id}`"
                await status_message.update()
                return run_id

        if run_future.done():
            return None
        await asyncio.sleep(1)


def build_agent_command(task: str) -> list[str]:
    model_config = resolve_model_config()
    if model_config is None:
        tried = ", ".join(str(path) for path in MODEL_CONFIG_CANDIDATES)
        raise FileNotFoundError(f"找不到模型配置文件，已尝试：{tried}")
    return [
        sys.executable,
        "-m",
        "computer_use_agent.cli",
        task,
        "--mode",
        "autonomous",
        "--workspace",
        str(WORKSPACE),
        "--runs-root",
        str(RUNS_ROOT),
        "--max-steps",
        "50",
        "--step-timeout",
        "180",
        "--model-config",
        str(model_config),
        "--model-role",
        "mainAgent",
        "--quiet",
    ]


async def start_agent_process(task: str) -> asyncio.subprocess.Process:
    creationflags = 0
    preexec_fn = None
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        preexec_fn = getattr(os, "setsid", None)
    command = build_agent_command(task)
    return await asyncio.create_subprocess_exec(
        *command,
        cwd=str(WORKSPACE),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=creationflags,
        preexec_fn=preexec_fn,
    )


def current_agent_process() -> asyncio.subprocess.Process | None:
    process = cl.user_session.get("agent_process")
    return process if isinstance(process, asyncio.subprocess.Process) else None


async def terminate_process_tree(process: asyncio.subprocess.Process | None) -> None:
    if process is None or process.returncode is not None:
        return
    pid = process.pid
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True, text=True)
    else:
        try:
            killpg = getattr(os, "killpg")
            getpgid = getattr(os, "getpgid")
            killpg(getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except asyncio.TimeoutError:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False, capture_output=True, text=True)
        else:
            killpg = getattr(os, "killpg")
            getpgid = getattr(os, "getpgid")
            sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)
            killpg(getpgid(pid), sigkill)
        await process.wait()


async def wait_for_agent_process(process: asyncio.subprocess.Process) -> dict[str, Any]:
    stdout_bytes, stderr_bytes = await process.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
    stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
    return {"returncode": process.returncode, "stdout": stdout, "stderr": stderr}


@cl.on_chat_start
async def on_chat_start() -> None:
    await cl.Message(
        content=(
            "输入一个电脑使用任务，系统会启动 agent 执行，"
            "并在运行时基于 `runs/` 目录实时解析展示 summary、trace、命令日志、examiner 检查和截图。\n\n"
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

    progress_message = cl.Message(content=f"任务已提交：\n\n> {task}\n\n正在等待运行文件创建...")
    await progress_message.send()

    process: asyncio.subprocess.Process | None = None
    try:
        cl.user_session.set("agent_stop_requested", False)
        known_run_ids = {path.name for path in iter_run_directories(RUNS_ROOT)}
        started_at = time.time()
        process = await start_agent_process(task)
        cl.user_session.set("agent_process", process)
        process_future: asyncio.Future[Any] = asyncio.ensure_future(wait_for_agent_process(process))
        streamed_run_id = await stream_run_updates(
            task,
            process_future,
            progress_message,
            known_run_ids=known_run_ids,
            started_at=started_at,
        )
        result = await process_future
        stop_requested = bool(cl.user_session.get("agent_stop_requested"))
        if stop_requested:
            progress_message.content = "任务已停止，后台 agent 进程已终止。"
            await progress_message.update()
        elif result["returncode"] not in (0, None):
            stderr = truncate_text(result["stderr"], 2000)
            stdout = truncate_text(result["stdout"], 2000)
            await cl.Message(
                content=(
                    "Agent 进程已结束但返回失败状态。\n\n"
                    f"- Exit code: `{result['returncode']}`\n"
                    f"- Stdout: `{stdout or 'N/A'}`\n"
                    f"- Stderr: `{stderr or 'N/A'}`"
                ),
            ).send()
        if streamed_run_id is None:
            await cl.Message(
                content="Agent 运行结束，但未能发现新的 run 目录，请检查 CLI 输出或运行日志。",
            ).send()
    except asyncio.CancelledError:
        cl.user_session.set("agent_stop_requested", True)
        await terminate_process_tree(process or current_agent_process())
        cl.user_session.set("agent_process", None)
        progress_message.content = "任务已停止，后台 agent 进程已终止。"
        await progress_message.update()
        raise
    except Exception as exc:
        await terminate_process_tree(process or current_agent_process())
        traceback.print_exc()
        await cl.Message(
            content=(
                "Agent 运行失败。\n\n"
                f"- 错误类型：`{type(exc).__name__}`\n"
                f"- 错误信息：`{str(exc)}`\n\n"
                "请检查模型配置、依赖安装和运行环境。"
            ),
        ).send()
    finally:
        cl.user_session.set("agent_process", None)


@cl.on_stop
async def on_stop() -> None:
    cl.user_session.set("agent_stop_requested", True)
    process = current_agent_process()
    await terminate_process_tree(process)
    cl.user_session.set("agent_process", None)
    await cl.Message(content="已收到停止请求，后台 agent 进程已终止。").send()
