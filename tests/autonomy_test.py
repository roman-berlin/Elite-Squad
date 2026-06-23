"""QA for proactive autonomy — MEETING: detection, the event reactor's auto-convene, meeting
auto-file, and the after-merge Scout. Stubs the agents/backlog so nothing real runs."""
import asyncio, sys, tempfile, types
from pathlib import Path

REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))

from orchestrator import council, events, loop, filing
from orchestrator.config import Config, AppConfig
from orchestrator.contracts import Outcome, Ticket

class FakeAudit:
    def __init__(self): self.events = []
    def record(self, event, **kw): self.events.append((event, kw))

d = Path(tempfile.mkdtemp())
(d / "audit.jsonl").write_text("", encoding="utf-8")
app = AppConfig(name="automatixy", repo_path=str(d), base_branch="DEV", protected_branch="MAIN",
                backlog_backend="none")
cfg = Config(apps=[app], audit_path=str(d / "audit.jsonl"), use_worktree=False)

# ===================== 1) extract_meeting_requests =====================
transcript = ("### Inspector General\nWe keep bouncing on a11y.\n"
              "MEETING: close the superadmin authz gap, attendees: Provost, Field Engineer\n\n"
              "### Provost\nAgreed.\nMEETING: close the superadmin authz gap\n"
              "### Scout\nMEETING: hi\n")
reqs = council.extract_meeting_requests(transcript)
check("extract: finds the request", reqs and reqs[0] == "close the superadmin authz gap", str(reqs))
check("extract: strips the 'attendees:' clause", all("attendees" not in r.lower() for r in reqs))
check("extract: de-dupes + drops too-short", len(reqs) == 1, str(reqs))
check("extract: none -> []", council.extract_meeting_requests("no requests here") == [])

# ===================== 2) pending_meeting_requests =====================
council.history = lambda c, limit=1: [{"file": "council-1.md", "ts": "x", "title": "t", "summary": "s"}]
council.transcript_text = lambda c, f: transcript
src, topics = council.pending_meeting_requests(cfg)
check("pending: reads latest transcript", src == "council-1.md" and topics == ["close the superadmin authz gap"])

# ===================== 3) _autospawn_tickets =====================
DEC = ('**DECISION** Do the thing.\n\n===TICKETS===\n'
       '[{"title":"Add authz probe test","type":"Task","severity":"HIGH","body":"b"}]\n===END===')
filing.file_findings = lambda app, label, report: filing.FilingResult(
    filed=["AUTO-201"], lines=["✓ AUTO-201 filed — Add authz probe test"])
cfg.meeting_autospawn = True
audit3 = FakeAudit()
clean, note = council._autospawn_tickets(cfg, DEC, audit3)
check("autospawn on: files + notes it", "Auto-filed" in note and "AUTO-201" in note and "===TICKETS===" not in clean)
check("autospawn on: records audit (filed=1)",
      any(e == "meeting_autospawn" and kw.get("filed") == 1 for e, kw in audit3.events), str(audit3.events))
cfg.meeting_autospawn = False
clean2, note2 = council._autospawn_tickets(cfg, DEC, FakeAudit())
check("autospawn off: proposes, files nothing", "Proposed tickets" in note2 and "Auto-filed" not in note2)
check("no ticket block -> no note", council._autospawn_tickets(cfg, "just a decision", None) == ("just a decision", ""))

# ===================== 4) events._meeting_request gating =====================
council.pending_meeting_requests = lambda c: ("council-9.md", ["do the thing"])
check("reactor: fresh request -> returned", events._meeting_request(cfg, {}) == ("do the thing", "council-9.md"))
check("reactor: already-acted request -> None", events._meeting_request(cfg, {"acted_council": "council-9.md"}) is None)

# ===================== 5) after_cycle auto-convenes the meeting =====================
called = {"meeting": None}
async def _fake_meeting(c, topic, officers=None, rounds=None, audit=None):
    called["meeting"] = topic
    return "decided"
council.hold_meeting = _fake_meeting
audit5 = FakeAudit()
fired = asyncio.run(events.after_cycle(cfg, reports=[], audit=audit5, blocked=None))
check("after_cycle: fires 'officer-requested-meeting'", fired == "officer-requested-meeting", str(fired))
check("after_cycle: actually convened the requested topic", called["meeting"] == "do the thing")
import json as _json
st = _json.loads((d / "autonomy.json").read_text())
check("after_cycle: marks the council acted (won't repeat)", st.get("acted_council") == "council-9.md", str(st))

# ===================== 6) after-merge Scout (loop) =====================
_c0 = Config(apps=[app], audit_path=str(d / "audit.jsonl"))
check("config: scout_after_merge + meeting_autospawn default False",
      _c0.scout_after_merge is False and _c0.meeting_autospawn is False)
from orchestrator import scout as scout_mod, notify as notify_mod
notify_mod.send = lambda *a, **k: None
async def _fake_recon(c, app_name, url=None):
    return "Scout smoke: PASS. No runtime regressions.\n===TICKETS===\n[]\n===END==="
scout_mod.recon = _fake_recon
cfg.dry_run = True   # do_file=False -> filing won't touch a backlog
audit6 = FakeAudit()
asyncio.run(loop._after_merge_scout(cfg, app, Ticket(id="AUTO-12", key="AUTO-12", summary="s",
            description="d", app="automatixy"), audit6))
check("after-merge scout: writes scout-report.md (block stripped)",
      (d / "scout-report.md").exists() and "===TICKETS===" not in (d / "scout-report.md").read_text())
check("after-merge scout: records audit", any(e == "scout_smoke" for e, _ in audit6.events))

# ===================== report =====================
print("\n================ PROACTIVE AUTONOMY QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if (detail and not ok) else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAILURE(S) ❌")
