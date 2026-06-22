from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


class ScreenshotExecutionError(RuntimeError):
    pass


@dataclass(slots=True)
class ScreenshotResult:
    screenshot_id: str
    path: str
    width: int
    height: int
    success: bool
    duration_ms: int
    timestamp: str
    target: str = "screen"
    source_action_id: str = ""
    step_id: int = 0
    note: str = ""
    error: dict[str, str] | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class ScreenshotBackend(Protocol):
    def capture(self, path: Path, target: str = "screen") -> tuple[int, int]: ...


class ImageGrabBackend:
    def capture(self, path: Path, target: str = "screen") -> tuple[int, int]:
        if target != "screen":
            raise ScreenshotExecutionError(f"Unsupported screenshot target: {target}")

        from PIL import ImageGrab

        try:
            image = ImageGrab.grab(all_screens=True)
        except Exception:
            image = ImageGrab.grab()
        image.save(path, format="PNG")
        return image.size


def create_default_screenshot_backend() -> ScreenshotBackend:
    try:
        from .windows_use_desktop import WindowsUseDesktopBackend
    except Exception:
        return ImageGrabBackend()
    return WindowsUseDesktopBackend()


def take_screenshot(
    *,
    screenshot_id: str,
    path: Path,
    step_id: int,
    source_action_id: str = "",
    target: str = "screen",
    backend: ScreenshotBackend | None = None,
) -> ScreenshotResult:
    started_at = time.perf_counter()
    captured_at = datetime.now(UTC).isoformat()
    active_backend = backend or create_default_screenshot_backend()
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        width, height = active_backend.capture(path=path, target=target)
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        return ScreenshotResult(
            screenshot_id=screenshot_id,
            path=str(path),
            width=width,
            height=height,
            success=True,
            duration_ms=duration_ms,
            timestamp=captured_at,
            target=target,
            source_action_id=source_action_id,
            step_id=step_id,
            metadata={"backend": active_backend.__class__.__name__},
        )
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        return ScreenshotResult(
            screenshot_id=screenshot_id,
            path=str(path),
            width=0,
            height=0,
            success=False,
            duration_ms=duration_ms,
            timestamp=captured_at,
            target=target,
            source_action_id=source_action_id,
            step_id=step_id,
            note="Screenshot capture failed",
            error={
                "code": "SCREENSHOT_CAPTURE_FAILED",
                "message": f"{type(exc).__name__}: {exc}",
            },
            metadata={"backend": active_backend.__class__.__name__},
        )
