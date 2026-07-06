#!/bin/bash
# Elite Unit — install the server's crontab in one shot (no `crontab -e`/vim needed).
#
# Installs, idempotently:
#   - self-update (auto-deploy main)          every 15 min, offset :05/:20/:35/:50
#   - Mac->server state sync (pull-only)       every 15 min
#   - weekly patrol                            Mon 09:00
# Phase-2 §2 (2026-07-06): the 06:30 council muster and the 11/14/16 corridor small-talk crons
# were RETIRED — councils/meetings convene on demand (CLI / cockpit / Telegram) now. Re-run this
# script on a box that installed an older crontab to drop those lines.
#
# All times are LOCAL via CRON_TZ=Asia/Jerusalem, so 06:30 = the Commander's 06:30 (and follows DST)
# even though the VPS system clock is UTC.
#
# Re-run any time — it replaces the unit's own lines and leaves any other cron entries you have alone.
set -uo pipefail
cd "$HOME/General" || { echo "Run this from the server's ~/General directory."; exit 1; }

TMP="$(mktemp)"
# Keep your non-unit cron lines; drop the unit's so we can re-add a clean set (idempotent).
crontab -l 2>/dev/null | grep -vE 'General &&|self-update\.sh|RANDOM % 2100|CRON_TZ=Asia' > "$TMP" || true
cat >> "$TMP" <<'CRON'
CRON_TZ=Asia/Jerusalem
5,20,35,50 * * * * cd $HOME/General && bash scripts/self-update.sh
*/15 * * * * cd $HOME/General && GENERAL_HOST_ID=server GENERAL_SYNC_PULL_ONLY=1 ./general sync >> council/cron.log 2>&1
0 9 * * 1 cd $HOME/General && ./general patrol >> council/cron.log 2>&1
# SWE-bench Verified weekly benchmark — Mon 04:00 (off-peak), deterministic sample via --weekly seed
0 4 * * 1 cd $HOME/General && python3 scripts/swebench_builder.py --weekly --sample 20 >> council/cron.log 2>&1
CRON
crontab "$TMP"
rm -f "$TMP"

echo "Elite Unit server cron installed:"
crontab -l | grep -E 'General|self-update' || true
