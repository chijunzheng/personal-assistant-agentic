"""Tests for the proactive daily-digest entrypoint (``agent/digest.py``).

These never hit the network or spawn ``claude -p``. The Telegram POST and
the ``claude -p`` subprocess are both injected so the tests are honest
unit tests: they pin the *invocation shape*, not live behavior.

End-to-end behavior is covered by the launchd smoke test documented in
``infra/launchd/README.md`` (``launchctl start ...``).
"""

from __future__ import annotations

import pytest

from agent import digest


# ---------------------------------------------------------------------------
# Outbound send helper: POST to the Telegram Bot API sendMessage endpoint
# ---------------------------------------------------------------------------


def test_send_telegram_message_targets_sendmessage_url_and_payload() -> None:
    """The push primitive POSTs to ``.../bot<token>/sendMessage`` with a
    JSON body carrying ``chat_id`` and ``text``. Unlike the reply path it
    has no incoming message to ``reply_text`` against."""
    captured: dict[str, object] = {}

    def fake_post(url: str, payload: dict[str, object]) -> None:
        captured["url"] = url
        captured["payload"] = payload

    digest.send_telegram_message(
        text="good morning",
        token="123:ABC",
        chat_id="5240954069",
        post_fn=fake_post,
    )

    assert captured["url"] == "https://api.telegram.org/bot123:ABC/sendMessage"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["chat_id"] == "5240954069"
    assert payload["text"] == "good morning"


def test_send_telegram_message_wraps_post_failure_as_digest_error() -> None:
    """A network error inside the POST surfaces as ``DigestError`` so the
    process exits non-zero rather than crashing with a bare urllib
    exception. No silent crash."""

    def exploding_post(_url: str, _payload: dict) -> None:
        raise ConnectionError("network down")

    with pytest.raises(digest.DigestError):
        digest.send_telegram_message(
            text="hi",
            token="t",
            chat_id="c",
            post_fn=exploding_post,
        )


# ---------------------------------------------------------------------------
# Mode dispatch: --mode=daily selects prompts/digest.md (NOT system.md)
# ---------------------------------------------------------------------------


def test_daily_mode_selects_digest_prompt_not_system_prompt() -> None:
    """``--mode=daily`` resolves to ``prompts/digest.md`` — the digest
    turn is a proactive generation with no user message, a different
    contract from ``prompts/system.md``."""
    path = digest.build_digest_argv_prompt_path("daily")
    assert path.name == "digest.md"
    assert path.parent.name == "prompts"
    assert path.name != "system.md"


def test_unknown_mode_raises_digest_error() -> None:
    with pytest.raises(digest.DigestError, match="unknown digest mode"):
        digest.build_digest_argv_prompt_path("monthly")


# ---------------------------------------------------------------------------
# generate_digest: assembles the correct claude -p invocation
# ---------------------------------------------------------------------------


def test_generate_digest_invokes_claude_with_digest_prompt(tmp_path) -> None:
    """``generate_digest`` runs ``claude -p`` with ``cwd=vault_root`` and
    the ``prompts/digest.md`` contents as the system prompt, then returns
    the envelope's reply text."""
    captured: dict[str, object] = {}

    def fake_invoke(user_message, *, cwd, system_prompt, **_kwargs):
        captured["user_message"] = user_message
        captured["cwd"] = cwd
        captured["system_prompt"] = system_prompt
        return ("Here is your digest.", 10, 5, ())

    text = digest.generate_digest(
        mode="daily",
        vault_root=tmp_path,
        invoke_fn=fake_invoke,
    )

    assert text == "Here is your digest."
    assert captured["cwd"] == tmp_path
    # The system prompt is the real digest.md file content, not system.md.
    assert isinstance(captured["system_prompt"], str)
    assert captured["system_prompt"].strip() != ""
    # The user message is the digest trigger, not a Telegram message.
    assert "digest" in str(captured["user_message"]).lower()


def test_generate_digest_wraps_claude_error_as_digest_error(tmp_path) -> None:
    """A ``ClaudeRunnerError`` from the subprocess becomes a
    ``DigestError`` — the scheduled job exits non-zero and logs, never
    silently crashes."""
    from agent.runner import ClaudeRunnerError

    def failing_invoke(_user_message, **_kwargs):
        raise ClaudeRunnerError("claude -p exited with code 1")

    with pytest.raises(digest.DigestError, match="claude -p failed"):
        digest.generate_digest(
            mode="daily",
            vault_root=tmp_path,
            invoke_fn=failing_invoke,
        )


def test_generate_digest_rejects_empty_reply(tmp_path) -> None:
    """An empty reply from ``claude -p`` is an error — we never push a
    blank message to Telegram."""

    def empty_invoke(_user_message, **_kwargs):
        return ("   ", 1, 1, ())

    with pytest.raises(digest.DigestError, match="empty digest"):
        digest.generate_digest(
            mode="daily",
            vault_root=tmp_path,
            invoke_fn=empty_invoke,
        )


# ---------------------------------------------------------------------------
# run_daily_digest: end-to-end wiring (generate -> push), env-driven
# ---------------------------------------------------------------------------


def test_run_daily_digest_generates_then_pushes(tmp_path, monkeypatch) -> None:
    """``run_daily_digest`` reads env, generates via ``claude -p``, and
    pushes the result to Telegram addressed by ``TELEGRAM_CHAT_ID``."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_ROOT", str(vault))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:XYZ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "5240954069")

    sent: dict[str, object] = {}

    def fake_invoke(_user_message, **_kwargs):
        return ("Morning digest body.", 1, 1, ())

    def fake_post(url: str, payload: dict) -> None:
        sent["url"] = url
        sent["payload"] = payload

    text = digest.run_daily_digest(invoke_fn=fake_invoke, post_fn=fake_post)

    assert text == "Morning digest body."
    assert sent["url"] == "https://api.telegram.org/bot999:XYZ/sendMessage"
    assert sent["payload"]["chat_id"] == "5240954069"
    assert sent["payload"]["text"] == "Morning digest body."


def test_run_daily_digest_requires_chat_id(tmp_path, monkeypatch) -> None:
    """Missing ``TELEGRAM_CHAT_ID`` is a hard error — the push has no
    destination without it."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_ROOT", str(vault))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:XYZ")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(digest.DigestError, match="TELEGRAM_CHAT_ID"):
        digest.run_daily_digest(
            invoke_fn=lambda *_a, **_k: ("x", 1, 1, ()),
            post_fn=lambda _u, _p: None,
        )


# ---------------------------------------------------------------------------
# main(): CLI exit codes — 0 on success, non-zero on failure
# ---------------------------------------------------------------------------


def test_main_daily_returns_zero_on_success(monkeypatch) -> None:
    """``python -m agent.digest --mode=daily`` exits 0 when the run
    succeeds."""
    monkeypatch.setattr(
        digest, "run_daily_digest", lambda **_kwargs: "ok digest"
    )
    assert digest.main(["--mode=daily"]) == 0


def test_main_daily_returns_nonzero_on_digest_error(monkeypatch) -> None:
    """A ``DigestError`` (claude failure, network failure, missing env)
    makes the CLI exit non-zero so launchd logs a visible failure rather
    than a silent skip."""

    def boom(**_kwargs):
        raise digest.DigestError("claude -p failed")

    monkeypatch.setattr(digest, "run_daily_digest", boom)
    assert digest.main(["--mode=daily"]) == 1
