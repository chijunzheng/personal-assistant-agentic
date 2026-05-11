"""Active session manager.

Ported from v1's ``kernel/session.py``. Same on-disk shape:
``<vault_root>/_index/active_session.md`` with YAML frontmatter (chat_id,
session_id, started_at, last_updated, turns) and a running markdown
summary body.

In agentic v2 the runner injects the session frontmatter+summary into
the per-turn user message so the LLM has continuity across turns
without having to Read the file itself. The LLM updates the summary by
writing back through the Write tool (or the runner does it on its
behalf — the runner-managed path is what we use for the v1 parity
baseline).
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from agent.vault import atomic_write

__all__ = ["Session", "load_or_create", "update"]


_SESSION_RELATIVE_PATH = Path("_index") / "active_session.md"
_FRONTMATTER_DELIMITER = "---"


@dataclass(frozen=True)
class Session:
    chat_id: str
    session_id: str
    started_at: str
    last_updated: str
    turns: int
    summary: str


def _session_path(vault_root: str | os.PathLike[str]) -> Path:
    return Path(vault_root) / _SESSION_RELATIVE_PATH


def _now_iso(clock: Optional[callable] = None) -> str:
    fn = clock or (lambda: datetime.now(tz=timezone.utc))
    return fn().isoformat()


def _serialize(session: Session) -> str:
    frontmatter = {
        "chat_id": session.chat_id,
        "session_id": session.session_id,
        "started_at": session.started_at,
        "last_updated": session.last_updated,
        "turns": session.turns,
    }
    head = yaml.safe_dump(frontmatter, sort_keys=True).strip()
    body = session.summary.rstrip()
    return (
        f"{_FRONTMATTER_DELIMITER}\n"
        f"{head}\n"
        f"{_FRONTMATTER_DELIMITER}\n\n"
        f"{body}\n"
    )


def _deserialize(raw: str) -> Optional[Session]:
    if not raw.startswith(_FRONTMATTER_DELIMITER):
        return None
    parts = raw.split(_FRONTMATTER_DELIMITER, 2)
    if len(parts) < 3:
        return None
    head = parts[1].strip()
    body = parts[2].lstrip("\n").rstrip()
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict):
        return None
    required = ("chat_id", "session_id", "started_at", "last_updated", "turns")
    if not all(field in meta for field in required):
        return None
    return Session(
        chat_id=str(meta["chat_id"]),
        session_id=str(meta["session_id"]),
        started_at=str(meta["started_at"]),
        last_updated=str(meta["last_updated"]),
        turns=int(meta["turns"]),
        summary=body,
    )


def load_or_create(
    chat_id: str,
    *,
    vault_root: str | os.PathLike[str],
    clock: Optional[callable] = None,
) -> Session:
    path = _session_path(vault_root)
    if path.exists():
        existing = _deserialize(path.read_text(encoding="utf-8"))
        if existing is not None and existing.chat_id == chat_id:
            return existing

    now = _now_iso(clock)
    session = Session(
        chat_id=chat_id,
        session_id=uuid.uuid4().hex,
        started_at=now,
        last_updated=now,
        turns=0,
        summary="",
    )
    atomic_write(path, _serialize(session))
    return session


def update(
    session: Session,
    note: str,
    *,
    vault_root: str | os.PathLike[str],
    clock: Optional[callable] = None,
) -> Session:
    body = session.summary.rstrip()
    appended = f"{body}\n- {note.strip()}".strip()
    next_session = replace(
        session,
        last_updated=_now_iso(clock),
        turns=session.turns + 1,
        summary=appended,
    )
    atomic_write(_session_path(vault_root), _serialize(next_session))
    return next_session
