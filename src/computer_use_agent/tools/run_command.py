from __future__ import annotations

import base64
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


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


class PowerShellBackend:
    def __init__(self, executable: str = "powershell") -> None:
        self.executable = executable

    def execute(self, command: str, timeout: int = 10, cwd: Path | None = None) -> CommandResult:
        started_at = time.perf_counter()
        working_dir = cwd or Path(os.path.expanduser("~"))

        try:
            utf8_command = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command
            encoded = base64.b64encode(utf8_command.encode("utf-16le")).decode("ascii")
            completed = subprocess.run(
                [self.executable, "-NoProfile", "-EncodedCommand", encoded],
                capture_output=True,
                timeout=timeout,
                cwd=str(working_dir),
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
                metadata={"cwd": str(working_dir), "risk_policy": "reserved"},
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int((time.perf_counter() - started_at) * 1000)
            stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
            stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
            return CommandResult(
                command=command,
                stdout=stdout,
                stderr=stderr,
                exit_code=1,
                success=False,
                duration_ms=duration_ms,
                timed_out=True,
                note="Command execution timed out",
                metadata={"cwd": str(working_dir), "risk_policy": "reserved"},
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
                metadata={"cwd": str(working_dir), "risk_policy": "reserved"},
            )


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
