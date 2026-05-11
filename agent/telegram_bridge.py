"""Telegram polling bridge.

A thin wrapper over ``python-telegram-bot ~= 21.x`` that:

  1. Builds a polling Application bound to ``TELEGRAM_BOT_TOKEN``
  2. Registers a single ``MessageHandler`` for text messages
  3. Hands each message off to ``agent.runner.handle_turn``
  4. Sends the runner's reply back over the same chat

Compared to v1: dropped the Orchestrator object and the two-message
``send_progress`` callback for now. The agentic runner replies once
per turn. If progressive replies become useful, route them through
PTB's ``message.reply_text`` from inside ``handle_turn``.
"""

from __future__ import annotations

import logging
import os
from typing import Awaitable, Callable, Optional

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent.format import markdown_to_telegram_html

__all__ = [
    "MessageReplyFn",
    "build_application",
    "make_message_handler",
    "run_polling_loop",
]


logger = logging.getLogger(__name__)


# Async callback: takes (chat_id, text), returns reply text.
MessageReplyFn = Callable[[str, str], Awaitable[str]]


async def _send_reply(message, reply_text: str) -> None:
    """Send a reply with Telegram HTML rendering, falling back to plain text.

    Telegram parses a small HTML subset when ``parse_mode=HTML``. If our
    converted markup is malformed (unbalanced tags, missing quote, etc.)
    Telegram returns ``BadRequest`` and the message would be lost — so
    we retry once as plain text. Users see something either way.
    """
    html_text = markdown_to_telegram_html(reply_text)
    try:
        await message.reply_text(
            html_text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as err:
        logger.warning("Telegram rejected HTML reply (%s); falling back to plain", err)
        await message.reply_text(reply_text, disable_web_page_preview=True)


def make_message_handler(reply_fn: MessageReplyFn):
    """Build a PTB handler that delegates to ``reply_fn(chat_id, text)``."""

    async def _handler(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None or not message.text:
            return
        chat_id = str(message.chat_id)
        try:
            reply_text = await reply_fn(chat_id, message.text)
        except Exception:  # noqa: BLE001
            logger.exception("runner failed handling Telegram message")
            reply_text = "Sorry — something went wrong handling that message."
        await _send_reply(message, reply_text)

    return _handler


def build_application(
    *,
    reply_fn: MessageReplyFn,
    token: Optional[str] = None,
) -> Application:
    bot_token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in the environment")

    application = ApplicationBuilder().token(bot_token).build()
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            make_message_handler(reply_fn),
        )
    )
    return application


def run_polling_loop(
    *,
    reply_fn: MessageReplyFn,
    token: Optional[str] = None,
) -> None:
    application = build_application(reply_fn=reply_fn, token=token)
    application.run_polling()


def _build_default_reply_fn() -> MessageReplyFn:
    """Wire the default agentic runner as the reply function."""
    from agent.runner import handle_turn

    async def _reply(chat_id: str, text: str) -> str:
        # The runner is synchronous (subprocess.run inside). We could
        # offload to a thread executor, but for a single-user assistant
        # the subprocess wait dominates and we'd rather keep the call
        # graph simple. PTB's polling is event-driven; one in-flight
        # turn at a time is the operating model.
        return handle_turn(chat_id=chat_id, user_msg=text)

    return _reply


if __name__ == "__main__":
    # Auto-load .env from the repo root so the operator doesn't have to
    # remember to `source .env` before running. Fails silently if no
    # .env is present (env vars may still come from the shell or launchd).
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    run_polling_loop(reply_fn=_build_default_reply_fn())
