"""EU-61 Part B — a proposal batch is de-duped at enqueue time (the 'de-duped' acceptance).

eu61_proposal_approval_test.py pins board-level de-dup (filing skips already-open tickets) and
cross-batch idempotency (the same batch doesn't stack a second card). This pins the remaining slice:
WITHIN a single batch, repeated/blank titles collapse so the Commander never sees the same proposed
ticket twice on one card. Pure (no Jira, no network, no models)."""
import sys, types, tempfile
from pathlib import Path

sys.path.insert(0, ".")
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req

from orchestrator import approvals
from orchestrator.config import AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _cfg():
    tmp = Path(tempfile.mkdtemp())
    return types.SimpleNamespace(
        audit_path=str(tmp / "audit.jsonl"),
        apps=[AppConfig(name="automatixy", repo_path="/tmp/x", base_branch="dev",
                        protected_branch="main", backlog_backend="none")])


# A report whose ticket block repeats a title (case-insensitively) and includes a blank one.
REPORT = (
    "===TICKETS===\n"
    '[{"title": "Add deploy retry", "type": "Task", "severity": "MEDIUM", "body": "flaky"},'
    ' {"title": "add deploy retry", "type": "Bug", "severity": "HIGH", "body": "dup, diff case"},'
    ' {"title": "   ", "type": "Task", "severity": "LOW", "body": "blank title"},'
    ' {"title": "Write the runbook", "type": "Task", "severity": "LOW", "body": "ok"}]\n'
    "===END===\n"
)

cfg = _cfg()
bid = approvals.enqueue_proposals(cfg, app_name="automatixy", officer_label="council",
                                  source="council/daily", report=REPORT)
chk("a batch is queued", bool(bid))
batch = approvals.pending_proposals(cfg)[0]
titles = [p["title"] for p in batch["proposals"]]
chk("a case-insensitive duplicate title is collapsed", titles.count("Add deploy retry") <= 1, str(titles))
chk("the blank-title proposal is dropped", all(t.strip() for t in titles), str(titles))
chk("the two distinct real proposals survive",
    titles == ["Add deploy retry", "Write the runbook"], str(titles))

passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
