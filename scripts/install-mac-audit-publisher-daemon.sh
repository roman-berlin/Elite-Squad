#!/bin/bash
# Elite Unit — install (or uninstall) the Mac launchd AUDIT PUBLISHER (EU-428 AC0).
#
# WHY: the Mac's audit had not been published since late June — crontab has no sync entry and no
# launchd agent ran `./general sync`, so shared/mac.jsonl froze at 2026-06-26 while the VPS cron kept
# logging a healthy `pulled=True` (it was pulling a file that never changed). EU-181 removed the
# BROKEN Mac sync agent (it pointed at a deleted script) and never replaced the PUBLISH half. This
# agent restores it: a PERIODIC launchd job that runs the existing sync.publish / git_sync path on a
# schedule, so shared/mac.jsonl tracks the live audit again and the daily brief stops being written by
# the one host that cannot see a build.
#
# It is deliberately a PERIODIC TIMER (StartInterval), NOT KeepAlive: publish is a short git
# fetch+commit+push that must run on a cadence and EXIT — KeepAlive would respawn it on every exit
# (a busy loop) and could stack overlapping syncs. StartInterval fires it every ~15 min and leaves it
# idle otherwise. git_sync is idempotent and race-handled (rebase-on-push-conflict), so a slow run
# straddling the next tick is harmless.
#
# Cadence: 900s (15 min) — matches the server's pull-only sync cadence (install-server-cron.sh), so a
# freshly-published Mac audit is on the unit-state branch well before the server's next pull.
#
# Pairs with the EU-403 watchdog (scripts/install-mac-watchdog-daemon.sh): the watchdog watches the
# Mac cockpit is alive; this agent ships the Mac's audit OFF the Mac so the VPS can see it. They share
# nothing — both are pure periodic timers.
#
# Usage:
#   bash scripts/install-mac-audit-publisher-daemon.sh            # install (default) — runs a sync now
#   bash scripts/install-mac-audit-publisher-daemon.sh uninstall  # stop + remove
#
# The plist label is:  com.roman.general.audit-publisher
# Log output goes to:  ~/Library/Logs/General/audit-publisher.log
set -euo pipefail

ACTION="${1:-install}"
LABEL="com.roman.general.audit-publisher"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/General"
LOG_FILE="$LOG_DIR/audit-publisher.log"
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
    <!-- Unique reverse-DNS label — distinct from the cockpit/autopilot keepalives and the watchdog. -->
    <key>Label</key>
    <string>${LABEL}</string>

    <!-- PERIODIC TIMER: launchd runs './general sync' every StartInterval seconds, NOT a KeepAlive
         respawn. 900s (15 min) matches the server's pull cadence. Publish is a short git op that
         exits; KeepAlive would busy-loop and stack overlapping syncs. -->
    <key>StartInterval</key>
    <integer>900</integer>
    <key>RunAtLoad</key>
    <true/>

    <!-- A CURATED static PATH (mirrors install-mac-watchdog-daemon.sh): launchd agents get a minimal
         PATH with no shell profile, so the 'general' wrapper's venv activation + python must resolve.
         NOTE: this heredoc is UNQUOTED (it must expand \${LABEL}/\${GENERAL_BIN}), so backticks here
         are COMMAND SUBSTITUTION, not quoting. The original text wrapped the command names in
         backticks: install actually EXECUTED them, printed "general: command not found", and pasted
         a stray sync line into this comment. Use single quotes in this heredoc, never backticks. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:\${HOME}/.bun/bin:\${HOME}/bin</string>
        <key>GENERAL_DIR</key>
        <string>${HERE}</string>
        <!-- Pin the host id so the published file is ALWAYS shared/mac.jsonl regardless of what the
             live .env declares (or doesn't). The server cron pins GENERAL_HOST_ID=server symmetrically. -->
        <key>GENERAL_HOST_ID</key>
        <string>mac</string>
    </dict>

    <!-- WorkingDirectory = repo root, so state/ + .env resolve exactly as the cockpit sees them. -->
    <key>WorkingDirectory</key>
    <string>${HERE}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${GENERAL_BIN}</string>
        <string>sync</string>
    </array>

    <!-- The publisher's own log (one sync-summary line per run; the sync command prints the
         peers=age=... line, which is itself the transport-honesty signal EU-428 AC1 added). -->
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
</dict>
</plist>
PLIST_EOF

# Bootstrap the agent — RunAtLoad runs the first sync immediately.
launchctl bootstrap gui/$(id -u) "$PLIST"
launchctl load "$PLIST" 2>/dev/null || true   # legacy fallback for very old macOS

echo "Installed + started: $LABEL"
echo "  cadence:   every 900s (publish + push the Mac audit to the unit-state branch)"
echo "  command:   ${GENERAL_BIN} sync"
echo "  log:       $LOG_FILE"
echo "  stop:      launchctl bootout gui/\$(id -u)/$LABEL"
echo ""
echo "  QA:  after one cycle, the newest ts in ~/General/.unit-state/shared/mac.jsonl should be"
echo "       within ~15 min of the newest ts in ~/General/state/audit.jsonl."
