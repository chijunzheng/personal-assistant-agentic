"""Append-only verbatim chat log.

Ported from v1's ``kernel/chat_log.py``. Same on-disk shape:
``<vault_root>/_chat_log/<chat_id>/<YYYY-MM-DD>.jsonl``.

The runner injects the most recent N turns into the per-turn message so
the LLM resolves referential follow-ups like *"yes, walk me through it"*
against the literal antecedent.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from agent.vault import atomic_write

__all__ = ["ChatTurn", "append", "load_recent"]


_CHAT_LOG_RELATIVE = Path("_chat_log")
_MAX_DAYS_BACK = 90


@dataclass(frozen=True)
class ChatTurn:
    id: str
    ts: str
    chat_id: str
    user_msg: str
    bot_reply: str


def _chat_dir(vault_root: str | os.PathLike[str], chat_id: str) -> Path:
    return Path(vault_root) / _CHAT_LOG_RELATIVE / chat_id


def _daily_path(vault_root: str | os.PathLike[str], chat_id: str, day: date) -> Path:
    return _chat_dir(vault_root, chat_id) / f"{day.isoformat()}.jsonl"


def _now_utc(clock: Optional[Callable[[], datetime]] = None) -> datetime:
    fn = clock or (lambda: datetime.now(tz=timezone.utc))
    return fn()


def _turn_id(*, chat_id: str, ts: str, user_msg: str, bot_reply: str) -> str:
    canonical = f"{chat_id}|{ts}|{user_msg}|{bot_reply}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_turns(path: Path) -> list[ChatTurn]:
    if not path.exists():
        return []
    seen: set[str] = set()
    turns: list[ChatTurn] = []
    with open(path, "r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            turn_id = record.get("id")
            if not turn_id or turn_id in seen:
                continue
            try:
                turn = ChatTurn(
                    id=str(turn_id),
                    ts=str(record["ts"]),
                    chat_id=str(record["chat_id"]),
                    user_msg=str(record["user_msg"]),
                    bot_reply=str(record["bot_reply"]),
                )
            except KeyError:
                continue
            seen.add(turn_id)
            turns.append(turn)
    return turns


def load_recent(
    chat_id: str,
    n: int,
    vault_root: str | os.PathLike[str],
) -> list[ChatTurn]:
    if n <= 0:
        return []

    chat_dir = _chat_dir(vault_root, chat_id)
    if not chat_dir.exists():
        return []

    today = _now_utc().date()
    collected: list[ChatTurn] = []
    for offset in range(_MAX_DAYS_BACK):
        day = today - timedelta(days=offset)
        day_turns = _read_turns(_daily_path(vault_root, chat_id, day))
        if day_turns:
            collected = day_turns + collected
            if len(collected) >= n:
                break

    return collected[-n:]


def append(
    chat_id: str,
    *,
    user_msg: str,
    bot_reply: str,
    vault_root: str | os.PathLike[str],
    clock: Optional[Callable[[], datetime]] = None,
) -> ChatTurn:
    now = _now_utc(clock)
    ts = now.isoformat()
    turn = ChatTurn(
        id=_turn_id(chat_id=chat_id, ts=ts, user_msg=user_msg, bot_reply=bot_reply),
        ts=ts,
        chat_id=chat_id,
        user_msg=user_msg,
        bot_reply=bot_reply,
    )

    target = _daily_path(vault_root, chat_id, now.date())
    target.parent.mkdir(parents=True, exist_ok=True)

    existing = ""
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = ""

    if existing and turn.id in existing:
        for line in existing.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("id") == turn.id:
                return turn

    serialized = json.dumps(
        {
            "id": turn.id,
            "ts": turn.ts,
            "chat_id": turn.chat_id,
            "user_msg": turn.user_msg,
            "bot_reply": turn.bot_reply,
        },
        separators=(",", ":"),
        ensure_ascii=False,
    )

    if existing and not existing.endswith("\n"):
        existing = existing + "\n"
    new_content = existing + serialized + "\n"

    atomic_write(target, new_content)
    return turn
