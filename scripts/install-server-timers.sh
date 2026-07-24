#!/bin/bash
# Elite Unit — install systemd timers for daily stand-up and officer council (DST-proof).
#
# Replaces the two cron jobs installed by install-server-cron.sh (EU-437 follow-up to EU-432):
#   - daily stand-up:  every day 08:30 Asia/Jerusalem
#   - officer council: Mon 09:30 Asia/Jerusalem
#
# Uses systemd's OnCalendar= with explicit timezone + Persistent= so local times hold across
# DST transitions (IDT↔IST). Requires systemd >= 240 (tz-suffixed OnCalendar with explicit
# timezone requires 240+; any current Ubuntu VPS qualifies; the script checks `systemd --version`
# and aborts if older).
#
# Idempotent: can be safely re-run to update the unit files.
#
# Environment variables:
#   DRY_RUN=1       - Write to OUTPUT_DIR/* instead of /etc/systemd/system/ (for testing)
#   OUTPUT_DIR=     - Directory for dry-run output (default /tmp/general-timers-dryrun)
#
# NOTE: Enabling the timers on the VPS is an ops step — run this script ON THE BOX as root/sudo.
# The self-update path does NOT apply installer scripts from a PR landing.
#
set -uo pipefail

# ── Version gate: tz-suffixed OnCalendar needs systemd >= 240 ────────────────
SYSVer=$(systemd --version 2>/dev/null | head -n1 | sed 's/.*[[:space:]]//')
if [ "${SYSVer:-0}" -lt 240 ] 2>/dev/null; then
  echo "ERROR: requires systemd >= 240 for tz-suffixed OnCalendar (got ${SYSVer:-unknown}). Aborting."
  exit 1
fi

cd "$HOME/General" || { echo "Run this from the server's ~/General directory."; exit 1; }

USER="${USER:-$(whoami)}"
HOME_DIR="${HOME:-$(eval echo ~$USER)}"

# Determine where to write units
if [ "${DRY_RUN:-}" = "1" ]; then
  UNIT_DIR="${OUTPUT_DIR:-/tmp/general-timers-dryrun}"
  mkdir -p "$UNIT_DIR"
else
  UNIT_DIR="/etc/systemd/system"
fi

# ── general-daily.service ────────────────────────────────────────────────────
cat > "$UNIT_DIR/general-daily.service" <<EOF
[Unit]
Description=Elite Unit — light daily stand-up (DST-proof, timer-driven)

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$HOME_DIR/General
ExecStart=/bin/bash -c 'cd $HOME_DIR/General && ./general cron-guard --job daily -- ./general daily >> council/cron.log 2>&1'

[Install]
WantedBy=multi-user.target
EOF

# ── general-daily.timer ──────────────────────────────────────────────────────
cat > "$UNIT_DIR/general-daily.timer" <<EOF
[Unit]
Description=Elite Unit — daily stand-up timer (08:30 Asia/Jerusalem, every day)

[Timer]
OnCalendar=*-*-* 08:30:00 Asia/Jerusalem
Persistent=true

[Install]
WantedBy=timers.target
EOF

# ── general-council.service ──────────────────────────────────────────────────
cat > "$UNIT_DIR/general-council.service" <<EOF
[Unit]
Description=Elite Unit — deep officer council (DST-proof, timer-driven)

[Service]
Type=oneshot
User=$USER
WorkingDirectory=$HOME_DIR/General
ExecStart=/bin/bash -c 'cd $HOME_DIR/General && ./general cron-guard --job council -- ./general council >> council/cron.log 2>&1'

[Install]
WantedBy=multi-user.target
EOF

# ── general-council.timer ────────────────────────────────────────────────────
cat > "$UNIT_DIR/general-council.timer" <<EOF
[Unit]
Description=Elite Unit — weekly officer council timer (Mon 09:30 Asia/Jerusalem)

[Timer]
OnCalendar=Mon *-*-* 09:30:00 Asia/Jerusalem
Persistent=true

[Install]
WantedBy=timers.target
EOF

echo "Systemd timer units written to $UNIT_DIR:"
ls -1 "$UNIT_DIR"/general-daily.* "$UNIT_DIR"/general-council.*

# Reload systemd and enable the timers (unless dry-run)
if [ "${DRY_RUN:-}" != "1" ]; then
  systemctl daemon-reload
  systemctl enable --now general-daily.timer
  systemctl enable --now general-council.timer
  echo ""
  echo "Systemd daemon-reloaded; timers enabled and started."
  echo ""
  echo "--- timedatectl ---"
  timedatectl
  echo ""
  echo "--- active timers ---"
  systemctl list-timers 'general-*'
fi
