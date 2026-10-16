"""Live demo: heartbeat → stream → tool_call → result.

Run against a listening control-machine (or this script's in-process app).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from control_machine.access import FactorySessions
from control_machine.config import Settings
from control_machine.factory.api import router as factory_router
from control_machine.factory.connector import ConnectorHub, ToolResult
from control_machine.factory.live_links import FactoryLiveLinks
from control_machine.factory.store import FactoryStore
from control_machine.web.session import router as session_router

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/control_machine?sslmode=disable",
)


async def main() -> int:
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(
        conninfo=DATABASE_URL,
        min_size=1,
        max_size=4,
        open=False,
        timeout=3,
        kwargs={"row_factory": dict_row, "autocommit": True},
    )
    await pool.open()
    store = await FactoryStore.setup(pool)
    settings = Settings(
        database_url=DATABASE_URL,
        factory_access_key="demo-key",
        factory_session_secret="demo-session",
        factory_session_ttl_seconds=3600,
        live_link_secret="demo-live",
        public_base_url="https://example.test",
    )
    hub = ConnectorHub(default_timeout=10.0)
    app = FastAPI()
    app.state.factory = store
    app.state.factory_sessions = FactorySessions(settings)
    app.state.factory_live = FactoryLiveLinks(settings)
    app.state.connector_hub = hub
    app.include_router(session_router)
    app.include_router(factory_router)
    client = TestClient(app)

    stamp = datetime.now(timezone.utc).strftime("%H%M%S")
    print("== login ==")
    login = client.post("/api/session", json={"key": "demo-key"})
    print(login.status_code, login.json())
    headers = {"Authorization": f"Bearer {login.json()['token']}"}

    print("== create agent ==")
    created = client.post(
        "/api/agents",
        headers=headers,
        json={
            "name": f"Demo-{stamp}",
            "description": "Connector stream demo",
            "computer": {"name": "This Mac", "kind": "mac"},
        },
    )
    body = created.json()
    print(created.status_code, {"id": body["id"], "computer": body["computer"]["id"]})
    credential = body["connector_credential"]
    computer_id = int(body["computer"]["id"])
    agent_id = int(body["id"])

    print("== heartbeat ==")
    beat = client.post(
        "/api/connector/heartbeat",
        headers={"Authorization": f"Bearer {credential}"},
    )
    print(beat.status_code, beat.json())

    print("== open session (stream still quiet) ==")
    msg = client.post(
        f"/api/agents/{agent_id}/messages",
        headers=headers,
        json={"body": "Fill this week's timesheet"},
    )
    detail = client.get(f"/api/agents/{agent_id}", headers=headers).json()
    session_id = int(detail["session"]["id"])
    agent_reply = client.get(f"/api/agents/{agent_id}/messages", headers=headers).json()[-1]
    print(msg.status_code, "session", session_id)
    print("agent:", agent_reply["body"])

    print("== dial outbound stream ==")
    with client.websocket_connect(
        "/api/connector/stream",
        headers={"Authorization": f"Bearer {credential}"},
    ) as ws:
        hello = json.loads(ws.receive_text())
        print("←", hello)

        box: dict[str, object] = {}

        def enqueue() -> None:
            box["result"] = hub.call_tool_blocking(
                computer_id,
                session_id=session_id,
                name="browser_navigate",
                arguments={"url": "https://mywipro.example/timesheet"},
                timeout=10.0,
            )

        worker = threading.Thread(target=enqueue)
        worker.start()
        while True:
            frame = json.loads(ws.receive_text())
            print("←", frame)
            if frame.get("type") == "tool_call":
                break
        print("== post observation ==")
        posted = client.post(
            "/api/connector/result",
            headers={"Authorization": f"Bearer {credential}"},
            json={
                "id": frame["id"],
                "ok": True,
                "observation": "MyWipro sign-in page (ARIA snapshot stub)",
            },
        )
        print(posted.status_code, posted.json())
        worker.join(timeout=10)
        result = box["result"]
        assert isinstance(result, ToolResult)
        print("== hub received ==")
        print(result)

    print("== stream_connected after close ==")
    beat2 = client.post(
        "/api/connector/heartbeat",
        headers={"Authorization": f"Bearer {credential}"},
    )
    print(beat2.json())
    await pool.close()
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
