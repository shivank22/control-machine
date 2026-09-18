"""Deep Agent wiring.

Two details are load-bearing for a small local model:

- The system prompt stays short. Deep Agents already inject their own middleware prompts,
  and gemma4 degrades noticeably once the system turn grows, so extra prose here costs
  accuracy rather than buying it.
- Screenshots reach the model as a user-role image, not as tool output. Ollama's gemma4
  template silently drops images attached to tool-role messages, so a picture returned
  directly from the screenshot tool would be invisible.
"""

from __future__ import annotations

import asyncio
from urllib.parse import urlparse

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from .browser import BrowserSession
from .config import Settings, get_settings
from .scene import SceneCallback
from .tools import VisionBuffer, build_browser_tools

SYSTEM_PROMPT = """You control a real Chrome browser for the user.

Every tool result shows the page as a list of elements, each tagged with a ref like e12.
Act on refs: browser_click(ref=...), browser_type(ref=...). Never invent a ref.

Working rules:
- One action per step. Each action returns the updated page, so do not re-snapshot for no reason.
- If a control is not in the element list, call browser_screenshot with marks=True, then click
  the ref shown in the legend. Only fall back to browser_click_xy if that also fails.
- Use browser_read_page to read or extract page content.
- If an action fails, take a fresh browser_snapshot and reconsider rather than repeating it.
- After ask_user returns, or after a live browser update is injected, treat that current page
  as ground truth. Do not keep assuming you are still on an earlier MFA or login screen.

Never type passwords, payment details, one-time codes, or solve captchas. If a login,
captcha, sensitive field, or ambiguous high-impact choice blocks you, call ask_user with a
short explanation. The user can take control of the live browser and reply when finished.
An authentication flow is still blocked while the page says "approve sign in", displays an
MFA number, waits for an authenticator, or asks for a verification code. In that situation,
include any displayed number in ask_user and wait; do not report the number and finish the run.

/memories/ survives between conversations. When a site turns out to be fiddly, save what you
learned to /memories/sites/<domain>.md so the next run is faster, and read that file first when
you return to a site.

Finish by telling the user in plain language what you did or found."""


class SceneRefreshMiddleware(AgentMiddleware):
    """Inject a live page observation queued by SceneCallback before the next model call."""

    name = "scene_refresh"

    def __init__(self, scene: SceneCallback) -> None:
        super().__init__()
        self._scene = scene

    async def awrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        update = self._scene.consume()
        if update:
            request = request.override(
                messages=[*request.messages, HumanMessage(content=update)]
            )
        return await handler(request)


class ScreenshotVisionMiddleware(AgentMiddleware):
    """Replays the latest screenshot as a user-role image before each model call.

    Only the most recent image is ever sent. Stale screenshots do not describe the page
    any more, and on a 131k context shared with page snapshots they are expensive noise.
    """

    name = "screenshot_vision"

    def __init__(self, vision: VisionBuffer, provider: str) -> None:
        super().__init__()
        self._vision = vision
        self._provider = provider

    async def awrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        image = self._vision.image_b64
        if image:
            request = request.override(
                messages=[*request.messages, _screenshot_message(self._vision, self._provider)]
            )
        return await handler(request)


def _screenshot_message(vision: VisionBuffer, provider: str) -> HumanMessage:
    data_url = f"data:image/jpeg;base64,{vision.image_b64}"
    # ChatOpenAI wants image_url as an object; ChatOllama accepts a data-URL string.
    image_block: dict[str, object]
    if provider == "openai":
        image_block = {"type": "image_url", "image_url": {"url": data_url}}
    else:
        image_block = {"type": "image_url", "image_url": data_url}
    return HumanMessage(
        content=[
            {"type": "text", "text": f"{vision.caption} (current view)"},
            image_block,
        ]
    )


class PauseGateMiddleware(AgentMiddleware):
    """Stop immediately before the next tool while the user controls the browser."""

    name = "pause_gate"

    def __init__(self, running: asyncio.Event) -> None:
        super().__init__()
        self._running = running

    async def awrap_tool_call(self, request, handler):  # type: ignore[no-untyped-def]
        await self._running.wait()
        return await handler(request)


@tool
async def ask_user(question: str) -> str:
    """Ask the user to intervene in the live browser or answer a blocking question.

    Args:
        question: A concise explanation of what the user needs to do or decide.
    """
    # HumanInTheLoopMiddleware replaces this call with the user's response.
    return question


def build_model(settings: Settings | None = None) -> BaseChatModel:
    settings = settings or get_settings()
    provider = settings.resolved_llm_provider
    if provider == "openai":
        if not settings.openai_api_key.strip():
            raise ValueError("OPENAI_API_KEY is required when LLM_PROVIDER=openai")
        return ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            temperature=0,
            base_url=settings.openai_base_url.strip() or None,
        )
    # gemma4 ships with temperature 1, which makes tool arguments unstable.
    return ChatOllama(
        model=settings.ollama_model,
        base_url=settings.ollama_base_url,
        temperature=0,
        num_ctx=16384,
    )


def _off_allowlist(settings: Settings):
    """Interrupt predicate: ask before visiting a domain outside the allowlist."""
    allowed = settings.allowed_domains

    def when(request) -> bool:  # type: ignore[no-untyped-def]
        if not allowed:
            return False
        url = str(request.tool_call.get("args", {}).get("url", ""))
        host = (urlparse(url).hostname or "").lower()
        return not any(host == d or host.endswith(f".{d}") for d in allowed)

    return when


def build_agent(
    *,
    session: BrowserSession,
    vision: VisionBuffer,
    scene: SceneCallback,
    checkpointer: BaseCheckpointSaver,
    store: BaseStore,
    running: asyncio.Event,
    settings: Settings | None = None,
):
    """Compile the browser agent."""
    settings = settings or get_settings()
    tools = [*build_browser_tools(session, vision, settings), ask_user]

    interrupt_on: dict[str, object] = {
        "ask_user": {
            "allowed_decisions": ["respond"],
            "description": "The agent needs your help in the live browser",
        },
        # Clicking raw coordinates bypasses every check the ref path gives us.
        "browser_click_xy": {
            "allowed_decisions": ["approve", "reject"],
            "description": "The agent wants to click a raw screen position",
        },
    }
    if settings.allowed_domains:
        interrupt_on["browser_navigate"] = {
            "allowed_decisions": ["approve", "edit", "reject"],
            "description": "The agent wants to visit a site outside the allowlist",
            "when": _off_allowlist(settings),
        }

    return create_deep_agent(
        model=build_model(settings),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=[
            PauseGateMiddleware(running),
            SceneRefreshMiddleware(scene),
            ScreenshotVisionMiddleware(vision, settings.resolved_llm_provider),
        ],
        backend=CompositeBackend(
            default=StateBackend(),
            # Everything under /memories/ goes to Postgres and outlives the thread.
            routes={"/memories/": StoreBackend(namespace=lambda _rt: ("memories",))},
        ),
        checkpointer=checkpointer,
        store=store,
        interrupt_on=interrupt_on,  # type: ignore[arg-type]
    )
