"""EU-427 — boot_reconcile must age out a prior resume the ticket has since CLOSED OUT, so a
ticket resumed → merged → killed again much later is RESUMED (not parked) on its current dangling
run (2026-07-22).

The EU-398 recurrence check (``_prior_resumed_ids``) scanned the WHOLE audit window for any
``boot_reconcile`` 'resumed' entry with no time bound. A ticket resumed once, completed successfully
(merged), and genuinely killed again much later still had its id in an OLD ``boot_reconcile`` row,
so the reconcile honest-PARKed it (Blocked) instead of resuming it — phantom-blocking a ticket that
was honestly killed in a brand-new run.

Fix bound (the ticket's "at/after the ticket's most-recent terminal" reading — the only one of the
two suggested bounds that keeps the genuine recurring-kill PARK intact): a prior resume counts as
recurrent ONLY when NO terminal outcome follows it (the resume sits inside the same unclosed run as
the current dangle). A terminal at/after the resume ages it out.

Pins:
  §1 PURE CORE (_prior_resumed_ids):
     - resume with a terminal AFTER it (merged closed the run out) → AGED OUT (not recurring)
     - resume with NO terminal after it (same unclosed run, recurring kill) → still counts
     - resume AFTER a terminal, with no later terminal (recurrence in a fresh window) → counts
     - per-ticket age-out: an unrelated id closed out is aged out while a not-closed-out id counts
  §2 I/O (boot_reconcile):
     - resumed → merged → killed again much later → RESUMED (not parked), one resumed=[id] event
  §3 I/O (no regression):
     - a genuine recurring kill (no terminal between resume and now) STILL honest-parks

Offline — the SDK + requests are stubbed; make_backlog is faked; no network, no real models.
"""
import json
import os
import sys
import tempfile
import types
from pathlib import Path

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

# Hermetic even when run standalone (run_all already scrubs these from harness env).
for _k in list(os.environ):
    if _k.startswith(("TELEGRAM_", "JIRA_")):
        os.environ.pop(_k, None)

from orchestrator import autopilot as ap
from orchestrator.backlog import base as backlog_base
from orchestrator.config import AppConfig, Config
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


# ── helpers ─────────────────────────────────────────────────────────────────── #
def _ev(event, tid, ts):
    return {"event": event, "ticket_id": tid, "ts": ts}


def _boot_ev(ts, resumed, parked=None, skipped=None):
    return {"ts": ts, "event": "boot_reconcile",
            "resumed": list(resumed), "parked": parked or [], "skipped_active": skipped or []}


def _write_audit(path, rows):
    """Write controlled-order JSONL rows straight to the audit file (deterministic ts ordering)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


class FakeBacklog:
    """Serves a fixed ticket list and records every comment / set_status call."""
    def __init__(self, tickets):
        self._tickets = list(tickets)
        self.comments = []        # [(key, body)]
        self.statuses = []        # [(key, status)]

    def get_ready_tasks(self, limit):
        return list(self._tickets)

    def add_comment(self, ticket, body):
        self.comments.append((ticket.key, body))

    def set_status(self, ticket, status):
        self.statuses.append((ticket.key, status))


def _cfg_with_app(tmp, tickets):
    """A one-jira-app Config whose make_backlog serves `tickets`, with a fresh audit path."""
    app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="jira",
                    backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
    cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
    fake = FakeBacklog(tickets)
    backlog_base.make_backlog = lambda a: fake
    return cfg, fake


def _audit_events(path):
    try:
        return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]
    except OSError:
        return []


# ============================================================================================ #
# §1 PURE CORE: _prior_resumed_ids — age out a resume the ticket closed out after
# ============================================================================================ #
print("\n=== §1 pure core: _prior_resumed_ids age-out ===")
T = "AUTO-427"

# AC1 shape: resumed once, then MERGED (closed out), then killed again much later.
rows_aged = [
    _ev("ticket_start", T, "2026-07-19T13:00:00+0000"),            # run 1
    _boot_ev("2026-07-19T13:01:00+0000", [T]),                      # a prior boot resumed it
    _ev("ticket_start", T, "2026-07-19T14:00:00+0000"),            # run 2 (the resumed run)
    _ev("merged", T, "2026-07-19T14:30:00+0000"),                  # COMPLETED — terminal AFTER resume
    _ev("ticket_start", T, "2026-07-22T09:00:00+0000"),            # run 3 (much later), killed
]
prior_aged = ap._prior_resumed_ids(rows_aged)
chk("resume with a terminal AFTER it is AGED OUT (not recurring)", T not in prior_aged, str(prior_aged))

# AC2 shape: recurring kill within the same unclosed run (no terminal after the resume).
rows_rec = [
    _ev("ticket_start", T, "2026-07-19T13:00:00+0000"),
    _boot_ev("2026-07-19T13:01:00+0000", [T]),
    _ev("ticket_start", T, "2026-07-19T14:00:00+0000"),            # ran again and was killed — NO terminal
]
prior_rec = ap._prior_resumed_ids(rows_rec)
chk("resume with NO terminal after it IS recurring (same unclosed run)", T in prior_rec, str(prior_rec))

# A terminal BEFORE the resume but none after it = recurrence in a fresh window (still counts).
rows_newrec = [
    _ev("ticket_start", T, "2026-07-19T13:00:00+0000"),
    _ev("merged", T, "2026-07-19T13:30:00+0000"),                  # terminal (run 1 closed)
    _ev("ticket_start", T, "2026-07-19T14:00:00+0000"),            # run 2
    _boot_ev("2026-07-19T14:01:00+0000", [T]),                      # resumed AFTER the terminal...
    _ev("ticket_start", T, "2026-07-19T15:00:00+0000"),            # ...ran again and was killed (no terminal)
]
prior_newrec = ap._prior_resumed_ids(rows_newrec)
chk("resume after a terminal with no later terminal IS recurring", T in prior_newrec, str(prior_newrec))

# Per-ticket age-out: an unrelated id closed out is aged out while a not-closed-out id still counts.
T2 = "AUTO-999"
rows_two = [
    _boot_ev("2026-07-19T13:01:00+0000", [T]),                      # T resumed, no later terminal -> recurring
    _ev("ticket_start", T2, "2026-07-19T13:00:00+0000"),
    _boot_ev("2026-07-19T13:01:00+0000", [T2]),
    _ev("merged", T2, "2026-07-19T14:30:00+0000"),                 # T2 closed out -> aged out
]
prior_two = ap._prior_resumed_ids(rows_two)
chk("only the not-closed-out id is recurring (per-ticket age-out)",
    prior_two == {T}, str(prior_two))


# ============================================================================================ #
# §2 I/O: boot_reconcile — resumed → merged → killed again is RESUMED, not parked
# ============================================================================================ #
print("\n=== §2 I/O: resumed → merged → killed again is resumed ===")
tmp = Path(tempfile.mkdtemp())
t = Ticket(id=T, key=T, summary="died again later", description="spec",
           acceptance_criteria=["x"], app="automatixy", status="In Progress")
cfg, fake = _cfg_with_app(tmp, [t])
_write_audit(cfg.audit_path, [
    _ev("ticket_start", T, "2026-07-19T13:00:00+0000"),
    _boot_ev("2026-07-19T13:01:00+0000", [T]),                      # resumed once long ago
    _ev("ticket_start", T, "2026-07-19T14:00:00+0000"),
    _ev("merged", T, "2026-07-19T14:30:00+0000"),                  # ...and finished (closed out)
    _ev("ticket_start", T, "2026-07-22T09:00:00+0000"),            # a NEW run, killed — current dangle
])
out = ap.boot_reconcile(cfg)
chk("resumed→merged→killed-again is RESUMED (not parked)",
    out.get("resumed") == [T] and out.get("parked") == [], str(out))
chk("resume posts exactly one comment", len(fake.comments) == 1, str(fake.comments))
chk("resume comment says 'unclean stop'", "unclean stop" in fake.comments[0][1].lower(),
    str(fake.comments))
chk("resume does NOT transition to Blocked", fake.statuses == [], str(fake.statuses))
ev = [e for e in _audit_events(cfg.audit_path) if e.get("event") == "boot_reconcile"]
chk("this run's boot_reconcile records resumed=[T] (a second resumed event, not parked)",
    any(e.get("resumed") == [T] for e in ev), str(ev))


# ============================================================================================ #
# §3 I/O: a genuine recurring kill (no terminal between resume and now) STILL parks — no regression
# ============================================================================================ #
print("\n=== §3 I/O: recurring kill (same unclosed run) still parks ===")
tmp2 = Path(tempfile.mkdtemp())
t_rec = Ticket(id=T, key=T, summary="keeps dying", description="spec",
               acceptance_criteria=["x"], app="automatixy", status="In Progress")
cfg2, fake2 = _cfg_with_app(tmp2, [t_rec])
_write_audit(cfg2.audit_path, [
    _ev("ticket_start", T, "2026-07-19T13:00:00+0000"),
    _boot_ev("2026-07-19T13:01:00+0000", [T]),                      # prior resume
    _ev("ticket_start", T, "2026-07-19T14:00:00+0000"),            # ran again, killed — NO terminal
])
out2 = ap.boot_reconcile(cfg2)
chk("recurring kill (no terminal after resume) is still honest-PARKED",
    out2.get("parked") == [T] and out2.get("resumed") == [], str(out2))
chk("park transitions the ticket to Blocked", (T, "Blocked") in fake2.statuses, str(fake2.statuses))


# ============================================================================================ #
passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\neu427_boot_reconcile_ageout_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
