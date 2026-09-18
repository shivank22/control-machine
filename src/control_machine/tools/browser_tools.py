"""The browser tool surface.

Deliberately small. Playwright MCP exposes 40+ tools; an 8B model picking from that many
schemas spends most of its budget choosing rather than acting. These eleven cover the
flows we care about, and every acting tool returns a fresh page snapshot so the model
never has to spend a turn re-observing.

Refs come from the snapshot and resolve through Playwright's ``aria-ref=`` engine, which
is exact. ``browser_screenshot`` is for looking, not acting, and ``browser_click_xy`` is
the escape hatch of last resort.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, tool
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from ..browser import BrowserError, BrowserSession
from ..config import Settings

ACTING_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_select_option",
        "browser_press_key",
        "browser_scroll",
        "browser_click_xy",
    }
)


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
) -> list[BaseTool]:
    """Bind the tool surface to one browser session."""

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
    async def browser_snapshot() -> str:
        """Re-read the current page's elements and their refs."""
        try:
            return await session.observation("Current page.")
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, "Could not read the page")

    @tool
    async def browser_click(ref: str, element: str) -> str:
        """Click an element.

        Args:
            ref: Element ref from the latest snapshot, for example e12.
            element: Short human description of what you are clicking, for the activity log.
        """
        try:
            _, locator = await session.resolve(ref)
            await locator.scroll_into_view_if_needed(timeout=5_000)
            await locator.click(timeout=15_000)
            vision.clear()
            return await observe(f"Clicked {element}.")
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, f"Could not click {element} ({ref})")

    @tool
    async def browser_type(ref: str, text: str, submit: bool = False) -> str:
        """Type text into an input or textarea, replacing what is already there.

        Args:
            ref: Element ref from the latest snapshot.
            text: Text to enter.
            submit: Press Enter afterwards, which submits most search boxes and forms.
        """
        try:
            page, locator = await session.resolve(ref)
            await locator.scroll_into_view_if_needed(timeout=5_000)
            await locator.fill(text, timeout=15_000)
            note = f"Typed {text!r}."
            if submit:
                await locator.press("Enter")
                await _settle(page)
                note += " Pressed Enter."
            vision.clear()
            return await observe(note)
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, f"Could not type into {ref}")

    @tool
    async def browser_select_option(ref: str, value: str) -> str:
        """Choose an option in a dropdown.

        Args:
            ref: Element ref of the select element.
            value: Visible label of the option to choose.
        """
        try:
            _, locator = await session.resolve(ref)
            try:
                await locator.select_option(label=value, timeout=10_000)
            except PlaywrightError:
                await locator.select_option(value=value, timeout=10_000)
            vision.clear()
            return await observe(f"Selected {value!r}.")
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, f"Could not select {value!r} in {ref}")

    @tool
    async def browser_press_key(key: str) -> str:
        """Press a keyboard key on the page, such as Enter, Escape, Tab or ArrowDown.

        Args:
            key: Key name as Playwright spells it.
        """
        try:
            page = await session.page()
            await page.keyboard.press(key)
            await _settle(page)
            vision.clear()
            return await observe(f"Pressed {key}.")
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, f"Could not press {key}")

    @tool
    async def browser_scroll(direction: str = "down", pages: float = 1.0) -> str:
        """Scroll the page to bring offscreen content into view.

        Args:
            direction: "down", "up", "top" or "bottom".
            pages: How many viewport heights to scroll, for up and down.
        """
        try:
            page = await session.page()
            move = {
                "top": "() => window.scrollTo(0, 0)",
                "bottom": "() => window.scrollTo(0, document.body.scrollHeight)",
            }.get(direction.lower())
            if move:
                await page.evaluate(move)
            elif direction.lower() in {"down", "up"}:
                sign = 1 if direction.lower() == "down" else -1
                await page.evaluate(
                    "([sign, pages]) => window.scrollBy(0, sign * pages * window.innerHeight)",
                    [sign, pages],
                )
            else:
                return "direction must be one of: down, up, top, bottom."
            await page.wait_for_timeout(350)
            vision.clear()
            return await observe(f"Scrolled {direction}.")
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, f"Could not scroll {direction}")

    @tool
    async def browser_wait_for(text: str | None = None, seconds: float | None = None) -> str:
        """Wait for text to appear on the page, or just wait a fixed time.

        Args:
            text: Text to wait for, up to 20 seconds.
            seconds: Fixed wait instead, capped at 20.
        """
        try:
            page = await session.page()
            if text:
                await page.get_by_text(text, exact=False).first.wait_for(
                    state="visible", timeout=20_000
                )
                return await observe(f"{text!r} appeared.")
            wait = min(float(seconds or 2.0), 20.0)
            await page.wait_for_timeout(wait * 1000)
            return await observe(f"Waited {wait:g}s.")
        except PlaywrightTimeout:
            return await observe(f"{text!r} did not appear within 20s.")
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, "Could not wait")

    @tool
    async def browser_read_page() -> str:
        """Read the page as plain text. Use this to extract or summarise content."""
        try:
            page = await session.page()
            text = await page.evaluate(
                "() => (document.querySelector('main') || document.body).innerText"
            )
            text = "\n".join(line.rstrip() for line in text.splitlines() if line.strip())
            limit = settings.snapshot_max_chars * 2
            if len(text) > limit:
                text = text[:limit] + "\n... text truncated; scroll for more."
            header = await session.page_header()
            return f"{header}\n\ntext:\n{text}"
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, "Could not read the page text")

    @tool
    async def browser_screenshot(reason: str, marks: bool = False) -> str:
        """Look at the page as an image. Use only when the elements list is not enough,
        for example canvas, charts, maps, unlabelled icons, or to confirm how something looks.

        Args:
            reason: Why the picture is needed.
            marks: Draw numbered boxes over clickable elements and list their refs. Use this
                when you can see a control in the picture but cannot find it in the elements.
        """
        try:
            image_b64, found, path = await session.screenshot(
                marks=marks, save_to=vision.next_path()
            )
            caption = f"Screenshot: {reason}"
            legend = ""
            if marks and found:
                lines = "\n".join(f"  {m.number}. {m.description} -> ref {m.ref}" for m in found)
                legend = (
                    f"\n{len(found)} numbered elements in the picture. "
                    f"Click one with browser_click using its ref:\n{lines}"
                )
            elif marks:
                legend = "\nNo clickable elements could be marked in the current viewport."

            vision.set(image_b64, caption, path)
            note = f"{caption} The image is attached to this conversation.{legend}"
            if path is not None:
                note += f"\n[screenshot: {path.as_posix()}]"
            return note
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, "Could not take a screenshot")

    @tool
    async def browser_click_xy(x: float, y: float, element: str) -> str:
        """Last resort: click raw viewport coordinates. Only use when the element has no ref
        and cannot be marked in a screenshot. Prefer browser_click with a ref.

        Args:
            x: Horizontal position in CSS pixels from the left of the viewport.
            y: Vertical position in CSS pixels from the top of the viewport.
            element: Short description of what is being clicked.
        """
        try:
            page = await session.page()
            await page.mouse.click(x, y)
            await _settle(page)
            vision.clear()
            return await observe(f"Clicked {element} at ({x:g}, {y:g}).")
        except (BrowserError, PlaywrightError) as exc:
            return _explain(exc, f"Could not click at ({x:g}, {y:g})")

    return [
        browser_navigate,
        browser_snapshot,
        browser_click,
        browser_type,
        browser_select_option,
        browser_press_key,
        browser_scroll,
        browser_wait_for,
        browser_read_page,
        browser_screenshot,
        browser_click_xy,
    ]


async def _settle(page: Any) -> None:
    """Give a click or keypress a moment to navigate before we snapshot again."""
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8_000)
    except PlaywrightError:
        pass


def _explain(exc: Exception, prefix: str) -> str:
    """Hand failures back as guidance; the agent should retry, not crash."""
    if isinstance(exc, PlaywrightTimeout):
        return (
            f"{prefix}: timed out. The element may be hidden, covered, or the page may "
            "still be loading. Take a fresh browser_snapshot and try again."
        )
    detail = str(exc).split("\nCall log:")[0].strip()
    return f"{prefix}: {detail}"
