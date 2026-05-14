# Implementation plan

**Last updated**: 2026-05-14
**Open issues**: 2 — **Closed**: 2 — **Ready now**: 1

## Recently closed

For orientation:

- #1 Bridge captures file attachments and forwards them to the agent turn
- #2 System-prompt rules: handle attachments + ingest credit card / bank statements into finance/transactions.jsonl

## Wave structure

| Wave | Issues | Unblocks | Notes |
|---|---|---|---|
| 0 (foundation) | #5 | Wave 1 | Ready immediately — no open blockers |
| 1 | #6 | — | Blocked only on #5; terminal |

Per-issue detail, grouped by wave:

### Wave 0

- **#5 Daily digest: proactive 6am push of overdue reminders + memory TODOs + one suggestion** — Blocked by: none. HITL: no. Touches: new `agent/digest.py`, new `prompts/digest.md`, new `infra/launchd/com.jason.personal-assistant.digest-daily.plist`, new `docs/adr/0002-proactive-digest-modality.md`, new `tests/test_digest.py`, `.env` (+`TELEGRAM_CHAT_ID`). Prior art: `agent/telegram_bridge.py` (`_send_reply`), `agent/runner.py` (`invoke_claude`, `_load_system_prompt`).

### Wave 1

- **#6 Weekly reflection digest: Sunday 6am, preps a journal draft + Telegram nudge** — Blocked by: #5. HITL: no. Touches: extends `agent/digest.py` (`--mode=weekly`), extends `prompts/digest.md` (weekly section), new `infra/launchd/com.jason.personal-assistant.digest-weekly.plist`, extends `tests/test_digest.py`. Reuses the outbound send helper + entrypoint scaffold from #5.

## Currently ready

Issues with zero open blockers as of 2026-05-14:

1. **#5 Daily digest** — Recommended next. It's the entire foundation: the `agent/digest.py` entrypoint, the outbound Telegram push primitive, the `prompts/digest.md` scaffold, and the push-modality ADR all land here. #6 cannot start until these exist.

## Critical path

Longest remaining chain from a ready-now issue to closure:

`#5 → #6`

**Length**: 2 issues.

## HITL pause points

None. Both issues are AFK — sub-agent-dispatchable.

## Parallelism windows

- **Closing #5 unblocks**: #6 (Wave 1). No parallel pairs — #6 is the only downstream issue.

No parallelism available in this queue; it's a linear chain. Dispatch #5, merge, then dispatch #6.

## Dispatch prompt

Populated for the recommended next issue (#5):

```
You are implementing GitHub issue #5 end-to-end via the /tdd workflow.

## Bootloader — read in this order

1. `gh issue view 5 --comments` — issue body, acceptance criteria
2. `CLAUDE.md` (project root) — the project has no CONTEXT.md; CLAUDE.md is the
   canonical contract. Note the "cardinal rule" and the kernel-change escape hatch.
3. `docs/adr/0001-attachment-input-modality.md` — the ADR style + the precedent for
   a kernel change authorized by a new modality. Your ADR 0002 follows this shape.
4. Existing code touching the bridge + runner:
   - `agent/telegram_bridge.py` — `_send_reply` is the *reply* primitive; you need a
     new *push* primitive (HTTP POST to the Bot API `sendMessage`), since a digest
     has no incoming message to reply to.
   - `agent/runner.py` — `invoke_claude` and `_load_system_prompt` show the `claude -p`
     invocation pattern; `agent/digest.py` invokes claude -p with `prompts/digest.md`.

## Workflow — /tdd

- RED: write a failing test that pins the behavior described in the acceptance criteria
- GREEN: minimal implementation that makes the test pass
- IMPROVE: refactor without changing behavior; clean up names, extract helpers
- Repeat per acceptance criterion

## Definition of done

- Every acceptance criterion from the issue body is satisfied
- All tests pass (existing + new)
- Lint and type checks pass
- Pull request opened against `main`
  - Title format: `<type>: <description>` (feat/fix/refactor/docs/test/chore/perf/ci)
  - Body links the issue: `Closes #5`
  - No Claude attribution
- PR is NOT merged — orchestrator owns merge

## Stop conditions (surface, do not proceed)

- An acceptance criterion is ambiguous or contradicts an ADR
- An architectural decision needs to be made (decisions belong in ADRs, not code)
- Test infrastructure is missing such that you cannot honestly write a RED test first
- The digest entrypoint genuinely cannot avoid touching a kernel invariant beyond the
  authorized push-modality expansion — surface it rather than silently editing the kernel
```
