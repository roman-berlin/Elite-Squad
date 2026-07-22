#!/bin/bash
# EU-428 AC2 — out-of-process PEER watcher for the builder host, run on the VPS.
#
# WHY: the EU-403 watchdog is a launchd agent ON THE MAC — when the Mac sleeps, the watchdog sleeps
# with it, so by construction it can never report "this host is down". The 2026-07-21 outage (~3h,
# Mac closed mid-drain) produced zero alerts. This watcher runs on the VPS — the one host that is
# always up — and watches the SYNCED peer audit (shared/mac.jsonl, the Mac's published heartbeat).
#
# WHAT IT CHECKS, each run: the age of the NEWEST EVENT in each synced peer audit (parsed from the
# `ts` INSIDE the file — NOT the file mtime: a no-op sync refreshes mtime while the event inside
# stays ancient, which is exactly how the dead heartbeat hid for 25 days). It pages ONLY when a peer
# went quiet WHILE IT HAD WORK IN FLIGHT:
#   • a drain intent RUNNING (an autopilot_start with no later autopilot_stop), OR
#   • a ticket_start with no terminal event.
# A deliberately-closed laptop with an IDLE drain must NEVER page — Roman closes it nightly, and a
# nightly false alarm trains him to ignore the channel. So: stale + work-in-flight -> alert;
# stale + idle -> silent; fresh -> silent.
#
# Threshold: PEER_STALE_SEC (default 7200s = 2h) — ~2× the 15-min sync interval + the 60-min builder
# ceiling, so a long pass plus one missed sync is not an alert. One alert per outage + a recovery
# notice on the first fresh check after an alert, same discipline as EU-403.
#
# This script is deliberately DUMB: only bash + curl + grep + awk + date — no cockpit import, no
# SDK, no higher runtime. It NEVER exits non-zero (it is timer-driven; its job is to REPORT via
# Telegram, not to signal the scheduler). The shell errexit flag is intentionally NOT set.
#
# Install (VPS cron):  */5 * * * *  /path/to/repo/scripts/peer-watchdog.sh >> ~/general-peer-watchdog.log 2>&1
# Tunables (env): PEER_DIR PEER_STALE_SEC FAIL_THRESHOLD RE_ALERT_EVERY STATE_DIR GENERAL_DIR
#                 TELEGRAM_API_URL TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID
set -uo pipefail

GENERAL_DIR="${GENERAL_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
STATE_DIR="${STATE_DIR:-$GENERAL_DIR/state}"
PEER_STALE_SEC="${PEER_STALE_SEC:-7200}"        # 2h: 2× sync interval + builder ceiling
FAIL_THRESHOLD="${FAIL_THRESHOLD:-2}"           # consecutive fails before the first alert
RE_ALERT_EVERY="${RE_ALERT_EVERY:-15}"          # re-alert every N further fails
TELEGRAM_API_URL="${TELEGRAM_API_URL:-https://api.telegram.org}"
STATE_FILE="$STATE_DIR/peer_watchdog_state"
mkdir -p "$STATE_DIR" 2>/dev/null || true

# Locate the synced peer audits. On the VPS shared/*.jsonl holds the builders' published heartbeats
# (the VPS itself is pull-only, so it never appears here). Probe the state-clone shared/ and the bare
# shared/ forms (mirror dashboard._audit_paths). PEER_DIR overrides all of this.
PEER_DIR="${PEER_DIR:-}"
if [[ -z "$PEER_DIR" ]]; then
  for d in "$GENERAL_DIR/.unit-state/shared" "$GENERAL_DIR/state/.unit-state/shared" "$GENERAL_DIR/state/shared"; do
    if [[ -d "$d" ]]; then PEER_DIR="$d"; break; fi
  done
fi

# Telegram creds: pull ONLY these two values out of .env if not already in the environment. We never
# source the whole file — the watcher stays side-effect-free (mirrors watchdog.sh).
_env_val() {
  [[ -f "$GENERAL_DIR/.env" ]] || return 0
  grep -E "^(export[[:space:]]+)?$1=" "$GENERAL_DIR/.env" 2>/dev/null | tail -n 1 \
    | sed -E "s/^(export[[:space:]]+)?$1=//" | sed "s/^\"//; s/\"$//; s/^'//; s/'$//" || true
}
: "${TELEGRAM_BOT_TOKEN:=$(_env_val TELEGRAM_BOT_TOKEN)}"
: "${TELEGRAM_CHAT_ID:=$(_env_val TELEGRAM_CHAT_ID)}"

# best-effort ISO-8601 ts -> unix epoch. GNU date first (the VPS is Linux); BSD date fallback
# (macOS/dev) parses WITH the +HHMM offset so a UTC timestamp is not re-read as local (which would
# skew the age by the dev box's TZ offset and false-alarm on a fresh peer).
_ts_epoch() {
  local ts="$1" e
  [[ -n "$ts" ]] || return 0
  e="$(date -d "$ts" +%s 2>/dev/null || true)"
  [[ -n "$e" ]] && { printf '%s' "$e"; return; }
  e="$(date -j -f "%Y-%m-%dT%H:%M:%S%z" "$ts" +%s 2>/dev/null || true)"
  [[ -n "$e" ]] && { printf '%s' "$e"; return; }
  e="$(date -j -f "%Y-%m-%dT%H:%M:%S" "${ts:0:19}" +%s 2>/dev/null || true)"
  [[ -n "$e" ]] && { printf '%s' "$e"; return; }
  e="$(date -d "${ts:0:10} ${ts:11:8}" +%s 2>/dev/null || true)"
  printf '%s' "$e"
}

_fmt_age() {
  local s="$1"
  [[ -n "$s" ]] || { echo "?"; return; }
  if [[ "$s" -lt 3600 ]]; then echo "$((s / 60))m"
  elif [[ "$s" -lt 86400 ]]; then echo "$((s / 3600))h"
  else echo "$((s / 86400))d"; fi
}

_telegram() {
  local text="$1"
  if [[ -z "${TELEGRAM_BOT_TOKEN:-}" || -z "${TELEGRAM_CHAT_ID:-}" ]]; then
    echo "peer-watchdog: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID unset — alert not sent" >&2
    return 0
  fi
  curl -s --max-time 15 \
    --data-urlencode "chat_id=$TELEGRAM_CHAT_ID" \
    --data-urlencode "text=$text" \
    "${TELEGRAM_API_URL}/bot${TELEGRAM_BOT_TOKEN}/sendMessage" >/dev/null 2>&1 || true
}

# Per peer file: newest event ts, drain-running flag, open-ticket flag. Plain awk.
# Work-in-flight = a drain that started and never stopped (last autopilot_* event was a start), OR a
# ticket whose last event is a non-terminal one (ticket_start/build/gate/review).
_peer_state() {
  awk '
    function extract(s, key,   re, m) {
      re = "\"" key "\"[ \t]*:[ \t]*\"[^\"]*\""
      if (match(s, re)) {
        m = substr(s, RSTART, RLENGTH)
        sub(/^"[^"]*"[ \t]*:[ \t]*"/, "", m); sub(/"$/, "", m)
        return m
      }
      return ""
    }
    {
      ev = extract($0, "event"); ts = extract($0, "ts")
      if (ts != "" && (maxts == "" || ts > maxts)) maxts = ts
      if (ev == "autopilot_start") lastdrain = "start"
      else if (ev == "autopilot_stop") lastdrain = "stop"
      tid = extract($0, "ticket_id")
      if (tid != "") lastkind[tid] = ev
    }
    END {
      print "TS=" maxts
      print "DRAIN=" (lastdrain == "start" ? 1 : 0)
      open = 0
      for (t in lastkind) {
        k = lastkind[t]
        if (k == "ticket_start" || k == "build" || k == "gate" || k == "review") open = 1
      }
      print "OPEN=" open
    }
  ' "$1"
}

# ── evaluate every peer; FAIL only if some peer is stale AND has work in flight ─────────
now_s="$(date +%s)"
fail_reason=""
detail=""
newest_age=""
if [[ -n "$PEER_DIR" && -d "$PEER_DIR" ]]; then
  for f in "$PEER_DIR"/*.jsonl; do
    [[ -f "$f" ]] || continue
    peer="$(basename "${f%.jsonl}")"
    out="$(_peer_state "$f")"
    ts="$(printf '%s\n' "$out" | sed -n 's/^TS=//p')"
    drain="$(printf '%s\n' "$out" | sed -n 's/^DRAIN=//p')"
    open="$(printf '%s\n' "$out" | sed -n 's/^OPEN=//p')"
    drain="${drain:-0}"; open="${open:-0}"
    age=""
    [[ -n "$ts" ]] && epoch="$(_ts_epoch "$ts")" || epoch=""
    [[ -n "$epoch" ]] && age="$((now_s - epoch))"
    # track the freshest peer's age for the OK/recovery messages
    if [[ -n "$age" ]]; then newest_age="$age"; fi
    # undatable ts (empty/foreign file) -> can't assert staleness -> never a false alarm
    if [[ -z "$ts" || -z "$age" ]]; then
      continue
    elif [[ "$age" -gt "$PEER_STALE_SEC" ]] && { [[ "$drain" == "1" ]] || [[ "$open" == "1" ]]; }; then
      detail+="${detail:+; }${peer}(age=$(_fmt_age "$age"), drain_running=${drain}, open_ticket=${open})"
      fail_reason="builder peer quiet with work in flight"
    fi
    # fresh (age <= threshold) OR stale+idle -> no fail for THIS peer
  done
fi
[[ -n "$fail_reason" ]] && fail_reason="$fail_reason — $detail"
[[ -z "$fail_reason" ]] && result="OK" || result="FAIL"

# ── load persisted state (fail_count + alerted) without sourcing the file ──────────────
fail_count=0
alerted=0
if [[ -f "$STATE_FILE" ]]; then
  fail_count="$(grep -E '^fail_count=' "$STATE_FILE" 2>/dev/null | tail -n 1 | sed 's/^fail_count=//' || true)"
  alerted="$(grep -E '^alerted=' "$STATE_FILE" 2>/dev/null | tail -n 1 | sed 's/^alerted=//' || true)"
fi
fail_count="${fail_count:-0}"
alerted="${alerted:-0}"

# ── alert / recovery decision (same shape as EU-403 watchdog.sh) ───────────────────────
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

# ── send ───────────────────────────────────────────────────────────────────────────────
if $send_alert; then
  _telegram "🚨 ELITE UNIT PEER WATCHDOG — builder host went dark mid-drain

$fail_reason
threshold:   ${PEER_STALE_SEC}s ($(_fmt_age "$PEER_STALE_SEC"))
freshest peer event age: $(_fmt_age "$newest_age")

A builder host stopped publishing while it had work in flight — it may have crashed or the host
went down mid-build. The on-host watchdog (EU-403) cannot report this: it sleeps with the host.
The cockpit + builder logs live on that host; this alert is the only signal the always-on VPS has."
fi

if $send_recovery; then
  _telegram "✅ ELITE UNIT PEER WATCHDOG — builder host recovered (publishing again)

freshest peer event age: $(_fmt_age "$newest_age")
The peer audit is fresh again; the watcher resumes silent monitoring."
fi

exit 0
