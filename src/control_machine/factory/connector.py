"""Outbound connector stream: deliver tool calls, collect observations.

The agent loop runs in the Factory. The computer dials out over a WebSocket,
receives tool calls on that stream, and posts observations back. Heartbeats
still write ``computers.last_seen_at``; the stream is what turns a decision
into a click. One connector connection per computer.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from typing import Any


class ConnectorError(Exception):
    """Raised when a tool call cannot be delivered or completed."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    session_id: int
    name: str
    arguments: dict[str, Any]

    def to_message(self) -> dict[str, Any]:
        return {
            "type": "tool_call",
            "id": self.id,
            "session_id": self.session_id,
            "name": self.name,
            "arguments": self.arguments,
        }


@dataclass(frozen=True)
class ToolResult:
    id: str
    ok: bool
    observation: str = ""
    error: str | None = None

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> ToolResult:
        call_id = str(body.get("id") or "").strip()
        if not call_id:
            raise ValueError("Tool result requires id.")
        ok = bool(body.get("ok", True))
        observation = str(body.get("observation") or "")
        error = body.get("error")
        error_s = str(error) if error is not None else None
        if not ok and not error_s:
            error_s = "Tool call failed."
        return cls(id=call_id, ok=ok, observation=observation, error=error_s)


@dataclass
class _Pending:
    call: ToolCall
    future: ConcurrentFuture[ToolResult]
    enqueued_at: float = field(default_factory=time.monotonic)


@dataclass
class _Link:
    computer_id: int
    outbound: asyncio.Queue[dict[str, Any]]
    loop: asyncio.AbstractEventLoop
    pending: dict[str, _Pending] = field(default_factory=dict)
    connected_at: float = field(default_factory=time.monotonic)


class ConnectorHub:
    """In-process registry of dialed-out connectors for one Factory process.

    ``call_tool`` / ``call_tool_blocking`` may run on a different thread or
    event loop than the WebSocket pump. Outbound puts are scheduled on the
    stream's loop; results complete a thread-safe future.
    """

    def __init__(self, *, default_timeout: float = 120.0) -> None:
        self._links: dict[int, _Link] = {}
        self._lock = asyncio.Lock()
        self.default_timeout = default_timeout
        self._waiters: dict[int, list[asyncio.Future[None]]] = {}

    def connected(self, computer_id: int) -> bool:
        return computer_id in self._links

    def connected_ids(self) -> set[int]:
        return set(self._links)

    async def attach(self, computer_id: int) -> asyncio.Queue[dict[str, Any]]:
        """Register a connector. Replaces any existing stream for that computer."""
        async with self._lock:
            previous = self._links.pop(computer_id, None)
            if previous is not None:
                self._fail_pending(previous, "Connector replaced by a new stream.")
            link = _Link(
                computer_id=computer_id,
                outbound=asyncio.Queue(),
                loop=asyncio.get_running_loop(),
            )
            self._links[computer_id] = link
            waiters = self._waiters.pop(computer_id, [])
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)
        return link.outbound

    async def detach(self, computer_id: int, outbound: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            link = self._links.get(computer_id)
            if link is None or link.outbound is not outbound:
                return
            self._links.pop(computer_id, None)
            self._fail_pending(link, "Connector stream closed.")

    async def wait_until_connected(
        self, computer_id: int, *, timeout: float | None = None
    ) -> None:
        if self.connected(computer_id):
            return
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        async with self._lock:
            if computer_id in self._links:
                return
            self._waiters.setdefault(computer_id, []).append(fut)
        try:
            await asyncio.wait_for(fut, timeout=timeout)
        except TimeoutError as exc:
            async with self._lock:
                waiters = self._waiters.get(computer_id, [])
                if fut in waiters:
                    waiters.remove(fut)
            raise ConnectorError(
                "Connector stream did not dial out in time."
            ) from exc

    async def call_tool(
        self,
        computer_id: int,
        *,
        session_id: int,
        name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
        call_id: str | None = None,
    ) -> ToolResult:
        """Enqueue a tool call on the outbound stream and wait for the result."""
        call, pending_fut = self._enqueue(
            computer_id,
            session_id=session_id,
            name=name,
            arguments=arguments,
            call_id=call_id,
        )
        link = self._links[computer_id]
        await self._put_outbound(link, call.to_message())
        wait_s = self.default_timeout if timeout is None else timeout
        try:
            return await asyncio.wait_for(asyncio.wrap_future(pending_fut), timeout=wait_s)
        except TimeoutError as exc:
            link.pending.pop(call.id, None)
            raise ConnectorError(f"Tool call {call.id} timed out.") from exc

    def call_tool_blocking(
        self,
        computer_id: int,
        *,
        session_id: int,
        name: str,
        arguments: dict[str, Any] | None = None,
        timeout: float | None = None,
        call_id: str | None = None,
    ) -> ToolResult:
        """Enqueue from a sync thread (TestClient) and wait for the result."""
        call, pending_fut = self._enqueue(
            computer_id,
            session_id=session_id,
            name=name,
            arguments=arguments,
            call_id=call_id,
        )
        link = self._links[computer_id]
        wait_s = self.default_timeout if timeout is None else timeout
        put = asyncio.run_coroutine_threadsafe(
            link.outbound.put(call.to_message()), link.loop
        )
        try:
            put.result(timeout=wait_s)
            return pending_fut.result(timeout=wait_s)
        except TimeoutError as exc:
            link.pending.pop(call.id, None)
            raise ConnectorError(f"Tool call {call.id} timed out.") from exc
        except Exception:
            link.pending.pop(call.id, None)
            raise

    async def deliver_result(self, computer_id: int, result: ToolResult) -> bool:
        """Complete a pending tool call. Returns False if the id was unknown."""
        link = self._links.get(computer_id)
        if link is None:
            return False
        pending = link.pending.pop(result.id, None)
        if pending is None:
            return False
        if not pending.future.done():
            pending.future.set_result(result)
        return True

    async def cancel_session(self, computer_id: int, session_id: int) -> None:
        """Tell the connector to drop in-flight work for a session."""
        link = self._links.get(computer_id)
        if link is None:
            return
        await self._put_outbound(link, {"type": "cancel", "session_id": session_id})
        for call_id, pending in list(link.pending.items()):
            if pending.call.session_id == session_id:
                link.pending.pop(call_id, None)
                if not pending.future.done():
                    pending.future.set_exception(
                        ConnectorError("Session was cancelled.")
                    )

    def _enqueue(
        self,
        computer_id: int,
        *,
        session_id: int,
        name: str,
        arguments: dict[str, Any] | None,
        call_id: str | None,
    ) -> tuple[ToolCall, ConcurrentFuture[ToolResult]]:
        link = self._links.get(computer_id)
        if link is None:
            raise ConnectorError("Connector stream is not connected.")
        call = ToolCall(
            id=call_id or _new_call_id(),
            session_id=session_id,
            name=name,
            arguments=dict(arguments or {}),
        )
        pending_fut: ConcurrentFuture[ToolResult] = ConcurrentFuture()
        link.pending[call.id] = _Pending(call=call, future=pending_fut)
        return call, pending_fut

    async def _put_outbound(self, link: _Link, message: dict[str, Any]) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is link.loop:
            await link.outbound.put(message)
            return
        fut = asyncio.run_coroutine_threadsafe(link.outbound.put(message), link.loop)
        await asyncio.wrap_future(fut)

    def _fail_pending(self, link: _Link, reason: str) -> None:
        for pending in list(link.pending.values()):
            if not pending.future.done():
                pending.future.set_exception(ConnectorError(reason))
        link.pending.clear()


def _new_call_id() -> str:
    return f"tc_{secrets.token_urlsafe(10)}"
