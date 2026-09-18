"""Run concurrent agents, each with an isolated browser slot."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from .agent import build_agent
from .browser import BrowserPool, BrowserSlot
from .config import Settings, get_settings
from .db import Database
from .events import EventBus
from .scene import SceneCallback
from .tools import VisionBuffer

_SCREENSHOT_RE = re.compile(r"\[screenshot: ([^\]]+)\]")


class RunnerBusy(RuntimeError):
    """Raised when every configured browser slot is occupied."""


@dataclass
class Run:
    task: dict[str, Any]
    slot: BrowserSlot
    vision: VisionBuffer
    scene: SceneCallback
    agent: Any
    running: asyncio.Event
    job: asyncio.Task[None] | None = None


@dataclass
class StickyLease:
    thread_id: str
    slot: BrowserSlot
    touched_at: float
    expiry: asyncio.Task[None] | None = None


class RunManager:
    def __init__(
        self,
        *,
        db: Database,
        pool: BrowserPool,
        bus: EventBus,
        settings: Settings | None = None,
    ) -> None:
        self.db = db
        self.pool = pool
        self.bus = bus
        self.settings = settings or get_settings()
        self._runs: dict[int, Run] = {}
        self._sticky: dict[str, StickyLease] = {}
        self._last_typing: dict[int, tuple[str, str, str]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ state

    @property
    def busy(self) -> bool:
        return len(self._runs) >= self.settings.effective_parallel_runs

    @property
    def active_task_ids(self) -> list[int]:
        return sorted(self._runs)

    def run_status(self, task_id: int) -> dict[str, Any] | None:
        run = self._runs.get(task_id)
        if run is None:
            return None
        return {
            "task_id": task_id,
            "slot_id": run.slot.id,
            "novnc_url": run.slot.novnc_url,
            "paused": not run.running.is_set(),
            "executing": run.job is not None and not run.job.done(),
        }

    def thread_status(self, thread_id: str) -> dict[str, Any] | None:
        lease = self._sticky.get(thread_id)
        if lease is None:
            return None
        return {
            "task_id": None,
            "slot_id": lease.slot.id,
            "novnc_url": lease.slot.novnc_url,
            "paused": False,
            "executing": False,
            "idle": True,
        }

    # ----------------------------------------------------------------- control

    def automation_is_running(self, automation_id: int) -> bool:
        return any(run.task.get("automation_id") == automation_id for run in self._runs.values())

    async def start(
        self,
        prompt: str,
        *,
        title: str | None = None,
        skill_id: int | None = None,
        automation_id: int | None = None,
        preferred_slot_id: int | None = None,
    ) -> dict[str, Any]:
        return await self._start_new(
            thread_id=f"thread-{uuid.uuid4().hex[:12]}",
            prompt=prompt,
            title=title,
            skill_id=skill_id,
            automation_id=automation_id,
            preferred_slot_id=preferred_slot_id,
        )

    async def follow_up(self, task_id: int, prompt: str) -> dict[str, Any]:
        """Continue an existing thread, so the agent keeps its context and memory."""
        previous = await self.db.get_task(task_id)
        if not previous:
            raise ValueError(f"No task {task_id}")
        return await self._start_new(
            thread_id=previous["thread_id"],
            prompt=prompt,
            preferred_slot_id=previous.get("slot_id"),
            continue_browser=True,
        )

    async def _start_new(
        self,
        *,
        thread_id: str,
        prompt: str,
        title: str | None = None,
        skill_id: int | None = None,
        automation_id: int | None = None,
        preferred_slot_id: int | None = None,
        continue_browser: bool = False,
    ) -> dict[str, Any]:
        async with self._lock:
            if any(run.task["thread_id"] == thread_id for run in self._runs.values()):
                raise RunnerBusy(
                    "This conversation already has an active run. "
                    "Wait for it to finish before sending a follow-up."
                )
            sticky = self._claim_sticky(thread_id)
            if sticky is None and not self.pool.has_available:
                await self._evict_oldest_sticky()
            if sticky is None and not self.pool.has_available:
                raise RunnerBusy("All browser slots are in use. Stop a run or wait for one.")
            task = await self.db.create_task(
                thread_id=thread_id,
                title=title or _title_for(prompt),
                prompt=prompt,
                skill_id=skill_id,
                automation_id=automation_id,
            )
            try:
                run = await self._make_run(
                    task,
                    slot=sticky,
                    preferred_slot_id=preferred_slot_id,
                )
            except Exception as exc:
                await self.db.finish_task(
                    task["id"], status="error", error=f"Browser unavailable: {exc}"
                )
                raise
            if continue_browser or sticky is not None:
                await self._refresh_scene(
                    run,
                    "Continuing this conversation in the live browser.",
                )
            self._launch(run, {"messages": [{"role": "user", "content": prompt}]})
            return task

    async def decide(self, task_id: int, decisions: list[dict[str, Any]]) -> None:
        """Answer a human-in-the-loop interrupt and let the run continue."""
        async with self._lock:
            run = self._runs.get(task_id)
            if run is None:
                if not self.pool.has_available:
                    await self._evict_oldest_sticky()
                if not self.pool.has_available:
                    raise RunnerBusy("All browser slots are in use.")
                task = await self.db.get_task(task_id)
                if not task:
                    raise ValueError(f"No task {task_id}")
                if task["status"] != "awaiting_approval":
                    raise ValueError(f"Task {task_id} is not waiting for a decision.")
                run = await self._make_run(
                    task,
                    preferred_slot_id=task.get("slot_id"),
                )
            if run.job is not None and not run.job.done():
                raise RunnerBusy(f"Task {task_id} is already running.")
            decisions = await self._with_live_scene(run, decisions)
            run.running.set()
            await self.db.set_task_status(task_id, "running")
            self._launch(run, Command(resume={"decisions": decisions}), fresh=False)

    async def pause(self, task_id: int) -> None:
        run = self._require_run(task_id)
        if run.job is None or run.job.done():
            raise ValueError(f"Task {task_id} is not actively running.")
        run.running.clear()
        await self.db.set_task_status(task_id, "paused")
        self._emit(task_id, {"type": "status", "status": "paused"})
        self._emit_typing(task_id, "⏸️", "Paused")

    async def resume(self, task_id: int) -> None:
        run = self._require_run(task_id)
        if run.running.is_set():
            return
        await self._refresh_scene(run, "User finished controlling the browser.")
        run.running.set()
        await self.db.set_task_status(task_id, "running")
        self._emit(task_id, {"type": "status", "status": "running"})
        self._emit_typing(task_id, "✍️", "Thinking")

    async def cancel(self, task_id: int) -> None:
        run = self._require_run(task_id)
        if run.job is not None and not run.job.done():
            run.job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run.job
            return
        await self.db.finish_task(task_id, status="cancelled", error="Cancelled.")
        self._emit(task_id, {"type": "status", "status": "cancelled"})
        await self._release(run, preserve=False)

    async def close(self) -> None:
        for task_id in list(self._runs):
            with contextlib.suppress(ValueError):
                await self.cancel(task_id)
        async with self._lock:
            leases = list(self._sticky.values())
            self._sticky.clear()
            for lease in leases:
                if lease.expiry is not None:
                    lease.expiry.cancel()
                await self.pool.release(lease.slot)

    async def _make_run(
        self,
        task: dict[str, Any],
        *,
        slot: BrowserSlot | None = None,
        preferred_slot_id: int | None = None,
    ) -> Run:
        task_id = int(task["id"])
        if slot is None:
            slot = await self.pool.acquire(task_id, preferred_id=preferred_slot_id)
        else:
            self.pool.activate_held(slot, task_id)
        try:
            vision = VisionBuffer(run_dir=self.settings.runs_dir / str(task_id))
            scene = SceneCallback(session=slot.session, vision=vision)
            running = asyncio.Event()
            running.set()
            agent = build_agent(
                session=slot.session,
                vision=vision,
                scene=scene,
                checkpointer=self.db.checkpointer,
                store=self.db.store,
                running=running,
                settings=self.settings,
            )
        except Exception:
            await self.pool.release(slot)
            raise
        await self.db.set_task_slot(task_id, slot.id)
        task["slot_id"] = slot.id
        run = Run(
            task=task,
            slot=slot,
            vision=vision,
            scene=scene,
            agent=agent,
            running=running,
        )
        self._runs[task_id] = run
        self._emit(
            task_id,
            {"type": "slot", "slot_id": slot.id, "novnc_url": slot.novnc_url},
        )
        return run

    def _require_run(self, task_id: int) -> Run:
        run = self._runs.get(task_id)
        if run is None:
            raise ValueError(f"Task {task_id} has no active browser session.")
        return run

    def _launch(self, run: Run, payload: Any, *, fresh: bool = True) -> None:
        if fresh:
            run.vision.clear()
        run.job = asyncio.create_task(self._run(run, payload))

    # -------------------------------------------------------------------- loop

    async def _run(self, run: Run, payload: Any) -> None:
        task_id = int(run.task["id"])
        config = {
            "configurable": {"thread_id": run.task["thread_id"]},
            # Two graph steps per tool call, plus headroom for planning turns.
            "recursion_limit": max(10, self.settings.max_steps * 2),
        }
        self._emit(task_id, {"type": "status", "status": "running"})
        self._emit_typing(task_id, "✍️", "Thinking")
        terminal = True
        preserve = True

        try:
            interrupted = await self._consume_with_active_timeout(run, payload, config)
            if interrupted:
                terminal = False
        except TimeoutError:
            await self._fail(task_id, f"Stopped after {self.settings.run_timeout_seconds}s.")
        except asyncio.CancelledError:
            preserve = False
            await self.db.finish_task(task_id, status="cancelled", error="Cancelled.")
            self._emit(task_id, {"type": "status", "status": "cancelled"})
            raise
        except Exception as exc:  # noqa: BLE001 - the dashboard shows the message
            await self._fail(task_id, f"{type(exc).__name__}: {exc}")
        finally:
            if terminal:
                await self._release(run, preserve=preserve)

    async def _consume_with_active_timeout(
        self, run: Run, payload: Any, config: dict[str, Any]
    ) -> bool:
        """Apply the timeout only while the agent is not paused by the user."""
        work = asyncio.create_task(self._consume(run, payload, config))
        remaining = float(self.settings.run_timeout_seconds)
        loop = asyncio.get_running_loop()
        previous = loop.time()
        try:
            while True:
                done, _ = await asyncio.wait({work}, timeout=min(0.5, remaining))
                now = loop.time()
                if run.running.is_set():
                    remaining -= now - previous
                previous = now
                if work in done:
                    return work.result()
                if remaining <= 0:
                    raise TimeoutError
        finally:
            if not work.done():
                work.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await work

    async def _consume(self, run: Run, payload: Any, config: dict[str, Any]) -> bool:
        task_id = int(run.task["id"])
        pending_calls: dict[str, Any] = {}
        final_text = ""
        tool_steps = 0
        interrupted = False

        async for mode, chunk in run.agent.astream(
            payload, config, stream_mode=["updates", "messages", "tasks"]
        ):
            if mode == "tasks":
                self._on_task_event(task_id, chunk, pending_calls)
                continue

            if mode == "messages":
                message, meta = chunk
                text = getattr(message, "text", None)
                node = str(meta.get("langgraph_node") or "")
                if isinstance(text, str) and text and node == "model":
                    self._emit_typing(task_id, "⌨️", "Writing a reply")
                    self._emit(task_id, {"type": "token", "text": text})
                continue

            for node, update in (chunk or {}).items():
                if node == "__interrupt__":
                    interrupted = True
                    await self._interrupt(task_id, update)
                    continue
                if not isinstance(update, dict):
                    continue

                if update.get("todos"):
                    self._emit(task_id, {"type": "todos", "todos": update["todos"]})

                for message in update.get("messages", []) or []:
                    if isinstance(message, AIMessage):
                        for call in message.tool_calls or []:
                            pending_calls[_tool_call_id(call)] = call
                        if message.tool_calls:
                            name, args = _tool_call_parts(message.tool_calls[0])
                            self._emit_tool_activity(task_id, name, args)
                        text = _plain_text(message)
                        if text and not message.tool_calls:
                            final_text = text
                            self._emit(
                                task_id, {"type": "message", "role": "assistant", "text": text}
                            )

                    elif isinstance(message, ToolMessage):
                        tool_steps += 1
                        call = pending_calls.pop(message.tool_call_id, {})
                        await self._record(task_id, call, message)
                        self._emit_pending_or_thinking(task_id, pending_calls)

                        if tool_steps >= self.settings.max_steps:
                            await self._fail(
                                task_id,
                                f"Stopped after {tool_steps} actions (step budget reached).",
                            )
                            return False

        if interrupted:
            return True

        await self.db.finish_task(task_id, status="done", result=final_text or None)
        self._emit(task_id, {"type": "status", "status": "done", "result": final_text})
        return False

    # ------------------------------------------------------------------ pieces

    async def _refresh_scene(self, run: Run, reason: str) -> str:
        task_id = int(run.task["id"])
        self._emit_tool_activity(task_id, "scene_refresh", {"reason": reason})
        observation = await run.scene.refresh(reason)
        screenshot = None
        if run.vision.path is not None:
            try:
                screenshot = run.vision.path.relative_to(self.settings.runs_dir).as_posix()
            except ValueError:
                screenshot = run.vision.path.name
        step = await self.db.add_step(
            task_id,
            kind="scene",
            tool_name="scene_refresh",
            args={"reason": reason},
            result_summary=_summarise(observation),
            screenshot_path=screenshot,
        )
        self._emit(task_id, {"type": "step", "step": step})
        return observation

    async def _with_live_scene(
        self,
        run: Run,
        decisions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Attach a fresh page observation when the user answers an interrupt."""
        reason = next(
            (
                str(decision.get("message") or "").strip()
                for decision in decisions
                if decision.get("type") == "respond" and decision.get("message")
            ),
            "User answered the interrupt.",
        )
        observation = await self._refresh_scene(run, reason)
        updated: list[dict[str, Any]] = []
        for decision in decisions:
            item = dict(decision)
            if item.get("type") == "respond":
                user_text = str(item.get("message") or "").strip() or "Done."
                item["message"] = (
                    f"{user_text}\n\n"
                    "Ignore earlier MFA/login page snapshots from this thread. "
                    "The live browser is now:\n"
                    f"{observation}"
                )
            updated.append(item)
        return updated

    async def _record(self, task_id: int, call: dict[str, Any], message: ToolMessage) -> None:
        content = _plain_text(message)
        match = _SCREENSHOT_RE.search(content)
        screenshot = None
        if match:
            content = _SCREENSHOT_RE.sub("", content).strip()
            # Store it relative to the runs directory so the dashboard can serve it
            # from /runs/... regardless of where the project lives on disk.
            candidate = Path(match.group(1))
            try:
                screenshot = candidate.relative_to(self.settings.runs_dir).as_posix()
            except ValueError:
                screenshot = candidate.name

        name, args = _tool_call_parts(call)
        step = await self.db.add_step(
            task_id,
            kind="tool",
            tool_name=name or message.name,
            args=args,
            result_summary=_summarise(content),
            screenshot_path=screenshot,
        )
        self._emit(task_id, {"type": "step", "step": step})

    async def _interrupt(self, task_id: int, payload: Any) -> None:
        requests: list[dict[str, Any]] = []
        for item in payload if isinstance(payload, (list, tuple)) else [payload]:
            value = getattr(item, "value", item)
            if isinstance(value, dict):
                requests.extend(value.get("action_requests", []))

        question = None
        if requests and requests[0].get("name") == "ask_user":
            question = requests[0].get("args", {}).get("question")
        await self.db.set_task_status(task_id, "awaiting_approval")
        await self.db.add_step(
            task_id,
            kind="interrupt",
            tool_name=requests[0]["name"] if requests else None,
            args={"action_requests": requests},
            result_summary=question or "Waiting for your approval.",
        )
        self._emit(
            task_id,
            {"type": "interrupt", "requests": requests, "question": question},
        )
        self._emit(task_id, {"type": "status", "status": "awaiting_approval"})

    async def _fail(self, task_id: int, message: str) -> None:
        await self.db.finish_task(task_id, status="error", error=message)
        self._emit(task_id, {"type": "status", "status": "error", "error": message})

    def _on_task_event(
        self, task_id: int, chunk: Any, pending_calls: dict[str, Any]
    ) -> None:
        """LangGraph `tasks` stream: a node started. Skip completions and noisy hooks."""
        if not isinstance(chunk, dict) or "result" in chunk:
            return
        name = str(chunk.get("name") or "")
        if not name or "." in name:
            return
        if name == "tools":
            if pending_calls:
                self._emit_pending_or_thinking(task_id, pending_calls)
                return
            extracted = _calls_from_task_messages(chunk.get("input"))
            if extracted:
                tool_name, args = extracted[0]
                self._emit_tool_activity(task_id, tool_name, args)
                return
            self._emit_typing(task_id, "🛠️", "Using a tool")
            return
        if name in _TOOL_ACTIVITY or name.startswith("browser_"):
            args = _args_from_task_input(chunk.get("input"))
            if not args and pending_calls:
                _, args = _tool_call_parts(next(iter(pending_calls.values())))
            self._emit_tool_activity(task_id, name, args)
            return
        emoji, label = _activity_for_node(name)
        self._emit_typing(task_id, emoji, label)

    def _emit_pending_or_thinking(
        self,
        task_id: int,
        pending_calls: dict[str, Any],
        *,
        fallback: str | None = None,
    ) -> None:
        if pending_calls:
            name, args = _tool_call_parts(next(iter(pending_calls.values())))
            self._emit_tool_activity(task_id, name, args)
            return
        if fallback:
            self._emit_typing(task_id, "🛠️", fallback)
            return
        self._emit_typing(task_id, "✍️", "Thinking")

    def _emit_tool_activity(self, task_id: int, name: str, args: dict[str, Any] | None) -> None:
        emoji, label = _activity_for_tool(name, args)
        self._emit_typing(task_id, emoji, label, tool=name)

    def _emit_typing(
        self, task_id: int, emoji: str, label: str, *, tool: str | None = None
    ) -> None:
        key = (emoji, label, tool or "")
        if self._last_typing.get(task_id) == key:
            return
        self._last_typing[task_id] = key
        event: dict[str, Any] = {"type": "typing", "emoji": emoji, "label": label}
        if tool:
            event["tool"] = tool
        self._emit(task_id, event)

    def _emit(self, task_id: int, event: dict[str, Any]) -> None:
        if event.get("type") == "status" and event.get("status") in {
            "done",
            "error",
            "cancelled",
            "awaiting_approval",
        }:
            self._last_typing.pop(task_id, None)
        self.bus.publish(task_id, event)

    def _claim_sticky(self, thread_id: str) -> BrowserSlot | None:
        lease = self._sticky.pop(thread_id, None)
        if lease is None:
            return None
        if lease.expiry is not None:
            lease.expiry.cancel()
        return lease.slot

    async def _evict_oldest_sticky(self) -> None:
        if not self._sticky:
            return
        thread_id, lease = min(
            self._sticky.items(),
            key=lambda item: item[1].touched_at,
        )
        self._sticky.pop(thread_id, None)
        if lease.expiry is not None:
            lease.expiry.cancel()
        await self.pool.release(lease.slot)

    def _hold_sticky(self, run: Run) -> None:
        thread_id = str(run.task["thread_id"])
        lease = StickyLease(
            thread_id=thread_id,
            slot=run.slot,
            touched_at=time.monotonic(),
        )
        self.pool.hold(run.slot, thread_id)
        self._sticky[thread_id] = lease
        lease.expiry = asyncio.create_task(self._expire_sticky(lease))

    async def _expire_sticky(self, lease: StickyLease) -> None:
        try:
            await asyncio.sleep(self.settings.browser_session_idle_seconds)
            async with self._lock:
                if self._sticky.get(lease.thread_id) is not lease:
                    return
                self._sticky.pop(lease.thread_id, None)
                await self.pool.release(lease.slot)
        except asyncio.CancelledError:
            return

    async def _release(self, run: Run, *, preserve: bool) -> None:
        task_id = int(run.task["id"])
        async with self._lock:
            if self._runs.get(task_id) is not run:
                return
            self._runs.pop(task_id, None)
            self._last_typing.pop(task_id, None)
            if preserve:
                self._hold_sticky(run)
            else:
                await self.pool.release(run.slot)


def _plain_text(message: Any) -> str:
    """Flatten message content, which may be a string or a list of blocks."""
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(p for p in parts if p).strip()


def _summarise(text: str, limit: int = 400) -> str:
    """Tool results carry a whole page snapshot; the timeline only needs the gist."""
    head = text.split("\n\npage:", 1)[0].strip() or text.strip()
    head = " ".join(head.split())
    return head[:limit] + ("..." if len(head) > limit else "")


def _title_for(prompt: str) -> str:
    title = " ".join(prompt.split())
    return title[:70] + ("..." if len(title) > 70 else "")


_NODE_ACTIVITY: dict[str, tuple[str, str]] = {
    "model": ("✍️", "Thinking"),
}

_TOOL_ACTIVITY: dict[str, tuple[str, str]] = {
    "browser_navigate": ("🌐", "Opening a page"),
    "browser_snapshot": ("👀", "Reading page elements"),
    "browser_click": ("🖱️", "Clicking"),
    "browser_click_xy": ("🖱️", "Clicking"),
    "browser_type": ("⌨️", "Typing into the page"),
    "browser_select_option": ("📋", "Choosing an option"),
    "browser_press_key": ("⌨️", "Pressing a key"),
    "browser_scroll": ("↕️", "Scrolling"),
    "browser_wait_for": ("⏳", "Waiting"),
    "browser_read_page": ("📄", "Reading the page"),
    "browser_screenshot": ("📸", "Taking a screenshot"),
    "ask_user": ("🙋", "Asking you"),
    "write_file": ("📁", "Writing a file"),
    "read_file": ("📁", "Reading a file"),
    "edit_file": ("📁", "Editing a file"),
    "ls": ("📁", "Listing files"),
    "glob": ("📁", "Finding files"),
    "grep": ("📁", "Searching files"),
    "scene_refresh": ("👀", "Looking at the page"),
}


def _activity_for_node(name: str) -> tuple[str, str]:
    if name in _NODE_ACTIVITY:
        return _NODE_ACTIVITY[name]
    pretty = name.replace("_", " ").strip() or "Working"
    return "⏳", pretty[:48]


def _activity_for_tool(name: str, args: dict[str, Any] | None = None) -> tuple[str, str]:
    args = args or {}
    emoji, fallback = _TOOL_ACTIVITY.get(name, ("🛠️", (name or "tool").replace("_", " ")))

    if name == "browser_navigate":
        url = str(args.get("url") or "").strip()
        target = _host(url) or url
        return emoji, f"Opening {_clip(target)}" if target else fallback
    if name in {"browser_click", "browser_click_xy"}:
        target = str(args.get("element") or args.get("ref") or "").strip()
        if target:
            return emoji, f"Clicking {_clip(target)}"
        x, y = args.get("x"), args.get("y")
        if x is not None and y is not None:
            return emoji, f"Clicking ({_number(x)}, {_number(y)})"
        return emoji, fallback
    if name == "browser_type":
        target = str(args.get("element") or args.get("ref") or "").strip()
        typed = _clip(str(args.get("text") or ""), 28)
        if target and typed:
            return emoji, f"Typing “{typed}” into {_clip(target, 24)}"
        if target:
            return emoji, f"Typing into {_clip(target)}"
        if typed:
            return emoji, f"Typing “{typed}”"
        return emoji, fallback
    if name == "browser_select_option":
        value = str(args.get("value") or "").strip()
        return emoji, f"Choosing {_clip(value)}" if value else fallback
    if name == "browser_press_key":
        key = str(args.get("key") or "").strip()
        return emoji, f"Pressing {key}" if key else fallback
    if name == "browser_scroll":
        direction = str(args.get("direction") or "down").strip()
        return emoji, f"Scrolling {direction}"
    if name == "browser_wait_for":
        text = str(args.get("text") or "").strip()
        seconds = args.get("seconds")
        if text:
            return emoji, f"Waiting for {_clip(text, 32)}"
        if seconds is not None:
            return emoji, f"Waiting {seconds}s"
        return emoji, fallback
    if name == "browser_screenshot":
        reason = str(args.get("reason") or "").strip()
        return emoji, f"Screenshot: {_clip(reason)}" if reason else fallback
    if name == "ask_user":
        question = str(args.get("question") or "").strip()
        return emoji, f"Asking: {_clip(question)}" if question else fallback
    if name in {"write_file", "read_file", "edit_file"}:
        path = str(args.get("file_path") or args.get("path") or "").strip()
        verb = {"write_file": "Writing", "read_file": "Reading", "edit_file": "Editing"}[name]
        return emoji, f"{verb} {_clip(path)}" if path else fallback
    if name == "ls":
        path = str(args.get("path") or "").strip()
        return emoji, f"Listing {_clip(path)}" if path else fallback
    if name == "glob":
        pattern = str(args.get("pattern") or args.get("glob") or "").strip()
        return emoji, f"Finding {_clip(pattern)}" if pattern else fallback
    if name == "grep":
        pattern = str(args.get("pattern") or args.get("query") or "").strip()
        return emoji, f"Searching {_clip(pattern)}" if pattern else fallback
    if name == "scene_refresh":
        reason = str(args.get("reason") or "").strip()
        return emoji, f"Refreshing view: {_clip(reason)}" if reason else fallback

    gist = _args_gist(args)
    if gist:
        return emoji, f"{fallback}: {gist}"
    return emoji, fallback


def _tool_call_id(call: Any) -> str:
    if isinstance(call, dict):
        return str(call.get("id") or "")
    return str(getattr(call, "id", "") or "")


def _tool_call_parts(call: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(call, dict):
        name = str(call.get("name") or "tool")
        args = call.get("args") or {}
    else:
        name = str(getattr(call, "name", None) or "tool")
        args = getattr(call, "args", None) or {}
    return name, args if isinstance(args, dict) else {}


def _args_from_task_input(inp: Any) -> dict[str, Any]:
    if not isinstance(inp, dict):
        return {}
    if "messages" in inp:
        return {}
    return {
        key: value
        for key, value in inp.items()
        if isinstance(value, (str, int, float, bool)) or value is None
    }


def _calls_from_task_messages(inp: Any) -> list[tuple[str, dict[str, Any]]]:
    if not isinstance(inp, dict):
        return []
    for message in reversed(list(inp.get("messages") or [])):
        calls = getattr(message, "tool_calls", None)
        if not calls and isinstance(message, dict):
            calls = message.get("tool_calls")
        if calls:
            return [_tool_call_parts(call) for call in calls]
    return []


def _args_gist(args: dict[str, Any], limit: int = 40) -> str:
    for key in ("url", "element", "text", "query", "question", "path", "file_path", "ref", "reason"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value, limit)
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return _clip(value, limit)
    return ""


def _host(url: str) -> str:
    return urlparse(url).netloc or ""


def _clip(text: str, limit: int = 48) -> str:
    compact = " ".join(str(text).split())
    return compact[:limit] + ("…" if len(compact) > limit else "")


def _number(value: Any) -> str:
    try:
        return str(int(float(value)))
    except (TypeError, ValueError):
        return str(value)
