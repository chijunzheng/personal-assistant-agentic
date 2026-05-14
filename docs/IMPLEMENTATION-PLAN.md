# Implementation plan

**Last updated**: 2026-05-14
**Open issues**: 1 — **Closed**: 4 — **Ready now**: 1

## Recently closed

For orientation:

- #1 Bridge captures file attachments and forwards them to the agent turn
- #2 System-prompt rules: handle attachments + ingest credit card / bank statements into finance/transactions.jsonl
- #5 Daily digest: proactive 6am push of overdue reminders + memory TODOs + one suggestion
- #6 Weekly reflection digest: Sunday 6am, preps a journal draft + Telegram nudge

## Wave structure

| Wave | Issues | Unblocks | Notes |
|---|---|---|---|
| 0 (foundation) | #9 | — | Ready immediately — no open blockers; terminal |

Per-issue detail, grouped by wave:

### Wave 0

- **#9 Switch digest trigger from launchd to in-process scheduling (PTB JobQueue)** — Blocked by: none. HITL: no. Touches: `agent/telegram_bridge.py` (add JobQueue jobs), `agent/digest.py` (unchanged logic, callbacks wire to it), deletes `infra/launchd/*`, new `docs/adr/0003-in-process-digest-scheduling.md`, status note on `docs/adr/0002`, tests for the bridge job registration. Prior art: `agent/digest.py` (`run_daily_digest`, `run_weekly_digest`), `agent/telegram_bridge.py` (the PTB `Application` setup), `docs/adr/0002-proactive-digest-modality.md` (the ADR being superseded in part).

## Currently ready

Issues with zero open blockers as of 2026-05-14:

1. **#9 Switch digest trigger to in-process scheduling** — Recommended next. Only open issue; terminal. Swaps the digest trigger mechanism now that the host is confirmed always-on (Mac mini).

## Critical path

`#9`

**Length**: 1 issue.

## HITL pause points

None. #9 is AFK — the architectural decision (in-process scheduling, given an always-on host) is already made; implementation is mechanical.

## Parallelism windows

None — single-issue queue.

## Dispatch prompt

Populated for #9:

```
You are implementing GitHub issue #9 end-to-end via the /tdd workflow.

## Bootloader — read in this order

1. `gh issue view 9 --comments` — issue body and acceptance criteria
2. `CLAUDE.md` (project root) — no CONTEXT.md exists; CLAUDE.md is the canonical contract
3. `docs/adr/0002-proactive-digest-modality.md` — the ADR whose scheduling-mechanism
   decision #9 supersedes. Your ADR 0003 follows the same format.
4. Existing code:
   - `agent/digest.py` — `run_daily_digest` / `run_weekly_digest` are the digest logic.
     They DO NOT change. The new in-process jobs call them. The `--mode` CLI stays.
   - `agent/telegram_bridge.py` — the PTB `Application` setup; you register two
     `JobQueue` jobs here on startup.
   - `infra/launchd/` — the two plists + README being deleted.

## Workflow — /tdd

RED → GREEN → IMPROVE, one test per acceptance criterion. Do not write all tests first.

## Definition of done

- Every acceptance criterion from issue #9 satisfied
- Full pytest suite green (84+ tests before your changes)
- Lint / type checks pass (if ruff/mypy unavailable in venv, run py_compile and say so)
- PR opened against main, title `<type>: <description>`, body `Closes #9`, no Claude attribution
- PR NOT merged — orchestrator owns merge

## Stop conditions (surface, do not proceed)

- An acceptance criterion is ambiguous or contradicts an ADR
- PTB's JobQueue cannot be made to work without a dependency change you're unsure about
- An architectural decision beyond the trigger swap needs making
```
