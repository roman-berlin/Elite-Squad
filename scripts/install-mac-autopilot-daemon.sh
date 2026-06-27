#!/bin/bash
# Elite Unit — install (or uninstall) the Mac launchd keepalive daemon for autopilot.
#
# This agent is ALWAYS-ON. Because KeepAlive=true, `launchctl load` (run at the end of install)
# STARTS LIVE autopilot immediately and launchd then restarts it whenever it exits — so installing
# the agent starts a live `./general --live autopilot <app>` worker right away, and there is NO
# separate manual start step. (RunAtLoad=true is set to agree with that; with KeepAlive=true the job
# would start at load regardless.) This decouples the worker from the foreground `./general serve`
# viewer, so closing the War Room terminal can no longer stop development.
#
# Stopping it: because KeepAlive=true, a plain `launchctl stop` is IMMEDIATELY respawned by
# launchd, so it does NOT durably stop the daemon. To make it stay down, UNLOAD the agent
# (`launchctl unload "$PLIST"`) or run the `uninstall` action below (which unloads + removes it),
# then re-run this installer to bring it back. The autopilot still stops gracefully on the SIGTERM
# that unload sends — it finishes the in-flight ticket, removes its PID file, and exits cleanly
# (see the SIGTERM handler in orchestrator/autopilot.py).
#
# Usage:
#   bash scripts/install-mac-autopilot-daemon.sh <app>           # install (default)
#   bash scripts/install-mac-autopilot-daemon.sh <app> uninstall # stop + remove
#
# <app> is the application name passed to `./general --live autopilot <app>`.
#
# The plist label is:  com.roman.general.autopilot-keepalive
# Log output goes to:  ~/Library/Logs/General/autopilot-keepalive.log
# PID file managed by: orchestrator/autopilot.py at /tmp/general-autopilot.pid
set -euo pipefail

APP="${1:-}"
if [[ -z "$APP" ]]; then
  echo "Usage: $0 <app> [uninstall]" >&2
  exit 1
fi

ACTION="${2:-install}"
LABEL="com.roman.general.autopilot-keepalive"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/General"
LOG_FILE="$LOG_DIR/autopilot-keepalive.log"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GENERAL_BIN="$HERE/general"

# ── Uninstall path ──────────────────────────────────────────────────────────
if [[ "$ACTION" == "uninstall" ]]; then
  if [[ -f "$PLIST" ]]; then
    # Unload first (ignore errors — agent may not be loaded yet).
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $PLIST"
  else
    echo "Nothing to uninstall — $PLIST not found."
  fi
  # Also clean up a stale PID file left by an ungraceful kill, if present.
  rm -f /tmp/general-autopilot.pid
  exit 0
fi

# ── Install path ────────────────────────────────────────────────────────────
mkdir -p "$PLIST_DIR" "$LOG_DIR"

# Unload an existing copy first so launchctl sees the refreshed plist.
if [[ -f "$PLIST" ]]; then
  launchctl unload "$PLIST" 2>/dev/null || true
fi

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- Unique reverse-DNS label — distinct from all retired scheduler agents -->
    <key>Label</key>
    <string>${LABEL}</string>

    <!-- Keep autopilot alive. KeepAlive=true makes this an ALWAYS-ON job: launchd starts it at
         load and restarts it whenever it exits. So loading the agent (launchctl load, below) starts
         LIVE autopilot immediately. A plain 'launchctl stop' is respawned, so UNLOAD the agent to
         stop it durably. -->
    <key>KeepAlive</key>
    <true/>

    <!-- RunAtLoad=true agrees with KeepAlive=true (which already forces a start at load), so both
         keys point the same way and there is no separate manual first start: the 'launchctl load'
         below launches LIVE autopilot right away.
         Stop durably with:  launchctl unload "${PLIST}"   (KeepAlive=true respawns the job after a
         plain 'launchctl stop', so UNLOAD it to make the stop stick; re-run this installer to bring
         it back). -->
    <key>RunAtLoad</key>
    <true/>

    <!-- The command to run; WorkingDirectory ensures relative paths work. -->
    <key>WorkingDirectory</key>
    <string>${HERE}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${GENERAL_BIN}</string>
        <string>--live</string>
        <string>autopilot</string>
        <string>${APP}</string>
    </array>

    <!-- Stdout and stderr land in a single rotating log file. -->
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
</dict>
</plist>
PLIST_EOF

# Load the agent. Because KeepAlive=true (and RunAtLoad=true), this STARTS LIVE autopilot immediately
# and launchd keeps it alive (restarting it whenever it exits). There is no separate manual start.
launchctl load "$PLIST"

echo ""
echo "✅ Installed & STARTED: $LABEL"
echo "   (KeepAlive=true: 'launchctl load' just started LIVE autopilot; launchd restarts it on exit.)"
echo "   Plist :  $PLIST"
echo "   Log   :  $LOG_FILE"
echo "   App   :  $APP"
echo ""
echo "   Status   →  cat /tmp/general-autopilot.pid   (PID of the live autopilot worker)"
echo "   Stop     →  launchctl unload $PLIST   (KeepAlive respawns it after a plain 'launchctl stop')"
echo "   Restart  →  bash scripts/install-mac-autopilot-daemon.sh $APP   (reload = unload + reload)"
echo "   Logs     →  tail -f $LOG_FILE"
echo ""
echo "   Uninstall  →  bash scripts/install-mac-autopilot-daemon.sh $APP uninstall"
