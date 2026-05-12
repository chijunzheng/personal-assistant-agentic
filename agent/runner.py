"""The agentic runner — replaces v1's 1949-line orchestrator.

One function: ``handle_turn(chat_id, user_msg) -> reply_text``.

Flow:

  1. Load session + last-N chat turns from the vault.
  2. Compose a per-turn message that prepends the session summary and
     recent turn log so the LLM has continuity without having to Read
     those files itself.
  3. Invoke ``claude -p`` with ``cwd=<vault_root>`` so Read/Glob/Grep
     default to vault paths, ``--allowed-tools`` constraining tools to
     filesystem reads + writes only, and ``--append-system-prompt``
     pointing at ``prompts/system.md``.
  4. Parse the JSON envelope: extract final reply text, all tool_use
     events, and token usage.
  5. Mirror every tool_use into the audit log.
  6. Append the chat_log turn.
  7. Update the session summary with a one-line note.
  8. Return the reply text.

The kernel knows nothing about *what* the LLM did. It only knows *that*
the LLM ran, what tools it called, and what it replied. The agent
decides everything else.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from agent import audit, chat_log, session

__all__ = [
    "ClaudeRunnerError",
    "TurnResult",
    "handle_turn",
    "invoke_claude",
]


logger = logging.getLogger(__name__)


DEFAULT_BIN = "claude"
DEFAULT_TIMEOUT_SEC = 180
DEFAULT_ALLOWED_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write")
DEFAULT_CHAT_TURN_WINDOW = 5

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SYSTEM_PROMPT_PATH = _REPO_ROOT / "prompts" / "system.md"


class ClaudeRunnerError(RuntimeError):
    """Raised when the subprocess fails or its output cannot be parsed."""


@dataclass(frozen=True)
class TurnResult:
    """Everything one turn produced. Returned for tests + tracing."""

    reply: str
    tokens_in: int
    tokens_out: int
    tool_calls: tuple[Mapping[str, Any], ...]
    duration_ms: int


# ---------------------------------------------------------------------------
# Environment + paths
# ---------------------------------------------------------------------------


def _vault_root() -> Path:
    raw = os.environ.get("VAULT_ROOT")
    if not raw:
        raise ClaudeRunnerError("VAULT_ROOT is not set in the environment")
    path = Path(raw).expanduser()
    if not path.exists():
        raise ClaudeRunnerError(f"VAULT_ROOT does not exist: {path}")
    return path


def _audit_root() -> Path:
    return _vault_root() / "_audit"


def _load_system_prompt() -> str:
    return _SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Per-turn message assembly
# ---------------------------------------------------------------------------


def _compose_user_text(
    *,
    caption: str,
    attachments: Sequence[Path],
    vault_root: Path,
) -> str:
    """Merge the user's caption text with attachment-path notices.

    Output shape:

      * Caption + one attachment:
          ``"<caption>\\n\\n[attachment saved to: <rel>]"``
      * Caption + multiple attachments:
          ``"<caption>\\n\\n[attachment saved to: <a>]\\n[attachment saved to: <b>]"``
      * Empty caption + attachments:
          ``"[attachment saved to: <rel>]"`` (just the notice(s))
      * No attachments: caption is returned verbatim — guarantees the
        existing text-only path is byte-identical.

    Relative paths are computed against ``vault_root``. The LLM runs
    with ``cwd=vault_root`` so it can call ``Read`` on the relative
    path directly. Paths outside the vault fall back to the absolute
    string representation.
    """
    if not attachments:
        return caption

    def _rel(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(vault_root.resolve()))
        except (ValueError, OSError):
            return str(p)

    notices = "\n".join(
        f"[attachment saved to: {_rel(p)}]" for p in attachments
    )
    if caption:
        return f"{caption}\n\n{notices}"
    return notices


def _format_chat_turns(turns: Sequence[chat_log.ChatTurn]) -> str:
    if not turns:
        return "(no prior turns in this session)"
    lines: list[str] = []
    for t in turns:
        lines.append(f"USER  ({t.ts}): {t.user_msg}")
        lines.append(f"BOT   ({t.ts}): {t.bot_reply}")
    return "\n".join(lines)


def _build_user_message(
    *,
    user_msg: str,
    sess: session.Session,
    recent_turns: Sequence[chat_log.ChatTurn],
) -> str:
    """Wrap the user's message with continuity context for the agent."""
    summary = sess.summary.strip() or "(no prior notes this session)"
    turns_block = _format_chat_turns(recent_turns)
    return (
        "## Session\n"
        f"chat_id={sess.chat_id} session_id={sess.session_id} "
        f"turns={sess.turns} last_updated={sess.last_updated}\n\n"
        "## Running summary (one note per prior turn this session)\n"
        f"{summary}\n\n"
        "## Recent chat turns (verbatim, oldest first)\n"
        f"{turns_block}\n\n"
        "## New user message\n"
        f"{user_msg}\n"
    )


# ---------------------------------------------------------------------------
# Subprocess invocation
# ---------------------------------------------------------------------------


def _build_argv(
    user_message: str,
    *,
    binary: str,
    allowed_tools: Sequence[str],
    system_prompt: str | None,
) -> list[str]:
    argv: list[str] = [binary, "-p", "--output-format", "json"]
    if allowed_tools:
        argv.extend(["--allowed-tools", ",".join(allowed_tools)])
    if system_prompt:
        # Replace (not append) the default Claude Code system prompt.
        # The default includes an "auto memory at ~/.claude/projects/...
        # /memory/" instruction we explicitly need to override — the
        # bot's memory lives in the vault, not in Claude Code's
        # auto-memory dir. See prompts/system.md.
        argv.extend(["--system-prompt", system_prompt])
    argv.append(user_message)
    return argv


def invoke_claude(
    user_message: str,
    *,
    cwd: str | os.PathLike[str],
    binary: str | None = None,
    allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
    system_prompt: str | None = None,
    timeout_sec: int = DEFAULT_TIMEOUT_SEC,
) -> tuple[str, int, int, tuple[Mapping[str, Any], ...]]:
    """Spawn ``claude -p`` once with ``cwd`` set to the vault root.

    Returns ``(reply_text, tokens_in, tokens_out, tool_calls)``.

    Args:
        user_message: the assembled per-turn message (session context +
            chat turns + new user text). Goes through verbatim.
        cwd: the working directory for the subprocess. The agent's
            Read/Glob/Grep/Edit/Write tools are scoped to this dir.
        binary: override ``claude`` executable path. Test-only.
        allowed_tools: which tools the agent may use. Defaults to the
            filesystem-scoped set; Bash and WebFetch are deliberately
            excluded.
        system_prompt: REPLACES the agent's built-in system prompt.
            Caller should pass the contents of ``prompts/system.md``.
            Replacement (not append) is intentional: the default
            includes auto-memory instructions pointing at
            ``~/.claude/projects/.../memory/`` which would conflict
            with the vault-as-source-of-truth contract.
        timeout_sec: kill the subprocess after this many seconds.

    Raises:
        ClaudeRunnerError: subprocess failed, timed out, or returned
            unparseable output.
    """
    argv = _build_argv(
        user_message,
        binary=binary or os.environ.get("CLAUDE_BIN", DEFAULT_BIN),
        allowed_tools=allowed_tools,
        system_prompt=system_prompt,
    )

    try:
        proc = subprocess.run(
            argv,
            cwd=os.fspath(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise ClaudeRunnerError(f"claude -p timed out after {timeout_sec}s") from err

    if proc.returncode != 0:
        raise ClaudeRunnerError(
            f"claude -p exited with code {proc.returncode}: {proc.stderr.strip()}"
        )

    return _parse_envelope(proc.stdout)


def _parse_envelope(
    raw_stdout: str,
) -> tuple[str, int, int, tuple[Mapping[str, Any], ...]]:
    """Decode ``claude -p --output-format json`` output.

    Output is a JSON array of stream events:
      * ``{"type": "system", ...}`` — startup / config
      * ``{"type": "assistant", "message": {"content": [...], "usage": {...}}}``
        — each LLM turn; ``content`` may include ``text`` blocks and
        ``tool_use`` blocks
      * ``{"type": "user", "message": {"content": [{"type": "tool_result", ...}]}}``
        — tool results fed back to the model
      * ``{"type": "result", "result": "<final text>", "usage": {...}}``
        — terminal event; ``result`` is the authoritative reply

    We extract the final reply from the result event, tally token usage
    from the same event, and collect every ``tool_use`` block across all
    assistant events for the audit log.
    """
    try:
        payload = json.loads(raw_stdout)
    except json.JSONDecodeError as err:
        raise ClaudeRunnerError(
            f"could not parse claude -p JSON envelope: {err}"
        ) from err

    if not isinstance(payload, list):
        raise ClaudeRunnerError(
            f"unexpected claude -p envelope type: {type(payload).__name__}"
        )

    reply = ""
    tokens_in = 0
    tokens_out = 0
    tool_calls: list[Mapping[str, Any]] = []

    for event in payload:
        if not isinstance(event, dict):
            continue
        etype = event.get("type")
        if etype == "assistant":
            msg = event.get("message") or {}
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "name": block.get("name", ""),
                            "input": block.get("input") or {},
                            "id": block.get("id", ""),
                        }
                    )
        elif etype == "result":
            reply = str(event.get("result") or "")
            usage = event.get("usage") or {}
            tokens_in = int(usage.get("input_tokens", 0) or 0)
            tokens_out = int(usage.get("output_tokens", 0) or 0)

    return reply, tokens_in, tokens_out, tuple(tool_calls)


# ---------------------------------------------------------------------------
# Audit mirroring
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _audit_turn(
    *,
    chat_id: str,
    user_msg: str,
    reply: str,
    tool_calls: Sequence[Mapping[str, Any]],
    duration_ms: int,
    tokens_in: int,
    tokens_out: int,
    outcome: str,
) -> None:
    """Write one audit entry per tool_use, plus a final ``turn`` entry.

    Mirrors a human-readable markdown summary alongside the jsonl so
    Obsidian (which can't render `.jsonl`) has something to display.
    The jsonl remains the source of truth — the `.md` is a view.
    """
    audit_root = _audit_root()
    ts = _now_iso()

    for call in tool_calls:
        audit.write_audit_entry(
            {
                "ts": ts,
                "op": f"tool.{call.get('name', 'unknown')}",
                "actor": "agent",
                "outcome": "ok",
                "duration_ms": 0,
                "chat_id": chat_id,
                "tool_name": call.get("name", ""),
                "tool_input": call.get("input") or {},
                "tool_id": call.get("id", ""),
            },
            audit_root=audit_root,
        )

    audit.write_audit_entry(
        {
            "ts": ts,
            "op": "turn",
            "actor": "agent",
            "outcome": outcome,
            "duration_ms": duration_ms,
            "chat_id": chat_id,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tool_call_count": len(tool_calls),
        },
        audit_root=audit_root,
    )

    _append_markdown_mirror(
        ts=ts,
        chat_id=chat_id,
        user_msg=user_msg,
        reply=reply,
        tool_calls=tool_calls,
        duration_ms=duration_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        outcome=outcome,
    )


def _vault_relative(path: str) -> str:
    """Trim the vault prefix off a tool's file_path for compact rendering."""
    try:
        return str(Path(path).resolve().relative_to(_vault_root().resolve()))
    except (ValueError, OSError):
        return path


def _format_tool_line(call: Mapping[str, Any]) -> str:
    name = call.get("name", "?")
    inp = call.get("input") or {}
    fp = inp.get("file_path")
    pattern = inp.get("pattern")
    if fp:
        return f"- **{name}** `{_vault_relative(str(fp))}`"
    if pattern:
        return f"- **{name}** `{pattern}`"
    keys = ", ".join(sorted(inp.keys())) or "no-args"
    return f"- **{name}** ({keys})"


def _append_markdown_mirror(
    *,
    ts: str,
    chat_id: str,
    user_msg: str,
    reply: str,
    tool_calls: Sequence[Mapping[str, Any]],
    duration_ms: int,
    tokens_in: int,
    tokens_out: int,
    outcome: str,
) -> None:
    """Append a Markdown section for this turn to ``_audit/<date>.md``.

    Format is grep-friendly and Obsidian-renderable. New turns append
    at the end of the file with a fresh ``## <time>`` heading.
    """
    audit_root = _audit_root()
    audit_root.mkdir(parents=True, exist_ok=True)
    day = ts[:10]
    target = audit_root / f"{day}.md"

    user_block = "\n".join(f"> {line}" for line in user_msg.splitlines() or [""])
    reply_block = "\n".join(f"> {line}" for line in reply.splitlines() or [""])
    tool_lines = "\n".join(_format_tool_line(c) for c in tool_calls) or "_(no tool calls)_"

    section = (
        f"\n---\n\n"
        f"## {ts[11:19]} — turn ({outcome}, {duration_ms}ms, {len(tool_calls)} tools)\n\n"
        f"**chat_id:** `{chat_id}` · **tokens:** in={tokens_in} out={tokens_out}\n\n"
        f"### User\n{user_block}\n\n"
        f"### Tools used\n{tool_lines}\n\n"
        f"### Reply\n{reply_block}\n"
    )

    if not target.exists():
        prelude = f"# Audit log — {day}\n"
    else:
        prelude = ""
    with open(target, "a", encoding="utf-8") as fh:
        if prelude:
            fh.write(prelude)
        fh.write(section)
        fh.flush()


# ---------------------------------------------------------------------------
# Top-level turn handler
# ---------------------------------------------------------------------------


def _summary_note(
    *,
    user_msg: str,
    reply: str,
    tool_calls: Sequence[Mapping[str, Any]],
) -> str:
    """One-line session-summary note for the turn.

    Kept short on purpose — the summary is read on every turn, so
    bloating it costs tokens forever.
    """
    snippet = user_msg.strip().replace("\n", " ")
    if len(snippet) > 80:
        snippet = snippet[:77] + "..."
    tool_names = ",".join(c.get("name", "") for c in tool_calls) or "none"
    return f"{_now_iso()} | user: {snippet} | tools: {tool_names}"


def handle_turn(
    *,
    chat_id: str,
    user_msg: str,
    attachments: Sequence[Path] = (),
) -> str:
    """Run one Telegram turn end-to-end and return the reply text.

    Args:
        chat_id: Telegram chat id (stringified).
        user_msg: the user's text — caption text from a file message or
            the body of a plain text message. May be empty when the user
            sends a file with no caption.
        attachments: optional sequence of vault-relative or absolute
            paths to files the bridge has already downloaded into the
            vault. The runner threads each path into the LLM's user
            message as ``[attachment saved to: <relative-path>]`` so the
            LLM can ``Read`` it under ``cwd=vault_root``.
    """
    vault_root = _vault_root()
    started = time.monotonic()

    sess = session.load_or_create(chat_id, vault_root=vault_root)
    recent_turns = chat_log.load_recent(chat_id, DEFAULT_CHAT_TURN_WINDOW, vault_root)
    composed_msg = _compose_user_text(
        caption=user_msg, attachments=attachments, vault_root=vault_root
    )
    user_message = _build_user_message(
        user_msg=composed_msg, sess=sess, recent_turns=recent_turns
    )

    try:
        reply, tokens_in, tokens_out, tool_calls = invoke_claude(
            user_message,
            cwd=vault_root,
            system_prompt=_load_system_prompt(),
        )
        outcome = "ok"
    except ClaudeRunnerError:
        logger.exception("claude runner failed")
        reply = "Sorry — the agent failed mid-turn. The error has been logged."
        tokens_in = 0
        tokens_out = 0
        tool_calls = ()
        outcome = "error"

    duration_ms = int((time.monotonic() - started) * 1000)

    _audit_turn(
        chat_id=chat_id,
        user_msg=user_msg,
        reply=reply,
        tool_calls=tool_calls,
        duration_ms=duration_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        outcome=outcome,
    )
    chat_log.append(
        chat_id,
        user_msg=user_msg,
        bot_reply=reply,
        vault_root=vault_root,
    )
    session.update(
        sess,
        _summary_note(user_msg=user_msg, reply=reply, tool_calls=tool_calls),
        vault_root=vault_root,
    )

    return reply
