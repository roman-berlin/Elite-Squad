"""EU-404 — crash-loop breaker + respawn-into-bad-code guard on boot resume.

The production-audit P1s (2026-07-21): (a) ``resume_armed_drains`` had no attempt counter, so a
build that repeatably kills the process (the macOS OOM class) looped respawn→resume→rebuild→kill,
burning a full planner+builder spend per cycle with no strike (abrupt death writes no ERRORED
report) and no park; (b) a bad land on the unit's OWN repo respawned INTO the bad code with no
smoke and no start limit.

This harness pins the three ACs:

  §1 AC2 PURE (bump_error_strike): a death mid-build charges one durable strike to the ticket's
     consecutive-error count, so the 3-strike park survives crashes that wrote no terminal outcome.
  §2 AC2 INTEGRATION: boot_reconcile charges the strike for every dangling ticket.
  §3 AC2 CROSS-CRASH: three boot-bumps reach _MAX_TICKET_ERRORS, and _tally_errored then parks —
     the 3-strike park now works across crashes.
  §4 AC1 PURE (_recent_resume_count): counts autopilot_resume events for the app in the window.
  §5 AC1 INTEGRATION: ≥3 recent resumes for an app → resume HELD (intent kept), one
     autopilot_resume_skipped audit event, Telegram alerted; <3 → resumes normally.
  §6 AC3 PURE (boot_smoke): green subprocesses → (True,…); a red import or red harness subset →
     (False,…) with a boot_smoke_fail audit event + Telegram alert.
  §7 AC3 INTEGRATION: resume_armed_drains(block_reason=…) holds every resume (intent kept),
     one audit event, alerted — the cockpit still serves because serving is downstream.

Offline — the SDK + requests are stubbed; subprocess.run is monkeypatched for the smoke unit
tests; no network, no real models.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
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
from orchestrator import health, notify
from orchestrator.backlog import base as backlog_base
from orchestrator.audit import AuditLog
from orchestrator.config import AppConfig, Config
from orchestrator.contracts import Outcome, Ticket, TicketReport

# resume_armed_drains gates on health.summary; stub it healthy so the crash-loop / block_reason
# paths are actually reached (the EU-385 harness pattern). Offline → no real creds either way.
health.summary = lambda cfg: {"healthy": True}

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))
    print(("PASS: " if c else "FAIL: ") + n + (f" - {d}" if d and not c else ""))


# ── helpers ─────────────────────────────────────────────────────────────────── #
def _ev(event, tid, ts):
    return {"event": event, "ticket_id": tid, "ts": ts}


def _resume_ev(app, age_s, *, now=None):
    """An autopilot_resume audit row `age_s` seconds old for `app` (local-time %z stamp)."""
    ts = (time.time() if now is None else now) - age_s
    return {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts)),
            "event": "autopilot_resume", "app": app}


def _write_audit(path, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _events(path):
    try:
        return [json.loads(ln) for ln in Path(path).read_text().splitlines() if ln.strip()]
    except OSError:
        return []


class FakeBacklog:
    def __init__(self, tickets):
        self._tickets = list(tickets)
        self.comments = []
        self.statuses = []

    def get_ready_tasks(self, limit):
        return list(self._tickets)

    def add_comment(self, ticket, body):
        self.comments.append((ticket.key, body))

    def set_status(self, ticket, status):
        self.statuses.append((ticket.key, status))


def _cfg_with_app(tmp, tickets):
    app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                    protected_branch="main", backlog_backend="jira",
                    backlog={"base_url": "https://x.atlassian.net", "project_key": "AUTO"})
    cfg = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
    fake = FakeBacklog(tickets)
    backlog_base.make_backlog = lambda a: fake
    return cfg, fake


def _intent(cfg):
    p = Path(cfg.audit_path).with_name("autopilot_intent.json")
    try:
        return json.loads(p.read_text())
    except OSError:
        return None


# ============================================================================================ #
# §1 AC2 PURE: bump_error_strike — a durable strike on the consecutive-error store
# ============================================================================================ #
print("\n=== §1 AC2 pure: bump_error_strike ===")
tmp_s1 = Path(tempfile.mkdtemp())
cfg_s1 = Config(apps=[], audit_path=str(tmp_s1 / "audit.jsonl"))
chk("error_counts starts empty", ap.load_error_counts(cfg_s1) == {})
n1 = ap.bump_error_strike(cfg_s1, "AUTO-177")
chk("first bump returns the new count (1)", n1 == 1, str(n1))
chk("first bump persists {AUTO-177: 1}", ap.load_error_counts(cfg_s1) == {"AUTO-177": 1})
n2 = ap.bump_error_strike(cfg_s1, "AUTO-177")
chk("second bump returns 2", n2 == 2, str(n2))
chk("second bump persists {AUTO-177: 2}", ap.load_error_counts(cfg_s1) == {"AUTO-177": 2})

# preserves a sibling key (a concurrent drain's tally is not clobbered — the EU-274 invariant)
counts_path = Path(cfg_s1.audit_path).with_name("error_counts.json")
counts_path.write_text(json.dumps({"OTHER-9": 5, "AUTO-177": 2}))   # AUTO-177 sits at 2 on disk
n3 = ap.bump_error_strike(cfg_s1, "AUTO-177")     # true RMW: reads 2, adds 1
chk("third bump returns 3 (read-modify-write, not overwrite)", n3 == 3, str(n3))
after = ap.load_error_counts(cfg_s1)
chk("third bump preserved the foreign key OTHER-9", after.get("OTHER-9") == 5, str(after))
chk("third bump set AUTO-177 to 3", after.get("AUTO-177") == 3, str(after))

# never raises on a corrupt store — fail-safe (a bad state file must not crash the boot)
counts_path.write_text("{not json")
try:
    ap.bump_error_strike(cfg_s1, "AUTO-500")
    corrupt_ok = True
except Exception:
    corrupt_ok = False
chk("bump never raises on a corrupt error_counts store", corrupt_ok)


# ============================================================================================ #
# §2 AC2 INTEGRATION: boot_reconcile charges a dangling ticket one durable strike
# ============================================================================================ #
print("\n=== §2 AC2 integration: boot_reconcile charges a strike ===")
tmp_s2 = Path(tempfile.mkdtemp())
t_kill = Ticket(id="AUTO-177", key="AUTO-177", summary="died mid-build", description="spec",
                acceptance_criteria=["x"], app="automatixy", status="In Progress")
cfg_s2, fake2 = _cfg_with_app(tmp_s2, [t_kill])
_write_audit(cfg_s2.audit_path, [_ev("ticket_start", "AUTO-177", "2026-07-19T13:00:00+0000")])
chk("error_counts empty before boot_reconcile", ap.load_error_counts(cfg_s2) == {})
out2 = ap.boot_reconcile(cfg_s2)
chk("boot_reconcile resumed the dangling ticket", out2.get("resumed") == ["AUTO-177"], str(out2))
chk("boot_reconcile charged the dangling ticket exactly one durable strike",
    ap.load_error_counts(cfg_s2) == {"AUTO-177": 1}, str(ap.load_error_counts(cfg_s2)))


# ============================================================================================ #
# §3 AC2 CROSS-CRASH: three boot-bumps reach the threshold, and _tally_errored then parks
# ============================================================================================ #
print("\n=== §3 AC2 cross-crash: 3 strikes → park ===")
tmp_s3 = Path(tempfile.mkdtemp())
cfg_s3 = Config(apps=[], audit_path=str(tmp_s3 / "audit.jsonl"))
tid = "AUTO-404"
# simulate three crash→boot cycles, each charging one strike (no ERRORED report was ever written)
last = 0
for _ in range(3):
    last = ap.bump_error_strike(cfg_s3, tid)
chk("three boot-bumps reach _MAX_TICKET_ERRORS (3)", last == ap._MAX_TICKET_ERRORS == 3,
    f"last={last}, MAX={ap._MAX_TICKET_ERRORS}")
chk("the strike is DURABLE across the (simulated) crashes — reloaded from disk at 3",
    ap.load_error_counts(cfg_s3) == {tid: 3}, str(ap.load_error_counts(cfg_s3)))
# feed the durable count into the park decision exactly as the drain cycle does
counts = dict(ap.load_error_counts(cfg_s3))
park_now, errored, retrying, infra, _ = ap._tally_errored(
    [TicketReport(ticket_id=tid, outcome=Outcome.ERRORED, iterations=1, cost_usd=0.0,
                  notes="builder killed mid-pass")], counts)
chk("a crash-built strike count feeds the 3-strike park on the next ERRORED",
    tid in park_now, str(park_now))


# ============================================================================================ #
# §4 AC1 PURE: _recent_resume_count — recent autopilot_resume events for the app in the window
# ============================================================================================ #
print("\n=== §4 AC1 pure: _recent_resume_count ===")
tmp_s4 = Path(tempfile.mkdtemp())
cfg_s4 = Config(apps=[], audit_path=str(tmp_s4 / "audit.jsonl"))
NOW = time.time()
_write_audit(cfg_s4.audit_path, [
    _resume_ev("a1", 60, now=NOW),       # recent, this app
    _resume_ev("a1", 600, now=NOW),      # recent, this app
    _resume_ev("a1", 3000, now=NOW),     # recent (<2h), this app
    _resume_ev("a1", 8000, now=NOW),     # OLD (>2h) — excluded
    _resume_ev("a2", 30, now=NOW),       # recent but a DIFFERENT app — excluded
    _resume_ev("", 90, now=NOW),         # unit-wide — excluded when asking for a specific app
    {"ts": "2026-07-19T13:00:00+0000", "event": "autopilot_start", "app": "a1"},  # not a resume
])
chk("counts this app's recent resumes (3)", ap._recent_resume_count(cfg_s4, "a1", now=NOW) == 3,
    str(ap._recent_resume_count(cfg_s4, "a1", now=NOW)))
# the >2h resume is excluded: with ONLY an 8000s-old a1 resume on disk, the count is 0
tmp_old = Path(tempfile.mkdtemp())
cfg_old = Config(apps=[], audit_path=str(tmp_old / "audit.jsonl"))
_write_audit(cfg_old.audit_path, [_resume_ev("a1", 8000, now=NOW)])
chk("excludes the >2h resume (an 8000s-old a1 resume alone counts 0)",
    ap._recent_resume_count(cfg_old, "a1", now=NOW) == 0,
    str(ap._recent_resume_count(cfg_old, "a1", now=NOW)))
chk("counts a different app separately (a2 → 1)",
    ap._recent_resume_count(cfg_s4, "a2", now=NOW) == 1, str(ap._recent_resume_count(cfg_s4, "a2", now=NOW)))
chk("unit-wide key counts only unit-wide resumes (1)",
    ap._recent_resume_count(cfg_s4, "", now=NOW) == 1, str(ap._recent_resume_count(cfg_s4, "", now=NOW)))
chk("missing audit file → 0 (never raises)", ap._recent_resume_count(
    Config(apps=[], audit_path=str(tmp_s4 / "nope" / "audit.jsonl")), "a1", now=NOW) == 0)


# ============================================================================================ #
# §5 AC1 INTEGRATION: ≥3 recent resumes for an app → resume HELD; <3 → resumes
# ============================================================================================ #
print("\n=== §5 AC1 integration: crash-loop breaker holds resume ===")
_orig_send = notify.send
_orig_pid = ap._PID_FILE
notify.send = lambda *a, **k: False

try:
    # ── (a) the crash loop: 3 recent autopilot_resume events for a1 → NOT resumed ─────────
    tmp5a = Path(tempfile.mkdtemp())
    cfg5a = Config(apps=[AppConfig(name="a1", repo_path=str(tmp5a), base_branch="DEV",
                                   backlog_backend="none")],
                   audit_path=str(tmp5a / "audit.jsonl"))
    ap._PID_FILE = tmp5a / "ap.pid"
    (tmp5a / "autopilot_intent.json").write_text(json.dumps(
        {"a1": {"state": "RUNNING", "armed_ts": time.time() - 60, "pid": 999999}}))
    NOW5 = time.time()
    _write_audit(cfg5a.audit_path, [_resume_ev("a1", 60, now=NOW5),
                                    _resume_ev("a1", 600, now=NOW5),
                                    _resume_ev("a1", 3000, now=NOW5)])
    sent = []
    notify.send = lambda msg, *a, **k: sent.append(msg) or False
    calls = []
    async def _stub_ap(c, app_name=None, once=False, interval=60, stop_event=None):
        calls.append(app_name)
    _real_ap = ap.autopilot
    ap.autopilot = _stub_ap
    try:
        resumed5a = ap.resume_armed_drains(cfg5a, wait_s=5)
    finally:
        ap.autopilot = _real_ap
    chk("crash-loop (≥3 resumes): the drain is NOT resumed", resumed5a == [], str(resumed5a))
    chk("crash-loop: no autopilot() spawn happened", calls == [], str(calls))
    chk("crash-loop: intent is KEPT RUNNING (not cleared — the window may clear)",
        (_intent(cfg5a) or {}).get("a1", {}).get("state") == "RUNNING", str(_intent(cfg5a)))
    evs5a = _events(cfg5a.audit_path)
    chk("crash-loop: one autopilot_resume_skipped audit event (reason=crash-loop)",
        any(e.get("event") == "autopilot_resume_skipped" and e.get("reason") == "crash-loop"
            for e in evs5a), str([e.get("event") for e in evs5a]))
    chk("crash-loop: the Commander was alerted via Telegram",
        any("crash" in m.lower() for m in sent), str(sent))

    # ── (b) under the threshold (2 recent): resumes normally ──────────────────────────────
    tmp5b = Path(tempfile.mkdtemp())
    cfg5b = Config(apps=[AppConfig(name="a1", repo_path=str(tmp5b), base_branch="DEV",
                                   backlog_backend="none")],
                   audit_path=str(tmp5b / "audit.jsonl"))
    ap._PID_FILE = tmp5b / "ap.pid"
    (tmp5b / "autopilot_intent.json").write_text(json.dumps(
        {"a1": {"state": "RUNNING", "armed_ts": time.time() - 60, "pid": 999999}}))
    NOW5b = time.time()
    _write_audit(cfg5b.audit_path, [_resume_ev("a1", 60, now=NOW5b),
                                    _resume_ev("a1", 600, now=NOW5b)])   # only 2 → under threshold
    calls_b = []
    async def _stub_ap_b(c, app_name=None, once=False, interval=60, stop_event=None):
        calls_b.append(app_name)
        return None
    ap.autopilot = _stub_ap_b
    try:
        resumed5b = ap.resume_armed_drains(cfg5b, wait_s=5)
    finally:
        ap.autopilot = _real_ap
    chk("under threshold (2 resumes): the drain IS resumed", resumed5b == ["a1"], str(resumed5b))
    chk("under threshold: autopilot() spawned exactly once for a1", calls_b == ["a1"], str(calls_b))
finally:
    ap._PID_FILE = _orig_pid
    notify.send = _orig_send


# ============================================================================================ #
# §6 AC3 PURE: boot_smoke — green subprocesses pass; red import / red harness fails + alerts
# ============================================================================================ #
print("\n=== §6 AC3 pure: boot_smoke ===")
_real_run = subprocess.run


def _patch_run(red_when):
    """subprocess.run stub: return code 1 when the cmd matches red_when(target), else 0."""
    calls = []

    def _run(cmd, *a, **kw):
        calls.append(list(cmd))
        is_import = "-c" in cmd and "import orchestrator.server" in " ".join(cmd)
        is_harness = str(cmd[-1]).endswith("run_all.py") or any(
            str(c).endswith("run_all.py") for c in cmd)
        target = "import" if is_import else ("harness" if is_harness else "other")
        rc = 1 if red_when == target else 0
        return subprocess.CompletedProcess(cmd, rc, "stdout-tail" if rc else "",
                                           ("stderr-tail: boom" if rc else ""))
    return _run, calls


tmp_s6 = Path(tempfile.mkdtemp())
cfg_s6 = Config(apps=[], audit_path=str(tmp_s6 / "audit.jsonl"))
_orig_send6 = notify.send
sent6 = []
notify.send = lambda msg, *a, **k: sent6.append(msg) or False
try:
    # green: both subprocesses exit 0
    run, calls = _patch_run(None)
    subprocess.run = run
    ok, detail = ap.boot_smoke(cfg_s6)
    chk("boot_smoke green → ok=True", ok, detail)
    chk("boot_smoke runs the IMPORT smoke (python -c 'import orchestrator.server')",
        any("-c" in c and "import orchestrator.server" in " ".join(c) for c in calls), str(calls))
    chk("boot_smoke runs the fast-harness subset (run_all.py --smoke)",
        any(any(str(x).endswith("run_all.py") for x in c) and "--smoke" in c for c in calls), str(calls))
    evs6 = _events(cfg_s6.audit_path)
    chk("boot_smoke green records a boot_smoke_pass audit event",
        any(e.get("event") == "boot_smoke_pass" for e in evs6), str([e.get("event") for e in evs6]))

    # red import: the just-landed code does not even import
    tmp_s6b = Path(tempfile.mkdtemp())
    cfg_s6b = Config(apps=[], audit_path=str(tmp_s6b / "audit.jsonl"))
    run, _ = _patch_run("import")
    subprocess.run = run
    ok, detail = ap.boot_smoke(cfg_s6b)
    chk("boot_smoke red import → ok=False", not ok, detail)
    evs6b = _events(cfg_s6b.audit_path)
    chk("boot_smoke red records a boot_smoke_fail audit event",
        any(e.get("event") == "boot_smoke_fail" for e in evs6b), str([e.get("event") for e in evs6b]))
    chk("boot_smoke red alerts the Commander via Telegram",
        any("smoke" in m.lower() for m in sent6), str(sent6))

    # red harness subset: import ok but a smoke harness failed
    tmp_s6c = Path(tempfile.mkdtemp())
    cfg_s6c = Config(apps=[], audit_path=str(tmp_s6c / "audit.jsonl"))
    run, _ = _patch_run("harness")
    subprocess.run = run
    ok, detail = ap.boot_smoke(cfg_s6c)
    chk("boot_smoke red harness subset → ok=False", not ok, detail)
finally:
    subprocess.run = _real_run
    notify.send = _orig_send6


# ============================================================================================ #
# §7 AC3 INTEGRATION: resume_armed_drains(block_reason=…) holds every resume; cockpit still serves
# ============================================================================================ #
print("\n=== §7 AC3 integration: block_reason holds resume ===")
tmp7 = Path(tempfile.mkdtemp())
cfg7 = Config(apps=[AppConfig(name="a1", repo_path=str(tmp7), base_branch="DEV",
                              backlog_backend="none")], audit_path=str(tmp7 / "audit.jsonl"))
ap._PID_FILE = tmp7 / "ap.pid"
(tmp7 / "autopilot_intent.json").write_text(json.dumps(
    {"a1": {"state": "RUNNING", "armed_ts": time.time() - 60, "pid": 999999}}))
sent7 = []
_orig_send7 = notify.send
notify.send = lambda msg, *a, **k: sent7.append(msg) or False
calls7 = []
async def _stub_ap7(c, app_name=None, once=False, interval=60, stop_event=None):
    calls7.append(app_name)
_real_ap7 = ap.autopilot
ap.autopilot = _stub_ap7
try:
    resumed7 = ap.resume_armed_drains(cfg7, wait_s=5, block_reason="boot-smoke-failed: import")
finally:
    ap.autopilot = _real_ap7
    notify.send = _orig_send7
chk("block_reason: nothing is resumed", resumed7 == [], str(resumed7))
chk("block_reason: no autopilot() spawned", calls7 == [], str(calls7))
chk("block_reason: intent is KEPT RUNNING (the next clean boot re-evaluates)",
    (_intent(cfg7) or {}).get("a1", {}).get("state") == "RUNNING", str(_intent(cfg7)))
evs7 = _events(cfg7.audit_path)
chk("block_reason: one autopilot_resume_skipped audit event carrying the reason",
    any(e.get("event") == "autopilot_resume_skipped" and "smoke" in str(e.get("reason", ""))
        for e in evs7), str([e.get("event") for e in evs7]))
# the alert is boot_smoke's job (it has the failure detail); the resume path only refuses + audits,
# so the Commander gets exactly ONE page per smoke failure. Assert this path did NOT double-page.
chk("block_reason: resume path does NOT re-alert (boot_smoke owns the single page)",
    sent7 == [], str(sent7))


# ============================================================================================ #
passed = [n for n, ok, _ in results if ok]
failed = [(n, d) for n, ok, d in results if not ok]
print(f"\neu404_crash_breaker_test: {len(passed)}/{len(results)} passed")
for n, d in failed:
    print(f"  FAIL: {n}" + (f" — {d}" if d else ""))
if failed:
    sys.exit(1)
