#!/bin/bash
# Elite Unit — install the server's crontab in one shot (no `crontab -e`/vim needed).
#
# Installs, idempotently:
#   - self-update (auto-deploy main)          every 15 min, offset :05/:20/:35/:50
#   - Mac->server state sync (pull-only)       every 15 min
#   - VPS watchdog                            every 5 min (local wedge detection)
# NOTE: the daily stand-up (08:30 Jerusalem) and weekly council (Mon 09:30) are now scheduled by
# systemd timers (EU-437: install-server-timers.sh) so they are DST-proof — not crontab'd here.
# Phase-2 §2 (2026-07-06): the 06:30 council muster and the 11/14/16 corridor small-talk crons
# were RETIRED. 2026-07-07: best-practice ceremony split — a LIGHT daily stand-up (`general daily`)
# runs every morning, and the DEEP multi-officer council (`general council`) is now WEEKLY. Re-run
# this script on a box that installed an older crontab to drop stale lines and pick up the new set.
#
# EU-432 (2026-07-22): the previous crontab set `CRON_TZ=Asia/Jerusalem` and claimed times were
# LOCAL. Ubuntu's cron IGNORES per-crontab CRON_TZ (man 5 crontab, LIMITATIONS), and the box's
# system clock is Etc/UTC (timedatectl) — so every job fired at its stated HOUR IN UTC, i.e. 3h
# late during IDT (the "08:30 daily brief" landed at 11:30). All times below are now UTC, converted
# from the Commander's LOCAL (Asia/Jerusalem) intent at IDT (UTC+3).
# NOTE: daily stand-up (08:30 Jerusalem / 05:30 UTC) and weekly council (Mon 09:30 Jerusalem /
# 06:30 UTC) are no longer crontab'd here — they are driven by systemd timers (EU-437).
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
# EU-433 (2026-07-22): AC4 — this host's OWN watchdog.sh was installed but scheduled by nothing, so a
# local cockpit wedge (alive-but-stuck, or a dead cockpit systemd hasn't restarted yet) went unreported.
# Added a */5 cron that runs it out-of-process (bash+curl), pointed at this host's own cockpit, so a
# local wedge is caught locally AND still pages with general.service stopped. The Mac's cross-host
# watcher (AC1, install-mac-server-watchdog-daemon.sh) is the reverse-direction belt to this suspenders.
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
# EU-433 AC4 — VPS LOCAL watchdog. The on-box watchdog.sh script ships in the repo but was scheduled
# by NOTHING, so a local wedge (cockpit alive-but-stuck, or systemd's Restart=always not yet catching
# a dead cockpit) went unreported. It runs OUT OF general.service (pure bash+curl), curls THIS host's
# own cockpit (the script's built-in default http://127.0.0.1:8787/api/health — the cockpit is never
# exposed to the internet) and reads THIS host's own state/audit.jsonl, then pages via curl-to-Telegram
# directly — so it STILL ALERTS with general.service stopped (the VPS cannot report its own wedge via
# the serve process; this independent cron path can). Pairs with the Mac's cross-host watcher (AC1).
*/5 * * * * cd $HOME/General && bash scripts/watchdog.sh >> council/watchdog.log 2>&1
CRON
crontab "$TMP"
rm -f "$TMP"

echo "Elite Unit server cron installed:"
crontab -l | grep -E 'General|self-update|cron-guard' || true
