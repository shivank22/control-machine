"""Factory JSON API. Matches docs/design/api.md. Agent loop is stubbed."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..access import TOOL_NAMES, computer_online, session_is_open, tool_name_ok
from ..web.session import require_factory_session
from .live_links import FactoryLiveError, FactoryLiveLinks, expires_datetime
from .serialize import (
    agent_detail,
    agent_list_item,
    event_public,
    memory_public,
    message_public,
    skill_public,
)
from .store import FactoryStore, split_list_row

router = APIRouter()

REFUSAL = (
    "I can't start machine work while the computer is offline. "
    "I'll be here when the connector dials out again."
)
STUB_ACK = (
    "Got it. I opened a session on this computer. "
    "The agent loop is not wired yet — this is the platform foundation."
)


class AgentCreateIn(BaseModel):
    name: str
    description: str = ""
    computer: dict[str, str]


class AgentPatchIn(BaseModel):
    name: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    avatar_color: str | None = None
    tags: list[str] | None = None
    memory_enabled: bool | None = None
    memory_preferences: list[dict[str, Any]] | None = None


class ToolPatchIn(BaseModel):
    enabled: bool


class MemoryIn(BaseModel):
    body: str
    detail: str = ""


class SkillIn(BaseModel):
    name: str
    description: str = ""
    body: str


class SkillPatchIn(BaseModel):
    name: str | None = None
    description: str | None = None
    body: str | None = None


class MessageIn(BaseModel):
    body: str = Field(min_length=1)


class BotIn(BaseModel):
    token: str = Field(min_length=1)


def _store(request: Request) -> FactoryStore:
    return request.app.state.factory


def _live(request: Request) -> FactoryLiveLinks:
    return request.app.state.factory_live


def _auth(_: Any = Depends(require_factory_session)) -> None:
    return None


@router.get("/api/agents")
async def list_agents(request: Request, __: None = Depends(_auth)) -> list[dict[str, Any]]:
    rows = await _store(request).list_agents()
    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        agent, computer, open_session = split_list_row(row)
        out.append(agent_list_item(agent, computer, open_session, now=now))
    return out


@router.post("/api/agents", status_code=201)
async def create_agent(
    request: Request, body: AgentCreateIn, __: None = Depends(_auth)
) -> dict[str, Any]:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required.")
    computer = body.computer or {}
    computer_name = (computer.get("name") or "").strip() or "This Mac"
    kind = (computer.get("kind") or "").strip().lower()
    provider = (computer.get("provider") or "").strip().lower()
    if kind == "mac":
        provider = "local"
    elif kind == "vm":
        if not provider or provider == "local":
            raise HTTPException(
                status_code=400,
                detail="A VM requires a provider other than local.",
            )
    else:
        raise HTTPException(status_code=400, detail="kind must be mac or vm.")
    store = _store(request)
    try:
        bundle, credential = await store.create_agent(
            name=name,
            description=body.description.strip(),
            computer_name=computer_name,
            kind=kind,
            provider=provider,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    detail = _detail_from_bundle(bundle)
    detail["connector_credential"] = credential
    return detail


@router.get("/api/agents/{agent_id}")
async def get_agent(
    request: Request, agent_id: int, __: None = Depends(_auth)
) -> dict[str, Any]:
    bundle = await _require_agent(request, agent_id)
    return _detail_from_bundle(bundle)


@router.patch("/api/agents/{agent_id}")
async def patch_agent(
    request: Request, agent_id: int, body: AgentPatchIn, __: None = Depends(_auth)
) -> dict[str, Any]:
    await _require_agent(request, agent_id)
    fields = body.model_dump(exclude_unset=True)
    if "name" in fields and not str(fields["name"] or "").strip():
        raise HTTPException(status_code=400, detail="Name is required.")
    bundle = await _store(request).update_agent(agent_id, fields)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return _detail_from_bundle(bundle)


@router.patch("/api/agents/{agent_id}/tools/{name}")
async def patch_tool(
    request: Request,
    agent_id: int,
    name: str,
    body: ToolPatchIn,
    __: None = Depends(_auth),
) -> dict[str, Any]:
    await _require_agent(request, agent_id)
    if not tool_name_ok(name):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown tool. Expected one of: {', '.join(sorted(TOOL_NAMES))}.",
        )
    row = await _store(request).set_tool(agent_id, name, body.enabled)
    if row is None:
        raise HTTPException(status_code=404, detail="Tool not found.")
    return {"name": row["name"], "enabled": bool(row["enabled"])}


@router.get("/api/agents/{agent_id}/memories")
async def list_memories(
    request: Request, agent_id: int, __: None = Depends(_auth)
) -> list[dict[str, Any]]:
    await _require_agent(request, agent_id)
    rows = await _store(request).list_memories(agent_id)
    return [memory_public(r) for r in rows]


@router.post("/api/agents/{agent_id}/memories", status_code=201)
async def create_memory(
    request: Request, agent_id: int, body: MemoryIn, __: None = Depends(_auth)
) -> dict[str, Any]:
    await _require_agent(request, agent_id)
    if not body.body.strip():
        raise HTTPException(status_code=400, detail="Body is required.")
    row = await _store(request).create_memory(
        agent_id, body=body.body.strip(), detail=body.detail.strip()
    )
    return memory_public(row)


@router.delete(
    "/api/agents/{agent_id}/memories/{memory_id}",
    status_code=204,
    response_class=Response,
)
async def delete_memory(
    request: Request, agent_id: int, memory_id: int, __: None = Depends(_auth)
) -> Response:
    await _require_agent(request, agent_id)
    ok = await _store(request).delete_memory(agent_id, memory_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Memory not found.")
    return Response(status_code=204)


@router.get("/api/agents/{agent_id}/skills")
async def list_skills(
    request: Request, agent_id: int, __: None = Depends(_auth)
) -> list[dict[str, Any]]:
    bundle = await _require_agent(request, agent_id)
    open_skill = None
    if bundle["session"] is not None:
        open_skill = bundle["session"].get("skill_id")
    rows = await _store(request).list_skills(agent_id)
    return [
        skill_public(r, in_use=open_skill is not None and int(r["id"]) == int(open_skill))
        for r in rows
    ]


@router.post("/api/agents/{agent_id}/skills", status_code=201)
async def create_skill(
    request: Request, agent_id: int, body: SkillIn, __: None = Depends(_auth)
) -> dict[str, Any]:
    await _require_agent(request, agent_id)
    if not body.name.strip() or not body.body.strip():
        raise HTTPException(status_code=400, detail="Name and body are required.")
    row = await _store(request).create_skill(
        agent_id,
        name=body.name.strip(),
        description=body.description.strip(),
        body=body.body.strip(),
    )
    return skill_public(row, in_use=False)


@router.patch("/api/agents/{agent_id}/skills/{skill_id}")
async def patch_skill(
    request: Request,
    agent_id: int,
    skill_id: int,
    body: SkillPatchIn,
    __: None = Depends(_auth),
) -> dict[str, Any]:
    bundle = await _require_agent(request, agent_id)
    row = await _store(request).update_skill(
        agent_id, skill_id, body.model_dump(exclude_unset=True)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Skill not found.")
    open_skill = None
    if bundle["session"] is not None:
        open_skill = bundle["session"].get("skill_id")
    return skill_public(
        row, in_use=open_skill is not None and int(row["id"]) == int(open_skill)
    )


@router.delete(
    "/api/agents/{agent_id}/skills/{skill_id}",
    status_code=204,
    response_class=Response,
)
async def delete_skill(
    request: Request, agent_id: int, skill_id: int, __: None = Depends(_auth)
) -> Response:
    await _require_agent(request, agent_id)
    ok = await _store(request).delete_skill(agent_id, skill_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Skill not found.")
    return Response(status_code=204)


@router.get("/api/agents/{agent_id}/events")
async def list_events(
    request: Request,
    agent_id: int,
    session_id: int | None = None,
    __: None = Depends(_auth),
) -> list[dict[str, Any]]:
    await _require_agent(request, agent_id)
    rows = await _store(request).list_events(agent_id, session_id=session_id)
    return [event_public(r) for r in rows]


@router.get("/api/agents/{agent_id}/messages")
async def list_messages(
    request: Request, agent_id: int, __: None = Depends(_auth)
) -> list[dict[str, Any]]:
    await _require_agent(request, agent_id)
    rows = await _store(request).list_messages(agent_id)
    return [message_public(r) for r in rows]


@router.post("/api/agents/{agent_id}/messages", status_code=201)
async def post_message(
    request: Request, agent_id: int, body: MessageIn, __: None = Depends(_auth)
) -> dict[str, Any]:
    """Compose box and pause answer. Agent replies are stubbed for now."""
    store = _store(request)
    await store.expire_stale_pauses()
    bundle = await _require_agent(request, agent_id)
    text = body.body.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Body is required.")

    computer = bundle["computer"]
    online = computer_online(computer.get("last_seen_at"))
    open_session = bundle["session"]
    waiting = bundle["pause"]

    if open_session is not None and str(open_session["status"]) == "running":
        raise HTTPException(
            status_code=409,
            detail="This agent already has an open session.",
        )

    if open_session is not None and str(open_session["status"]) == "paused" and waiting:
        user_msg = await store.add_message(
            agent_id,
            role="user",
            body=text,
            session_id=int(open_session["id"]),
        )
        await store.answer_pause(int(waiting["id"]), int(user_msg["id"]), apply=online)
        if online:
            await store.add_message(
                agent_id,
                role="agent",
                body="Thanks — continuing from where we paused. (Agent loop stub.)",
                session_id=int(open_session["id"]),
            )
        else:
            await store.add_message(
                agent_id,
                role="agent",
                body=(
                    "Got your reply. I'll apply it when the connector is online again."
                ),
                session_id=int(open_session["id"]),
            )
        return message_public(user_msg)

    # No open session: start or refuse.
    user_msg = await store.add_message(agent_id, role="user", body=text)
    if not online:
        await store.add_message(agent_id, role="agent", body=REFUSAL)
        return message_public(user_msg)

    title = text if len(text) <= 80 else text[:77] + "…"
    session = await store.create_session(
        agent_id, int(computer["id"]), title=title
    )
    await store.add_message(
        agent_id,
        role="agent",
        body=STUB_ACK,
        session_id=int(session["id"]),
    )
    await store.add_event(
        agent_id,
        kind="said",
        title="Opened a session",
        body=title,
        session_id=int(session["id"]),
    )
    return message_public(user_msg)


@router.post(
    "/api/agents/{agent_id}/session/cancel",
    status_code=204,
    response_class=Response,
)
async def cancel_session(
    request: Request, agent_id: int, __: None = Depends(_auth)
) -> Response:
    await _require_agent(request, agent_id)
    ok = await _store(request).cancel_open_session(agent_id)
    if not ok:
        raise HTTPException(status_code=409, detail="No open session.")
    return Response(status_code=204)


@router.post("/api/agents/{agent_id}/desktop", status_code=201)
async def mint_desktop(
    request: Request, agent_id: int, __: None = Depends(_auth)
) -> dict[str, str]:
    bundle = await _require_agent(request, agent_id)
    open_session = bundle["session"]
    if open_session is None or not session_is_open(str(open_session["status"])):
        raise HTTPException(status_code=409, detail="No open session.")
    if not computer_online(bundle["computer"].get("last_seen_at")):
        raise HTTPException(status_code=503, detail="Computer is offline.")
    try:
        public, link = _live(request).mint(int(open_session["id"]))
    except FactoryLiveError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    await _store(request).set_live_link(
        int(open_session["id"]),
        jti=link.jti,
        expires_at=expires_datetime(link.expires_at),
    )
    return public


@router.post("/api/agents/{agent_id}/bot", status_code=201)
async def bind_bot(
    request: Request, agent_id: int, body: BotIn, __: None = Depends(_auth)
) -> dict[str, Any]:
    """Bind a Telegram bot token. Lookup of username is stubbed without network."""
    await _require_agent(request, agent_id)
    token = body.token.strip()
    if ":" not in token:
        raise HTTPException(status_code=400, detail="Token looks invalid.")
    bot_id_s, _, _ = token.partition(":")
    try:
        telegram_bot_id = int(bot_id_s)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Token looks invalid.") from exc
    username = f"bot_{telegram_bot_id}"
    bot = await _store(request).bind_bot(
        agent_id,
        token=token,
        username=username,
        telegram_bot_id=telegram_bot_id,
    )
    from ..access import public_bot

    return public_bot(
        bot_id=int(bot["id"]),
        username=bot.get("username"),
        chat_id=bot.get("chat_id"),
    )


@router.delete(
    "/api/agents/{agent_id}/bot",
    status_code=204,
    response_class=Response,
)
async def unbind_bot(
    request: Request, agent_id: int, __: None = Depends(_auth)
) -> Response:
    await _require_agent(request, agent_id)
    await _store(request).unbind_bot(agent_id)
    return Response(status_code=204)


@router.post("/api/connector/heartbeat")
async def connector_heartbeat(request: Request) -> dict[str, Any]:
    """Connector dials out. Does not use the Factory bearer."""
    raw = request.headers.get("authorization", "")
    scheme, _, token = raw.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Missing connector credential.")
    store = _store(request)
    computer = await store.computer_for_credential(token.strip())
    if computer is None:
        raise HTTPException(status_code=401, detail="Connector credential was not accepted.")
    updated = await store.heartbeat(int(computer["id"]))
    assert updated is not None
    # Apply any pause replies that arrived while offline.
    bundle_rows = await store.list_agents()
    for row in bundle_rows:
        if int(row["c_id"]) != int(computer["id"]):
            continue
        if row.get("s_status") != "paused":
            continue
        agent_id = int(row["id"])
        full = await store.get_agent_bundle(agent_id)
        if full is None or full["pause"] is None or full["session"] is None:
            continue
        # Find the latest user message after the pause with no answered_message yet.
        messages = await store.list_messages(agent_id)
        pause = full["pause"]
        if pause.get("answered_message_id"):
            continue
        for msg in reversed(messages):
            if msg["role"] == "user" and msg.get("session_id") == full["session"]["id"]:
                if msg["created_at"] and pause.get("created_at"):
                    if msg["created_at"] >= pause["created_at"]:
                        await store.answer_pause(
                            int(pause["id"]), int(msg["id"]), apply=True
                        )
                break
    return {
        "computer_id": int(updated["id"]),
        "last_seen_at": updated["last_seen_at"].isoformat().replace("+00:00", "Z")
        if updated.get("last_seen_at")
        else None,
    }


async def _require_agent(request: Request, agent_id: int) -> dict[str, Any]:
    bundle = await _store(request).get_agent_bundle(agent_id)
    if bundle is None:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return bundle


def _detail_from_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    return agent_detail(
        bundle["agent"],
        bundle["computer"],
        bundle["bot"],
        bundle["tools"],
        bundle["session"],
        bundle["pause"],
    )
