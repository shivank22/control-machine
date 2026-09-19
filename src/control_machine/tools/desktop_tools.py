"""Minimal computer-use tools for this Mac session.

These drive native apps when the user is not sitting in the live view. Web work
should still go through the browser tools. Accessibility and Screen Recording must
be granted to the process that runs control-machine.
"""

from __future__ import annotations

import base64
import subprocess

from langchain_core.tools import BaseTool, tool

from ..config import Settings
from ..desktop import DesktopError, capture_jpeg, click_at, press_key, type_text
from .browser_tools import VisionBuffer


def build_desktop_tools(settings: Settings, vision: VisionBuffer) -> list[BaseTool]:
    @tool
    def desktop_screenshot(reason: str = "") -> str:
        """Capture this Mac's screen so you can see native apps (not the Docker Chrome).

        Args:
            reason: Short note about why you need the picture.
        """
        dest = vision.next_path()
        if dest is None:
            dest = settings.runs_dir / "desktop.jpg"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            data, _, _ = capture_jpeg()
        except DesktopError as exc:
            return str(exc)
        dest.write_bytes(data)
        caption = reason.strip() or "Desktop"
        vision.set(base64.b64encode(data).decode("ascii"), caption, dest)
        return f"{caption}. Screenshot saved."

    @tool
    def app_open(name: str) -> str:
        """Open a macOS application by name, e.g. Slack, Finder, Notes.

        Args:
            name: Application name as shown in /Applications.
        """
        app = name.strip().strip('"')
        if not app or any(ch in app for ch in "\n\r;|&"):
            return "Invalid application name."
        result = subprocess.run(
            ["open", "-a", app],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "open failed").strip()
            return f"Could not open {app}: {err}"
        return f"Opened {app}."

    @tool
    def desktop_click(x: int, y: int) -> str:
        """Click a pixel on this Mac's screen. Coordinates match desktop_screenshot.

        Args:
            x: Horizontal pixel from the left.
            y: Vertical pixel from the top.
        """
        try:
            click_at(x, y)
        except DesktopError as exc:
            return str(exc)
        return f"Clicked ({int(x)}, {int(y)})."

    @tool
    def desktop_type(text: str) -> str:
        """Type text into the frontmost app using the system keyboard.

        Args:
            text: Characters to type. Do not use this for passwords.
        """
        if not text:
            return "Nothing to type."
        try:
            type_text(text)
        except DesktopError as exc:
            return str(exc)
        return f"Typed {len(text)} characters."

    @tool
    def desktop_key(key: str) -> str:
        """Press a named key in the frontmost app (return, tab, escape, space, delete).

        Args:
            key: Key name.
        """
        try:
            press_key(key)
        except DesktopError as exc:
            return str(exc)
        return f"Pressed {key.strip()}."

    return [desktop_screenshot, app_open, desktop_click, desktop_type, desktop_key]
