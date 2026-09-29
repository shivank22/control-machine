# control-machine

A LangGraph Deep Agent that drives this Mac from Telegram (and a local dashboard): isolated
Chrome over CDP, host files under the current user's home, and a live desktop view for
logins. Postgres stores checkpoints and long-term memory.

Telegram talks to one supervisor Deep Agent. It delegates to three isolated specialists.
The browser specialist is itself a Deep Agent, so it can load its own skills. The file
and desktop specialists stay plain agents. The supervisor does not see their page
snapshots or file reads. Ask for a link and it mints the same signed live desktop as `/watch`.

## How it controls the browser

Snapshot-first, vision second. The agent acts on Playwright's AI-mode ARIA snapshot, where
every interactive element has a stable `[ref=e12]` handle, and clicks resolve through
`page.locator("aria-ref=e12")`. That is deterministic and costs a few hundred tokens per page,
versus thousands for a screenshot plus coordinate guessing that a small local model gets wrong.

Screenshots are a *reading* channel: canvas and charts, icon-only controls with no accessible
name, and final visual verification. When pixels are genuinely unavoidable, the agent asks for a
marked screenshot, where numbered labels are drawn over the bounding boxes from
`aria_snapshot(boxes=True)`, so it picks a number instead of a coordinate. Raw `browser_click_xy`
exists but requires human approval.

## Files and desktop

The file specialist can list, read, write, and download files under your home directory
(`FILESYSTEM_ROOT`, default `~`), mounted at `/home` for Deep Agents' built-in file
tools. Secrets such as `~/.ssh`, keychains, and `.env` are blocked. Deletes and
overwrites of existing files ask for approval in Telegram. The browser and desktop
specialists cannot read or write that mount.

The desktop specialist uses `app_open`, `desktop_screenshot`, `desktop_click`, and `desktop_type`. Grant
**Screen Recording** and **Accessibility** to the process that runs `control-machine`.

## Setup

```bash
uv sync
cp .env.example .env
docker compose --profile browsers up -d --build
```

## Remote control (Telegram + ngrok)

Phone takeover needs three values in `.env`: the bot token, your Telegram user id, and the
public https URL of this dashboard. Paste them exactly as below, then restart
`uv run control-machine` whenever `.env` changes.

### 1. Create the bot and copy the token

1. Open Telegram and message [BotFather](https://t.me/BotFather).
2. Send `/newbot`, pick a name and username.
3. Copy the token (`123456789:AAH...`). Paste it into `.env`:

```
TELEGRAM_BOT_TOKEN=123456789:AAH-your-token-here
```

Leave `TELEGRAM_ALLOWLIST` empty for the first start — the bot will tell you your user id.

### 2. Start ngrok and copy the https URL

The dashboard listens on `127.0.0.1:8100`. Telegram **Open desktop** buttons only work over
https, so tunnel that port (not Chrome, noVNC, or CDP):

```bash
ngrok http 8100
```

Leave that terminal running. In the ngrok UI, copy the **https** Forwarding URL
(`https://….ngrok-free.app`) and paste it into `.env`:

```
PUBLIC_BASE_URL=https://YOUR-SUBDOMAIN.ngrok-free.app
```

No trailing slash. If you restart ngrok, the URL changes — update `PUBLIC_BASE_URL` and
restart `control-machine`.

### 3. API key (or Ollama)

Default model is OpenAI `gpt-4.1`:

```
OPENAI_API_KEY=sk-...
LLM_PROVIDER=openai
```

For a local model instead:

```
LLM_PROVIDER=ollama
OLLAMA_MODEL=gemma4:latest
```

```bash
ollama pull gemma4
```

### 4. Permissions, then start

System Settings → Privacy & Security:

1. **Screen Recording** — allow Terminal, iTerm, or Cursor (whichever runs `control-machine`).
2. **Accessibility** — same app.

```bash
uv run control-machine
```

Open http://127.0.0.1:8100 locally. Telegram uses the ngrok URL.

### 5. Allow your Telegram user

Message the bot `/start`. It replies with your numeric user id. Paste that into `.env`:

```
TELEGRAM_ALLOWLIST=123456789
```

Restart `uv run control-machine`. `/start` should then greet you instead of repeating the id.

Optional: `TELEGRAM_NOTIFY_CHAT_ID` (often the same number) gets live links when a scheduled
automation needs a human. `LIVE_LINK_SECRET` signs viewer cookies; if empty, the bot token is
used.

### Using the live desktop

Send a task, or send a document (saved under `~/Downloads/control-machine`). When the agent
needs a login, Telegram sends **Open desktop**. That page shows one screen at a time
(**1** / **2** if two displays are attached). Tap to click, pinch to zoom, and type from the
phone keyboard. **Apps** toggles Mission Control. **Menu** opens the Apple menu on the screen
you are viewing. Tap **Done** in Telegram when you have finished — the live view cannot
resume the agent by itself.

Commands: `/watch` (mint a live URL on demand), `/new`, `/cancel`.

## Dashboard

The left column has three tabs:

- **Chat** — talk to the agent for one-off work.
- **Skills** — save reusable instructions and run them as a fresh browser task.
- **Automations** — schedule a skill or a prompt (every hour, weekdays at 09:00, or a custom
  cron). Times use the server's local timezone. If every browser slot is busy, the job retries
  on the next tick instead of being skipped.

Two browser slots are available by default. Select a running task to watch its browser. Use
**Take control** before clicking or typing so the agent pauses at the next tool boundary, then
use **Resume agent** when finished. If the agent encounters a login, captcha, sensitive field,
or decision it should not make, it asks for help and waits while you use the embedded browser.

Each slot stores its Chrome profile in a named Docker volume. Logins therefore survive runs and
container restarts. A completed conversation also keeps its exact tab and slot warm for 15
minutes, so follow-up messages continue from the same page. Idle conversations are evicted
early only when a new conversation needs the browser capacity. CDP and noVNC ports are bound
to `127.0.0.1`; do not expose them publicly.

Useful commands:

```bash
docker compose --profile browsers ps
docker compose --profile browsers logs -f browser-1 browser-2
docker compose --profile browsers down
```

## Logs

Traces and errors stay on disk under `logs/` (override with `LOG_DIR`):

| Path | What it is |
| --- | --- |
| `logs/control-machine.log` | Rotating app log |
| `logs/errors.log` | Errors and tracebacks only |
| `logs/traces/task-{id}.jsonl` | LangGraph LLM, tool, and node spans for one task |

Screenshots and images are stripped from traces. Set `LOG_LEVEL=DEBUG` for more noise.

## Layout

| Path | What it does |
| --- | --- |
| `src/control_machine/browser.py` | CDP session, ARIA snapshots, refs, screenshots, marks |
| `src/control_machine/fs.py` | Home jail + Deep Agents `FilesystemBackend` at `/home` |
| `src/control_machine/tools/fs_tools.py` | `fs_download` onto this Mac |
| `src/control_machine/tools/desktop_tools.py` | Screenshot, click, type, open apps |
| `src/control_machine/tools/browser_tools.py` | The curated browser tool surface |
| `src/control_machine/agent.py` | Supervisor plus browser, file, and desktop subagents |
| `src/control_machine/runner.py` | Run loop, step persistence, event streaming |
| `src/control_machine/scheduler.py` | Cron helpers and the loop that fires due automations |
| `src/control_machine/live.py` | Signed live-desktop sessions and minting |
| `src/control_machine/telegram.py` | Telegram operator bot |
| `src/control_machine/tracing.py` | Local JSONL traces and rotating error logs |
| `src/control_machine/web/` | FastAPI + HTMX dashboard, live desktop stream |
