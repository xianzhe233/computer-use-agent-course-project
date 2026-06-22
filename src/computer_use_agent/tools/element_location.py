from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

LocationSource = Literal["vision", "uia", "hybrid", "fallback"]


@dataclass(slots=True)
class ElementLocationCandidate:
    bbox: tuple[int, int, int, int]
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
    bbox: tuple[int, int, int, int] | None = None
    confidence: float = 0.0
    source: LocationSource | None = None
    reason: str = ""
    candidates: list[ElementLocationCandidate] = field(default_factory=list)
    error: dict[str, str] | None = None
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


class WindowsUIAElementLocator:
    def locate(
        self,
        *,
        query: str,
        screenshot_path: Path,
        screenshot_id: str = "",
    ) -> list[ElementLocationCandidate]:
        del screenshot_path, screenshot_id
        from computer_use_agent._vendor.windows_use import uia

        foreground = uia.GetForegroundControl()
        normalized_query = query.strip()
        candidates: list[ElementLocationCandidate] = []

        exact_control = foreground.Control(Name=normalized_query)
        if exact_control.Exists(maxSearchSeconds=0.5):
            candidates.append(_control_to_candidate(exact_control, normalized_query, 0.97, "exact Name match"))

        regex = re.escape(normalized_query)
        regex_control = foreground.Control(RegexName=f"(?i).*{regex}.*")
        if regex_control.Exists(maxSearchSeconds=0.5):
            candidates.append(
                _control_to_candidate(regex_control, normalized_query, 0.9, "regex Name match in foreground window")
            )

        subname_control = foreground.Control(SubName=normalized_query)
        if subname_control.Exists(maxSearchSeconds=0.5):
            candidates.append(
                _control_to_candidate(subname_control, normalized_query, 0.84, "substring Name match in foreground window")
            )

        return _deduplicate_candidates(candidates)


def create_default_element_locator_backend() -> ElementLocatorBackend:
    try:
        return WindowsUIAElementLocator()
    except Exception:
        return NullElementLocatorBackend()


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
        bbox=best_candidate.bbox,
        confidence=best_candidate.confidence,
        source=best_candidate.source,
        reason=best_candidate.reason,
        candidates=candidates,
        artifacts=artifacts,
        metadata={"backend": active_backend.__class__.__name__},
    )


def bbox_center(bbox: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) // 2, (y1 + y2) // 2)


def _control_to_candidate(control: object, query: str, confidence: float, reason: str) -> ElementLocationCandidate:
    rect = getattr(control, "BoundingRectangle")
    metadata = {
        "query": query,
        "control_name": getattr(control, "Name", ""),
        "automation_id": getattr(control, "AutomationId", ""),
        "class_name": getattr(control, "ClassName", ""),
    }
    return ElementLocationCandidate(
        bbox=(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)),
        confidence=confidence,
        source="uia",
        reason=reason,
        metadata=metadata,
    )


def _deduplicate_candidates(candidates: list[ElementLocationCandidate]) -> list[ElementLocationCandidate]:
    deduplicated: list[ElementLocationCandidate] = []
    seen: set[tuple[int, int, int, int]] = set()
    for candidate in candidates:
        if candidate.bbox in seen:
            continue
        deduplicated.append(candidate)
        seen.add(candidate.bbox)
    return deduplicated
