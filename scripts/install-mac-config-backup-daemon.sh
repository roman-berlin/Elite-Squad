#!/bin/bash
# EU-408 — install (or uninstall) the Mac launchd agent that backs up .env + config.yaml nightly.
#
# .env and config.yaml are gitignored (they hold secrets), so they have no version-control safety
# net — disk failure, an errant cleanup, or a bad agent edit loses them and the unit is down until
# every credential is re-issued and the config is rebuilt. This installer wires scripts/backup-config.sh
# to run once a day via launchd (StartInterval=86400). The backups land OUTSIDE the repo
# (~/.general-config-backups by default) so a repo wipe does not take them too. See DEPLOYMENT.md
# § "Config & secrets backup" for restore.
#
# Usage:
#   bash scripts/install-mac-config-backup-daemon.sh            # install (runs a backup immediately too)
#   bash scripts/install-mac-config-backup-daemon.sh uninstall  # stop + remove
#
# The plist label is:  com.roman.general.config-backup
# Log output goes to:  ~/Library/Logs/General/config-backup.log
# Backup dir:          ~/.general-config-backups  (override: GENERAL_CONFIG_BACKUP_DIR)
set -euo pipefail

ACTION="${1:-install}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.roman.general.config-backup"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST="$PLIST_DIR/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/General"
LOG_FILE="$LOG_DIR/config-backup.log"
SCRIPT="$HERE/scripts/backup-config.sh"

if [[ ! -f "$SCRIPT" ]]; then
  echo "scripts/backup-config.sh not found at $SCRIPT (run this installer from the repo root)" >&2
  exit 1
fi
mkdir -p "$LOG_DIR" "$PLIST_DIR" 2>/dev/null || true

# ── Uninstall path ──────────────────────────────────────────────────────────
if [[ "$ACTION" == "uninstall" ]]; then
  if [[ -f "$PLIST" ]]; then
    launchctl bootout gui/$(id -u)/"$LABEL" 2>/dev/null || true
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed $PLIST"
  else
    echo "No plist at $PLIST — nothing to remove."
  fi
  echo "Stopped: $LABEL (existing backups in ~/.general-config-backups are left in place)."
  exit 0
fi

if [[ "$ACTION" != "install" ]]; then
  echo "Usage: $0 [install|uninstall]" >&2
  exit 1
fi

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- Unique reverse-DNS label — distinct from the cockpit / autopilot / watchdog agents. -->
    <key>Label</key>
    <string>${LABEL}</string>

    <!-- PERIODIC TIMER: launchd runs the backup once a day. The off-peak hour is left to launchd's
         scheduler jitter (StartInterval is relative, not wall-clock-pinned); the exact minute does
         not matter for a backup. RunAtLoad runs one immediately on install/bootstrap. -->
    <key>StartInterval</key>
    <integer>86400</integer>
    <key>RunAtLoad</key>
    <true/>

    <!-- A CURATED static PATH (mirrors install-mac-cockpit-daemon.sh): launchd agents get a minimal
         PATH with no shell profile, so /bin/bash + coreutils must resolve. -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:\${HOME}/.bun/bin:\${HOME}/bin</string>
        <!-- The backup script derives the repo root from GENERAL_DIR. -->
        <key>GENERAL_DIR</key>
        <string>${HERE}</string>
    </dict>

    <!-- WorkingDirectory = repo root, so .env + config.yaml resolve exactly as the cockpit sees them. -->
    <key>WorkingDirectory</key>
    <string>${HERE}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${SCRIPT}</string>
    </array>

    <!-- The backup's own log (one line per run; the backups themselves are NOT logged here). -->
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
</dict>
</plist>
PLIST_EOF

# Bootstrap the agent — RunAtLoad runs the first backup immediately.
launchctl bootstrap gui/$(id -u) "$PLIST"
launchctl load "$PLIST" 2>/dev/null || true   # legacy fallback for very old macOS

echo "Installed + started: $LABEL"
echo "  cadence:   every 86400s (~daily), plus once immediately (RunAtLoad)"
echo "  script:    $SCRIPT"
echo "  log:       $LOG_FILE"
echo "  backups:   \$HOME/.general-config-backups  (override: GENERAL_CONFIG_BACKUP_DIR)"
echo "  retention: newest 14 snapshots per file  (override: BACKUP_KEEP)"
echo "  stop:      bash scripts/install-mac-config-backup-daemon.sh uninstall"
echo ""
echo "  RESTORE: see DEPLOYMENT.md § 'Config & secrets backup (EU-408)'."
