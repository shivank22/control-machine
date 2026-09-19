"""Signed live-link tokens."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from control_machine.config import Settings
from control_machine.live import LiveError, LiveSessions


class LiveSessionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions = LiveSessions(
            Settings(
                live_link_secret="test-secret",
                live_link_ttl_seconds=3600,
                telegram_bot_token="",
            )
        )

    def test_verify_after_process_restart(self) -> None:
        public = self.sessions.mint(59, 0)
        token = str(public["url"]).rsplit("t=", 1)[-1]
        restarted = LiveSessions(self.sessions.settings)
        session = restarted.verify_token(token)
        self.assertEqual(session.task_id, 59)

    def test_expired_token_asks_for_watch(self) -> None:
        with patch("control_machine.live.time.time", return_value=1_000):
            public = self.sessions.mint(1, 0)
        token = str(public["url"]).rsplit("t=", 1)[-1]
        with patch("control_machine.live.time.time", return_value=1_000 + 3601):
            with self.assertRaises(LiveError) as caught:
                self.sessions.verify_token(token)
        self.assertIn("/watch", str(caught.exception))

    def test_mint_without_browser_slot(self) -> None:
        public = self.sessions.mint(0, 0)
        self.assertIn("/live/0?", public["url"])
        self.assertGreater(int(public["expires_at"]), int(time.time()))
