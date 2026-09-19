"""Cron helpers and the loop that fires due automations.

Schedules use the machine's local timezone. A missed slot (every browser busy) is
retried on the next tick rather than skipped until the following cron occurrence.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime
from typing import Any

from croniter import croniter

from .db import Database
from .runner import RunManager, RunnerBusy

log = logging.getLogger(__name__)

SCHEDULE_PRESETS: dict[str, str] = {
    "*/15 * * * *": "Every 15 minutes",
    "0 * * * *": "Every hour",
    "0 */6 * * *": "Every 6 hours",
    "0 9 * * *": "Daily at 09:00",
    "0 9 * * 1-5": "Weekdays at 09:00",
    "0 9 * * 1": "Mondays at 09:00",
}

TICK_SECONDS = 10


def validate_cron(expr: str) -> str:
    expr = expr.strip()
    if not expr:
        raise ValueError("Schedule is empty.")
    try:
        croniter(expr)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"Invalid cron expression: {expr}") from exc
    return expr


def next_run_at(expr: str, after: datetime | None = None) -> datetime:
    base = after or datetime.now().astimezone()
    if base.tzinfo is None:
        base = base.astimezone()
    return croniter(validate_cron(expr), base).get_next(datetime)


def schedule_label(expr: str) -> str:
    return SCHEDULE_PRESETS.get(expr.strip(), expr.strip())


def build_run_prompt(*, skill: dict[str, Any] | None = None, extra: str = "") -> str:
    """Compose the user message for a skill or automation run."""
    parts: list[str] = []
    if skill and (skill.get("prompt") or "").strip():
        parts.append(str(skill["prompt"]).strip())
    if extra.strip():
        parts.append(extra.strip())
    prompt = "\n\n".join(parts)
    if not prompt:
        raise ValueError("Add a skill or some instructions before running.")
    return prompt


def prompt_for_automation(row: dict[str, Any]) -> str:
    skill = None
    if (row.get("skill_prompt") or "").strip():
        skill = {"prompt": row["skill_prompt"]}
    return build_run_prompt(skill=skill, extra=row.get("prompt") or "")


def public_automation(row: dict[str, Any]) -> dict[str, Any]:
    return {**row, "schedule_label": schedule_label(row.get("schedule") or "")}


class AutomationScheduler:
    """Polls Postgres for due automations and starts the browser agent."""

    def __init__(self, *, db: Database, runner: RunManager) -> None:
        self.db = db
        self.runner = runner
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="automation-scheduler")

    async def close(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("automation scheduler tick failed")

    async def tick(self) -> None:
        for automation in await self.db.due_automations():
            await self._fire(automation)

    async def _fire(self, automation: dict[str, Any]) -> None:
        automation_id = int(automation["id"])
        if self.runner.automation_is_running(automation_id):
            return
        try:
            prompt = prompt_for_automation(automation)
        except ValueError as exc:
            await self.db.set_automation_error(automation_id, str(exc), disable=True)
            return

        last_task = (
            await self.db.get_task(int(automation["last_task_id"]))
            if automation.get("last_task_id")
            else None
        )
        preferred_slot = last_task.get("slot_id") if last_task else None
        try:
            task = await self.runner.start(
                prompt,
                title=automation["name"],
                skill_id=automation.get("skill_id"),
                automation_id=automation_id,
                preferred_slot_id=preferred_slot,
            )
        except RunnerBusy:
            await self.db.set_automation_error(
                automation_id,
                "All browser slots are in use. Will retry shortly.",
            )
            return
        except Exception as exc:  # noqa: BLE001 - recorded on the automation
            log.exception("Could not start automation %s", automation_id)
            await self.db.set_automation_error(
                automation_id, f"Could not start: {type(exc).__name__}: {exc}"
            )
            return

        await self.db.mark_automation_ran(
            automation_id,
            task_id=int(task["id"]),
            next_run=next_run_at(automation["schedule"]),
        )
