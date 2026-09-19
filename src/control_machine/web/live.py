"""Live VNC session API and cookie-gated reverse-proxy.

JSON routes mint/inspect/revoke a signed session. ``/live/{task}`` serves a phone
viewer that talks RFB to this Mac's Screen Sharing and can fall back to a browser
slot's noVNC. Raw CDP is never proxied.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import websockets
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..desktop import (
    DesktopError,
    capture_jpeg,
    click_at,
    display_count,
    input_is_trusted,
    press_key,
    show_apps,
    show_menu,
    type_text,
)
from ..live import COOKIE_NAME, VIEWER_QUERY, LiveError, LiveSessions, open_live_session
from ..runner import RunManager

log = logging.getLogger(__name__)

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "cookie",
}

router = APIRouter()
_TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _live(request: Request) -> LiveSessions:
    return request.app.state.live


def _runner(request: Request) -> RunManager:
    return request.app.state.runner


async def _task_or_404(request: Request, task_id: int) -> dict[str, Any]:
    task = await request.app.state.db.get_task(task_id)
    if not task:
        raise HTTPException(404, f"No task {task_id}")
    return task


async def mint_live_session(request: Request, task_id: int) -> dict[str, object]:
    """Pause a running agent if needed, then mint (or reuse) a signed viewer URL."""
    try:
        return await open_live_session(
            task_id,
            runner=_runner(request),
            db=request.app.state.db,
            sessions=_live(request),
        )
    except LiveError as exc:
        message = str(exc)
        status = 404 if message.startswith("No task") else 409
        raise HTTPException(status, message) from exc


@router.post("/api/tasks/{task_id}/live")
async def create_live_session(request: Request, task_id: int) -> dict[str, object]:
    return await mint_live_session(request, task_id)


@router.get("/api/tasks/{task_id}/live")
async def get_live_session(request: Request, task_id: int) -> dict[str, object]:
    await _task_or_404(request, task_id)
    session = _live(request).inspect(task_id)
    if session is None:
        raise HTTPException(404, "No live session for this task.")
    return session


@router.delete("/api/tasks/{task_id}/live")
async def delete_live_session(request: Request, task_id: int) -> dict[str, object]:
    await _task_or_404(request, task_id)
    if not _live(request).revoke(task_id):
        raise HTTPException(404, "No live session for this task.")
    return {"ok": True}


@router.get("/live/{task_id}")
async def open_live_viewer(request: Request, task_id: int) -> Response:
    live = _live(request)
    token = request.query_params.get("t") or request.cookies.get(COOKIE_NAME)
    if not token:
        return _denied("This live link is missing a token.")
    try:
        session = live.verify_token(token)
    except LiveError as exc:
        return _denied(str(exc))
    if session.task_id != task_id:
        return _denied("This live link does not match the task.")
    if request.query_params.get("legacy") == "1":
        ws_path = f"live/slots/{session.slot_id}/ws"
        target = (
            f"/live/slots/{session.slot_id}/vnc.html?{VIEWER_QUERY}&path={quote(ws_path, safe='')}"
        )
        response: Response = RedirectResponse(target, status_code=307)
    else:
        response = _TEMPLATES.TemplateResponse(
            request,
            "live.html",
            {
                "desktop_ws_path": "/live/desktop/stream",
                "token": session.token,
            },
        )
    response.set_cookie(
        COOKIE_NAME,
        session.token,
        httponly=True,
        samesite="lax",
        secure=live.cookie_secure(),
        path="/live",
        expires=session.expires_at,
    )
    return response


@router.websocket("/live/desktop/stream")
async def stream_desktop(websocket: WebSocket) -> None:
    live: LiveSessions = websocket.app.state.live
    token = websocket.cookies.get(COOKIE_NAME) or websocket.query_params.get("t")
    if not token:
        await websocket.close(code=4401)
        return
    try:
        live.verify_token(token)
    except LiveError:
        await websocket.close(code=4403)
        return
    await websocket.accept()
    send_lock = asyncio.Lock()

    async def send_json(payload: dict[str, object]) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(payload))

    async def send_jpeg(data: bytes) -> None:
        async with send_lock:
            await websocket.send_bytes(data)

    state = {"page": 1}
    pages = max(1, display_count())
    ready = {
        "ready": input_is_trusted(),
        "page": 1,
        "pages": pages,
    }
    if not ready["ready"]:
        ready["error"] = (
            "This Mac is visible, but clicks and typing are blocked. Grant "
            "Accessibility to the app running control-machine (Terminal, iTerm, "
            "or Cursor) in System Settings → Privacy & Security → Accessibility, "
            "then restart control-machine."
        )
    await send_json(ready)

    async def inbound() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                text = message.get("text")
                if not text:
                    continue
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    continue
                kind = payload.get("type")
                try:
                    if kind == "click":
                        await asyncio.to_thread(
                            click_at,
                            int(payload["x"]),
                            int(payload["y"]),
                            image_width=int(payload.get("w") or 0) or None,
                            image_height=int(payload.get("h") or 0) or None,
                        )
                    elif kind == "type":
                        await asyncio.to_thread(type_text, str(payload.get("text") or ""))
                    elif kind == "key":
                        await asyncio.to_thread(press_key, str(payload.get("key") or ""))
                    elif kind == "apps":
                        await asyncio.to_thread(show_apps)
                    elif kind == "menu":
                        await asyncio.to_thread(show_menu, page=state["page"])
                    elif kind == "page":
                        pages_now = max(1, display_count())
                        state["page"] = min(max(int(payload.get("n") or 1), 1), pages_now)
                        await send_json({"page": state["page"], "pages": pages_now})
                except (DesktopError, KeyError, TypeError, ValueError) as exc:
                    log.warning("live desktop %s failed: %s", kind, exc)
                    await send_json({"error": str(exc)})
        except WebSocketDisconnect:
            return

    async def outbound() -> None:
        try:
            while True:
                try:
                    jpeg, _, _ = await asyncio.to_thread(capture_jpeg, page=state["page"])
                except DesktopError as exc:
                    await send_json({"error": str(exc)})
                    await asyncio.sleep(2)
                    continue
                await send_jpeg(jpeg)
                await asyncio.sleep(0.35)
        except WebSocketDisconnect:
            return

    tasks = [asyncio.create_task(inbound()), asyncio.create_task(outbound())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*tasks, return_exceptions=True)


@router.api_route(
    "/live/slots/{slot_id}/{path:path}",
    methods=["GET"],
    name="proxy_novnc_http",
)
async def proxy_novnc_http(request: Request, slot_id: int, path: str) -> Response:
    live = _live(request)
    denied = _require_slot_cookie(request, slot_id)
    if denied is not None:
        return denied
    try:
        host, port = live.slot_upstream(slot_id)
    except LiveError as exc:
        return _denied(str(exc))
    query = str(request.query_params)
    url = f"http://{host}:{port}/{path}"
    if query:
        url = f"{url}?{query}"
    headers = _forward_headers(request.headers, host, port)
    client: httpx.AsyncClient = request.app.state.live_http
    try:
        upstream = await client.request(request.method, url, headers=headers)
    except httpx.HTTPError:
        return _denied("The live browser is not reachable.", status=502)
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )


@router.websocket("/live/slots/{slot_id}/{path:path}")
async def proxy_novnc_ws(websocket: WebSocket, slot_id: int, path: str) -> None:
    del path
    live: LiveSessions = websocket.app.state.live
    token = websocket.cookies.get(COOKIE_NAME)
    if not token:
        await websocket.close(code=4401)
        return
    try:
        live.verify_token(token, slot_id=slot_id)
        host, port = live.slot_upstream(slot_id)
    except LiveError:
        await websocket.close(code=4403)
        return
    # websockify accepts the RFB websocket on the listen port regardless of path.
    await _pump_websocket(websocket, f"ws://{host}:{port}/")


def _require_slot_cookie(request: Request, slot_id: int) -> HTMLResponse | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return _denied("Open the signed live link from Telegram first.")
    try:
        _live(request).verify_token(token, slot_id=slot_id)
    except LiveError as exc:
        return _denied(str(exc))
    return None


def _forward_headers(headers: Mapping[str, str], host: str, port: int) -> dict[str, str]:
    forwarded = {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP
    }
    forwarded["host"] = f"{host}:{port}"
    return forwarded


def _response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP and key.lower() != "content-type"
    }


def _denied(message: str, status: int = 403) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><title>Live view</title><p>{message}</p>",
        status_code=status,
    )


async def _pump_tcp(client: WebSocket, host: str, port: int) -> None:
    requested = client.headers.get("sec-websocket-protocol", "")
    subprotocols = [item.strip() for item in requested.split(",") if item.strip()]
    await client.accept(subprotocol=subprotocols[0] if subprotocols else None)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=5,
        )
    except (OSError, TimeoutError):
        with contextlib.suppress(Exception):
            await client.close(code=1011)
        return

    async def client_to_vnc() -> None:
        try:
            while True:
                message = await client.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if data is None:
                    text = message.get("text")
                    if text is None:
                        continue
                    data = text.encode()
                writer.write(data)
                await writer.drain()
        except WebSocketDisconnect:
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def vnc_to_client() -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    await client.close()
                    return
                await client.send_bytes(data)
        except (WebSocketDisconnect, OSError):
            return

    try:
        await asyncio.gather(client_to_vnc(), vnc_to_client())
    except Exception:  # noqa: BLE001 - keep the live proxy from crashing the app
        log.exception("Live VNC proxy failed")
        with contextlib.suppress(Exception):
            await client.close(code=1011)


async def _pump_websocket(client: WebSocket, upstream_url: str) -> None:
    requested = client.headers.get("sec-websocket-protocol", "")
    subprotocols = [item.strip() for item in requested.split(",") if item.strip()]
    await client.accept(subprotocol=subprotocols[0] if subprotocols else None)
    try:
        async with websockets.connect(
            upstream_url,
            subprotocols=subprotocols or None,
            max_size=None,
            open_timeout=8,
        ) as upstream:
            await asyncio.gather(
                _client_to_upstream(client, upstream),
                _upstream_to_client(client, upstream),
            )
    except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
        return
    except OSError:
        log.warning("Live noVNC upstream at %s refused the connection", upstream_url)
        with contextlib.suppress(Exception):
            await client.close(code=1011)


async def _client_to_upstream(client: WebSocket, upstream: Any) -> None:
    try:
        while True:
            message = await client.receive()
            msg_type = message.get("type")
            if msg_type == "websocket.disconnect":
                await upstream.close()
                return
            data = message.get("bytes", None)
            if data is not None:
                await upstream.send(data)
                continue
            text = message.get("text", None)
            if text is not None:
                await upstream.send(text)
    except WebSocketDisconnect:
        await upstream.close()


async def _upstream_to_client(client: WebSocket, upstream: Any) -> None:
    async for message in upstream:
        if isinstance(message, bytes):
            await client.send_bytes(message)
        else:
            await client.send_text(message)
