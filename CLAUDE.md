# personal-assistant-agentic — Project Instructions for Claude Code

You are working on Jason's agentic personal assistant: Telegram input on one end, `claude -p` with vault-scoped Read/Glob/Grep/Edit/Write tools on the other.

## The cardinal rule

> **No classifier. No retrieval module. No domain plugins. The agent picks what to read and where to write.**

The architecture has three layers:

```
+---------- telegram_bridge ---------+
|  polling loop, one message per turn |
+-------------------------------------+
                 |
+---------- agent/runner.py ----------+
|  spawn claude -p, parse JSON envelope,
|  mirror tool_use events to audit log
+-------------------------------------+
                 |
+---------- prompts/system.md --------+
|  the only contract Claude follows:
|  vault layout, naming, mtime buffer,
|  idempotency on jsonl event logs
+-------------------------------------+
```

If you find yourself adding a `domains/` directory, an `if intent == ...` branch, or a "retrieval" module, **stop.** The whole point of v2 is that the agent decides. The conventions live in `prompts/system.md`.

## What this repo does NOT have (intentional)

- `kernel/classifier.py` — the LLM doesn't need a classifier
- `kernel/retrieval.py` — the LLM has Glob/Grep
- `domains/*/handler.py` + `domain.yaml` — the LLM picks files via tool calls
- `configs/default.yaml` + `configs/baseline.yaml` — no 8 Booleans in the agentic model
- `kernel/index.py` (INDEX.md refresher) — Glob is fast enough; revisit if it isn't

If a feature seems to need any of the above, write it in `prompts/system.md` as a convention instead.

## Files

| Path | Owner | Edit freely? |
|---|---|---|
| `agent/runner.py` | runtime | yes — but every change needs a runner test |
| `agent/audit.py` | ported from v1 | rarely — schema is load-bearing |
| `agent/vault.py` | ported from v1 | rarely — atomic-write contract |
| `agent/session.py` | ported from v1 | yes for session shape changes |
| `agent/chat_log.py` | ported from v1 | yes for turn-log shape changes |
| `agent/telegram_bridge.py` | ported from v1 | rarely — thin wrapper |
| `prompts/system.md` | **the LLM's contract** | yes, but treat as a public API — eval before merging |
| `eval/` | head-to-head harness | yes |

## Quality gates

1. **Test the runner.** Any change to `agent/runner.py` argv assembly or response parsing gets a test in `tests/test_runner.py`.
2. **Audit must populate.** A smoke test (or live turn) should produce a new `_audit/YYYY-MM-DD.jsonl` entry per tool call.
3. **Idempotency on `.jsonl` writes.** The system prompt tells Claude to use a content-hash id for event-log appends. Don't ship a system-prompt change that drops that rule.
4. **30-min user-edit buffer.** `agent/vault.py:atomic_write` stages mid-edit overwrites. The system prompt tells Claude to use Edit (not Write) on narrative markdown when possible.
5. **No mutation, small files (<400 lines), no console.log, no hardcoded secrets** — same coding-style rules as everywhere else.

## When kernel changes ARE appropriate

Most feature requests are answered by editing `prompts/system.md`, not the runner. Kernel changes are warranted for:

- New input modality (voice, image) — telegram_bridge change
- Observability wiring (langfuse, otel) — runner change
- New tool exposure (e.g. WebFetch) — runner argv + system-prompt change in sync
- Audit schema migration — audit.py change, eval re-run

For these, write a short ADR in `docs/adr/` before editing.

## TL;DR

The agent decides. The repo's job is to constrain *where* it can read/write (vault only), enforce *how* it writes (atomic, mtime-buffered, audited), and document *what* the vault expects (`prompts/system.md`).
