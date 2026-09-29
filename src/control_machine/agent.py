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
from collections.abc import Awaitable, Callable, Sequence
from urllib.parse import urlparse

from deepagents import (
    CompiledSubAgent,
    FilesystemPermission,
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.store.base import BaseStore

from .browser import BrowserSession
from .config import Settings, get_settings
from .fs import HOME_MOUNT, build_agent_backend, filesystem_permissions, host_path_exists
from .scene import SceneCallback
from .tools import (
    VisionBuffer,
    build_browser_tools,
    build_desktop_tools,
    build_fs_tools,
)

# The supervisor model may call only these. Filesystem and shell tools stay on specialists.
SUPERVISOR_TOOL_NAMES = frozenset({"task", "open_live_desktop", "read_file"})
_HIDDEN_FROM_SUPERVISOR = frozenset(
    {
        "execute",
        "ls",
        "write_file",
        "edit_file",
        "glob",
        "grep",
        "delete",
    }
)

SUPERVISOR_PROMPT = """You talk to the user and delegate.
You do not browse, edit files, or click the desktop yourself.

Call task with one of:
- browser: websites in Docker Chrome
- files: this Mac's files under /home, and downloads
- desktop: native apps (Slack, Finder, Notes)

When a skill matches, read its SKILL.md under /skills/supervisor/ and follow it.
You may read only /skills/.

When the user asks for a link, a live view, or to control this computer,
call open_live_desktop and send them the URL.
Answer in plain language with what the specialist reported. Do not invent results."""

BROWSER_PROMPT = """You drive Docker Chrome for the user.

Every tool result shows the page as a list of elements, each tagged with a ref like e12.
Act on refs: browser_click(ref=...), browser_type(ref=...). Never invent a ref.
One action per step. If a control is not in the element list, call browser_screenshot with
marks=True. Only fall back to browser_click_xy if that also fails.
Use browser_read_page to extract page content. After ask_user or a live update, treat the
current page as ground truth.

Never type passwords, payment details, one-time codes, or solve captchas. If a login,
captcha, sensitive field, or high-impact choice blocks you, call ask_user. The user can
open the live desktop from Telegram and reply when finished.
An authentication flow is still blocked while the page says "approve sign in", displays an
MFA number, waits for an authenticator, or asks for a verification code. Include any
displayed number in ask_user and wait.

Finish with a short summary of what you did or found. Do not include raw page dumps."""

FILES_PROMPT = """You manage files on this Mac. Home is /home.

Use ls, read_file, write_file, edit_file, glob, grep, delete.
Scratch notes can live outside /home; /memories/ survives between conversations.
Use fs_download to save a URL onto this Mac under /home — Chrome downloads stay inside Docker.

Finish with a short summary of the paths you read or changed."""

DESKTOP_PROMPT = """You drive native apps on this Mac.

Use app_open, desktop_screenshot, desktop_click, desktop_type, desktop_key for native
software (Slack, Finder, Notes). Do not use these tools for websites.

Never type passwords, payment details, one-time codes, or solve captchas. If a login,
captcha, or sensitive field blocks you, call ask_user and wait.

Finish with a short summary of what you did."""

LiveOpener = Callable[[], Awaitable[str]]


def _disable_general_purpose_subagent() -> None:
    """Stop Deep Agents from inserting a fourth agent that inherits the parent tools.

    Registrations merge, and only this field is set, so specialist filesystem tools stay.
    """
    profile = HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
    )
    register_harness_profile("openai", profile)
    register_harness_profile("ollama", profile)


_disable_general_purpose_subagent()


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


def skills_read_allowed(path: str) -> bool:
    """The supervisor reads skill files only. Home and memories stay on specialists."""
    text = path.strip()
    return text == "/skills" or text.startswith("/skills/")


def supervisor_visible_tools(tools: Sequence[object]) -> list[object]:
    """Keep the delegation tool and the live-link tool. Hide the filesystem and shell."""
    visible: list[object] = []
    for item in tools:
        name = getattr(item, "name", None)
        if name in _HIDDEN_FROM_SUPERVISOR:
            continue
        if name in SUPERVISOR_TOOL_NAMES or name == "open_live_desktop":
            visible.append(item)
    return visible


def repair_tool_pairs(messages: Sequence[object]) -> list[object]:
    """Drop tool results that are not a direct reply to the preceding assistant call.

    OpenAI rejects a tool message unless the previous message is an assistant
    message whose tool_calls include that id. Summarization can leave the result
    behind after the call is removed, which shows up as messages[2].role == tool.
    """
    repaired: list[object] = []
    index = 0
    items = list(messages)
    while index < len(items):
        msg = items[index]
        if isinstance(msg, AIMessage) and msg.tool_calls:
            pending = {tc.get("id") for tc in msg.tool_calls if tc.get("id")}
            tools: list[ToolMessage] = []
            extras: list[object] = []
            scan = index + 1
            while scan < len(items) and pending:
                nxt = items[scan]
                if isinstance(nxt, ToolMessage) and nxt.tool_call_id in pending:
                    tools.append(nxt)
                    pending.discard(nxt.tool_call_id)
                elif isinstance(nxt, ToolMessage):
                    pass
                elif isinstance(nxt, HumanMessage):
                    extras.append(nxt)
                else:
                    break
                scan += 1
            if pending:
                kept = [tc for tc in msg.tool_calls if tc.get("id") not in pending]
                content = msg.content or "Incomplete tool call was dropped."
                msg = msg.model_copy(update={"tool_calls": kept, "content": content})
            if isinstance(msg, AIMessage) and not msg.tool_calls:
                repaired.append(msg)
                repaired.extend(extras)
            else:
                repaired.append(msg)
                repaired.extend(tools)
                repaired.extend(extras)
            index = scan
            continue
        if isinstance(msg, ToolMessage):
            index += 1
            continue
        repaired.append(msg)
        index += 1
    return repaired


class SkillsReadGate(AgentMiddleware):
    """Reject supervisor read_file calls that are not under /skills/."""

    name = "skills_read_gate"

    def _blocked(self, request):  # type: ignore[no-untyped-def]
        call = request.tool_call
        if call.get("name") != "read_file":
            return None
        path = str(call.get("args", {}).get("file_path", ""))
        if skills_read_allowed(path):
            return None
        return ToolMessage(
            content="The supervisor may only read files under /skills/.",
            tool_call_id=call.get("id", ""),
            name="read_file",
            status="error",
        )

    def wrap_tool_call(self, request, handler):  # type: ignore[no-untyped-def]
        blocked = self._blocked(request)
        return blocked if blocked is not None else handler(request)

    async def awrap_tool_call(self, request, handler):  # type: ignore[no-untyped-def]
        blocked = self._blocked(request)
        return blocked if blocked is not None else await handler(request)


class RepairToolMessagesMiddleware(AgentMiddleware):
    """Repair tool-call pairing immediately before the model sees the messages."""

    name = "repair_tool_messages"

    def _apply(self, request):  # type: ignore[no-untyped-def]
        return request.override(messages=repair_tool_pairs(request.messages))

    def wrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        return handler(self._apply(request))

    async def awrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        return await handler(self._apply(request))


class SupervisorToolFilter(AgentMiddleware):
    """Show the supervisor only `task` and `open_live_desktop`.

    Subagents do not inherit this middleware, so the file agent still has filesystem tools.
    """

    name = "supervisor_tool_filter"

    def _filter(self, request):  # type: ignore[no-untyped-def]
        return request.override(tools=supervisor_visible_tools(request.tools))

    def wrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        return handler(self._filter(request))

    async def awrap_model_call(self, request, handler):  # type: ignore[no-untyped-def]
        return await handler(self._filter(request))


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


def _host_file_exists(settings: Settings, arg: str = "file_path"):
    """Interrupt when the tool would overwrite an existing file on this Mac."""

    def when(request) -> bool:  # type: ignore[no-untyped-def]
        raw = str(request.tool_call.get("args", {}).get(arg, "") or "")
        return host_path_exists(raw, settings)

    return when


def _host_path(arg: str = "file_path"):
    """Interrupt when the tool targets this Mac's /home mount."""

    def when(request) -> bool:  # type: ignore[no-untyped-def]
        raw = str(request.tool_call.get("args", {}).get(arg, "") or "")
        return raw == "/home" or raw.startswith("/home/")

    return when


def build_open_live_desktop(opener: LiveOpener | None) -> BaseTool:
    """Tool the supervisor calls when the user asks for a control link."""

    @tool
    async def open_live_desktop() -> str:
        """Mint a signed URL so the user can see and control this Mac.

        Call this when the user asks for a link, a live view, or to take over the computer.
        """
        if opener is None:
            return "Live desktop is not configured."
        try:
            url = await opener()
        except Exception as exc:  # noqa: BLE001 - the model should see the failure
            return f"Could not open a live desktop link: {exc}"
        return (
            f"Live desktop: {url}\n"
            "The user can click and type on that page. Wait until they say they are done."
        )

    return open_live_desktop


def _locked_filesystem(backend: object) -> FilesystemMiddleware:
    """read_file only, and never this Mac's home or long-term memories.

    FilesystemMiddleware requires read_file so large tool results can be evicted.
    An empty tool list is rejected by the library. Denying /home keeps the Mac's
    files on the file specialist.
    """
    return FilesystemMiddleware(
        backend=backend,  # type: ignore[arg-type]
        tools=["read_file"],
        _permissions=[
            FilesystemPermission(
                operations=["read", "write"],
                paths=[
                    HOME_MOUNT,
                    f"{HOME_MOUNT}/**",
                    "/memories",
                    "/memories/**",
                ],
                mode="deny",
            )
        ],
    )


def _ask_user_interrupt() -> dict[str, object]:
    return {
        "ask_user": {
            "allowed_decisions": ["respond"],
            "description": "The agent needs your help on the live desktop",
        }
    }


def build_browser_agent(
    *,
    session: BrowserSession,
    vision: VisionBuffer,
    scene: SceneCallback,
    running: asyncio.Event,
    settings: Settings,
    backend: object,
    model: BaseChatModel,
):
    """Compile the browser specialist as its own Deep Agent.

    No checkpointer: the task tool forwards the supervisor's thread id, and a second
    saver on that thread would overwrite the supervisor's checkpoint. Skills can be
    attached here later; they are not inherited from the supervisor.
    """
    provider = settings.resolved_llm_provider
    browser_tools: list[BaseTool] = [*build_browser_tools(session, vision, settings), ask_user]
    browser_interrupt: dict[str, object] = {
        **_ask_user_interrupt(),
        "browser_click_xy": {
            "allowed_decisions": ["approve", "reject"],
            "description": "The agent wants to click a raw screen position",
        },
    }
    if settings.allowed_domains:
        browser_interrupt["browser_navigate"] = {
            "allowed_decisions": ["approve", "edit", "reject"],
            "description": "The agent wants to visit a site outside the allowlist",
            "when": _off_allowlist(settings),
        }
    locked_home = [
        FilesystemPermission(
            operations=["read", "write"],
            paths=[HOME_MOUNT, f"{HOME_MOUNT}/**", "/memories", "/memories/**"],
            mode="deny",
        )
    ]
    return create_deep_agent(
        model=model,
        tools=browser_tools,
        system_prompt=BROWSER_PROMPT,
        middleware=[
            PauseGateMiddleware(running),
            SceneRefreshMiddleware(scene),
            ScreenshotVisionMiddleware(vision, provider),
            _locked_filesystem(backend),
            RepairToolMessagesMiddleware(),
        ],
        interrupt_on=browser_interrupt,  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        permissions=locked_home,
        skills=["/skills/browser/"],
        name="browser",
    )


def build_specialist_specs(
    *,
    session: BrowserSession,
    vision: VisionBuffer,
    scene: SceneCallback,
    running: asyncio.Event,
    settings: Settings,
    backend: object,
    model: BaseChatModel,
) -> list[dict[str, object]]:
    """Browser is a nested Deep Agent. File and desktop stay plain subagents."""
    provider = settings.resolved_llm_provider
    file_tools: list[BaseTool] = [*build_fs_tools(settings)]
    desktop_tools: list[BaseTool] = [*build_desktop_tools(settings, vision), ask_user]

    file_interrupt: dict[str, object] = {
        "delete": {
            "allowed_decisions": ["approve", "reject"],
            "description": "The agent wants to delete a file on this Mac",
            "when": _host_path(),
        },
        "write_file": {
            "allowed_decisions": ["approve", "reject"],
            "description": "The agent wants to overwrite an existing file on this Mac",
            "when": _host_file_exists(settings),
        },
        "edit_file": {
            "allowed_decisions": ["approve", "reject"],
            "description": "The agent wants to edit a file on this Mac",
            "when": _host_path(),
        },
    }

    browser = CompiledSubAgent(
        name="browser",
        description=(
            "Drive Docker Chrome: open pages, click, type, and read websites. "
            "Use for anything in a browser. This specialist loads its own skills."
        ),
        runnable=build_browser_agent(
            session=session,
            vision=vision,
            scene=scene,
            running=running,
            settings=settings,
            backend=backend,
            model=model,
        ),
    )
    return [
        browser,
        {
            "name": "files",
            "description": (
                "Read, write, and download files on this Mac under /home. "
                "Use for documents, downloads, and notes. Not for websites or apps."
            ),
            "system_prompt": FILES_PROMPT,
            "tools": file_tools,
            "middleware": [PauseGateMiddleware(running), RepairToolMessagesMiddleware()],
            "interrupt_on": file_interrupt,
        },
        {
            "name": "desktop",
            "description": (
                "Control native Mac apps: open them, screenshot the screen, click, and type. "
                "Use for Slack, Finder, Notes, and other apps that are not websites."
            ),
            "system_prompt": DESKTOP_PROMPT,
            "tools": desktop_tools,
            "middleware": [
                PauseGateMiddleware(running),
                ScreenshotVisionMiddleware(vision, provider),
                _locked_filesystem(backend),
                RepairToolMessagesMiddleware(),
            ],
            "interrupt_on": _ask_user_interrupt(),
            "permissions": [
                FilesystemPermission(
                    operations=["read", "write"],
                    paths=[HOME_MOUNT, f"{HOME_MOUNT}/**", "/memories", "/memories/**"],
                    mode="deny",
                )
            ],
        },
    ]


def build_agent(
    *,
    session: BrowserSession,
    vision: VisionBuffer,
    scene: SceneCallback,
    checkpointer: BaseCheckpointSaver,
    store: BaseStore,
    running: asyncio.Event,
    settings: Settings | None = None,
    open_live: LiveOpener | None = None,
):
    """Compile the Telegram-facing supervisor and its specialist subagents."""
    settings = settings or get_settings()
    backend = build_agent_backend(settings)
    model = build_model(settings)
    return create_deep_agent(
        model=model,
        tools=[build_open_live_desktop(open_live)],
        system_prompt=SUPERVISOR_PROMPT,
        middleware=[
            PauseGateMiddleware(running),
            SupervisorToolFilter(),
            SkillsReadGate(),
            RepairToolMessagesMiddleware(),
        ],
        skills=["/skills/supervisor/"],
        subagents=build_specialist_specs(
            session=session,
            vision=vision,
            scene=scene,
            running=running,
            settings=settings,
            backend=backend,
            model=model,
        ),
        backend=backend,
        permissions=filesystem_permissions(settings),
        checkpointer=checkpointer,
        store=store,
        name="supervisor",
    )
