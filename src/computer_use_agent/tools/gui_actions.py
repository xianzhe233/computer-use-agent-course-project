from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

GuiButton = Literal["left", "right", "middle"]
CaretPosition = Literal["start", "idle", "end"]

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
    def hotkey(self, keys: tuple[str, ...]) -> None: ...
    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None: ...


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

    def hotkey(self, keys: tuple[str, ...]) -> None:
        self._pyautogui.hotkey(*keys)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._pyautogui.moveTo(x=x1, y=y1, duration=0.1)
        self._pyautogui.dragTo(x=x2, y=y2, duration=0.2, button="left")


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


def click(
    x: int,
    y: int,
    *,
    button: GuiButton = "left",
    clicks: int = 1,
    backend: GuiAutomationBackend | None = None,
) -> GuiActionResult:
    active_backend = backend or PyAutoGuiBackend()
    return _wrap_gui_action(
        "click",
        lambda: active_backend.click(x=x, y=y, button=button, clicks=clicks),
        {"x": x, "y": y, "button": button, "clicks": clicks},
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
    active_backend = backend or PyAutoGuiBackend()
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
    active_backend = backend or PyAutoGuiBackend()
    normalized = normalize_shortcut(shortcut)
    return _wrap_gui_action(
        "hotkey",
        lambda: active_backend.hotkey(normalized),
        {"shortcut": shortcut, "normalized_keys": list(normalized)},
    )


def drag(
    x1: int,
    y1: int,
    x2: int,
    y2: int,
    *,
    backend: GuiAutomationBackend | None = None,
) -> GuiActionResult:
    active_backend = backend or PyAutoGuiBackend()
    return _wrap_gui_action(
        "drag",
        lambda: active_backend.drag(x1=x1, y1=y1, x2=x2, y2=y2),
        {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
    )
