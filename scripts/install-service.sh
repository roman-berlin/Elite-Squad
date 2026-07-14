#!/bin/bash
# Elite Unit — install the hardened systemd unit for general.service
#
# This script writes the systemd unit file with the hardening parameters
# specified in EU-184 and VPS_DEPLOYMENT.md (StartLimitIntervalSec=300,
# StartLimitBurst=5) to prevent crash-loop restarts.
#
# Idempotent: can be safely re-run to update the unit file.
#
# Environment variables:
#   DRY_RUN=1       - Write to OUTPUT_FILE instead of /etc/systemd/system (for testing)
#   OUTPUT_FILE=    - Path to write the unit file when DRY_RUN=1
#
set -uo pipefail

# Determine where to write the unit file
if [ "${DRY_RUN:-}" = "1" ]; then
  UNIT_FILE="${OUTPUT_FILE:-/tmp/general.service}"
else
  UNIT_FILE="/etc/systemd/system/general.service"
fi

# Get the current user and home directory
USER="${USER:-$(whoami)}"
HOME="${HOME:-$(eval echo ~$USER)}"

# Write the hardened unit file
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Elite Unit — cockpit + Telegram listener
After=network-online.target
Wants=network-online.target
# EU-184 (Wave 0): bound the restart storm. Without a start-limit, a main deploy that crashes on
# startup restarts forever (Restart=always + RestartSec=5 → ~864 restarts/min was observed on
# 2026-07-05). After StartLimitBurst restarts within StartLimitIntervalSec, systemd gives up and
# leaves the unit 'failed' instead of pegging the box. self-update.sh's smoke-test-before-restart
# is the first line of defence; this is the backstop for a RUNTIME crash the import test can't catch.
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
User=$USER
WorkingDirectory=$HOME/General
ExecStart=$HOME/General/general serve --port 8787
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "Systemd unit file written to $UNIT_FILE"

# Reload systemd and enable the service (unless dry-run)
if [ "${DRY_RUN:-}" != "1" ]; then
  systemctl daemon-reload
  systemctl enable --now general.service
  echo "Systemd daemon-reloaded and general.service enabled"
fi
