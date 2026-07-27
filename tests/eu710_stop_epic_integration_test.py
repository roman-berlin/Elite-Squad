"""EU-710: Stop-controls epic — the FULL chain, end to end (runs LAST).

The epic (original EU-686) was split into siblings that each pin ONE piece:
  EU-692  the run-card stop form posts hidden app+ticket
  EU-693  /api/stop-run resolves the EXACT named run
  EU-695/EU-706  a stop click with no live run shows a visible message
  EU-705  with 2+ concurrent runs, a card's Stop stops ONLY its own run
  EU-689  the toolbar Stop names its real consequence (autopilot off)
  EU-709  a drain keeps the hard-stop control next to the 'Stopping…' chip
  EU-710  (this ticket, AC2) a confirmed stop flips the run card's story at once:
          'Working · <phase>' → amber 'Stopping — finishing the current step · <phase>',
          computed in render_board from the slot's stop_event, cleared by release_run.

This harness is the integration check none of those pieces covers alone: it drives the
CHAIN through the real surfaces — server-rendered board HTML (GET /api/board), the real
/api/stop-run handler, the real control bar — instead of hand-built fragments.

Checks:
  A (AC2) working→stopping→cleared, through warroom.render_board:
          a live card says 'Working · Build'; once the slot's stop_event fires the SAME
          render flips to the amber 'Stopping — finishing the current step' chip; after
          release_run (+ a terminal audit row) the card goes idle and the chip is gone.
  B (AC1) per-card targeting with 2+ concurrent runs, full chain: two real runs claimed
          via /api/run-selected; each server-rendered board carries a stop form naming its
          OWN app+ticket; POSTing exactly card A's form sets ONLY A's event; A's next board
          render shows the stopping chip while B's still says 'Working'.
  C (AC1) a stop click with no live run surfaces the visible 'No active run found to stop'
          warning on the page (EU-695/EU-706), never a silent redirect.
  D (AC3/4) during a drain the control bar keeps BOTH the autopilot Stop and the hard-stop
          form (naming the in-flight ticket); clicking hard-stop signals the run's event;
          and the drain's run card shows the AC2 chip with NO per-card stop form (EU-692
          AC3: autopilot-run cards stop from the control bar, not the card).
"""
import html
import json
import re
import sys
import tempfile
import threading
import time
import types
from datetime import datetime, timezone
from pathlib import Path

# Minimal stub so orchestrator imports succeed without the real SDK.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

sys.path.insert(0, ".")

import orchestrator.server as srv
from orchestrator import autopilot as _autopilot_mod
from orchestrator import cockpit_state, warroom
from orchestrator import cockpit_views as V
from orchestrator.config import AppConfig, Config

# Isolate from any AMBIENT autopilot daemon on the machine running the test: a live
# PID file would flip ap_on → manual=False and (correctly, in production) suppress the
# manual run-card stop form this harness asserts on. The harness owns all run state.
_autopilot_mod.daemon_running = lambda: False
_autopilot_mod.daemon_is_external = lambda: False

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


# --- shared fixtures ----------------------------------------------------------

started: list = []
started_lock = threading.Lock()
release = threading.Event()


async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    with started_lock:
        started.append(worklist)
    release.wait(20)   # safety timeout only — every phase drains via release.set()


srv.health.summary = lambda c: {"healthy": True, "checks": []}
srv.intake.from_tickets = lambda rcfg, app, keys: [f"wl:{app}:{len(keys)}"]
srv.run_loop = fake_run_loop

d = Path(tempfile.mkdtemp())
AUDIT = d / "audit.jsonl"
AUDIT.write_text("", encoding="utf-8")
cfg = Config(
    apps=[AppConfig(name="alpha", repo_path=str(d), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none"),
          AppConfig(name="beta", repo_path=str(d), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(AUDIT), use_worktree=False)

cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)   # unlimited — we WANT concurrent runs
client = srv.create_app(cfg).test_client()


def _now_ts() -> str:
    """A fresh audit timestamp in the exact format dashboard._parse_ts accepts."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def _reset(audit: bool = True) -> None:
    """Blank run-state between sections; optionally truncate the audit too (the
    dashboard cache keys on (size, mtime_ns), so a rewrite invalidates it)."""
    global release
    release.set()
    for _ in range(100):
        if not any(cockpit_state.is_active(a) for a in ("alpha", "beta")):
            break
        time.sleep(0.05)
    cockpit_state.reset_run_state()
    cockpit_state.set_max_parallel_runs(0)
    started.clear()
    release = threading.Event()
    if audit:
        AUDIT.write_text("", encoding="utf-8")


def _audit_live(ticket: str, app: str) -> None:
    """Append audit rows for a genuinely-live run in its Build phase: a fresh
    ticket_start + build event, no terminal outcome — exactly what live_runs()
    needs to render a live card for this app/ticket on the board."""
    ts = _now_ts()
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "ticket_start", "ticket_id": ticket, "app": app,
                            "branch": f"eu710/{ticket.lower()}", "ts": ts}) + "\n")
        f.write(json.dumps({"event": "build", "ticket_id": ticket, "app": app, "ts": ts,
                            "iteration": 1, "turns": 4, "cost_usd": 0.0,
                            "summary": "", "tools": []}) + "\n")


def _audit_terminal(ticket: str, app: str) -> None:
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "ticket_exception", "ticket_id": ticket, "app": app,
                            "ts": _now_ts(), "error": "test teardown"}) + "\n")


def _slot(app: str) -> dict:
    return cockpit_state.get_state(app)


def _runsub(board: str) -> str:
    """The card's runsub div — the exact slot the Working/Stopping line renders in."""
    m = re.search(r'<div class=runsub>(.*?)</div>', board, re.S)
    return m.group(1) if m else ""


def _card_form(board: str) -> tuple[str | None, str | None]:
    """The (app, ticket) a board's run-card stop form would POST — extracted from the
    HTML the form actually emits, entity-unescaped the way a browser decodes it."""
    frag = board.split("action=/api/stop-run", 1)[1].split("</form>", 1)[0] \
        if "action=/api/stop-run" in board else ""
    # The warroom card form emits QUOTED attribute names (name="app") — unlike the
    # control bar's unquoted forms — so match the card's actual syntax (EU-692).
    ma = re.search(r'name="app" value="([^"]*)"', frag)
    mt = re.search(r'name="ticket" value="([^"]*)"', frag)
    return (html.unescape(ma.group(1)) if ma else None,
            html.unescape(mt.group(1)) if mt else None)


def _wait_started(n: int) -> None:
    for _ in range(100):
        with started_lock:
            if len(started) >= n:
                return
        time.sleep(0.05)


# ═══════════════════════════════════════════════════════════════════════════════
# A — AC2: the run card flips Working → Stopping the moment the stop fires,
#     and release_run clears it. Through warroom.render_board, the real renderer.
# ═══════════════════════════════════════════════════════════════════════════════
_reset()
_audit_live("EU-710A", "alpha")
st = _slot("alpha")
st["active"] = True
st["dry_run"] = False
ev = threading.Event()
st["stop_event"] = ev          # bound but NOT set — a run working normally

b1 = warroom.render_board(cfg, "alpha", st)
sub1 = _runsub(b1)
chk("A1: live card says 'Working · Build' before any stop",
    "Working &middot; <b>Build</b>" in sub1, f"runsub={sub1[:200]!r}")
chk("A2: no stopping chip while the event is unset",
    "runstopping" not in b1, "")

ev.set()                       # the confirmed stop — manual card-Stop or autopilot drain
b2 = warroom.render_board(cfg, "alpha", st)
sub2 = _runsub(b2)
chk("A3: the SAME card now shows the amber 'Stopping — finishing the current step' chip",
    "class=runstopping" in sub2
    and "Stopping &mdash; finishing the current step" in sub2,
    f"runsub={sub2[:300]!r}")
chk("A4: the current phase stays visible beside the chip",
    "&middot; <b>Build</b>" in sub2, f"runsub={sub2[:300]!r}")
chk("A5: 'Working' is gone from the runsub line",
    "Working &middot;" not in sub2, f"runsub={sub2[:300]!r}")

# AC2 close: 'cleared in release_run' — the run ends, the slot is released, and the
# chip can never survive into the idle 'last run' header. A terminal audit row lets
# the board see the run is over (without it the fresh audit would read as in-flight).
_audit_terminal("EU-710A", "alpha")
cockpit_state.release_run("alpha")
b3 = warroom.render_board(cfg, "alpha", st)
chk("A6: release_run clears the chip (stop_event zeroed)",
    "runstopping" not in b3 and "Stopping &mdash;" not in b3, "")
chk("A7: the card went idle — 'last run', not a live header",
    "runlive" not in b3 and ">errored<" in b3, "")


# ═══════════════════════════════════════════════════════════════════════════════
# B — AC1: 2+ concurrent runs, full chain — server-rendered boards, real handler.
#     Each card names its OWN run; stopping A flips A's card, never B's.
# ═══════════════════════════════════════════════════════════════════════════════
_reset()
client.post("/api/run-selected", data={"app": "alpha", "ticket": "EU-710B"})
client.post("/api/run-selected", data={"app": "beta", "ticket": "EU-710C"})
_wait_started(2)
_audit_live("EU-710B", "alpha")
_audit_live("EU-710C", "beta")
chk("B1: two concurrent runs claimed (distinct slots)",
    cockpit_state.is_active("alpha") and cockpit_state.is_active("beta")
    and _slot("alpha").get("stop_event") is not _slot("beta").get("stop_event"))

ba = client.get("/api/board?app=alpha").get_data(as_text=True)
bb = client.get("/api/board?app=beta").get_data(as_text=True)
fa, ta = _card_form(ba)
fb, tb = _card_form(bb)
chk("B2: card A's server-rendered stop form names A's own run",
    (fa, ta) == ("alpha", "EU-710B"), f"form posts app={fa!r} ticket={ta!r}")
chk("B3: card B's server-rendered stop form names B's own run",
    (fb, tb) == ("beta", "EU-710C"), f"form posts app={fb!r} ticket={tb!r}")
chk("B4: both boards say 'Working' before any stop",
    "Working &middot; <b>Build</b>" in _runsub(ba)
    and "Working &middot; <b>Build</b>" in _runsub(bb)
    and "runstopping" not in ba and "runstopping" not in bb)

# Click card A's Stop: POST EXACTLY what its rendered form emits.
r = client.post("/api/stop-run", data={"app": fa, "ticket": ta})
chk("B5: clicking card A's Stop redirects (click completed)",
    r.status_code in (302, 303), f"status={r.status_code}")
chk("B6: card A's click sets ONLY run A's stop_event",
    _slot("alpha")["stop_event"].is_set()
    and not _slot("beta")["stop_event"].is_set(),
    "card A must never touch run B's event")

ba2 = client.get("/api/board?app=alpha").get_data(as_text=True)
bb2 = client.get("/api/board?app=beta").get_data(as_text=True)
chk("B7: A's next board render shows the stopping chip (full chain)",
    "class=runstopping" in _runsub(ba2)
    and "Stopping &mdash; finishing the current step" in _runsub(ba2),
    f"runsub={_runsub(ba2)[:300]!r}")
chk("B8: B's board STILL says 'Working' — the sibling run is untouched",
    "Working &middot; <b>Build</b>" in _runsub(bb2) and "runstopping" not in bb2,
    f"runsub={_runsub(bb2)[:300]!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# C — AC1: a stop click with NO live run must surface a visible warning, never a
#     silent redirect (EU-695/EU-706), and the page actually renders it.
# ═══════════════════════════════════════════════════════════════════════════════
_reset()
r = client.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-999"})
chk("C1: no-run stop still redirects", r.status_code in (302, 303),
    f"status={r.status_code}")
msg = _slot("alpha").get("last_msg") or ""
chk("C2: a visible warning names the target the click asked for",
    "No active run found to stop for alpha/EU-999" in msg, f"last_msg={msg!r}")
# The control bar is the sanctioned surface for per-app last_msg (EU-646) — prove
# the operator actually SEES it on the page, not just in state.
bar = V._control_bar(cfg, "alpha", True)
chk("C3: the control bar renders the warning (operator sees it on the page)",
    "No active run found to stop for alpha/EU-999" in bar, "")


# ═══════════════════════════════════════════════════════════════════════════════
# D — AC3/AC4: a drain keeps BOTH stop controls, hard-stop reaches the run's
#     event, and the draining run's card carries the AC2 chip (no card form).
# ═══════════════════════════════════════════════════════════════════════════════
_reset()
_audit_live("EU-710D", "alpha")


class _FakeStopEvent:
    """is_set() always True (genuine drain); set() counted so the hard-stop click
    proves the handler reached THIS slot's event."""
    def __init__(self) -> None:
        self.sets = 0

    def is_set(self) -> bool:
        return True

    def set(self) -> None:
        self.sets += 1


st = _slot("alpha")
st["active"] = True
st["autopilot_on"] = True
st["run_tickets"] = ["EU-710D"]
fake_ev = _FakeStopEvent()
st["stop_event"] = fake_ev

bar = V._control_bar(cfg, "alpha", True)
chk("D1: drain renders the 'Stopping…' chip (not a collapsed passive bar)",
    'class="tbap stopping"' in bar and "Stopping" in bar, bar[:400])
frag = bar.split("action=/api/stop-run", 1)[1].split("</form>", 1)[0] \
    if "action=/api/stop-run" in bar else ""
d_app = re.search(r'name=app value="([^"]*)"', frag)
d_tkt = re.search(r'name=ticket value="([^"]*)"', frag)
chk("D2: the hard-stop form survives the drain, naming the in-flight run",
    bool(frag) and d_app and html.unescape(d_app.group(1)) == "alpha"
    and d_tkt and html.unescape(d_tkt.group(1)) == "EU-710D",
    f"app={d_app.group(1) if d_app else None!r} ticket={d_tkt.group(1) if d_tkt else None!r}")
chk("D3: the EU-689 autopilot Stop still sits alongside it",
    "action=/api/autopilot" in bar and "action value=stop" in bar
    and "Stop</button>" in bar, "")

r = client.post("/api/stop-run",
                data={"app": html.unescape(d_app.group(1)),
                      "ticket": html.unescape(d_tkt.group(1))})
chk("D4: clicking hard-stop mid-drain signals the run's own event",
    r.status_code in (302, 303) and fake_ev.sets == 1,
    f"status={r.status_code} set()x{fake_ev.sets}")

bd = warroom.render_board(cfg, "alpha", st)
chk("D5: the draining run's card shows the AC2 'Stopping' chip",
    "class=runstopping" in _runsub(bd)
    and "Stopping &mdash; finishing the current step" in _runsub(bd),
    f"runsub={_runsub(bd)[:300]!r}")
chk("D6: an autopilot run's card carries NO per-card stop form (EU-692 AC3 intact)",
    "class=stoprun" not in bd, "")

_reset()

# ── report ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 67)
print("  EU-710: STOP-CONTROLS EPIC — END-TO-END INTEGRATION")
print("  " + "─" * 63)
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{extra}")
print("  " + "─" * 63)
print(f"  {passed}/{len(results)} checks passed")
print("  RESULT:", "ALL GREEN" if passed == len(results)
      else f"{len(results) - passed} FAILED")
sys.exit(0 if passed == len(results) else 1)
