# control-machine

A LangGraph Deep Agent that drives isolated Chrome containers over CDP, backed by Postgres
for checkpoints and long-term memory. The dashboard embeds each browser through noVNC, so
you can watch the agent and take control without leaving the page.

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

## Setup

```bash
uv sync
cp .env.example .env
docker compose --profile browsers up -d --build
```

Set `OPENAI_API_KEY` in `.env` (the default model is `gpt-4.1`) and run the dashboard:

```bash
uv run control-machine
```

To use a local Ollama model instead, set `LLM_PROVIDER=ollama` and pull the model:

```bash
ollama pull gemma4
uv run control-machine
```

Open http://127.0.0.1:8100.

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

## Layout

| Path | What it does |
| --- | --- |
| `src/control_machine/browser.py` | CDP session, ARIA snapshots, refs, screenshots, marks |
| `src/control_machine/tools/browser_tools.py` | The curated tool surface the agent sees |
| `src/control_machine/agent.py` | Deep Agent wiring, model, prompt, HITL gates |
| `src/control_machine/runner.py` | Run loop, step persistence, event streaming |
| `src/control_machine/scheduler.py` | Cron helpers and the loop that fires due automations |
| `src/control_machine/web/` | FastAPI + HTMX dashboard |
