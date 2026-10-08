"""The browser tool surface the specialist model is allowed to call.

The model opens a known URL, reads the page, and hands the goal to ``browser_drive``.
Jev then picks one id from the page catalog. Click, type, select, key, scroll, and
coordinate clicks stay on ``BrowserSession`` and are performed by that loop, not chosen
by the model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..browser import BrowserError, BrowserSession
from ..config import Settings
from ..jev_browser import drive_browser

log = logging.getLogger(__name__)

ACTING_TOOLS = frozenset({"browser_navigate", "browser_drive"})


@dataclass
class VisionBuffer:
    """Holds the most recent screenshot so middleware can show it to the model.

    Ollama's gemma4 template drops images attached to tool-role messages, so a screenshot
    returned straight from a tool is invisible to the model. The tool stores it here and
    ``ScreenshotVisionMiddleware`` replays it as a user-role image on the next model call.
    Keeping only the latest image also stops a long run from accreting megabytes of stale
    screenshots in the context window.
    """

    image_b64: str | None = None
    caption: str = ""
    path: Path | None = None
    run_dir: Path | None = None
    counter: int = field(default=0)

    def set(self, image_b64: str, caption: str, path: Path | None) -> None:
        self.image_b64 = image_b64
        self.caption = caption
        self.path = path

    def clear(self) -> None:
        """Called once the page has moved on and the image no longer shows reality."""
        self.image_b64 = None
        self.caption = ""
        self.path = None

    def next_path(self) -> Path | None:
        if self.run_dir is None:
            return None
        self.counter += 1
        return self.run_dir / f"shot-{self.counter:03d}.jpg"


def build_browser_tools(
    session: BrowserSession,
    vision: VisionBuffer,
    settings: Settings,
    *,
    model: BaseChatModel | None = None,
    running: Any | None = None,
) -> list[BaseTool]:
    """Bind the model-facing browser tools to one session."""

    async def observe(note: str) -> str:
        try:
            return await session.observation(note)
        except BrowserError as exc:
            return f"{note}\n\n(Could not re-read the page: {exc})"

    @tool
    async def browser_navigate(url: str) -> str:
        """Open a URL in the browser. Returns the new page's elements.

        Args:
            url: Full URL including https://.
        """
        try:
            page = await session.page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            vision.clear()
            return await observe(f"Opened {url}")
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, f"Could not open {url}")

    @tool
    async def browser_read_page() -> str:
        """Read the page as plain text. Use this to extract or summarise content."""
        try:
            header = await session.page_header()
            text = await session.visible_text(limit=settings.snapshot_max_chars * 2)
            return f"{header}\n\ntext:\n{text}"
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, "Could not read the page text")

    @tool
    async def browser_drive(goal: str) -> str:
        """Choose and perform clicks, scrolls, and typing until the goal is done.

        Args:
            goal: What to accomplish on the current page, including any values to enter.
                Say so explicitly if this run must not submit, save, or pay.
        """
        if model is None:
            return "No chat model is available to type into the page."
        try:
            return await drive_browser(
                session=session,
                settings=settings,
                model=model,
                goal=goal,
                running=running,
                vision=vision,
            )
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, "Could not drive the browser")

    return [browser_navigate, browser_read_page, browser_drive]


def _explain(exc: Exception, prefix: str) -> str:
    """Hand failures back as guidance; the agent should retry, not crash."""
    log.warning("%s: %s", prefix, exc)
    if isinstance(exc, PlaywrightTimeout):
        return (
            f"{prefix}: timed out. The page may still be loading. "
            "Call browser_drive again or browser_read_page to see where things stand."
        )
    detail = str(exc).split("\nCall log:")[0].strip()
    return f"{prefix}: {detail}"
