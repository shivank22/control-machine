"""Browser pool target: Docker slots or the Chrome already running on this Mac."""

from __future__ import annotations

import unittest

from control_machine.browser import BrowserPool
from control_machine.config import Settings


class BrowserPoolTargetTests(unittest.TestCase):
    def test_docker_backend_uses_container_ports(self) -> None:
        settings = Settings(
            browser_backend="docker",
            browser_slot_count=2,
            browser_cdp_base_port=9231,
            llm_provider="ollama",
        )
        pool = BrowserPool.from_settings(settings)
        self.assertEqual(
            [slot.cdp_endpoint for slot in pool.slots],
            ["http://127.0.0.1:9231", "http://127.0.0.1:9232"],
        )
        self.assertTrue(pool.slots[0].novnc_url)
        self.assertFalse(pool.slots[0].session._preserve_pages)

    def test_host_backend_attaches_to_one_local_chrome(self) -> None:
        settings = Settings(
            browser_backend="host",
            browser_cdp_endpoint="http://127.0.0.1:9222",
            browser_slot_count=2,
            llm_provider="ollama",
        )
        pool = BrowserPool.from_settings(settings)
        self.assertEqual(len(pool.slots), 1)
        self.assertEqual(pool.slots[0].cdp_endpoint, "http://127.0.0.1:9222")
        self.assertEqual(pool.slots[0].novnc_url, "")
        self.assertTrue(pool.slots[0].session._preserve_pages)


if __name__ == "__main__":
    unittest.main()
