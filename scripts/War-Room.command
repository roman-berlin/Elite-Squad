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
export GENERAL_HOST_ID="${GENERAL_HOST_ID:-mac}"   # labels this cockpit "mac" in the header
export GENERAL_COCKPIT_PROMOTE=1                    # this cockpit may Deploy DEV -> main (the Mac has push access)

echo "★ Refreshing the War Room…"

# 1) Stop any cockpit already on the port (this is the "refresh")
PIDS=$(lsof -ti tcp:$PORT 2>/dev/null)
if [[ -n "$PIDS" ]]; then
  echo "  stopping old cockpit (pid: $PIDS)"
  kill $PIDS 2>/dev/null; sleep 1
  PIDS=$(lsof -ti tcp:$PORT 2>/dev/null)
  [[ -n "$PIDS" ]] && kill -9 $PIDS 2>/dev/null
fi

# 1b) Close EVERY other War Room terminal window. First kill what those windows are running — old
#     `caffeinate -i ./general serve` awake-holds and older runs of this script (never $$ = us) — so they
#     go idle ([Process completed]) and close without a "terminate process?" prompt. Then tell Terminal to
#     close the now-idle windows, recognised by the cockpit banner in their scrollback (NOT a tag), so it
#     catches the whole pile — even windows opened before this existed. THIS window is skipped by tty AND
#     by the busy guard. (First run asks once: System Settings > Privacy & Security > Automation > Terminal.)
MY_TTY=$(tty 2>/dev/null)
[[ "$MY_TTY" == /dev/* ]] || MY_TTY="/dev/$(ps -o tty= -p $$ | tr -d ' ')"
old_pids=()
for pid in \
    $(pgrep -fx "caffeinate -i" 2>/dev/null) \
    $(pgrep -f "caffeinate -i .*general serve" 2>/dev/null) \
    $(pgrep -f "zsh .*War.Room\.command" 2>/dev/null); do
  [[ "$pid" == "$$" ]] && continue
  old_pids+=("$pid")
done
if (( ${#old_pids[@]} )); then
  echo "  stopping ${#old_pids[@]} old War Room process(es)"
  kill "${old_pids[@]}" 2>/dev/null
  sleep 1
fi
/usr/bin/osascript - "$MY_TTY" >/dev/null 2>&1 <<OSA || echo "  ⚠ couldn't close old Terminal windows (allow: System Settings → Privacy & Security → Automation → Terminal)"
on run argv
  set myTTY to item 1 of argv
  tell application "Terminal"
    set wids to id of every window
    repeat with wid in wids
      try
        set w to first window whose id is (contents of wid)
        set t to selected tab of w
        if (tty of t) is not myTTY and (busy of t) is false then
          set htxt to history of t
          if (htxt contains "localhost:$PORT") or (htxt contains "War Room") or (htxt contains "general serve") then
            close w saving no
          end if
        end if
      end try
    end repeat
  end tell
end run
OSA

# 2) Quick pre-flight (health is also shown inside the dashboard)
echo
./general doctor
echo

# 3) Close stale War Room browser tab(s) in every browser we can find, then open ONE fresh tab once the
#    server answers. Only installed AND running browsers are touched (the install check also keeps
#    AppleScript from failing to compile against an absent app's dictionary). Per-browser LITERAL app
#    names — the old closeIn() used `tell application <variable>`, which compiles but mis-resolves the
#    tab/URL terminology at runtime, so with stderr silenced it never actually closed a tab.
#    (First run prompts once per browser: System Settings > Privacy & Security > Automation > Terminal.)
close_cockpit_tabs() {
  local app
  for app in "Safari" "Google Chrome" "Brave Browser" "Microsoft Edge" "Arc"; do
    [[ -d "/Applications/$app.app" || -d "$HOME/Applications/$app.app" ]] || continue
    /usr/bin/osascript >/dev/null 2>&1 <<OSA || echo "  ⚠ couldn't tidy $app tabs (allow: System Settings → Privacy & Security → Automation → Terminal → $app)"
tell application "System Events"
  if not (exists process "$app") then return
end tell
tell application "$app"
  repeat with w in windows
    try
      set k to count of tabs of w
      repeat while k > 0
        try
          set u to URL of tab k of w
          if (u contains "localhost:$PORT") or (u contains "127.0.0.1:$PORT") then close tab k of w
        end try
        set k to k - 1
      end repeat
    end try
  end repeat
end tell
OSA
  done
}
close_cockpit_tabs
( for i in {1..60}; do
    curl -s -o /dev/null "$URL" 2>/dev/null && { open "$URL"; break; }
    sleep 0.5
  done ) &

# 4) Run the cockpit in the foreground, under caffeinate so the Mac doesn't idle-sleep
#    while it's open — that keeps the scheduled 10:00 council alive. (Output here; Ctrl-C stops.)
echo "★ Cockpit → $URL   (Ctrl-C to stop; Mac stays awake while this is open)"
exec caffeinate -i ./general serve
