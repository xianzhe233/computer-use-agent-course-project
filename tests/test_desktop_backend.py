from __future__ import annotations

from computer_use_agent.tools.desktop_backend import DesktopBackend


class StoreAppBackend(DesktopBackend):
    def __init__(self) -> None:
        super().__init__()
        self.launch_calls: list[str] = []

    def launch_app(self, name: str) -> tuple[str, int, int]:
        self.launch_calls.append(name)
        return "started", 0, 0


def test_open_app_returns_after_successful_store_app_launch_without_uia_lookup() -> None:
    backend = StoreAppBackend()

    result = backend.open_app("记事本")

    assert result == "Launching 记事本 sent."
    assert backend.launch_calls == ["记事本"]
