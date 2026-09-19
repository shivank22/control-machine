"""Telegram operator for control-machine.

The bot chats with the runner and, when a human needs the live desktop, sends a
signed viewer URL. VNC itself cannot resume the agent; Done / Cancel stay in Telegram.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .browser import BrowserError
from .config import Settings
from .db import Database
from .events import EventBus
from .fs import inbox_dir
from .live import LiveError, LiveSessions, open_live_session
from .runner import RunManager, RunnerBusy

log = logging.getLogger(__name__)

_TERMINAL = frozenset({"done", "error", "cancelled"})
_TELEGRAM_LIMIT = 3900


class TelegramBridge:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        runner: RunManager,
        bus: EventBus,
        live: LiveSessions,
    ) -> None:
        self.settings = settings
        self.db = db
        self.runner = runner
        self.bus = bus
        self.live = live
        self._app: Application | None = None
        self._events_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[tuple[int, dict[str, Any]]] | None = None

    async def start(self) -> None:
        token = self.settings.telegram_bot_token.strip()
        if not token:
            log.info("Telegram bot disabled (no TELEGRAM_BOT_TOKEN).")
            return
        application = Application.builder().token(token).build()
        application.add_handler(CommandHandler("start", self._cmd_start))
        application.add_handler(CommandHandler("help", self._cmd_help))
        application.add_handler(CommandHandler("new", self._cmd_new))
        application.add_handler(CommandHandler("watch", self._cmd_watch))
        application.add_handler(CommandHandler("cancel", self._cmd_cancel))
        application.add_handler(CallbackQueryHandler(self._on_callback))
        application.add_handler(
            MessageHandler(filters.Document.ALL | filters.PHOTO, self._on_file)
        )
        application.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text)
        )
        application.add_error_handler(self._on_error)
        await application.initialize()
        await application.start()
        assert application.updater is not None
        await application.updater.start_polling(drop_pending_updates=True)
        self._app = application
        self._queue = self.bus.subscribe_all()
        self._events_task = asyncio.create_task(self._pump_events(), name="telegram-events")
        allow = ",".join(str(i) for i in sorted(self.settings.telegram_user_ids)) or "(empty)"
        log.info("Telegram bot polling. allowlist=%s public=%s", allow, self.live.public_base_url())

    async def stop(self) -> None:
        if self._events_task is not None:
            self._events_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._events_task
            self._events_task = None
        if self._queue is not None:
            self.bus.unsubscribe_all(self._queue)
            self._queue = None
        if self._app is None:
            return
        assert self._app.updater is not None
        await self._app.updater.stop()
        await self._app.stop()
        await self._app.shutdown()
        self._app = None

    # ---------------------------------------------------------------- commands

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        await update.effective_message.reply_text(
            "I run on this Mac. Send a task and I will use Chrome, files, and desktop apps.\n"
            "Send a document and I save it under Downloads/control-machine.\n"
            "When a login blocks the run I send a live desktop link — tap Type on that page "
            "to use your phone keyboard.\n\n"
            "/watch — open this Mac's desktop\n"
            "/new — start a fresh conversation\n"
            "/cancel — stop the current run"
        )

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self._cmd_start(update, context)

    async def _cmd_new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        chat_id = update.effective_chat.id
        await self.db.clear_telegram_thread(chat_id)
        await update.effective_message.reply_text("New conversation. Send the next task.")

    async def _cmd_watch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        task = await self._current_task(update.effective_chat.id)
        task_id = int(task["id"]) if task else 0
        await self._send_live_link(
            update.effective_chat.id,
            task_id,
            "Open this Mac's desktop. Use Keyboard on that page to type.",
        )

    async def _cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        task = await self._current_task(update.effective_chat.id)
        if not task:
            await update.effective_message.reply_text("No active task to cancel.")
            return
        await self._cancel_task(update.effective_chat.id, int(task["id"]))

    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        text = (update.effective_message.text or "").strip()
        if not text:
            return
        chat_id = update.effective_chat.id
        binding = await self.db.get_telegram_thread(chat_id)
        last = None
        if binding and binding.get("last_task_id"):
            last = await self.db.get_task(int(binding["last_task_id"]))
        try:
            if last and last.get("status") == "awaiting_approval":
                await self.runner.decide(
                    int(last["id"]),
                    [{"type": "respond", "message": text}],
                )
                await update.effective_message.reply_text("Resuming the agent.")
                return
            if last and last.get("status") in {"running", "paused"}:
                await update.effective_message.reply_text(
                    "This run is still active. Send /cancel, /watch, or wait for it to finish."
                )
                return
            if last:
                task = await self.runner.follow_up(int(last["id"]), text)
            else:
                task = await self.runner.start(text)
        except RunnerBusy as exc:
            await update.effective_message.reply_text(str(exc))
            return
        except (BrowserError, ValueError) as exc:
            await update.effective_message.reply_text(str(exc))
            return
        except Exception:
            log.exception("Telegram text handler failed for chat %s", chat_id)
            await update.effective_message.reply_text(
                "Something went wrong on this Mac. I logged the error."
            )
            return
        await self.db.upsert_telegram_thread(
            chat_id, thread_id=task["thread_id"], task_id=int(task["id"])
        )
        await update.effective_message.reply_text(
            "Working on it.",
            reply_markup=_control_keyboard(int(task["id"]), live_url=None),
        )

    async def _on_file(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._gate(update):
            return
        message = update.effective_message
        if message is None:
            return
        dest_dir = inbox_dir(self.settings)
        document = message.document
        if document is not None:
            name = _safe_filename(document.file_name or f"upload-{document.file_unique_id}")
            telegram_file = await document.get_file()
        elif message.photo:
            photo = message.photo[-1]
            name = f"photo-{photo.file_unique_id}.jpg"
            telegram_file = await photo.get_file()
        else:
            return
        dest = dest_dir / name
        try:
            await telegram_file.download_to_drive(custom_path=dest)
        except OSError as exc:
            log.exception("Could not save Telegram file to %s", dest)
            await message.reply_text(f"Could not save the file: {exc}")
            return
        caption = (message.caption or "").strip()
        extra = f"\nCaption: {caption}" if caption else ""
        await message.reply_text(f"Saved to {dest}{extra}")

    async def _on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not await self._gate(update):
            await query.answer()
            return
        await query.answer()
        data = query.data or ""
        chat_id = update.effective_chat.id
        try:
            action, raw_id = data.split(":", 1)
            task_id = int(raw_id)
        except ValueError:
            return
        if action == "watch":
            await self._send_live_link(
                chat_id, task_id, "Open this Mac's desktop. This link is fresh."
            )
        elif action == "done":
            await self._done(chat_id, task_id)
        elif action == "cancel":
            await self._cancel_task(chat_id, task_id)
        elif action == "approve":
            await self._decide(chat_id, task_id, {"type": "approve"})
        elif action == "reject":
            await self._decide(chat_id, task_id, {"type": "reject", "message": "Rejected from Telegram."})

    async def _on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Telegram update failed: %s", context.error, exc_info=context.error)
        message = getattr(update, "effective_message", None) if update is not None else None
        if message is None:
            return
        with contextlib.suppress(Exception):
            await message.reply_text("Something went wrong on this Mac. I logged the error.")

    # ---------------------------------------------------------------- events

    async def _pump_events(self) -> None:
        assert self._queue is not None
        try:
            while True:
                task_id, event = await self._queue.get()
                try:
                    await self._handle_event(task_id, event)
                except Exception:  # noqa: BLE001 - keep the pump alive
                    log.exception("Telegram event handler failed for task %s", task_id)
        except asyncio.CancelledError:
            raise

    async def _handle_event(self, task_id: int, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind not in {"interrupt", "message", "status"}:
            return
        if kind == "message" and event.get("role") != "assistant":
            return
        if kind == "status" and event.get("status") not in _TERMINAL | {"awaiting_approval"}:
            return
        chat_id = await self._chat_for_task(task_id)
        if chat_id is None:
            return
        if kind == "interrupt":
            question = event.get("question") or "The agent needs you on the live desktop."
            requests = event.get("requests") or []
            name = requests[0].get("name") if requests else None
            session = await self._try_mint(task_id)
            url = str(session["url"]) if session else None
            extra = name if name and name != "ask_user" else None
            body = _clip(str(question))
            if url and not url.startswith("https://"):
                body = f"{body}\n\n{url}"
            await self._send(
                chat_id,
                body,
                reply_markup=_interrupt_keyboard(task_id, url, tool=extra),
            )
            return
        if kind == "message":
            await self._send(chat_id, _clip(str(event.get("text") or "")))
            return
        status = event.get("status")
        if status == "awaiting_approval":
            return
        if status == "done":
            await self._send(chat_id, "Done.")
        elif status == "error":
            await self._send(chat_id, f"Error: {_clip(str(event.get('error') or 'unknown'))}")
        elif status == "cancelled":
            await self._send(chat_id, "Cancelled.")

    # ---------------------------------------------------------------- helpers

    async def _done(self, chat_id: int, task_id: int) -> None:
        if not task_id:
            await self._send(chat_id, "No agent run to resume.")
            return
        task = await self.db.get_task(task_id)
        if not task:
            await self._send(chat_id, f"No task {task_id}.")
            return
        status = task.get("status")
        try:
            if status == "awaiting_approval":
                await self.runner.decide(
                    task_id,
                    [{"type": "respond", "message": "Done from Telegram."}],
                )
                await self._send(chat_id, "Resuming the agent.")
            elif status == "paused":
                await self.runner.resume(task_id)
                await self._send(chat_id, "Resuming the agent.")
            else:
                await self._send(chat_id, "Nothing to resume.")
        except (RunnerBusy, ValueError) as exc:
            await self._send(chat_id, str(exc))

    async def _decide(self, chat_id: int, task_id: int, decision: dict[str, Any]) -> None:
        try:
            await self.runner.decide(task_id, [decision])
        except (RunnerBusy, ValueError) as exc:
            await self._send(chat_id, str(exc))
            return
        await self._send(chat_id, "Resuming the agent.")

    async def _cancel_task(self, chat_id: int, task_id: int) -> None:
        if not task_id:
            await self._send(chat_id, "No agent run to cancel.")
            return
        try:
            await self.runner.cancel(task_id)
        except ValueError as exc:
            await self._send(chat_id, str(exc))
            return
        self.live.revoke(task_id)

    async def _send_live_link(self, chat_id: int, task_id: int, caption: str) -> None:
        session = await self._try_mint(task_id)
        if session is None:
            await self._send(
                chat_id,
                "Could not open a live desktop link. Check that control-machine is running.",
            )
            return
        url = str(session["url"])
        await self._send(
            chat_id,
            caption if url.startswith("https://") else f"{caption}\n{url}",
            reply_markup=_control_keyboard(task_id, live_url=url),
        )

    async def _try_mint(self, task_id: int) -> dict[str, object] | None:
        try:
            return await open_live_session(
                task_id,
                runner=self.runner,
                db=self.db,
                sessions=self.live,
            )
        except LiveError as exc:
            log.info("Could not mint live session for task %s: %s", task_id, exc)
            return None

    async def _current_task(self, chat_id: int) -> dict[str, Any] | None:
        binding = await self.db.get_telegram_thread(chat_id)
        if not binding or not binding.get("last_task_id"):
            return None
        return await self.db.get_task(int(binding["last_task_id"]))

    async def _chat_for_task(self, task_id: int) -> int | None:
        chat_id = await self.db.get_telegram_chat_for_task(task_id)
        if chat_id is not None:
            return chat_id
        notify = self.settings.notify_chat_id
        if notify:
            task = await self.db.get_task(task_id)
            if task:
                await self.db.upsert_telegram_thread(
                    notify, thread_id=task["thread_id"], task_id=task_id
                )
            return notify
        return None

    async def _gate(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        allow = self.settings.telegram_user_ids
        if not allow:
            if update.effective_message:
                await update.effective_message.reply_text(
                    f"Your Telegram user id is {user.id}. Add it to TELEGRAM_ALLOWLIST."
                )
            return False
        if user.id not in allow:
            if update.effective_message:
                await update.effective_message.reply_text("You are not on the allowlist.")
            return False
        return True

    async def _send(
        self,
        chat_id: int,
        text: str,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        if self._app is None or not text:
            return
        await self._app.bot.send_message(
            chat_id,
            text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )


def _control_keyboard(task_id: int, live_url: str | None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if live_url and live_url.startswith("https://"):
        rows.append([InlineKeyboardButton("Open desktop", url=live_url)])
    rows.append([InlineKeyboardButton("New live link", callback_data=f"watch:{task_id}")])
    rows.append(
        [
            InlineKeyboardButton("Done", callback_data=f"done:{task_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"cancel:{task_id}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def _interrupt_keyboard(
    task_id: int, live_url: str | None, *, tool: str | None
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if live_url and live_url.startswith("https://"):
        rows.append([InlineKeyboardButton("Open desktop", url=live_url)])
    else:
        rows.append([InlineKeyboardButton("Watch", callback_data=f"watch:{task_id}")])
    if tool and tool != "ask_user":
        rows.append(
            [
                InlineKeyboardButton("Approve", callback_data=f"approve:{task_id}"),
                InlineKeyboardButton("Reject", callback_data=f"reject:{task_id}"),
            ]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton("Done", callback_data=f"done:{task_id}"),
                InlineKeyboardButton("Cancel", callback_data=f"cancel:{task_id}"),
            ]
        )
    return InlineKeyboardMarkup(rows)


def _clip(text: str) -> str:
    text = text.strip()
    if len(text) <= _TELEGRAM_LIMIT:
        return text
    return text[: _TELEGRAM_LIMIT - 1] + "…"


def _safe_filename(name: str) -> str:
    base = Path(name).name.strip() or "upload"
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    return cleaned[:120] or "upload"
