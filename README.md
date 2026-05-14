# personal-assistant-agentic

A scoped Claude Code agent over a personal Obsidian/iCloud vault. Telegram input on one end; `claude -p` with vault-restricted Read/Glob/Grep/Edit/Write on the other.

## The shape

```
Telegram message
     |
     v
agent/telegram_bridge.py   (polling)
     |
     v
agent/runner.py            (one claude -p subprocess per turn)
     |
     |  --add-dir <VAULT_ROOT>
     |  --allowed-tools Read Glob Grep Edit Write
     |  --append-system-prompt prompts/system.md
     v
claude -p          <----+
     |                  |
     v                  |
Read / Glob / Grep / Edit / Write (scoped to vault)
     |                  |
     +------------------+
     |
     v
reply text + tool_use trail
     |
     v
agent/audit.py             (mirror tool_use events into _audit/YYYY-MM-DD.jsonl)
     |
     v
Telegram reply
```

No classifier. No retrieval module. No domain plugins. Claude picks what to read and where to write, constrained by the conventions in `prompts/system.md`.

## What ships with the repo

| Path | Purpose |
|---|---|
| `agent/runner.py` | Invoke `claude -p` with vault-scoped tools, capture audit trail |
| `agent/audit.py` | Append-only JSONL audit log (ported from v1) |
| `agent/vault.py` | `atomic_write` + 30-min user-edit buffer (ported from v1) |
| `agent/session.py` | Active session frontmatter file (ported from v1) |
| `agent/chat_log.py` | Verbatim turn log for referential follow-ups (ported from v1) |
| `agent/telegram_bridge.py` | python-telegram-bot polling loop (ported from v1) |
| `prompts/system.md` | The contract Claude reads every turn (vault layout, naming, mtime buffer rule, idempotency rule) |
| `eval/` | Head-to-head harness (placeholder; methodology rebuilt for agentic) |

## Quickstart

```bash
git clone <this-repo>
cd personal-assistant-agentic
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env       # then fill in TELEGRAM_BOT_TOKEN, VAULT_ROOT
pytest                     # smoke tests
python -m agent.telegram_bridge
```

## Relation to v1 (`personal-assistant`)

v1 is the deterministic kernel + plugin recipe (8 engineering Booleans, classifier, per-domain `handler.py`). v1 still works and is not archived.

v2 is the same problem with the LLM-as-agent primitive. The trade-off: v1 wins on token cost and latency per turn; v2 wins on flexibility (any domain works without registering a plugin) and complexity (~1k LOC vs ~5k LOC).

The portfolio writeup compares them side by side.
