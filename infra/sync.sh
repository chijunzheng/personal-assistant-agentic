#!/bin/zsh
# Keep this host-only Mac mini's checkout in sync with origin/main.
#
# Invoked every 5 min by the launchd job
# com.jasonchi.personal-assistant-sync. Dev happens on another machine;
# this box only hosts the bot. Each tick:
#   fetch -> if behind: pull --ff-only + reinstall deps -> run pytest;
#   if red, attempt one self-heal reinstall + retry -> restart the bot
#   whenever we pulled OR the self-heal recovered the venv.
#
# A failing test suite (after self-heal) leaves the *running* bot
# process untouched (it keeps serving the old code); the bad commit
# just sits on disk until a later good push supersedes it. We do not
# roll back — a reset would re-pull and re-test the same bad commit
# every 5 min.
set -euo pipefail

# Self-locating: REPO is the parent of this script's infra/ dir, so the
# script keeps working regardless of where the checkout lives.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
BOT_LABEL="com.jasonchi.personal-assistant"
cd "$REPO"

git fetch --quiet origin main

local_rev=$(git rev-parse @)
remote_rev=$(git rev-parse origin/main)

needs_restart=0
if [ "$local_rev" != "$remote_rev" ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') behind: ${local_rev:0:9} -> ${remote_rev:0:9}"
  git pull --ff-only --quiet origin main
  # Always reinstall on pull. The previous gating on `git diff --
  # pyproject.toml` missed venv drift from transitively-pulled extras
  # (python-telegram-bot[job-queue] → APScheduler), since adding an
  # extra to an existing dep declaration doesn't always change
  # pyproject's hash. Idempotent reinstall is ~1.5s of overhead per
  # pull and removes that whole failure class.
  .venv/bin/pip install -q -e ".[dev]"
  needs_restart=1
fi

if ! .venv/bin/python -m pytest -q; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') pytest red — attempting self-heal reinstall"
  .venv/bin/pip install -q -e ".[dev]"
  if ! .venv/bin/python -m pytest -q; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') pytest STILL red after reinstall — bot NOT restarted"
    exit 1
  fi
  echo "$(date '+%Y-%m-%d %H:%M:%S') self-heal recovered the venv"
  needs_restart=1
fi

if [ "$needs_restart" -eq 1 ]; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') tests green — restarting $BOT_LABEL"
  launchctl kickstart -k "gui/$(id -u)/${BOT_LABEL}"
  echo "$(date '+%Y-%m-%d %H:%M:%S') synced to ${remote_rev:0:9}"
fi
