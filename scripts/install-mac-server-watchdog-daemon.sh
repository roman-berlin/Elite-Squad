#!/bin/bash
# Elite Unit — install (or uninstall) the Mac launchd CROSS-HOST WATCHDOG that watches the VPS (EU-433).
#
# WHY: nothing watches the VPS. The Mac's on-box watchdog (EU-403) is hardwired to 127.0.0.1:8787 —
# itself — so a wedged or gone VPS is invisible to it. Worse, the VPS is the elected Telegram sender
# (EU-303), so a wedged VPS CANNOT page about itself: the only alert channel IS the VPS, and silence
# is indistinguishable from health (it already bit — since 07-20 the daily brief arrived carrying a
# raw 401 as its body, EU-430). This agent is the missing reverse direction of EU-428: the Mac probes
# the VPS over SSH and pages from the MAC, so the alert path does not depend on the VPS process at all.
#
# It runs `./general server-watchdog` (orchestrator/server_watchdog.py) as a SEPARATE periodic launchd
# job, completely outside the serve process. That tick SSHes the VPS, runs a dumb remote probe (cockpit
# /api/health + the newest daily_brief audit row), and classifies three independent conditions:
#   1. down          — SSH refused / timed out, or the VPS cockpit answered non-200 (AC1 liveness);
#   2. brief_missing — no daily brief arrived within the window (AC2 content: a MISSING scheduled brief);
#   3. brief_bad     — the newest brief's body is a provider/auth error, not a brief (AC2 content).
# Each condition raises ONE alert + ONE recovery (the watchdog.sh discipline), and `down` suppresses
# the brief checks so an unreachable VPS produces one alert, not three.
#
# It is deliberately a PERIODIC TIMER (StartInterval), NOT KeepAlive: the job runs the short tick every
# 5 min and exits. KeepAlive would respawn it on every exit (a busy loop).
#
# SLEEP RESILIENCE (AC1 caveat): a StartInterval does not fire while the Mac sleeps, but fires once on
# wake — so a VPS outage that began mid-sleep is caught on wake + after the 2-check threshold. Absence
# of a check is not evidence of health, so this watcher is PAIRED with the VPS's OWN out-of-process
# watchdog (AC4, scheduled by install-server-cron.sh), which runs 24/7 on the always-on box and catches
# a local wedge the sleeping Mac cannot see.
#
# Requires GENERAL_SERVER_SSH (e.g. ubuntu@1.2.3.4) in .env — the Mac is the only host that points at
# the VPS. No-op (no alert, clean exit) when it is unset, so the agent is safe to install anywhere.
#
# Usage:
#   bash scripts/install-mac-server-watchdog-daemon.sh            # install (default) — runs a check now
#   bash scripts/install-mac-server-watchdog-daemon.sh uninstall  # stop + remove
#
# The plist label is:  com.roman.general.server-watchdog
# Log output goes to:  ~/Library/Logs/General/server-watchdog.log
set -euo pipefail

ACTION="${1:-install}"
LABEL="com.roman.general.server-watchdog"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/General"
LOG_FILE="$LOG_DIR/server-watchdog.log"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
    <!-- Unique reverse-DNS label — distinct from the on-box watchdog + cockpit + autopilot agents. -->
    <key>Label</key>
    <string>${LABEL}</string>

    <!-- PERIODIC TIMER: launchd runs the short cross-host tick every StartInterval seconds, not a
         KeepAlive respawn. 300s keeps the 2-consecutive-failure alert inside ~10 min even with
         launchd jitter + SSH/curl hang timeouts. -->
    <key>StartInterval</key>
    <integer>300</integer>
    <key>RunAtLoad</key>
    <true/>

    <!-- A CURATED static PATH (mirrors the on-box watchdog installer): launchd agents get a minimal
         PATH with no shell profile, so /usr/bin/ssh + /bin/bash must resolve. The ./general wrapper
         activates the venv and sources .env (GENERAL_SERVER_SSH + TELEGRAM_*), so the tick can reach
         the VPS over SSH and page Telegram from the Mac. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:\${HOME}/.bun/bin:\${HOME}/bin</string>
        <key>GENERAL_DIR</key>
        <string>${HERE}</string>
    </dict>

    <!-- WorkingDirectory = repo root, so state/ + .env resolve exactly as the cockpit sees them. -->
    <key>WorkingDirectory</key>
    <string>${HERE}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${HERE}/general</string>
        <string>server-watchdog</string>
    </array>

    <!-- The tick's own log (one line per run; alerts go to Telegram). -->
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
echo "  cadence:   every 300s (alert after 2 consecutive failures; re-notify ~hourly)"
echo "  command:   ./general server-watchdog  (SSH probe of the VPS + content check)"
echo "  target:    \$GENERAL_SERVER_SSH  (set it in .env — no-op if unset)"
echo "  log:       $LOG_FILE"
echo "  state:     $HERE/state/server_watchdog_state.json"
echo "  stop:      launchctl bootout gui/\$(id -u)/$LABEL"
echo ""
echo "  Requires GENERAL_SERVER_SSH in .env (e.g. ubuntu@1.2.3.4). The VPS cockpit is never"
echo "  exposed to the internet, so the tick SSHes in and curls 127.0.0.1:8787 ON the VPS."
echo ""
echo "  QA:  stop general.service on the VPS (or block SSH) -> expect a 'VPS unreachable' Telegram"
echo "       alert from the MAC within ~10 min, then a 'reachable again' notice on recovery."
