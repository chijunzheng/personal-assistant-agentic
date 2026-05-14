"""Smoke tests for the agentic runner.

These don't actually spawn ``claude -p`` — they verify argv assembly and
envelope parsing against canned JSON. End-to-end behavior is covered by
the live smoke test (run the Telegram bot, send a message, watch the
audit log).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import runner


# ---------------------------------------------------------------------------
# argv assembly
# ---------------------------------------------------------------------------


def test_argv_includes_allowed_tools_and_system_prompt() -> None:
    argv = runner._build_argv(
        "hello",
        binary="claude",
        allowed_tools=("Read", "Write"),
        system_prompt="be helpful",
    )
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "--output-format" in argv
    assert "stream-json" in argv
    assert "--verbose" in argv

    i = argv.index("--allowed-tools")
    assert argv[i + 1] == "Read,Write"

    # The runner uses --system-prompt (replace), not --append-system-prompt,
    # so Claude Code's default auto-memory instructions are overridden.
    j = argv.index("--system-prompt")
    assert argv[j + 1] == "be helpful"
    assert "--append-system-prompt" not in argv

    assert argv[-1] == "hello"


def test_argv_omits_system_prompt_when_none() -> None:
    argv = runner._build_argv(
        "hello",
        binary="claude",
        allowed_tools=("Read",),
        system_prompt=None,
    )
    assert "--system-prompt" not in argv
    assert "--append-system-prompt" not in argv


def test_argv_omits_allowed_tools_when_empty() -> None:
    argv = runner._build_argv(
        "hello",
        binary="claude",
        allowed_tools=(),
        system_prompt=None,
    )
    assert "--allowed-tools" not in argv


# ---------------------------------------------------------------------------
# Envelope parsing
# ---------------------------------------------------------------------------


def _stream(events: list[dict]) -> str:
    """Render events as JSONL — the shape ``--output-format stream-json`` emits."""
    return "\n".join(json.dumps(e) for e in events)


def test_parse_extracts_final_reply_and_usage() -> None:
    envelope = _stream(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "thinking"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "Read",
                            "input": {"file_path": "fitness/profile.yaml"},
                        },
                    ],
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_1", "content": "..."}
                    ]
                },
            },
            {
                "type": "result",
                "result": "Logged your run.",
                "usage": {"input_tokens": 500, "output_tokens": 40},
            },
        ]
    )

    reply, tin, tout, tools = runner._parse_envelope(envelope)
    assert reply == "Logged your run."
    assert tin == 500
    assert tout == 40
    assert len(tools) == 1
    assert tools[0]["name"] == "Read"
    assert tools[0]["input"] == {"file_path": "fitness/profile.yaml"}
    assert tools[0]["id"] == "toolu_1"


def test_parse_handles_multiple_tool_uses() -> None:
    envelope = _stream(
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "a", "name": "Glob", "input": {"pattern": "*.md"}},
                        {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "x.md"}},
                    ]
                },
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "id": "c", "name": "Write", "input": {"file_path": "y.md", "content": "..."}},
                    ]
                },
            },
            {"type": "result", "result": "done", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
    )

    _reply, _tin, _tout, tools = runner._parse_envelope(envelope)
    assert [t["name"] for t in tools] == ["Glob", "Read", "Write"]


def test_parse_raises_on_invalid_json() -> None:
    with pytest.raises(runner.ClaudeRunnerError, match="could not parse"):
        runner._parse_envelope("not json")


def test_parse_raises_on_empty_output() -> None:
    with pytest.raises(runner.ClaudeRunnerError, match="no output"):
        runner._parse_envelope("   \n  ")


def test_parse_raises_when_no_result_event() -> None:
    envelope = _stream(
        [
            {"type": "system", "subtype": "init"},
            {"type": "assistant", "message": {"content": []}},
        ]
    )
    with pytest.raises(runner.ClaudeRunnerError, match="no result event"):
        runner._parse_envelope(envelope)


# ---------------------------------------------------------------------------
# User-message assembly
# ---------------------------------------------------------------------------


def test_build_user_message_includes_session_and_turns(tmp_path: Path) -> None:
    from agent import chat_log, session

    sess = session.Session(
        chat_id="12345",
        session_id="abc",
        started_at="2026-05-11T10:00:00+00:00",
        last_updated="2026-05-11T10:05:00+00:00",
        turns=2,
        summary="- prior note 1\n- prior note 2",
    )
    turns = [
        chat_log.ChatTurn(
            id="t1",
            ts="2026-05-11T10:04:00+00:00",
            chat_id="12345",
            user_msg="how many runs this month?",
            bot_reply="8 runs.",
        ),
    ]

    msg = runner._build_user_message(
        user_msg="add an extra back exercise",
        sess=sess,
        recent_turns=turns,
    )

    assert "chat_id=12345" in msg
    assert "prior note 1" in msg
    assert "how many runs this month?" in msg
    assert "add an extra back exercise" in msg
    assert "## New user message" in msg


def test_build_user_message_handles_empty_session() -> None:
    from agent import session

    sess = session.Session(
        chat_id="1",
        session_id="z",
        started_at="2026-05-11T10:00:00+00:00",
        last_updated="2026-05-11T10:00:00+00:00",
        turns=0,
        summary="",
    )
    msg = runner._build_user_message(user_msg="hi", sess=sess, recent_turns=[])
    assert "no prior notes this session" in msg
    assert "no prior turns in this session" in msg


# ---------------------------------------------------------------------------
# Attachment composition: combining user caption with saved-file paths
# ---------------------------------------------------------------------------


def test_compose_user_text_with_caption_and_one_attachment(tmp_path: Path) -> None:
    """With a caption AND one attachment, the agent sees:

        <caption>\\n\\n[attachment saved to: <rel>]

    The relative path is computed against ``vault_root`` so the LLM
    (running with ``cwd=vault_root``) can ``Read`` it directly.
    """
    vault_root = tmp_path
    att = vault_root / "_inbox" / "raw" / "2026-05-11" / "120000-stmt.pdf"
    att.parent.mkdir(parents=True, exist_ok=True)
    att.write_bytes(b"x")

    composed = runner._compose_user_text(
        caption="april statement",
        attachments=(att,),
        vault_root=vault_root,
    )
    assert composed == (
        "april statement\n\n"
        "[attachment saved to: _inbox/raw/2026-05-11/120000-stmt.pdf]"
    )


def test_compose_user_text_with_empty_caption_only_attachment(tmp_path: Path) -> None:
    """No caption + one attachment → just the bracketed notice, no
    leading newlines or empty string prefix."""
    att = tmp_path / "_inbox" / "raw" / "2026-05-11" / "130000-x.pdf"
    att.parent.mkdir(parents=True, exist_ok=True)
    att.write_bytes(b"x")

    composed = runner._compose_user_text(
        caption="",
        attachments=(att,),
        vault_root=tmp_path,
    )
    assert composed == "[attachment saved to: _inbox/raw/2026-05-11/130000-x.pdf]"
    assert not composed.startswith("\n")


def test_compose_user_text_no_attachments_is_passthrough(tmp_path: Path) -> None:
    """Regression guard: with no attachments, the caption is returned
    byte-for-byte. This keeps the existing text-only call path identical
    to its pre-attachment behavior."""
    for caption in ["hello", "", "multi\nline\ntext", "  spaces  "]:
        out = runner._compose_user_text(
            caption=caption,
            attachments=(),
            vault_root=tmp_path,
        )
        assert out == caption


def test_compose_user_text_multiple_attachments(tmp_path: Path) -> None:
    """Each attachment becomes its own ``[attachment saved to: ...]`` line,
    appended after a blank line under the caption."""
    a = tmp_path / "_inbox" / "raw" / "2026-05-11" / "100000-a.pdf"
    b = tmp_path / "_inbox" / "raw" / "2026-05-11" / "100001-b.pdf"
    for p in (a, b):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    composed = runner._compose_user_text(
        caption="two files",
        attachments=(a, b),
        vault_root=tmp_path,
    )
    assert composed == (
        "two files\n\n"
        "[attachment saved to: _inbox/raw/2026-05-11/100000-a.pdf]\n"
        "[attachment saved to: _inbox/raw/2026-05-11/100001-b.pdf]"
    )


# ---------------------------------------------------------------------------
# handle_turn with attachments — end-to-end wiring
# ---------------------------------------------------------------------------


def test_handle_turn_threads_attachments_into_user_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``handle_turn`` accepts ``attachments=`` and the synthesized user
    message handed to ``invoke_claude`` contains the bracketed notice
    with the vault-relative path."""
    # Set up a fake vault.
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_ROOT", str(vault))

    # Place an attachment so the relative-path computation works.
    att = vault / "_inbox" / "raw" / "2026-05-11" / "120000-stmt.pdf"
    att.parent.mkdir(parents=True, exist_ok=True)
    att.write_bytes(b"x")

    # Capture the assembled user message instead of spawning claude -p.
    captured: dict[str, str] = {}

    def fake_invoke_claude(user_message, **_kwargs):
        captured["user_message"] = user_message
        return ("ok", 1, 1, ())

    monkeypatch.setattr(runner, "invoke_claude", fake_invoke_claude)

    reply = runner.handle_turn(
        chat_id="42",
        user_msg="april statement",
        attachments=(att,),
    )

    assert reply == "ok"
    assert "april statement" in captured["user_message"]
    assert (
        "[attachment saved to: _inbox/raw/2026-05-11/120000-stmt.pdf]"
        in captured["user_message"]
    )


def test_handle_turn_default_attachments_is_text_only_passthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: calling ``handle_turn`` with no ``attachments``
    kwarg produces a user message that does NOT contain the
    ``[attachment saved to: ...]`` marker. Existing callers stay
    behaviorally identical."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_ROOT", str(vault))

    captured: dict[str, str] = {}

    def fake_invoke_claude(user_message, **_kwargs):
        captured["user_message"] = user_message
        return ("ok", 1, 1, ())

    monkeypatch.setattr(runner, "invoke_claude", fake_invoke_claude)

    runner.handle_turn(chat_id="1", user_msg="just a text message")

    assert "just a text message" in captured["user_message"]
    assert "[attachment saved to:" not in captured["user_message"]
