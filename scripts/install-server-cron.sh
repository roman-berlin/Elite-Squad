#!/bin/bash
# Elite Unit — install the server's crontab in one shot (no `crontab -e`/vim needed).
#
# Installs, idempotently:
#   - self-update (auto-deploy main)          every 15 min, offset :05/:20/:35/:50
#   - Mac->server state sync (pull-only)       every 15 min
#   - light daily stand-up                     every day 08:30 Jerusalem (05:30 UTC)
#   - deep officer council                     Mon 09:30 Jerusalem / 06:30 UTC (WEEKLY; the muster)
# Phase-2 §2 (2026-07-06): the 06:30 council muster and the 11/14/16 corridor small-talk crons
# were RETIRED. 2026-07-07: best-practice ceremony split — a LIGHT daily stand-up (`general daily`)
# runs every morning, and the DEEP multi-officer council (`general council`) is now WEEKLY. Re-run
# this script on a box that installed an older crontab to drop stale lines and pick up the new set.
#
# EU-432 (2026-07-22): the previous crontab set `CRON_TZ=Asia/Jerusalem` and claimed times were
# LOCAL. Ubuntu's cron IGNORES per-crontab CRON_TZ (man 5 crontab, LIMITATIONS), and the box's
# system clock is Etc/UTC (timedatectl) — so every job fired at its stated HOUR IN UTC, i.e. 3h
# late during IDT (the "08:30 daily brief" landed at 11:30). All times below are now UTC, converted
# from the Commander's LOCAL (Asia/Jerusalem) intent at IDT (UTC+3): 08:30->05:30, 09:30->06:30.
# CAVEAT: these are FIXED UTC hours, so they drift by 1h when Israel flips DST (IDT<->IST). That is
# strictly better than the old permanent 3h error; for DST-perfect scheduling move these to systemd
# timers (which honour OnCalendar= timezone + Persistent=), a deliberate follow-up not done here.
# EU-432 also REMOVED two jobs that had never once succeeded (see notes on each below):
#   - `patrol automatixy`: this host has no real product repo (automatixy.repo_path is a placeholder
#     pointing at the orchestrator's OWN source), so patrol would file AUTO tickets against the
#     product backlog from the wrong code. The server's role is "discusses, never builds/patrols."
#   - `swebench_builder.py`: the host's role is NEVER BUILDS (no gate_commands, no venv SDK, no
#     datasets/swebench). Plus an ungated build was possible (gate.py returned passed=True on an
#     empty gate) — that hole is now closed in code: run_gate REFUSES when no gate is configured.
# EU-432: the sync/daily/council jobs run through `./general cron-guard`, which timestamps every
# output line, size-rotates council/cron.log, and alerts Telegram on repeated failure (exactly one
# alert per N consecutive failures) — so two jobs failing weekly can no longer pass silently.
# self-update keeps its own `>> council/cron.log` redirect (it already Telegram-alerts its one
# critical failure, the import smoke-test) and its header lines carry $(date); the guard's rotation
# still rolls the whole file.
#
# Re-run any time — it replaces the unit's own lines and leaves any other cron entries you have alone.
set -uo pipefail
cd "$HOME/General" || { echo "Run this from the server's ~/General directory."; exit 1; }

TMP="$(mktemp)"
# Keep your non-unit cron lines; drop the unit's so we can re-add a clean set (idempotent). The
# pattern also strips the legacy CRON_TZ=Asia line and any old cron-guard lines from a prior install.
crontab -l 2>/dev/null | grep -vE 'General &&|self-update\.sh|cron-guard|RANDOM % 2100|CRON_TZ=Asia|patrol automatixy|swebench_builder' > "$TMP" || true
cat >> "$TMP" <<'CRON'
# ── All times are UTC (the VPS clock is Etc/UTC; Ubuntu cron ignores CRON_TZ — see header) ──
5,20,35,50 * * * * cd $HOME/General && bash scripts/self-update.sh >> council/cron.log 2>&1
# sync is pull-only on this host; cron-guard timestamps + rotates the log and alerts on repeated failure.
*/15 * * * * cd $HOME/General && GENERAL_HOST_ID=server GENERAL_SYNC_PULL_ONLY=1 ./general cron-guard --job sync -- ./general sync >> council/cron.log 2>&1
# Light daily stand-up — every morning 08:30 Jerusalem (IDT, UTC+3) -> 05:30 UTC (~1 model call).
30 5 * * * cd $HOME/General && ./general cron-guard --job daily -- ./general daily >> council/cron.log 2>&1
# Deep officer council — WEEKLY, Mon 09:30 Jerusalem (IDT, UTC+3) -> 06:30 UTC (the multi-officer muster).
30 6 * * 1 cd $HOME/General && ./general cron-guard --job council -- ./general council >> council/cron.log 2>&1
CRON
crontab "$TMP"
rm -f "$TMP"

echo "Elite Unit server cron installed:"
crontab -l | grep -E 'General|self-update|cron-guard' || true
