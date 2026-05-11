"""Smoke tests for the vault primitives."""

from __future__ import annotations

import os
from pathlib import Path

from agent import vault


def test_atomic_write_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "journal" / "2026-05-11.md"
    result = vault.atomic_write(target, "hello")
    assert result.staged is False
    assert target.read_text() == "hello"


def test_atomic_write_overwrites_without_vault_root(tmp_path: Path) -> None:
    target = tmp_path / "x.md"
    target.write_text("first")
    vault.atomic_write(target, "second")
    assert target.read_text() == "second"


def test_mtime_buffer_stages_recent_edits(tmp_path: Path) -> None:
    vault_root = tmp_path
    target = vault_root / "journal" / "2026-05-11.md"
    target.parent.mkdir(parents=True)
    target.write_text("user just edited this")

    # File mtime is "now"; default 30-min buffer should stage the write.
    result = vault.atomic_write(
        target,
        "agent attempt",
        vault_root=vault_root,
        write_buffer_min=30,
    )

    assert result.staged is True
    assert target.read_text() == "user just edited this"  # canonical untouched
    assert result.path.exists()
    assert vault.PENDING_EDITS_DIR in result.path.as_posix()
    assert result.path.read_text() == "agent attempt"


def test_mtime_buffer_allows_old_writes(tmp_path: Path) -> None:
    vault_root = tmp_path
    target = vault_root / "journal" / "2026-05-11.md"
    target.parent.mkdir(parents=True)
    target.write_text("old content")

    # Backdate mtime by 1 hour.
    old = target.stat().st_mtime - 3600
    os.utime(target, (old, old))

    result = vault.atomic_write(
        target,
        "fresh content",
        vault_root=vault_root,
        write_buffer_min=30,
    )

    assert result.staged is False
    assert target.read_text() == "fresh content"
