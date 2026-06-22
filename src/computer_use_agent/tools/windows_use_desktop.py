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
from PIL import Image, ImageGrab

from computer_use_agent._vendor.windows_use import uia
from computer_use_agent._vendor.windows_use.desktop.config import KEY_ALIASES
from computer_use_agent._vendor.windows_use.desktop.utils import escape_text_for_sendkeys


class WindowsUseDesktopBackend:
    def __init__(self) -> None:
        self._last_app_name = ""
        self._last_appid = ""

    def execute_command(
        self,
        command: str,
        timeout: int = 10,
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

    def get_apps_from_start_menu(self) -> dict[str, str]:
        command = "Get-StartApps | ConvertTo-Csv -NoTypeInformation"
        apps_info, status = self.execute_command(command)

        if status != 0 or not apps_info:
            return {}

        try:
            reader = csv.DictReader(io.StringIO(apps_info.strip()))
            apps_map: dict[str, str] = {}
            for row in reader:
                name = row.get("Name")
                appid = row.get("AppID")
                if name and appid:
                    apps_map[name.lower()] = appid
            return apps_map
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
            return response

        launched = False
        target_window = None
        try:
            if pid > 0:
                target_window = uia.WindowControl(ProcessId=pid)
                if target_window.Exists(maxSearchSeconds=10):
                    launched = True

            if not launched:
                for candidate in self._window_name_candidates(name):
                    safe_name = re.escape(candidate)
                    target_window = uia.WindowControl(RegexName=f"(?i).*{safe_name}.*")
                    if target_window.Exists(maxSearchSeconds=2):
                        launched = True
                        break
        except Exception:
            launched = True

        if launched and target_window is not None:
            try:
                rect = target_window.BoundingRectangle
                uia.Click(rect.left + 200, rect.top + 200)
            except Exception:
                pass
            return f"{name.title()} launched."
        return f"Launching {name.title()} sent, but window not detected yet."

    def get_screenshot(self, as_bytes: bool = False) -> bytes | Image.Image:
        try:
            screenshot = ImageGrab.grab(all_screens=True)
        except Exception:
            screenshot = ImageGrab.grab()
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
        image = (
            screenshot if isinstance(screenshot, Image.Image) else Image.open(io.BytesIO(screenshot))
        )
        image.save(path, format="PNG")
        return image.size

    def click(self, x: int, y: int, *, button: str = "left", clicks: int = 1) -> None:
        loc = (x, y)
        x, y = loc
        if clicks == 0:
            uia.SetCursorPos(x, y)
            return
        match button:
            case "left":
                if clicks >= 2:
                    uia.DoubleClick(x, y)
                else:
                    uia.Click(x, y)
            case "right":
                for _ in range(clicks):
                    uia.RightClick(x, y)
            case "middle":
                for _ in range(clicks):
                    uia.MiddleClick(x, y)

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
            uia.Click(x, y)
        if caret_position == "start":
            uia.SendKeys("{Home}", waitTime=0.05)
        elif caret_position == "end":
            uia.SendKeys("{End}", waitTime=0.05)
        if clear:
            sleep(0.5)
            uia.SendKeys("{Ctrl}a", waitTime=0.05)
            uia.SendKeys("{Back}", waitTime=0.05)
        if self._should_use_clipboard_paste(text):
            self._set_clipboard_text(text)
            self.hotkey("ctrl+v")
        else:
            escaped_text = escape_text_for_sendkeys(text)
            uia.SendKeys(escaped_text, interval=0.01, waitTime=0.05)
        if press_enter:
            uia.SendKeys("{Enter}", waitTime=0.05)

    def move(self, x: int, y: int) -> None:
        loc = (x, y)
        x, y = loc
        uia.MoveTo(x, y, moveSpeed=10)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.move(x1, y1)
        loc = (x2, y2)
        x, y = loc
        cx, cy = uia.GetCursorPos()
        uia.DragTo(cx, cy, x, y)

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
        uia.SendKeys(sendkeys_str, interval=0.01)

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
