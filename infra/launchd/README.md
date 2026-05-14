# launchd jobs

macOS `launchd` agents that run the assistant's proactive (push) jobs.
The Telegram *reply* path runs separately as a long-lived polling process
(`python -m agent.telegram_bridge`); the jobs here are scheduled, one-shot,
and exit when done.

## Jobs

| Plist | Schedule | Entrypoint |
|---|---|---|
| `com.jason.personal-assistant.digest-daily.plist` | 06:00 daily | `python -m agent.digest --mode=daily` |
| `com.jason.personal-assistant.digest-weekly.plist` | 06:00 Sundays | `python -m agent.digest --mode=weekly` |

The daily digest is a read-only morning push. The weekly digest is a
Sunday reflection turn: the agent Writes a
`journal/YYYY-MM-DD-weekly-reflection.md` draft into the vault and sends a
short Telegram nudge pointing at it. Both reuse the same proactive-push
modality — see `docs/adr/0002-proactive-digest-modality.md`.

## Install / load

`launchd` agents live in `~/Library/LaunchAgents/`. The plists in this
directory are the source of truth — copy them in, then `load`. Set
`PLIST` to whichever job you're installing:

```sh
PLIST=com.jason.personal-assistant.digest-daily.plist
# or, for the weekly reflection job:
PLIST=com.jason.personal-assistant.digest-weekly.plist

# unload an old copy first if one is already loaded (no-op otherwise):
launchctl unload ~/Library/LaunchAgents/$PLIST 2>/dev/null

cp infra/launchd/$PLIST ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/$PLIST
```

Re-run the same three lines after editing a plist — `launchd` only picks
up changes on reload. Install both jobs to run both digests.

## Smoke test (don't wait for the schedule)

```sh
# daily:
launchctl start com.jason.personal-assistant.digest-daily
tail -f logs/digest-daily.err.log logs/digest-daily.out.log

# weekly (don't wait for Sunday 6am):
launchctl start com.jason.personal-assistant.digest-weekly
tail -f logs/digest-weekly.err.log logs/digest-weekly.out.log
```

`launchctl start` runs the job immediately, ignoring the schedule. A
successful daily run pushes the digest to Telegram and exits 0; a
successful weekly run writes the reflection draft, pushes the nudge, and
exits 0. A failure (missing env, `claude -p` error, network error) exits
non-zero and the traceback lands in the job's `.err.log`.

## Uninstall / unload

```sh
# daily:
launchctl unload ~/Library/LaunchAgents/com.jason.personal-assistant.digest-daily.plist
rm ~/Library/LaunchAgents/com.jason.personal-assistant.digest-daily.plist

# weekly:
launchctl unload ~/Library/LaunchAgents/com.jason.personal-assistant.digest-weekly.plist
rm ~/Library/LaunchAgents/com.jason.personal-assistant.digest-weekly.plist
```

## Troubleshooting

- **Nothing happens / no log output.** Check the job is loaded:
  `launchctl list | grep personal-assistant`. The exit code of the last
  run is the first column.
- **`ModuleNotFoundError: agent`.** `WorkingDirectory` in the plist must
  be the repo root and `ProgramArguments[0]` must point at the repo's
  `.venv/bin/python`. If the repo or venv moved, edit the plist and
  reload.
- **`DigestError: TELEGRAM_CHAT_ID is not set`.** The entrypoint loads
  `.env` from `WorkingDirectory`. Confirm `.env` has `TELEGRAM_CHAT_ID`,
  `TELEGRAM_BOT_TOKEN`, and `VAULT_ROOT`.
- **Paths with spaces.** The repo path contains spaces; the plist quotes
  them correctly as separate `<string>` elements. Don't collapse them.
