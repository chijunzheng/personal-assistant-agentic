# launchd jobs

macOS `launchd` agents that run the assistant's proactive (push) jobs.
The Telegram *reply* path runs separately as a long-lived polling process
(`python -m agent.telegram_bridge`); the jobs here are scheduled, one-shot,
and exit when done.

## Jobs

| Plist | Schedule | Entrypoint |
|---|---|---|
| `com.jason.personal-assistant.digest-daily.plist` | 06:00 daily | `python -m agent.digest --mode=daily` |

## Install / load

`launchd` agents live in `~/Library/LaunchAgents/`. The plists in this
directory are the source of truth — copy them in, then `load`:

```sh
PLIST=com.jason.personal-assistant.digest-daily.plist

# unload an old copy first if one is already loaded (no-op otherwise):
launchctl unload ~/Library/LaunchAgents/$PLIST 2>/dev/null

cp infra/launchd/$PLIST ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/$PLIST
```

Re-run the same three lines after editing a plist — `launchd` only picks
up changes on reload.

## Smoke test (don't wait for 6am)

```sh
launchctl start com.jason.personal-assistant.digest-daily
tail -f logs/digest-daily.err.log logs/digest-daily.out.log
```

`launchctl start` runs the job immediately, ignoring the schedule. A
successful run pushes the digest to Telegram and exits 0; a failure
(missing env, `claude -p` error, network error) exits non-zero and the
traceback lands in `logs/digest-daily.err.log`.

## Uninstall / unload

```sh
launchctl unload ~/Library/LaunchAgents/com.jason.personal-assistant.digest-daily.plist
rm ~/Library/LaunchAgents/com.jason.personal-assistant.digest-daily.plist
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
