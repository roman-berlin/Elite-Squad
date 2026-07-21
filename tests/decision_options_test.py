"""Structured Needs-you decisions (2026-07-19, Commander order — the EU-337 format, wired):
a decision reads as a BRIEF problem + 2-3 numbered options, exactly one (RECOMMENDED); the
cockpit renders one-click option buttons; the chosen option ships through the EXISTING
/api/answer path (Jira comment + ticket re-runs); a Telegram reply can be just the number.

Pins:
  (1) parse_options: numbered lists ("1." / "2)") and dash bullets parse; the (RECOMMENDED)
      marker is detected and stripped from the option text; summary = prose before the list;
  (2) prose with no option list (or a single item) → None — free-text stays the surface;
  (3) expand_option_reply: '2' / 'Option 2' → the full option text; other answers untouched;
  (4) handle_reply expands a bare-number reply against the pending entry's stored question
      BEFORE resolving (so the Jira comment carries the decision, not a digit);
  (5) the /needs decision card renders the option buttons (recommended highlighted, star),
      each shipping its full option text via /api/answer, plus the Other free-text fallback;
  (6) an unstructured decision renders the classic free-text card unchanged;
  (7) both PM escalation templates demand numbered options with exactly one (RECOMMENDED)."""
import sys
import tempfile
import types
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

from orchestrator import decisions
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


Q = ("The date column can be rendered two ways and the ticket doesn't say which.\n"
     "OPTIONS:\n"
     "1. DD/MM/YYYY everywhere (RECOMMENDED) — matches the EU locale of every current tenant\n"
     "2. MM/DD/YYYY everywhere\n"
     "3. Per-tenant locale setting\n")

# ── (1) parser ──
po = decisions.parse_options(Q)
chk("(1a) three options parsed", po is not None and len(po["options"]) == 3, str(po))
chk("(1b) summary is the prose before the list",
    po and po["summary"].startswith("The date column"), str(po and po["summary"]))
chk("(1c) option 1 is recommended, marker stripped from text",
    po and po["options"][0]["recommended"] and "RECOMMENDED" not in po["options"][0]["text"].upper()
    and po["options"][0]["text"].startswith("DD/MM/YYYY"), str(po and po["options"][0]))
chk("(1d) options 2/3 not recommended",
    po and not po["options"][1]["recommended"] and not po["options"][2]["recommended"])
chk("(1e) dash bullets parse too",
    (decisions.parse_options("Pick one\n- red (recommended)\n- blue\n") or {}).get("options", [])[0:1] != [])

# ── (2) unstructured → None ──
chk("(2a) plain prose → None", decisions.parse_options("Which date format should I use?") is None)
chk("(2b) a single bullet → None", decisions.parse_options("Pick\n1. only choice\n") is None)

# ── (3) numeric expansion ──
chk("(3a) '2' expands to the full option text",
    decisions.expand_option_reply(Q, "2") == "Option 2: MM/DD/YYYY everywhere")
chk("(3b) 'Option 1' expands", "DD/MM/YYYY" in decisions.expand_option_reply(Q, "Option 1"))
chk("(3c) a real sentence passes through",
    decisions.expand_option_reply(Q, "use ISO dates") == "use ISO dates")
chk("(3d) numeric against unstructured question passes through",
    decisions.expand_option_reply("free question", "2") == "2")

# ── (4) handle_reply expands before resolving ──
tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="dev",
                             protected_branch="main", backlog_backend="none")],
             audit_path=str(tmp / "audit.jsonl"), use_worktree=False)
seen = {}
_orig_resolve, _orig_load = decisions.resolve, decisions.load
decisions.load = lambda c: [{"id": "AUTO-9", "question": Q}]
def _fake_resolve(c, answer, ticket_id, claim=False):
    seen["answer"] = answer
    return None   # stop the flow after capture — no run, no notify
decisions.resolve = _fake_resolve
try:
    decisions.handle_reply(cfg, types.SimpleNamespace(record=lambda *a, **k: None), "AUTO-9: 2")
finally:
    decisions.resolve, decisions.load = _orig_resolve, _orig_load
chk("(4) a bare-number reply reaches resolve as the full option text",
    seen.get("answer") == "Option 2: MM/DD/YYYY everywhere", str(seen))

# ── (5)+(6) the /needs card ──
from orchestrator import server, needs
_orig_summary = needs.summary
def _fake_summary(c, app_name=None):
    rows = [
        {"id": "AUTO-9", "app": "automatixy", "question": Q, "why": Q.splitlines()[0],
         "category": "decision"},
        {"id": "AUTO-10", "app": "automatixy", "question": "Just tell me what to do here.",
         "why": "Just tell me what to do here.", "category": "decision"},
    ]
    return {"rows": rows, "decisions": rows, "proposals": [], "tasks": [], "total": 2}
needs.summary = _fake_summary
try:
    cfg.detected_auth = lambda: "test"
    client = server.create_app(cfg).test_client()
    body = client.get("/needs").get_data(as_text=True)
finally:
    needs.summary = _orig_summary
chk("(5a) option buttons render with numbers", ">1. " in body.replace("&#9733; ", "") or "1. DD/MM" in body)
chk("(5b) the recommended option is highlighted with a star", "&#9733;" in body)
chk("(5c) an option button ships the FULL option text through /api/answer",
    'value="Option 2: MM/DD/YYYY everywhere"' in body)
chk("(5d) the Other free-text fallback stays", "Other — type your own decision" in body)
chk("(5e) the brief problem line heads the card", "The date column can be rendered two ways" in body)
chk("(6) an unstructured decision keeps the classic free-text card",
    "Just tell me what to do here." in body
    and "Answer the unit — your decision re-runs the ticket" in body)

# ── (8) synthesized options for the pre-format backlog classes ──
WALL = ("Base branch 'dev' is RED before any build — the gate fails on the clean base tree. "
        "Fix the base (or land the fix ticket) before re-queuing this one. $ /Users/x/.venv/bin/python "
        "tests/run_all.py (exit 1) 17/17 passed ✓ needs_ui_test.py 33/33 passed ✓ " * 3)
sp = decisions.synthesize_options(WALL)
chk("(8a) the red-base wall synthesizes options", sp is not None and sp.get("synthesized") is True)
chk("(8b) …with a brief summary, not the wall",
    sp and len(sp["summary"]) < 160 and "RED" in sp["summary"])
chk("(8c) …re-queue is the recommended option",
    sp and sp["options"][0]["recommended"] and "Re-queue" in sp["options"][0]["text"])
# 2026-07-19: a turn-limit park now only happens AFTER auto-split hit its depth cap and one
# boosted retry — so the honest recommendation is re-scope, with retry as the manual override.
chk("(8d) turn-limit walls synthesize a re-scope-first recommendation",
    (decisions.synthesize_options("AUTO-152 ran out of turns before finishing — split it") or
     {}).get("options", [{}])[0].get("text", "").startswith("Re-scope"))
chk("(8e) a genuinely free-form question does NOT synthesize",
    decisions.synthesize_options("Which date format should the dashboard use?") is None)
# 2026-07-21: leaked RULE-0 officer banners never reach a brief or a stored park
chk("(8g) summarize_question strips leaked officer banner lines",
    "Model: Opus" not in decisions.summarize_question(
        "\U0001f916 Model: Opus\n\u2699\ufe0f Effort: MAX — deep\nWhich store should hold the flag?")
    and "Which store should hold the flag?" in decisions.summarize_question(
        "\U0001f916 Model: Opus\nWhich store should hold the flag?"))
chk("(8f) summarize_question cuts command dumps",
    "$" not in decisions.summarize_question(WALL) and len(decisions.summarize_question(WALL)) <= 141)

# ── (9) the card renders synthesized options + the folded full context ──
_orig_summary2 = needs.summary
def _wall_summary(c, app_name=None):
    rows = [{"id": "EU-242", "app": "Elite-Unit", "question": WALL,
             "why": WALL[:80], "category": "decision"}]
    return {"rows": rows, "decisions": rows, "proposals": [], "tasks": [], "total": 1}
needs.summary = _wall_summary
try:
    body2 = server.create_app(cfg).test_client().get("/needs").get_data(as_text=True)
finally:
    needs.summary = _orig_summary2
chk("(9a) the wall renders as a BRIEF headline",
    "This ticket parked while base" in body2 and "gate failed on the clean tree" in body2)
chk("(9b) …with the recommended re-queue button",
    "Re-queue now" in body2 and "&#9733;" in body2)
chk("(9c) …and the full original text behind a fold", "Full context" in body2)
chk("(9d) the raw wall is not the headline",
    "needs_ui_test.py 33/33" not in body2.split("Full context")[0])

# ── (7) PM templates demand the format ──
pm_src = Path("orchestrator/pm.py").read_text(encoding="utf-8")
chk("(7a) escalation template numbers the options", pm_src.count("(RECOMMENDED)") >= 2)
chk("(7b) exactly-one-recommended rule stated", "EXACTLY ONE" in pm_src)

print("\n========== STRUCTURED DECISIONS QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
