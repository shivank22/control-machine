"""The browser session the agent acts through.

Attaches to a browser-container Chrome over CDP. Each container has a persistent profile
and an interactive noVNC view exposed in the dashboard.

The unit of perception is Playwright's AI-mode ARIA snapshot: a compact YAML tree where
every interactive element carries a ``[ref=e12]`` handle that resolves through the
``aria-ref=`` selector engine. Screenshots exist alongside it for the cases the
accessibility tree cannot describe, and the marked variant draws the snapshot's own
bounding boxes onto the image so a visual observation can still end in a ref-based click.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

from .config import Settings, get_settings

log = logging.getLogger(__name__)

# Refs are "e12" on the main frame and "f1e12" inside an iframe.
_REF_PATTERN = r"(?:f\d+)?e\d+"
# "- button \"Submit\" [ref=e12] [box=10,20,80,30]"
_BOX_RE = re.compile(
    rf"^\s*-\s*(?P<desc>.*?)\s*\[ref=(?P<ref>{_REF_PATTERN})\].*?\[box=(?P<box>[\d.,-]+)\]",
)
_REF_RE = re.compile(rf"^{_REF_PATTERN}$")


# Roles worth putting a number on. Marking layout nodes (table, rowgroup, cell, generic)
# buries the handful of things the agent can actually click.
_INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "link",
        "listbox",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "option",
        "radio",
        "searchbox",
        "slider",
        "spinbutton",
        "switch",
        "tab",
        "textbox",
        "treeitem",
    }
)
_MAX_MARKS = 60

# Fields the controller fills. Everything else interactive is a click.
_TYPE_ROLES = frozenset({"textbox", "searchbox"})
_CATALOG_LINE = re.compile(
    rf"^\s*-\s+(?P<desc>.*?)\s*\[ref=(?P<ref>{_REF_PATTERN})\](?P<tail>.*)$"
)


class BrowserError(RuntimeError):
    """Raised for failures we want to hand back to the model as a readable message."""


@dataclass(frozen=True)
class BrowserAction:
    """One action Jev may choose. The id is the only string that can be executed."""

    id: str
    kind: str
    description: str
    ref: str = ""
    value: str = ""


# Always offered, independent of the page. Ids stay closed so Jev cannot invent a URL.
STANDING_ACTIONS: tuple[BrowserAction, ...] = (
    BrowserAction(
        "scroll:down",
        "scroll",
        "Scroll down to reveal content below the current view",
        value="down",
    ),
    BrowserAction(
        "scroll:up",
        "scroll",
        "Scroll up to reveal content above the current view",
        value="up",
    ),
    BrowserAction(
        "wait:short",
        "wait",
        "Wait briefly for the page to finish a small update",
        value="short",
    ),
    BrowserAction(
        "wait:content",
        "wait",
        "Wait for loading content, a spinner, or a slow page update before acting",
        value="content",
    ),
    BrowserAction(
        "key:Enter",
        "key",
        "Press Enter, which submits most search boxes and forms",
        value="Enter",
    ),
    BrowserAction(
        "key:Escape",
        "key",
        "Press Escape to dismiss a dialog or menu",
        value="Escape",
    ),
    BrowserAction(
        "done",
        "done",
        "Stop because the goal is already achieved on this page",
    ),
    BrowserAction(
        "ask_user",
        "ask_user",
        "Stop because a person must take over, or no listed control can advance the goal",
    ),
)

# Closed wait budgets. Jev picks the id; code owns the duration.
WAIT_BUDGET_MS: dict[str, int] = {
    "short": 1_500,
    "content": 5_000,
}

_PAGE_CONTROL_KINDS = frozenset({"click", "type", "select", "click_xy"})
_LOADING_TEXT = re.compile(
    r"\b(loading|please wait|still working|fetching|one moment|spinner)\b",
    re.IGNORECASE,
)

# Sample the live DOM for settle. Avoid networkidle: analytics keep it busy forever.
_DOM_SAMPLE_JS = """() => {
  const root = document.querySelector('main') || document.body;
  const text = (root && root.innerText) || '';
  const busy = Boolean(
    document.querySelector(
      "[aria-busy='true'], [role='progressbar']:not([aria-hidden='true'])"
    )
  );
  const controls = document.querySelectorAll(
    'a[href], button:not([disabled]), input:not([type="hidden"]):not([disabled]), '
    + 'select:not([disabled]), textarea:not([disabled]), [role="button"], '
    + '[role="link"], [role="textbox"], [role="searchbox"], [role="combobox"]'
  ).length;
  const sig = text.slice(0, 500).replace(/\\s+/g, ' ').trim();
  return { busy, controls, sig };
}"""


@dataclass
class Mark:
    """One numbered label drawn on a marked screenshot."""

    number: int
    ref: str
    description: str
    box: tuple[float, float, float, float]


class BrowserSession:
    """A lazily-connected, auto-reconnecting handle on the user's Chrome."""

    def __init__(self, cdp_endpoint: str, *, preserve_pages: bool = False) -> None:
        settings = get_settings()
        self._endpoint = cdp_endpoint
        self._settings = settings
        self._preserve_pages = preserve_pages
        self._user_pages: list[Page] = []
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._lock = asyncio.Lock()
        self._marks: dict[int, Mark] = {}

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        async with self._lock:
            await self._ensure_connected()

    async def close(self) -> None:
        async with self._lock:
            try:
                # Detach from container Chrome; the container owns its lifetime. During
                # process shutdown Playwright's transport may already be gone.
                if self._browser is not None and self._browser.is_connected():
                    with contextlib.suppress(Exception):
                        await self._browser.close()
                if self._playwright is not None:
                    with contextlib.suppress(Exception):
                        await self._playwright.stop()
            finally:
                self._browser = None
                self._context = None
                self._page = None
                self._playwright = None

    @property
    def connected(self) -> bool:
        return self._browser is not None and self._browser.is_connected()

    async def _ensure_connected(self) -> None:
        if self.connected:
            return

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                self._endpoint,
                is_local=True,
                no_defaults=True,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as guidance
            log.exception("CDP attach failed for %s", self._endpoint)
            if self._preserve_pages:
                hint = (
                    "Chrome 136 and later will not open a debugging port on the "
                    "everyday profile, and a second launch is ignored while Chrome "
                    "is already open. Start a separate window with "
                    "'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
                    "--remote-debugging-port=9222 "
                    "--user-data-dir=$HOME/Library/Application\\ Support/control-machine/chrome' "
                    "and try again."
                )
            else:
                hint = (
                    "Start the browser containers with "
                    "'docker compose --profile browsers up -d' and try again."
                )
            raise BrowserError(
                f"Could not attach to Chrome at {self._endpoint}. {hint} "
                f"({type(exc).__name__}: {exc})"
            ) from exc

        if not self._browser.contexts:
            raise BrowserError("Chrome is running but exposes no browser context.")

        self._context = self._browser.contexts[0]
        # Follow popups and target=_blank tabs, which is where flows often continue.
        self._context.on("page", self._on_new_page)
        self._user_pages = [page for page in self._context.pages if not page.is_closed()]
        # A host browser keeps the user's existing tabs. The agent works in a new one.
        if self._preserve_pages:
            self._page = None
        else:
            self._page = self._user_pages[-1] if self._user_pages else None

    def _on_new_page(self, page: Page) -> None:
        self._page = page

    async def page(self) -> Page:
        async with self._lock:
            await self._ensure_connected()
            assert self._context is not None

            if self._preserve_pages:
                if self._page is None or self._page.is_closed():
                    self._page = await self._context.new_page()
                    await self._page.bring_to_front()
                return self._page

            if self._page is None or self._page.is_closed():
                open_pages = [p for p in self._context.pages if not p.is_closed()]
                self._page = open_pages[-1] if open_pages else await self._context.new_page()
            return self._page

    async def list_pages(self) -> list[Page]:
        await self.page()
        assert self._context is not None
        return [p for p in self._context.pages if not p.is_closed()]

    async def switch_page(self, index: int) -> Page:
        pages = await self.list_pages()
        if not 0 <= index < len(pages):
            raise BrowserError(f"No tab at index {index}; there are {len(pages)}.")
        self._page = pages[index]
        await self._page.bring_to_front()
        return self._page

    async def reset(self) -> None:
        """Return a slot to one blank tab without clearing its persistent login state."""
        if self._preserve_pages:
            self._marks.clear()
            self._page = None
            if self._context is not None:
                for page in list(self._context.pages):
                    if page in self._user_pages or page.is_closed():
                        continue
                    with contextlib.suppress(Exception):
                        await page.close()
            return
        pages = await self.list_pages()
        keep = pages[0]
        for page in pages[1:]:
            await page.close()
        await keep.goto("about:blank")
        await keep.bring_to_front()
        self._page = keep
        self._marks.clear()

    # ----------------------------------------------------------- perception

    async def snapshot(self, *, max_chars: int | None = None) -> str:
        """The compact accessibility view the agent normally acts on."""
        page = await self.page()
        limit = max_chars or self._settings.snapshot_max_chars

        try:
            text = await page.aria_snapshot(mode="ai")
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Could not read the page structure: {exc}") from exc

        if len(text) > limit:
            # Trim on a line boundary so we never hand the model a half-written node.
            head = text[:limit].rsplit("\n", 1)[0]
            omitted = text[len(head) :].count("\n")
            text = (
                f"{head}\n"
                f"... snapshot truncated, {omitted} more lines. "
                "Scroll or use browser_read_page to reach the rest."
            )
        return text

    async def page_header(self) -> str:
        page = await self.page()
        try:
            title = await page.title()
        except Exception:  # noqa: BLE001 - a navigating page can refuse this briefly
            title = ""
        return f"url: {page.url}\ntitle: {title}"

    async def observation(self, note: str = "") -> str:
        """Standard post-action payload: what happened, plus the fresh page state."""
        header = await self.page_header()
        snapshot = await self.snapshot()
        parts = [p for p in (note.strip(), header, f"page:\n{snapshot}") if p]
        return "\n\n".join(parts)

    def locator(self, page: Page, ref: str):
        ref = ref.strip().removeprefix("[ref=").removesuffix("]").strip()
        if not _REF_RE.match(ref):
            raise BrowserError(
                f"'{ref}' is not an element ref. Refs look like e12 (or f1e12 inside an "
                "iframe) and come from the latest page snapshot."
            )
        return page.locator(f"aria-ref={ref}")

    async def resolve(self, ref: str):
        page = await self.page()
        locator = self.locator(page, ref)
        if await locator.count() == 0:
            raise BrowserError(
                f"Ref {ref} is no longer on the page. Call browser_snapshot for current refs."
            )
        return page, locator

    async def action_catalog(self) -> list[BrowserAction]:
        """Interactive controls on the current page, plus the standing actions."""
        page = await self.page()
        try:
            text = await page.aria_snapshot(mode="ai")
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"Could not read the page structure: {exc}") from exc
        return catalog_from_snapshot(text)

    async def page_location(self) -> tuple[str, str]:
        page = await self.page()
        try:
            title = await page.title()
        except Exception:  # noqa: BLE001 - a navigating page can refuse this briefly
            title = ""
        return page.url, title

    async def visible_text(self, *, limit: int | None = None) -> str:
        """Plain text of the main content, truncated for a judgment request."""
        page = await self.page()
        raw = await page.evaluate(
            "() => (document.querySelector('main') || document.body).innerText"
        )
        text = "\n".join(line.rstrip() for line in str(raw or "").splitlines() if line.strip())
        cap = self._settings.snapshot_max_chars if limit is None else limit
        if len(text) > cap:
            text = text[:cap] + "\n... text truncated."
        return text

    async def settle(self, *, timeout_ms: int = 8_000) -> None:
        """Wait until the current page looks finished updating."""
        page = await self.page()
        await settle_page(page, timeout_ms=timeout_ms)

    async def wait_ready(self, budget: str = "content") -> None:
        """Wait using a closed budget id (short or content)."""
        timeout_ms = WAIT_BUDGET_MS.get(budget, WAIT_BUDGET_MS["content"])
        await self.settle(timeout_ms=timeout_ms)

    async def click_ref(self, ref: str) -> None:
        page, locator = await self.resolve(ref)
        await locator.scroll_into_view_if_needed(timeout=5_000)
        await locator.click(timeout=15_000)
        await settle_page(page)

    async def fill_ref(self, ref: str, text: str) -> None:
        page, locator = await self.resolve(ref)
        await locator.scroll_into_view_if_needed(timeout=5_000)
        await locator.fill(text, timeout=15_000)
        await settle_page(page, timeout_ms=4_000)

    async def select_ref(self, ref: str, value: str) -> None:
        page, locator = await self.resolve(ref)
        try:
            await locator.select_option(label=value, timeout=10_000)
        except PlaywrightError:
            await locator.select_option(value=value, timeout=10_000)
        await settle_page(page, timeout_ms=4_000)

    async def press_key(self, key: str) -> None:
        page = await self.page()
        await page.keyboard.press(key)
        await settle_page(page)

    async def scroll_page(self, direction: str, *, pages: float = 1.0) -> None:
        page = await self.page()
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
            raise BrowserError(f"Cannot scroll {direction!r}.")
        await page.wait_for_timeout(350)

    async def click_xy(self, x: float, y: float) -> None:
        page = await self.page()
        await page.mouse.click(x, y)
        await settle_page(page)

    # ------------------------------------------------------------- screenshots

    async def screenshot(
        self,
        *,
        marks: bool = False,
        save_to: Path | None = None,
    ) -> tuple[str, list[Mark], Path | None]:
        """Return a base64 JPEG of the viewport, optionally with numbered marks drawn on.

        Captured at ``scale="css"`` so image pixels line up with the CSS-pixel boxes the
        snapshot reports, which is what makes the overlay land on the right elements.
        """
        page = await self.page()
        raw = await page.screenshot(type="png", scale="css", animations="disabled")
        image = Image.open(io.BytesIO(raw)).convert("RGB")

        found: list[Mark] = []
        if marks:
            found = await self._collect_marks(page)
            image = _draw_marks(image, found)
            self._marks = {m.number: m for m in found}

        max_width = self._settings.screenshot_max_width
        if image.width > max_width:
            height = round(image.height * max_width / image.width)
            image = image.resize((max_width, height), Image.LANCZOS)

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=self._settings.screenshot_quality)
        data = buffer.getvalue()

        path: Path | None = None
        if save_to is not None:
            save_to.parent.mkdir(parents=True, exist_ok=True)
            save_to.write_bytes(data)
            path = save_to

        return base64.b64encode(data).decode("ascii"), found, path

    async def _collect_marks(self, page: Page) -> list[Mark]:
        text = await page.aria_snapshot(mode="ai", boxes=True)
        viewport = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
        vw, vh = float(viewport[0]), float(viewport[1])

        marks: list[Mark] = []
        seen_boxes: set[tuple[int, int, int, int]] = set()

        for line in text.splitlines():
            match = _BOX_RE.match(line)
            if not match:
                continue

            desc = match.group("desc")
            role = desc.split(maxsplit=1)[0].strip('":').lower() if desc.strip() else ""
            clickable = "[cursor=pointer]" in line
            if role not in _INTERACTIVE_ROLES and not clickable:
                continue

            try:
                x, y, w, h = (float(v) for v in match.group("box").split(","))
            except ValueError:
                continue
            # Only label what a human could actually see and hit.
            if w <= 0 or h <= 0 or w * h < 120:
                continue
            if x + w <= 0 or y + h <= 0 or x >= vw or y >= vh:
                continue

            # A link wrapping its own text reports the same box twice; label it once.
            key = (round(x), round(y), round(w), round(h))
            if key in seen_boxes:
                continue
            seen_boxes.add(key)

            marks.append(
                Mark(
                    number=len(marks) + 1,
                    ref=match.group("ref"),
                    description=_clean_description(desc),
                    box=(x, y, w, h),
                )
            )
            if len(marks) >= _MAX_MARKS:
                break

        return marks

    def mark(self, number: int) -> Mark | None:
        return self._marks.get(number)


@dataclass
class BrowserSlot:
    """One independently controllable browser and its dashboard viewer."""

    id: int
    cdp_endpoint: str
    novnc_url: str
    session: BrowserSession
    task_id: int | None = None
    held_thread_id: str | None = None
    enabled: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "cdp_endpoint": self.cdp_endpoint,
            "novnc_url": self.novnc_url,
            "task_id": self.task_id,
            "held_thread_id": self.held_thread_id,
            "connected": self.session.connected,
            "available": (
                self.enabled and self.task_id is None and self.held_thread_id is None
            ),
        }


class BrowserPool:
    """Own browser slots and lease each slot to at most one run."""

    def __init__(self, slots: list[BrowserSlot], max_parallel: int) -> None:
        self.slots = slots
        capacity = min(max_parallel, len(slots))
        self._available: asyncio.Queue[BrowserSlot] = asyncio.Queue(maxsize=capacity)
        for index, slot in enumerate(slots):
            slot.enabled = index < capacity
            if slot.enabled:
                self._available.put_nowait(slot)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> BrowserPool:
        resolved = settings or get_settings()
        if resolved.uses_host_browser:
            endpoint = resolved.browser_cdp_endpoint.strip() or "http://127.0.0.1:9222"
            return cls(
                [
                    BrowserSlot(
                        id=1,
                        cdp_endpoint=endpoint,
                        novnc_url="",
                        session=BrowserSession(endpoint, preserve_pages=True),
                    )
                ],
                1,
            )
        settings = resolved
        slots = []
        for index in range(settings.browser_slot_count):
            slot_id = index + 1
            cdp_port = settings.browser_cdp_base_port + index
            novnc_port = settings.browser_novnc_base_port + index
            endpoint = f"http://{settings.browser_host}:{cdp_port}"
            slots.append(
                BrowserSlot(
                    id=slot_id,
                    cdp_endpoint=endpoint,
                    novnc_url=(
                        f"http://{settings.browser_host}:{novnc_port}/vnc.html"
                        "?autoconnect=1&resize=scale&reconnect=1&show_dot=1"
                    ),
                    session=BrowserSession(endpoint),
                )
            )
        return cls(slots, settings.effective_parallel_runs)

    @property
    def has_available(self) -> bool:
        return not self._available.empty()

    async def acquire(self, task_id: int, preferred_id: int | None = None) -> BrowserSlot:
        candidates = []
        try:
            while True:
                candidates.append(self._available.get_nowait())
        except asyncio.QueueEmpty as exc:
            if not candidates:
                raise BrowserError("All browser slots are currently in use.") from exc

        slot = next((item for item in candidates if item.id == preferred_id), candidates[0])
        for candidate in candidates:
            if candidate is not slot:
                self._available.put_nowait(candidate)
        slot.task_id = task_id
        slot.held_thread_id = None
        try:
            await slot.session.start()
        except Exception:
            log.exception("Slot %s failed to start for task %s", slot.id, task_id)
            slot.task_id = None
            self._available.put_nowait(slot)
            raise
        return slot

    def activate_held(self, slot: BrowserSlot, task_id: int) -> None:
        slot.held_thread_id = None
        slot.task_id = task_id

    def hold(self, slot: BrowserSlot, thread_id: str) -> None:
        slot.task_id = None
        slot.held_thread_id = thread_id

    async def release(self, slot: BrowserSlot) -> None:
        try:
            await slot.session.reset()
        except Exception:  # noqa: BLE001 - a reconnect on the next lease is sufficient
            log.exception("Slot %s reset failed; closing the CDP session", slot.id)
            await slot.session.close()
        slot.task_id = None
        slot.held_thread_id = None
        self._available.put_nowait(slot)

    async def close(self) -> None:
        await asyncio.gather(*(slot.session.close() for slot in self.slots))

    def status(self) -> list[dict[str, object]]:
        return [slot.as_dict() for slot in self.slots]


def catalog_from_snapshot(text: str, *, limit: int = _MAX_MARKS) -> list[BrowserAction]:
    """Turn an AI-mode accessibility snapshot into the only actions Jev may pick.

    Layout nodes are dropped. At most ``limit`` page controls are kept, then the
    standing actions (scroll, wait, keys, done, ask_user) are appended.
    """
    found: list[BrowserAction] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = _CATALOG_LINE.match(line)
        if not match:
            continue
        ref = match.group("ref")
        if ref in seen:
            continue
        desc = match.group("desc")
        role = _role_of(desc)
        clickable = "[cursor=pointer]" in line
        if role not in _INTERACTIVE_ROLES and not clickable:
            continue
        kind = "type" if role in _TYPE_ROLES else "click"
        found.append(
            BrowserAction(
                id=f"{kind}:{ref}",
                kind=kind,
                description=_clean_description(desc),
                ref=ref,
            )
        )
        seen.add(ref)
        if len(found) >= limit:
            break
    return [*found, *STANDING_ACTIONS]


def looks_loading(*, visible_text: str, actions: Sequence[BrowserAction]) -> bool:
    """True when the snapshot still looks mid-load and acting would be premature.

    Pure heuristic for the drive loop: empty catalogs, or loading copy with almost
    no interactive controls. Busy pages with real controls are left alone.
    """
    controls = [action for action in actions if action.kind in _PAGE_CONTROL_KINDS]
    if not controls:
        return True
    head = visible_text[:800]
    return bool(_LOADING_TEXT.search(head) and len(controls) < 4)


def _role_of(desc: str) -> str:
    token = desc.strip().split(maxsplit=1)[0] if desc.strip() else ""
    return token.strip('":').lower()


def _clean_description(desc: str) -> str:
    desc = re.sub(r"\[[^\]]*\]", "", desc).strip().rstrip(":").strip()
    return desc[:80] or "element"


async def settle_page(page: Page, *, timeout_ms: int = 8_000) -> None:
    """Wait until navigation or an action's side-effects look finished.

    Uses ``domcontentloaded``, then a short DOM-quiet window. Avoids
    ``networkidle`` because analytics and long-polling keep the network busy.
    """
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 8_000))
    except PlaywrightError:
        pass
    quiet_ms = min(max(timeout_ms, 0), 5_000)
    if quiet_ms:
        await _wait_dom_quiet(page, timeout_ms=quiet_ms)


async def _wait_dom_quiet(page: Page, *, timeout_ms: int) -> None:
    """Require two matching non-busy DOM samples ~300ms apart, or hit the budget."""
    sample_gap_ms = 300
    needed = 2
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    last: dict[str, object] | None = None
    stable = 0
    while time.monotonic() < deadline:
        try:
            sample = await page.evaluate(_DOM_SAMPLE_JS)
        except PlaywrightError:
            return
        if not isinstance(sample, dict):
            return
        if last is not None and sample == last and not sample.get("busy"):
            stable += 1
            if stable >= needed:
                return
        else:
            stable = 0
        last = sample
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        try:
            await page.wait_for_timeout(min(sample_gap_ms, remaining_ms))
        except PlaywrightError:
            return


def _draw_marks(image: Image.Image, marks: list[Mark]) -> Image.Image:
    canvas = image.copy()
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 13)
    except OSError:
        font = ImageFont.load_default()

    for mark in marks:
        x, y, w, h = mark.box
        draw.rectangle([x, y, x + w, y + h], outline=(255, 0, 90), width=2)

        label = str(mark.number)
        left, top, right, bottom = draw.textbbox((0, 0), label, font=font)
        tw, th = right - left, bottom - top
        # Keep the tag inside the image even for elements flush against an edge.
        bx = min(max(x, 0), canvas.width - tw - 6)
        by = min(max(y - th - 5, 0), canvas.height - th - 6)
        draw.rectangle([bx, by, bx + tw + 6, by + th + 6], fill=(255, 0, 90))
        draw.text((bx + 3, by + 3), label, fill=(255, 255, 255), font=font)

    return canvas
