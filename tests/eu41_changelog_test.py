"""EU-41: the Technical Writer appends a feature-changelog line on every successful LIVE land.

Asserts loop._record_changelog writes a one-line entry (ticket id + summary + DEV test URL) into
Documentation/Development_Status.md on a simulated land, creates the file with a header when absent,
appends newest-first without losing history, is skipped for dry-run and ephemeral tickets, and never
raises on a write failure (best-effort, like the Scribe)."""
import sys, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import loop
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Ticket

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
doc = tmp / "Documentation" / "Development_Status.md"

app = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                protected_branch="main", backlog_backend="none")
live = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=False)
dry = Config(apps=[app], audit_path=str(tmp / "audit.jsonl"), dry_run=True)
tkt = Ticket(id="EU-41", key="EU-41", summary="changelog on land", description="")
eph = Ticket(id="adhoc-x", key="adhoc-x", summary="scratch", description="", ephemeral=True)

# --- 1) live land: file created with header + the entry ---
wrote = loop._record_changelog(live, tkt, app, "Added the Development_Status changelog",
                               "https://dev.example.com/x", today="2026-06-26", path=str(doc))
body = doc.read_text(encoding="utf-8")
chk("returned True on a live land", wrote)
chk("file created with header", body.startswith("# Development Status"))
chk("entry has date + ticket + app", "2026-06-26 · EU-41 · Elite-Unit" in body)
chk("entry has the what-was-done summary", "Added the Development_Status changelog" in body)
chk("entry has the DEV test URL", "https://dev.example.com/x" in body)

# --- 2) second land: appended newest-first, history preserved ---
loop._record_changelog(live, tkt, app, "second change", "", today="2026-06-27", path=str(doc))
body2 = doc.read_text(encoding="utf-8")
lines = [ln for ln in body2.splitlines() if ln.startswith("- ")]
chk("both entries kept (append, never lose history)", len(lines) == 2, str(lines))
chk("newest entry is first", lines[0].startswith("- 2026-06-27") and lines[1].startswith("- 2026-06-26"))
chk("only one header after re-write", body2.count("# Development Status") == 1)

# --- 3) skipped for dry-run ---
doc2 = tmp / "Documentation" / "dry.md"
chk("dry-run writes nothing (returns False)",
    not loop._record_changelog(dry, tkt, app, "x", "u", today="2026-06-26", path=str(doc2)))
chk("dry-run leaves no file", not doc2.exists())

# --- 4) skipped for ephemeral tickets ---
doc3 = tmp / "Documentation" / "eph.md"
chk("ephemeral writes nothing (returns False)",
    not loop._record_changelog(live, eph, app, "x", "u", today="2026-06-26", path=str(doc3)))
chk("ephemeral leaves no file", not doc3.exists())

# --- 4b) summary fallback: empty/None review+build summary falls back to the ticket summary ---
doc_fb = tmp / "Documentation" / "fallback.md"
loop._record_changelog(live, tkt, app, "", "https://dev.example.com/fb",
                       today="2026-06-26", path=str(doc_fb))
fb = doc_fb.read_text(encoding="utf-8")
chk("empty summary falls back to the ticket summary", "changelog on land" in fb, fb)

doc_fb2 = tmp / "Documentation" / "fallback2.md"
loop._record_changelog(live, tkt, app, None, "", today="2026-06-26", path=str(doc_fb2))
chk("None summary falls back to the ticket summary",
    "changelog on land" in doc_fb2.read_text(encoding="utf-8"))

# --- 4c) long summary is briefed/truncated (entry stays one scannable line) ---
doc_long = tmp / "Documentation" / "long.md"
loop._record_changelog(live, tkt, app, "word " * 200, "", today="2026-06-26", path=str(doc_long))
entry_lines = [ln for ln in doc_long.read_text(encoding="utf-8").splitlines() if ln.startswith("- ")]
chk("a long summary produces exactly one entry line", len(entry_lines) == 1, str(len(entry_lines)))
chk("the long entry is bounded (briefed, not the full 1000 chars)", len(entry_lines[0]) < 320,
    str(len(entry_lines[0])))

# --- 5) best-effort: a write failure must never raise ---
raised = False
out: bool | None = None
try:
    # point at a path whose parent is an existing FILE -> mkdir/write fails internally, swallowed
    bad = tmp / "Documentation" / "Development_Status.md" / "nope.md"
    out = loop._record_changelog(live, tkt, app, "x", "u", today="2026-06-26", path=str(bad))
except Exception:
    raised = True
chk("write failure swallowed (never raises)", not raised)
chk("write failure returns False", out is False)

# --- 6) WIRING: loop._land actually invokes the changelog hook on the LIVE land path ---
# Pins the integration point the ticket calls for ("hook into the LIVE-merge path of loop._land,
# after git.land_trial"). The 18 checks above only exercise _record_changelog directly — a refactor
# could delete the call in _land and they'd all still pass. This drives _land end-to-end with fakes.
from types import SimpleNamespace
from orchestrator.contracts import Outcome

class _FakeGit:
    def commit_all(self, *a, **k): pass
    def trial_merge(self, *a, **k): return True
    def changed_paths(self, *a, **k): return []
    def current_sha(self, *a, **k): return "deadbeef"
    def land_trial(self, *a, **k): pass
    def abandon_trial(self, *a, **k): pass
    def delete_local_branch(self, *a, **k): pass
    def sync_main_base(self, *a, **k): return ""

class _FakeBacklog:
    def set_status(self, *a, **k): pass
    def add_comment(self, *a, **k): pass

class _FakeAudit:
    def record(self, *a, **k): pass

land_app = AppConfig(name="Elite-Unit", repo_path=str(tmp), base_branch="dev",
                     protected_branch="main", backlog_backend="none")  # no postmerge_commands -> SRE skipped

# Spy that replaces the real changelog writer so we observe the call without touching the repo doc.
calls = []
orig_record = loop._record_changelog
loop._record_changelog = lambda cfg, t, ap, summ, url, **kw: calls.append((cfg.dry_run, t.id, ap.name, summ, url))
orig_gate = loop.run_gate
loop.run_gate = lambda *a, **k: SimpleNamespace(passed=True, report="ok")
orig_notify = loop._notify
loop._notify = lambda *a, **k: None
bld = SimpleNamespace(summary="build did the thing")
rev = SimpleNamespace(summary="review confirmed the changelog hook")
try:
    rep = loop._land(tkt, land_app, live, _FakeGit(), _FakeBacklog(), _FakeAudit(),
                     branch="autodev/EU-41", iteration=1, cost=0.0, build=bld, review=rev)
    chk("live _land reports MERGED", rep.outcome == Outcome.MERGED, str(getattr(rep, "outcome", rep)))
    chk("_land called the changelog hook exactly once on live", len(calls) == 1, str(calls))
    chk("hook received this ticket id + app", calls and calls[0][1] == "EU-41" and calls[0][2] == "Elite-Unit",
        str(calls))
    chk("hook is fed the review summary (the 'what was done')", calls and calls[0][3] == rev.summary, str(calls))

    # dry-run land must NOT reach the changelog hook (DEV untouched, no entry).
    calls.clear()
    rep_dry = loop._land(tkt, land_app, dry, _FakeGit(), _FakeBacklog(), _FakeAudit(),
                         branch="autodev/EU-41", iteration=1, cost=0.0, build=bld, review=rev)
    chk("dry-run _land never calls the changelog hook", len(calls) == 0, str(calls))
finally:
    loop._record_changelog = orig_record
    loop.run_gate = orig_gate
    loop._notify = orig_notify

print("\n=============== EU-41 CHANGELOG QA ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
