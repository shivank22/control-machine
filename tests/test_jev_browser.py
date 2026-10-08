"""Jev's browser catalog and stop rules, with no network and no TypeSafe call."""

from __future__ import annotations

import asyncio
import unittest

from control_machine.browser import catalog_from_snapshot
from control_machine.config import Settings
from control_machine.jev_browser import (
    MIN_MARGIN,
    MIN_TOP_PROBABILITY,
    choice_criteria,
    decide,
    drive_browser,
    questions_for,
)

_SNAPSHOT = """
- navigation "Primary":
  - link "Home" [ref=e1]
  - link "Flights" [ref=e2]
- main:
  - textbox "From" [ref=e14]
  - button "Search" [ref=e12]
  - generic "layout note" [ref=e99]
  - generic "menu" [ref=e7] [cursor=pointer]
  - link "Inside" [ref=f1e12]
"""


class CatalogTests(unittest.TestCase):
    def test_snapshot_ids_are_the_only_choice_keys(self) -> None:
        actions = catalog_from_snapshot(_SNAPSHOT)
        criteria = choice_criteria(actions)
        questions = questions_for(actions)

        self.assertEqual(set(criteria), {action.id for action in actions})
        self.assertEqual(set(questions["next_action"].criteria), set(criteria))
        self.assertIn("click:e12", criteria)
        self.assertIn("type:e14", criteria)
        self.assertIn("click:e7", criteria)
        self.assertIn("click:f1e12", criteria)
        self.assertNotIn("click:e99", criteria)
        self.assertNotIn("click:e999", criteria)
        self.assertIn("scroll:down", criteria)
        self.assertIn("done", criteria)
        self.assertIn("ask_user", criteria)

        by_id = {action.id: action for action in actions}
        self.assertEqual(by_id["type:e14"].kind, "type")
        self.assertEqual(by_id["click:e12"].kind, "click")
        self.assertEqual(by_id["click:e12"].ref, "e12")

    def test_caps_page_controls_and_keeps_standing_actions(self) -> None:
        lines = "\n".join(f'- button "B{i}" [ref=e{i}]' for i in range(1, 71))
        actions = catalog_from_snapshot(lines)
        clicks = [action for action in actions if action.kind == "click"]
        self.assertEqual(len(clicks), 60)
        self.assertEqual(clicks[-1].ref, "e60")
        self.assertIn("done", {action.id for action in actions})
        self.assertNotIn("click:e61", {action.id for action in actions})


class DecideTests(unittest.TestCase):
    def test_sensitive_page_stops_before_a_click(self) -> None:
        decision = decide(
            choice="click:e12",
            probabilities={"click:e12": 0.9, "done": 0.1},
            goal_done=0.1,
            sensitive=0.9,
            known_ids={"click:e12", "done"},
        )
        self.assertEqual(decision.outcome, "need_user")
        self.assertEqual(decision.note, "sensitive")

    def test_ask_user_hands_off(self) -> None:
        decision = decide(
            choice="ask_user",
            probabilities={"ask_user": 0.7, "click:e12": 0.3},
            goal_done=0.1,
            sensitive=0.1,
            known_ids={"ask_user", "click:e12"},
        )
        self.assertEqual(decision.outcome, "need_user")

    def test_done_requires_the_goal_to_look_complete(self) -> None:
        finished = decide(
            choice="done",
            probabilities={"done": 0.8, "click:e12": 0.2},
            goal_done=0.9,
            sensitive=0.0,
            known_ids={"done", "click:e12"},
        )
        early = decide(
            choice="done",
            probabilities={"done": 0.8, "click:e12": 0.2},
            goal_done=0.2,
            sensitive=0.0,
            known_ids={"done", "click:e12"},
        )
        self.assertEqual(finished.outcome, "done")
        self.assertEqual(early.outcome, "uncertain")

    def test_weak_pick_does_not_act(self) -> None:
        decision = decide(
            choice="click:e12",
            probabilities={
                "click:e12": MIN_TOP_PROBABILITY,
                "click:e3": MIN_TOP_PROBABILITY - 0.02,
            },
            goal_done=0.1,
            sensitive=0.0,
            known_ids={"click:e12", "click:e3"},
        )
        self.assertLess(decision.margin, MIN_MARGIN)
        self.assertEqual(decision.outcome, "uncertain")

    def test_clear_click_acts(self) -> None:
        decision = decide(
            choice="click:e12",
            probabilities={"click:e12": 0.8, "done": 0.1, "ask_user": 0.1},
            goal_done=0.1,
            sensitive=0.05,
            known_ids={"click:e12", "done", "ask_user"},
        )
        self.assertEqual(decision.outcome, "act")

    def test_unknown_id_is_rejected(self) -> None:
        decision = decide(
            choice="click:e999",
            probabilities={"click:e999": 0.9},
            goal_done=0.0,
            sensitive=0.0,
            known_ids={"click:e12", "done"},
        )
        self.assertEqual(decision.outcome, "unknown")


class DriveGuardTests(unittest.TestCase):
    def test_missing_key_does_not_touch_the_browser(self) -> None:
        settings = Settings(typesafe_api_key="", llm_provider="ollama")

        text = asyncio.run(
            drive_browser(
                session=object(),  # type: ignore[arg-type]
                settings=settings,
                model=object(),  # type: ignore[arg-type]
                goal="Search flights",
            )
        )

        self.assertIn("TYPESAFE_API_KEY", text)


if __name__ == "__main__":
    unittest.main()
