"""Proactive digest entrypoint — the bot's first outbound-initiated message.

Every other path in this codebase is a *reply*: an incoming Telegram
message comes in, ``agent/runner.py`` runs ``claude -p``, the bridge
sends the answer back via PTB's ``message.reply_text``. The digest has
no incoming message. It is a scheduled *push*.

So this module needs two things the reply path doesn't:

  1. ``run_daily_digest`` / ``run_weekly_digest`` — the digest *logic*,
     scheduled in-process by the Telegram bridge's PTB ``JobQueue`` (see
     ``agent/telegram_bridge.py:register_digest_jobs`` and ADR 0003). The
     ``python -m agent.digest --mode=daily|weekly`` CLI entrypoint stays
     as the manual smoke-test path.
  2. An outbound send primitive that POSTs straight to the Telegram Bot
     API ``sendMessage`` endpoint, addressed by ``TELEGRAM_CHAT_ID``,
     because there is no ``Message`` object to reply against.

The Python here is deliberately thin. It invokes ``claude -p`` with
``prompts/digest.md`` as the system prompt and lets the LLM read the
vault (overdue reminders, ``TODO:`` markers in ``memory/*.md``, the last
three days of ``journal/``) and produce the digest text. This module
does NOT parse ``reminders.jsonl`` itself — the content sourcing is
*instructed* in ``prompts/digest.md``.

See ``docs/adr/0002-proactive-digest-modality.md`` for the proactive-push
modality, and ``docs/adr/0003-in-process-digest-scheduling.md`` for why
the trigger is the bridge's in-process scheduler rather than launchd.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from agent.format import markdown_to_telegram_html
from agent.runner import ClaudeRunnerError, invoke_claude

__all__ = [
    "DigestError",
    "PostFn",
    "build_digest_argv_prompt_path",
    "generate_digest",
    "main",
    "run_daily_digest",
    "run_weekly_digest",
    "send_telegram_message",
]


logger = logging.getLogger(__name__)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROMPTS_DIR = _REPO_ROOT / "prompts"

# Map of ``--mode`` value -> the system-prompt file driving that digest.
# Both modes share ``prompts/digest.md`` — the file carries a daily
# section and a weekly section; ``--mode`` selects which user message
# (and so which section) the LLM acts on. Registering a mode here is the
# whole wiring step: ``main`` and argparse ``choices`` read this map.
_MODE_PROMPTS = {
    "daily": _PROMPTS_DIR / "digest.md",
    "weekly": _PROMPTS_DIR / "digest.md",
}

# The user message handed to ``claude -p``. The *instructions* (what to
# read, what to include, tone, rendering constraints) live entirely in
# the system prompt; this is just the trigger. Each mode gets a distinct
# trigger so the LLM acts on the matching section of ``prompts/digest.md``
# — and so daily vs weekly is distinguishable in the invocation.
_DAILY_USER_MESSAGE = (
    "Generate today's daily digest. Follow the daily-digest section of "
    "your system prompt: read the vault, assemble the sections, and reply "
    "with the digest text only."
)

_WEEKLY_USER_MESSAGE = (
    "Generate this week's weekly reflection. Follow the weekly-reflection "
    "section of your system prompt: review the last 7 days across "
    "fitness/finance/memory/reminders/decisions, Write a substantive "
    "reflection draft (inline rollups + 4-6 event-grounded prompts) into "
    "journal/<today>-weekly-reflection.md, and reply with the substantive "
    "week-in-review Telegram nudge (rollups + open threads with "
    "age-escalated language + short pointer to the draft file)."
)

# Map of ``--mode`` value -> the trigger message for that digest turn.
_MODE_USER_MESSAGES = {
    "daily": _DAILY_USER_MESSAGE,
    "weekly": _WEEKLY_USER_MESSAGE,
}

_TELEGRAM_API_BASE = "https://api.telegram.org"
_HTTP_TIMEOUT_SEC = 15

# Where loud-failure records land inside the vault. Relative to
# ``VAULT_ROOT``; the directory is created on demand. The shape mirrors
# the existing ``_audit/<date>.jsonl`` convention — same parent dir, but a
# distinct ``digest-failures/`` subdir because the schema is per-incident
# JSON (one file per failure) rather than per-day JSONL (many rows per
# day). Issue #27.
_FAILURE_SUBDIR = ("_audit", "digest-failures")


class DigestError(RuntimeError):
    """Raised when digest generation or delivery fails."""


# Injectable POST seam: takes (url, json-payload), returns nothing. The
# default implementation hits the network; tests pass a fake.
PostFn = Callable[[str, dict], None]


# ---------------------------------------------------------------------------
# Loud-observability helpers for digest send failures (issue #27)
# ---------------------------------------------------------------------------


def _failure_dir(vault_root: Path) -> Path:
    """Resolve the per-vault failure-record directory.

    Pure path math — the caller decides when to ``mkdir``. Keeping the
    path computation out of the dump function makes the next-run notice
    helper trivial (it only needs to *list* the dir, not write to it).
    """
    return vault_root.joinpath(*_FAILURE_SUBDIR)


def _utc_timestamp_for_filename() -> str:
    """Return a filesystem-safe UTC timestamp like ``20260516T120000Z``.

    Filename-safe means no ``:`` (Windows-hostile) and no spaces. Using
    UTC keeps the lexicographic order of filenames consistent with the
    chronological order — important because the next-run notice picks the
    *most recent* failure file and that ordering must not surprise.
    """
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_timestamp_iso() -> str:
    """Return an ISO-8601 UTC timestamp like ``2026-05-16T12:00:00+00:00``.

    Stored *inside* the failure record (not the filename). Two timestamps
    look redundant but they serve different jobs: the filename one is
    sortable + filesystem-safe; the in-record one is round-trippable to a
    ``datetime`` for any future tooling.
    """
    return datetime.now(timezone.utc).isoformat()


def _dump_failure_record(
    *,
    vault_root: Path,
    mode: str,
    source_markdown: str,
    converted_html: str,
    response_body: str,
    http_status: int,
) -> Path:
    """Write a structured failure record under ``<vault>/_audit/digest-failures/``.

    One JSON file per failure; filename is ``<UTC-ts>-<mode>.json`` so the
    operator can ``ls`` the dir and see incidents in chronological order
    by mode. Returns the path so the caller can name it in a log line.

    Schema (issue #27):
      - ``source_markdown`` — what the LLM produced (input to the HTML
        converter). Lets the operator replay the converter offline.
      - ``converted_html`` — what we actually POSTed. Lets the operator
        see the exact payload Telegram rejected.
      - ``response_body`` — full HTTP body from Telegram, not just
        ``err.reason``. The previous code only logged ``.reason``, which
        is too vague to root-cause.
      - ``http_status`` — the HTTP status code that triggered the dump.
      - ``mode`` — ``daily`` or ``weekly``; matches the filename suffix.
      - ``timestamp`` — UTC ISO-8601, round-trippable to ``datetime``.
    """
    dir_path = _failure_dir(vault_root)
    dir_path.mkdir(parents=True, exist_ok=True)
    filename = f"{_utc_timestamp_for_filename()}-{mode}.json"
    record_path = dir_path / filename
    record = {
        "source_markdown": source_markdown,
        "converted_html": converted_html,
        "response_body": response_body,
        "http_status": http_status,
        "mode": mode,
        "timestamp": _utc_timestamp_iso(),
    }
    record_path.write_text(
        json.dumps(record, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return record_path


def _read_response_body(http_err: urllib.error.HTTPError) -> str:
    """Pull the response body off the ``HTTPError`` defensively.

    ``HTTPError`` carries a ``fp`` (a file-like over the response body)
    that has often been partly consumed by urllib. ``.read()`` may raise
    if the stream is closed, so we wrap it — observability must never
    blow up the send path.
    """
    try:
        raw = http_err.read() if hasattr(http_err, "read") else b""
    except Exception:  # noqa: BLE001 — fp may be closed/exhausted
        raw = b""
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return str(raw)
    return str(raw)


def _consume_latest_failure_notice(vault_root: Path) -> str | None:
    """Return the one-line operator notice for the most recent failure
    file, then delete that file ("consume" it).

    Issue #27 acceptance criterion (c): the next digest body is prefixed
    with a single-line operator notice naming the most recent failure
    file. The notice is dropped once the failure file is consumed.
    "Consumed" here means deleted by the digest run that emits the notice
    — without that, every subsequent digest would carry a stale prefix
    forever.

    Returns ``None`` when the dir doesn't exist or contains no ``*.json``
    failure records — the steady-state shape. Errors during consumption
    (missing dir, unreadable file) never raise: observability must not
    take down the digest itself.
    """
    dir_path = _failure_dir(vault_root)
    if not dir_path.is_dir():
        return None

    try:
        candidates = sorted(dir_path.glob("*.json"))
    except OSError:
        return None
    if not candidates:
        return None

    # Filenames embed a UTC timestamp; lexicographic sort = chronological.
    latest = candidates[-1]
    # Render the path *relative to the vault root* in the notice so it
    # matches the issue body's spec literally: "_audit/digest-failures/..."
    relative = latest.relative_to(vault_root).as_posix()
    notice = f"(HTML send failed last run — see {relative})"

    # Consume the file so subsequent runs don't keep re-emitting the same
    # notice. If deletion fails (permissions, race), swallow — the notice
    # still went out, and the operator can clear the file by hand.
    try:
        latest.unlink()
    except OSError:
        pass
    return notice


# ---------------------------------------------------------------------------
# Outbound Telegram push primitive
# ---------------------------------------------------------------------------


def _http_post_json(url: str, payload: dict) -> None:
    """POST ``payload`` as JSON to ``url``. Raises on a non-2xx response.

    Uses ``urllib`` from the stdlib rather than adding a ``requests``
    dependency — the payload is tiny and the call is one-shot.

    ``urllib.error.HTTPError`` is allowed to propagate verbatim — the
    caller (``send_telegram_message``) needs to distinguish HTTP 400 (the
    Telegram "malformed HTML" signal) from other transport failures to
    drive the plain-text fallback. All other exceptions are wrapped as
    ``DigestError`` so the run fails loudly.
    """
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SEC) as response:
            status = getattr(response, "status", 200)
            if status >= 300:
                raise DigestError(f"Telegram sendMessage returned HTTP {status}")
    except urllib.error.HTTPError:
        # Surface the structured HTTPError so the caller can branch on
        # ``.code`` for the 400 retry-as-plain path.
        raise
    except DigestError:
        raise
    except Exception as err:  # noqa: BLE001 — urllib raises a wide tree
        raise DigestError(f"Telegram sendMessage POST failed: {err}") from err


def _build_send_payload(
    *,
    chat_id: str,
    text: str,
    parse_mode: str | None,
) -> dict:
    """Compose the ``sendMessage`` JSON body.

    ``parse_mode`` is included only when set — the plain-text retry omits
    it entirely so Telegram does not attempt to parse entities at all.
    """
    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if parse_mode is not None:
        payload["parse_mode"] = parse_mode
    return payload


def send_telegram_message(
    *,
    text: str,
    token: str,
    chat_id: str,
    post_fn: PostFn = _http_post_json,
    vault_root: Path | None = None,
    mode: str | None = None,
) -> None:
    """Push ``text`` to ``chat_id`` via the Telegram Bot API.

    This is the *push* counterpart to the bridge's ``_send_reply`` (which
    needs an incoming ``Message`` to reply to). A scheduled digest has no
    such message, so it addresses the chat directly by id.

    The body is rendered through ``markdown_to_telegram_html`` and sent
    with ``parse_mode=HTML`` so ``**bold**`` and bullets arrive formatted
    rather than as literal asterisks. If Telegram rejects the HTML payload
    with HTTP 400 (malformed markup), we retry once with the raw text and
    no ``parse_mode`` — the same fallback shape as the bridge's reply path
    (``_send_reply``'s ``BadRequest`` handler). Users see something either
    way.

    Loud observability (issue #27): when ``vault_root`` and ``mode`` are
    both supplied, a non-2xx from the HTML send path also (a) logs the
    full Telegram response body plus the html/markdown character counts
    and the digest mode, and (b) writes a structured failure-record JSON
    to ``<vault>/_audit/digest-failures/<UTC-ts>-<mode>.json``. The
    fallback behavior itself is unchanged — observability is additive so
    legacy callers (and the existing 400-fallback test) keep working.

    Args:
        text: the digest body to send (markdown).
        token: the bot token (``TELEGRAM_BOT_TOKEN``).
        chat_id: the destination chat (``TELEGRAM_CHAT_ID``).
        post_fn: injectable HTTP POST; defaults to a real ``urllib`` call.
        vault_root: vault root for the failure-record dump. Optional;
            when omitted no record is dumped (legacy compat).
        mode: digest mode (``daily`` | ``weekly``) for the failure-record
            filename and log line. Optional; when omitted no record is
            dumped (legacy compat).

    Raises:
        DigestError: the POST failed or Telegram returned a non-2xx code
            other than the 400 that drives the plain-text retry.
    """
    url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
    html_text = markdown_to_telegram_html(text)
    html_payload = _build_send_payload(
        chat_id=chat_id, text=html_text, parse_mode="HTML"
    )

    try:
        post_fn(url, html_payload)
        return
    except urllib.error.HTTPError as http_err:
        # Pull the full body up front — both the log line and the dump
        # file want it, and the underlying ``fp`` can only be read once.
        response_body = _read_response_body(http_err)
        _handle_html_send_failure(
            http_err=http_err,
            response_body=response_body,
            source_markdown=text,
            converted_html=html_text,
            mode=mode,
            vault_root=vault_root,
        )
        if http_err.code != 400:
            raise DigestError(
                f"Telegram sendMessage failed: HTTP {http_err.code}"
            ) from http_err
    except DigestError:
        raise
    except Exception as err:  # noqa: BLE001 — any transport failure
        raise DigestError(f"Telegram sendMessage failed: {err}") from err

    # Plain-text retry: no ``parse_mode`` and the raw markdown text. If
    # this also fails, the failure surfaces as ``DigestError`` so the run
    # exits non-zero — better to fail loudly than swallow a second error.
    plain_payload = _build_send_payload(
        chat_id=chat_id, text=text, parse_mode=None
    )
    try:
        post_fn(url, plain_payload)
    except DigestError:
        raise
    except Exception as err:  # noqa: BLE001 — any transport failure
        raise DigestError(
            f"Telegram sendMessage plain-text retry failed: {err}"
        ) from err


def _handle_html_send_failure(
    *,
    http_err: urllib.error.HTTPError,
    response_body: str,
    source_markdown: str,
    converted_html: str,
    mode: str | None,
    vault_root: Path | None,
) -> None:
    """Emit the loud failure signals on a non-2xx from the HTML send path.

    Two side effects, both additive:
      1. Log line carrying the full HTTP response body plus html/markdown
         character counts and the digest mode. Replaces the previous
         ``err.reason`` log that was too vague to root-cause.
      2. If ``vault_root`` and ``mode`` are both supplied, dump a
         structured failure record under ``<vault>/_audit/digest-failures/``.

    Issue #27. Kept separate from the send path so the send path stays
    readable and the dump logic is independently testable.
    """
    logger.warning(
        "Telegram rejected digest HTML payload (HTTP %s, mode=%s, "
        "markdown_len=%d, html_len=%d). Response body: %s",
        http_err.code,
        mode if mode is not None else "<unknown>",
        len(source_markdown),
        len(converted_html),
        response_body if response_body else "<empty>",
    )

    if vault_root is None or mode is None:
        # Observability is opt-in: callers without a vault/mode (legacy
        # tests, ad-hoc usage) still get the log line but no audit dump.
        return

    try:
        record_path = _dump_failure_record(
            vault_root=vault_root,
            mode=mode,
            source_markdown=source_markdown,
            converted_html=converted_html,
            response_body=response_body,
            http_status=http_err.code,
        )
        logger.warning("Digest failure record written: %s", record_path)
    except OSError as dump_err:
        # The dump is best-effort — a filesystem error here must not mask
        # the send failure itself. Log loudly and continue.
        logger.error(
            "Failed to write digest failure record (mode=%s): %s",
            mode,
            dump_err,
        )


# ---------------------------------------------------------------------------
# Digest generation via claude -p
# ---------------------------------------------------------------------------


def build_digest_argv_prompt_path(mode: str) -> Path:
    """Resolve the system-prompt path for ``mode``.

    ``--mode=daily`` selects ``prompts/digest.md`` — NOT
    ``prompts/system.md``. The digest turn is a proactive generation with
    no user message; it follows a different contract.
    """
    try:
        return _MODE_PROMPTS[mode]
    except KeyError:
        raise DigestError(
            f"unknown digest mode {mode!r}; expected one of "
            f"{sorted(_MODE_PROMPTS)}"
        ) from None


def generate_digest(
    *,
    mode: str,
    vault_root: Path,
    invoke_fn: Callable[..., tuple] = invoke_claude,
) -> str:
    """Run ``claude -p`` with the digest system prompt; return the text.

    The LLM does all the vault reading. This function only assembles the
    invocation and extracts the reply from the envelope.

    Raises:
        DigestError: the prompt is missing, or ``claude -p`` failed.
    """
    prompt_path = build_digest_argv_prompt_path(mode)
    if not prompt_path.exists():
        raise DigestError(f"digest system prompt not found: {prompt_path}")
    system_prompt = prompt_path.read_text(encoding="utf-8")

    try:
        user_message = _MODE_USER_MESSAGES[mode]
    except KeyError:
        raise DigestError(
            f"unknown digest mode {mode!r}; expected one of "
            f"{sorted(_MODE_USER_MESSAGES)}"
        ) from None

    try:
        reply, _tokens_in, _tokens_out, _tool_calls = invoke_fn(
            user_message,
            cwd=vault_root,
            system_prompt=system_prompt,
        )
    except ClaudeRunnerError as err:
        raise DigestError(f"claude -p failed during digest generation: {err}") from err

    if not reply.strip():
        raise DigestError("claude -p returned an empty digest")
    return reply


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise DigestError(f"{name} is not set in the environment")
    return value


def _run_digest(
    *,
    mode: str,
    invoke_fn: Callable[..., tuple],
    post_fn: PostFn,
) -> str:
    """Shared run path: resolve env, generate via ``claude -p``, push.

    Both ``run_daily_digest`` and ``run_weekly_digest`` are thin wrappers
    over this — the only difference between them is ``mode``. Any failure
    raises ``DigestError``: the in-process job callback logs it loudly and
    the CLI exits non-zero — a job that fails silently is worse than one
    that fails visibly.

    Loud observability (issue #27): if the *previous* digest run dumped a
    failure record, this run prefixes its body with a one-line operator
    notice naming that record file — so the next digest *is* the alert.
    The notice is consumed (the file deleted) after the prefix is added,
    so we don't re-emit the same alert run after run. The send path also
    receives ``vault_root`` + ``mode`` so any failure *this* run dumps a
    fresh record for the next one.
    """
    vault_raw = _require_env("VAULT_ROOT")
    vault_root = Path(vault_raw).expanduser()
    if not vault_root.exists():
        raise DigestError(f"VAULT_ROOT does not exist: {vault_root}")

    token = _require_env("TELEGRAM_BOT_TOKEN")
    chat_id = _require_env("TELEGRAM_CHAT_ID")

    digest_text = generate_digest(
        mode=mode, vault_root=vault_root, invoke_fn=invoke_fn
    )

    # If a prior run left an unconsumed failure record, lead with the
    # operator notice so Jason sees the alert in Telegram (rather than
    # only in the server log). ``_consume_latest_failure_notice`` returns
    # ``None`` in the happy steady state where no record exists.
    notice = _consume_latest_failure_notice(vault_root)
    if notice is not None:
        digest_text = f"{notice}\n\n{digest_text}"

    send_telegram_message(
        text=digest_text,
        token=token,
        chat_id=chat_id,
        post_fn=post_fn,
        vault_root=vault_root,
        mode=mode,
    )
    logger.info("%s digest sent (%d chars)", mode, len(digest_text))
    return digest_text


def run_daily_digest(
    *,
    invoke_fn: Callable[..., tuple] = invoke_claude,
    post_fn: PostFn = _http_post_json,
) -> str:
    """Generate the daily digest and push it to Telegram. Returns the text.

    Reads ``VAULT_ROOT``, ``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID``
    from the environment.
    """
    return _run_digest(mode="daily", invoke_fn=invoke_fn, post_fn=post_fn)


def run_weekly_digest(
    *,
    invoke_fn: Callable[..., tuple] = invoke_claude,
    post_fn: PostFn = _http_post_json,
) -> str:
    """Generate the weekly reflection and push the nudge to Telegram.

    Unlike the daily digest (a fire-and-forget push), the weekly turn is
    reflection-oriented: ``claude -p`` reviews the last 7 days and *Writes*
    a ``journal/YYYY-MM-DD-weekly-reflection.md`` draft itself (it has
    Write access), then replies with a short nudge. This function stays
    thin — it only selects the weekly mode and pushes the nudge text; the
    draft authoring is entirely the LLM's, instructed in
    ``prompts/digest.md``. Reuses ``send_telegram_message`` via
    ``_run_digest`` — the push helper is not duplicated.
    """
    return _run_digest(mode="weekly", invoke_fn=invoke_fn, post_fn=post_fn)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: ``python -m agent.digest --mode=daily``.

    This is the manual smoke-test path — the production trigger is the
    bridge's in-process ``JobQueue`` (ADR 0003). Returns 0 on success,
    non-zero on any failure (and logs the error) so a broken digest is
    visible rather than silently skipped.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(prog="agent.digest")
    parser.add_argument(
        "--mode",
        choices=sorted(_MODE_PROMPTS),
        required=True,
        help="which digest to generate and push",
    )
    args = parser.parse_args(argv)

    # Each registered mode maps to its run function. argparse's ``choices``
    # already rejects anything outside ``_MODE_PROMPTS``; this map is the
    # mode -> handler wiring.
    handlers: dict[str, Callable[[], str]] = {
        "daily": run_daily_digest,
        "weekly": run_weekly_digest,
    }

    handler = handlers.get(args.mode)
    if handler is None:
        # Unreachable while every registered mode has a handler — argparse
        # rejects unknown modes. Kept explicit as a guard.
        logger.error("mode %r has no handler", args.mode)
        return 1

    try:
        handler()
    except DigestError:
        logger.exception("%s digest failed", args.mode)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
