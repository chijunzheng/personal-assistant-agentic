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
    assert "json" in argv

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
    return json.dumps(events)


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


def test_parse_raises_on_wrong_envelope_type() -> None:
    with pytest.raises(runner.ClaudeRunnerError, match="unexpected"):
        runner._parse_envelope('{"type": "result", "result": "x"}')


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
