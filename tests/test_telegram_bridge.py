"""Smoke tests for the Telegram bridge handler.

Verifies that the PTB-style handler:
  * Delegates to the injected reply_fn
  * Sends the reply through the markdown -> Telegram HTML transformer
  * Falls back to plain text if Telegram rejects the HTML
  * Falls back to a user-visible error message when the reply_fn raises
  * Captures Document/Photo attachments to the vault inbox and forwards
    the saved Path(s) to the runner.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
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


# ---------------------------------------------------------------------------
# Attachment handler — Document / Photo capture into vault inbox
# ---------------------------------------------------------------------------


def _make_document_update(
    *,
    chat_id: int = 999,
    file_id: str = "AgADBAADq6cxG5",
    file_unique_id: str = "uniq-abc",
    file_name: str = "Statement.pdf",
    caption: str | None = None,
    download_bytes: bytes = b"%PDF-1.4 fake",
) -> tuple[MagicMock, AsyncMock]:
    """Build a MagicMock ``Update`` that looks like a Document message.

    Returns ``(update, download_mock)`` so tests can inspect the download
    call. The mocked ``get_file().download_to_drive(path)`` writes
    ``download_bytes`` to the requested path so the rest of the bridge
    code can treat the file as real.
    """
    update = MagicMock()
    update.effective_message.chat_id = chat_id
    update.effective_message.text = None
    update.effective_message.caption = caption
    update.effective_message.photo = ()

    doc = MagicMock()
    doc.file_id = file_id
    doc.file_unique_id = file_unique_id
    doc.file_name = file_name
    update.effective_message.document = doc

    file_obj = MagicMock()

    async def _download_to_drive(path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(download_bytes)
        return Path(path)

    download_mock = AsyncMock(side_effect=_download_to_drive)
    file_obj.download_to_drive = download_mock

    async def _get_file():
        return file_obj

    update.effective_message.document.get_file = _get_file

    # No-op reply_text so the handler can post an ack.
    update.effective_message.reply_text = AsyncMock()
    return update, download_mock


@pytest.mark.asyncio
async def test_attachment_handler_saves_document_to_inbox(tmp_path: Path) -> None:
    reply_fn = AsyncMock(return_value="ack")
    handler = telegram_bridge.make_attachment_handler(reply_fn, vault_root=tmp_path)

    update, download_mock = _make_document_update(file_name="Statement.pdf")

    await handler(update, MagicMock())

    # Exactly one file was downloaded.
    download_mock.assert_awaited_once()
    saved_path = Path(download_mock.await_args.args[0])
    assert saved_path.exists()
    # Path shape: <vault>/_inbox/raw/<YYYY-MM-DD>/<HHMMSS>-Statement.pdf
    rel = saved_path.relative_to(tmp_path)
    parts = rel.parts
    assert parts[0] == "_inbox"
    assert parts[1] == "raw"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[2])
    assert re.fullmatch(r"\d{6}-Statement\.pdf", parts[3])


@pytest.mark.asyncio
async def test_attachment_handler_forwards_caption_and_paths(tmp_path: Path) -> None:
    reply_fn = AsyncMock(return_value="got it")
    handler = telegram_bridge.make_attachment_handler(reply_fn, vault_root=tmp_path)

    update, download_mock = _make_document_update(
        chat_id=42,
        file_name="Chase-Apr.pdf",
        caption="april statement",
    )

    await handler(update, MagicMock())

    reply_fn.assert_awaited_once()
    args, _kwargs = reply_fn.await_args
    assert args[0] == "42"
    assert args[1] == "april statement"
    paths = args[2]
    assert len(paths) == 1
    assert isinstance(paths[0], Path)
    assert paths[0].name.endswith("-Chase-Apr.pdf")
    # The saved path was actually written to.
    saved = Path(download_mock.await_args.args[0])
    assert paths[0] == saved


@pytest.mark.asyncio
async def test_attachment_handler_empty_caption(tmp_path: Path) -> None:
    """When the user attaches a file with no caption, caption_text must
    be the empty string (NOT None) — the runner's user-message builder
    relies on string concatenation."""
    reply_fn = AsyncMock(return_value="ack")
    handler = telegram_bridge.make_attachment_handler(reply_fn, vault_root=tmp_path)

    update, _download = _make_document_update(caption=None)

    await handler(update, MagicMock())

    args, _kwargs = reply_fn.await_args
    assert args[1] == ""
    assert isinstance(args[1], str)


@pytest.mark.asyncio
async def test_attachment_handler_sanitizes_filename(tmp_path: Path) -> None:
    """Filenames with spaces / special chars / unicode collapse to ``_``.

    Only ``[A-Za-z0-9._-]`` survive; everything else (including ``$``,
    ``%``, spaces, ``é``) becomes a single underscore. Repeated runs of
    unsafe chars collapse to one ``_``.
    """
    reply_fn = AsyncMock(return_value="ack")
    handler = telegram_bridge.make_attachment_handler(reply_fn, vault_root=tmp_path)

    update, download_mock = _make_document_update(
        file_name="My Statement (Apr 2026).pdf",
    )

    await handler(update, MagicMock())

    saved_path = Path(download_mock.await_args.args[0])
    # Spaces and parens both become underscores; repeated unsafe chars
    # collapse to a single underscore.
    assert saved_path.name.endswith("-My_Statement_Apr_2026_.pdf") or \
        saved_path.name.endswith("-My_Statement_Apr_2026.pdf")
    # No spaces, parens, or other unsafe characters survived.
    assert " " not in saved_path.name
    assert "(" not in saved_path.name
    assert ")" not in saved_path.name


def test_sanitize_filename_unit() -> None:
    """Spot-check the sanitizer in isolation."""
    f = telegram_bridge._sanitize_filename
    assert f("simple.pdf") == "simple.pdf"
    assert f("a b c.pdf") == "a_b_c.pdf"
    assert f("Chase-Apr_2026.pdf") == "Chase-Apr_2026.pdf"
    # Repeated unsafe characters collapse to a single underscore.
    assert f("foo   bar.pdf") == "foo_bar.pdf"
    assert f("café.pdf") == "caf_.pdf"
    # Leading/trailing junk is trimmed.
    assert f("...weird...") == "weird"
    # Empty / all-unsafe falls back.
    assert f("") == "file"
    assert f("$$$") == "file"


@pytest.mark.asyncio
async def test_attachment_handler_skips_download_when_path_exists(tmp_path: Path) -> None:
    """Telegram retries deliver the same ``file_unique_id`` again; since
    the landing path is deterministic per second + filename, a retry
    arriving within the same second collides on path. Behavior: skip the
    download and keep the existing file.
    """
    reply_fn = AsyncMock(return_value="ack")
    handler = telegram_bridge.make_attachment_handler(reply_fn, vault_root=tmp_path)

    # Pre-create the file at a path the handler would also choose. Since
    # the timestamp is "now", we can't fully guarantee equality, so we
    # instead drive determinism by stubbing ``_inbox_landing_path``.
    target = tmp_path / "_inbox" / "raw" / "2026-05-11" / "120000-dup.pdf"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"original-content")

    import agent.telegram_bridge as tb

    orig = tb._inbox_landing_path
    tb._inbox_landing_path = lambda **_kw: target  # type: ignore[assignment]
    try:
        update, download_mock = _make_document_update(
            file_name="dup.pdf",
            download_bytes=b"new-content-should-not-overwrite",
        )
        await handler(update, MagicMock())
    finally:
        tb._inbox_landing_path = orig  # type: ignore[assignment]

    # Download was skipped entirely.
    download_mock.assert_not_awaited()
    # File contents are untouched.
    assert target.read_bytes() == b"original-content"
    # Runner still receives the path so it can re-process if it wants.
    args, _kw = reply_fn.await_args
    assert args[2] == (target,)


def _make_photo_update(
    *,
    chat_id: int = 7,
    caption: str | None = None,
    sizes: tuple[tuple[str, int], ...] = (
        ("photo-thumb", 200),
        ("photo-medium", 800),
        ("photo-largest", 3000),
    ),
) -> tuple[MagicMock, AsyncMock]:
    """Build a MagicMock Update for a Telegram photo message.

    Telegram delivers photos as a tuple of ``PhotoSize`` from smallest to
    largest. We want the bridge to pick the LAST (largest) one — that's
    the original-resolution version. Each PhotoSize has a ``file_size``
    int and ``file_unique_id``.
    """
    update = MagicMock()
    update.effective_message.chat_id = chat_id
    update.effective_message.text = None
    update.effective_message.caption = caption
    update.effective_message.document = None
    update.effective_message.reply_text = AsyncMock()

    photos = []
    file_objs = []
    for uniq, size in sizes:
        ps = MagicMock()
        ps.file_unique_id = uniq
        ps.file_size = size
        # PhotoSize has no file_name attribute — the bridge must invent one.
        ps.file_name = None
        file_obj = MagicMock()
        file_objs.append(file_obj)

        async def _download_to_drive(path, _content=uniq.encode()):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(_content)
            return Path(path)

        file_obj.download_to_drive = AsyncMock(side_effect=_download_to_drive)

        async def _get_file(_fo=file_obj):
            return _fo

        ps.get_file = _get_file
        photos.append(ps)

    update.effective_message.photo = tuple(photos)
    # Return the download mock for the LARGEST size — that's what the
    # handler should call.
    return update, file_objs[-1].download_to_drive


@pytest.mark.asyncio
async def test_attachment_handler_picks_largest_photo(tmp_path: Path) -> None:
    """Telegram delivers photos as a tuple of sizes (smallest → largest).
    The bridge must pick the largest and ignore the smaller previews."""
    reply_fn = AsyncMock(return_value="ack")
    handler = telegram_bridge.make_attachment_handler(reply_fn, vault_root=tmp_path)

    update, largest_download = _make_photo_update(caption="snapshot")

    await handler(update, MagicMock())

    largest_download.assert_awaited_once()
    saved_path = Path(largest_download.await_args.args[0])
    assert saved_path.exists()
    # Content matches the largest size's stub bytes.
    assert saved_path.read_bytes() == b"photo-largest"
    # Reply fn was called with the caption + the saved path.
    args, _kw = reply_fn.await_args
    assert args[1] == "snapshot"
    assert args[2] == (saved_path,)


@pytest.mark.asyncio
async def test_attachment_handler_ignores_message_with_no_attachment(
    tmp_path: Path,
) -> None:
    """If neither document nor photo is present, handler is a no-op (no
    file IO, no reply_fn call)."""
    reply_fn = AsyncMock()
    handler = telegram_bridge.make_attachment_handler(reply_fn, vault_root=tmp_path)

    update = MagicMock()
    update.effective_message.chat_id = 1
    update.effective_message.text = "just text"
    update.effective_message.caption = None
    update.effective_message.document = None
    update.effective_message.photo = ()
    update.effective_message.reply_text = AsyncMock()

    await handler(update, MagicMock())
    reply_fn.assert_not_awaited()


def test_build_application_registers_text_and_attachment_handlers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_application`` must register BOTH the text handler and an
    attachment handler covering ``filters.Document.ALL | filters.PHOTO``.

    We don't connect to Telegram — we inspect ``application.handlers``
    after the build.
    """
    from telegram.ext import MessageHandler

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-for-test")
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))

    async def _text_reply(_chat_id: str, _text: str) -> str:
        return "ok"

    async def _attach_reply(_chat_id: str, _text: str, _atts) -> str:
        return "ok"

    app = telegram_bridge.build_application(
        reply_fn=_text_reply,
        attachment_reply_fn=_attach_reply,
        vault_root=tmp_path,
    )

    handlers = [h for group in app.handlers.values() for h in group]
    message_handlers = [h for h in handlers if isinstance(h, MessageHandler)]
    assert len(message_handlers) >= 2

    filter_strs = [str(h.filters) for h in message_handlers]
    joined = " | ".join(filter_strs)
    # One handler still covers TEXT (existing behavior).
    assert "TEXT" in joined or "text" in joined.lower()
    # Another covers Document/Photo (new behavior).
    assert "Document" in joined or "PHOTO" in joined or "photo" in joined.lower()


# ---------------------------------------------------------------------------
# In-process digest scheduling — JobQueue jobs registered at startup (#9)
# ---------------------------------------------------------------------------


class _FakeJobQueue:
    """Records ``run_daily`` calls without touching APScheduler.

    PTB's real ``JobQueue.run_daily`` needs a running event loop and an
    initialized application; for registration tests we only care that the
    bridge asked for the right schedules, so we capture the calls.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run_daily(self, callback, time, days=(0, 1, 2, 3, 4, 5, 6), name=None):
        self.calls.append(
            {"callback": callback, "time": time, "days": tuple(days), "name": name}
        )
        return MagicMock()


def test_register_digest_jobs_schedules_daily_and_weekly() -> None:
    """``register_digest_jobs`` registers exactly two recurring jobs:
    a daily digest at 06:00 local time (every day) and a weekly digest at
    06:00 local time on Sundays only (PTB day index 0)."""
    jq = _FakeJobQueue()

    telegram_bridge.register_digest_jobs(jq)

    assert len(jq.calls) == 2

    by_name = {c["name"]: c for c in jq.calls}
    assert set(by_name) == {"digest-daily", "digest-weekly"}

    daily = by_name["digest-daily"]
    assert daily["time"].hour == 6
    assert daily["time"].minute == 0
    # Every day of the week.
    assert daily["days"] == (0, 1, 2, 3, 4, 5, 6)

    weekly = by_name["digest-weekly"]
    assert weekly["time"].hour == 6
    assert weekly["time"].minute == 0
    # PTB 21: 0-6 == Sunday-Saturday, so Sunday-only is (0,).
    assert weekly["days"] == (0,)


def test_register_digest_jobs_uses_tz_aware_local_times() -> None:
    """The 06:00 schedules must be timezone-aware (the host's local tz),
    not naive — a naive time would be interpreted as UTC by PTB and fire
    at the wrong wall-clock hour."""
    jq = _FakeJobQueue()

    telegram_bridge.register_digest_jobs(jq)

    for call in jq.calls:
        scheduled = call["time"]
        assert isinstance(scheduled, dt.time)
        assert scheduled.tzinfo is not None, "digest schedule time must be tz-aware"


@pytest.mark.asyncio
async def test_digest_callback_dispatches_to_run_fn() -> None:
    """The job callback invokes the wrapped digest run function exactly
    once. The run function is the *existing* ``run_daily_digest`` /
    ``run_weekly_digest`` — digest logic is not reimplemented in the
    bridge."""
    calls: list[str] = []

    def fake_run() -> str:
        calls.append("ran")
        return "digest text"

    callback = telegram_bridge.make_digest_callback(fake_run)
    await callback(MagicMock())

    assert calls == ["ran"]


@pytest.mark.asyncio
async def test_digest_callback_swallows_failure() -> None:
    """A failing digest run must not propagate out of the callback — an
    uncaught exception in a PTB job would otherwise be logged loudly but
    the callback contract is that the bot keeps polling regardless."""
    def boom() -> str:
        raise RuntimeError("claude -p failed")

    callback = telegram_bridge.make_digest_callback(boom)

    # Must not raise.
    await callback(MagicMock())


def test_register_digest_jobs_wires_correct_digest_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``digest-daily`` dispatches to ``run_daily_digest`` and
    ``digest-weekly`` to ``run_weekly_digest`` — not the other way round,
    and not a reimplementation."""
    import agent.digest as digest_mod

    daily_calls: list[str] = []
    weekly_calls: list[str] = []
    monkeypatch.setattr(
        digest_mod, "run_daily_digest", lambda: daily_calls.append("d") or "d"
    )
    monkeypatch.setattr(
        digest_mod, "run_weekly_digest", lambda: weekly_calls.append("w") or "w"
    )

    jq = _FakeJobQueue()
    telegram_bridge.register_digest_jobs(jq)

    by_name = {c["name"]: c for c in jq.calls}

    import asyncio

    asyncio.run(by_name["digest-daily"]["callback"](MagicMock()))
    assert daily_calls == ["d"] and weekly_calls == []

    asyncio.run(by_name["digest-weekly"]["callback"](MagicMock()))
    assert daily_calls == ["d"] and weekly_calls == ["w"]


def test_build_application_registers_digest_jobs_on_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``build_application`` wires the two digest jobs onto the
    application's JobQueue at construction time — so simply starting the
    bot is enough; there is no separate launchd step (#9)."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token-for-test")
    monkeypatch.setenv("VAULT_ROOT", str(tmp_path))

    async def _text_reply(_chat_id: str, _text: str) -> str:
        return "ok"

    app = telegram_bridge.build_application(reply_fn=_text_reply)

    assert app.job_queue is not None, "JobQueue must be available (job-queue extra)"
    jobs = app.job_queue.jobs()
    job_names = {job.name for job in jobs}
    assert "digest-daily" in job_names
    assert "digest-weekly" in job_names
    assert len(jobs) == 2
