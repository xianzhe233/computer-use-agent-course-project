from __future__ import annotations

import base64
import io
import json
import math
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from PIL import Image
from langchain_core.prompts import ChatPromptTemplate, HumanMessagePromptTemplate, MessagesPlaceholder
from pydantic import BaseModel, ConfigDict, Field

LocationSource = Literal["vision", "fallback"]


@dataclass(slots=True)
class ElementLocationCandidate:
    point: tuple[int, int]
    confidence: float
    source: LocationSource
    reason: str
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class ElementLocationResult:
    tool_name: str
    query: str
    screenshot_id: str
    screenshot_path: str
    success: bool
    duration_ms: int
    timestamp: str
    point: tuple[int, int] | None = None
    confidence: float = 0.0
    source: LocationSource | None = None
    reason: str = ""
    candidates: list[ElementLocationCandidate] = field(default_factory=list)
    error: dict[str, str] | None = None
    suggested_next_steps: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)


class ElementLocatorBackend(Protocol):
    def locate(
        self,
        *,
        query: str,
        screenshot_path: Path,
        screenshot_id: str = "",
    ) -> list[ElementLocationCandidate]: ...


class NullElementLocatorBackend:
    def locate(
        self,
        *,
        query: str,
        screenshot_path: Path,
        screenshot_id: str = "",
    ) -> list[ElementLocationCandidate]:
        return []


class LocatorResponsePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    point: list[int] = Field(
        default_factory=list,
        description="Single point as [x, y] in rendered image coordinates. Return an empty list if no match exists.",
    )


UITARS_LOCATOR_SYSTEM_PROMPT = """
You are a UI element locator for Windows screenshots.
Use the provided screenshot image and user context to identify the requested UI element.
Return only one structured point in rendered image coordinates.
Do not output any explanation text.
If no good match exists, return an empty point list.
""".strip()

UITARS_LOCATOR_PROMPT_TEMPLATE = ChatPromptTemplate.from_messages(
    [
        ("system", UITARS_LOCATOR_SYSTEM_PROMPT),
        MessagesPlaceholder("input_messages"),
    ]
)

LOCATOR_MODEL_ALIASES: dict[str, str] = {
    "uitars": "bytedance/ui-tars-1.5-7b",
    "ui-tars": "bytedance/ui-tars-1.5-7b",
    "ui_tars": "bytedance/ui-tars-1.5-7b",
}

UITARS_IMAGE_FACTOR = 28
UITARS_MAX_PIXELS = 16384 * 28 * 28
UITARS_MAX_RATIO = 200


class UITarsElementLocator:
    def __init__(
        self,
        *,
        client: Any,
        max_pixels: int = UITARS_MAX_PIXELS,
        image_factor: int = UITARS_IMAGE_FACTOR,
    ) -> None:
        self.client = client
        self.max_pixels = max_pixels
        self.image_factor = image_factor

    @classmethod
    def from_config_file(
        cls,
        config_path: Path,
        *,
        role: str = "locator",
        timeout_s: int = 60,
        max_pixels: int = UITARS_MAX_PIXELS,
        image_factor: int = UITARS_IMAGE_FACTOR,
    ) -> UITarsElementLocator:
        from computer_use_agent.computer_agent import OpenAICompatibleChatClient

        _normalize_locator_model_alias(config_path=config_path, role=role)
        client = OpenAICompatibleChatClient.from_config_file(
            config_path=config_path,
            role=role,
            timeout_s=timeout_s,
        )
        return cls(client=client, max_pixels=max_pixels, image_factor=image_factor)

    def locate(
        self,
        *,
        query: str,
        screenshot_path: Path,
        screenshot_id: str = "",
    ) -> list[ElementLocationCandidate]:
        data_url, rendered_size, original_size = _prepare_locator_image(
            screenshot_path,
            max_pixels=self.max_pixels,
            image_factor=self.image_factor,
        )
        user_message = HumanMessagePromptTemplate.from_template(
            [
                {"type": "text", "text": _locator_user_text_template()},
                {"type": "image_url", "image_url": {"url": "{locator_image_url}", "detail": "high"}},
            ]
        ).format(
            query=query,
            screenshot_id=screenshot_id or "<none>",
            rendered_width=rendered_size[0],
            rendered_height=rendered_size[1],
            original_width=original_size[0],
            original_height=original_size[1],
            locator_image_url=data_url,
        )
        messages = UITARS_LOCATOR_PROMPT_TEMPLATE.invoke({"input_messages": [user_message]}).to_messages()
        response = self.client.invoke_structured(
            messages,
            schema=LocatorResponsePayload,
        )
        return _structured_locator_candidates(
            response,
            rendered_size=rendered_size,
            original_size=original_size,
        )


def create_default_element_locator_backend(
    *,
    model_config_path: Path | None = None,
    role: str = "locator",
    timeout_s: int = 60,
) -> ElementLocatorBackend:
    config_path = model_config_path or Path("config/models.local.json")
    return UITarsElementLocator.from_config_file(
        config_path=config_path,
        role=role,
        timeout_s=timeout_s,
    )


def locate_element(
    *,
    query: str,
    screenshot_path: Path,
    screenshot_id: str = "",
    backend: ElementLocatorBackend | None = None,
) -> ElementLocationResult:
    started_at = time.perf_counter()
    located_at = datetime.now(UTC).isoformat()
    normalized_query = query.strip()
    active_backend = backend or create_default_element_locator_backend()
    artifacts = [f"screenshot:{screenshot_id}"] if screenshot_id else []

    if not normalized_query:
        return ElementLocationResult(
            tool_name="locate_element",
            query=query,
            screenshot_id=screenshot_id,
            screenshot_path=str(screenshot_path),
            success=False,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            timestamp=located_at,
            reason="query must not be empty",
            error={"code": "INVALID_QUERY", "message": "locate_element requires a non-empty query"},
            suggested_next_steps=["provide a non-empty element description", "take_screenshot before retrying"],
            artifacts=artifacts,
            metadata={"backend": active_backend.__class__.__name__},
        )

    if not screenshot_path.exists():
        return ElementLocationResult(
            tool_name="locate_element",
            query=normalized_query,
            screenshot_id=screenshot_id,
            screenshot_path=str(screenshot_path),
            success=False,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            timestamp=located_at,
            reason="screenshot path does not exist",
            error={
                "code": "SCREENSHOT_NOT_FOUND",
                "message": f"Screenshot not found: {screenshot_path}",
            },
            suggested_next_steps=["take_screenshot", "retry locate_element with the new screenshot"],
            artifacts=artifacts,
            metadata={"backend": active_backend.__class__.__name__},
        )

    try:
        candidates = active_backend.locate(
            query=normalized_query,
            screenshot_path=screenshot_path,
            screenshot_id=screenshot_id,
        )
    except Exception as exc:
        return ElementLocationResult(
            tool_name="locate_element",
            query=normalized_query,
            screenshot_id=screenshot_id,
            screenshot_path=str(screenshot_path),
            success=False,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            timestamp=located_at,
            reason="locator backend failed",
            error={
                "code": "LOCATOR_BACKEND_FAILED",
                "message": f"{type(exc).__name__}: {exc}",
            },
            suggested_next_steps=["take_screenshot", "wait", "retry locate_element", "abort current GUI strategy"],
            artifacts=artifacts,
            metadata={"backend": active_backend.__class__.__name__},
        )

    candidates = sorted(candidates, key=lambda candidate: candidate.confidence, reverse=True)
    best_candidate = candidates[0] if candidates else None
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    if best_candidate is None:
        return ElementLocationResult(
            tool_name="locate_element",
            query=normalized_query,
            screenshot_id=screenshot_id,
            screenshot_path=str(screenshot_path),
            success=False,
            duration_ms=duration_ms,
            timestamp=located_at,
            reason="no matching element candidate found",
            error={
                "code": "ELEMENT_NOT_FOUND",
                "message": f"No element matched query: {normalized_query}",
            },
            suggested_next_steps=["take_screenshot", "scroll or reveal more UI", "retry locate_element"],
            artifacts=artifacts,
            metadata={"backend": active_backend.__class__.__name__},
        )

    return ElementLocationResult(
        tool_name="locate_element",
        query=normalized_query,
        screenshot_id=screenshot_id,
        screenshot_path=str(screenshot_path),
        success=True,
        duration_ms=duration_ms,
        timestamp=located_at,
        point=best_candidate.point,
        confidence=best_candidate.confidence,
        source=best_candidate.source,
        reason=best_candidate.reason,
        candidates=candidates,
        artifacts=artifacts,
        metadata={"backend": active_backend.__class__.__name__},
    )


def _normalize_locator_model_alias(*, config_path: Path, role: str) -> None:
    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return
    roles = raw_config.get("roles", {})
    role_config = roles.get(role)
    if not isinstance(role_config, dict):
        return
    model = str(role_config.get("model", "")).strip()
    normalized = LOCATOR_MODEL_ALIASES.get(model.lower())
    if normalized:
        role_config["model"] = normalized
        config_path.write_text(json.dumps(raw_config, ensure_ascii=False, indent=2), encoding="utf-8")


def _locator_user_text_template() -> str:
    return (
        "请在截图中定位用户描述的 Windows 界面元素。\n"
        "元素描述：{query}\n"
        "截图编号：{screenshot_id}\n"
        "你看到的图片尺寸（rendered image）是 {rendered_width}x{rendered_height}。\n"
        "原始截图尺寸（original image）是 {original_width}x{original_height}。\n"
        "要求：\n"
        "1. point 使用 rendered image 的整数像素坐标。\n"
        "2. 坐标格式必须是 [x, y]。\n"
        "3. 只返回一个 point 字段，格式必须是 [x, y]。\n"
        "4. 不要输出 Markdown、说明文字、置信度、reason 或其他额外字段。"
    )


def _prepare_locator_image(
    path: Path,
    *,
    max_pixels: int = UITARS_MAX_PIXELS,
    image_factor: int = UITARS_IMAGE_FACTOR,
) -> tuple[str, tuple[int, int], tuple[int, int]]:
    with Image.open(path) as image:
        original_width, original_height = image.size
        prepared = image.convert("RGB") if image.mode not in {"RGB", "L"} else image.copy()
        rendered_width, rendered_height = _locator_render_size(
            width=original_width,
            height=original_height,
            max_pixels=max_pixels,
            image_factor=image_factor,
        )
        if (rendered_width, rendered_height) != (original_width, original_height):
            prepared = prepared.resize((rendered_width, rendered_height))
        buffer = io.BytesIO()
        prepared.save(buffer, format="JPEG", quality=90, optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return (
        f"data:image/jpeg;base64,{encoded}",
        (rendered_width, rendered_height),
        (original_width, original_height),
    )


def _locator_render_size(
    *,
    width: int,
    height: int,
    max_pixels: int = UITARS_MAX_PIXELS,
    image_factor: int = UITARS_IMAGE_FACTOR,
    max_ratio: int = UITARS_MAX_RATIO,
) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise ValueError("image width and height must be positive")
    if max(width, height) / min(width, height) > max_ratio:
        raise ValueError(
            f"absolute aspect ratio must be smaller than {max_ratio}, got {max(width, height) / min(width, height)}"
        )
    if width * height <= max_pixels:
        return (width, height)

    scale = math.sqrt(max_pixels / (width * height))
    scaled_width = max(image_factor, _floor_by_factor(int(width * scale), image_factor))
    scaled_height = max(image_factor, _floor_by_factor(int(height * scale), image_factor))

    while scaled_width * scaled_height > max_pixels:
        if scaled_width >= scaled_height and scaled_width > image_factor:
            scaled_width -= image_factor
        elif scaled_height > image_factor:
            scaled_height -= image_factor
        else:
            break
    return (scaled_width, scaled_height)


def _floor_by_factor(number: int, factor: int) -> int:
    return max(factor, math.floor(number / factor) * factor)


def _structured_locator_candidates(
    payload: LocatorResponsePayload,
    *,
    rendered_size: tuple[int, int],
    original_size: tuple[int, int],
) -> list[ElementLocationCandidate]:
    point = _coerce_point(payload.point)
    if point is None:
        return []
    scaled_point = _scale_point(point, rendered_size=rendered_size, original_size=original_size)
    return [
        ElementLocationCandidate(
            point=scaled_point,
            confidence=1.0,
            source="vision",
            reason="locator point",
            metadata={
                "rendered_point": list(point),
                "rendered_size": {"width": rendered_size[0], "height": rendered_size[1]},
                "original_size": {"width": original_size[0], "height": original_size[1]},
            },
        )
    ]


def _coerce_point(value: object) -> tuple[int, int] | None:
    if not isinstance(value, list | tuple):
        return None
    if len(value) == 2:
        point_like = value
    elif len(value) == 4:
        point_like = value[:2]
    else:
        return None
    try:
        x_raw, y_raw = point_like
        if not isinstance(x_raw, int | float | str) or not isinstance(y_raw, int | float | str):
            return None
        x = int(float(x_raw))
        y = int(float(y_raw))
    except (TypeError, ValueError):
        return None
    return (x, y)


def _coerce_confidence(value: object, *, default: float) -> float:
    try:
        if not isinstance(value, int | float | str):
            raise TypeError
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = default
    return max(0.0, min(1.0, confidence))


def _scale_point(
    point: tuple[int, int],
    *,
    rendered_size: tuple[int, int],
    original_size: tuple[int, int],
) -> tuple[int, int]:
    rendered_width, rendered_height = rendered_size
    original_width, original_height = original_size
    if rendered_width <= 0 or rendered_height <= 0:
        return point

    scale_x = original_width / rendered_width
    scale_y = original_height / rendered_height
    scaled = (
        int(round(point[0] * scale_x)),
        int(round(point[1] * scale_y)),
    )
    return _clamp_point(scaled, width=original_width, height=original_height)


def _clamp_point(
    point: tuple[int, int],
    *,
    width: int,
    height: int,
) -> tuple[int, int]:
    x, y = point
    return (
        max(0, min(x, max(width - 1, 0))),
        max(0, min(y, max(height - 1, 0))),
    )


def _deduplicate_candidates(candidates: list[ElementLocationCandidate]) -> list[ElementLocationCandidate]:
    deduplicated: list[ElementLocationCandidate] = []
    seen: set[tuple[int, int]] = set()
    for candidate in candidates:
        if candidate.point in seen:
            continue
        deduplicated.append(candidate)
        seen.add(candidate.point)
    return deduplicated
