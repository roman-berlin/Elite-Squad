#!/bin/zsh
# The General — refresh the War Room cockpit.
# Stops any running cockpit on :8787, re-runs the health check, and relaunches
# serve so it picks up the latest code (Flask does not hot-reload). Double-click it,
# or run it from a Terminal.
#
# Canonical copy lives here in the repo. Install/refresh the Desktop shortcut with:
#   cp ~/Projects/General/scripts/Refresh-General.command ~/Desktop/Refresh-General.command
#   chmod +x ~/Desktop/Refresh-General.command

set -e
PORT=8787

cd "$HOME/Projects/General" || { echo "General repo not found at ~/Projects/General"; exit 1; }

# Match what ./general does: activate the venv, load secrets from .env
[[ -f .venv/bin/activate ]] && source .venv/bin/activate
[[ -f .env ]] && { set -a; source .env; set +a; }

echo "★ Refreshing the War Room…"

# 1) Stop any cockpit already bound to the port
PIDS=$(lsof -ti tcp:$PORT 2>/dev/null || true)
if [[ -n "$PIDS" ]]; then
  echo "  stopping old cockpit (pid: $PIDS)"
  kill $PIDS 2>/dev/null || true
  sleep 1
  PIDS=$(lsof -ti tcp:$PORT 2>/dev/null || true)
  [[ -n "$PIDS" ]] && { echo "  forcing stop"; kill -9 $PIDS 2>/dev/null || true; }
else
  echo "  no cockpit running — fresh start"
fi

# 2) Preflight health check
echo
./general doctor
echo

# 3) Relaunch the cockpit and open it like an app (chromeless window if Chrome/Edge is present)
open_app() {
  local url="http://localhost:$PORT"
  if [[ -d "/Applications/Google Chrome.app" ]]; then
    open -na "Google Chrome" --args --app="$url" --new-window
  elif [[ -d "/Applications/Microsoft Edge.app" ]]; then
    open -na "Microsoft Edge" --args --app="$url" --new-window
  else
    open "$url"
  fi
}

echo "★ Launching cockpit → http://localhost:$PORT   (Ctrl-C to stop)"
( sleep 2; open_app ) &
exec ./general serve
