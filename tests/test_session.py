"""Smoke tests for the ported session module."""

from __future__ import annotations

from pathlib import Path

from agent import session


def test_load_or_create_creates_new(tmp_path: Path) -> None:
    sess = session.load_or_create("12345", vault_root=tmp_path)
    assert sess.chat_id == "12345"
    assert sess.turns == 0
    assert sess.summary == ""
    assert (tmp_path / "_index" / "active_session.md").exists()


def test_load_or_create_reuses_existing_same_chat(tmp_path: Path) -> None:
    a = session.load_or_create("12345", vault_root=tmp_path)
    b = session.load_or_create("12345", vault_root=tmp_path)
    assert a.session_id == b.session_id


def test_load_or_create_replaces_on_chat_id_switch(tmp_path: Path) -> None:
    a = session.load_or_create("12345", vault_root=tmp_path)
    b = session.load_or_create("99999", vault_root=tmp_path)
    assert a.session_id != b.session_id


def test_update_appends_note_and_bumps_turn(tmp_path: Path) -> None:
    sess = session.load_or_create("12345", vault_root=tmp_path)
    sess2 = session.update(sess, "first note", vault_root=tmp_path)
    sess3 = session.update(sess2, "second note", vault_root=tmp_path)

    assert sess3.turns == 2
    assert "first note" in sess3.summary
    assert "second note" in sess3.summary
