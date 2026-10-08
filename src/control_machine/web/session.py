"""Factory session login. The bearer is checked again on every later JSON call."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from ..access import FactorySessions, SessionError, expires_iso

router = APIRouter()


class SessionIn(BaseModel):
    key: str


def require_factory_session(request: Request) -> FactorySession:
    """Refuse a Factory JSON call that has no usable bearer."""
    raw = request.headers.get("authorization", "")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Missing session.")
    sessions = _sessions(request)
    try:
        return sessions.verify(token.strip())
    except SessionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/api/session", status_code=201)
async def create_session(request: Request, body: SessionIn) -> dict[str, str]:
    sessions = _sessions(request)
    if not sessions.configured():
        raise HTTPException(status_code=401, detail="Factory access is not configured.")
    try:
        return sessions.login(body.key)
    except SessionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.get("/api/session")
async def read_session(request: Request) -> dict[str, str]:
    session = require_factory_session(request)
    return {"expires_at": expires_iso(session.expires_at)}


@router.delete("/api/session", status_code=204, response_class=Response)
async def delete_session(request: Request) -> Response:
    raw = request.headers.get("authorization", "")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Missing session.")
    try:
        _sessions(request).revoke(token.strip())
    except SessionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return Response(status_code=204)


def _sessions(request: Request) -> FactorySessions:
    return request.app.state.factory_sessions
