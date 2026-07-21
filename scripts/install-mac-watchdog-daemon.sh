#!/bin/bash
# Elite Unit — install (or uninstall) the Mac launchd OUT-OF-PROCESS WATCHDOG (EU-403).
#
# Why: every heartbeat lives inside the serve process. A wedged-but-alive process (deadlock,
# hung HTTP, stuck event loop) or a host that slept is invisible until a human opens the
# cockpit — the 2026-07-20 16:23->23:05 gap (host down 6.7h) produced zero alerts. This agent
# runs scripts/watchdog.sh as a SEPARATE periodic launchd job, completely outside the serve
# process, so it cannot share its failure modes. The watchdog curls the cockpit health
# endpoint and checks audit.jsonl freshness; on 2 consecutive failures it pages via Telegram.
#
# It is deliberately a PERIODIC TIMER (StartInterval), NOT KeepAlive: the job runs the short
# watchdog script every ~4 min and exits. KeepAlive would respawn it on every exit (a busy
# loop); StartInterval fires it on the cadence and leaves it otherwise idle.
#
# Cadence: 240s. The "alert within 10 min" contract needs 2 consecutive failures, and with
# launchd's per-fire jitter + curl's --max-time on a hung serve, a 300s interval can land the
# 2nd failure just past 10 min. 240s keeps worst-case 2-poll latency comfortably under 10 min.
#
# Pairs with scripts/install-mac-cockpit-daemon.sh (the keepalive that restarts the cockpit on
# exit) — that one keeps the cockpit ALIVE; this one watches that it is actually making
# progress. They share nothing: the watchdog is pure shell+curl, so a cockpit deadlock cannot
# blind it.
#
# Usage:
#   bash scripts/install-mac-watchdog-daemon.sh            # install (default) — runs a check now
#   bash scripts/install-mac-watchdog-daemon.sh uninstall  # stop + remove
#
# The plist label is:  com.roman.general.watchdog
# Log output goes to:  ~/Library/Logs/General/watchdog.log
set -euo pipefail

ACTION="${1:-install}"
LABEL="com.roman.general.watchdog"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/General"
LOG_FILE="$LOG_DIR/watchdog.log"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WATCHDOG="$HERE/scripts/watchdog.sh"

# ── Uninstall path ──────────────────────────────────────────────────────────
if [[ "$ACTION" == "uninstall" ]]; then
  if [[ -f "$PLIST" ]]; then
    launchctl bootout gui/$(id -u)/"$LABEL" 2>/dev/null || true
    launchctl unload "$PLIST" 2>/dev/null || true   # legacy fallback for very old macOS
    rm -f "$PLIST"
    echo "Removed $PLIST"
  else
    echo "Nothing to uninstall — $PLIST not found."
  fi
  exit 0
fi

# ── Install path ────────────────────────────────────────────────────────────
mkdir -p "$PLIST_DIR" "$LOG_DIR"

# Bootout an existing copy first so launchctl sees the refreshed plist.
if [[ -f "$PLIST" ]]; then
  launchctl bootout gui/$(id -u)/"$LABEL" 2>/dev/null || true
  launchctl unload "$PLIST" 2>/dev/null || true
fi

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- Unique reverse-DNS label — distinct from the cockpit + autopilot keepalives. -->
    <key>Label</key>
    <string>${LABEL}</string>

    <!-- PERIODIC TIMER: launchd runs the short watchdog script every StartInterval seconds,
         not a KeepAlive respawn. 240s keeps the 2-consecutive-failure alert inside 10 min
         even with launchd jitter + curl's hang timeout on a STOP'd serve. -->
    <key>StartInterval</key>
    <integer>240</integer>
    <key>RunAtLoad</key>
    <true/>

    <!-- A CURATED static PATH (mirrors install-mac-cockpit-daemon.sh): launchd agents get a
         minimal PATH with no shell profile, so /usr/bin/curl + /bin/bash must resolve. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:\${HOME}/.bun/bin:\${HOME}/bin</string>
        <!-- The watchdog derives everything else from GENERAL_DIR (the repo root below). -->
        <key>GENERAL_DIR</key>
        <string>${HERE}</string>
    </dict>

    <!-- WorkingDirectory = repo root, so state/ + .env resolve exactly as the cockpit sees them. -->
    <key>WorkingDirectory</key>
    <string>${HERE}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${WATCHDOG}</string>
    </array>

    <!-- The watchdog's own log (one line per run; alerts go to Telegram). -->
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
</dict>
</plist>
PLIST_EOF

# Bootstrap the agent — RunAtLoad runs the first check immediately.
launchctl bootstrap gui/$(id -u) "$PLIST"
launchctl load "$PLIST" 2>/dev/null || true   # legacy fallback for very old macOS

echo "Installed + started: $LABEL"
echo "  cadence:   every 240s (alert after 2 consecutive failures)"
echo "  script:    $WATCHDOG"
echo "  log:       $LOG_FILE"
echo "  state:     $HERE/state/watchdog_state"
echo "  stop:      launchctl bootout gui/\$(id -u)/$LABEL"
echo ""
echo "  QA:  launchctl stop com.roman.general.cockpit  (or kill -STOP <serve pid>)"
echo "       -> expect a 'cockpit unreachable or wedged' Telegram alert within ~8 min."
