"""Vault I/O primitives.

Ported from v1's ``kernel/vault.py``. The runner does NOT call
``atomic_write`` directly — Claude's Write/Edit tools handle the writes
inside the vault. This module is here for:

  1. Any kernel-side writes (session updates, audit log entry assembly
     when the runner needs to stage state itself).
  2. The 30-min user-edit buffer rule, which is enforced *by convention*
     in ``prompts/system.md`` (the LLM is told to use Edit on recent
     narrative files) and *as a safety net* here if the runner ever
     needs to stage a write on the LLM's behalf.

Two defenses survive from v1:

  * Defense 1 — atomic writes (tmp + ``os.replace``).
  * Defense 3 — 30-min user-edit buffer staging to
    ``<vault_root>/_inbox/_pending_edits/``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

__all__ = ["AtomicWriteResult", "atomic_write"]


DEFAULT_WRITE_BUFFER_MIN = 30
PENDING_EDITS_DIR = "_inbox/_pending_edits"


@dataclass(frozen=True)
class AtomicWriteResult:
    path: Path
    staged: bool


def _seconds_since_mtime(target: Path, *, now: float) -> Optional[float]:
    try:
        st = target.stat()
    except FileNotFoundError:
        return None
    return now - st.st_mtime


def _stage_path(*, target: Path, vault_root: Path, now: float) -> Path:
    try:
        rel = target.resolve().relative_to(vault_root.resolve())
    except ValueError:
        rel = Path(target.name)
    base = vault_root / PENDING_EDITS_DIR / rel.parent
    suffix = f"{int(now * 1e9)}{target.suffix}"
    stem = target.stem
    return base / f"{stem}.{suffix}"


def _write_atomic(target: Path, content: str) -> None:
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    tmp = target.with_name(target.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except Exception:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def atomic_write(
    path: str | os.PathLike[str],
    content: str,
    *,
    vault_root: str | os.PathLike[str] | None = None,
    write_buffer_min: int = DEFAULT_WRITE_BUFFER_MIN,
    now: Callable[[], float] | None = None,
) -> AtomicWriteResult:
    """Write ``content`` to ``path`` atomically with optional mtime-buffer staging."""
    target = Path(path)
    clock = now or time.time

    if vault_root is None:
        _write_atomic(target, content)
        return AtomicWriteResult(path=target, staged=False)

    vault = Path(vault_root)
    current = clock()
    age = _seconds_since_mtime(target, now=current)
    buffer_seconds = max(0, write_buffer_min) * 60

    if age is not None and age < buffer_seconds:
        staged = _stage_path(target=target, vault_root=vault, now=current)
        _write_atomic(staged, content)
        return AtomicWriteResult(path=staged, staged=True)

    _write_atomic(target, content)
    return AtomicWriteResult(path=target, staged=False)
