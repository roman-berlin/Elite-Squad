#!/bin/zsh
# The General — refresh & run the War Room in your browser.
# Double-click any time: it stops the old cockpit, starts serve on the latest code,
# waits until it's actually listening, then opens your browser. Ctrl-C in this window stops it.

REPO="$HOME/Projects/General"
PORT=8787
URL="http://localhost:$PORT"

# GUI-launched scripts get a minimal PATH — add the usual tool locations.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.bun/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

cd "$REPO" || { echo "General repo not found at ~/Projects/General"; exit 1; }
[[ -f .venv/bin/activate ]] && source .venv/bin/activate
[[ -f .env ]] && { set -a; source .env; set +a; }

echo "★ Refreshing the War Room…"

# 1) Stop any cockpit already on the port (this is the "refresh")
PIDS=$(lsof -ti tcp:$PORT 2>/dev/null)
if [[ -n "$PIDS" ]]; then
  echo "  stopping old cockpit (pid: $PIDS)"
  kill $PIDS 2>/dev/null; sleep 1
  PIDS=$(lsof -ti tcp:$PORT 2>/dev/null)
  [[ -n "$PIDS" ]] && kill -9 $PIDS 2>/dev/null
fi

# 2) Quick pre-flight (health is also shown inside the dashboard)
echo
./general doctor
echo

# 3) Open the browser as soon as the server is actually listening (up to ~30s)
( for i in {1..60}; do
    curl -s -o /dev/null "$URL" 2>/dev/null && { open "$URL"; break; }
    sleep 0.5
  done ) &

# 4) Run the cockpit in the foreground (output here; Ctrl-C stops it)
echo "★ Cockpit → $URL   (Ctrl-C to stop)"
exec ./general serve
