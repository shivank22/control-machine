from __future__ import annotations

import unittest

from control_machine.desktop import capture_jpeg, display_count


class DesktopCaptureTests(unittest.TestCase):
    def test_one_page_is_not_ultrawide(self) -> None:
        jpeg, width, height = capture_jpeg(page=1)
        self.assertGreater(len(jpeg), 1000)
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        self.assertGreaterEqual(height / width, 0.4)

    def test_page_two_clamps_when_only_one_display(self) -> None:
        pages = display_count()
        jpeg, width, height = capture_jpeg(page=99)
        self.assertGreater(len(jpeg), 1000)
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        self.assertGreaterEqual(pages, 1)
