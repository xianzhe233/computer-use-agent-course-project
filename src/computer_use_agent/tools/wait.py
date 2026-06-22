from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(slots=True)
class WaitResult:
    tool_name: str
    success: bool
    duration_ms: int
    result: dict[str, object]
    error: dict[str, str] | None = None


def wait(seconds: int) -> WaitResult:
    started_at = time.perf_counter()
    time.sleep(seconds)
    duration_ms = int((time.perf_counter() - started_at) * 1000)
    return WaitResult(
        tool_name="wait",
        success=True,
        duration_ms=duration_ms,
        result={"seconds": seconds},
    )
