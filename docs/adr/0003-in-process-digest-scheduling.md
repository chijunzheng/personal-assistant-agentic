# ADR 0003: In-process digest scheduling (PTB JobQueue, not launchd)

**Status:** Accepted
**Date:** 2026-05-14
**Issue:** [#9](https://github.com/chijunzheng/personal-assistant-agentic/issues/9)

## Context

[ADR 0002](0002-proactive-digest-modality.md) introduced the proactive
digest: at 06:00 the assistant looks over the vault and pushes Jason a
morning message (daily), plus a Sunday reflection nudge (weekly). That
ADR chose **launchd** as the trigger — two XML plists invoking
`python -m agent.digest --mode=daily|weekly` on a `StartCalendarInterval`
schedule — and explicitly rejected an in-process scheduler (its
"Alternative A").

The launchd decision rested on three claimed advantages: it survives
reboots, it is the macOS-native scheduler, and — crucially — it handles
"missed runs across sleep/wake more gracefully." The host has since been
confirmed to be an **always-on Mac mini**. That confirmation removes the
load-bearing advantage:

- **Missed-run-on-wake is moot.** A box that never sleeps never misses a
  06:00 fire. The one thing launchd did that an in-process timer can't is
  irrelevant on this host.
- **Reboot survival is already covered.** The bot process itself must be
  kept alive across reboots regardless (it is the polling loop). Whatever
  keeps the bot up also keeps its in-process scheduler up — launchd added
  a *second*, separate liveness story for no extra coverage.

What launchd still *costs* is real: two plists carrying absolute machine
paths that must be hand-edited if the repo or venv moves (ADR 0002's own
"Negative" section flags this), a README of `launchctl load` steps, and
a second process to reason about. ADR 0002's Alternative A rejected the
in-process scheduler partly because "launchd already exists" — but that
is sunk-cost reasoning once its unique advantage is gone.

The bot (`python -m agent.telegram_bridge`) is already a permanently
running process. `python-telegram-bot` ships a `JobQueue` built for
exactly this: recurring jobs registered on the running `Application`.
The digest jobs belong inside the process that is already always up.

## Decision

Replace the launchd trigger with **in-process scheduling** via PTB's
`JobQueue`. Only the *trigger* changes — ADR 0002's proactive-push
*modality* is unchanged, and so is every line of digest *logic*.

1. **Two `JobQueue` jobs registered at startup.**
   `agent/telegram_bridge.py:register_digest_jobs` registers
   `digest-daily` (every day, 06:00 local) and `digest-weekly` (Sundays
   only, 06:00 local) on `application.job_queue`. `build_application`
   calls it, so simply starting the bot arms both jobs — there is no
   separate install step.

2. **The callbacks call the existing digest functions.** Each job
   callback dispatches to `run_daily_digest` / `run_weekly_digest` from
   `agent/digest.py` — the same functions launchd's CLI path invoked.
   The digest logic, the `claude -p` invocation, and
   `send_telegram_message` are untouched. No logic is duplicated or
   reimplemented in the bridge.

3. **The CLI entrypoint stays.** `python -m agent.digest --mode=daily|weekly`
   remains as the manual smoke-test path. It is no longer the production
   trigger, but it is the cheapest way to exercise a digest end-to-end
   on demand without waiting for 06:00.

4. **Explicit local timezone.** The 06:00 schedules use a tz-aware
   `datetime.time` resolved from the host's local zone
   (`datetime.now().astimezone().tzinfo`). A naive time would be
   interpreted by PTB as the bot's default timezone (UTC) and fire at
   the wrong wall-clock hour. The host's zone is read at startup rather
   than hardcoded, so the digest tracks the machine.

5. **Blocking runs offloaded to a thread.** `run_daily_digest` /
   `run_weekly_digest` are synchronous (they spawn `claude -p` and POST
   over `urllib`). The job callbacks wrap them in `asyncio.to_thread` so
   the digest generation does not block the bot's polling loop.

6. **A failed digest does not kill the bot.** The job callback catches
   and logs any exception from the run function. A broken digest is
   visible in the bot log but the polling loop keeps serving replies —
   the digest's liveness is coupled to the bot's, but its *failures* are
   not.

7. **The `[job-queue]` extra becomes a hard dependency.** PTB's
   `JobQueue` requires APScheduler, pulled via the
   `python-telegram-bot[job-queue]` extra. `pyproject.toml` now declares
   the extra on the base dependency (not as optional) — without it
   `application.job_queue` is `None` and `build_application` raises.

## Alternatives considered

### A. Keep launchd

Rejected — this ADR exists because launchd's one unique advantage
(missed-run-on-wake) is moot on an always-on host, leaving only its
costs (absolute-path plists, a separate liveness story, `launchctl`
ceremony). ADR 0002 chose it before the host was confirmed always-on.

### B. cron

Rejected for the same reason as launchd, and ADR 0002 already rejected
cron on its own merits. Any OS-level scheduler is now unnecessary
complexity.

### C. A bare `asyncio` timer instead of PTB's `JobQueue`

Rejected. We could hand-roll an `asyncio.sleep`-until-06:00 loop, but
`JobQueue` is purpose-built: it handles the day-of-week filtering, DST
transitions (via APScheduler's cron trigger), and job naming/inspection
we lean on in tests. Hand-rolling re-implements a solved problem.

## Consequences

### Positive

- Zero OS configuration. No plists, no `launchctl load`, no README of
  install steps, no absolute machine paths to hand-edit if the repo or
  venv moves. The trigger lives in version-controlled Python.
- One process to reason about. The bridge polls *and* schedules; there
  is no second process whose independent liveness must be monitored.
- The digest jobs are inspectable in tests without a live Telegram
  connection or a real `claude -p` subprocess — tests assert on the
  registered jobs' schedules and callbacks (see
  `tests/test_telegram_bridge.py`).
- ADR 0002's digest logic, push primitive, and CLI entrypoint are all
  untouched — the existing `tests/test_digest.py` digest-logic tests
  stay green. The change surface is the trigger only.

### Negative

- **The digest does not fire if the bot process is down.** launchd would
  have run the job independently of the bot's health. This is the
  accepted tradeoff: on a single-user, always-on box the bot being down
  is itself the bigger problem (no replies either), and a missed digest
  is a minor, self-correcting loss. If the bot is up, the digest fires;
  if it is down, the missing morning message is a visible signal that
  something needs attention.
- `python-telegram-bot[job-queue]` (and so APScheduler + tzlocal) is now
  a hard runtime dependency. The bridge raises at startup if the extra
  is missing rather than silently skipping the digest.

### Neutral

- `infra/launchd/` and its two plists + README are deleted. The
  directory is removed if nothing else uses it.
- The digest still writes nothing to `_audit/` — as ADR 0002 noted,
  there is no audit entry for a digest run. The bot log is the run
  record. That remains a deliberate follow-up decision, not changed here.
- ADR 0002 keeps its "Accepted" status with a one-line supersession note
  at the top; ADRs are immutable except for supersession links.
