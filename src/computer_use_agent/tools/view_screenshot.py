from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(slots=True)
class ViewScreenshotResult:
    tool_name: str
    screenshot_ids: list[str]
    screenshots: list[dict[str, object]]
    success: bool
    duration_ms: int
    timestamp: str
    note: str = ""
    error: dict[str, str] | None = None
    artifacts: list[str] = field(default_factory=list)


def view_screenshot(
    screenshot_ids: list[str],
    screenshots_dir: Path,
) -> ViewScreenshotResult:
    """Validate screenshot IDs and return metadata for each existing screenshot.

    This tool only reads metadata (id / path / dimensions); it does NOT load
    pixel data.  Image loading for multimodal model context happens separately
    in the agent message builder.
    """
    started_at = time.perf_counter()
    viewed_at = datetime.now(UTC).isoformat()

    normalized = [sid.strip() for sid in screenshot_ids if sid.strip()]
    if not normalized:
        return ViewScreenshotResult(
            tool_name="view_screenshot",
            screenshot_ids=[],
            screenshots=[],
            success=False,
            duration_ms=int((time.perf_counter() - started_at) * 1000),
            timestamp=viewed_at,
            note="screenshot_ids must not be empty",
            error={
                "code": "INVALID_SCREENSHOT_ID",
                "message": "view_screenshot requires non-empty screenshot_ids",
            },
        )

    screenshots: list[dict[str, object]] = []
    for sid in normalized:
        path = screenshots_dir / f"{sid}.png"
        if not path.exists():
            return ViewScreenshotResult(
                tool_name="view_screenshot",
                screenshot_ids=normalized,
                screenshots=screenshots,
                success=False,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                timestamp=viewed_at,
                note="screenshot not found",
                error={
                    "code": "SCREENSHOT_NOT_FOUND",
                    "message": f"Screenshot not found: {path}",
                },
            )

        width, height = _read_image_size(path)
        screenshots.append(
            {
                "screenshot_id": sid,
                "path": str(path),
                "width": width,
                "height": height,
            }
        )

    artifact_refs = [f"screenshot:{sid}" for sid in normalized]
    return ViewScreenshotResult(
        tool_name="view_screenshot",
        screenshot_ids=normalized,
        screenshots=screenshots,
        success=True,
        duration_ms=int((time.perf_counter() - started_at) * 1000),
        timestamp=viewed_at,
        note="selected screenshots for visual context",
        artifacts=artifact_refs,
    )


def _read_image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception:
        return 0, 0
