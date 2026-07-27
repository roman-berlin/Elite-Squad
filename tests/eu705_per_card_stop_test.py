"""EU-705: per-card stop targeting — each run-card's Stop stops ONLY its own run.

Round-trip coverage the sibling tickets left uncovered: EU-692 pins the form HTML in
isolation, EU-693/EU-695 pin the handler against HAND-BUILT POST data. This harness
closes the loop end to end — it renders REAL run cards via warroom._run_html (the same
call the warroom's multi-card board path makes), extracts the hidden app/ticket values
from the HTML each card's stop form ACTUALLY emits (entities unescaped, exactly as a
browser decodes them), POSTs precisely those values to /api/stop-run, and asserts that
only the clicked card's run sees its stop_event set.

Checks:
  AC1  Two concurrent runs on different apps: clicking Stop on card A (posting what its
       form emits) sets ONLY run A's stop_event — run B keeps running. Card B's own
       click then stops B.
  AC2  Exactly one active run: its card's Stop still stops it (no regression), and the
       legacy empty POST's sole-stoppable-run fallback still fires for old callers.
  AC3  A STALE card — one rendered for the run that USED to hold the app's slot — stops
       nothing: the handler refuses to kill the unrelated run now holding it (the
       original EU-705 bug was silently stopping the wrong run, or none, with 2+ live).
"""
import html
import re
import sys, types, tempfile, threading, time
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.server as srv
from orchestrator import cockpit_state, warroom
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), str(d)))

# --- stubs: healthy gate; a run_loop that BLOCKS until released so runs stay live at once. ---
started = []
started_lock = threading.Lock()
release = threading.Event()
async def fake_run_loop(rcfg, worklist, audit, stop_event=None):
    with started_lock:
        started.append(worklist)
    release.wait(20)   # safety timeout only — every phase drains via release.set()
srv.health.summary = lambda c: {"healthy": True, "checks": []}
srv.intake.from_tickets = lambda rcfg, app, keys: [f"wl:{app}:{len(keys)}"]
srv.run_loop = fake_run_loop

d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
def _app(name):
    return AppConfig(name=name, repo_path=str(d), base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[_app("alpha"), _app("beta")],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)

cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)   # unlimited — we WANT concurrent runs

client = srv.create_app(cfg).test_client()

def _start(app, tickets):
    client.post("/api/run-selected", data={"app": app, "ticket": tickets})

def _wait_started(n):
    for _ in range(100):
        with started_lock:
            if len(started) >= n:
                return
        time.sleep(0.05)

def _ev(app):
    return cockpit_state.get_state(app).get("stop_event")

def _drain_and_reset():
    global release
    release.set()
    for _ in range(100):
        if not any(cockpit_state.is_active(a) for a in ("alpha", "beta")):
            break
        time.sleep(0.05)
    cockpit_state.reset_run_state()
    started.clear()
    release = threading.Event()

def _card_run(app, ticket):
    """A minimal live run object for one card — same shape the multi-card board path feeds
    warroom._run_html (per-ticket card, own app + own current ticket)."""
    return {"live": True, "ticket": ticket, "app": app, "branch": f"eu705-{ticket}",
            "passes": 1, "verdict": "", "outcome": "running", "cost": 0,
            "phases": ["Build", "Gate", "Review", "Land"], "reached": 0,
            "failed_phase": None, "sparkline": [], "started": "2026-07-27T12:00:00+0300"}

def _card_form(app, ticket):
    """Render this card exactly as the warroom does and return the (app, ticket) its Stop
    form would POST — extracted from the HTML the form actually emits, entity-unescaped
    the way a browser decodes it before submitting."""
    h = warroom._run_html(_card_run(app, ticket), mode="live", elapsed="1m 00s", manual=True)
    assert "class=stopbtn" in h, "card rendered no Stop form at all"
    ma = re.search(r'<input type="hidden" name="app" value="([^"]*)"', h)
    mt = re.search(r'<input type="hidden" name="ticket" value="([^"]*)"', h)
    assert ma and mt, "card stop form is missing its hidden app/ticket inputs"
    return html.unescape(ma.group(1)), html.unescape(mt.group(1))

def _click_stop(app, ticket):
    """Simulate a literal click on THIS card's Stop button: POST exactly what its form emits."""
    form_app, form_ticket = _card_form(app, ticket)
    return client.post("/api/stop-run", data={"app": form_app, "ticket": form_ticket})


# ═══════════════════════════════════════════════════════════════════════════════════
# AC1: two concurrent runs — Stop on card A stops ONLY run A; run B keeps running.
# ═══════════════════════════════════════════════════════════════════════════════════
_start("alpha", ["EU-701"])
_start("beta", ["EU-702"])
_wait_started(2)
chk("AC1a: two concurrent runs active",
    cockpit_state.is_active("alpha") and cockpit_state.is_active("beta"))
chk("AC1b: the two runs hold DISTINCT stop_events",
    _ev("alpha") is not None and _ev("beta") is not None
    and _ev("alpha") is not _ev("beta"))

fa, ft = _card_form("alpha", "EU-701")
chk("AC1c: card A's form names card A's own run",
    (fa, ft) == ("alpha", "EU-701"), f"form posts app={fa!r} ticket={ft!r}")
fb, ftb = _card_form("beta", "EU-702")
chk("AC1d: card B's form names card B's own run",
    (fb, ftb) == ("beta", "EU-702"), f"form posts app={fb!r} ticket={ftb!r}")

r = _click_stop("alpha", "EU-701")
chk("AC1e: clicking Stop on card A redirects", r.status_code == 302)
chk("AC1f: card A's click sets ONLY run A's stop_event",
    _ev("alpha").is_set() and not _ev("beta").is_set(),
    "card A must never touch run B's event")
chk("AC1g: run B keeps running",
    cockpit_state.is_active("beta") and not _ev("beta").is_set())
_click_stop("beta", "EU-702")
chk("AC1h: card B's own click then stops run B", _ev("beta").is_set())
_drain_and_reset()


# ═══════════════════════════════════════════════════════════════════════════════════
# AC2: exactly one active run — single-run stop unchanged (no regression).
# ═══════════════════════════════════════════════════════════════════════════════════
_start("alpha", ["EU-703"])
_wait_started(1)
chk("AC2a: single run active", cockpit_state.is_active("alpha"))
r = _click_stop("alpha", "EU-703")
chk("AC2b: the single run's card Stop redirects", r.status_code == 302)
chk("AC2c: the single run's card Stop sets its event", _ev("alpha").is_set())
_drain_and_reset()

# Legacy caller posting NEITHER field still falls back to the sole stoppable run.
_start("beta", ["EU-704"])
_wait_started(1)
r = client.post("/api/stop-run", data={})
chk("AC2d: legacy empty POST still stops the sole stoppable run",
    r.status_code == 302 and _ev("beta") is not None and _ev("beta").is_set())
_drain_and_reset()


# ═══════════════════════════════════════════════════════════════════════════════════
# AC3: a stale card (its ticket no longer owns the app's slot) must stop NOTHING —
# the run now holding the slot keeps running, and stays stoppable via its OWN card.
# ═══════════════════════════════════════════════════════════════════════════════════
_start("alpha", ["EU-705"])
_wait_started(1)
r = _click_stop("alpha", "EU-000")   # a card left over from the slot's PREVIOUS owner
chk("AC3a: stale card's Stop redirects without crashing", r.status_code == 302)
chk("AC3b: stale card stops NOTHING — the current run is untouched",
    not _ev("alpha").is_set(),
    "alpha's slot claimed EU-705; a card posting EU-000 must be ignored, not honoured")
_click_stop("alpha", "EU-705")
chk("AC3c: the live run is still stoppable via its OWN card", _ev("alpha").is_set())
_drain_and_reset()


print("\n================ EU-705 PER-CARD STOP TARGETING QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
