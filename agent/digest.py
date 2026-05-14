"""Proactive digest entrypoint — the bot's first outbound-initiated message.

Every other path in this codebase is a *reply*: an incoming Telegram
message comes in, ``agent/runner.py`` runs ``claude -p``, the bridge
sends the answer back via PTB's ``message.reply_text``. The digest has
no incoming message. It is a scheduled *push*.

So this module needs two things the reply path doesn't:

  1. A standalone entrypoint (``python -m agent.digest --mode=daily``),
     invoked by launchd at 06:00 — see
     ``infra/launchd/com.jason.personal-assistant.digest-daily.plist``.
  2. An outbound send primitive that POSTs straight to the Telegram Bot
     API ``sendMessage`` endpoint, addressed by ``TELEGRAM_CHAT_ID``,
     because there is no ``Message`` object to reply against.

The Python here is deliberately thin. It invokes ``claude -p`` with
``prompts/digest.md`` as the system prompt and lets the LLM read the
vault (overdue reminders, ``TODO:`` markers in ``memory/*.md``, the last
three days of ``journal/``) and produce the digest text. This module
does NOT parse ``reminders.jsonl`` itself — the content sourcing is
*instructed* in ``prompts/digest.md``.

See ``docs/adr/0002-proactive-digest-modality.md`` for why launchd + a
standalone entrypoint rather than an in-process scheduler.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
from pathlib import Path
from typing import Callable

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
    "section of your system prompt: review the last 7 days, Write the "
    "reflection draft into journal/, and reply with the short Telegram "
    "nudge text only."
)

# Map of ``--mode`` value -> the trigger message for that digest turn.
_MODE_USER_MESSAGES = {
    "daily": _DAILY_USER_MESSAGE,
    "weekly": _WEEKLY_USER_MESSAGE,
}

_TELEGRAM_API_BASE = "https://api.telegram.org"
_HTTP_TIMEOUT_SEC = 15


class DigestError(RuntimeError):
    """Raised when digest generation or delivery fails."""


# Injectable POST seam: takes (url, json-payload), returns nothing. The
# default implementation hits the network; tests pass a fake.
PostFn = Callable[[str, dict], None]


# ---------------------------------------------------------------------------
# Outbound Telegram push primitive
# ---------------------------------------------------------------------------


def _http_post_json(url: str, payload: dict) -> None:
    """POST ``payload`` as JSON to ``url``. Raises on a non-2xx response.

    Uses ``urllib`` from the stdlib rather than adding a ``requests``
    dependency — the payload is tiny and the call is one-shot.
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
    except DigestError:
        raise
    except Exception as err:  # noqa: BLE001 — urllib raises a wide tree
        raise DigestError(f"Telegram sendMessage POST failed: {err}") from err


def send_telegram_message(
    *,
    text: str,
    token: str,
    chat_id: str,
    post_fn: PostFn = _http_post_json,
) -> None:
    """Push ``text`` to ``chat_id`` via the Telegram Bot API.

    This is the *push* counterpart to the bridge's ``_send_reply`` (which
    needs an incoming ``Message`` to reply to). A scheduled digest has no
    such message, so it addresses the chat directly by id.

    Args:
        text: the digest body to send.
        token: the bot token (``TELEGRAM_BOT_TOKEN``).
        chat_id: the destination chat (``TELEGRAM_CHAT_ID``).
        post_fn: injectable HTTP POST; defaults to a real ``urllib`` call.

    Raises:
        DigestError: the POST failed or Telegram returned a non-2xx code.
    """
    url = f"{_TELEGRAM_API_BASE}/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        post_fn(url, payload)
    except DigestError:
        raise
    except Exception as err:  # noqa: BLE001 — any transport failure
        raise DigestError(f"Telegram sendMessage failed: {err}") from err


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
    raises ``DigestError`` so the scheduled process exits non-zero — a
    job that fails silently is worse than one that fails loudly in the
    launchd log.
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
    send_telegram_message(
        text=digest_text, token=token, chat_id=chat_id, post_fn=post_fn
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

    Returns 0 on success, non-zero on any failure (and logs the error).
    A non-zero exit surfaces in the launchd log so a broken digest is
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
