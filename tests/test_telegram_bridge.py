"""Smoke tests for the Telegram bridge handler.

Verifies that the PTB-style handler:
  * Delegates to the injected reply_fn
  * Sends the reply through the markdown -> Telegram HTML transformer
  * Falls back to plain text if Telegram rejects the HTML
  * Falls back to a user-visible error message when the reply_fn raises
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.constants import ParseMode
from telegram.error import BadRequest

from agent import telegram_bridge


@pytest.mark.asyncio
async def test_handler_delegates_and_sends_html() -> None:
    reply_fn = AsyncMock(return_value="**bold** reply")
    handler = telegram_bridge.make_message_handler(reply_fn)

    update = MagicMock()
    update.effective_message.text = "hi"
    update.effective_message.chat_id = 12345
    update.effective_message.reply_text = AsyncMock()

    await handler(update, MagicMock())

    reply_fn.assert_awaited_once_with("12345", "hi")
    # The reply was passed through markdown_to_telegram_html and sent
    # with parse_mode=HTML.
    update.effective_message.reply_text.assert_awaited_once()
    args, kwargs = update.effective_message.reply_text.call_args
    assert args[0] == "<b>bold</b> reply"
    assert kwargs["parse_mode"] == ParseMode.HTML
    assert kwargs["disable_web_page_preview"] is True


@pytest.mark.asyncio
async def test_handler_falls_back_to_plain_on_bad_request() -> None:
    reply_fn = AsyncMock(return_value="**bold** reply")
    handler = telegram_bridge.make_message_handler(reply_fn)

    update = MagicMock()
    update.effective_message.text = "hi"
    update.effective_message.chat_id = 1
    # First call (HTML) raises BadRequest; second call (plain) succeeds.
    update.effective_message.reply_text = AsyncMock(
        side_effect=[BadRequest("bad html"), None]
    )

    await handler(update, MagicMock())

    assert update.effective_message.reply_text.await_count == 2
    # The fallback call sends the ORIGINAL markdown text (no parse_mode).
    second_args, second_kwargs = update.effective_message.reply_text.await_args_list[1]
    assert second_args[0] == "**bold** reply"
    assert "parse_mode" not in second_kwargs


@pytest.mark.asyncio
async def test_handler_falls_back_on_reply_fn_exception() -> None:
    reply_fn = AsyncMock(side_effect=RuntimeError("boom"))
    handler = telegram_bridge.make_message_handler(reply_fn)

    update = MagicMock()
    update.effective_message.text = "hi"
    update.effective_message.chat_id = 1
    update.effective_message.reply_text = AsyncMock()

    await handler(update, MagicMock())

    args, _kwargs = update.effective_message.reply_text.call_args
    assert "something went wrong" in args[0].lower()


@pytest.mark.asyncio
async def test_handler_skips_non_text_messages() -> None:
    reply_fn = AsyncMock()
    handler = telegram_bridge.make_message_handler(reply_fn)

    update = MagicMock()
    update.effective_message = None

    await handler(update, MagicMock())
    reply_fn.assert_not_awaited()
