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

# 3) Close any stale War Room tab(s), then open ONE fresh tab once the server is listening.
#    (First run prompts once to allow controlling the browser: System Settings > Privacy > Automation.)
close_old_tabs() {
  /usr/bin/osascript >/dev/null 2>&1 <<'OSA'
on closeIn(appName)
  tell application "System Events"
    if not (exists (processes whose name is appName)) then return
  end tell
  tell application appName
    repeat with w in windows
      try
        set k to (count of tabs of w)
        repeat while k > 0
          if (URL of tab k of w) contains "localhost:8787" then close tab k of w
          set k to k - 1
        end repeat
      end try
    end repeat
  end tell
end closeIn
closeIn("Google Chrome")
closeIn("Safari")
OSA
}
( for i in {1..60}; do
    curl -s -o /dev/null "$URL" 2>/dev/null && { close_old_tabs; open "$URL"; break; }
    sleep 0.5
  done ) &

# 4) Run the cockpit in the foreground, under caffeinate so the Mac doesn't idle-sleep
#    while it's open — that keeps the scheduled 10:00 council alive. (Output here; Ctrl-C stops.)
echo "★ Cockpit → $URL   (Ctrl-C to stop; Mac stays awake while this is open)"
exec caffeinate -i ./general serve
