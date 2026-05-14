"""Tests for the proactive daily-digest entrypoint (``agent/digest.py``).

These never hit the network or spawn ``claude -p``. The Telegram POST and
the ``claude -p`` subprocess are both injected so the tests are honest
unit tests: they pin the *invocation shape*, not live behavior.

The production trigger is in-process (PTB ``JobQueue``, see ADR 0003) —
job registration is covered in ``tests/test_telegram_bridge.py``. Manual
end-to-end smoke testing uses the CLI: ``python -m agent.digest --mode=daily``.
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


def test_weekly_mode_selects_weekly_digest_prompt() -> None:
    """``--mode=weekly`` resolves to ``prompts/digest.md`` — the weekly
    reflection digest reuses the same digest contract file as daily; the
    weekly *section* inside it drives the reflection-oriented behavior."""
    path = digest.build_digest_argv_prompt_path("weekly")
    assert path.name == "digest.md"
    assert path.parent.name == "prompts"


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


def test_generate_digest_weekly_uses_distinct_user_message(tmp_path) -> None:
    """``generate_digest(mode="weekly")`` is distinguishable from
    ``mode="daily"`` in the invocation: the user message names the weekly
    reflection turn, so the LLM acts on the weekly section of the prompt
    and Writes a reflection draft rather than producing a daily push."""
    captured: dict[str, object] = {}

    def fake_invoke(user_message, *, cwd, system_prompt, **_kwargs):
        captured["user_message"] = user_message
        return ("reflection draft ready", 1, 1, ())

    daily_msg_holder: dict[str, object] = {}

    def fake_invoke_daily(user_message, *, cwd, system_prompt, **_kwargs):
        daily_msg_holder["user_message"] = user_message
        return ("daily digest", 1, 1, ())

    digest.generate_digest(
        mode="weekly", vault_root=tmp_path, invoke_fn=fake_invoke
    )
    digest.generate_digest(
        mode="daily", vault_root=tmp_path, invoke_fn=fake_invoke_daily
    )

    weekly_msg = str(captured["user_message"]).lower()
    daily_msg = str(daily_msg_holder["user_message"]).lower()
    assert "weekly" in weekly_msg
    assert "reflection" in weekly_msg
    # The two modes must not send the same trigger message.
    assert weekly_msg != daily_msg


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
# run_weekly_digest: end-to-end wiring (generate -> push), env-driven
# ---------------------------------------------------------------------------


def test_run_weekly_digest_generates_then_pushes(tmp_path, monkeypatch) -> None:
    """``run_weekly_digest`` reads env, generates the weekly reflection via
    ``claude -p`` (the LLM Writes the draft into ``journal/`` itself), and
    pushes the short nudge to Telegram addressed by ``TELEGRAM_CHAT_ID``.
    It reuses ``send_telegram_message`` — the push helper is not
    duplicated."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_ROOT", str(vault))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:XYZ")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "5240954069")

    seen: dict[str, object] = {}
    sent: dict[str, object] = {}

    def fake_invoke(user_message, *, cwd, system_prompt, **_kwargs):
        seen["user_message"] = user_message
        return ("weekly reflection's ready in journal/...", 1, 1, ())

    def fake_post(url: str, payload: dict) -> None:
        sent["url"] = url
        sent["payload"] = payload

    text = digest.run_weekly_digest(invoke_fn=fake_invoke, post_fn=fake_post)

    assert text == "weekly reflection's ready in journal/..."
    # The weekly trigger drove the turn, not the daily one.
    assert "weekly" in str(seen["user_message"]).lower()
    assert sent["url"] == "https://api.telegram.org/bot999:XYZ/sendMessage"
    assert sent["payload"]["chat_id"] == "5240954069"
    assert sent["payload"]["text"] == "weekly reflection's ready in journal/..."


def test_run_weekly_digest_reuses_send_telegram_message(tmp_path, monkeypatch) -> None:
    """The weekly path must route its push through the shared
    ``send_telegram_message`` helper — not a duplicated sender."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_ROOT", str(vault))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    calls: list[dict] = []

    def spy_send(*, text, token, chat_id, post_fn):
        calls.append({"text": text, "token": token, "chat_id": chat_id})

    monkeypatch.setattr(digest, "send_telegram_message", spy_send)

    digest.run_weekly_digest(
        invoke_fn=lambda *_a, **_k: ("nudge", 1, 1, ()),
        post_fn=lambda _u, _p: None,
    )

    assert calls == [{"text": "nudge", "token": "t", "chat_id": "c"}]


def test_run_weekly_digest_requires_chat_id(tmp_path, monkeypatch) -> None:
    """Missing ``TELEGRAM_CHAT_ID`` is a hard error for the weekly push
    too — the nudge has no destination without it."""
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("VAULT_ROOT", str(vault))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "999:XYZ")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(digest.DigestError, match="TELEGRAM_CHAT_ID"):
        digest.run_weekly_digest(
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
    makes the CLI exit non-zero so a manual smoke-test run surfaces the
    failure visibly rather than as a silent skip."""

    def boom(**_kwargs):
        raise digest.DigestError("claude -p failed")

    monkeypatch.setattr(digest, "run_daily_digest", boom)
    assert digest.main(["--mode=daily"]) == 1


def test_main_weekly_dispatches_to_run_weekly_digest(monkeypatch) -> None:
    """``python -m agent.digest --mode=weekly`` runs ``run_weekly_digest``
    (not the daily one) and exits 0 on success."""
    called: list[str] = []

    def fake_daily(**_kwargs):
        called.append("daily")
        return "daily"

    def fake_weekly(**_kwargs):
        called.append("weekly")
        return "weekly nudge"

    monkeypatch.setattr(digest, "run_daily_digest", fake_daily)
    monkeypatch.setattr(digest, "run_weekly_digest", fake_weekly)

    assert digest.main(["--mode=weekly"]) == 0
    assert called == ["weekly"]


def test_main_weekly_returns_nonzero_on_digest_error(monkeypatch) -> None:
    """A ``DigestError`` on the weekly path makes the CLI exit non-zero so
    a manual smoke-test run surfaces the failure visibly."""

    def boom(**_kwargs):
        raise digest.DigestError("claude -p failed")

    monkeypatch.setattr(digest, "run_weekly_digest", boom)
    assert digest.main(["--mode=weekly"]) == 1
