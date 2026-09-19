"""Postgres persistence.

Three things live in Postgres:

- LangGraph checkpoints (``AsyncPostgresSaver``), so a conversation thread can be resumed
  and a human-in-the-loop interrupt can be answered later.
- The LangGraph store (``AsyncPostgresStore``), which backs the agent's ``/memories/``
  filesystem route and therefore survives across threads.
- Our own ``tasks`` / ``task_steps`` / ``skills`` / ``automations`` tables. Checkpoint
  rows are opaque blobs, so the dashboard needs plain queryable rows to render history,
  reusable skills, and scheduled runs.
"""

from __future__ import annotations

import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.store.postgres.aio import AsyncPostgresStore
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            BIGSERIAL PRIMARY KEY,
    thread_id     TEXT        NOT NULL,
    title         TEXT        NOT NULL,
    prompt        TEXT        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'queued',
    slot_id       INTEGER,
    step_count    INTEGER     NOT NULL DEFAULT 0,
    result        TEXT,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS slot_id INTEGER;

CREATE INDEX IF NOT EXISTS tasks_thread_id_idx ON tasks (thread_id);
CREATE INDEX IF NOT EXISTS tasks_created_at_idx ON tasks (created_at DESC);

CREATE TABLE IF NOT EXISTS task_steps (
    id              BIGSERIAL PRIMARY KEY,
    task_id         BIGINT      NOT NULL REFERENCES tasks (id) ON DELETE CASCADE,
    seq             INTEGER     NOT NULL,
    kind            TEXT        NOT NULL,
    tool_name       TEXT,
    args_json       JSONB,
    result_summary  TEXT,
    screenshot_path TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS task_steps_task_id_seq_idx ON task_steps (task_id, seq);

CREATE TABLE IF NOT EXISTS skills (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT        NOT NULL,
    description   TEXT        NOT NULL DEFAULT '',
    prompt        TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS automations (
    id            BIGSERIAL PRIMARY KEY,
    name          TEXT        NOT NULL,
    description   TEXT        NOT NULL DEFAULT '',
    skill_id      BIGINT      REFERENCES skills (id) ON DELETE SET NULL,
    prompt        TEXT        NOT NULL DEFAULT '',
    schedule      TEXT        NOT NULL,
    enabled       BOOLEAN     NOT NULL DEFAULT TRUE,
    last_run_at   TIMESTAMPTZ,
    last_task_id  BIGINT,
    last_error    TEXT,
    next_run_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS automations_due_idx ON automations (enabled, next_run_at);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS skill_id BIGINT REFERENCES skills (id) ON DELETE SET NULL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS automation_id BIGINT REFERENCES automations (id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS telegram_threads (
    chat_id       BIGINT      PRIMARY KEY,
    thread_id     TEXT        NOT NULL,
    last_task_id  BIGINT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS telegram_threads_task_idx ON telegram_threads (last_task_id);
CREATE INDEX IF NOT EXISTS telegram_threads_thread_idx ON telegram_threads (thread_id);
"""


@dataclass
class Database:
    """Owns the connection pool, the checkpointer and the store for the process lifetime."""

    pool: AsyncConnectionPool
    checkpointer: AsyncPostgresSaver
    store: AsyncPostgresStore
    _stack: AsyncExitStack

    @classmethod
    async def connect(cls) -> Database:
        settings = get_settings()
        stack = AsyncExitStack()

        # LangGraph's helpers want a plain conn string; our own queries use a separate pool.
        checkpointer = await stack.enter_async_context(
            AsyncPostgresSaver.from_conn_string(settings.database_url)
        )
        store = await stack.enter_async_context(
            AsyncPostgresStore.from_conn_string(settings.database_url)
        )
        await checkpointer.setup()
        await store.setup()

        pool = AsyncConnectionPool(
            conninfo=settings.database_url,
            min_size=1,
            max_size=8,
            open=False,
            kwargs={"row_factory": dict_row, "autocommit": True},
        )
        await stack.enter_async_context(pool)
        await pool.wait()

        async with pool.connection() as conn:
            await conn.execute(SCHEMA)

        return cls(pool=pool, checkpointer=checkpointer, store=store, _stack=stack)

    async def close(self) -> None:
        await self._stack.aclose()

    # ---------------------------------------------------------------- tasks

    async def create_task(
        self,
        *,
        thread_id: str,
        title: str,
        prompt: str,
        skill_id: int | None = None,
        automation_id: int | None = None,
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO tasks (thread_id, title, prompt, status, skill_id, automation_id)
                VALUES (%s, %s, %s, 'running', %s, %s)
                RETURNING *
                """,
                (thread_id, title, prompt, skill_id, automation_id),
            )
            return _row(await cur.fetchone())

    async def finish_task(
        self,
        task_id: int,
        *,
        status: str,
        result: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE tasks
                   SET status = %s, result = %s, error = %s, finished_at = now()
                 WHERE id = %s
                """,
                (status, result, error, task_id),
            )

    async def reconcile_orphans(self) -> int:
        """Close out runs that died with a previous process.

        A run only exists as an asyncio task, so anything still marked 'running' or 'paused' at
        startup was cut off mid-flight and will never finish. Tasks parked at
        'awaiting_approval' are left alone: their checkpoint survives, so answering the
        approval still resumes them.
        """
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE tasks
                   SET status = 'error',
                       error = 'Interrupted: the server stopped while this task was running.',
                       finished_at = now()
                 WHERE status IN ('running', 'paused')
                """
            )
            return cur.rowcount

    async def set_task_status(self, task_id: int, status: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE tasks SET status = %s WHERE id = %s", (status, task_id))

    async def set_task_slot(self, task_id: int, slot_id: int) -> None:
        async with self.pool.connection() as conn:
            await conn.execute("UPDATE tasks SET slot_id = %s WHERE id = %s", (slot_id, task_id))

    async def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"{_TASK_SELECT} ORDER BY tasks.created_at DESC LIMIT %s",
                (limit,),
            )
            return [_row(r) for r in await cur.fetchall()]

    async def list_conversations(self, limit: int = 80) -> list[dict[str, Any]]:
        """One row per thread: the latest task, plus how many turns it contains."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""
                SELECT * FROM (
                    SELECT DISTINCT ON (tasks.thread_id)
                           {_TASK_COLUMNS},
                           (SELECT COUNT(*) FROM tasks t2
                             WHERE t2.thread_id = tasks.thread_id) AS task_count
                      FROM tasks
                      LEFT JOIN skills ON skills.id = tasks.skill_id
                      LEFT JOIN automations ON automations.id = tasks.automation_id
                     ORDER BY tasks.thread_id, tasks.created_at DESC, tasks.id DESC
                ) latest
                ORDER BY latest.created_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            return [_row(r) for r in await cur.fetchall()]

    async def list_thread_tasks(self, thread_id: str) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"{_TASK_SELECT} WHERE tasks.thread_id = %s "
                "ORDER BY tasks.created_at ASC, tasks.id ASC",
                (thread_id,),
            )
            return [_row(r) for r in await cur.fetchall()]

    async def get_task(self, task_id: int) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"{_TASK_SELECT} WHERE tasks.id = %s",
                (task_id,),
            )
            row = await cur.fetchone()
            return _row(row) if row else None

    async def get_running_task(self) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM tasks
                 WHERE status IN ('running', 'awaiting_approval')
                 ORDER BY created_at DESC LIMIT 1
                """
            )
            row = await cur.fetchone()
            return _row(row) if row else None

    # ----------------------------------------------------------- task steps

    async def add_step(
        self,
        task_id: int,
        *,
        kind: str,
        tool_name: str | None = None,
        args: dict[str, Any] | None = None,
        result_summary: str | None = None,
        screenshot_path: str | None = None,
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO task_steps
                       (task_id, seq, kind, tool_name, args_json, result_summary, screenshot_path)
                VALUES (%s,
                        (SELECT COALESCE(MAX(seq), 0) + 1 FROM task_steps WHERE task_id = %s),
                        %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    task_id,
                    task_id,
                    kind,
                    tool_name,
                    json.dumps(args) if args is not None else None,
                    result_summary,
                    screenshot_path,
                ),
            )
            step = _row(await cur.fetchone())
            await conn.execute(
                "UPDATE tasks SET step_count = step_count + 1 WHERE id = %s", (task_id,)
            )
            return step

    async def list_steps(self, task_id: int) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM task_steps WHERE task_id = %s ORDER BY seq", (task_id,)
            )
            return [_row(r) for r in await cur.fetchall()]

    # ---------------------------------------------------------------- skills

    async def list_skills(self) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM skills ORDER BY updated_at DESC, id DESC"
            )
            return [_row(r) for r in await cur.fetchall()]

    async def get_skill(self, skill_id: int) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM skills WHERE id = %s", (skill_id,))
            row = await cur.fetchone()
            return _row(row) if row else None

    async def create_skill(
        self, *, name: str, description: str, prompt: str
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO skills (name, description, prompt)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (name, description, prompt),
            )
            return _row(await cur.fetchone())

    async def update_skill(
        self, skill_id: int, *, name: str, description: str, prompt: str
    ) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE skills
                   SET name = %s, description = %s, prompt = %s, updated_at = now()
                 WHERE id = %s
                RETURNING *
                """,
                (name, description, prompt, skill_id),
            )
            row = await cur.fetchone()
            return _row(row) if row else None

    async def delete_skill(self, skill_id: int) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM skills WHERE id = %s RETURNING id", (skill_id,)
            )
            return await cur.fetchone() is not None

    # ----------------------------------------------------------- automations

    async def list_automations(self) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"{_AUTOMATION_SELECT} ORDER BY automations.updated_at DESC, automations.id DESC"
            )
            return [_row(r) for r in await cur.fetchall()]

    async def get_automation(self, automation_id: int) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"{_AUTOMATION_SELECT} WHERE automations.id = %s",
                (automation_id,),
            )
            row = await cur.fetchone()
            return _row(row) if row else None

    async def create_automation(
        self,
        *,
        name: str,
        description: str,
        skill_id: int | None,
        prompt: str,
        schedule: str,
        enabled: bool,
        next_run_at: datetime | None,
    ) -> dict[str, Any]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                INSERT INTO automations
                    (name, description, skill_id, prompt, schedule, enabled, next_run_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (name, description, skill_id, prompt, schedule, enabled, next_run_at),
            )
            created = await cur.fetchone()
        return await self.get_automation(int(created["id"]))  # type: ignore[index]

    async def update_automation(
        self,
        automation_id: int,
        *,
        name: str,
        description: str,
        skill_id: int | None,
        prompt: str,
        schedule: str,
        enabled: bool,
        next_run_at: datetime | None,
    ) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                UPDATE automations
                   SET name = %s,
                       description = %s,
                       skill_id = %s,
                       prompt = %s,
                       schedule = %s,
                       enabled = %s,
                       next_run_at = %s,
                       last_error = NULL,
                       updated_at = now()
                 WHERE id = %s
                RETURNING id
                """,
                (
                    name,
                    description,
                    skill_id,
                    prompt,
                    schedule,
                    enabled,
                    next_run_at,
                    automation_id,
                ),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return await self.get_automation(automation_id)

    async def delete_automation(self, automation_id: int) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM automations WHERE id = %s RETURNING id", (automation_id,)
            )
            return await cur.fetchone() is not None

    async def due_automations(self) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""
                {_AUTOMATION_SELECT}
                 WHERE automations.enabled = TRUE
                   AND automations.next_run_at IS NOT NULL
                   AND automations.next_run_at <= now()
                 ORDER BY automations.next_run_at
                """
            )
            return [_row(r) for r in await cur.fetchall()]

    async def mark_automation_ran(
        self, automation_id: int, *, task_id: int, next_run: datetime
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE automations
                   SET last_run_at = now(),
                       last_task_id = %s,
                       last_error = NULL,
                       next_run_at = %s,
                       updated_at = now()
                 WHERE id = %s
                """,
                (task_id, next_run, automation_id),
            )

    async def set_automation_error(
        self, automation_id: int, error: str, *, disable: bool = False
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                UPDATE automations
                   SET last_error = %s,
                       enabled = CASE WHEN %s THEN FALSE ELSE enabled END,
                       updated_at = now()
                 WHERE id = %s
                """,
                (error, disable, automation_id),
            )

    # -------------------------------------------------------------- telegram

    async def upsert_telegram_thread(
        self, chat_id: int, *, thread_id: str, task_id: int
    ) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """
                INSERT INTO telegram_threads (chat_id, thread_id, last_task_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (chat_id) DO UPDATE
                   SET thread_id = EXCLUDED.thread_id,
                       last_task_id = EXCLUDED.last_task_id,
                       updated_at = now()
                """,
                (chat_id, thread_id, task_id),
            )

    async def get_telegram_thread(self, chat_id: int) -> dict[str, Any] | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM telegram_threads WHERE chat_id = %s",
                (chat_id,),
            )
            row = await cur.fetchone()
            return _row(row) if row else None

    async def clear_telegram_thread(self, chat_id: int) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "DELETE FROM telegram_threads WHERE chat_id = %s",
                (chat_id,),
            )

    async def get_telegram_chat_for_task(self, task_id: int) -> int | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT chat_id FROM telegram_threads
                 WHERE last_task_id = %s
                 LIMIT 1
                """,
                (task_id,),
            )
            row = await cur.fetchone()
            if row:
                return int(row["chat_id"])
            cur = await conn.execute(
                """
                SELECT telegram_threads.chat_id
                  FROM telegram_threads
                  JOIN tasks ON tasks.thread_id = telegram_threads.thread_id
                 WHERE tasks.id = %s
                 LIMIT 1
                """,
                (task_id,),
            )
            row = await cur.fetchone()
            return int(row["chat_id"]) if row else None


_TASK_COLUMNS = """
tasks.*,
skills.name AS skill_name,
automations.name AS automation_name
"""

_TASK_SELECT = f"""
SELECT {_TASK_COLUMNS}
  FROM tasks
  LEFT JOIN skills ON skills.id = tasks.skill_id
  LEFT JOIN automations ON automations.id = tasks.automation_id
"""

_AUTOMATION_SELECT = """
SELECT automations.*,
       skills.name AS skill_name,
       skills.prompt AS skill_prompt
  FROM automations
  LEFT JOIN skills ON skills.id = automations.skill_id
"""


def _row(row: dict[str, Any] | None) -> dict[str, Any]:
    """Make a row JSON-serialisable for the dashboard."""
    if row is None:
        return {}
    out: dict[str, Any] = {}
    for key, value in row.items():
        out[key] = value.isoformat() if isinstance(value, datetime) else value
    return out
