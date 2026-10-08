-- Factory UI. Every primary key is a bigint identity: 1, 2, 3.
-- One agent controls one computer. Bot tokens, the machine credential,
-- passwords, and bridge keystrokes are not columns.

create table computers (
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

create table agents (
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

create table bots (
    id               bigint generated always as identity primary key,
    agent_id         bigint not null unique references agents (id) on delete cascade,
    username         text,
    telegram_bot_id  bigint,
    chat_id          bigint
);

create table skills (
    id          bigint generated always as identity primary key,
    agent_id    bigint not null references agents (id) on delete cascade,
    name        text not null,
    description text not null default '',
    body        text not null,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create table memories (
    id         bigint generated always as identity primary key,
    agent_id   bigint not null references agents (id) on delete cascade,
    body       text not null,
    detail     text not null default '',
    created_at timestamptz not null default now()
);

create index memories_agent_idx on memories (agent_id, created_at desc);

create table agent_tools (
    id       bigint generated always as identity primary key,
    agent_id bigint not null references agents (id) on delete cascade,
    name     text not null check (name in ('browser', 'clicks', 'files', 'sap')),
    enabled  boolean not null default false,
    unique (agent_id, name)
);

create table sessions (
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

-- One open intent for this agent, on its computer. The two indexes are one lock.
create unique index sessions_one_open_per_agent
    on sessions (agent_id)
    where status in ('running', 'paused');

create unique index sessions_one_open_per_computer
    on sessions (computer_id)
    where status in ('running', 'paused');

create unique index sessions_one_live_link
    on sessions (live_jti)
    where live_jti is not null;

create index sessions_agent_idx on sessions (agent_id, started_at desc);

create table messages (
    id         bigint generated always as identity primary key,
    agent_id   bigint not null references agents (id) on delete cascade,
    session_id bigint references sessions (id) on delete set null,
    pause_id   bigint,
    role       text not null check (role in ('user', 'agent')),
    body       text not null,
    created_at timestamptz not null default now()
);

create index messages_agent_idx on messages (agent_id, created_at);

create table pauses (
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

create unique index pauses_one_waiting
    on pauses (session_id)
    where status = 'waiting';

-- The bubble that announced the pause. The reply that releases it stays
-- pauses.answered_message_id.
alter table messages
    add constraint messages_pause_id_fkey
    foreign key (pause_id) references pauses (id);

-- Server state, not a Factory table. One person, one Factory: logout must
-- be visible to every replica. The bearer itself is not stored.
create table factory_session_revocations (
    jti        text primary key,
    expires_at timestamptz not null
);

create table events (
    id         bigint generated always as identity primary key,
    agent_id   bigint not null references agents (id) on delete cascade,
    session_id bigint references sessions (id) on delete set null,
    kind       text not null check (kind in ('said', 'tried', 'decided')),
    title      text not null,
    body       text not null default '',
    created_at timestamptz not null default now()
);

create index events_agent_idx on events (agent_id, created_at desc);
