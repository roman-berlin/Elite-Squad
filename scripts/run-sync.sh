#!/bin/zsh
# Mac <-> server state sync — publish this machine's audit log and pull the other machine's, so the
# server's cockpit + councils reflect what the Mac ships (and vice-versa). Runs every ~15 min via
# launchd (com.roman.general.sync.plist); harmless to invoke by hand after a build too. Dispatches to
# ./general, which activates the venv + loads .env. The VPS runs the same `general sync` from cron
# (see the VPS runbook). Single-writer shared/<host>.jsonl on an orphan `unit-state` branch — no
# merge conflicts, best-effort (a git hiccup just no-ops; the cockpit still works from local audit).
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null
export GENERAL_HOST_ID="mac"     # clean, stable name for shared/mac.jsonl (the server sets "server")
export GENERAL_SERVER_SSH="ubuntu@151.145.91.229"   # pull the server's living log down over SSH (server->Mac)

cd "$HOME/Projects/General" || exit 1
mkdir -p council
echo "----- sync run $(date) -----" >> council/cron.log
exec ./general sync >> council/cron.log 2>&1
