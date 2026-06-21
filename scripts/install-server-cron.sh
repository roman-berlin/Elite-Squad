#!/bin/bash
# Elite Unit — install the server's crontab in one shot (no `crontab -e`/vim needed).
#
# Installs, idempotently:
#   - self-update (auto-deploy main)          every 15 min, offset :05/:20/:35/:50
#   - Mac->server state sync (pull-only)       every 15 min
#   - daily muster (council + stand-up)        06:30  (the unit's ONE daily council — the Mac's launchd
#                                                      council is retired; this is the single source)
#   - one freshening sync just before it       06:30
#   - corridor small-talk (jittered)           11:00 / 14:30-ish / 16:00-ish
#   - weekly patrol                            Mon 09:00
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
30 6 * * * cd $HOME/General && GENERAL_HOST_ID=server GENERAL_SYNC_PULL_ONLY=1 ./general sync >> council/cron.log 2>&1
30 6 * * * cd $HOME/General && ./general council >> council/cron.log 2>&1
0 11,14,16 * * * bash -c 'sleep $((RANDOM % 2100)); cd $HOME/General && ./general smalltalk >> council/cron.log 2>&1'
0 9 * * 1 cd $HOME/General && ./general patrol >> council/cron.log 2>&1
CRON
crontab "$TMP"
rm -f "$TMP"

echo "Elite Unit server cron installed:"
crontab -l | grep -E 'General|self-update' || true
