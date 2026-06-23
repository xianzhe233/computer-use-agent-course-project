from computer_use_agent.tools.gui_actions import (
    click,
    double_click,
    drag,
    focus_window,
    hotkey,
    hover,
    move_mouse,
    normalize_shortcut,
    open_app,
    right_click,
    scroll,
    switch_app,
    type_text,
)


class FakeGuiBackend:
    def __init__(self, *, fail_tool: str | None = None) -> None:
        self.fail_tool = fail_tool
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        self.calls.append(("click", (x, y), {"button": button, "clicks": clicks}))
        if self.fail_tool == "click":
            raise RuntimeError("click failed")

    def type_text(
        self,
        text: str,
        *,
        x: int | None = None,
        y: int | None = None,
        clear: bool = False,
        caret_position: str = "idle",
        press_enter: bool = False,
    ) -> None:
        self.calls.append(
            (
                "type_text",
                (text,),
                {
                    "x": x,
                    "y": y,
                    "clear": clear,
                    "caret_position": caret_position,
                    "press_enter": press_enter,
                },
            )
        )
        if self.fail_tool == "type_text":
            raise RuntimeError("type failed")

    def hotkey(self, shortcut: str) -> None:
        self.calls.append(("hotkey", (shortcut,), {}))
        if self.fail_tool == "hotkey":
            raise RuntimeError("hotkey failed")

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.calls.append(("drag", (x1, y1, x2, y2), {}))
        if self.fail_tool == "drag":
            raise RuntimeError("drag failed")

    def move(self, x: int, y: int) -> None:
        self.calls.append(("move", (x, y), {}))
        if self.fail_tool == "move":
            raise RuntimeError("move failed")

    def scroll(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        axis: str = "vertical",
        direction: str = "down",
        amount: int = 1,
    ) -> None:
        self.calls.append(
            (
                "scroll",
                (),
                {"x": x, "y": y, "axis": axis, "direction": direction, "amount": amount},
            )
        )
        if self.fail_tool == "scroll":
            raise RuntimeError("scroll failed")

    def open_app(self, name: str) -> str:
        self.calls.append(("open_app", (name,), {}))
        if self.fail_tool == "open_app":
            raise RuntimeError("open_app failed")
        return f"opened {name}"

    def switch_app(self, name: str) -> str:
        self.calls.append(("switch_app", (name,), {}))
        if self.fail_tool == "switch_app":
            raise RuntimeError("switch_app failed")
        return f"switched {name}"

    def focus_window(self, title: str) -> str:
        self.calls.append(("focus_window", (title,), {}))
        if self.fail_tool == "focus_window":
            raise RuntimeError("focus_window failed")
        return f"focused {title}"


def test_normalize_shortcut_reuses_windows_use_aliases() -> None:
    assert normalize_shortcut("Ctrl+Shift+S") == ("ctrl", "shift", "s")
    assert normalize_shortcut("Windows+R") == ("win", "r")
    assert normalize_shortcut("Option+F4") == ("alt", "f4")


def test_click_returns_structured_result() -> None:
    backend = FakeGuiBackend()

    result = click(120, 240, button="right", clicks=1, backend=backend)

    assert result.success is True
    assert result.tool_name == "click"
    assert result.result == {"x": 120, "y": 240, "button": "right", "clicks": 1}
    assert backend.calls == [("click", (120, 240), {"button": "right", "clicks": 1})]


def test_double_click_right_click_move_hover_and_scroll_return_structured_result() -> None:
    backend = FakeGuiBackend()

    double_result = double_click(12, 34, backend=backend)
    right_result = right_click(56, 78, backend=backend)
    move_result = move_mouse(90, 91, backend=backend)
    hover_result = hover(22, 33, duration_ms=0, backend=backend)
    scroll_result = scroll(direction="right", amount=3, x=10, y=20, backend=backend)

    assert double_result.success is True
    assert double_result.result == {"x": 12, "y": 34, "button": "left", "clicks": 2}
    assert right_result.success is True
    assert right_result.result == {"x": 56, "y": 78, "button": "right", "clicks": 1}
    assert move_result.success is True
    assert move_result.result == {"x": 90, "y": 91}
    assert hover_result.success is True
    assert hover_result.result == {"x": 22, "y": 33, "duration_ms": 0}
    assert scroll_result.success is True
    assert scroll_result.result == {
        "x": 10,
        "y": 20,
        "axis": "horizontal",
        "direction": "right",
        "amount": 3,
    }


def test_type_text_returns_structured_result() -> None:
    backend = FakeGuiBackend()

    result = type_text(
        "hello",
        x=20,
        y=30,
        clear=True,
        caret_position="end",
        press_enter=True,
        backend=backend,
    )

    assert result.success is True
    assert result.result["typed_length"] == 5
    assert backend.calls == [
        (
            "type_text",
            ("hello",),
            {
                "x": 20,
                "y": 30,
                "clear": True,
                "caret_position": "end",
                "press_enter": True,
            },
        )
    ]


def test_hotkey_drag_and_window_tools_return_structured_result() -> None:
    backend = FakeGuiBackend()

    hotkey_result = hotkey("Ctrl+S", backend=backend)
    drag_result = drag(1, 2, 30, 40, backend=backend)
    open_result = open_app("notepad", backend=backend)
    switch_result = switch_app("notepad", backend=backend)
    focus_result = focus_window("Untitled - Notepad", backend=backend)

    assert hotkey_result.success is True
    assert hotkey_result.result == {"shortcut": "Ctrl+S", "normalized_keys": ["ctrl", "s"]}
    assert drag_result.success is True
    assert drag_result.result == {"x1": 1, "y1": 2, "x2": 30, "y2": 40}
    assert open_result.success is True
    assert open_result.result == {"name": "notepad", "message": "opened notepad"}
    assert switch_result.success is True
    assert switch_result.result == {"name": "notepad", "message": "switched notepad"}
    assert focus_result.success is True
    assert focus_result.result == {"title": "Untitled - Notepad", "message": "focused Untitled - Notepad"}


def test_gui_action_returns_structured_error() -> None:
    backend = FakeGuiBackend(fail_tool="click")

    result = click(1, 2, backend=backend)

    assert result.success is False
    assert result.error == {"code": "CLICK_FAILED", "message": "RuntimeError: click failed"}
