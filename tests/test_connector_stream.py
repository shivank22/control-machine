"""Outbound connector stream: deliver tool calls and collect results."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from control_machine.access import FactorySessions
from control_machine.config import Settings
from control_machine.factory.api import router as factory_router
from control_machine.factory.connector import ConnectorError, ConnectorHub, ToolResult
from control_machine.factory.live_links import FactoryLiveLinks
from control_machine.factory.store import FactoryStore
from control_machine.web.session import router as session_router

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:postgres@127.0.0.1:5432/control_machine?sslmode=disable",
)


def _settings() -> Settings:
    return Settings(
        database_url=DATABASE_URL,
        factory_access_key="workspace-key",
        factory_session_secret="session-secret",
        factory_session_ttl_seconds=3600,
        live_link_secret="live-secret",
        public_base_url="https://example.test",
    )


async def _connect_store():
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
    return pool, store


def _recv_until(ws, typ: str, *, limit: int = 20) -> dict:
    for _ in range(limit):
        payload = json.loads(ws.receive_text())
        if payload.get("type") == typ:
            return payload
    raise AssertionError(f"Did not receive type={typ!r}")


class ConnectorHubUnitTests(unittest.IsolatedAsyncioTestCase):
    async def test_call_and_result(self) -> None:
        hub = ConnectorHub(default_timeout=2.0)
        outbound = await hub.attach(7)

        async def consumer() -> None:
            msg = await outbound.get()
            self.assertEqual(msg["type"], "tool_call")
            self.assertEqual(msg["name"], "browser_navigate")
            await hub.deliver_result(
                7,
                ToolResult(id=msg["id"], ok=True, observation="ok"),
            )

        consumer_task = asyncio.create_task(consumer())
        result = await hub.call_tool(
            7,
            session_id=3,
            name="browser_navigate",
            arguments={"url": "https://example.test"},
        )
        await consumer_task
        self.assertTrue(result.ok)
        self.assertEqual(result.observation, "ok")

    async def test_detach_fails_pending(self) -> None:
        hub = ConnectorHub(default_timeout=2.0)
        outbound = await hub.attach(8)

        async def caller() -> None:
            with self.assertRaises(ConnectorError):
                await hub.call_tool(8, session_id=1, name="ping", arguments={})

        task = asyncio.create_task(caller())
        await outbound.get()
        await hub.detach(8, outbound)
        await task

    async def test_call_without_stream(self) -> None:
        hub = ConnectorHub()
        with self.assertRaises(ConnectorError):
            await hub.call_tool(1, session_id=1, name="ping", arguments={})


class ConnectorStreamApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            self.pool, self.store = await _connect_store()
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"Postgres not available: {exc}")
        settings = _settings()
        self.hub = ConnectorHub(default_timeout=5.0)
        app = FastAPI()
        app.state.factory = self.store
        app.state.factory_sessions = FactorySessions(settings)
        app.state.factory_live = FactoryLiveLinks(settings)
        app.state.connector_hub = self.hub
        app.include_router(session_router)
        app.include_router(factory_router)
        self.client = TestClient(app)
        login = self.client.post("/api/session", json={"key": "workspace-key"})
        self.assertEqual(login.status_code, 201)
        self.headers = {"Authorization": f"Bearer {login.json()['token']}"}
        self._stamp = datetime.now(timezone.utc).strftime("%H%M%S%f")

    async def asyncTearDown(self) -> None:
        if getattr(self, "pool", None) is not None:
            await self.pool.close()

    def _create_agent(self) -> tuple[int, str, int]:
        created = self.client.post(
            "/api/agents",
            headers=self.headers,
            json={
                "name": f"Stream-{self._stamp}",
                "description": "Connector stream",
                "computer": {"name": "This Mac", "kind": "mac"},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        return int(body["id"]), body["connector_credential"], int(body["computer"]["id"])

    def test_stream_delivers_tool_call_and_result_post(self) -> None:
        agent_id, credential, computer_id = self._create_agent()
        beat = self.client.post(
            "/api/connector/heartbeat",
            headers={"Authorization": f"Bearer {credential}"},
        )
        self.assertEqual(beat.status_code, 200)
        self.assertFalse(beat.json()["stream_connected"])

        posted = self.client.post(
            f"/api/agents/{agent_id}/messages",
            headers=self.headers,
            json={"body": "Open the timesheet"},
        )
        self.assertEqual(posted.status_code, 201)
        detail = self.client.get(f"/api/agents/{agent_id}", headers=self.headers).json()
        session_id = int(detail["session"]["id"])
        messages = self.client.get(
            f"/api/agents/{agent_id}/messages", headers=self.headers
        ).json()
        self.assertIn("outbound stream", messages[1]["body"].lower())

        with self.client.websocket_connect(
            "/api/connector/stream",
            headers={"Authorization": f"Bearer {credential}"},
        ) as ws:
            hello = _recv_until(ws, "hello")
            self.assertEqual(hello["computer_id"], computer_id)
            self.assertTrue(self.hub.connected(computer_id))

            import threading

            box: dict[str, object] = {}

            def enqueue() -> None:
                box["result"] = self.hub.call_tool_blocking(
                    computer_id,
                    session_id=session_id,
                    name="browser_navigate",
                    arguments={"url": "https://example.test/timesheet"},
                    timeout=5.0,
                )

            worker = threading.Thread(target=enqueue)
            worker.start()
            frame = _recv_until(ws, "tool_call")
            self.assertEqual(frame["name"], "browser_navigate")
            self.assertEqual(frame["session_id"], session_id)
            self.assertEqual(
                frame["arguments"]["url"], "https://example.test/timesheet"
            )

            result = self.client.post(
                "/api/connector/result",
                headers={"Authorization": f"Bearer {credential}"},
                json={
                    "id": frame["id"],
                    "ok": True,
                    "observation": "Signed-in home",
                },
            )
            self.assertEqual(result.status_code, 200, result.text)
            self.assertTrue(result.json()["accepted"])
            worker.join(timeout=5)
            self.assertTrue(worker.is_alive() is False)
            done = box["result"]
            assert isinstance(done, ToolResult)
            self.assertTrue(done.ok)
            self.assertEqual(done.observation, "Signed-in home")

        self.assertFalse(self.hub.connected(computer_id))

    def test_tool_result_on_websocket(self) -> None:
        _, credential, computer_id = self._create_agent()
        with self.client.websocket_connect(
            f"/api/connector/stream?credential={credential}",
        ) as ws:
            _recv_until(ws, "hello")

            import threading

            box: dict[str, object] = {}

            def enqueue() -> None:
                box["result"] = self.hub.call_tool_blocking(
                    computer_id,
                    session_id=1,
                    name="desktop_screenshot",
                    arguments={},
                    timeout=5.0,
                )

            worker = threading.Thread(target=enqueue)
            worker.start()
            frame = _recv_until(ws, "tool_call")
            ws.send_text(
                json.dumps(
                    {
                        "type": "tool_result",
                        "id": frame["id"],
                        "ok": True,
                        "observation": "JPEG frame",
                    }
                )
            )
            worker.join(timeout=5)
            done = box["result"]
            assert isinstance(done, ToolResult)
            self.assertEqual(done.observation, "JPEG frame")

    def test_cancel_notifies_stream(self) -> None:
        agent_id, credential, _computer_id = self._create_agent()
        self.client.post(
            "/api/connector/heartbeat",
            headers={"Authorization": f"Bearer {credential}"},
        )
        self.client.post(
            f"/api/agents/{agent_id}/messages",
            headers=self.headers,
            json={"body": "Do a thing"},
        )
        detail = self.client.get(f"/api/agents/{agent_id}", headers=self.headers).json()
        session_id = int(detail["session"]["id"])

        with self.client.websocket_connect(
            "/api/connector/stream",
            headers={"Authorization": f"Bearer {credential}"},
        ) as ws:
            _recv_until(ws, "hello")
            cancel = self.client.post(
                f"/api/agents/{agent_id}/session/cancel",
                headers=self.headers,
            )
            self.assertEqual(cancel.status_code, 204)
            frame = _recv_until(ws, "cancel")
            self.assertEqual(frame["session_id"], session_id)


if __name__ == "__main__":
    unittest.main()
