"""Telegram polling bridge.

A thin wrapper over ``python-telegram-bot ~= 21.x`` that:

  1. Builds a polling Application bound to ``TELEGRAM_BOT_TOKEN``
  2. Registers two ``MessageHandler`` s — one for text, one for
     ``filters.Document.ALL | filters.PHOTO``
  3. For text: hands the message body off to ``agent.runner.handle_turn``
  4. For attachments: downloads the file into the vault inbox under
     ``_inbox/raw/<YYYY-MM-DD>/<HHMMSS>-<safe-name>``, then dispatches
     to ``handle_turn`` with both the caption and the saved Path(s)
  5. Sends the runner's reply back over the same chat

Bridge-side intentionally does NOT inspect attachment contents or
classify by filename — that is the LLM's job once it ``Read`` s the
file under ``cwd=vault_root``.

Compared to v1: dropped the Orchestrator object and the two-message
``send_progress`` callback for now. The agentic runner replies once
per turn. If progressive replies become useful, route them through
PTB's ``message.reply_text`` from inside ``handle_turn``.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional, Sequence

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
    "AttachmentReplyFn",
    "MessageReplyFn",
    "build_application",
    "make_attachment_handler",
    "make_message_handler",
    "run_polling_loop",
]


logger = logging.getLogger(__name__)


# Async callback: takes (chat_id, text), returns reply text.
MessageReplyFn = Callable[[str, str], Awaitable[str]]

# Async callback: takes (chat_id, caption_text, attachments). The bridge
# downloads any incoming Document/Photo to the vault inbox and passes the
# saved Path(s) through this callback; the runner builds the per-turn
# message from those.
AttachmentReplyFn = Callable[[str, str, Sequence[Path]], Awaitable[str]]


# Whitelist for filename sanitization: alphanumerics plus . _ - survive
# verbatim; everything else (including spaces, slashes, unicode) is
# collapsed to a single ``_``. Repeated runs collapse to one ``_``.
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(name: str) -> str:
    """Collapse unsafe characters to ``_`` and trim leading/trailing junk.

    Empty input falls back to ``"file"``. The sanitizer is deliberately
    aggressive — the vault is a shared mount with iCloud/Drive sync and
    odd characters in filenames have historically caused sync failures.
    """
    if not name:
        return "file"
    safe = _FILENAME_SAFE_RE.sub("_", name).strip("_.")
    return safe or "file"


def _inbox_landing_path(
    *,
    vault_root: Path,
    original_name: str,
    now: datetime | None = None,
) -> Path:
    """Compute the deterministic landing path for an incoming attachment.

    Shape: ``<vault_root>/_inbox/raw/<YYYY-MM-DD>/<HHMMSS>-<safe-name>``.

    Determinism matters for idempotency: a Telegram retry with the same
    ``file_unique_id`` arriving within the same second resolves to the
    same path, so we never double-write. Callers must skip the download
    if the path already exists.
    """
    ts = now or datetime.now(tz=timezone.utc)
    day = ts.strftime("%Y-%m-%d")
    hms = ts.strftime("%H%M%S")
    safe_name = _sanitize_filename(original_name)
    return vault_root / "_inbox" / "raw" / day / f"{hms}-{safe_name}"


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


def make_attachment_handler(
    reply_fn: AttachmentReplyFn,
    *,
    vault_root: Path,
):
    """Build a PTB handler that downloads attachments and dispatches a turn.

    The handler:
      1. Pulls the Document (or largest PhotoSize) off the update.
      2. Computes a deterministic landing path under ``_inbox/raw/<date>/``.
      3. Downloads the file (skipping if the path already exists — Telegram
         retries with the same ``file_unique_id`` are common).
      4. Calls ``reply_fn(chat_id, caption, attachments)`` and sends the
         returned reply text back through the same HTML rendering path.
    """

    async def _handler(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message is None:
            return

        attachment_source, default_name = _pick_attachment_source(message)
        if attachment_source is None:
            return

        original_name = (
            getattr(attachment_source, "file_name", None) or default_name
        )
        landing = _inbox_landing_path(
            vault_root=vault_root, original_name=original_name
        )

        if not landing.exists():
            landing.parent.mkdir(parents=True, exist_ok=True)
            file_obj = await attachment_source.get_file()
            await file_obj.download_to_drive(landing)

        chat_id = str(message.chat_id)
        caption = message.caption or ""
        try:
            reply_text = await reply_fn(chat_id, caption, (landing,))
        except Exception:  # noqa: BLE001
            logger.exception("runner failed handling Telegram attachment")
            reply_text = "Sorry — something went wrong handling that attachment."
        await _send_reply(message, reply_text)

    return _handler


def _pick_attachment_source(message) -> tuple[object | None, str]:
    """Return ``(source, default_name)`` for the first attachment on the message.

    Documents win over photos when both are present (Telegram doesn't
    deliver both in practice, but the priority is explicit). For photos,
    the largest size (last in the ``photo`` tuple) is chosen — that's
    the original-resolution upload; the earlier entries are thumbnails.

    ``default_name`` is used when the attachment has no ``file_name``
    attribute (true for ``PhotoSize``). It is generated from the
    ``file_unique_id`` so retries collide on the same path.
    """
    document = getattr(message, "document", None)
    if document is not None:
        return document, getattr(document, "file_unique_id", "document")

    photos = getattr(message, "photo", None) or ()
    if photos:
        largest = photos[-1]
        return largest, f"{getattr(largest, 'file_unique_id', 'photo')}.jpg"

    return None, ""


def build_application(
    *,
    reply_fn: MessageReplyFn,
    attachment_reply_fn: AttachmentReplyFn | None = None,
    vault_root: Path | None = None,
    token: Optional[str] = None,
) -> Application:
    """Construct the PTB ``Application`` with text + attachment handlers.

    The text handler is always registered. The attachment handler is
    registered iff both ``attachment_reply_fn`` and ``vault_root`` are
    provided; passing only one of the two is a programming error.
    """
    if (attachment_reply_fn is None) != (vault_root is None):
        raise ValueError(
            "attachment_reply_fn and vault_root must both be supplied or both omitted"
        )

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
    if attachment_reply_fn is not None and vault_root is not None:
        application.add_handler(
            MessageHandler(
                filters.Document.ALL | filters.PHOTO,
                make_attachment_handler(
                    attachment_reply_fn, vault_root=vault_root
                ),
            )
        )
    return application


def run_polling_loop(
    *,
    reply_fn: MessageReplyFn,
    attachment_reply_fn: AttachmentReplyFn | None = None,
    vault_root: Path | None = None,
    token: Optional[str] = None,
) -> None:
    application = build_application(
        reply_fn=reply_fn,
        attachment_reply_fn=attachment_reply_fn,
        vault_root=vault_root,
        token=token,
    )
    application.run_polling()


def _build_default_reply_fn() -> MessageReplyFn:
    """Wire the default agentic runner as the text reply function."""
    from agent.runner import handle_turn

    async def _reply(chat_id: str, text: str) -> str:
        # The runner is synchronous (subprocess.run inside). We could
        # offload to a thread executor, but for a single-user assistant
        # the subprocess wait dominates and we'd rather keep the call
        # graph simple. PTB's polling is event-driven; one in-flight
        # turn at a time is the operating model.
        return handle_turn(chat_id=chat_id, user_msg=text)

    return _reply


def _build_default_attachment_reply_fn() -> AttachmentReplyFn:
    """Wire the default agentic runner as the attachment reply function."""
    from agent.runner import handle_turn

    async def _reply(chat_id: str, caption: str, attachments: Sequence[Path]) -> str:
        return handle_turn(chat_id=chat_id, user_msg=caption, attachments=attachments)

    return _reply


def _resolve_vault_root() -> Path:
    raw = os.environ.get("VAULT_ROOT")
    if not raw:
        raise RuntimeError("VAULT_ROOT is not set in the environment")
    return Path(raw).expanduser()


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
    run_polling_loop(
        reply_fn=_build_default_reply_fn(),
        attachment_reply_fn=_build_default_attachment_reply_fn(),
        vault_root=_resolve_vault_root(),
    )
