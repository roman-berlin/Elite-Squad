#!/bin/bash
# Elite Unit — server self-update (auto-deploy `main`), with a smoke-test backstop (EU-184).
#
# When origin/main moves, pull it and restart the service — the manual deploy ritual
# (`git reset --hard origin/main` + restart), automatic, so a `git push origin main` from the Mac
# reaches this 24/7 box on its own. Idempotent: no-ops unless main actually moved, so running it
# every 15 min from cron costs one `git fetch` and nothing else until you promote.
#
# EU-184 (Wave 0 backstop): before restarting into the new HEAD, SMOKE-TEST it (import the service's
# entry modules under the venv). If the new code can't even import — the class of bug that
# crash-looped the box ~864× in one minute on 2026-07-05 — we do NOT restart into it: we revert the
# tree to the last-known-good HEAD (so the live process, still running the old code, stays healthy
# and a future systemd restart is safe too) and alert. Defense-in-depth with the unit file's
# StartLimitIntervalSec/StartLimitBurst (see VPS_DEPLOYMENT.md), which bounds a RUNTIME crash-loop
# the import test can't catch.
#
# Needs passwordless sudo for `systemctl restart general.service` (see VPS_DEPLOYMENT.md sudoers).
set -uo pipefail
HERE="$HOME/General"
cd "$HERE" || exit 1
mkdir -p council
LOG="council/cron.log"

git fetch origin main --quiet 2>>"$LOG" || exit 0
OLD="$(git rev-parse HEAD 2>/dev/null)"
NEW="$(git rev-parse origin/main 2>/dev/null)"
if [ "$OLD" = "$NEW" ]; then
  exit 0   # already current — nothing to deploy
fi

echo "----- self-update $(date): ${OLD:0:8} -> ${NEW:0:8} -----" >> "$LOG"
git checkout main >> "$LOG" 2>&1
git reset --hard origin/main >> "$LOG" 2>&1

# Pick the interpreter the service actually runs under (venv if present).
PY="$HERE/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

# Smoke-test the NEW HEAD: can the service's entry modules import cleanly?
if ! SMOKE_OUT="$("$PY" -c "import orchestrator.main, orchestrator.server, orchestrator.autopilot, orchestrator.loop, orchestrator.decisions" 2>&1)"; then
  echo "self-update: SMOKE-TEST FAILED on ${NEW:0:8} — reverting to ${OLD:0:8}, NOT restarting." >> "$LOG"
  echo "$SMOKE_OUT" | tail -20 >> "$LOG"
  git reset --hard "$OLD" >> "$LOG" 2>&1
  # Best-effort Telegram alert (never blocks the revert). Load .env for the bot creds.
  [ -f "$HERE/.env" ] && set -a && . "$HERE/.env" && set +a
  if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
    MSG="⛔ self-update BLOCKED: main ${NEW:0:8} fails the import smoke-test — reverted to ${OLD:0:8}, box still on the last-good code. Fix main + re-push."
    curl -s -m 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=${MSG}" >/dev/null 2>>"$LOG" || true
  fi
  exit 1
fi

sudo systemctl restart general.service >> "$LOG" 2>&1 \
  && echo "self-update: smoke-test OK — pulled main + restarted general.service" >> "$LOG"
