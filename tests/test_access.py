"""Factory bearer, and the checks that keep it separate from the other two doors."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from control_machine.access import (
    FactorySessions,
    SessionError,
    computer_online,
    in_workspace,
    new_session_block,
    public_bot,
    session_is_open,
    tool_name_ok,
)
from control_machine.config import Settings
from control_machine.live import LiveError, LiveSessions
from control_machine.web.session import router as session_router


def _settings() -> Settings:
    return Settings(
        factory_access_key="workspace-key",
        factory_session_secret="session-secret",
        factory_session_ttl_seconds=3600,
        live_link_secret="live-secret",
        telegram_bot_token="bot-token",
    )


def _client(settings: Settings | None = None) -> TestClient:
    app = FastAPI()
    app.state.factory_sessions = FactorySessions(settings or _settings())
    app.include_router(session_router)
    return TestClient(app)


class FactorySessionTests(unittest.TestCase):
    def test_login_issues_a_bearer_that_expires(self) -> None:
        client = _client()
        response = client.post("/api/session", json={"key": "workspace-key"})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertIn("token", body)
        self.assertTrue(body["expires_at"].endswith("Z"))
        read = client.get("/api/session", headers={"Authorization": f"Bearer {body['token']}"})
        self.assertEqual(read.status_code, 200)
        self.assertEqual(read.json()["expires_at"], body["expires_at"])

    def test_missing_and_bad_session_are_401(self) -> None:
        client = _client()
        missing = client.get("/api/session")
        self.assertEqual(missing.status_code, 401)
        self.assertEqual(missing.json(), {"detail": "Missing session."})
        bad = client.get("/api/session", headers={"Authorization": "Bearer not-a-session"})
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(bad.json(), {"detail": "Session is invalid or expired."})

    def test_wrong_key_and_unconfigured_login_are_refused(self) -> None:
        client = _client()
        wrong = client.post("/api/session", json={"key": "nope"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(wrong.json(), {"detail": "Workspace key was not accepted."})
        closed = _client(Settings(factory_access_key="", factory_session_secret=""))
        refused = closed.post("/api/session", json={"key": "workspace-key"})
        self.assertEqual(refused.status_code, 401)
        self.assertEqual(refused.json(), {"detail": "Factory access is not configured."})

    def test_expired_and_revoked_sessions_are_refused(self) -> None:
        sessions = FactorySessions(_settings())
        with patch("control_machine.access.time.time", return_value=1_000):
            issued = sessions.login("workspace-key")
        token = issued["token"]
        with patch("control_machine.access.time.time", return_value=1_000 + 3601):
            with self.assertRaises(SessionError):
                sessions.verify(token)
        fresh = sessions.login("workspace-key")
        sessions.revoke(fresh["token"])
        with self.assertRaises(SessionError):
            sessions.verify(fresh["token"])
        client = _client()
        created = client.post("/api/session", json={"key": "workspace-key"})
        token = created.json()["token"]
        gone = client.delete("/api/session", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(gone.status_code, 204)
        again = client.get("/api/session", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(again.status_code, 401)

    def test_factory_bearer_is_not_a_live_link(self) -> None:
        settings = _settings()
        factory = FactorySessions(settings)
        live = LiveSessions(settings)
        token = factory.login("workspace-key")["token"]
        with self.assertRaises(LiveError):
            live.verify_token(token)
        live_token = str(live.mint(4, 1)["url"]).rsplit("t=", 1)[-1]
        with self.assertRaises(SessionError):
            factory.verify(live_token)

    def test_scope_machine_work_and_bot_shape(self) -> None:
        now = datetime(2026, 9, 30, 20, 46, 12, tzinfo=timezone.utc)
        self.assertTrue(computer_online(now - timedelta(seconds=30), now=now))
        self.assertFalse(computer_online(now - timedelta(seconds=31), now=now))
        self.assertFalse(computer_online(None, now=now))
        self.assertTrue(in_workspace(computer_id=1, workspace_computer_ids={1}))
        self.assertFalse(in_workspace(computer_id=2, workspace_computer_ids={1}))
        self.assertEqual(
            new_session_block(online=False, open_session=False),
            "Computer is offline.",
        )
        self.assertEqual(
            new_session_block(online=True, open_session=True),
            "Computer already has an open session.",
        )
        self.assertIsNone(new_session_block(online=True, open_session=False))
        self.assertTrue(session_is_open("running"))
        self.assertTrue(session_is_open("paused"))
        self.assertFalse(session_is_open("done"))
        self.assertTrue(tool_name_ok("browser"))
        self.assertFalse(tool_name_ok("shell"))
        bot = public_bot(bot_id=1, username="office_bot", chat_id=9)
        self.assertEqual(bot, {"id": 1, "username": "office_bot", "connected": True})
        self.assertNotIn("token", bot)
