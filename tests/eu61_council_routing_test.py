"""EU-61 Part B — council/meeting proposals route to the approval QUEUE by default, and
`meeting_autospawn` is the explicit file-immediately bypass.

Before EU-61 a council/meeting either auto-filed everything (meeting_autospawn on) or proposed
nothing actionable. This pins the new wiring in council._autospawn_tickets:

  * default (autospawn OFF) → the ticket block is parsed and ENQUEUED for the Commander (a card
    appears in pending_proposals); NOTHING is filed to the board; the note points at "Needs you".
  * autospawn ON            → the old behaviour: file straight to the primary app, no queue card.
  * no ticket block         → no queue card, no file, clean decision text unchanged.

A stub filing.file_findings records whether the board was touched; no Jira, no network, no models.
"""
import sys, types, tempfile, asyncio
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
class _Opts:                       # store kwargs so a test can inspect the chair's system_prompt
    def __init__(s, **k): s.__dict__.update(k)
sdk.ClaudeAgentOptions = _Opts
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import council, approvals, filing
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

ns = types.SimpleNamespace
audit_events = []
audit = ns(record=lambda kind, **kw: audit_events.append((kind, kw)))


def _cfg(autospawn):
    tmp = Path(tempfile.mkdtemp())
    return Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                                  protected_branch="main", backlog_backend="none")],
                  audit_path=str(tmp / "audit.jsonl"), use_worktree=False,
                  meeting_autospawn=autospawn)


DECISION = (
    "We agreed to harden the deploy.\n"
    "===TICKETS===\n"
    '[{"title": "Add deploy retry", "type": "Task", "severity": "MEDIUM", "body": "flaky"}]\n'
    "===END===\n"
)

# Stub filing.file_findings so the autospawn path never touches a real board; record the call.
filed_calls = []
class _Res:
    filed_n = 1; deduped_n = 0; failed_n = 0; lines = ["• filed AUTO-700"]
filing.file_findings = lambda app, label, raw: (filed_calls.append((app.name, label)), _Res())[1]


# --- 1) default (autospawn OFF) → enqueue, do NOT file ----------------------------------------- #
cfg = _cfg(False)
audit_events.clear(); filed_calls.clear()
clean, note = council._autospawn_tickets(cfg, DECISION, audit, source="council/daily",
                                         officer_label="council")
chk("default path files NOTHING to the board", filed_calls == [], str(filed_calls))
pend = approvals.pending_proposals(cfg)
chk("default path enqueues exactly one batch", len(pend) == 1, str(len(pend)))
chk("queued batch records its source", pend and pend[0].get("source") == "council/daily")
chk("queued batch carries the proposed title",
    pend and [p["title"] for p in pend[0]["proposals"]] == ["Add deploy retry"], str(pend))
chk("the ticket block is stripped from the clean decision", "===TICKETS===" not in clean)
chk("note points the Commander at Needs you", "Needs you" in note or "approval" in note.lower(), note)
chk("default path audits proposals_queued", any(k == "proposals_queued" for k, _ in audit_events),
    str(audit_events))
chk("default path does NOT audit an autospawn file",
    not any(k == "meeting_autospawn" for k, _ in audit_events), str(audit_events))


# --- 2) autospawn ON → file straight to the board, no queue card ------------------------------- #
cfg2 = _cfg(True)
audit_events.clear(); filed_calls.clear()
clean2, note2 = council._autospawn_tickets(cfg2, DECISION, audit, source="council/daily",
                                           officer_label="council")
chk("autospawn files to the primary app", filed_calls == [("automatixy", "council")], str(filed_calls))
chk("autospawn does NOT enqueue an approval card", approvals.pending_proposals(cfg2) == [])
chk("autospawn note reflects an auto-file", "filed" in note2.lower(), note2)
chk("autospawn audits meeting_autospawn", any(k == "meeting_autospawn" for k, _ in audit_events),
    str(audit_events))


# --- 3) no ticket block → nothing queued, nothing filed, text intact -------------------------- #
cfg3 = _cfg(False)
filed_calls.clear()
clean3, note3 = council._autospawn_tickets(cfg3, "Just a discussion, no tickets.", audit)
chk("no block → nothing filed", filed_calls == [])
chk("no block → nothing queued", approvals.pending_proposals(cfg3) == [])
chk("no block → note is empty", note3 == "", repr(note3))
chk("no block → decision text unchanged", clean3 == "Just a discussion, no tickets.")


# --- 4) hold_meeting end-to-end (autospawn OFF) → chair IS told to propose + batch enqueued ----- #
# The exact regression iter-1 shipped: the ad-hoc meeting chair only received TICKET_BLOCK_RULE when
# meeting_autospawn was on, so the default queue-for-approval mode never emitted a ===TICKETS===
# block and never enqueued anything. Drive the real hold_meeting wiring (discuss + chair stubbed) to
# prove the chair is always told to propose and that proposals flow into the approval queue.
cfg4 = _cfg(False)                  # autospawn OFF — the default, queue-for-approval mode
chair_calls = []

async def _fake_run_agent(prompt, options, tag=""):
    chair_calls.append((tag, options))
    return ns(final=DECISION, text="")

async def _fake_discuss(*a, **k):
    return [("Dev Team Lead", "We should harden the deploy.")]

async def _fake_scribe(_cfg):
    return "scribe: ok"

council.collect_signals = lambda _cfg: {}
council.format_signals = lambda _sig: "DIGEST"
council.discuss = _fake_discuss
council.run_agent = _fake_run_agent
council.memory.preamble = lambda: ""
council.memory.scribe = _fake_scribe
council.notify.send = lambda *a, **k: None

filed_calls.clear()
decision = asyncio.run(council.hold_meeting(cfg4, "harden the deploy", audit=audit))

chair = next((o for t, o in chair_calls if t == "the-general"), None)
chk("hold_meeting ran the chair", chair is not None)
chk("chair is told to propose tickets even with autospawn OFF",
    chair is not None and filing.TICKET_BLOCK_RULE in getattr(chair, "system_prompt", ""),
    "chair system_prompt missing the ticket-block rule")
pend4 = approvals.pending_proposals(cfg4)
chk("ad-hoc meeting enqueues exactly one batch (default mode)", len(pend4) == 1, str(len(pend4)))
chk("enqueued batch records the meeting source",
    pend4 and pend4[0].get("source") == "meeting: harden the deploy", str(pend4))
chk("enqueued batch carries the proposed title",
    pend4 and [p["title"] for p in pend4[0]["proposals"]] == ["Add deploy retry"], str(pend4))
chk("hold_meeting files NOTHING to the board in default mode", filed_calls == [], str(filed_calls))
chk("the ticket block is stripped from the returned decision", "===TICKETS===" not in decision)


passed = sum(1 for _, c, _ in results if c)
for n, c, d in results:
    print(f"  {'✓' if c else '✗'} {n}" + (f"  [{d}]" if (not c and d) else ""))
print(f"{passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
