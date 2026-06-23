from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

GuiButton = Literal["left", "right", "middle"]
CaretPosition = Literal["start", "idle", "end"]
ScrollAxis = Literal["horizontal", "vertical"]
ScrollDirection = Literal["up", "down", "left", "right"]

KEY_ALIASES: dict[str, str] = {
    "backspace": "backspace",
    "capslock": "capslock",
    "scrolllock": "scrolllock",
    "windows": "win",
    "command": "win",
    "option": "alt",
}


@dataclass(slots=True)
class GuiActionResult:
    tool_name: str
    success: bool
    duration_ms: int
    result: dict[str, object] = field(default_factory=dict)
    error: dict[str, str] | None = None
    artifacts: list[str] = field(default_factory=list)
    note: str = ""


class GuiAutomationBackend(Protocol):
    def click(self, x: int, y: int, *, button: GuiButton = "left", clicks: int = 1) -> None: ...
    def type_text(
        self,
        text: str,
        *,
        x: int | None = None,
        y: int | None = None,
        clear: bool = False,
        caret_position: CaretPosition = "idle",
        press_enter: bool = False,
    ) -> None: ...
    def hotkey(self, shortcut: str) -> None: ...
    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None: ...
    def move(self, x: int, y: int) -> None: ...
    def scroll(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        axis: ScrollAxis = "vertical",
        direction: ScrollDirection = "down",
        amount: int = 1,
    ) -> None: ...
    def open_app(self, name: str) -> str: ...
    def switch_app(self, name: str) -> str: ...
    def focus_window(self, title: str) -> str: ...


class PyAutoGuiBackend:
    def __init__(self) -> None:
        import pyautogui

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        self._pyautogui = pyautogui

    def click(self, x: int, y: int, *, button: GuiButton = "left", clicks: int = 1) -> None:
        if clicks == 0:
            self._pyautogui.moveTo(x=x, y=y, duration=0.1)
            return
        if clicks == 2 and button == "left":
            self._pyautogui.doubleClick(x=x, y=y, button=button)
            return
        for _ in range(clicks):
            self._pyautogui.click(x=x, y=y, button=button)

    def type_text(
        self,
        text: str,
        *,
        x: int | None = None,
        y: int | None = None,
        clear: bool = False,
        caret_position: CaretPosition = "idle",
        press_enter: bool = False,
    ) -> None:
        if x is not None and y is not None:
            self._pyautogui.click(x=x, y=y, button="left")
        if caret_position == "start":
            self._pyautogui.press("home")
        elif caret_position == "end":
            self._pyautogui.press("end")
        if clear:
            self._pyautogui.hotkey("ctrl", "a")
            self._pyautogui.press("backspace")
        self._pyautogui.write(text, interval=0.01)
        if press_enter:
            self._pyautogui.press("enter")

    def hotkey(self, shortcut: str) -> None:
        self._pyautogui.hotkey(*normalize_shortcut(shortcut))

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._pyautogui.moveTo(x=x1, y=y1, duration=0.1)
        self._pyautogui.dragTo(x=x2, y=y2, duration=0.2, button="left")

    def move(self, x: int, y: int) -> None:
        self._pyautogui.moveTo(x=x, y=y, duration=0.1)

    def scroll(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        axis: ScrollAxis = "vertical",
        direction: ScrollDirection = "down",
        amount: int = 1,
    ) -> None:
        if x is not None and y is not None:
            self._pyautogui.moveTo(x=x, y=y, duration=0.1)
        if axis == "vertical":
            delta = amount if direction == "up" else -amount
            self._pyautogui.scroll(delta)
            return
        delta = -amount if direction == "left" else amount
        self._pyautogui.hscroll(delta)

    def open_app(self, name: str) -> str:
        raise RuntimeError(f"open_app requires WindowsUseDesktopBackend: {name}")

    def switch_app(self, name: str) -> str:
        raise RuntimeError(f"switch_app requires WindowsUseDesktopBackend: {name}")

    def focus_window(self, title: str) -> str:
        raise RuntimeError(f"focus_window requires WindowsUseDesktopBackend: {title}")


def normalize_shortcut(shortcut: str) -> tuple[str, ...]:
    normalized: list[str] = []
    for key in shortcut.split("+"):
        alias = KEY_ALIASES.get(key.strip().lower(), key.strip().lower())
        normalized.append(alias)
    return tuple(normalized)


def _wrap_gui_action(
    tool_name: str,
    func: Callable[[], None],
    result: dict[str, object],
) -> GuiActionResult:
    started_at = time.perf_counter()
    try:
        func()
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        return GuiActionResult(
            tool_name=tool_name,
            success=False,
            duration_ms=duration_ms,
            result={},
            error={
                "code": f"{tool_name.upper()}_FAILED",
                "message": f"{type(exc).__name__}: {exc}",
            },
            note=f"{tool_name} failed",
        )
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    return GuiActionResult(tool_name=tool_name, success=True, duration_ms=duration_ms, result=result)


def create_default_gui_backend() -> GuiAutomationBackend:
    try:
        from .windows_use_desktop import WindowsUseDesktopBackend
    except Exception:
        return PyAutoGuiBackend()
    return WindowsUseDesktopBackend()


def click(
    x: int,
    y: int,
    *,
    button: GuiButton = "left",
    clicks: int = 1,
    backend: GuiAutomationBackend | None = None,
) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()
    return _wrap_gui_action(
        "click",
        lambda: active_backend.click(x=x, y=y, button=button, clicks=clicks),
        {"x": x, "y": y, "button": button, "clicks": clicks},
    )


def double_click(
    x: int,
    y: int,
    *,
    backend: GuiAutomationBackend | None = None,
) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()
    return _wrap_gui_action(
        "double_click",
        lambda: active_backend.click(x=x, y=y, button="left", clicks=2),
        {"x": x, "y": y, "button": "left", "clicks": 2},
    )


def right_click(
    x: int,
    y: int,
    *,
    backend: GuiAutomationBackend | None = None,
) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()
    return _wrap_gui_action(
        "right_click",
        lambda: active_backend.click(x=x, y=y, button="right", clicks=1),
        {"x": x, "y": y, "button": "right", "clicks": 1},
    )


def move_mouse(
    x: int,
    y: int,
    *,
    backend: GuiAutomationBackend | None = None,
) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()
    return _wrap_gui_action(
        "move_mouse",
        lambda: active_backend.move(x=x, y=y),
        {"x": x, "y": y},
    )


def hover(
    x: int,
    y: int,
    *,
    duration_ms: int = 500,
    backend: GuiAutomationBackend | None = None,
) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()

    def _run() -> None:
        active_backend.move(x=x, y=y)
        if duration_ms > 0:
            time.sleep(duration_ms / 1000)

    return _wrap_gui_action(
        "hover",
        _run,
        {"x": x, "y": y, "duration_ms": duration_ms},
    )


def type_text(
    text: str,
    *,
    x: int | None = None,
    y: int | None = None,
    clear: bool = False,
    caret_position: CaretPosition = "idle",
    press_enter: bool = False,
    backend: GuiAutomationBackend | None = None,
) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()
    return _wrap_gui_action(
        "type_text",
        lambda: active_backend.type_text(
            text,
            x=x,
            y=y,
            clear=clear,
            caret_position=caret_position,
            press_enter=press_enter,
        ),
        {
            "typed_length": len(text),
            "x": x,
            "y": y,
            "clear": clear,
            "caret_position": caret_position,
            "press_enter": press_enter,
        },
    )


def hotkey(shortcut: str, *, backend: GuiAutomationBackend | None = None) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()
    normalized = normalize_shortcut(shortcut)
    return _wrap_gui_action(
        "hotkey",
        lambda: active_backend.hotkey(shortcut),
        {"shortcut": shortcut, "normalized_keys": list(normalized)},
    )


def scroll(
    *,
    direction: ScrollDirection = "down",
    amount: int = 1,
    x: int | None = None,
    y: int | None = None,
    axis: ScrollAxis | None = None,
    backend: GuiAutomationBackend | None = None,
) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()
    resolved_axis: ScrollAxis = axis or ("horizontal" if direction in {"left", "right"} else "vertical")
    return _wrap_gui_action(
        "scroll",
        lambda: active_backend.scroll(
            x=x,
            y=y,
            axis=resolved_axis,
            direction=direction,
            amount=amount,
        ),
        {
            "x": x,
            "y": y,
            "axis": resolved_axis,
            "direction": direction,
            "amount": amount,
        },
    )


def drag(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    backend: GuiAutomationBackend | None = None,
) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()
    return _wrap_gui_action(
        "drag",
        lambda: active_backend.drag(x1=x1, y1=y1, x2=x2, y2=y2),
        {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
    )


def open_app(name: str, *, backend: GuiAutomationBackend | None = None) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()
    result: dict[str, object] = {"name": name, "message": ""}

    def _run() -> None:
        result["message"] = active_backend.open_app(name)

    return _wrap_gui_action("open_app", _run, result)


def switch_app(name: str, *, backend: GuiAutomationBackend | None = None) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()
    result: dict[str, object] = {"name": name, "message": ""}

    def _run() -> None:
        result["message"] = active_backend.switch_app(name)

    return _wrap_gui_action("switch_app", _run, result)


def focus_window(title: str, *, backend: GuiAutomationBackend | None = None) -> GuiActionResult:
    active_backend = backend or create_default_gui_backend()
    result: dict[str, object] = {"title": title, "message": ""}

    def _run() -> None:
        result["message"] = active_backend.focus_window(title)

    return _wrap_gui_action("focus_window", _run, result)
