"""Factory API: agents, sessions, connector heartbeat, stubbed agent replies."""

from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from control_machine.access import FactorySessions
from control_machine.config import Settings
from control_machine.factory.api import router as factory_router
from control_machine.factory.connector import ConnectorHub
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


class FactoryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        try:
            self.pool, self.store = await _connect_store()
        except Exception as exc:  # pragma: no cover
            self.skipTest(f"Postgres not available: {exc}")
        settings = _settings()
        app = FastAPI()
        app.state.factory = self.store
        app.state.factory_sessions = FactorySessions(settings)
        app.state.factory_live = FactoryLiveLinks(settings)
        app.state.connector_hub = ConnectorHub()
        app.include_router(session_router)
        app.include_router(factory_router)
        self.client = TestClient(app)
        login = self.client.post("/api/session", json={"key": "workspace-key"})
        self.assertEqual(login.status_code, 201)
        self.headers = {"Authorization": f"Bearer {login.json()['token']}"}
        # Isolate: wipe factory tables for this test run's agents we create by unique names.
        self._stamp = datetime.now(timezone.utc).strftime("%H%M%S%f")

    async def asyncTearDown(self) -> None:
        if getattr(self, "pool", None) is not None:
            await self.pool.close()

    def test_create_list_and_patch_agent(self) -> None:
        name = f"Office-{self._stamp}"
        created = self.client.post(
            "/api/agents",
            headers=self.headers,
            json={
                "name": name,
                "description": "Desk work",
                "computer": {"name": "This Mac", "kind": "mac"},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertEqual(body["name"], name)
        self.assertIn("connector_credential", body)
        self.assertEqual(body["computer"]["provider"], "local")
        self.assertEqual(body["computer"]["status"], "offline")
        tools = {t["name"]: t["enabled"] for t in body["tools"]}
        self.assertEqual(tools, {"browser": True, "clicks": True, "files": True, "sap": False})
        agent_id = body["id"]

        listed = self.client.get("/api/agents", headers=self.headers)
        self.assertEqual(listed.status_code, 200)
        self.assertTrue(any(a["id"] == agent_id for a in listed.json()))

        patched = self.client.patch(
            f"/api/agents/{agent_id}",
            headers=self.headers,
            json={"description": "Updated desk work", "avatar_color": "#2563eb"},
        )
        self.assertEqual(patched.status_code, 200)
        self.assertEqual(patched.json()["description"], "Updated desk work")
        self.assertEqual(patched.json()["avatar_color"], "#2563eb")

        tool = self.client.patch(
            f"/api/agents/{agent_id}/tools/sap",
            headers=self.headers,
            json={"enabled": True},
        )
        self.assertEqual(tool.status_code, 200)
        self.assertTrue(tool.json()["enabled"])

    def test_message_offline_refuses_and_online_opens_session(self) -> None:
        name = f"Sales-{self._stamp}"
        created = self.client.post(
            "/api/agents",
            headers=self.headers,
            json={
                "name": name,
                "description": "Pipeline",
                "computer": {"name": "Sales desk", "kind": "vm", "provider": "daytona"},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        agent_id = created.json()["id"]
        credential = created.json()["connector_credential"]

        offline = self.client.post(
            f"/api/agents/{agent_id}/messages",
            headers=self.headers,
            json={"body": "Fill the pipeline"},
        )
        self.assertEqual(offline.status_code, 201)
        messages = self.client.get(
            f"/api/agents/{agent_id}/messages", headers=self.headers
        ).json()
        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[1]["role"], "agent")
        self.assertIn("offline", messages[1]["body"].lower())
        detail = self.client.get(f"/api/agents/{agent_id}", headers=self.headers).json()
        self.assertIsNone(detail["session"])

        beat = self.client.post(
            "/api/connector/heartbeat",
            headers={"Authorization": f"Bearer {credential}"},
        )
        self.assertEqual(beat.status_code, 200, beat.text)

        online = self.client.post(
            f"/api/agents/{agent_id}/messages",
            headers=self.headers,
            json={"body": "Draft the Acme note"},
        )
        self.assertEqual(online.status_code, 201)
        detail = self.client.get(f"/api/agents/{agent_id}", headers=self.headers).json()
        self.assertIsNotNone(detail["session"])
        self.assertEqual(detail["session"]["status"], "running")
        self.assertEqual(detail["computer"]["status"], "online")

        conflict = self.client.post(
            f"/api/agents/{agent_id}/messages",
            headers=self.headers,
            json={"body": "Another ask"},
        )
        self.assertEqual(conflict.status_code, 409)

        cancel = self.client.post(
            f"/api/agents/{agent_id}/session/cancel",
            headers=self.headers,
        )
        self.assertEqual(cancel.status_code, 204)
        detail = self.client.get(f"/api/agents/{agent_id}", headers=self.headers).json()
        self.assertIsNone(detail["session"])

    def test_skills_memories_and_desktop_mint(self) -> None:
        name = f"Coder-{self._stamp}"
        created = self.client.post(
            "/api/agents",
            headers=self.headers,
            json={
                "name": name,
                "description": "Repo",
                "computer": {"name": "This Mac", "kind": "mac"},
            },
        )
        agent_id = created.json()["id"]
        credential = created.json()["connector_credential"]

        mem = self.client.post(
            f"/api/agents/{agent_id}/memories",
            headers=self.headers,
            json={"body": "Ask before writes", "detail": "From last week"},
        )
        self.assertEqual(mem.status_code, 201)
        mem_id = mem.json()["id"]
        listed = self.client.get(
            f"/api/agents/{agent_id}/memories", headers=self.headers
        ).json()
        self.assertEqual(listed[0]["body"], "Ask before writes")
        deleted = self.client.delete(
            f"/api/agents/{agent_id}/memories/{mem_id}", headers=self.headers
        )
        self.assertEqual(deleted.status_code, 204)

        skill = self.client.post(
            f"/api/agents/{agent_id}/skills",
            headers=self.headers,
            json={
                "name": "Repo",
                "description": "When inspecting code",
                "body": "Read freely. Ask before writing.",
            },
        )
        self.assertEqual(skill.status_code, 201)

        no_session = self.client.post(
            f"/api/agents/{agent_id}/desktop", headers=self.headers
        )
        self.assertEqual(no_session.status_code, 409)

        self.client.post(
            "/api/connector/heartbeat",
            headers={"Authorization": f"Bearer {credential}"},
        )
        self.client.post(
            f"/api/agents/{agent_id}/messages",
            headers=self.headers,
            json={"body": "Inspect browser.py"},
        )
        link = self.client.post(
            f"/api/agents/{agent_id}/desktop", headers=self.headers
        )
        self.assertEqual(link.status_code, 201, link.text)
        self.assertIn("/desktop?t=", link.json()["url"])


if __name__ == "__main__":
    unittest.main()
