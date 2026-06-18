#!/bin/zsh
# Corridor small-talk — a couple of spontaneous officer exchanges per day (launchd/cron).
# A random 0-35 min jitter is added so it never feels clockwork. Dispatches to ./general,
# which activates the venv + loads .env itself. When the unit moves to the VPS, the same
# command runs from cron there (see Documentation/VPS_Deployment.md).
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null

cd "$HOME/Projects/General" || exit 1
mkdir -p council
sleep $((RANDOM % 2100))     # 0-35 min jitter — spontaneous, not on the hour
echo "----- smalltalk run $(date) -----" >> council/cron.log
exec ./general smalltalk >> council/cron.log 2>&1
