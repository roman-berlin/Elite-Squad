"""EU-687: a CONFIRMED stop persists as ``st['stopping']`` on the run's state dict.

EU-710 gave the run card its amber 'Stopping — finishing the current step' chip, computed
at render time from the slot's ``stop_event.is_set()``. EU-687 pins the STATE side of that
contract: the cockpit's confirmed-stop paths set a durable ``stopping`` flag on the run's
state dict in the same instant as ``stop_event.set()``, and ``release_run`` clears it — so
the chip is driven by persisted state and dies exactly when the run releases, never lingering
on the idle 'last run' card.

Checks:
  A  schema: ``stopping`` is a first-class run-state key (``_STATE_KEYS`` + ``_new_state()``
     default False) — the flag the ticket names actually exists on every fresh slot.
  B  set on stop-confirm (full chain through the real /api/stop-run handler): a live run's
     slot has the flag False and its card says 'Working'; POSTing the card's own stop form
     sets ``st['stopping']`` True at once, and the server-rendered board shows the amber chip
     immediately — no checkpoint tick in between. A stop click with NO live run sets nothing.
  C  cleared on release: ``release_run`` resets the flag to False and the card returns to
     idle — the chip is gone from the next render.
  D  defense: ``claim_run`` resets a stale True so a run that died without releasing can
     never open its successor's card on the 'Stopping' chip.
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
from orchestrator.config import AppConfig, Config

# Isolate from any AMBIENT autopilot daemon on the machine running the test: a live
# PID file would flip ap_on → manual=False and suppress the manual run-card stop form
# this harness drives. The harness owns all run state.
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
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(AUDIT), use_worktree=False)

cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)
client = srv.create_app(cfg).test_client()


def _now_ts() -> str:
    """A fresh audit timestamp in the exact format dashboard._parse_ts accepts."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")


def _reset() -> None:
    """Blank run-state between sections and truncate the audit."""
    global release
    release.set()
    for _ in range(100):
        if not cockpit_state.is_active("alpha"):
            break
        time.sleep(0.05)
    cockpit_state.reset_run_state()
    cockpit_state.set_max_parallel_runs(0)
    started.clear()
    release = threading.Event()
    AUDIT.write_text("", encoding="utf-8")
    # The audit is APPEND-ONLY in production, so dashboard's EU-345 incremental reader
    # caches consumed lines per path and trusts an anchor match as "appended, not
    # rewritten". This harness REWRITES the same path each section with near-identical
    # bytes (same events, same-second timestamps) — a corner the anchor can't tell from
    # a genuine append, which splices the old section's lines onto the new one and reads
    # a finished run as in-flight. Production never rewrites its audit, so the right fix
    # is here: drop the read caches along with the file contents.
    from orchestrator import dashboard as _D
    _D._file_line_cache.clear()
    _D._audit_cache.clear()
    _D._tasks_cache.clear()


def _audit_live(ticket: str, app: str) -> None:
    """Audit rows for a genuinely-live run in its Build phase: a fresh ticket_start +
    build event, no terminal outcome — what live_runs() needs to render a live card."""
    ts = _now_ts()
    with AUDIT.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event": "ticket_start", "ticket_id": ticket, "app": app,
                            "branch": f"eu687/{ticket.lower()}", "ts": ts}) + "\n")
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
# A — schema: 'stopping' is a first-class run-state key, default False.
# ═══════════════════════════════════════════════════════════════════════════════
_reset()
fresh = cockpit_state._new_state()
chk("A1: _new_state() carries 'stopping', default False",
    "stopping" in fresh and fresh["stopping"] is False, f"keys={sorted(fresh)}")
chk("A2: _STATE_KEYS names 'stopping' (schema doc is in sync)",
    "stopping" in cockpit_state._STATE_KEYS, str(cockpit_state._STATE_KEYS))


# ═══════════════════════════════════════════════════════════════════════════════
# B — set on stop-confirm, through the real /api/stop-run handler: the flag flips
#     the INSTANT the click confirms, and the very next board render carries the
#     amber chip — no checkpoint tick in between.
# ═══════════════════════════════════════════════════════════════════════════════
_reset()
client.post("/api/run-selected", data={"app": "alpha", "ticket": "EU-687A"})
_wait_started(1)
_audit_live("EU-687A", "alpha")
st = _slot("alpha")
chk("B1: the run claimed its slot with the flag False",
    st.get("active") and st.get("stopping") is False,
    f"active={st.get('active')} stopping={st.get('stopping')}")

b0 = client.get("/api/board?app=alpha").get_data(as_text=True)
fa, ta = _card_form(b0)
chk("B2: before any stop the card says 'Working' and shows its stop form",
    "Working &middot; <b>Build</b>" in _runsub(b0) and "runstopping" not in b0
    and (fa, ta) == ("alpha", "EU-687A"),
    f"form=({fa!r},{ta!r}) runsub={_runsub(b0)[:200]!r}")

# Click the card's Stop: POST exactly what its rendered form emits.
r = client.post("/api/stop-run", data={"app": fa, "ticket": ta})
chk("B3: the stop click confirms (redirect) and sets st['stopping'] AT ONCE",
    r.status_code in (302, 303) and st.get("stopping") is True,
    f"status={r.status_code} stopping={st.get('stopping')}")
chk("B4: the confirmed stop also set the slot's stop_event (flag travels with it)",
    st.get("stop_event") is not None and st["stop_event"].is_set(), "")

b1 = client.get("/api/board?app=alpha").get_data(as_text=True)
sub1 = _runsub(b1)
chk("B5: the next board render shows the amber 'Stopping — finishing the current step' chip",
    "class=runstopping" in sub1 and "Stopping &mdash; finishing the current step" in sub1,
    f"runsub={sub1[:300]!r}")
chk("B6: 'Working' is gone from the runsub line, the phase stays beside the chip",
    "Working &middot;" not in sub1 and "&middot; <b>Build</b>" in sub1,
    f"runsub={sub1[:300]!r}")

# The flag is driven by PERSISTED state, not only the event: hide the event and the
# chip must still render from st['stopping'] alone (render_board ORs the two).
st_ev = st.get("stop_event")
st["stop_event"] = None
try:
    b1b = warroom.render_board(cfg, "alpha", st)
    chk("B7: the chip renders from the persisted flag even with no readable event",
        "class=runstopping" in _runsub(b1b), f"runsub={_runsub(b1b)[:300]!r}")
finally:
    st["stop_event"] = st_ev

# A stop click with NO live run confirms nothing — the flag must stay False.
_reset()
r = client.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-999"})
chk("B8: an unconfirmed stop (no live run) leaves st['stopping'] False",
    r.status_code in (302, 303) and _slot("alpha").get("stopping") is False,
    f"status={r.status_code} stopping={_slot('alpha').get('stopping')}")


# ═══════════════════════════════════════════════════════════════════════════════
# C — cleared on release: release_run resets the flag and the card goes idle —
#     the chip can never survive into the 'last run' header.
# ═══════════════════════════════════════════════════════════════════════════════
_reset()
client.post("/api/run-selected", data={"app": "alpha", "ticket": "EU-687C"})
_wait_started(1)
_audit_live("EU-687C", "alpha")
st = _slot("alpha")
client.post("/api/stop-run", data={"app": "alpha", "ticket": "EU-687C"})
chk("C1: pre-release the flag is True (confirmed stop)", st.get("stopping") is True,
    f"stopping={st.get('stopping')}")

_audit_terminal("EU-687C", "alpha")   # let the board see the run is over
cockpit_state.release_run("alpha")
chk("C2: release_run clears st['stopping']", st.get("stopping") is False,
    f"stopping={st.get('stopping')}")

b2 = client.get("/api/board?app=alpha").get_data(as_text=True)
chk("C3: the card returned to idle — chip gone, no live header",
    "runstopping" not in b2 and "Stopping &mdash;" not in b2
    and "runlive" not in b2, f"runsub={_runsub(b2)[:200]!r}")


# ═══════════════════════════════════════════════════════════════════════════════
# D — defense: claim_run resets a stale True so a crashed-without-release run can
#     never open its successor's card on the 'Stopping' chip.
# ═══════════════════════════════════════════════════════════════════════════════
_reset()
st = _slot("alpha")
st["stopping"] = True   # simulate a stale flag from a run that never released
chk("D1: claim_run resets a stale 'stopping' flag",
    cockpit_state.claim_run("alpha") and st.get("stopping") is False,
    f"stopping={st.get('stopping')}")
cockpit_state.release_run("alpha")
chk("D2: release_run leaves the freshly-claimed slot's flag False too",
    st.get("stopping") is False, f"stopping={st.get('stopping')}")

_reset()

# ── report ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 67)
print("  EU-687: 'stopping' STATE FLAG — set on confirm, cleared on release")
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
