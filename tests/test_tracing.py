"""Sanitization and JSONL tracing for local debug logs."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from control_machine.tracing import JsonlTracer, sanitize


class SanitizeTests(unittest.TestCase):
    def test_redacts_secrets_not_token_counts(self) -> None:
        cleaned = sanitize(
            {
                "openai_api_key": "sk-secret",
                "max_tokens": 128,
                "telegram_bot_token": "123:abc",
            }
        )
        self.assertEqual(cleaned["openai_api_key"], "[redacted]")
        self.assertEqual(cleaned["telegram_bot_token"], "[redacted]")
        self.assertEqual(cleaned["max_tokens"], 128)

    def test_omits_images_and_truncates(self) -> None:
        cleaned = sanitize(
            {
                "image_url": {"url": "data:image/jpeg;base64,aaaa"},
                "text": "x" * 9_000,
                "nested": [{"type": "image_url", "image_url": "data:image/png;base64,bb"}],
            }
        )
        self.assertEqual(cleaned["image_url"], "[image omitted]")
        self.assertTrue(str(cleaned["text"]).endswith("more chars]"))
        self.assertEqual(cleaned["nested"][0]["image_url"], "[image omitted]")


class JsonlTracerTests(unittest.TestCase):
    def test_writes_start_and_error_events(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task-7.jsonl"
            tracer = JsonlTracer(path, task_id=7, thread_id="thread-abc")
            run = SimpleNamespace(
                id=uuid4(),
                parent_run_id=None,
                run_type="llm",
                name="ChatOpenAI",
                tags=["task:7"],
                inputs={"messages": "hello", "api_key": "sk-secret"},
                outputs=None,
                error="RateLimitError: boom",
                start_time=datetime(2026, 9, 19, tzinfo=timezone.utc),
                end_time=datetime(2026, 9, 19, 0, 0, 2, tzinfo=timezone.utc),
            )
            tracer._on_run_create(run)  # noqa: SLF001 - unit-testing the tracer hooks
            with self.assertLogs("control_machine", level="ERROR"):
                tracer._on_run_update(run)  # noqa: SLF001

            lines = path.read_text(encoding="utf-8").strip().splitlines()
            events = [json.loads(line) for line in lines]
            kinds = [event["event"] for event in events]
            self.assertEqual(kinds, ["task_start", "start", "error"])
            self.assertEqual(events[1]["task_id"], 7)
            self.assertEqual(events[1]["inputs"]["api_key"], "[redacted]")
            self.assertEqual(events[2]["error"], "RateLimitError: boom")
            self.assertEqual(events[2]["elapsed_ms"], 2000)


if __name__ == "__main__":
    unittest.main()
