"""Factory tables. Source of truth: research/schema.sql."""

from __future__ import annotations

# Applied after the legacy dashboard schema. The dashboard skills table is
# renamed to dashboard_skills so this skills table can own the name.

FACTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS computers (
    id           bigint generated always as identity primary key,
    name         text not null,
    kind         text not null check (kind in ('mac', 'vm')),
    provider     text not null,
    last_seen_at timestamptz,
    check (
        (kind = 'mac' and provider = 'local')
        or (kind = 'vm' and provider <> 'local' and length(trim(provider)) > 0)
    )
);

CREATE TABLE IF NOT EXISTS agents (
    id                  bigint generated always as identity primary key,
    computer_id         bigint not null unique references computers (id),
    name                text not null,
    description         text not null default '',
    system_prompt       text not null default '',
    avatar_color        text not null default '#6d4dff',
    tags                text[] not null default '{}',
    memory_enabled      boolean not null default true,
    memory_preferences  jsonb not null default '[]',
    created_at          timestamptz not null default now(),
    unique (id, computer_id)
);

CREATE TABLE IF NOT EXISTS bots (
    id               bigint generated always as identity primary key,
    agent_id         bigint not null unique references agents (id) on delete cascade,
    username         text,
    telegram_bot_id  bigint,
    chat_id          bigint
);

CREATE TABLE IF NOT EXISTS skills (
    id          bigint generated always as identity primary key,
    agent_id    bigint not null references agents (id) on delete cascade,
    name        text not null,
    description text not null default '',
    body        text not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

CREATE TABLE IF NOT EXISTS memories (
    id         bigint generated always as identity primary key,
    agent_id   bigint not null references agents (id) on delete cascade,
    body       text not null,
    detail     text not null default '',
    created_at timestamptz not null default now()
);

CREATE INDEX IF NOT EXISTS memories_agent_idx ON memories (agent_id, created_at desc);

CREATE TABLE IF NOT EXISTS agent_tools (
    id       bigint generated always as identity primary key,
    agent_id bigint not null references agents (id) on delete cascade,
    name     text not null check (name in ('browser', 'clicks', 'files', 'sap')),
    enabled  boolean not null default false,
    unique (agent_id, name)
);

CREATE TABLE IF NOT EXISTS sessions (
    id              bigint generated always as identity primary key,
    agent_id        bigint not null,
    computer_id     bigint not null,
    title           text not null,
    status          text not null default 'running'
                    check (status in ('running', 'paused', 'done', 'error', 'cancelled')),
    skill_id        bigint references skills (id) on delete set null,
    page_url        text,
    page_title      text,
    live_jti        text,
    live_expires_at timestamptz,
    started_at      timestamptz not null default now(),
    heartbeat_at    timestamptz,
    finished_at     timestamptz,
    foreign key (agent_id, computer_id) references agents (id, computer_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_open_per_agent
    ON sessions (agent_id)
    WHERE status IN ('running', 'paused');

CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_open_per_computer
    ON sessions (computer_id)
    WHERE status IN ('running', 'paused');

CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_live_link
    ON sessions (live_jti)
    WHERE live_jti IS NOT NULL;

CREATE INDEX IF NOT EXISTS sessions_agent_idx ON sessions (agent_id, started_at desc);

CREATE TABLE IF NOT EXISTS messages (
    id         bigint generated always as identity primary key,
    agent_id   bigint not null references agents (id) on delete cascade,
    session_id bigint references sessions (id) on delete set null,
    pause_id   bigint,
    role       text not null check (role in ('user', 'agent')),
    body       text not null,
    created_at timestamptz not null default now()
);

CREATE INDEX IF NOT EXISTS messages_agent_idx ON messages (agent_id, created_at);

CREATE TABLE IF NOT EXISTS pauses (
    id                   bigint generated always as identity primary key,
    session_id           bigint not null references sessions (id) on delete cascade,
    question             text not null,
    status               text not null default 'waiting'
                         check (status in ('waiting', 'answered', 'cancelled')),
    will_do              jsonb not null default '[]',
    will_not             jsonb not null default '[]',
    created_at           timestamptz not null default now(),
    answered_at          timestamptz,
    answered_message_id  bigint references messages (id)
);

CREATE UNIQUE INDEX IF NOT EXISTS pauses_one_waiting
    ON pauses (session_id)
    WHERE status = 'waiting';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'messages_pause_id_fkey'
    ) THEN
        ALTER TABLE messages
            ADD CONSTRAINT messages_pause_id_fkey
            FOREIGN KEY (pause_id) REFERENCES pauses (id);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS factory_session_revocations (
    jti        text primary key,
    expires_at timestamptz not null
);

CREATE TABLE IF NOT EXISTS events (
    id         bigint generated always as identity primary key,
    agent_id   bigint not null references agents (id) on delete cascade,
    session_id bigint references sessions (id) on delete set null,
    kind       text not null check (kind in ('said', 'tried', 'decided')),
    title      text not null,
    body       text not null default '',
    created_at timestamptz not null default now()
);

CREATE INDEX IF NOT EXISTS events_agent_idx ON events (agent_id, created_at desc);

-- Server-side only. The plaintext connector credential is shown once and never stored.
CREATE TABLE IF NOT EXISTS computer_credentials (
    computer_id     bigint primary key references computers (id) on delete cascade,
    credential_hash text not null,
    created_at      timestamptz not null default now()
);

CREATE TABLE IF NOT EXISTS bot_tokens (
    agent_id    bigint primary key references agents (id) on delete cascade,
    token_enc   text not null,
    updated_at  timestamptz not null default now()
);
"""

# Rename the legacy dashboard skills table so Factory can own `skills`.
LEGACY_SKILLS_RENAME = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'skills' AND column_name = 'prompt'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name = 'skills' AND column_name = 'agent_id'
    ) THEN
        ALTER TABLE skills RENAME TO dashboard_skills;
        IF EXISTS (
            SELECT 1 FROM information_schema.table_constraints
             WHERE table_name = 'automations' AND constraint_name = 'automations_skill_id_fkey'
        ) THEN
            ALTER TABLE automations DROP CONSTRAINT automations_skill_id_fkey;
        END IF;
        IF EXISTS (
            SELECT 1 FROM information_schema.table_constraints
             WHERE table_name = 'tasks' AND constraint_name = 'tasks_skill_id_fkey'
        ) THEN
            ALTER TABLE tasks DROP CONSTRAINT tasks_skill_id_fkey;
        END IF;
        ALTER TABLE automations
            ADD CONSTRAINT automations_skill_id_fkey
            FOREIGN KEY (skill_id) REFERENCES dashboard_skills (id) ON DELETE SET NULL;
        ALTER TABLE tasks
            ADD CONSTRAINT tasks_skill_id_fkey
            FOREIGN KEY (skill_id) REFERENCES dashboard_skills (id) ON DELETE SET NULL;
    END IF;
END $$;
"""
