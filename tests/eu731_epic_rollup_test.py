"""EU-731 — the Epic roll-up must fire on ANY child completing, and an Epic is never buildable.

Two defects, both measured on the live board 2026-07-26 (22 Epics open, one Epic planned 3x for
~$3.94 before parking without building a line).

DEFECT 1 — THE ROLL-UP HAD ONE CHANCE PER EPIC. `_maybe_close_epic` was gated on
`scrum.is_verify_child` AND called from exactly one site (the land path). Nothing orders the verify
child last at PICK time, so whenever a sibling was still open when the verify child landed, the
check returned early — and that single trigger was spent forever. Atlassian's own first-party rule
("close the epic when all children are done") triggers on ANY child transitioning to a terminal
state, and re-evaluates the whole child set each time; that is what this now does.

DEFECT 2 — THE DRAIN COULD PICK AN EPIC. An Epic is a CONTAINER: its acceptance criteria belong to
its children, so building it directly is always waste. EU-476 (the container for the EU-458 split)
was planned three times before parking, having never built a line — the effort sizer even tagged it
"sized XL … complexity: epic" and handed it to a builder anyway.

What deliberately did NOT change: every anti-vacuity guard. The children snapshot must CONTAIN the
landed child (so an auth-blind 200 or a partial search can never vacuously "prove" the siblings
done), every other child must be terminal, and any exception leaves the Epic open. Those do all the
real safety work; the verify-child gate did none of it.
"""
from __future__ import annotations

import pathlib
import sys
import types

_sdk = types.ModuleType("claude_agent_sdk")
_sdk.__getattr__ = lambda _n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", _sdk)
_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: None
_req.RequestException = Exception
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import intake  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def T(summary="t", labels=None, issue_type="Task"):
    return types.SimpleNamespace(summary=summary, labels=labels or [], issue_type=issue_type,
                                 id="EU-1", key="EU-1")


# ── DEFECT 2: an Epic is never buildable work ────────────────────────────────
chk("(1) an Epic is detected as non-buildable", intake.is_epic(T(issue_type="Epic")))
chk("(1a) …case/whitespace tolerant (Jira type names vary by board)",
    intake.is_epic(T(issue_type=" epic ")) and intake.is_epic(T(issue_type="EPIC")))
chk("(2) ordinary work is NOT an Epic", not intake.is_epic(T(issue_type="Task"))
    and not intake.is_epic(T(issue_type="Bug")))
chk("(2a) a ticket with no issue_type at all is buildable (fail toward building)",
    not intake.is_epic(types.SimpleNamespace(summary="x")))
chk("(3) the drain skip covers Epics as well as trackers",
    "if is_tracker_ticket(ticket) or is_epic(ticket):" in
    pathlib.Path("orchestrator/intake.py").read_text(encoding="utf-8"))
chk("(3a) tracker detection is unaffected by the new predicate",
    intake.is_tracker_ticket(T(summary="[postmortem] EU-460"))
    and not intake.is_epic(T(summary="[postmortem] EU-460")))

# ── DEFECT 1: the roll-up fires from every terminal path ─────────────────────
LOOP = pathlib.Path("orchestrator/loop.py").read_text(encoding="utf-8")
_fn = LOOP[LOOP.find("def _maybe_close_epic"):]
_fn = _fn[:_fn.find("\ndef _already_landed")]

chk("(4) the verify-child gate no longer blocks the roll-up",
    "is_verify_child(ticket.summary" not in _fn, "verify-child early-return still present")
chk("(5) the roll-up is called from EVERY terminal path, not just the land path",
    LOOP.count("_maybe_close_epic(backlog, ticket, audit)") >= 5,
    f"{LOOP.count('_maybe_close_epic(backlog, ticket, audit)')} call sites")

# the anti-vacuity guards must ALL survive — they are the real safety
chk("(6) guard kept: the snapshot must contain THIS child (no vacuous proof)",
    'if not any((c.get("key") or "") == ticket.key for c in children):' in _fn)
chk("(6a) guard kept: every OTHER child must be terminal",
    "open_sibs" in _fn and '_done = ("qa", "done", "closed")' in _fn)
chk("(6b) guard kept: any exception leaves the Epic OPEN (fail toward human attention)",
    "epic auto-close skipped" in _fn)
chk("(6c) guard kept: a missing epic parent is a no-op (standalone tickets unaffected)",
    "if not epic_key:" in _fn)

# idempotency — a re-opened, re-landed child must not re-post the roll-up
chk("(7) the roll-up is idempotent per Epic",
    "_EPIC_ROLLED_UP" in LOOP and "if epic_key in _EPIC_ROLLED_UP:" in _fn)
_add_at = _fn.find("_EPIC_ROLLED_UP.add(epic_key)")
_ok_at = _fn.find("if ok:")
chk("(7a) …and only records the Epic after a SUCCESSFUL transition",
    _add_at != -1 and _ok_at != -1 and _add_at > _ok_at,
    f"add@{_add_at} ok@{_ok_at} — both anchors must exist and the add must follow the success check")

# the reason must be readable by the next engineer, not only in a commit message
chk("(8) the root cause is recorded in the code",
    "EU-731" in _fn and "zombie Epics" in _fn)

print("\n========== EU-731 EPIC ROLL-UP ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
