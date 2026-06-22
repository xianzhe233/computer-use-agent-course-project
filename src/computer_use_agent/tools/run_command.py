from __future__ import annotations

import base64
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class CommandExecutionError(RuntimeError):
    pass


@dataclass(slots=True)
class CommandResult:
    command: str
    stdout: str
    stderr: str
    exit_code: int
    success: bool
    duration_ms: int
    shell: str = "powershell"
    timed_out: bool = False
    note: str = ""
    metadata: dict[str, object] = field(default_factory=dict)


class WindowsUseCommandBackend(Protocol):
    def execute_command(
        self,
        command: str,
        timeout: int = 10,
        cwd: Path | None = None,
    ) -> tuple[str, int]: ...


class PowerShellBackend:
    """Structured adapter around the Windows-Use PowerShell execution primitive.

    Windows-Use exposes `Desktop.execute_command(command, timeout) -> (response, status)`.
    Our runtime still needs `CommandResult` plus per-run cwd metadata, so this class keeps the
    upstream execution shape as the default path and only wraps its response into our structure.
    A local subprocess fallback is retained for non-Windows test environments or missing vendor deps.
    """

    def __init__(
        self,
        executable: str = "powershell",
        desktop_backend: WindowsUseCommandBackend | None = None,
    ) -> None:
        self.executable = executable
        self.desktop_backend = desktop_backend

    def execute(self, command: str, timeout: int = 10, cwd: Path | None = None) -> CommandResult:
        started_at = time.perf_counter()
        working_dir = cwd or Path(os.path.expanduser("~"))

        if self.executable == "powershell":
            windows_use_result = self._execute_with_windows_use(
                command=command,
                timeout=timeout,
                cwd=working_dir,
                started_at=started_at,
            )
            if windows_use_result is not None:
                return windows_use_result

        return self._execute_with_subprocess(
            command=command,
            timeout=timeout,
            cwd=working_dir,
            started_at=started_at,
            backend_name="subprocess_fallback",
        )

    def _execute_with_windows_use(
        self,
        *,
        command: str,
        timeout: int,
        cwd: Path,
        started_at: float,
    ) -> CommandResult | None:
        desktop_backend = self.desktop_backend or _create_windows_use_command_backend()
        if desktop_backend is None:
            return None

        try:
            response, status = desktop_backend.execute_command(command, timeout=timeout, cwd=cwd)
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            return CommandResult(
                command=command,
                stdout=response if status == 0 else "",
                stderr="" if status == 0 else response,
                exit_code=status,
                success=status == 0,
                duration_ms=duration_ms,
                metadata={
                    "cwd": str(cwd),
                    "risk_policy": "reserved",
                    "backend": desktop_backend.__class__.__name__,
                    "source": "Windows-Use Desktop.execute_command",
                },
            )
        except Exception:
            return None

    def _execute_with_subprocess(
        self,
        *,
        command: str,
        timeout: int,
        cwd: Path,
        started_at: float,
        backend_name: str,
    ) -> CommandResult:
        try:
            utf8_command = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command
            encoded = base64.b64encode(utf8_command.encode("utf-16le")).decode("ascii")
            completed = subprocess.run(
                [self.executable, "-NoProfile", "-EncodedCommand", encoded],
                capture_output=True,
                timeout=timeout,
                cwd=str(cwd),
            )
            stdout = completed.stdout.decode("utf-8", errors="replace") if completed.stdout else ""
            stderr = completed.stderr.decode("utf-8", errors="replace") if completed.stderr else ""
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            return CommandResult(
                command=command,
                stdout=stdout,
                stderr=stderr,
                exit_code=completed.returncode,
                success=completed.returncode == 0,
                duration_ms=duration_ms,
                metadata={"cwd": str(cwd), "risk_policy": "reserved", "backend": backend_name},
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            stdout = _decode_timeout_stream(exc.stdout)
            stderr = _decode_timeout_stream(exc.stderr)
            return CommandResult(
                command=command,
                stdout=stdout,
                stderr=stderr,
                exit_code=1,
                success=False,
                duration_ms=duration_ms,
                timed_out=True,
                note="Command execution timed out",
                metadata={"cwd": str(cwd), "risk_policy": "reserved", "backend": backend_name},
            )
        except Exception as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            return CommandResult(
                command=command,
                stdout="",
                stderr=f"Command execution failed: {type(exc).__name__}: {exc}",
                exit_code=1,
                success=False,
                duration_ms=duration_ms,
                note="Command execution failed",
                metadata={"cwd": str(cwd), "risk_policy": "reserved", "backend": backend_name},
            )


def _decode_timeout_stream(stream: bytes | str | None) -> str:
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream or ""


def _create_windows_use_command_backend() -> WindowsUseCommandBackend | None:
    try:
        from .windows_use_desktop import WindowsUseDesktopBackend
    except Exception:
        return None
    return WindowsUseDesktopBackend()


def run_command(
    command: str,
    timeout_s: int = 10,
    cwd: Path | None = None,
    backend: PowerShellBackend | None = None,
) -> CommandResult:
    if not command.strip():
        raise CommandExecutionError("command must not be empty")

    active_backend = backend or PowerShellBackend()
    return active_backend.execute(command=command, timeout=timeout_s, cwd=cwd)
