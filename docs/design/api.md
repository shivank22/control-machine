# API contract

JSON over HTTPS for the Factory shell, the Telegram page, and the live desktop. Ids are integers, the same `bigint` keys as [`research/schema.sql`](../../research/schema.sql).

This is the contract for [`research/index.html`](../../research/index.html), [`research/chat.html`](../../research/chat.html), and [`research/desktop.html`](../../research/desktop.html). It is not the dashboard API in `src/control_machine/web/app.py`.

```
Authorization: Bearer <session>
Content-Type: application/json
```

Who that session is, what it may call, and how the Telegram allowlist and the signed desktop link differ are in [Security](security.md).

Errors use one shape. The status is the HTTP status.

```json
{ "detail": "This agent already has an open session." }
```

| Status | When |
| --- | --- |
| 400 | The body fails a check (`name` empty, unknown tool). |
| 401 | The bearer is missing, bad, or expired, or the Factory access key was not accepted. |
| 404 | The id is not an agent in this Factory. |
| 409 | This agent already has an open session, or a desktop link was asked with no open session. |
| 503 | A desktop link was asked while that computer is offline. |

A new message while the computer is offline is not `409` and not `503`. See [Messages](#telegram).

## Session

One person owns this Factory. There is no user table and no workspace table. `POST /api/session` exchanges that person’s access key for a bearer. The key and the signing secret are server config (`factory_access_key`, `factory_session_secret`). They are not columns. Login is refused when either value is empty. There is no fallback key.

```json
{ "key": "…" }
```

`201`:

```json
{ "token": "…", "expires_at": "2026-09-30T21:00:00Z" }
```

The default lifetime is 12 hours (`factory_session_ttl_seconds`). The token is an HMAC of its expiry and a `jti`. `GET /api/session` returns `{ "expires_at" }` for the bearer on the request. `DELETE /api/session` writes that `jti` to `factory_session_revocations` so every replica rejects it. Both require the header above. A missing bearer is `{ "detail": "Missing session." }`. A bad, expired, or revoked bearer is `{ "detail": "Session is invalid or expired." }`.

The desktop socket does not accept this bearer. Telegram does not present it. The dashboard routes in `src/control_machine/web/app.py` are a different API and do not use this session.

The shell can poll `GET /api/agents/{agent_id}` every few seconds for the run bar, the pause badge, and the connection pills. Chat polls `GET /api/agents/{agent_id}/messages`.

**Templates** and **Add node** have no endpoints. The canvas is assembled from the agent, its skill, and its tools.

## Columns the pages read

These are columns in [`research/schema.sql`](../../research/schema.sql), not a second storage choice.

| Page | Column |
| --- | --- |
| Machine, live desktop, Connections | `sessions.page_url`, `sessions.page_title` |
| Skills “In use” | `sessions.skill_id` → `skills.id`. The agent sets it when it follows a skill. |
| Builder memory panel | `agents.memory_enabled`, `agents.memory_preferences` |
| Pause | `pauses.will_do`, `pauses.will_not` |
| Memory card, second line | `memories.detail` |
| Agent bubble while a pause is waiting | `messages.pause_id` → `pauses.id` |

`memory_preferences` is a list of `{ "key", "label", "enabled" }`. `will_do` and `will_not` are lists of `{ "title", "detail" }`. The agent writes them when it pauses, so Sales and Office do not share the timesheet copy. The reply that releases the pause is `pauses.answered_message_id`, not `messages.pause_id`.

## Factory

The Factory lists agents. Each card is one agent and the one computer it controls.

`GET /api/agents`

```json
[
  {
    "id": 1,
    "name": "Office",
    "description": "Fills the timesheet and other desk work. One Telegram bot.",
    "avatar_color": "#6d4dff",
    "tags": ["timesheet"],
    "status": "paused",
    "session_title": "Fill this week’s MyWipro timesheet",
    "computer": {
      "id": 1,
      "name": "This Mac",
      "kind": "mac",
      "provider": "local",
      "status": "online",
      "last_seen_at": "2026-09-30T20:46:12Z"
    }
  },
  {
    "id": 2,
    "name": "Sales",
    "description": "Works the pipeline in the browser.",
    "avatar_color": "#2563eb",
    "tags": ["pipeline"],
    "status": "idle",
    "session_title": null,
    "computer": {
      "id": 2,
      "name": "Sales desk",
      "kind": "vm",
      "provider": "daytona",
      "status": "offline",
      "last_seen_at": null
    }
  }
]
```

`computer.status` is `online` when that computer’s `last_seen_at` is within 30 seconds, otherwise `offline`. `agent.status` is `paused` or `running` when this agent has the open session, otherwise `idle`. Office being paused does not block Sales. Sales has its own computer.

`kind` is `mac` or `vm`. A Mac has `provider` `local`. A VM has `provider` `daytona`, `azure`, or another name. The provider is where that VM runs. It is not a second control path.

`POST /api/agents` is **New agent**. It creates the agent and the one computer it controls.

```json
{
  "name": "Support",
  "description": "Answers the inbox and asks before it sends.",
  "computer": { "name": "This Mac", "kind": "mac" }
}
```

A VM is the same call with `"kind": "vm"` and `"provider": "azure"`. `kind` `mac` stores `provider` `local`. `kind` `vm` requires a provider other than `local`. A missing name, a bad `kind`, or a VM without a provider is `400`.

Response `201` is the agent object from the next section. The insert also creates the bot row and four tool rows: `browser`, `clicks`, and `files` enabled, `sap` disabled, `memory_enabled` true, preferences empty. `files` is the file specialist.

## Agent

`GET /api/agents/{agent_id}` loads the header, the builder, the run bar, and the three panels in one response.

```json
{
  "id": 1,
  "computer_id": 1,
  "name": "Office",
  "description": "One agent, one Telegram bot.",
  "system_prompt": "You are Office…",
  "avatar_color": "#6d4dff",
  "tags": ["telegram", "timesheet", "browser"],
  "memory_enabled": true,
  "memory_preferences": [
    { "key": "conversation", "label": "Conversation in Telegram", "enabled": true },
    { "key": "confirm_submit", "label": "Do not submit until confirmed", "enabled": true },
    { "key": "last_project", "label": "Last week’s project code", "enabled": true },
    { "key": "summarize_older", "label": "Summarize older runs", "enabled": false }
  ],
  "bot": { "id": 1, "username": "office_bot", "connected": true },
  "computer": {
    "id": 1,
    "name": "This Mac",
    "kind": "mac",
    "provider": "local",
    "status": "online",
    "last_seen_at": "2026-09-30T20:46:12Z"
  },
  "tools": [
    { "name": "browser", "enabled": true },
    { "name": "clicks", "enabled": true },
    { "name": "files", "enabled": true },
    { "name": "sap", "enabled": false }
  ],
  "session": {
    "id": 4,
    "title": "Fill this week’s MyWipro timesheet",
    "status": "paused",
    "skill_id": 2,
    "page_url": "https://mywipro.example/sign-in",
    "page_title": "Sign in",
    "started_at": "2026-09-30T16:14:00Z",
    "heartbeat_at": "2026-09-30T16:15:12Z"
  },
  "pause": {
    "id": 3,
    "question": "Sign in to MyWipro",
    "status": "waiting",
    "created_at": "2026-09-30T16:15:00Z",
    "will_do": [
      { "title": "Read the page again", "detail": "Only after you say you are past sign-in." }
    ],
    "will_not": [
      { "title": "Type the password", "detail": "Keystrokes on the live desktop are not stored." }
    ]
  }
}
```

`bot.connected` is true when `bots.chat_id` is set. `session` and `pause` are `null` when the agent is idle. `pause` is present only while `status` is `waiting`.

`PATCH /api/agents/{agent_id}` is **Save**, the color swatches, the memory switch, and the memory checkboxes. Send only the fields that changed.

```json
{
  "name": "Office",
  "description": "…",
  "system_prompt": "…",
  "avatar_color": "#6d4dff",
  "tags": ["telegram", "timesheet"],
  "memory_enabled": true,
  "memory_preferences": [
    { "key": "conversation", "label": "Conversation in Telegram", "enabled": true }
  ]
}
```

`PATCH /api/agents/{agent_id}/tools/{name}` is a tool switch. `name` is `browser`, `clicks`, `files`, or `sap`. A disabled tool is not called.

```json
{ "enabled": false }
```

There is no route that sets the computer online or offline. Online means that agent’s `computers.last_seen_at` is within 30 seconds.

## Memory

`GET /api/agents/{agent_id}/memories`

```json
[
  {
    "id": 8,
    "body": "Timesheets are filed in MyWipro, usually on Friday.",
    "detail": "Learned from the timesheet skill and last week’s run.",
    "created_at": "2026-09-12T09:00:00Z"
  }
]
```

`detail` is `memories.detail`, the second line on the card.

`DELETE /api/agents/{agent_id}/memories/{memory_id}` is **Delete**. Response `204`.

The Memory screen has no create button. The agent inserts a note when it learns one:

`POST /api/agents/{agent_id}/memories`

```json
{
  "body": "Timesheets are filed in MyWipro, usually on Friday.",
  "detail": "Learned from the timesheet skill and last week’s run."
}
```

`201` returns the memory object. This call is the agent’s write. The person does not use it from the Memory screen.

## Skills

`GET /api/agents/{agent_id}/skills`

```json
[
  {
    "id": 2,
    "name": "Timesheet",
    "description": "When filling MyWipro efforts.",
    "body": "Fill MyWipro efforts in the browser. Hand login and submit back to the user.",
    "in_use": true
  }
]
```

`in_use` is true when the open session’s `skill_id` equals this id.

`POST /api/agents/{agent_id}/skills` is **Save skill**. `201`.

```json
{
  "name": "Inbox",
  "description": "When I ask you to check mail",
  "body": "Tell the agent what to do, and where it must stop and ask."
}
```

`description` is “When to use it”. `body` is “Instructions”.

`PATCH /api/agents/{agent_id}/skills/{skill_id}` edits the same three fields. Send only the fields that changed. `200` returns the skill.

`DELETE /api/agents/{agent_id}/skills/{skill_id}` is **Delete**. Response `204`. An open session whose `skill_id` was this skill has that column set to null.

The agent chooses the skill by setting `sessions.skill_id` on the open session. There is no separate choose route.

## Activity

`GET /api/agents/{agent_id}/events`

Optional query `session_id`. With no query, return events for the open session, or the latest session if the agent is idle.

```json
[
  {
    "id": 20,
    "session_id": 4,
    "kind": "decided",
    "title": "Pause. Do not type the password.",
    "body": "The page is a sign-in wall. Waiting for a reply in Telegram.",
    "created_at": "2026-09-30T16:15:00Z"
  }
]
```

`kind` is `said`, `tried`, or `decided`. Newest last, so the page can render top to bottom.

## Pause

The Pause screen uses `pause` and `session` from `GET /api/agents/{agent_id}`. It does not approve the step. There is no decide route on this page.

**Open the chat** is the messages resource below. **Open desktop** is the desktop link below.

## Machine

The Machine screen uses the same agent response: `computer`, `tools`, and `session.page_url` / `session.page_title`.

| Card | Source |
| --- | --- |
| Connector | `computer.status`, `computer.last_seen_at`, `computer.kind`, `computer.provider` |
| Machine clicks | `tools` where `name` is `clicks`, and whether `session` is using the browser instead |
| SAP | `tools` where `name` is `sap` |
| Browser | `session.page_url`, `session.page_title`, or hidden when `session` is null |

**Watch / Open desktop** is the desktop link.

## Telegram

`GET /api/agents/{agent_id}/messages`

```json
[
  {
    "id": 11,
    "role": "user",
    "body": "Fill this week’s timesheet in MyWipro.",
    "created_at": "2026-09-30T16:14:00Z",
    "pause_id": null
  },
  {
    "id": 12,
    "role": "agent",
    "body": "The page is the MyWipro sign-in. I did not type a password.",
    "created_at": "2026-09-30T16:15:00Z",
    "pause_id": 3
  }
]
```

When `pause_id` is set and that pause is still `waiting`, the bubble shows **Open desktop**.

`POST /api/agents/{agent_id}/messages` is the compose box, and it is how a pause is answered.

```json
{ "body": "Done. I’m on the efforts page." }
```

`201` returns the saved user message. The agent’s next reply appears on a later GET. It is not in this response.

What this call does depends on the open session and the connector:

| Open session | Computer | Result |
| --- | --- | --- |
| None | Offline | `201`. The user message is saved. The agent replies that it will not start machine work. No session is inserted. |
| None | Online | `201`. The user message is saved. A `running` session is inserted on this agent’s computer. |
| `running` | Either | `409`, `{ "detail": "This agent already has an open session." }`. The message is not saved. |
| `paused`, pause `waiting` | Online | `201`. The message is saved as `answered_message_id`. The pause becomes `answered`. The session returns to `running`. |
| `paused`, pause `waiting` | Offline | `201`. The message is saved on that session. The pause stays `waiting` and the session stays `paused`. When the connector is seen again, the Factory applies this reply: the pause becomes `answered` and the session returns to `running`. |

`409` is only the already-open case. The computer being asleep is the `201` rows above, not `409`. Another agent cannot take this computer. Its own session is a different row.

`POST /api/agents/{agent_id}/session/cancel` releases the lock. `204`. The open session becomes `cancelled`, `finished_at` is set, and a `waiting` pause becomes `cancelled` with `answered_message_id` null. `409` when there is no open session. The Factory does the same when a `waiting` pause is older than 24 hours, so one unanswered pause cannot keep the computer.

The agent writes `done` when the work finishes and `error` when it fails. Both set `finished_at`. Those three terminal statuses are the writers that free `sessions_one_open_per_agent` and `sessions_one_open_per_computer`.

`GET /api/agents/{agent_id}/messages` is scoped to that agent’s bot. Sales and Office do not share a thread, and they do not share a computer.

## Live desktop

The phone watches that agent’s computer through JPEG frames on a signed WebSocket. There is no VNC listener on the computer and no WebRTC peer connection. noVNC is only the localhost view of a browser container.

`POST /api/agents/{agent_id}/desktop` mints a link bound to the open session. `201`.

```json
{ "url": "https://host/desktop?t=signed-token", "expires_at": "2026-09-30T16:30:00Z" }
```

The token is HMAC-SHA256 over `session_id`, expiry, and a new `jti`. That `jti` and expiry are stored on the open session (`live_jti`, `live_expires_at`). Minting again replaces `live_jti`, which revokes the previous link for every replica. The signing key is `live_link_secret`. The call is refused, and nothing is signed, when that key is empty. There is no fallback to the bot token or to a fixed string. Lifetime is 15 minutes.

`409` when the agent has no open session. `503` when `computer.status` is `offline`. The link streams that agent’s computer. The noVNC view inside a browser container is not this route and is not served on the public listener.

The page connects to the existing stream:

`GET wss://host/live/desktop/stream?t=signed-token`

Server → client:

- Text JSON on open: `{ "ready": true, "page": 1, "pages": 2 }`. `ready: false` includes `error`.
- Binary JPEG frames of the current screen, about three a second.
- Text JSON `{ "page": 1, "pages": 2 }` after a screen change, or `{ "error": "…" }` when an action fails.

Client → server, text JSON:

| `type` | Body | Control on the page |
| --- | --- | --- |
| `click` | `x`, `y`, `w`, `h` | Tap the screen. `w` and `h` are the image size. |
| `type` | `text` | Characters from the keyboard field. |
| `key` | `key` | `return`, `backspace`, `tab`, `escape` |
| `page` | `n` | Screen **1** or **2** |
| `apps` | | **Apps** |
| `menu` | | **Menu** |

Clicks and typed text are applied on that agent’s computer only while the session is `paused`, and then discarded. They are not written to `messages` or `events`. While the session is `running`, the socket still sends frames and answers input with `{ "error": "The agent is working." }`. On a Mac the connector injects them with Accessibility. On a VM the same socket is injected by the connector on that VM.

## Connector

The agent loop runs in the Factory. The computer runs the connector. The connector dials out. It does not present the Factory bearer, and it is not a route in the shell above.

The credential is created with the computer, shown once, and kept on that computer. It is not a column. An empty credential is not accepted. Heartbeats and tool results use:

```
Authorization: Bearer <connector credential>
```

`POST /api/connector/heartbeat` writes `computers.last_seen_at`. The Factory treats the computer as online when that timestamp is within 30 seconds. While a session is open, the same write copies the timestamp onto `sessions.heartbeat_at`. Nothing else writes either column.

The connector holds an outbound stream to the Factory. A tool call the agent decided (a click, a navigation, a file read) is delivered on that stream. The connector performs it and posts the observation back. That is how a decision on the platform becomes a click. While the stream is quiet, the agent does not start or finish machine work.

Bot setup is separate. `POST /api/agents/{agent_id}/bot` takes `{ "token": "…" }`, stores the token in the secret store, and sets `username` and `telegram_bot_id`. The response is the public bot object. `DELETE /api/agents/{agent_id}/bot` unbinds: it clears `chat_id`, `username`, and `telegram_bot_id`, and drops the token. The token is never written to `bots`.
