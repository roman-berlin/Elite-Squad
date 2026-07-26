"""EU-231: close the self-healing loop — forensics output is CONSUMED, not shelved.

(a) A postmortem at threshold files ONE deduped backlog ticket (title = ticket_id + dominant
    category), audits `postmortem_filed`, and sends a one-line Telegram note; a re-fire dedupes.
(b) A cross-ticket crash-signature aggregator (`signature_sweep`) normalizes failure text
    (ticket ids / paths / hashes / line numbers / timestamps stripped) and at >=3 occurrences
    across >=2 tickets within 7 days auto-files ONE labelled infra ticket carrying the raw
    evidence — deterministic, no model call. Same text on ONE ticket, expected transients
    (worktree busy), and >7-day-old occurrences never file.
Kill switch: postmortem_after=0 disables all auto-filing. Everything best-effort — an
unresolvable app skips filing without raising. Stub backlog + captured notify (no Jira, no net).
"""
import sys, types, tempfile, json
from datetime import datetime, timedelta
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
class _RE(Exception):
    pass
req.RequestException = _RE
req.post = lambda *a, **k: (_ for _ in ()).throw(_RE("no network in tests"))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import filing, forensics, notify
from orchestrator.config import AppConfig, Config
from orchestrator.contracts import Outcome

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace
tmp = Path(tempfile.mkdtemp())
audit_file = tmp / "audit.jsonl"

_t = [0]
def ev(days_ago=0.0, path=audit_file, **kw):
    _t[0] += 1
    when = datetime.now() - timedelta(days=days_ago, minutes=(500 - _t[0]))
    kw.setdefault("ts", when.strftime("%Y-%m-%dT%H:%M:%S"))
    with path.open("a") as f:
        f.write(json.dumps(kw) + "\n")

def run(tid, term, days_ago=0.0, app="automatixy", path=audit_file, **extra):
    ev(days_ago, path, event="ticket_start", ticket_id=tid, app=app)
    ev(days_ago, path, event=term, ticket_id=tid, app=app, **extra)


class StubBacklog:
    """In-memory backlog mirroring JiraAdapter's filing contract, with DYNAMIC dedupe: a created
    summary becomes findable by find_open_by_summary, so a re-fire dedupes like live Jira would."""
    def __init__(self):
        self.created = []          # (summary, description, labels, issue_type, priority)
        self._open = {}
    def find_open_by_summary(self, summary):
        return self._open.get(str(summary).strip().lower())
    def create_task(self, summary, description, labels=None, issue_type="Task",
                    priority=None, parent=None):
        key = f"EU-{900 + len(self.created)}"
        self.created.append((summary, description, list(labels or []), issue_type, priority))
        self._open[str(summary).strip().lower()] = key
        return key


app = AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(audit_file), postmortem_after=3)

stub = StubBacklog()
filing.make_backlog = lambda a: stub
sent = []
notify.send = lambda text, chat_id=None: (sent.append(text), True)[1]
audits = []
fake_audit = ns(record=lambda k, **kw: audits.append((k, kw)))

def created_with(prefix, backlog=None):
    return [c for c in (backlog or stub).created if c[0].startswith(prefix)]

# --- 1) signature_key: run-variant text folds to ONE stable signature ------------------------- #
sig_a = forensics.signature_key(
    "Traceback line 412: SDKError connection reset at /Users/x/wt/EU-101/app.py (sha deadbeefcafe) after 32s on 2026-07-17T10:00:00")
sig_b = forensics.signature_key(
    "Traceback line 7: SDKError connection reset at /opt/general/wt/EU-202/app.py (sha 0123456abcd) after 8s on 2026-07-18T09:30:11")
chk("signature_key folds ids/paths/hashes/lines/timestamps", sig_a == sig_b, f"{sig_a!r} vs {sig_b!r}")
chk("signature_key keeps the crash shape", "sdkerror" in sig_a, sig_a)
chk("signature_key separates a different error",
    forensics.signature_key("gate failed: tsc exited 2") != sig_a)

# --- 2) postmortem at threshold -> ONE ticket + audit + Telegram one-liner -------------------- #
for _ in range(3):
    run("PM-1", "needs_human", reason="ran out of turns — ticket too big for one pass")
p = forensics.maybe_postmortem(cfg, ns(ticket_id="PM-1", outcome=Outcome.ESCALATED), fake_audit)
chk("postmortem file still written at threshold", p is not None and p.exists())
pm = created_with("[postmortem]")
chk("exactly one postmortem ticket filed", len(pm) == 1, str([c[0] for c in stub.created]))
if pm:
    title, body, labels, itype, prio = pm[0]
    chk("title carries ticket id + dominant category", "PM-1" in title and "Too big" in title, title)
    chk("body carries the recommended fix", "Split into smaller tickets" in body)
    chk("body carries the timeline", body.count("- ") >= 3, body[:300])
    chk("labelled postmortem + autofiled", "postmortem" in labels and "autofiled" in labels, str(labels))
chk("postmortem_filed audited with the new key",
    any(k == "postmortem_filed" and kw.get("filed") and kw.get("ticket_id") == "PM-1" for k, kw in audits))
chk("one-line Telegram note sent", any("Post-mortem" in s and "PM-1" in s for s in sent), str(sent))

# --- 3) a re-fire (4th failure) dedupes: no second ticket, no second note --------------------- #
run("PM-1", "needs_human", reason="ran out of turns — ticket too big for one pass")
forensics.maybe_postmortem(cfg, ns(ticket_id="PM-1", outcome=Outcome.ESCALATED), fake_audit)
chk("re-fire dedupes — still one postmortem ticket", len(created_with("[postmortem]")) == 1)
chk("no duplicate Telegram note", len([s for s in sent if "Post-mortem" in s]) == 1, str(sent))
chk("no duplicate postmortem_filed audit",
    len([1 for k, _ in audits if k == "postmortem_filed"]) == 1)
chk("same text on ONE ticket never signature-files (4x PM-1)",
    len(created_with("[infra-signature]")) == 0, str([c[0] for c in stub.created]))

# --- 4) crash-signature aggregator: >=3 occurrences across >=2 tickets in 7d ------------------ #
CRASH_A = "Traceback line 412: SDKError connection reset at /Users/x/wt/SIG-A/app.py (sha deadbeefcafe)"
CRASH_A2 = "Traceback line 88: SDKError connection reset at /Users/x/wt/SIG-A/app.py (sha 0badc0ffee9)"
CRASH_B = "Traceback line 7: SDKError connection reset at /opt/hosts/wt/SIG-B/app.py (sha abcdef012)"
run("SIG-A", "ticket_exception", error=CRASH_A)
run("SIG-A", "ticket_exception", error=CRASH_A2)
forensics.maybe_postmortem(cfg, ns(ticket_id="SIG-A", outcome=Outcome.ERRORED), fake_audit)
chk("2 occurrences on 1 ticket: below threshold, nothing filed",
    len(created_with("[infra-signature]")) == 0)
run("SIG-B", "ticket_exception", error=CRASH_B)
forensics.maybe_postmortem(cfg, ns(ticket_id="SIG-B", outcome=Outcome.ERRORED), fake_audit)
sig = created_with("[infra-signature]")
chk("3x/2-ticket signature files exactly ONE infra ticket", len(sig) == 1,
    str([c[0] for c in stub.created]))
if sig:
    title, body, labels, itype, prio = sig[0]
    chk("signature ticket labelled infra-signature + autofiled",
        "infra-signature" in labels and "autofiled" in labels, str(labels))
    chk("signature ticket carries the RAW evidence lines",
        "deadbeefcafe" in body and "SIG-A" in body and "SIG-B" in body, body[:400])
    # EU-570 (2026-07-26) deliberately SUPERSEDES the original "High priority" expectation here.
    # A tracker is excluded from the build queue by intake.is_tracker_ticket, so the unit never
    # builds it; once the drain started honouring priority (a9577a1) a High tracker squatted at the
    # top of the ready column forever and the board read as if priority ordering was broken. It is
    # still a Bug (the type carries the meaning), but it files at Low and is parked out of To Do.
    # The evidence + labels + Telegram note asserted around this line are unchanged.
    chk("signature ticket is a Bug at LOW priority (EU-570: never squats the ready queue)",
        itype == "Bug" and prio == "Low", f"{itype} {prio}")
chk("crash_signature_filed audited", any(k == "crash_signature_filed" for k, _ in audits))
chk("signature Telegram note sent", any("signature" in s.lower() for s in sent), str(sent))

# --- 5) re-sweep dedupes; sweep is public API (callable from a cycle hook) -------------------- #
forensics.maybe_postmortem(cfg, ns(ticket_id="SIG-B", outcome=Outcome.ERRORED), fake_audit)
chk("signature re-sweep dedupes — still one ticket", len(created_with("[infra-signature]")) == 1)
chk("signature_sweep is public and returns [] when all deduped",
    forensics.signature_sweep(cfg, fake_audit) == [])

# --- 6) expected transients (worktree busy) never aggregate ----------------------------------- #
for tid in ("WB-1", "WB-2", "WB-3"):
    run(tid, "needs_human", reason="worktree busy — deferred")
forensics.maybe_postmortem(cfg, ns(ticket_id="WB-3", outcome=Outcome.ESCALATED), fake_audit)
chk("worktree-busy transients never aggregate",
    not any("worktree" in c[0] for c in stub.created), str([c[0] for c in stub.created]))

# --- 7) >7-day-old occurrences are outside the window ----------------------------------------- #
audit_old = tmp / "audit_old.jsonl"
cfg_old = Config(apps=[app], audit_path=str(audit_old), postmortem_after=3)
stub_old = StubBacklog()
filing.make_backlog = lambda a: stub_old
run("OLD-A", "ticket_exception", days_ago=30, path=audit_old, error=CRASH_A)
run("OLD-A", "ticket_exception", days_ago=30, path=audit_old, error=CRASH_A2)
run("OLD-B", "ticket_exception", days_ago=30, path=audit_old, error=CRASH_B)
forensics.maybe_postmortem(cfg_old, ns(ticket_id="OLD-B", outcome=Outcome.ERRORED), fake_audit)
chk(">7d-old occurrences never signature-file", len(stub_old.created) == 0, str(stub_old.created))

# --- 8) kill switch + never-raise ------------------------------------------------------------- #
stub_off = StubBacklog()
filing.make_backlog = lambda a: stub_off
cfg_off = Config(apps=[app], audit_path=str(audit_file), postmortem_after=0)
forensics.maybe_postmortem(cfg_off, ns(ticket_id="SIG-B", outcome=Outcome.ERRORED), fake_audit)
chk("postmortem_after=0 kills ALL auto-filing", len(stub_off.created) == 0)
cfg_noapp = Config(apps=[], audit_path=str(audit_file), postmortem_after=3)
try:
    p2 = forensics.maybe_postmortem(cfg_noapp, ns(ticket_id="PM-1", outcome=Outcome.ESCALATED),
                                    fake_audit)
    ok = p2 is not None
except Exception as exc:  # noqa: BLE001
    ok = False
chk("unresolvable app: postmortem written, filing skipped, no raise", ok)

# --- 9) filing.make_block round-trips through parse_tickets ----------------------------------- #
blk = filing.make_block([{"title": "T", "type": "Bug", "severity": "LOW", "body": "b"}])
props, clean = filing.parse_tickets("prefix\n" + blk)
chk("make_block round-trips through parse_tickets",
    len(props) == 1 and props[0]["title"] == "T" and clean == "prefix", f"{props} {clean!r}")

# --- 9) 2026-07-20: infra signatures file to the UNIT's own project when it is a configured --- #
# --- app (repo_path == this orchestrator repo), not into the victim product backlog.        --- #
self_repo = str(Path(forensics.__file__).resolve().parents[1])
unit_app = AppConfig(name="Elite-Unit", repo_path=self_repo, base_branch="dev",
                     protected_branch="main", backlog_backend="jira",
                     backlog={"project_key": "EU", "base_url": "https://x.atlassian.net"})
audit_self = tmp / "audit_self.jsonl"
cfg_self = Config(apps=[unit_app, app], audit_path=str(audit_self), postmortem_after=3)
stub_by_app: dict[str, StubBacklog] = {}
filing.make_backlog = lambda a: stub_by_app.setdefault(a.name, StubBacklog())
run("SGA-1", "ticket_exception", path=audit_self, error=CRASH_A)
run("SGA-2", "ticket_exception", path=audit_self, error=CRASH_A2)
run("SGB-1", "ticket_exception", path=audit_self, error=CRASH_B)
forensics.maybe_postmortem(cfg_self, ns(ticket_id="SGB-1", outcome=Outcome.ERRORED), fake_audit)
_unit_created = stub_by_app.get("Elite-Unit", StubBacklog()).created
_prod_created = [c for c in stub_by_app.get("automatixy", StubBacklog()).created
                 if "[infra-signature]" in c[0]]
chk("infra signature files on the UNIT's project, not the victim product backlog",
    any("[infra-signature]" in c[0] for c in _unit_created) and not _prod_created,
    f"unit={[c[0] for c in _unit_created]} prod={_prod_created}")

# --- 10) 2026-07-21: a CLOSED tracker must not be resurrected by the SAME old evidence ------- #
# (Live loop: EU-415/416/401 were closed, and the very next sweep re-filed them as EU-422/423/424
# from the identical 7-day window. A filed ticket acknowledges the rows it carried.)
audit_res = tmp / "audit_resurrect.jsonl"
cfg_res = Config(apps=[app], audit_path=str(audit_res), postmortem_after=3)
stub_res = StubBacklog()
filing.make_backlog = lambda a: stub_res
run("RS-1", "ticket_exception", path=audit_res, error=CRASH_A)
run("RS-2", "ticket_exception", path=audit_res, error=CRASH_A2)
run("RS-3", "ticket_exception", path=audit_res, error=CRASH_B)
first = forensics.signature_sweep(cfg_res, fake_audit)
chk("resurrect-guard: the first sweep files the signature", len(first) >= 1, str(first))

# the SAME evidence must not file again even with the ticket gone from the board (closed)
stub_res.created.clear()
stub_res._open = {}   # the Commander CLOSED it — the board no longer dedupes; only our guard can
again = forensics.signature_sweep(cfg_res, fake_audit)
chk("resurrect-guard: closing the ticket does NOT re-file from the same old rows",
    again == [] and not stub_res.created, f"{again} {stub_res.created}")

# a genuinely NEW recurrence still re-opens the class
run("RS-4", "ticket_exception", path=audit_res, error=CRASH_A)
run("RS-5", "ticket_exception", path=audit_res, error=CRASH_A2)
run("RS-6", "ticket_exception", path=audit_res, error=CRASH_B)
fresh = forensics.signature_sweep(cfg_res, fake_audit)
chk("resurrect-guard: NEW occurrences after the filing DO re-open it", len(fresh) >= 1, str(fresh))

print("\n=========== EU-231 POSTMORTEM FILING QA ===========")
passed = sum(1 for _, ok_, _ in results if ok_)
for n, ok_, det in results:
    print(f"  [{'PASS' if ok_ else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok_ else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
