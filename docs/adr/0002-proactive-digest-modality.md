# ADR 0002: Proactive digest output modality (scheduled push, not reply)

**Status:** Accepted
**Date:** 2026-05-14
**Issue:** [#5](https://github.com/chijunzheng/personal-assistant-agentic/issues/5)

## Context

Every path in the system so far is a *reply*. A Telegram message arrives,
`agent/telegram_bridge.py` hands it to `agent/runner.py`, `claude -p`
runs, and the bridge sends the answer back through PTB's
`message.reply_text` — which needs the incoming `Message` object to
reply against. The bot has never *initiated* a message.

Issue #5 asks for a daily digest: at 06:00 the assistant looks over the
vault (overdue reminders, `TODO:` markers in `memory/*.md`, the last
three days of `journal/`) and sends Jason one short morning message.
There is no incoming message to trigger it and nothing to reply to.

This is a new **output modality** — a proactive, outbound-initiated
push. `CLAUDE.md` answers most feature requests with "edit
`prompts/system.md`, not the runner," but explicitly lists "new input
modality" as a legitimate kernel change requiring an ADR first. A new
*output* modality is the symmetric case: the same surface-expansion
reasoning applies, and ADR 0001 set the precedent for documenting a
modality change before it lands. This ADR is that document.

## Decision

Add a standalone entrypoint, `agent/digest.py`, invoked by a launchd
agent. It is a sibling of the runner, not a modification of it.

1. **Standalone entrypoint, not a runner change.** `agent/digest.py`
   exposes `python -m agent.digest --mode=daily`. `agent/runner.py` is
   untouched — `handle_turn` still means "handle one incoming Telegram
   turn." The digest reuses `runner.invoke_claude` (the `claude -p`
   subprocess primitive) and `runner.ClaudeRunnerError`, but adds no
   reply-path coupling. The runner's contract stays "reply to a
   message"; the digest's contract is "generate and push."

2. **A new outbound send primitive.** `digest.send_telegram_message`
   POSTs to `https://api.telegram.org/bot<token>/sendMessage` with a
   JSON body carrying `chat_id` and `text`. This is the push counterpart
   to the bridge's `_send_reply`. It cannot reuse `_send_reply` because
   that needs a `Message` object; a scheduled job has none, so it
   addresses the chat directly by id.

3. **A new env var, `TELEGRAM_CHAT_ID`.** The reply path answers
   whatever chat messaged it. A proactive push has no such chat — it
   must be *told* where to send. `TELEGRAM_CHAT_ID` (value `5240954069`,
   Jason's personal chat) joins `TELEGRAM_BOT_TOKEN` and `VAULT_ROOT` in
   `.env`. It is never hardcoded in Python.

4. **A digest-specific system prompt, `prompts/digest.md`.** The digest
   turn is fundamentally different from a chat turn: proactive
   generation, no user message, read-only (no Write/Edit), three fixed
   content sources, omit-empty-sections. It gets its own contract.
   `prompts/system.md` is untouched. `--mode` maps a mode name to a
   prompt file; `daily` is the only mode in this slice, `weekly` (#6)
   registers without touching the rest of the module.

5. **The Python stays thin — the LLM reads the vault.** `agent/digest.py`
   does not parse `reminders.jsonl`, does not grep `memory/*.md`, does
   not read `journal/`. All content sourcing is *instructed* in
   `prompts/digest.md`; the LLM does it at digest time with its
   Read/Glob/Grep tools. The entrypoint only: resolves env, invokes
   `claude -p`, pushes the result.

6. **launchd, not an in-process scheduler.** See alternatives below.

7. **Stateless run.** Each digest is a fresh process: load env, generate,
   push, exit. It keeps no state between runs — consistent with the
   `claude -p` one-shot model and the rest of the codebase. The vault is
   the only state; the digest reads it and writes nothing.

8. **Fail loud.** Any failure — missing env, `claude -p` error, network
   error, empty reply — raises `DigestError` and the CLI exits non-zero.
   A scheduled job that fails silently is worse than one that fails
   visibly in the launchd log.

## Alternatives considered

### A. In-process scheduler inside the polling bridge

Rejected. The bridge (`python -m agent.telegram_bridge`) is a long-lived
process; we could add an `apscheduler` / `asyncio` timer that fires the
digest at 06:00. Rejected because: (1) it couples the digest's liveness
to the bridge's — if the bridge crashes or is restarted, the digest
silently stops, with no separate signal. (2) It adds a scheduling
dependency and in-process timer state to a process whose job is "poll
and reply." (3) launchd already exists, is the platform-native
scheduler, survives reboots, and logs exit codes. A standalone
entrypoint + launchd keeps each process single-purpose.

### B. cron

Rejected. `cron` works, but launchd is the macOS-native choice: it
handles missed runs across sleep/wake more gracefully, has structured
plist config, per-job log paths, and `launchctl start` for an
on-demand smoke test without waiting for the schedule. The project is
single-machine macOS (per `CLAUDE.md`'s single-agent assumption), so
there is no portability reason to prefer cron.

### C. Reuse `handle_turn` with a synthetic "generate the digest" message

Rejected. We could feed `handle_turn` a fake user message like "generate
my daily digest" and let the normal path run. Rejected because: (1) it
would write a chat-log turn and an audit `turn` entry for a message
Jason never sent, polluting the conversation history. (2) It would use
`prompts/system.md`, which has Write/Edit tools and chat-reply tone
rules — wrong contract for a read-only proactive push. (3) The reply
would go back through `_send_reply`, which needs a `Message`. Forcing
the digest through the reply path means faking three things; a separate
entrypoint fakes nothing.

### D. Bot library call instead of a raw HTTP POST

Rejected for this slice. PTB can send a message to a chat id without an
incoming `Message` (`bot.send_message(chat_id=...)`). But that needs an
initialized `Application` / `Bot` object and an event loop, pulling the
async machinery into a one-shot script for a single POST. A direct
`urllib` POST to `sendMessage` is dependency-free, synchronous, trivial
to mock in tests, and exactly as reliable for one tiny request.

## Consequences

### Positive

- The bot can now initiate messages. The digest is the first; the weekly
  reflection digest (#6) and any future proactive nudge reuse
  `send_telegram_message` and the `--mode` entrypoint scaffold.
- The runner and the reply path are completely untouched — no
  regression surface on the existing 65 tests.
- Each process stays single-purpose: the bridge polls and replies, the
  digest generates and pushes. Either can fail without taking the other
  down.
- The digest is independently testable: the `claude -p` subprocess and
  the Telegram POST are both injected seams, so the unit tests are
  honest (no live network, no real subprocess).
- launchd gives a free `launchctl start` smoke test and per-job logs.

### Negative

- The kernel surface grew: a new module, a new outbound primitive, a new
  env var, a new system prompt, and an `infra/launchd/` plist to keep in
  sync with absolute machine paths. If the repo or venv moves, the plist
  must be hand-edited (documented in `infra/launchd/README.md`).
- There are now two ways a message reaches Telegram (`_send_reply` in
  the bridge, `send_telegram_message` in the digest). They share no
  code. If the markdown→Telegram-HTML rendering ever needs to apply to
  the digest too, that is a follow-up — for now the digest sends plain
  text and `prompts/digest.md` constrains the LLM to Telegram-safe
  markup.
- `TELEGRAM_CHAT_ID` is a second piece of Telegram config that must be
  present in `.env` for the digest to run; a missing value is a hard
  failure (by design — see "Fail loud").

### Neutral

- The digest writes nothing to the vault and reads nothing from
  `_audit/` — so there is no audit entry for a digest run. The launchd
  log (`logs/digest-daily.{out,err}.log`) is the run record. If digest
  runs need to appear in the audit log later, that is a deliberate
  follow-up decision, not an oversight here.
- `prompts/system.md` is unchanged. The digest's contract lives entirely
  in `prompts/digest.md`.
