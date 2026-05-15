"""Tests for the proactive daily-digest entrypoint (``agent/digest.py``).

These never hit the network or spawn ``claude -p``. The Telegram POST and
the ``claude -p`` subprocess are both injected so the tests are honest
unit tests: they pin the *invocation shape*, not live behavior.

The production trigger is in-process (PTB ``JobQueue``, see ADR 0003) —
job registration is covered in ``tests/test_telegram_bridge.py``. Manual
end-to-end smoke testing uses the CLI: ``python -m agent.digest --mode=daily``.
"""

from __future__ import annotations

import io
import urllib.error

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


def test_send_telegram_message_renders_markdown_as_telegram_html() -> None:
    """The push primitive converts markdown to Telegram-flavored HTML and
    sets ``parse_mode=HTML`` so ``**Due today:**`` arrives bolded rather
    than as literal asterisks. Mirrors the reply path's ``_send_reply``."""
    captured: dict[str, object] = {}

    def fake_post(url: str, payload: dict[str, object]) -> None:
        captured["payload"] = payload

    digest.send_telegram_message(
        text="**Due today:** finish PR",
        token="123:ABC",
        chat_id="5240954069",
        post_fn=fake_post,
    )

    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["parse_mode"] == "HTML"
    # Bold survives the conversion; literal asterisks do not.
    assert payload["text"] == "<b>Due today:</b> finish PR"
    assert "**" not in payload["text"]
    # The existing fields stay intact.
    assert payload["chat_id"] == "5240954069"
    assert payload["disable_web_page_preview"] is True


def test_send_telegram_message_retries_plain_on_http_400() -> None:
    """When Telegram rejects the HTML payload with HTTP 400 (malformed
    markup, unbalanced tag, etc.) the send retries once with the raw text
    and no ``parse_mode`` — same fallback shape as ``_send_reply``'s
    ``BadRequest`` handler. Users see something either way."""
    calls: list[dict[str, object]] = []

    def post_with_400(url: str, payload: dict[str, object]) -> None:
        calls.append({"url": url, "payload": dict(payload)})
        # Only the first (HTML) call fails; the plain retry succeeds.
        if len(calls) == 1:
            raise urllib.error.HTTPError(
                url=url,
                code=400,
                msg="Bad Request: can't parse entities",
                hdrs=None,
                fp=io.BytesIO(b""),
            )

    digest.send_telegram_message(
        text="**bad** _markup_",
        token="t",
        chat_id="c",
        post_fn=post_with_400,
    )

    assert len(calls) == 2
    # First attempt: HTML mode.
    first = calls[0]["payload"]
    assert first["parse_mode"] == "HTML"
    assert first["text"] == "<b>bad</b> <i>markup</i>"
    # Retry: raw text, no parse_mode at all.
    second = calls[1]["payload"]
    assert "parse_mode" not in second
    assert second["text"] == "**bad** _markup_"
    assert second["chat_id"] == "c"
    assert second["disable_web_page_preview"] is True


def test_send_telegram_message_does_not_retry_on_non_400_http_error() -> None:
    """Non-400 HTTP errors (auth, server, rate-limit) are not the
    malformed-markup signal and must not trigger the plain-text retry —
    they surface as ``DigestError`` so the run fails loudly."""
    calls: list[dict[str, object]] = []

    def post_with_500(url: str, payload: dict[str, object]) -> None:
        calls.append({"url": url, "payload": dict(payload)})
        raise urllib.error.HTTPError(
            url=url,
            code=500,
            msg="Internal Server Error",
            hdrs=None,
            fp=io.BytesIO(b""),
        )

    with pytest.raises(digest.DigestError):
        digest.send_telegram_message(
            text="hi",
            token="t",
            chat_id="c",
            post_fn=post_with_500,
        )

    # Only one attempt — no retry on 500.
    assert len(calls) == 1


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


# ---------------------------------------------------------------------------
# Daily digest prompt rewrite (issue #22) — wider scope, sharp prompts,
# persistence escalation. These tests pin the *contract* in prompts/digest.md
# rather than live LLM behavior; we treat the prompt file as a public API and
# assert the new section names, escalation language, and tool affordances are
# all present in the daily section.
# ---------------------------------------------------------------------------


def _read_daily_section() -> str:
    """Return just the daily section of ``prompts/digest.md``.

    The file carries a daily section and a weekly section separated by the
    ``# Weekly reflection`` heading. Slicing here keeps the assertions from
    accidentally passing because the same string appears under weekly.
    """
    path = digest.build_digest_argv_prompt_path("daily")
    text = path.read_text(encoding="utf-8")
    daily_start = text.index("# Daily digest")
    weekly_start = text.index("# Weekly reflection")
    return text[daily_start:weekly_start]


def test_daily_digest_lists_five_message_sections() -> None:
    """The rewritten daily section enumerates the five new message sections
    by name: due today/overdue, coming up, undated obligations, threads in
    motion, reflection prompts. Each must be discoverable in the prompt so
    the LLM has a checklist to assemble from."""
    daily = _read_daily_section()
    assert "Due today" in daily or "due today" in daily
    assert "overdue" in daily.lower()
    assert "Coming up" in daily or "coming up" in daily
    assert "14" in daily  # the ≤14-day window
    assert "Undated obligations" in daily or "undated obligations" in daily.lower()
    assert "Threads in motion" in daily or "threads in motion" in daily.lower()
    assert "reflection prompt" in daily.lower()


def test_daily_digest_calls_for_persistence_escalation() -> None:
    """Overdue items must be annotated with how long they've been overdue
    ("since 5/12 — 4 days") and the phrasing must escalate past 2 days."""
    daily = _read_daily_section()
    lowered = daily.lower()
    # Persistence is computed from JSONL ts/due; the spec must say so.
    assert "persistence" in lowered or "days overdue" in lowered or "how long" in lowered
    # The 2-day escalation threshold is named explicitly.
    assert "2 days" in lowered or "two days" in lowered
    assert "escalat" in lowered  # "escalate" / "escalation"


def test_daily_digest_persistence_computed_from_jsonl_not_audit() -> None:
    """Persistence escalation must come from ``reminders.jsonl`` ts/due
    fields directly. The digest stays out of ``_audit/``/``_index/``/
    ``_chat_log/``."""
    daily = _read_daily_section()
    # The reminders.jsonl source is named as the persistence input.
    assert "reminders.jsonl" in daily
    # The kernel-managed dirs are explicitly off-limits.
    assert "_audit" in daily
    assert "_index" in daily
    assert "_chat_log" in daily


def test_daily_digest_threads_in_motion_pulls_from_memory_and_journal() -> None:
    """Threads-in-motion is sourced from ``memory/MEMORY.md`` (project_*.md
    and in-flight user_*.md files) plus unresolved decisions in the last 3
    days of ``journal/``. Both sources must be named in the prompt."""
    daily = _read_daily_section()
    assert "memory/MEMORY.md" in daily
    assert "project_" in daily
    assert "user_" in daily
    assert "journal/" in daily
    assert "3 days" in daily.lower() or "three days" in daily.lower()


def test_daily_digest_reflection_prompts_grounded_and_capped_at_three() -> None:
    """At most 3 reflection prompts, each grounded in a specific event from
    the last 1-3 days. The 'could only have been written for today' test is
    the spec's quality bar — both must be in the prompt."""
    daily = _read_daily_section()
    lowered = daily.lower()
    # Cap is 3 prompts.
    assert "3 reflection" in lowered or "three reflection" in lowered or "at most 3" in lowered
    # The grounded/specific-event quality bar.
    assert "grounded" in lowered or "specific event" in lowered
    # The "could only have been written for today" test (or a paraphrase
    # carrying the same anti-generic intent).
    assert "could only have been written" in lowered or "only for today" in lowered


def test_daily_digest_lifts_read_only_rule_and_lists_write_tools() -> None:
    """The rewrite lifts the previous read-only constraint and grants the
    daily turn Read, Glob, Grep, Edit, Write. Writes are *rare and
    explicit* — the prompt must say so to prevent gratuitous mutation."""
    daily = _read_daily_section()
    # All five tools are listed for the daily turn.
    for tool in ("Read", "Glob", "Grep", "Edit", "Write"):
        assert tool in daily, f"{tool} should be listed for the daily turn"
    # Writes are constrained — the prompt must call them out as rare /
    # exceptional rather than routine.
    lowered = daily.lower()
    assert "rare" in lowered or "exception" in lowered or "explicit" in lowered


def test_daily_digest_message_length_scales_to_content() -> None:
    """Default short, denser when content warrants — the rewrite must say
    so explicitly so the LLM doesn't pad quiet days or truncate heavy
    ones. The single-sentence all-clear branch survives."""
    daily = _read_daily_section()
    lowered = daily.lower()
    # The length-scales-to-content rule is stated.
    assert "scale" in lowered or "scales" in lowered or "scaling" in lowered or "denser" in lowered
    # Default short remains.
    assert "short" in lowered
    # The all-clear branch (quiet day) is still a single-sentence message.
    assert "all-clear" in lowered or "nothing pressing" in lowered


def test_daily_digest_voice_rules_retained() -> None:
    """No robotic section headers, contractions, no emoji unless Jason uses
    one first — the voice rules from the previous spec must survive."""
    daily = _read_daily_section()
    lowered = daily.lower()
    assert "contraction" in lowered
    assert "emoji" in lowered
    # No-robotic-headers rule survives.
    assert "header" in lowered


def test_daily_digest_undated_obligations_filters_to_money_people_projects() -> None:
    """The Undated obligations section is the filter for the long tail of
    open reminders without ``due`` dates — it must mention the money /
    people / projects filter so the digest doesn't dump every undated
    chore."""
    daily = _read_daily_section()
    lowered = daily.lower()
    assert "undated" in lowered
    # The triage filter — only money / people / projects flavoured items.
    assert "money" in lowered
    assert ("people" in lowered or "person" in lowered)
    assert "project" in lowered


def test_daily_digest_anniversary_and_rogers_use_case_is_documented() -> None:
    """The rewrite was triggered by a real 2026-05-15 failure: the digest
    ignored the anniversary app (due 5/26) and the Rogers $8,280 payment
    (due 5/27). The prompt must call out the "coming up" window so those
    items surface — a future digest with that vault state must include
    both. We assert structurally: the prompt explains *why* coming-up
    matters and lists what kinds of items qualify."""
    daily = _read_daily_section()
    lowered = daily.lower()
    # The 14-day window is named.
    assert "14" in lowered
    # The window's purpose — surface items due within roughly two weeks so
    # Jason isn't blindsided — is in the prose. "Due" + "next" / "ahead"
    # framing is what we expect.
    assert "due" in lowered
    # Sorting rule is explicit so the LLM doesn't reorder by salience.
    assert "sort" in lowered or "order" in lowered
