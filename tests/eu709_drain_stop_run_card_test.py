"""EU-709: the RUN cluster keeps its hard-stop control during a drain.

Before: mid-drain the control bar's "run" cluster collapsed to a passive
"Stopping…" chip. EU-689 re-added the autopilot-scoped Stop button there
(posts /api/autopilot action=stop — marks autopilot off; the in-flight build
still runs to completion). This ticket adds the HARD-STOP control next to the
chip: a form that posts the in-flight run's own app + ticket to /api/stop-run
— the per-card targeting channel (EU-692/EU-705) — so a long (e.g. 30-minute)
ticket can be halted at its next checkpoint mid-drain. The ticket comes from
the slot's ``run_tickets``, which this ticket also makes the autopilot loop
publish when it takes a cycle's worklist (autopilot.py), mirroring
run_selected_api's claim-time record (EU-693).

Deliberately NOT a stop form on the war room's autopilot run cards: EU-692
pins that autopilot-run cards (``manual=False``) emit NO stop form at all —
the control bar's RUN cluster is the sanctioned stop surface for an autopilot
run (see eu692_stop_form_hidden_inputs_test.py AC3).

Checks:
  AC1  Drain-state render = "Stopping…" chip + hard-stop form
       (action=/api/stop-run with hidden app+ticket), EU-689's autopilot Stop
       intact alongside; "—" ticket sentinel when the slot carries no tickets;
       no hard-stop control outside the drain state (ON cluster unchanged).
  AC2  Round trip: POST exactly what the rendered form emits to /api/stop-run
       → the targeted slot's stop_event is re-signalled and the canonical
       success note lands; a stale ticket (one the slot never claimed) stops
       nothing — the EU-693 guard still holds during a drain.
"""
import html
import re
import sys
import tempfile
import threading
import types
from pathlib import Path

# Minimal stub so orchestrator imports succeed without the real SDK.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

sys.path.insert(0, ".")

from orchestrator import cockpit_state
from orchestrator import cockpit_views as V
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


class _FakeStopEvent:
    """threading.Event stand-in: is_set() is always True (exactly what makes the
    slot read as 'draining' to get_autopilot_status); set() calls are counted so
    the round trip proves the handler reached THIS slot's event."""

    def __init__(self) -> None:
        self.sets = 0

    def is_set(self) -> bool:
        return True

    def set(self) -> None:
        self.sets += 1


d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("", encoding="utf-8")
cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(d / "automatixy"), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")],
    audit_path=str(d / "audit.jsonl"), use_worktree=False)


def _drain_state(tickets) -> _FakeStopEvent:
    """Put the app's slot into a genuine drain: autopilot on, stop issued (event
    set), tickets claimed — returns the slot's (fake) stop_event."""
    st = cockpit_state.get_state("automatixy")
    st["active"] = True
    st["autopilot_on"] = True
    st["run_tickets"] = tickets
    ev = _FakeStopEvent()
    st["stop_event"] = ev
    return ev


def _stop_run_fragment(bar: str) -> str:
    """The hard-stop form's HTML only — the bar carries several forms with
    name=app, so slice out exactly the one posting to /api/stop-run."""
    if "action=/api/stop-run" not in bar:
        return ""
    return bar.split("action=/api/stop-run", 1)[1].split("</form>", 1)[0]


def _hidden(frag: str, field: str):
    m = re.search(r'name=' + field + r' value="([^"]*)"', frag)
    return html.unescape(m.group(1)) if m else None


# ── AC1: drain-state render carries the hard-stop control ────────────────────
cockpit_state.reset_run_state()
_drain_state(["EU-709"])
bar = V._control_bar(cfg, "automatixy", True)

chk("AC1a: 'Stopping…' chip still rendered during drain",
    'class="tbap stopping"' in bar and "Stopping" in bar, bar[:400])

frag = _stop_run_fragment(bar)
chk("AC1b: hard-stop form posts to /api/stop-run next to the chip",
    bool(frag), "no /api/stop-run form in drain-state render")
chk("AC1c: hard-stop form carries the app (per-card targeting)",
    _hidden(frag, "app") == "automatixy", f"app={_hidden(frag, 'app')!r}")
chk("AC1d: hard-stop form carries the in-flight ticket from run_tickets",
    _hidden(frag, "ticket") == "EU-709", f"ticket={_hidden(frag, 'ticket')!r}")
chk("AC1e: hard-stop button rendered",
    "&#9632; Hard&nbsp;stop</button>" in bar, "")

chk("AC1f: EU-689 autopilot Stop still rendered alongside (no regression; relabeled 'Stop autopilot' by EU-708)",
    "action=/api/autopilot" in bar and "action value=stop" in bar
    and "Stop&nbsp;autopilot</button>" in bar, "")

# Slot with NO claimed tickets → the "—" sentinel /api/stop-run understands
# (it then resolves the slot app-scoped) — never an empty value attribute.
cockpit_state.reset_run_state()
_drain_state([])
bar_empty = V._control_bar(cfg, "automatixy", True)
chk("AC1g: ticket sentinel '—' when the slot carries no tickets",
    _hidden(_stop_run_fragment(bar_empty), "ticket") == "—",
    f"ticket={_hidden(_stop_run_fragment(bar_empty), 'ticket')!r}")

# Autopilot ON but NOT draining → the ON cluster renders, and the hard-stop
# control is drain-only (the ON cluster has its own Finish & stop / Stop pair).
cockpit_state.reset_run_state()
st = cockpit_state.get_state("automatixy")
st["active"] = True
st["autopilot_on"] = True
st["stop_event"] = threading.Event()   # issued: NO — not set
bar_on = V._control_bar(cfg, "automatixy", True)
chk("AC1h: no /api/stop-run form outside the drain state",
    "action=/api/stop-run" not in bar_on and "action value=drain" in bar_on, "")

# ── AC2: clicking hard-stop mid-drain stops the targeted run ─────────────────
cockpit_state.reset_run_state()
ev = _drain_state(["EU-709"])
# POST exactly what the rendered form emits (browser-decoded), as a click would.
form_app = _hidden(frag, "app")
form_ticket = _hidden(frag, "ticket")

import orchestrator.server as srv   # noqa: E402  (after the sdk stub)

srv.health.summary = lambda c: {"healthy": True, "checks": []}
client = srv.create_app(cfg).test_client()

r = client.post("/api/stop-run", data={"app": form_app, "ticket": form_ticket})
chk("AC2a: hard-stop click signals the targeted run's stop_event",
    ev.sets == 1, f"set() called {ev.sets}x")
chk("AC2b: canonical app+ticket branch — checkpoint-safety note lands",
    cockpit_state.get_state("automatixy").get("last_msg")
    == "stopping after the current step — DEV untouched, no merge",
    repr(cockpit_state.get_state("automatixy").get("last_msg")))
chk("AC2c: handler redirects home (click completed)",
    r.status_code in (302, 303), f"status={r.status_code}")

# A STALE form — ticket the slot never claimed — must stop nothing (EU-693's
# guard still bites during a drain: the wrong run is never killed).
r_stale = client.post("/api/stop-run", data={"app": "automatixy", "ticket": "EU-999"})
chk("AC2d: stale ticket refused — targeted run NOT re-signalled",
    ev.sets == 1, f"set() called {ev.sets}x after stale POST")
chk("AC2e: stale click surfaces the visible no-run message (EU-695/EU-706)",
    cockpit_state.get_state("automatixy").get("last_msg")
    == "No active run found to stop for automatixy/EU-999",
    repr(cockpit_state.get_state("automatixy").get("last_msg")))
chk("AC2f: stale click still redirects home",
    r_stale.status_code in (302, 303), f"status={r_stale.status_code}")

cockpit_state.reset_run_state()

passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}{extra}")
print(f"\n{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
