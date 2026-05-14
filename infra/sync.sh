#!/bin/zsh
# Keep this host-only Mac mini's checkout in sync with origin/main.
#
# Invoked every 5 min by the launchd job
# com.jasonchi.personal-assistant-sync. Dev happens on another machine;
# this box only hosts the bot. Flow:
#   fetch -> if behind: pull --ff-only -> reinstall deps if pyproject
#   changed -> run pytest -> restart the bot ONLY if tests are green.
#
# A failing test suite leaves the *running* bot process untouched (it
# keeps serving the old code); the bad commit just sits on disk until a
# later good push supersedes it. We deliberately do not roll back —
# a reset would re-pull and re-test the same bad commit every 5 min.
set -euo pipefail

REPO="/Users/jasonchi/Documents/Coding/personal-assistant-agentic"
BOT_LABEL="com.jasonchi.personal-assistant"
cd "$REPO"

git fetch --quiet origin main

local_rev=$(git rev-parse @)
remote_rev=$(git rev-parse origin/main)

if [ "$local_rev" = "$remote_rev" ]; then
  exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') behind: ${local_rev:0:9} -> ${remote_rev:0:9}"

# Note whether dependencies changed before we move HEAD.
pyproject_changed=0
if ! git diff --quiet "$local_rev" "$remote_rev" -- pyproject.toml; then
  pyproject_changed=1
fi

git pull --ff-only --quiet origin main

if [ "$pyproject_changed" -eq 1 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') pyproject.toml changed — reinstalling deps"
  .venv/bin/pip install -q -e ".[dev]"
fi

if ! .venv/bin/python -m pytest -q; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') pytest FAILED at ${remote_rev:0:9} — bot NOT restarted"
  exit 1
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') tests green — restarting $BOT_LABEL"
launchctl kickstart -k "gui/$(id -u)/${BOT_LABEL}"
echo "$(date '+%Y-%m-%d %H:%M:%S') synced to ${remote_rev:0:9}"
