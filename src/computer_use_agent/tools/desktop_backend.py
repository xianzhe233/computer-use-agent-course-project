from __future__ import annotations

import base64
import csv
import io
import os
import re
import subprocess
from pathlib import Path
from time import sleep
from typing import Literal

from fuzzywuzzy import process
from PIL import Image

from . import _uia
from ._uia_config import KEY_ALIASES
from ._uia_utils import escape_text_for_sendkeys
from .screenshot import capture_screen_image


class DesktopBackend:
    """Windows GUI / command / screenshot backend built on our vendored UIA library."""

    def __init__(self) -> None:
        self._last_app_name = ""
        self._last_appid = ""

    # -- command ----------------------------------------------------------------

    def execute_command(
        self,
        command: str,
        timeout: int = 180,
        cwd: Path | None = None,
    ) -> tuple[str, int]:
        try:
            utf8_command = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + command
            encoded = base64.b64encode(utf8_command.encode("utf-16le")).decode("ascii")
            result = subprocess.run(
                ["powershell", "-NoProfile", "-EncodedCommand", encoded],
                capture_output=True,
                timeout=timeout,
                cwd=str(cwd or Path(os.path.expanduser(path="~"))),
            )
            stdout = result.stdout.decode("utf-8", errors="replace") if result.stdout else ""
            stderr = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""
            return (stdout or stderr, result.returncode)
        except subprocess.TimeoutExpired:
            return ("Command execution timed out", 1)
        except Exception as e:
            return (f"Command execution failed: {type(e).__name__}: {e}", 1)

    # -- app / window -----------------------------------------------------------

    def get_apps_from_start_menu(self) -> dict[str, str]:
        command = "Get-StartApps | ConvertTo-Csv -NoTypeInformation"
        apps_info, status = self.execute_command(command)
        if status != 0 or not apps_info:
            return {}
        try:
            reader = csv.DictReader(io.StringIO(apps_info.strip()))
            return {name.lower(): appid for row in reader if (name := row.get("Name")) and (appid := row.get("AppID"))}
        except Exception:
            return {}

    def launch_app(self, name: str) -> tuple[str, int, int]:
        apps_map = self.get_apps_from_start_menu()
        matched_app = process.extractOne(name, apps_map.keys(), score_cutoff=70)
        if matched_app is None:
            return (f"{name.title()} not found in start menu.", 1, 0)
        app_name, _ = matched_app
        appid = apps_map.get(app_name)
        if appid is None:
            return (f"{name.title()} not found in start menu.", 1, 0)

        self._last_app_name = app_name
        self._last_appid = appid

        pid = 0
        if os.path.exists(appid) or "\\" in appid:
            command = f'Start-Process "{appid}" -PassThru | Select-Object -ExpandProperty Id'
            response, status = self.execute_command(command)
            if status == 0 and response.strip().isdigit():
                pid = int(response.strip())
        else:
            command = f'Start-Process "shell:AppsFolder\\{appid}"'
            response, status = self.execute_command(command)
        return response, status, pid

    def open_app(self, name: str) -> str:
        response, status, pid = self.launch_app(name)
        if status != 0:
            raise RuntimeError(response)
        if pid <= 0:
            return f"Launching {name.title()} sent."

        launched = False
        target_window = None
        try:
            target_window = _uia.WindowControl(ProcessId=pid)
            if target_window.Exists(maxSearchSeconds=10):
                launched = True
        except Exception:
            launched = True

        if launched and target_window is not None:
            try:
                target_window.SetActive()
            except Exception:
                try:
                    rect = target_window.BoundingRectangle
                    _uia.Click(rect.left + 200, rect.top + 200)
                except Exception:
                    pass
            return f"{name.title()} launched."
        return f"Launching {name.title()} sent, but window not detected yet."

    def switch_app(self, name: str) -> str:
        target_window = self._find_window_by_name(name)
        if target_window is None:
            raise RuntimeError(f"Application {name.title()} not found.")
        target_window.SetActive()
        title = self._window_title(target_window) or name.title()
        if target_window.IsMinimize():
            return f"{title} restored from minimized and switched to it."
        return f"Switched to {title} window."

    def focus_window(self, title: str) -> str:
        target_window = self._find_window_by_name(title)
        if target_window is None:
            raise RuntimeError(f"Window {title!r} not found.")
        target_window.SetActive()
        resolved_title = self._window_title(target_window) or title
        return f"Focused window {resolved_title}."

    # -- screenshot -------------------------------------------------------------

    def get_screenshot(self, as_bytes: bool = False) -> bytes | Image.Image:
        screenshot = capture_screen_image()
        if as_bytes:
            buffered = io.BytesIO()
            screenshot.save(buffered, format="PNG")
            screenshot_bytes = buffered.getvalue()
            buffered.close()
            return screenshot_bytes
        return screenshot

    def capture(self, path: os.PathLike[str] | str, target: str = "screen") -> tuple[int, int]:
        if target != "screen":
            raise ValueError(f"Unsupported screenshot target: {target}")
        screenshot = self.get_screenshot()
        image = screenshot if isinstance(screenshot, Image.Image) else Image.open(io.BytesIO(screenshot))
        image.save(path, format="PNG")
        return image.size

    # -- mouse ------------------------------------------------------------------

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        if clicks == 0:
            _uia.SetCursorPos(x, y)
            return
        match button:
            case "left":
                if clicks >= 2:
                    _uia.DoubleClick(x, y)
                else:
                    _uia.Click(x, y)
            case "right":
                for _ in range(clicks):
                    _uia.RightClick(x, y)
            case "middle":
                for _ in range(clicks):
                    _uia.MiddleClick(x, y)
            case _:
                raise ValueError(f"Unsupported mouse button: {button}")

    def move(self, x: int, y: int) -> None:
        _uia.MoveTo(x, y, moveSpeed=10)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.move(x1, y1)
        cx, cy = _uia.GetCursorPos()
        _uia.DragTo(cx, cy, x2, y2)

    # -- keyboard ---------------------------------------------------------------

    def type_text(
        self,
        text: str,
        *,
        x: int | None = None,
        y: int | None = None,
        clear: bool = False,
        caret_position: Literal["start", "idle", "end"] = "idle",
        press_enter: bool = False,
    ) -> None:
        if x is not None and y is not None:
            _uia.Click(x, y)
        if caret_position == "start":
            _uia.SendKeys("{Home}", waitTime=0.05)
        elif caret_position == "end":
            _uia.SendKeys("{End}", waitTime=0.05)
        if clear:
            sleep(0.5)
            _uia.SendKeys("{Ctrl}a", waitTime=0.05)
            _uia.SendKeys("{Back}", waitTime=0.05)
        if self._should_use_clipboard_paste(text):
            self._set_clipboard_text(text)
            self.hotkey("ctrl+v")
        else:
            escaped_text = escape_text_for_sendkeys(text)
            _uia.SendKeys(escaped_text, interval=0.01, waitTime=0.05)
        if press_enter:
            _uia.SendKeys("{Enter}", waitTime=0.05)

    def hotkey(self, shortcut: str) -> None:
        keys = shortcut.split("+")
        sendkeys_str = ""
        for key in keys:
            key = key.strip()
            if len(key) == 1:
                sendkeys_str += key
            else:
                name = KEY_ALIASES.get(key.lower(), key)
                sendkeys_str += "{" + name + "}"
        _uia.SendKeys(sendkeys_str, interval=0.01)

    # -- scroll -----------------------------------------------------------------

    def scroll(
        self,
        x: int | None = None,
        y: int | None = None,
        *,
        axis: Literal["horizontal", "vertical"] = "vertical",
        direction: Literal["up", "down", "left", "right"] = "down",
        amount: int = 1,
    ) -> None:
        if x is not None and y is not None:
            self.move(x, y)
        if amount < 1:
            raise ValueError("amount must be >= 1")
        match axis:
            case "vertical":
                match direction:
                    case "up":
                        _uia.WheelUp(amount)
                    case "down":
                        _uia.WheelDown(amount)
                    case _:
                        raise ValueError('Invalid direction. Use "up" or "down" for vertical scroll.')
            case "horizontal":
                match direction:
                    case "left":
                        _uia.WheelLeft(amount)
                    case "right":
                        _uia.WheelRight(amount)
                    case _:
                        raise ValueError('Invalid direction. Use "left" or "right" for horizontal scroll.')
            case _:
                raise ValueError('Invalid axis. Use "horizontal" or "vertical".')

    # -- helpers ----------------------------------------------------------------

    def _find_window_by_name(self, name: str):
        for candidate in self._window_name_candidates(name):
            safe_name = re.escape(candidate)
            target_window = _uia.WindowControl(RegexName=f"(?i).*{safe_name}.*")
            if target_window.Exists(maxSearchSeconds=2):
                return target_window
        return None

    def _window_name_candidates(self, name: str) -> list[str]:
        candidates: list[str] = [name]
        if self._last_app_name:
            candidates.append(self._last_app_name)
        if self._last_appid:
            appid_prefix = self._last_appid.split("!", 1)[0].split("_", 1)[0].split(".")[-1]
            candidates.append(appid_prefix)
            camel_tokens = re.findall(r"[A-Z][a-z]*", appid_prefix)
            candidates.extend(camel_tokens)
        seen: set[str] = set()
        unique_candidates: list[str] = []
        for candidate in candidates:
            normalized = candidate.strip()
            if normalized and normalized not in seen:
                unique_candidates.append(normalized)
                seen.add(normalized)
        return unique_candidates

    @staticmethod
    def _window_title(window: object) -> str:
        name = getattr(window, "Name", "")
        if isinstance(name, str):
            return name.strip()
        return str(name).strip()

    @staticmethod
    def _should_use_clipboard_paste(text: str) -> bool:
        return any(ord(ch) > 127 for ch in text)

    @staticmethod
    def _set_clipboard_text(text: str) -> None:
        script = "$text = @'\n" + text + "\n'@; Set-Clipboard -Value $text"
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        subprocess.run(
            ["powershell", "-NoProfile", "-EncodedCommand", encoded],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
