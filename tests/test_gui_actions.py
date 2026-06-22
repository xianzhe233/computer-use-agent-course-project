from computer_use_agent.tools.gui_actions import click, drag, hotkey, normalize_shortcut, type_text


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

    def hotkey(self, keys: tuple[str, ...]) -> None:
        self.calls.append(("hotkey", keys, {}))
        if self.fail_tool == "hotkey":
            raise RuntimeError("hotkey failed")

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.calls.append(("drag", (x1, y1, x2, y2), {}))
        if self.fail_tool == "drag":
            raise RuntimeError("drag failed")


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


def test_hotkey_and_drag_return_structured_result() -> None:
    backend = FakeGuiBackend()

    hotkey_result = hotkey("Ctrl+S", backend=backend)
    drag_result = drag(1, 2, 30, 40, backend=backend)

    assert hotkey_result.success is True
    assert hotkey_result.result == {"shortcut": "Ctrl+S", "normalized_keys": ["ctrl", "s"]}
    assert drag_result.success is True
    assert drag_result.result == {"x1": 1, "y1": 2, "x2": 30, "y2": 40}


def test_gui_action_returns_structured_error() -> None:
    backend = FakeGuiBackend(fail_tool="click")

    result = click(1, 2, backend=backend)

    assert result.success is False
    assert result.error == {"code": "CLICK_FAILED", "message": "RuntimeError: click failed"}
