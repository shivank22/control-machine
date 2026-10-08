# Control Machine design

The platform stays up when the computer is off. A person talks to one agent in Telegram. That agent controls one computer, and nothing else runs on it. The connector on that computer dials out, and the agent works there only while the connector is online.

For now the computer is a Mac. In the enterprise case it is a VM, at Daytona, on Azure, or with another provider. The provider names where the VM runs. The control path is the connector either way.

This set is the Factory contract. The tables are in [`research/schema.sql`](../../research/schema.sql). It is the design we are building, not a description of the dashboard that ships today. The screen mock in [`research/index.html`](../../research/index.html) still draws several agents on one Mac; these pages are ahead of that drawing.

The picture of the same split is [Agent to computer](../agent-machine-architecture.png).

## Rules

- One agent has one Telegram bot. That chat is the only conversation.
- One agent controls one computer. That computer is controlled by that agent alone.
- The computer is a Mac for now (`kind` `mac`, `provider` `local`). Enterprise is a VM (`kind` `vm`). `provider` is `daytona`, `azure`, or another name.
- The platform keeps the agent, its memory, its skills, and its activity while its computer is off.
- While that computer’s connector is quiet, the agent refuses machine work.
- That computer runs one intent at a time. Another agent runs on its own computer.
- A sensitive step waits. The reply comes back in the same chat.
- The agent sees page text and screenshots. Passwords, the machine credential, bot tokens, and bridge keystrokes are not stored.
- One person owns the Factory. There is no user table and no workspace table.
- The agent loop runs in the Factory. The connector on the computer dials out, heartbeats, and carries a tool call back as a click.
- The live desktop is one signed JPEG socket, bound to the open session. The computer does not run a VNC server, and the phone does not open a WebRTC tunnel. noVNC stays on localhost inside the browser container and is not a public route.
- A waiting pause is released by the reply in the thread, and only once the connector is online. Cancel, a finished run, a failed run, or a pause older than 24 hours frees the computer.

## Pages

- [Blueprint](Control-Machine-blueprint.pdf) — the mock screens in one document.
- [Product overview](../../product-overview/product-overview.pdf) — the five-section overview and the architecture diagram.
- [Screens](screens.md) — Factory, the agent, and the two surfaces outside the shell.
- [Data model](data-model.md) — the ten tables, and what is only a view.
- [API](api.md) — the HTTP and desktop-stream contract for those pages.
- [Security](security.md) — the bearer session, the Telegram allowlist, the signed desktop link, and what stays off the tables.
- [Flows](flows.md) — opening an agent, a pause, and the computer going offline.
