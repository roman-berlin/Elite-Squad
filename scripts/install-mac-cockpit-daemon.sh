#!/bin/bash
# Elite Unit — install (or uninstall) the Mac launchd keepalive agent for the COCKPIT (./general serve).
#
# Why: `./general serve` is a foreground process — when the terminal that started it closes, SIGHUP
# kills the Flask cockpit and the War Room goes dark (live incident 2026-07-09: the cockpit was down
# for hours and nobody could see or drive the unit from the browser). This agent mirrors the EU-195
# VPS systemd unit (Restart=always, RestartSec=5) on the Mac: launchd starts the cockpit at login,
# restarts it within ~5s whenever it exits, and survives terminal closes entirely.
#
# It pairs with scripts/install-mac-autopilot-daemon.sh (the WORKER keepalive) — installing both makes
# the unit fully autonomic on this machine: the cockpit is always reachable on 127.0.0.1:8787 and the
# autopilot keeps draining, no open terminal required.
#
# Port safety: server.py binds 8787 at startup, so a second copy (e.g. a manual `./general serve` in a
# terminal while this agent is live) exits immediately on the bind error and launchd's respawn keeps
# the daemon copy as the survivor. The `general` wrapper owns .venv activation and .env sourcing
# (GLM_AUTH_TOKEN etc.), so no extra environment plumbing is needed here.
#
# Stopping it: KeepAlive=true means a plain `launchctl stop` is immediately respawned. To stop it
# durably, BOOTOUT the agent (`launchctl bootout gui/$(id -u)/com.roman.general.cockpit`) or run the
# `uninstall` action below; re-run this installer to bring it back.
#
# Usage:
#   bash scripts/install-mac-cockpit-daemon.sh            # install (default) — starts the cockpit now
#   bash scripts/install-mac-cockpit-daemon.sh uninstall  # stop + remove
#
# The plist label is:  com.roman.general.cockpit
# Log output goes to:  ~/Library/Logs/General/cockpit.log
set -euo pipefail

ACTION="${1:-install}"
LABEL="com.roman.general.cockpit"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/General"
LOG_FILE="$LOG_DIR/cockpit.log"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GENERAL_BIN="$HERE/general"

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
    <!-- Unique reverse-DNS label — distinct from the autopilot keepalive and all retired agents -->
    <key>Label</key>
    <string>${LABEL}</string>

    <!-- ALWAYS-ON: launchd starts the cockpit at load/login and restarts it whenever it exits.
         Stop durably with:  launchctl bootout gui/\$(id -u)/${LABEL} -->
    <key>KeepAlive</key>
    <true/>
    <key>RunAtLoad</key>
    <true/>

    <!-- Mirror the EU-195 systemd unit's RestartSec=5: wait 5s between respawns so a hard
         crash-loop (bad config, port squatter) doesn't spin the CPU. -->
    <key>ThrottleInterval</key>
    <integer>5</integer>

    <!-- launchd agents get a MINIMAL Path (no /opt/homebrew/bin, no shell profile) — without this
         the automatixy gate died with "[Errno 2] No such file or directory: 'bun'" on 2026-07-10.
         Embed the INSTALLING shell's PATH so every toolchain the gates need (bun, node, gh…)
         resolves exactly as it does when Roman runs serve by hand. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${PATH}</string>
    </dict>

    <!-- The command; WorkingDirectory makes config.yaml/state/ relative paths resolve. The
         general wrapper owns .venv activation and .env sourcing. -->
    <key>WorkingDirectory</key>
    <string>${HERE}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${GENERAL_BIN}</string>
        <string>serve</string>
    </array>

    <!-- Stdout and stderr (the live feed) land in one log file. -->
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
</dict>
</plist>
PLIST_EOF

# Bootstrap the agent — KeepAlive/RunAtLoad start the cockpit immediately.
launchctl bootstrap gui/$(id -u) "$PLIST"
launchctl load "$PLIST" 2>/dev/null || true   # legacy fallback for very old macOS

echo "Installed + started: $LABEL"
echo "  cockpit:   http://127.0.0.1:8787"
echo "  log:       $LOG_FILE"
echo "  stop:      launchctl bootout gui/\$(id -u)/$LABEL"
