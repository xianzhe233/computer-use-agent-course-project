from pathlib import Path

import pytest

from computer_use_agent.tools.run_command import (
    CommandExecutionError,
    CommandResult,
    PowerShellBackend,
    run_command,
)


class FakeBackend(PowerShellBackend):
    def __init__(self, result: CommandResult) -> None:
        super().__init__(executable="fake-powershell")
        self.result = result
        self.calls: list[tuple[str, int, Path | None]] = []

    def execute(self, command: str, timeout: int = 10, cwd: Path | None = None) -> CommandResult:
        self.calls.append((command, timeout, cwd))
        return self.result


class FakeWindowsUseDesktop:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, Path | None]] = []

    def execute_command(
        self,
        command: str,
        timeout: int = 10,
        cwd: Path | None = None,
    ) -> tuple[str, int]:
        self.calls.append((command, timeout, cwd))
        return "windows-use-output", 0


def test_run_command_returns_structured_result() -> None:
    expected = CommandResult(
        command="Get-ChildItem",
        stdout="file.txt",
        stderr="",
        exit_code=0,
        success=True,
        duration_ms=12,
    )
    backend = FakeBackend(expected)

    result = run_command("Get-ChildItem", timeout_s=15, cwd=Path("."), backend=backend)

    assert result.command == "Get-ChildItem"
    assert result.stdout == "file.txt"
    assert result.stderr == ""
    assert result.exit_code == 0
    assert result.success is True
    assert backend.calls == [("Get-ChildItem", 15, Path("."))]


def test_powershell_backend_wraps_windows_use_execute_command() -> None:
    desktop = FakeWindowsUseDesktop()
    backend = PowerShellBackend(desktop_backend=desktop)

    result = backend.execute("Get-ChildItem", timeout=7, cwd=Path("."))

    assert result.success is True
    assert result.stdout == "windows-use-output"
    assert result.stderr == ""
    assert result.metadata["source"] == "Windows-Use Desktop.execute_command"
    assert desktop.calls == [("Get-ChildItem", 7, Path("."))]


def test_run_command_rejects_empty_command() -> None:
    with pytest.raises(CommandExecutionError):
        run_command("   ")
