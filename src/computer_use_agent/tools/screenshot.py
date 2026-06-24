from __future__ import annotations

import ctypes
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageDraw


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

        image = capture_screen_image()
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


def capture_screen_image() -> Image.Image:
    from PIL import ImageGrab

    use_all_screens = _can_read_virtual_screen_metrics()
    try:
        image = ImageGrab.grab(all_screens=use_all_screens)
    except Exception:
        image = ImageGrab.grab()
        use_all_screens = False
    return overlay_cursor_marker(image, capture_origin=_capture_origin(all_screens=use_all_screens))


def overlay_cursor_marker(image: Image.Image, *, capture_origin: tuple[int, int] = (0, 0)) -> Image.Image:
    cursor_position = _get_cursor_position()
    if cursor_position is None:
        return image

    marker_x = cursor_position[0] - capture_origin[0]
    marker_y = cursor_position[1] - capture_origin[1]
    if marker_x >= image.width or marker_y >= image.height:
        return image
    if marker_x < -12 or marker_y < -18:
        return image

    annotated = image.convert("RGBA") if image.mode != "RGBA" else image.copy()
    overlay = Image.new("RGBA", annotated.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    shadow_points = _cursor_marker_points(marker_x + 1, marker_y + 1)
    draw.polygon(shadow_points, fill=(0, 0, 0, 96))

    pointer_points = _cursor_marker_points(marker_x, marker_y)
    draw.polygon(pointer_points, fill=(250, 250, 250, 245), outline=(0, 0, 0, 255))
    draw.line(
        [(marker_x + 2, marker_y + 11), (marker_x + 5, marker_y + 16)],
        fill=(0, 0, 0, 255),
        width=1,
    )

    annotated = Image.alpha_composite(annotated, overlay)
    return annotated if image.mode == "RGBA" else annotated.convert(image.mode)


def _cursor_marker_points(x: int, y: int) -> list[tuple[int, int]]:
    return [
        (x, y),
        (x, y + 14),
        (x + 3, y + 11),
        (x + 5, y + 17),
        (x + 7, y + 16),
        (x + 5, y + 10),
        (x + 10, y + 10),
    ]


def _capture_origin(*, all_screens: bool) -> tuple[int, int]:
    if not all_screens:
        return (0, 0)
    user32 = _user32()
    if user32 is None:
        return (0, 0)
    return (user32.GetSystemMetrics(76), user32.GetSystemMetrics(77))


def _get_cursor_position() -> tuple[int, int] | None:
    user32 = _user32()
    if user32 is None:
        return None

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = POINT()
    if not bool(user32.GetCursorPos(ctypes.byref(point))):
        return None
    return (int(point.x), int(point.y))


def _can_read_virtual_screen_metrics() -> bool:
    return _user32() is not None


def _user32() -> ctypes.WinDLL | None:
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    return getattr(windll, "user32", None)
