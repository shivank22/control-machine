"""Tool isolation for the supervisor and its specialists."""

from __future__ import annotations

import asyncio
import unittest

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from control_machine.agent import (
    SUPERVISOR_TOOL_NAMES,
    build_model,
    build_open_live_desktop,
    build_specialist_specs,
    repair_tool_pairs,
    skills_read_allowed,
    supervisor_visible_tools,
)
from control_machine.config import Settings
from control_machine.fs import build_agent_backend
from control_machine.scene import SceneCallback
from control_machine.tools import VisionBuffer, build_browser_tools


class _Tool:
    def __init__(self, name: str) -> None:
        self.name = name


class SupervisorFilterTests(unittest.TestCase):
    def test_keeps_only_task_and_live_link(self) -> None:
        tools = [
            _Tool(name)
            for name in (
                "task",
                "open_live_desktop",
                "execute",
                "ls",
                "read_file",
                "write_file",
                "edit_file",
                "glob",
                "grep",
                "delete",
                "browser_click",
                "fs_download",
                "desktop_click",
            )
        ]
        kept = [item.name for item in supervisor_visible_tools(tools)]
        self.assertEqual(kept, ["task", "open_live_desktop", "read_file"])
        self.assertEqual(set(kept), set(SUPERVISOR_TOOL_NAMES))

    def test_supervisor_reads_only_skills(self) -> None:
        self.assertTrue(skills_read_allowed("/skills/supervisor/timesheet/SKILL.md"))
        self.assertFalse(skills_read_allowed("/home/Documents/note.txt"))


class RepairToolPairsTests(unittest.TestCase):
    def test_drops_tool_message_without_a_call(self) -> None:
        orphan = ToolMessage(content="page", name="browser_snapshot", tool_call_id="call-1")
        repaired = repair_tool_pairs(
            [
                HumanMessage(content="Open the site"),
                orphan,
            ]
        )
        self.assertEqual(len(repaired), 1)
        self.assertIsInstance(repaired[0], HumanMessage)

    def test_keeps_a_tool_result_after_an_injected_note(self) -> None:
        call = AIMessage(
            content="",
            tool_calls=[{"name": "browser_snapshot", "args": {}, "id": "call-1"}],
        )
        note = HumanMessage(content="current page")
        result = ToolMessage(content="page", name="browser_snapshot", tool_call_id="call-1")
        repaired = repair_tool_pairs([HumanMessage(content="go"), call, note, result])
        self.assertEqual(
            [type(item) for item in repaired],
            [HumanMessage, AIMessage, ToolMessage, HumanMessage],
        )
        self.assertEqual(repaired[2].tool_call_id, "call-1")


class SpecialistIsolationTests(unittest.TestCase):
    def test_specialists_do_not_share_tools(self) -> None:
        settings = Settings(llm_provider="ollama", filesystem_root="/tmp")
        vision = VisionBuffer()
        specs = build_specialist_specs(
            session=object(),  # type: ignore[arg-type]
            vision=vision,
            scene=SceneCallback(session=object(), vision=vision),  # type: ignore[arg-type]
            running=asyncio.Event(),
            settings=settings,
            backend=build_agent_backend(settings),
            model=build_model(settings),
        )
        browser_spec = specs[0]
        self.assertEqual(browser_spec["name"], "browser")
        self.assertEqual(browser_spec["runnable"].name, "browser")
        self.assertIsNone(browser_spec["runnable"].checkpointer)
        browser = {tool.name for tool in build_browser_tools(object(), vision, settings)}  # type: ignore[arg-type]
        by_name = {
            str(spec["name"]): {tool.name for tool in spec["tools"]}  # type: ignore[attr-defined]
            for spec in specs
            if "tools" in spec
        }
        self.assertEqual(set(by_name), {"files", "desktop"})

        files, desktop = by_name["files"], by_name["desktop"]
        # The browser model navigates, reads, and hands the goal to Jev.
        # Clicks stay inside browser_drive. ask_user stays on the desktop specialist.
        self.assertEqual(
            browser,
            {"browser_navigate", "browser_read_page", "browser_drive"},
        )
        self.assertTrue(browser.isdisjoint(files))
        self.assertTrue(browser.isdisjoint(desktop))
        self.assertTrue(files.isdisjoint(desktop))

        self.assertNotIn("browser_click", browser)
        self.assertNotIn("ask_user", browser)
        self.assertNotIn("fs_download", browser)
        self.assertNotIn("desktop_click", browser)
        self.assertNotIn("open_live_desktop", browser)

        self.assertEqual(files, {"fs_download"})
        self.assertNotIn("ask_user", files)

        self.assertIn("desktop_click", desktop)
        self.assertIn("app_open", desktop)
        self.assertIn("ask_user", desktop)
        self.assertNotIn("browser_navigate", desktop)
        self.assertNotIn("fs_download", desktop)

        desktop_spec = next(spec for spec in specs if spec["name"] == "desktop")
        locked = next(
            item for item in desktop_spec["middleware"] if item.name == "FilesystemMiddleware"
        )
        self.assertEqual(set(locked._enabled_tools or ()), {"read_file"})

    def test_live_tool_name(self) -> None:
        tool = build_open_live_desktop(None)
        self.assertEqual(tool.name, "open_live_desktop")


if __name__ == "__main__":
    unittest.main()
