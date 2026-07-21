#!/bin/bash
# EU-403 — out-of-process watchdog for the Elite Unit cockpit.
#
# WHY: every heartbeat lives INSIDE the serve process, so a wedged-but-alive process
# (deadlock, hung HTTP, stuck event loop) — or a host that simply slept — is invisible
# until a human opens the cockpit. The 2026-07-20 16:23 -> 23:05 gap (host down 6.7h)
# produced zero alerts. This script runs as a SEPARATE launchd agent (or cron job),
# completely outside the serve process, so it cannot share its failure modes. It is
# deliberately DUMB: only bash + curl + grep + stat — no Python, no cockpit import, no
# SDK. If the serve process wedges, this independent process still runs and pages.
#
# WHAT IT CHECKS, each run:
#   1. HTTP probe — curl the cockpit /api/health with a hard --max-time. A dead host or a
#      wedged/dead serve process cannot answer (curl times out / is refused) -> failure.
#      This is the primary death + full-process-wedge detector.
#   2. Audit freshness — IF a drain is RUNNING (state/autopilot_intent.json), the audit log
#      (state/audit.jsonl) must carry a recent event. A drain that is RUNNING but has
#      produced no audit event in STALE_SEC is wedged past the HTTP layer (the cockpit still
#      answers /api/health on a worker thread while the drain loop is stuck). An IDLE serve
#      (no drain RUNNING) never fails here — a stale audit is the normal idle state.
#
# ALERTING: on FAIL_THRESHOLD (default 2) consecutive failures -> ONE Telegram alert
#   carrying the last audit event and its age. On the first SUCCESS after an alert -> ONE
#   recovery notice. A re-alert is sent every RE_ALERT_EVERY further failures so a single
#   missed ping at 3am is not the only signal. State (consecutive-fail count + alerted flag)
#   persists in $STATE_DIR/watchdog_state across runs.
#
# This script NEVER exits non-zero: it is timer-driven, and its job is to REPORT via
# Telegram, not to signal the scheduler. (A non-zero exit under a periodic timer would only
# spam the system log.) A failed probe is a reportable condition, not a script abort, so the
# shell errexit flag is intentionally NOT set (a failing curl would otherwise abort the run).
#
# Install:   bash scripts/install-mac-watchdog-daemon.sh            (Mac launchd agent)
# VPS cron:  */4 * * * *  /path/to/repo/scripts/watchdog.sh >> ~/.log/general-watchdog.log 2>&1
#
# Tunables (env, all optional): COCKPIT_URL STATE_DIR AUDIT_FILE INTENT_FILE HEALTH_TIMEOUT
#   STALE_SEC FAIL_THRESHOLD RE_ALERT_EVERY TELEGRAM_API_URL TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
set -uo pipefail

GENERAL_DIR="${GENERAL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DIR="${STATE_DIR:-$GENERAL_DIR/state}"
COCKPIT_URL="${COCKPIT_URL:-http://127.0.0.1:8787/api/health}"
AUDIT_FILE="${AUDIT_FILE:-$STATE_DIR/audit.jsonl}"
INTENT_FILE="${INTENT_FILE:-$STATE_DIR/autopilot_intent.json}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-8}"        # curl --max-time: bounds a STOP'd/hung serve
STALE_SEC="${STALE_SEC:-5400}"               # 90 min: above the 60-min builder-pass ceiling so a legit long pass never false-alarms
FAIL_THRESHOLD="${FAIL_THRESHOLD:-2}"        # consecutive failures before the first alert
RE_ALERT_EVERY="${RE_ALERT_EVERY:-15}"       # re-alert every N further fails (15 * 4min ~= hourly)
TELEGRAM_API_URL="${TELEGRAM_API_URL:-https://api.telegram.org}"
STATE_FILE="$STATE_DIR/watchdog_state"
mkdir -p "$STATE_DIR" 2>/dev/null || true

# Telegram creds: pull ONLY these two values out of .env if they are not already in the
# environment. We never source the whole file — the watchdog stays side-effect-free.
_env_val() {
  [[ -f "$GENERAL_DIR/.env" ]] || return 0
  # 2026-07-21: accept the `export KEY=value` form too. The live .env declares every secret with
  # an `export ` prefix (it is sourced by the `general` wrapper), so the bare `^KEY=` pattern
  # matched nothing and the watchdog installed MUTE — it detected the outage, crossed the
  # threshold, set alerted=1, then printed "TELEGRAM_* unset — alert not sent". An alarm that
  # cannot ring is worse than no alarm, because it is trusted.
  grep -E "^(export[[:space:]]+)?$1=" "$GENERAL_DIR/.env" 2>/dev/null | tail -n 1 \
    | sed -E "s/^(export[[:space:]]+)?$1=//" | sed "s/^\"//; s/\"$//; s/^'//; s/'$//" || true
}
: "${TELEGRAM_BOT_TOKEN:=$(_env_val TELEGRAM_BOT_TOKEN)}"
: "${TELEGRAM_CHAT_ID:=$(_env_val TELEGRAM_CHAT_ID)}"

# epoch mtime of a file (BSD/macOS stat -f %m vs GNU stat -c %Y)
_file_mtime() {
  if [[ "$(uname)" == "Darwin" ]]; then stat -f %m "$1" 2>/dev/null || true
  else stat -c %Y "$1" 2>/dev/null || true; fi
}

# best-effort "key":"value" extraction (no jq dependency)
_json_str() {
  printf '%s' "$1" | grep -o "\"$2\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -n 1 \
    | sed "s/.*\"$2\"[[:space:]]*:[[:space:]]*\"//; s/\"$//" || true
}
_last_audit_line() {
  [[ -f "$AUDIT_FILE" ]] || { echo ""; return; }
  tail -n 50 "$AUDIT_FILE" 2>/dev/null | grep -v '^[[:space:]]*$' | tail -n 1 || true
}

_telegram() {
  local text="$1"
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "watchdog: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset — alert not sent" >&2
    return 0
  fi
  curl -s -m 15 \
    --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" \
    --data-urlencode "text=$text" \
    "${TELEGRAM_API_URL}/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null 2>&1 || true
}

# ── check 1: HTTP probe ─────────────────────────────────────────────────────
fail_reason=""
health_status="?"
body="$(mktemp 2>/dev/null || echo "/tmp/wd_$$")"
http_code="$(curl -s -m "$HEALTH_TIMEOUT" -o "$body" -w '%{http_code}' "$COCKPIT_URL" 2>/dev/null || true)"
rm -f "$body" 2>/dev/null || true
if [[ "$http_code" == "200" ]]; then
  health_status="ok (HTTP 200)"
else
  fail_reason="cockpit health endpoint unreachable (HTTP ${http_code:-000})"
fi

# ── check 2: audit freshness (only meaningful while a drain is RUNNING) ──────
drain_running=false
if [[ -f "$INTENT_FILE" ]] \
   && grep -q '"state"[[:space:]]*:[[:space:]]*"RUNNING"' "$INTENT_FILE" 2>/dev/null; then
  drain_running=true
fi
audit_age_s=""
if $drain_running && [[ -f "$AUDIT_FILE" ]]; then
  mtime_s="$(_file_mtime "$AUDIT_FILE")"
  if [[ -n "$mtime_s" ]]; then
    audit_age_s=$(( $(date +%s) - mtime_s ))
    if [[ "$audit_age_s" -gt "$STALE_SEC" ]] && [[ -z "$fail_reason" ]]; then
      fail_reason="drain RUNNING but audit.jsonl is stale (${audit_age_s}s > ${STALE_SEC}s threshold)"
    fi
  fi
fi

if [[ -z "$fail_reason" ]]; then result="OK"; else result="FAIL"; fi

# ── load persisted state (fail_count + alerted) without sourcing the file ───
fail_count=0
alerted=0
if [[ -f "$STATE_FILE" ]]; then
  fail_count="$(grep -E '^fail_count=' "$STATE_FILE" 2>/dev/null | tail -n 1 | sed 's/^fail_count=//' || true)"
  alerted="$(grep -E '^alerted=' "$STATE_FILE" 2>/dev/null | tail -n 1 | sed 's/^alerted=//' || true)"
fi
fail_count="${fail_count:-0}"
alerted="${alerted:-0}"

# ── alert / recovery decision ───────────────────────────────────────────────
send_alert=false
send_recovery=false
if [[ "$result" == "OK" ]]; then
  if [[ "$alerted" == "1" ]]; then send_recovery=true; fi
  fail_count=0
  alerted=0
else
  fail_count=$(( fail_count + 1 ))
  if [[ "$fail_count" -ge "$FAIL_THRESHOLD" ]]; then
    if [[ "$alerted" != "1" ]] || [[ $(( fail_count % RE_ALERT_EVERY )) -eq 0 ]]; then
      send_alert=true
      alerted=1
    fi
  fi
fi

printf 'fail_count=%s\nalerted=%s\n' "$fail_count" "$alerted" > "$STATE_FILE" 2>/dev/null || true

# ── send ────────────────────────────────────────────────────────────────────
if $send_alert; then
  last_line="$(_last_audit_line)"
  last_ts="$(_json_str "$last_line" ts)"
  last_event="$(_json_str "$last_line" event)"
  age_txt="unknown"
  if [[ -n "$audit_age_s" ]]; then
    age_txt="$(( audit_age_s / 60 )) min ago"
  elif [[ -f "$AUDIT_FILE" ]]; then
    m="$(_file_mtime "$AUDIT_FILE")"
    [[ -n "$m" ]] && age_txt="$(( ( $(date +%s) - m ) / 60 )) min ago"
  fi
  drain_txt="none (idle)"
  $drain_running && drain_txt="RUNNING"
  _telegram "🚨 ELITE UNIT WATCHDOG — cockpit unreachable or wedged

reason:       $fail_reason
cockpit:      $COCKPIT_URL
health probe: ${http_code:-no-response}
drain intent: $drain_txt
last audit:   event=${last_event:-(unknown)}  ts=${last_ts:-(unknown)}
audit age:    $age_txt

Two consecutive checks failed — the serve process may be wedged or the host is down.
See ~/Library/Logs/General/cockpit.log and the cockpit keepalive."
fi

if $send_recovery; then
  drain_txt="none (idle)"
  $drain_running && drain_txt="RUNNING"
  _telegram "✅ ELITE UNIT WATCHDOG — cockpit recovered

health: $health_status
drain intent: $drain_txt
The cockpit is reachable again; the watchdog resumes silent monitoring."
fi

exit 0
