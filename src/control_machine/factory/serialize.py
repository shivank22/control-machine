"""Public JSON shapes for the Factory API."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..access import computer_online, public_bot, session_is_open


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def computer_public(row: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    last_seen = row.get("last_seen_at")
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "kind": row["kind"],
        "provider": row["provider"],
        "status": "online" if computer_online(last_seen, now=now) else "offline",
        "last_seen_at": iso(last_seen),
    }


def agent_list_item(
    agent: dict[str, Any],
    computer: dict[str, Any],
    open_session: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    status = "idle"
    session_title = None
    if open_session is not None and session_is_open(str(open_session["status"])):
        status = str(open_session["status"])
        session_title = open_session.get("title")
    return {
        "id": int(agent["id"]),
        "name": agent["name"],
        "description": agent["description"],
        "avatar_color": agent["avatar_color"],
        "tags": list(agent.get("tags") or []),
        "status": status,
        "session_title": session_title,
        "computer": computer_public(computer, now=now),
    }


def tool_public(row: dict[str, Any]) -> dict[str, Any]:
    return {"name": row["name"], "enabled": bool(row["enabled"])}


def session_public(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "title": row["title"],
        "status": row["status"],
        "skill_id": int(row["skill_id"]) if row.get("skill_id") is not None else None,
        "page_url": row.get("page_url"),
        "page_title": row.get("page_title"),
        "started_at": iso(row.get("started_at")),
        "heartbeat_at": iso(row.get("heartbeat_at")),
    }


def pause_public(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None or str(row.get("status")) != "waiting":
        return None
    return {
        "id": int(row["id"]),
        "question": row["question"],
        "status": row["status"],
        "created_at": iso(row.get("created_at")),
        "will_do": list(row.get("will_do") or []),
        "will_not": list(row.get("will_not") or []),
    }


def agent_detail(
    agent: dict[str, Any],
    computer: dict[str, Any],
    bot: dict[str, Any] | None,
    tools: list[dict[str, Any]],
    open_session: dict[str, Any] | None,
    waiting_pause: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    bot_obj: dict[str, object]
    if bot is None:
        bot_obj = {"id": 0, "username": None, "connected": False}
    else:
        bot_obj = public_bot(
            bot_id=int(bot["id"]),
            username=bot.get("username"),
            chat_id=bot.get("chat_id"),
        )
    return {
        "id": int(agent["id"]),
        "computer_id": int(agent["computer_id"]),
        "name": agent["name"],
        "description": agent["description"],
        "system_prompt": agent["system_prompt"],
        "avatar_color": agent["avatar_color"],
        "tags": list(agent.get("tags") or []),
        "memory_enabled": bool(agent["memory_enabled"]),
        "memory_preferences": list(agent.get("memory_preferences") or []),
        "bot": bot_obj,
        "computer": computer_public(computer, now=now),
        "tools": [tool_public(t) for t in tools],
        "session": session_public(open_session),
        "pause": pause_public(waiting_pause),
    }


def memory_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "body": row["body"],
        "detail": row.get("detail") or "",
        "created_at": iso(row.get("created_at")),
    }


def skill_public(row: dict[str, Any], *, in_use: bool = False) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "description": row.get("description") or "",
        "body": row["body"],
        "in_use": in_use,
    }


def event_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "session_id": int(row["session_id"]) if row.get("session_id") is not None else None,
        "kind": row["kind"],
        "title": row["title"],
        "body": row.get("body") or "",
        "created_at": iso(row.get("created_at")),
    }


def message_public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "role": row["role"],
        "body": row["body"],
        "created_at": iso(row.get("created_at")),
        "pause_id": int(row["pause_id"]) if row.get("pause_id") is not None else None,
    }
