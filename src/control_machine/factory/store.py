"""Postgres reads and writes for the Factory tables."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from psycopg_pool import AsyncConnectionPool

from ..access import computer_online, session_is_open
from .schema import FACTORY_SCHEMA, LEGACY_SKILLS_RENAME

DEFAULT_TOOLS = (
    ("browser", True),
    ("clicks", True),
    ("files", True),
    ("sap", False),
)
DEFAULT_AVATAR = "#6d4dff"
LIVE_LINK_TTL = timedelta(minutes=15)
PAUSE_TIMEOUT = timedelta(hours=24)


@dataclass
class FactoryStore:
    pool: AsyncConnectionPool

    @classmethod
    async def setup(cls, pool: AsyncConnectionPool) -> FactoryStore:
        async with pool.connection() as conn:
            await conn.execute(LEGACY_SKILLS_RENAME)
            await conn.execute(FACTORY_SCHEMA)
        return cls(pool=pool)

    # ---------------------------------------------------------------- agents

    async def list_agents(self) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT a.*,
                       c.id AS c_id, c.name AS c_name, c.kind AS c_kind,
                       c.provider AS c_provider, c.last_seen_at AS c_last_seen_at,
                       s.id AS s_id, s.title AS s_title, s.status AS s_status
                  FROM agents a
                  JOIN computers c ON c.id = a.computer_id
                  LEFT JOIN sessions s
                    ON s.agent_id = a.id AND s.status IN ('running', 'paused')
                 ORDER BY a.id
                """
            )
            return [dict(r) for r in await cur.fetchall()]

    async def get_agent_bundle(self, agent_id: int) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT a.*,
                       c.id AS c_id, c.name AS c_name, c.kind AS c_kind,
                       c.provider AS c_provider, c.last_seen_at AS c_last_seen_at
                  FROM agents a
                  JOIN computers c ON c.id = a.computer_id
                 WHERE a.id = %s
                """,
                (agent_id,),
            )
            agent = await cur.fetchone()
            if agent is None:
                return None
            bot = await (
                await conn.execute("SELECT * FROM bots WHERE agent_id = %s", (agent_id,))
            ).fetchone()
            tools = await (
                await conn.execute(
                    "SELECT * FROM agent_tools WHERE agent_id = %s ORDER BY id",
                    (agent_id,),
                )
            ).fetchall()
            session = await (
                await conn.execute(
                    """
                    SELECT * FROM sessions
                     WHERE agent_id = %s AND status IN ('running', 'paused')
                     LIMIT 1
                    """,
                    (agent_id,),
                )
            ).fetchone()
            pause = None
            if session is not None:
                pause = await (
                    await conn.execute(
                        """
                        SELECT * FROM pauses
                         WHERE session_id = %s AND status = 'waiting'
                         LIMIT 1
                        """,
                        (session["id"],),
                    )
                ).fetchone()
            return {
                "agent": dict(agent),
                "computer": {
                    "id": agent["c_id"],
                    "name": agent["c_name"],
                    "kind": agent["c_kind"],
                    "provider": agent["c_provider"],
                    "last_seen_at": agent["c_last_seen_at"],
                },
                "bot": dict(bot) if bot else None,
                "tools": [dict(t) for t in tools],
                "session": dict(session) if session else None,
                "pause": dict(pause) if pause else None,
            }

    async def create_agent(
        self,
        *,
        name: str,
        description: str,
        computer_name: str,
        kind: str,
        provider: str,
        system_prompt: str = "",
        avatar_color: str = DEFAULT_AVATAR,
        tags: list[str] | None = None,
    ) -> tuple[dict[str, Any], str]:
        """Create agent + computer + bot + tools. Returns (bundle, connector_credential)."""
        credential = secrets.token_urlsafe(32)
        credential_hash = _hash_credential(credential)
        prompt = system_prompt or (
            f"You are {name}. The chat is this agent's Telegram bot. "
            "If the computer is offline, refuse machine work."
        )
        async with self.pool.connection() as conn:
            async with conn.transaction():
                cur = await conn.execute(
                    """
                    INSERT INTO computers (name, kind, provider)
                    VALUES (%s, %s, %s)
                    RETURNING *
                    """,
                    (computer_name, kind, provider),
                )
                computer = dict(await cur.fetchone())  # type: ignore[arg-type]
                cur = await conn.execute(
                    """
                    INSERT INTO agents
                        (computer_id, name, description, system_prompt, avatar_color, tags)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        computer["id"],
                        name,
                        description,
                        prompt,
                        avatar_color,
                        tags or [],
                    ),
                )
                agent = dict(await cur.fetchone())  # type: ignore[arg-type]
                await conn.execute(
                    "INSERT INTO bots (agent_id) VALUES (%s)",
                    (agent["id"],),
                )
                for tool_name, enabled in DEFAULT_TOOLS:
                    await conn.execute(
                        """
                        INSERT INTO agent_tools (agent_id, name, enabled)
                        VALUES (%s, %s, %s)
                        """,
                        (agent["id"], tool_name, enabled),
                    )
                await conn.execute(
                    """
                    INSERT INTO computer_credentials (computer_id, credential_hash)
                    VALUES (%s, %s)
                    """,
                    (computer["id"], credential_hash),
                )
        bundle = await self.get_agent_bundle(int(agent["id"]))
        assert bundle is not None
        return bundle, credential

    async def update_agent(self, agent_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "name",
            "description",
            "system_prompt",
            "avatar_color",
            "tags",
            "memory_enabled",
            "memory_preferences",
        }
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return await self.get_agent_bundle(agent_id)
        cols = []
        vals: list[Any] = []
        for key, value in updates.items():
            if key == "memory_preferences":
                cols.append(f"{key} = %s::jsonb")
                vals.append(json.dumps(value))
            elif key == "tags":
                cols.append(f"{key} = %s")
                vals.append(list(value))
            else:
                cols.append(f"{key} = %s")
                vals.append(value)
        vals.append(agent_id)
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"UPDATE agents SET {', '.join(cols)} WHERE id = %s RETURNING id",
                tuple(vals),
            )
            if await cur.fetchone() is None:
                return None
        return await self.get_agent_bundle(agent_id)

    async def set_tool(self, agent_id: int, name: str, enabled: bool) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE agent_tools SET enabled = %s
                 WHERE agent_id = %s AND name = %s
                RETURNING *
                """,
                (enabled, agent_id, name),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    # -------------------------------------------------------------- memories

    async def list_memories(self, agent_id: int) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM memories
                 WHERE agent_id = %s
                 ORDER BY created_at DESC, id DESC
                """,
                (agent_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def create_memory(
        self, agent_id: int, *, body: str, detail: str = ""
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO memories (agent_id, body, detail)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (agent_id, body, detail),
            )
            return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def delete_memory(self, agent_id: int, memory_id: int) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM memories WHERE agent_id = %s AND id = %s RETURNING id",
                (agent_id, memory_id),
            )
            return await cur.fetchone() is not None

    # ---------------------------------------------------------------- skills

    async def list_skills(self, agent_id: int) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM skills
                 WHERE agent_id = %s
                 ORDER BY updated_at DESC, id DESC
                """,
                (agent_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def create_skill(
        self, agent_id: int, *, name: str, description: str, body: str
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO skills (agent_id, name, description, body)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """,
                (agent_id, name, description, body),
            )
            return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def update_skill(
        self, agent_id: int, skill_id: int, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        allowed = {"name", "description", "body"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            async with self.pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT * FROM skills WHERE agent_id = %s AND id = %s",
                    (agent_id, skill_id),
                )
                row = await cur.fetchone()
                return dict(row) if row else None
        cols = [f"{k} = %s" for k in updates]
        vals = list(updates.values()) + [agent_id, skill_id]
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""
                UPDATE skills
                   SET {', '.join(cols)}, updated_at = now()
                 WHERE agent_id = %s AND id = %s
                RETURNING *
                """,
                tuple(vals),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def delete_skill(self, agent_id: int, skill_id: int) -> bool:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE sessions SET skill_id = NULL
                 WHERE agent_id = %s AND skill_id = %s AND status IN ('running', 'paused')
                """,
                (agent_id, skill_id),
            )
            cur = await conn.execute(
                "DELETE FROM skills WHERE agent_id = %s AND id = %s RETURNING id",
                (agent_id, skill_id),
            )
            return await cur.fetchone() is not None

    # ---------------------------------------------------------------- events

    async def list_events(
        self, agent_id: int, *, session_id: int | None = None
    ) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            sid = session_id
            if sid is None:
                cur = await conn.execute(
                    """
                    SELECT id FROM sessions
                     WHERE agent_id = %s
                     ORDER BY
                       CASE WHEN status IN ('running', 'paused') THEN 0 ELSE 1 END,
                       started_at DESC
                     LIMIT 1
                    """,
                    (agent_id,),
                )
                row = await cur.fetchone()
                if row is None:
                    return []
                sid = int(row["id"])
            cur = await conn.execute(
                """
                SELECT * FROM events
                 WHERE agent_id = %s AND session_id = %s
                 ORDER BY created_at ASC, id ASC
                """,
                (agent_id, sid),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def add_event(
        self,
        agent_id: int,
        *,
        kind: str,
        title: str,
        body: str = "",
        session_id: int | None = None,
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO events (agent_id, session_id, kind, title, body)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (agent_id, session_id, kind, title, body),
            )
            return dict(await cur.fetchone())  # type: ignore[arg-type]

    # -------------------------------------------------------------- messages

    async def list_messages(self, agent_id: int) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM messages
                 WHERE agent_id = %s
                 ORDER BY created_at ASC, id ASC
                """,
                (agent_id,),
            )
            return [dict(r) for r in await cur.fetchall()]

    async def add_message(
        self,
        agent_id: int,
        *,
        role: str,
        body: str,
        session_id: int | None = None,
        pause_id: int | None = None,
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO messages (agent_id, session_id, pause_id, role, body)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (agent_id, session_id, pause_id, role, body),
            )
            return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def create_session(
        self, agent_id: int, computer_id: int, *, title: str
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO sessions (agent_id, computer_id, title, status, heartbeat_at)
                VALUES (%s, %s, %s, 'running', now())
                RETURNING *
                """,
                (agent_id, computer_id, title),
            )
            return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def cancel_open_session(self, agent_id: int) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT id FROM sessions
                 WHERE agent_id = %s AND status IN ('running', 'paused')
                 LIMIT 1
                """,
                (agent_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return False
            session_id = int(row["id"])
            await conn.execute(
                """
                UPDATE pauses
                   SET status = 'cancelled', answered_at = now()
                 WHERE session_id = %s AND status = 'waiting'
                """,
                (session_id,),
            )
            await conn.execute(
                """
                UPDATE sessions
                   SET status = 'cancelled', finished_at = now(),
                       live_jti = NULL, live_expires_at = NULL
                 WHERE id = %s
                """,
                (session_id,),
            )
            return True

    async def answer_pause(
        self, pause_id: int, message_id: int, *, apply: bool
    ) -> None:
        async with self.pool.connection() as conn:
            if not apply:
                # Reply stored; pause stays waiting until connector is online.
                return
            cur = await conn.execute(
                """
                UPDATE pauses
                   SET status = 'answered',
                       answered_at = now(),
                       answered_message_id = %s
                 WHERE id = %s AND status = 'waiting'
                RETURNING session_id
                """,
                (message_id, pause_id),
            )
            row = await cur.fetchone()
            if row is None:
                return
            await conn.execute(
                """
                UPDATE sessions SET status = 'running'
                 WHERE id = %s AND status = 'paused'
                """,
                (row["session_id"],),
            )

    # ------------------------------------------------------------ connector

    async def heartbeat(self, computer_id: int) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE computers SET last_seen_at = now()
                 WHERE id = %s
                RETURNING *
                """,
                (computer_id,),
            )
            computer = await cur.fetchone()
            if computer is None:
                return None
            await conn.execute(
                """
                UPDATE sessions SET heartbeat_at = now()
                 WHERE computer_id = %s AND status IN ('running', 'paused')
                """,
                (computer_id,),
            )
            return dict(computer)

    async def computer_for_credential(self, credential: str) -> dict[str, Any] | None:
        digest = _hash_credential(credential)
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT c.*
                  FROM computer_credentials cc
                  JOIN computers c ON c.id = cc.computer_id
                 WHERE cc.credential_hash = %s
                """,
                (digest,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    async def expire_stale_pauses(self, *, older_than: timedelta = PAUSE_TIMEOUT) -> int:
        cutoff = datetime.now(timezone.utc) - older_than
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT p.id, p.session_id
                  FROM pauses p
                  JOIN sessions s ON s.id = p.session_id
                 WHERE p.status = 'waiting' AND p.created_at < %s
                   AND s.status = 'paused'
                """,
                (cutoff,),
            )
            rows = await cur.fetchall()
            for row in rows:
                await conn.execute(
                    """
                    UPDATE pauses SET status = 'cancelled', answered_at = now()
                     WHERE id = %s
                    """,
                    (row["id"],),
                )
                await conn.execute(
                    """
                    UPDATE sessions
                       SET status = 'cancelled', finished_at = now(),
                           live_jti = NULL, live_expires_at = NULL
                     WHERE id = %s
                    """,
                    (row["session_id"],),
                )
            return len(rows)

    # ---------------------------------------------------------- live desktop

    async def set_live_link(
        self, session_id: int, *, jti: str, expires_at: datetime
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE sessions
                   SET live_jti = %s, live_expires_at = %s
                 WHERE id = %s
                """,
                (jti, expires_at, session_id),
            )

    async def get_session_by_live_jti(self, jti: str) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM sessions WHERE live_jti = %s",
                (jti,),
            )
            row = await cur.fetchone()
            return dict(row) if row else None

    # ----------------------------------------------------------- bot tokens

    async def bind_bot(
        self, agent_id: int, *, token: str, username: str, telegram_bot_id: int
    ) -> dict[str, Any]:
        enc = _encrypt_token(token)
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO bot_tokens (agent_id, token_enc)
                VALUES (%s, %s)
                ON CONFLICT (agent_id) DO UPDATE
                  SET token_enc = EXCLUDED.token_enc, updated_at = now()
                """,
                (agent_id, enc),
            )
            cur = await conn.execute(
                """
                UPDATE bots
                   SET username = %s, telegram_bot_id = %s
                 WHERE agent_id = %s
                RETURNING *
                """,
                (username, telegram_bot_id, agent_id),
            )
            return dict(await cur.fetchone())  # type: ignore[arg-type]

    async def unbind_bot(self, agent_id: int) -> bool:
        async with self.pool.connection() as conn:
            await conn.execute("DELETE FROM bot_tokens WHERE agent_id = %s", (agent_id,))
            cur = await conn.execute(
                """
                UPDATE bots
                   SET chat_id = NULL, username = NULL, telegram_bot_id = NULL
                 WHERE agent_id = %s
                RETURNING id
                """,
                (agent_id,),
            )
            return await cur.fetchone() is not None

    # -------------------------------------------------------- session revoke

    async def revoke_factory_jti(self, jti: str, expires_at: datetime) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO factory_session_revocations (jti, expires_at)
                VALUES (%s, %s)
                ON CONFLICT (jti) DO NOTHING
                """,
                (jti, expires_at),
            )

    async def is_factory_jti_revoked(self, jti: str) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM factory_session_revocations WHERE jti = %s",
                (jti,),
            )
            return await cur.fetchone() is not None


def split_list_row(row: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    agent = {
        "id": row["id"],
        "computer_id": row["computer_id"],
        "name": row["name"],
        "description": row["description"],
        "avatar_color": row["avatar_color"],
        "tags": row["tags"],
    }
    computer = {
        "id": row["c_id"],
        "name": row["c_name"],
        "kind": row["c_kind"],
        "provider": row["c_provider"],
        "last_seen_at": row["c_last_seen_at"],
    }
    open_session = None
    if row.get("s_id") is not None:
        open_session = {
            "id": row["s_id"],
            "title": row["s_title"],
            "status": row["s_status"],
        }
    return agent, computer, open_session


def _hash_credential(credential: str) -> str:
    return hashlib.sha256(credential.encode()).hexdigest()


def _encrypt_token(token: str) -> str:
    # Placeholder secret store: not plaintext in bots, but reversible in-process
    # until a real KMS is wired. Obfuscated with a fixed app salt.
    salt = b"control-machine-bot-token-v1"
    return hmac.new(salt, token.encode(), hashlib.sha256).hexdigest() + ":" + token.encode().hex()


def is_online(last_seen_at: datetime | None) -> bool:
    return computer_online(last_seen_at)


def has_open_session(status: str | None) -> bool:
    return bool(status) and session_is_open(str(status))
