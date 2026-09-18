"""Refresh the agent's view of the live browser after human intervention.

After MFA approval or a Take-control handoff, the conversation history still
contains older page snapshots. Without an explicit live update, small models
often keep acting as if they were still on that earlier screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.async_api import Error as PlaywrightError

from .browser import BrowserError, BrowserSession
from .tools import VisionBuffer


@dataclass
class SceneCallback:
    """Capture the current browser page and queue it for the next model call."""

    session: BrowserSession
    vision: VisionBuffer
    _pending: str | None = field(default=None, init=False, repr=False)

    @property
    def pending(self) -> str | None:
        return self._pending

    def consume(self) -> str | None:
        text = self._pending
        self._pending = None
        return text

    async def refresh(self, reason: str) -> str:
        """Re-read the live page and attach a fresh screenshot for the model."""
        page = await self.session.page()
        # MFA and SSO often redirect after the human finishes; wait briefly.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=8_000)
        except PlaywrightError:
            pass
        try:
            await page.wait_for_timeout(800)
        except PlaywrightError:
            pass

        note = f"Live browser update after user intervention: {reason}"
        try:
            observation = await self.session.observation(note)
        except (BrowserError, PlaywrightError) as exc:
            observation = f"{note}\n\n(Could not read the page: {exc})"

        try:
            image_b64, _, path = await self.session.screenshot(
                marks=False,
                save_to=self.vision.next_path(),
            )
            self.vision.set(
                image_b64,
                "Current page after user intervention",
                path,
            )
        except (BrowserError, PlaywrightError):
            self.vision.clear()

        self._pending = (
            "The user finished intervening in the browser. "
            "Ignore earlier MFA/login page snapshots from this thread. "
            "Treat the following as the only current page state:\n\n"
            f"{observation}"
        )
        return observation
