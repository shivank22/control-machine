"""In-process pub/sub between the run loop and the dashboard's SSE connections.

The dashboard and the agent share one uvicorn worker, so a plain asyncio fan-out is
enough; there is no need for a broker. Each task keeps a short replay buffer so a browser
that connects mid-run, or reconnects after a dropped stream, still sees what it missed.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any

REPLAY_LIMIT = 500
_DONE_STATUSES = frozenset({"done", "error", "cancelled", "awaiting_approval"})


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[int, set[asyncio.Queue]] = defaultdict(set)
        self._history: dict[int, list[dict[str, Any]]] = defaultdict(list)
        # Typing is live UI state. Storing every tick would flash a stale
        # indicator when a finished run is replayed, so only the latest is kept.
        self._typing: dict[int, dict[str, Any]] = {}
        self._all: set[asyncio.Queue] = set()

    def publish(self, task_id: int, event: dict[str, Any]) -> None:
        if event.get("type") == "typing":
            self._typing[task_id] = event
        else:
            if event.get("type") == "status" and event.get("status") in _DONE_STATUSES:
                self._typing.pop(task_id, None)
            history = self._history[task_id]
            history.append(event)
            if len(history) > REPLAY_LIMIT:
                del history[:-REPLAY_LIMIT]

        for queue in list(self._subscribers.get(task_id, ())):
            queue.put_nowait(event)
        packed = (task_id, event)
        for queue in list(self._all):
            queue.put_nowait(packed)

    def history(self, task_id: int) -> list[dict[str, Any]]:
        return list(self._history.get(task_id, ()))

    def latest_typing(self, task_id: int) -> dict[str, Any] | None:
        event = self._typing.get(task_id)
        return dict(event) if event else None

    def subscribe(self, task_id: int) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers[task_id].add(queue)
        return queue

    def unsubscribe(self, task_id: int, queue: asyncio.Queue) -> None:
        subscribers = self._subscribers.get(task_id)
        if subscribers:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(task_id, None)

    def subscribe_all(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._all.add(queue)
        return queue

    def unsubscribe_all(self, queue: asyncio.Queue) -> None:
        self._all.discard(queue)

    def forget(self, task_id: int) -> None:
        self._history.pop(task_id, None)
        self._typing.pop(task_id, None)
