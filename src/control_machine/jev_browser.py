"""Jev chooses the next browser action. Code performs it.

The chat model owns the goal and, when a field must be filled, the characters to type.
Jev never sees the screen and never emits a URL, a ref, or typed text. Every option it
can return was minted from the current page.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from playwright.async_api import Error as PlaywrightError
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, TypeSafeError

from .browser import BrowserAction, BrowserError, BrowserSession
from .config import Settings

log = logging.getLogger(__name__)

JEV_MODEL = "jev-latest"
# Choice confidence moves with the size of the catalog, so the gate uses the selected
# option's own probability and how far it sits ahead of the runner-up.
MIN_TOP_PROBABILITY = 0.25
MIN_MARGIN = 0.08
SENSITIVE_YES = 0.65
GOAL_DONE_YES = 0.6
_RECENT_LIMIT = 8
_TYPE_INSTRUCTIONS = (
    "Reply with only the characters to type into this field. "
    "No quotes, labels, or explanation."
)
_SECRET_FIELD = re.compile(
    r"password|passcode|one-time|otp|verification code|cvv|cvc|card number|credit card",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StepDecision:
    """What the loop should do with one Jev answer. Pure data, no I/O."""

    outcome: str
    action_id: str
    top: float
    margin: float
    note: str


def choice_criteria(actions: list[BrowserAction]) -> dict[str, str]:
    """Option keys are action ids. Jev cannot select an id that is not here."""
    return {action.id: action.description for action in actions}


def questions_for(actions: list[BrowserAction]) -> dict[str, Choice | Noul]:
    return {
        "next_action": Choice(
            instructions=(
                "Which one next action best advances `goal` on this page? "
                "Pick an action id from `actions`. "
                "Choose done only when the goal is already achieved. "
                "Choose ask_user when no listed control can advance the goal, "
                "or a person must take over. "
                "Do not choose a control that submits, saves, pays, or sends "
                "unless `goal` explicitly asks for that."
            ),
            criteria=choice_criteria(actions),
        ),
        "goal_done": Noul(
            instructions=(
                "Is `goal` already satisfied by what `page` shows, given `recent_steps`?"
            ),
        ),
        "sensitive": Noul(
            instructions=(
                "Does `page` ask for a password, payment details, a one-time code, or a captcha?"
            ),
        ),
    }


def control_state(
    *,
    goal: str,
    url: str,
    title: str,
    visible_text: str,
    actions: list[BrowserAction],
    recent_steps: list[str],
) -> dict[str, Any]:
    return {
        "goal": goal,
        "page": {"url": url, "title": title, "visible_text": visible_text},
        "actions": [{"id": action.id, "description": action.description} for action in actions],
        "recent_steps": recent_steps[-_RECENT_LIMIT:],
    }


def decide(
    *,
    choice: str,
    probabilities: dict[str, float],
    goal_done: float,
    sensitive: float,
    known_ids: set[str],
) -> StepDecision:
    """Turn one Jev response into a stop or an action. Does not trust confidence alone."""
    top = float(probabilities.get(choice, 0.0))
    others = [value for key, value in probabilities.items() if key != choice]
    margin = top - (max(others) if others else 0.0)
    if sensitive >= SENSITIVE_YES:
        return StepDecision("need_user", choice, top, margin, "sensitive")
    if choice not in known_ids:
        return StepDecision("unknown", choice, top, margin, "unknown id")
    if choice == "ask_user":
        return StepDecision("need_user", choice, top, margin, "ask_user")
    if choice == "done":
        strong = top >= MIN_TOP_PROBABILITY and margin >= MIN_MARGIN
        if goal_done >= GOAL_DONE_YES and strong:
            return StepDecision("done", choice, top, margin, "done")
        return StepDecision("uncertain", choice, top, margin, "done but the goal is not met")
    if top < MIN_TOP_PROBABILITY or margin < MIN_MARGIN:
        return StepDecision("uncertain", choice, top, margin, "weak pick")
    return StepDecision("act", choice, top, margin, "act")


def missing_key_message() -> str:
    return (
        "TYPESAFE_API_KEY is not set, so browser_drive cannot choose actions. "
        "Add it to the environment and try again."
    )


async def drive_browser(
    *,
    session: BrowserSession,
    settings: Settings,
    model: BaseChatModel,
    goal: str,
    running: Any | None = None,
    vision: Any | None = None,
) -> str:
    """Run Jev until the goal is done, a person is needed, or the step budget ends."""
    key = settings.typesafe_api_key.strip()
    if not key:
        return missing_key_message()

    steps: list[str] = []
    model_name = JEV_MODEL
    failures = 0
    url, title, text = "", "", ""
    try:
        async with AsyncTypeSafeClient(api_key=key, model=JEV_MODEL) as client:
            for _ in range(max(1, settings.max_steps)):
                if running is not None:
                    await running.wait()
                actions, url, title, text = await _read_page(session, settings)
                known = {action.id: action for action in actions}
                response = await client.system_one(
                    state=control_state(
                        goal=goal,
                        url=url,
                        title=title,
                        visible_text=text,
                        actions=actions,
                        recent_steps=steps,
                    ),
                    questions=questions_for(actions),
                    model=JEV_MODEL,
                )
                model_name = response.model or model_name
                answer = response.choices["next_action"]
                decision = decide(
                    choice=answer.choice,
                    probabilities=dict(answer.probabilities),
                    goal_done=float(response.nouls["goal_done"].noul),
                    sensitive=float(response.nouls["sensitive"].noul),
                    known_ids=set(known),
                )
                if decision.outcome == "need_user":
                    return _need_user(decision, url=url, title=title, text=text, steps=steps)
                if decision.outcome == "done":
                    return _finish(
                        "The goal looks complete.",
                        url=url,
                        title=title,
                        text=text,
                        steps=steps,
                        model_name=model_name,
                    )
                if decision.outcome != "act":
                    return _finish(
                        _uncertain_line(decision),
                        url=url,
                        title=title,
                        text=text,
                        steps=steps,
                        model_name=model_name,
                    )

                action = known[decision.action_id]
                if _repeating(steps, action.id):
                    return _finish(
                        f"Stopped because {action.id} was chosen again without progress.",
                        url=url,
                        title=title,
                        text=text,
                        steps=steps,
                        model_name=model_name,
                    )
                try:
                    note = await _perform(session, model, goal, action, text, decision)
                except (BrowserError, PlaywrightError) as exc:
                    failures += 1
                    steps.append(f"{action.id} failed: {exc}")
                    log.warning("Browser action %s failed: %s", action.id, exc)
                    if failures >= 3:
                        return _finish(
                            "Stopped after repeated browser errors.",
                            url=url,
                            title=title,
                            text=text,
                            steps=steps,
                            model_name=model_name,
                        )
                    continue
                if note.startswith("NEED_USER:"):
                    return note
                failures = 0
                steps.append(note)
                if vision is not None:
                    vision.clear()
    except TypeSafeError as exc:
        log.warning("TypeSafe request failed: %s", exc)
        return _finish(
            f"TypeSafe could not choose an action: {exc}",
            url=url,
            title=title,
            text=text,
            steps=steps,
            model_name=model_name,
        )
    except (BrowserError, PlaywrightError) as exc:
        log.warning("Could not read the page: %s", exc)
        return _finish(
            f"Could not read the page: {exc}",
            url=url,
            title=title,
            text=text,
            steps=steps,
            model_name=model_name,
        )

    return _finish(
        "Stopped at the step limit.",
        url=url,
        title=title,
        text=text,
        steps=steps,
        model_name=model_name,
    )


async def _read_page(
    session: BrowserSession,
    settings: Settings,
) -> tuple[list[BrowserAction], str, str, str]:
    actions = await session.action_catalog()
    url, title = await session.page_location()
    text = await session.visible_text(limit=settings.snapshot_max_chars)
    return actions, url, title, text


async def _perform(
    session: BrowserSession,
    model: BaseChatModel,
    goal: str,
    action: BrowserAction,
    page_text: str,
    decision: StepDecision,
) -> str:
    if action.kind == "click":
        await session.click_ref(action.ref)
    elif action.kind == "type":
        if _SECRET_FIELD.search(action.description):
            return (
                "NEED_USER: The next field looks like a password, payment, or one-time code. "
                "Do not type it. Hand this back and wait for the user.\n"
                f"Field: {action.description}"
            )
        typed = await _text_to_type(model, goal=goal, field=action.description, page_text=page_text)
        if not typed:
            return (
                f"Stopped because there was nothing to type into {action.description}. "
                "Call browser_drive again with the value included in the goal."
            )
        await session.fill_ref(action.ref, typed)
        return f"{action.id} {action.description} entered {typed!r} (p={decision.top:.2f})"
    elif action.kind == "scroll":
        await session.scroll_page(action.value or "down")
    elif action.kind == "key":
        await session.press_key(action.value or "Enter")
    elif action.kind == "select":
        await session.select_ref(action.ref, action.value)
    elif action.kind == "click_xy":
        x_text, _, y_text = action.value.partition(",")
        await session.click_xy(float(x_text), float(y_text))
    else:
        return f"NEED_USER: Cannot perform {action.id}."
    return f"{action.id} {action.description} (p={decision.top:.2f})"


async def _text_to_type(
    model: BaseChatModel,
    *,
    goal: str,
    field: str,
    page_text: str,
) -> str:
    message = await model.ainvoke(
        [
            SystemMessage(content=_TYPE_INSTRUCTIONS),
            HumanMessage(
                content=(
                    f"Goal:\n{goal}\n\nField:\n{field}\n\n"
                    f"Page text:\n{page_text[:2000]}"
                )
            ),
        ]
    )
    return _plain_text(message)


def _plain_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, str):
                chunks.append(block)
            elif isinstance(block, dict):
                chunks.append(str(block.get("text") or ""))
        content = "\n".join(chunks)
    text = str(content).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    line = text.splitlines()[0].strip() if text else ""
    return line[:500]


def _repeating(steps: list[str], action_id: str) -> bool:
    tail = [_step_id(step) for step in steps[-2:]]
    return len(tail) == 2 and tail[0] == action_id and tail[1] == action_id


def _step_id(step: str) -> str:
    return step.split(" ", 1)[0]


def _uncertain_line(decision: StepDecision) -> str:
    return (
        f"Stopped because the next action was uncertain ({decision.note}). "
        f"Top choice {decision.action_id} probability {decision.top:.2f}, "
        f"margin {decision.margin:.2f}."
    )


def _need_user(
    decision: StepDecision,
    *,
    url: str,
    title: str,
    text: str,
    steps: list[str],
) -> str:
    if decision.note == "sensitive":
        reason = (
            "The page asks for a password, payment, a one-time code, or a captcha. "
            "Do not type secrets."
        )
    else:
        reason = "No listed control can advance the goal, or a person needs to take over."
    excerpt = text[:800]
    done = "\n".join(f"- {step}" for step in steps) or "- (none)"
    return (
        f"NEED_USER: {reason} Hand this message back and wait for the user.\n"
        f"url: {url}\n"
        f"title: {title}\n"
        f"visible text:\n{excerpt}\n\n"
        f"steps:\n{done}"
    )


def _finish(
    status: str,
    *,
    url: str,
    title: str,
    text: str,
    steps: list[str],
    model_name: str,
) -> str:
    done = "\n".join(f"- {step}" for step in steps) or "- (none)"
    where = f"url: {url}\ntitle: {title}\n" if url or title else ""
    excerpt = f"visible text:\n{text[:800]}\n\n" if text else ""
    return (
        f"{status}\n"
        f"{where}"
        f"{excerpt}"
        f"steps:\n{done}\n"
        f"model: {model_name}"
    )
