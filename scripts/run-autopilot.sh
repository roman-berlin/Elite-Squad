#!/bin/zsh
# Always-on autopilot — for launchd (KeepAlive). Resumes In Progress, else takes the top
# To Do (yours) -> builds -> reviews -> lands on DEV -> moves to QA, continuously.
# Ensures the `claude` CLI + node/bun are on PATH (launchd's PATH is minimal), then runs
# ./general, which activates the venv and loads .env itself.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null

cd "$HOME/Projects/General" || exit 1
mkdir -p council
echo "----- autopilot start $(date) -----" >> council/autopilot.log
exec ./general --live autopilot automatixy >> council/autopilot.log 2>&1
