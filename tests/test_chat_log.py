"""Smoke tests for the ported chat_log module."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agent import chat_log


def _fixed_clock(when: datetime):
    return lambda: when


def test_append_creates_jsonl_under_chat_dir(tmp_path: Path) -> None:
    clock = _fixed_clock(datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc))
    turn = chat_log.append(
        "12345",
        user_msg="hi",
        bot_reply="hello",
        vault_root=tmp_path,
        clock=clock,
    )
    daily = tmp_path / "_chat_log" / "12345" / "2026-05-11.jsonl"
    assert daily.exists()
    assert turn.id and len(turn.id) == 64


def test_append_is_idempotent_on_identical_payload(tmp_path: Path) -> None:
    clock = _fixed_clock(datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc))
    chat_log.append("1", user_msg="x", bot_reply="y", vault_root=tmp_path, clock=clock)
    chat_log.append("1", user_msg="x", bot_reply="y", vault_root=tmp_path, clock=clock)
    daily = tmp_path / "_chat_log" / "1" / "2026-05-11.jsonl"
    assert len(daily.read_text().splitlines()) == 1


def test_load_recent_returns_chronological(tmp_path: Path) -> None:
    base = datetime(2026, 5, 11, 10, 0, tzinfo=timezone.utc)
    for i in range(3):
        ts = base.replace(minute=i)
        chat_log.append(
            "1",
            user_msg=f"msg-{i}",
            bot_reply=f"reply-{i}",
            vault_root=tmp_path,
            clock=lambda when=ts: when,
        )

    turns = chat_log.load_recent("1", 5, tmp_path)
    assert [t.user_msg for t in turns] == ["msg-0", "msg-1", "msg-2"]


def test_load_recent_empty_when_no_dir(tmp_path: Path) -> None:
    assert chat_log.load_recent("nobody", 5, tmp_path) == []
