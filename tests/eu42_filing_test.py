"""EU-42: out-of-scope build findings are routed into the backlog (filing.py) instead of lost.

Exercises filing.parse_tickets + filing.file_findings against a STUB backlog (no Jira, no network):
asserts a finding files exactly once, a matching already-open ticket de-dups (no create), and that
the out-of-scope LABEL and the assigned-to-you ASSIGNEE are carried onto every filed ticket — the
guarantees _route_out_of_scope relies on when it calls file_findings(app, "out-of-scope", report).
"""
import sys, types
sys.path.insert(0, ".")

# Stub the Agent SDK so importing the orchestrator package never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

from orchestrator import filing
from orchestrator.config import AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class StubBacklog:
    """In-memory backlog mirroring JiraAdapter's filing contract: create_task records the new
    ticket (with the labels passed and the adapter's default assignee), find_open_by_summary
    de-dups case-insensitively against a seeded set of open summaries."""
    def __init__(self, open_summaries=None, assignee="you"):
        self.assignee = assignee
        self._open = {s.strip().lower(): k for s, k in (open_summaries or {}).items()}
        self.created = []          # (summary, labels, issue_type, assignee)
        self.dedup_lookups = []

    def find_open_by_summary(self, summary):
        self.dedup_lookups.append(summary)
        return self._open.get(str(summary).strip().lower())

    def create_task(self, summary, description, labels=None, issue_type="Task", priority=None):
        key = f"EU-{900 + len(self.created)}"
        self.created.append((summary, list(labels or []), issue_type, self.assignee))
        return key


app = AppConfig(name="Elite-Unit", repo_path="/tmp/x", base_branch="dev",
                protected_branch="main", backlog_backend="none")

REPORT = (
    "Build summary: implemented the in-scope change.\n"
    "Noticed two off-spec issues while in there.\n"
    "===TICKETS===\n"
    '[{"title": "Fix unguarded null deref in parser", "type": "Bug", "severity": "HIGH",'
    ' "body": "orchestrator/x.py crashes on empty input"},'
    ' {"title": "Stale config key still referenced", "type": "Task", "severity": "LOW",'
    ' "body": "remove dead flag"}]\n'
    "===END===\n"
)

# --- 1) parse_tickets pulls the structured proposals out and strips the block ----------------- #
proposals, clean = filing.parse_tickets(REPORT)
chk("parse_tickets returns both proposals", len(proposals) == 2, str(proposals))
chk("parse_tickets strips the ===TICKETS=== block", "===TICKETS===" not in clean and "off-spec" in clean)

# --- 2) all-new findings file exactly once each, with the out-of-scope label + assignee -------- #
stub = StubBacklog(open_summaries={}, assignee="commander-acct")
filing.make_backlog = lambda a: stub
res = filing.file_findings(app, "out-of-scope", REPORT)
chk("two new findings filed", res.filed_n == 2, str(res.filed))
chk("nothing deduped when all new", res.deduped_n == 0)
chk("nothing failed", res.failed_n == 0, str(res.failed))
chk("create_task called once per finding (no double-file)", len(stub.created) == 2, str(stub.created))
chk("out-of-scope label applied to every filed ticket",
    all("out-of-scope" in labels for _, labels, _, _ in stub.created), str(stub.created))
chk("assignee (assigned-to-you) carried onto every filed ticket",
    all(asg == "commander-acct" for *_, asg in stub.created), str(stub.created))

# --- 3) a matching OPEN ticket de-dups — no second ticket created ----------------------------- #
stub2 = StubBacklog(open_summaries={"Fix unguarded null deref in parser": "EU-500"},
                    assignee="commander-acct")
filing.make_backlog = lambda a: stub2
res2 = filing.file_findings(app, "out-of-scope", REPORT)
chk("matching open ticket is de-duped to its key", res2.deduped == ["EU-500"], str(res2.deduped))
chk("only the genuinely-new finding is filed", res2.filed_n == 1, str(res2.filed))
chk("de-dup creates no ticket for the existing finding", len(stub2.created) == 1, str(stub2.created))
chk("the de-duped title is NOT re-created",
    all("null deref" not in s for s, *_ in stub2.created), str(stub2.created))

# --- 4) an empty / no-findings report files nothing ------------------------------------------- #
stub3 = StubBacklog()
filing.make_backlog = lambda a: stub3
res3 = filing.file_findings(app, "out-of-scope", "just a plain report, no block")
chk("no block -> nothing filed", res3.filed_n == 0 and len(stub3.created) == 0)

# ── 2026-07-21: subject-fingerprint de-dup (the EU-409..414 class) ──────────────────────────
from orchestrator.filing import subject_fingerprint

_w1 = subject_fingerprint("Fix eu255_env_minimization NATIVE run mislabelled", "tests/eu255_env_minimization_test.py fails")
_w2 = subject_fingerprint("[BLOCKER/tests] gate is RED", "eu255_env_minimization_test.py fails per EU-249")
chk("fingerprint: rewordings of one subject share a key", _w1 is not None and _w1 == _w2, f"{_w1} vs {_w2}")
chk("fingerprint: different subjects differ",
      subject_fingerprint("CI deploy", "scripts/deploy-edge-functions.sh on merge") != _w1)
chk("fingerprint: prose-only findings have none (title match stays their key)",
      subject_fingerprint("Improve the error copy", "friendlier") is None)

class LabelStub(StubBacklog):
    def __init__(s, hit):
        super().__init__(); s._hit = hit; s.label_lookups = []
    def find_open_by_label(s, label):
        s.label_lookups.append(label); return s._hit

_report = "===TICKETS===\n" + __import__("json").dumps(
    [{"title": "Totally new words here", "type": "Bug", "severity": "HIGH",
      "body": "but tests/eu255_env_minimization_test.py is the same subject"}]) + "\n===END==="
_lstub = LabelStub("EU-777")
filing.make_backlog = lambda a: _lstub
_res = filing.file_findings(app, "review", _report)
chk("label de-dup fires BEFORE title match — reworded duplicate never files",
      _res.deduped == ["EU-777"] and not _res.filed and _lstub.label_lookups, str(_res.deduped))

_lstub2 = LabelStub(None)
filing.make_backlog = lambda a: _lstub2
_res2 = filing.file_findings(app, "review", _report)
chk("no label hit → files, stamping the fingerprint label on the new ticket",
      len(_res2.filed) == 1 and any(str(l).startswith("fp-") for l in _lstub2.created[-1][1]),
      str(_lstub2.created[-1] if _lstub2.created else None))


passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
