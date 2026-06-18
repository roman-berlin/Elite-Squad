#!/bin/zsh
# Headless patrol — for launchd. Scout + Provost + Quartermaster sweep DEV and file findings.
# Ensures the `claude` CLI + node/bun are on PATH (launchd's PATH is minimal), then dispatches to
# ./general, which activates the venv and loads .env itself.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:$HOME/.bun/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
[ -f "$HOME/.zprofile" ] && source "$HOME/.zprofile" 2>/dev/null

cd "$HOME/Projects/General" || exit 1
mkdir -p patrol
echo "----- patrol run $(date) -----" >> patrol/cron.log
exec ./general patrol automatixy >> patrol/cron.log 2>&1
