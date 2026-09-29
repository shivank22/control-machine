"""The dashboard: chat with the agent, watch it work, review its history.

Everything runs in one uvicorn worker alongside the agent, which is what lets the SSE
endpoint read straight from the in-process event bus.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from ..browser import BrowserError, BrowserPool
from ..config import get_settings
from ..db import Database
from ..events import EventBus
from ..live import LiveSessions, open_live_session
from ..runner import RunManager, RunnerBusy
from ..scheduler import (
    SCHEDULE_PRESETS,
    AutomationScheduler,
    build_run_prompt,
    next_run_at,
    prompt_for_automation,
    public_automation,
    validate_cron,
)
from ..telegram import TelegramBridge
from ..tracing import configure_logging
from .live import router as live_router

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"


class ChatRequest(BaseModel):
    message: str
    task_id: int | None = None


class DecisionRequest(BaseModel):
    decision: str
    message: str | None = None


class SkillIn(BaseModel):
    name: str
    description: str = ""
    prompt: str


class SkillRunIn(BaseModel):
    extra: str = ""


class AutomationIn(BaseModel):
    name: str
    description: str = ""
    skill_id: int | None = None
    prompt: str = ""
    schedule: str
    enabled: bool = True


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)
    templates = Jinja2Templates(directory=str(TEMPLATES))

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = await Database.connect()
        orphans = await db.reconcile_orphans()
        if orphans:
            log.warning("Marked %s interrupted task(s) from a previous run as failed.", orphans)
        pool = BrowserPool.from_settings()
        bus = EventBus()
        live = LiveSessions(settings)
        live_http = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
        app.state.db = db
        app.state.pool = pool
        app.state.bus = bus
        app.state.live = live
        app.state.live_http = live_http
        app.state.runner = RunManager(db=db, pool=pool, bus=bus, settings=settings)

        async def _mint_live(task_id: int) -> str:
            session = await open_live_session(
                task_id,
                runner=app.state.runner,
                db=db,
                sessions=live,
            )
            return str(session["url"])

        app.state.runner.bind_live_opener(_mint_live)
        scheduler = AutomationScheduler(db=db, runner=app.state.runner)
        app.state.scheduler = scheduler
        telegram = TelegramBridge(
            settings=settings,
            db=db,
            runner=app.state.runner,
            bus=bus,
            live=live,
        )
        app.state.telegram = telegram
        scheduler.start()
        await telegram.start()
        try:
            yield
        finally:
            await telegram.stop()
            await scheduler.close()
            await app.state.runner.close()
            await pool.close()
            await live_http.aclose()
            await db.close()

    app = FastAPI(title="control-machine", lifespan=lifespan)
    app.include_router(live_router)
    app.mount("/runs", StaticFiles(directory=str(settings.runs_dir)), name="runs")

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "Unhandled error on %s %s",
            request.method,
            request.url.path,
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content={"detail": f"{type(exc).__name__}: {exc}"},
        )

    # ----------------------------------------------------------------- pages

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        conversations = await app.state.db.list_conversations()
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "groups": group_conversations(conversations),
                "model": settings.llm_model,
                "schedule_presets": SCHEDULE_PRESETS,
            },
        )

    @app.get("/partials/tasks", response_class=HTMLResponse)
    async def tasks_partial(request: Request) -> HTMLResponse:
        conversations = await app.state.db.list_conversations()
        return templates.TemplateResponse(
            request, "_tasks.html", {"groups": group_conversations(conversations)}
        )

    # ------------------------------------------------------------------- api

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        async def reachable(url: str) -> bool:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    return (await client.get(url)).status_code < 500
            except Exception:  # noqa: BLE001 - a down dependency is the answer
                return False

        async def llm_ready() -> bool:
            if settings.resolved_llm_provider == "openai":
                return settings.llm_configured
            return await reachable(f"{settings.ollama_base_url}/api/tags")

        slot_states = app.state.pool.status()
        chrome_checks, llm = await asyncio.gather(
            asyncio.gather(
                *(reachable(f"{slot['cdp_endpoint']}/json/version") for slot in slot_states)
            ),
            llm_ready(),
        )
        for slot, online in zip(slot_states, chrome_checks, strict=True):
            slot["online"] = online
        return {
            "chrome": all(chrome_checks),
            "slots": slot_states,
            "llm": llm,
            "ollama": llm,
            "provider": settings.resolved_llm_provider,
            "model": settings.llm_model,
            "busy": app.state.runner.busy,
            "active_task_ids": app.state.runner.active_task_ids,
        }

    @app.get("/api/slots")
    async def slots() -> list[dict[str, object]]:
        return app.state.pool.status()

    @app.get("/api/tasks")
    async def list_tasks() -> list[dict[str, Any]]:
        return await app.state.db.list_tasks()

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: int) -> dict[str, Any]:
        task = await app.state.db.get_task(task_id)
        if not task:
            raise HTTPException(404, f"No task {task_id}")
        run = app.state.runner.run_status(task_id)
        if run is None:
            run = app.state.runner.thread_status(task["thread_id"])
        return {
            "task": task,
            "thread_tasks": await app.state.db.list_thread_tasks(task["thread_id"]),
            "steps": await app.state.db.list_steps(task_id),
            "events": app.state.bus.history(task_id),
            "run": run,
        }

    @app.post("/api/chat")
    async def chat(payload: ChatRequest) -> JSONResponse:
        message = payload.message.strip()
        if not message:
            raise HTTPException(400, "Message is empty.")
        try:
            if payload.task_id:
                task = await app.state.runner.follow_up(payload.task_id, message)
            else:
                task = await app.state.runner.start(message)
        except RunnerBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except BrowserError as exc:
            raise HTTPException(503, str(exc)) from exc
        return JSONResponse(task)

    @app.post("/api/tasks/{task_id}/decide")
    async def decide(task_id: int, payload: DecisionRequest) -> JSONResponse:
        decision: dict[str, Any] = {"type": payload.decision}
        if payload.decision in {"reject", "respond"} and payload.message:
            decision["message"] = payload.message
        try:
            await app.state.runner.decide(task_id, [decision])
        except (RunnerBusy, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.post("/api/tasks/{task_id}/pause")
    async def pause(task_id: int) -> JSONResponse:
        try:
            await app.state.runner.pause(task_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.post("/api/tasks/{task_id}/resume")
    async def resume(task_id: int) -> JSONResponse:
        try:
            await app.state.runner.resume(task_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: int) -> JSONResponse:
        try:
            await app.state.runner.cancel(task_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return JSONResponse({"ok": True})

    @app.get("/api/schedule-presets")
    async def schedule_presets() -> list[dict[str, str]]:
        return [{"cron": cron, "label": label} for cron, label in SCHEDULE_PRESETS.items()]

    @app.get("/api/skills")
    async def list_skills() -> list[dict[str, Any]]:
        return await app.state.db.list_skills()

    @app.post("/api/skills")
    async def create_skill(payload: SkillIn) -> JSONResponse:
        name, description, prompt = _skill_fields(payload)
        skill = await app.state.db.create_skill(name=name, description=description, prompt=prompt)
        return JSONResponse(skill)

    @app.put("/api/skills/{skill_id}")
    async def update_skill(skill_id: int, payload: SkillIn) -> JSONResponse:
        name, description, prompt = _skill_fields(payload)
        skill = await app.state.db.update_skill(
            skill_id, name=name, description=description, prompt=prompt
        )
        if not skill:
            raise HTTPException(404, f"No skill {skill_id}")
        return JSONResponse(skill)

    @app.delete("/api/skills/{skill_id}")
    async def delete_skill(skill_id: int) -> JSONResponse:
        if not await app.state.db.delete_skill(skill_id):
            raise HTTPException(404, f"No skill {skill_id}")
        return JSONResponse({"ok": True})

    @app.post("/api/skills/{skill_id}/run")
    async def run_skill(skill_id: int, payload: SkillRunIn) -> JSONResponse:
        skill = await app.state.db.get_skill(skill_id)
        if not skill:
            raise HTTPException(404, f"No skill {skill_id}")
        try:
            prompt = build_run_prompt(skill=skill, extra=payload.extra)
            task = await app.state.runner.start(
                prompt, title=skill["name"], skill_id=skill_id
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RunnerBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except BrowserError as exc:
            raise HTTPException(503, str(exc)) from exc
        return JSONResponse(task)

    @app.get("/api/automations")
    async def list_automations() -> list[dict[str, Any]]:
        return [public_automation(row) for row in await app.state.db.list_automations()]

    @app.post("/api/automations")
    async def create_automation(payload: AutomationIn) -> JSONResponse:
        fields = await _automation_fields(app.state.db, payload)
        automation = await app.state.db.create_automation(**fields)
        return JSONResponse(public_automation(automation))

    @app.put("/api/automations/{automation_id}")
    async def update_automation(automation_id: int, payload: AutomationIn) -> JSONResponse:
        fields = await _automation_fields(app.state.db, payload)
        automation = await app.state.db.update_automation(automation_id, **fields)
        if not automation:
            raise HTTPException(404, f"No automation {automation_id}")
        return JSONResponse(public_automation(automation))

    @app.delete("/api/automations/{automation_id}")
    async def delete_automation(automation_id: int) -> JSONResponse:
        if not await app.state.db.delete_automation(automation_id):
            raise HTTPException(404, f"No automation {automation_id}")
        return JSONResponse({"ok": True})

    @app.post("/api/automations/{automation_id}/run")
    async def run_automation(automation_id: int) -> JSONResponse:
        automation = await app.state.db.get_automation(automation_id)
        if not automation:
            raise HTTPException(404, f"No automation {automation_id}")
        if app.state.runner.automation_is_running(automation_id):
            raise HTTPException(409, "This automation is already running.")
        last_task = (
            await app.state.db.get_task(int(automation["last_task_id"]))
            if automation.get("last_task_id")
            else None
        )
        try:
            prompt = prompt_for_automation(automation)
            task = await app.state.runner.start(
                prompt,
                title=automation["name"],
                skill_id=automation.get("skill_id"),
                automation_id=automation_id,
                preferred_slot_id=last_task.get("slot_id") if last_task else None,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RunnerBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except BrowserError as exc:
            raise HTTPException(503, str(exc)) from exc
        await app.state.db.mark_automation_ran(
            automation_id,
            task_id=int(task["id"]),
            next_run=next_run_at(automation["schedule"]),
        )
        return JSONResponse(task)

    @app.get("/api/stream/{task_id}")
    async def stream(request: Request, task_id: int) -> EventSourceResponse:
        bus: EventBus = app.state.bus

        async def publisher() -> AsyncIterator[dict[str, str]]:
            queue = bus.subscribe(task_id)
            try:
                # Replay first so a late or reconnecting client is not missing steps.
                for event in bus.history(task_id):
                    yield {"event": "agent", "data": _dump(event)}
                # History is done. The client can now show live typing without
                # flashing an indicator while a finished run is replayed.
                yield {"event": "agent", "data": _dump({"type": "caught_up"})}
                typing = bus.latest_typing(task_id)
                if typing:
                    yield {"event": "agent", "data": _dump(typing)}
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        yield {"event": "ping", "data": "{}"}
                        continue
                    yield {"event": "agent", "data": _dump(event)}
            finally:
                bus.unsubscribe(task_id, queue)

        return EventSourceResponse(publisher())

    return app


_ACTIVE_STATUSES = {"running", "paused", "awaiting_approval"}


ConversationGroup = tuple[str, list[dict[str, Any]]]


def group_conversations(conversations: list[dict[str, Any]]) -> list[ConversationGroup]:
    """Bucket threads for the sidebar: live work first, then recency."""
    now = datetime.now().astimezone()
    today = now.date()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)
    buckets: dict[str, list[dict[str, Any]]] = {
        "Active": [],
        "Today": [],
        "Yesterday": [],
        "This week": [],
        "Older": [],
    }
    for conv in conversations:
        if conv.get("status") in _ACTIVE_STATUSES:
            buckets["Active"].append(conv)
            continue
        raw = conv.get("created_at") or ""
        try:
            created = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            day = created.astimezone(now.tzinfo).date()
        except ValueError:
            buckets["Older"].append(conv)
            continue
        if day == today:
            buckets["Today"].append(conv)
        elif day == yesterday:
            buckets["Yesterday"].append(conv)
        elif day > week_ago:
            buckets["This week"].append(conv)
        else:
            buckets["Older"].append(conv)
    return [(label, items) for label, items in buckets.items() if items]


def _dump(event: dict[str, Any]) -> str:
    return json.dumps(event, default=str)


def _skill_fields(payload: SkillIn) -> tuple[str, str, str]:
    name = payload.name.strip()
    prompt = payload.prompt.strip()
    if not name:
        raise HTTPException(400, "Name is required.")
    if not prompt:
        raise HTTPException(400, "Instructions are required.")
    return name, payload.description.strip(), prompt


async def _automation_fields(db: Database, payload: AutomationIn) -> dict[str, Any]:
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "Name is required.")
    extra = payload.prompt.strip()
    skill_id = payload.skill_id
    if skill_id is not None:
        skill = await db.get_skill(skill_id)
        if not skill:
            raise HTTPException(400, f"No skill {skill_id}")
    if skill_id is None and not extra:
        raise HTTPException(400, "Pick a skill or write instructions.")
    try:
        schedule = validate_cron(payload.schedule)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "name": name,
        "description": payload.description.strip(),
        "skill_id": skill_id,
        "prompt": extra,
        "schedule": schedule,
        "enabled": payload.enabled,
        "next_run_at": next_run_at(schedule) if payload.enabled else None,
    }
