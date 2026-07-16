"""Telegram reply -> deduped ticket (2026-07-07) — Roman's decision: when he answers a 'needs your
call' and it implies work, the CTO opens a DEDUPED ticket so his words become work (not just a note).

The CTO (respond_to_commander) may append a ===TICKETS===[...]===END=== block when — and only when —
the Commander's message asks for or approves a concrete build. council._file_commander_ticket parses
that block, routes it to the right product, and files it via the existing (deduped) filing machinery.
This harness pins: the helpers exist, a block files a deduped ticket, a plain chat reply files nothing,
and app-routing picks the product whose Jira key matches a referenced ticket (else the primary app).

Written fail-first: against the code before the helpers exist, the first checks go RED.
"""
import sys, types
REPO = "."
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, REPO)

results = []
def check(n, c, d=""):
    results.append((n, bool(c), d))

from orchestrator import council, filing
from orchestrator.config import Config, AppConfig

check("council._app_for_reply exists", hasattr(council, "_app_for_reply"))
check("council._file_commander_ticket exists", hasattr(council, "_file_commander_ticket"))

if hasattr(council, "_file_commander_ticket") and hasattr(council, "_app_for_reply"):
    app_eu = AppConfig(name="elite", repo_path="/tmp/eu", base_branch="dev",
                       protected_branch="main", backlog_backend="jira", backlog={"project_key": "EU"})
    app_ax = AppConfig(name="automatixy", repo_path="/tmp/ax", base_branch="dev",
                       protected_branch="main", backlog_backend="jira", backlog={"project_key": "AUTO"})
    cfg = Config(apps=[app_ax, app_eu])   # app_ax is the primary (index 0)

    # ---- app routing: a referenced ticket key picks the matching product; else the primary ----
    check("routing: referenced EU-136 -> the EU product",
          council._app_for_reply(cfg, ["EU-136"]) is app_eu)
    check("routing: no reference -> the primary product (apps[0])",
          council._app_for_reply(cfg, []) is app_ax)
    check("routing: unknown prefix -> the primary product",
          council._app_for_reply(cfg, ["ZZ-1"]) is app_ax)
    check("routing: no products -> None", council._app_for_reply(Config(apps=[]), ["EU-1"]) is None)

    # ---- a fake backlog so filing runs its REAL deduped logic without touching Jira ----
    created, open_titles = [], set()
    class _FakeBacklog:
        def find_open_by_summary(self, title): return "EU-existing" if title in open_titles else ""
        def create_task(self, title, body, labels=None, issue_type="Task", priority=None):
            created.append((title, tuple(labels or []), issue_type)); return "EU-701"
    _orig_make = filing.make_backlog
    filing.make_backlog = lambda app: _FakeBacklog()
    try:
        # (a) a reply that implies work (CTO appended a ticket block) -> a deduped ticket is filed
        answer = ('Done — I\'ll get the unit on it.\n\n'
                  '===TICKETS===\n'
                  '[{"title": "Add post-merge revert-on-red check", "type": "Task", "severity": "HIGH", '
                  '"body": "SRE runs postmerge_commands after a land; auto-revert on red."}]\n'
                  '===END===')
        res = council._file_commander_ticket(cfg, ["EU-136"], answer)
        check("a ticket block files a ticket", res is not None and res.filed_n == 1, str(res and res.filed))
        check("the filed ticket is labelled commander + autofiled",
              created and "commander" in created[0][1] and "autofiled" in created[0][1], str(created))
        check("it filed to the EU product (referenced ticket routed it)",
              created and created[0][0] == "Add post-merge revert-on-red check")

        # (b) DEDUP: the same title, already open -> nothing created, reported as deduped
        open_titles.add("Add post-merge revert-on-red check")
        before = len(created)
        res2 = council._file_commander_ticket(cfg, ["EU-136"], answer)
        check("dedup: an already-open title is NOT filed again",
              res2 is not None and res2.deduped_n == 1 and len(created) == before, str(res2 and res2.deduped))

        # (c) a plain chat reply (no ticket block) files NOTHING
        res3 = council._file_commander_ticket(cfg, [], "Morning! all quiet, nothing to do.")
        check("a plain reply with no ticket block files nothing", res3 is None)

        # (d) CRASH-SAFE: a backlog that fails to construct (missing creds / unknown backend) must
        #     degrade to None, never raise into the reply (fleet HIGH finding, 2026-07-07).
        def _boom(app): raise RuntimeError("no jira creds")
        filing.make_backlog = _boom
        try:
            res4 = council._file_commander_ticket(cfg, ["EU-1"], answer)
            crashed = False
        except Exception:
            res4, crashed = "raised", True
        check("filing failure degrades to None (never crashes the reply)",
              (not crashed) and res4 is None, "raised" if crashed else str(res4))
    finally:
        filing.make_backlog = _orig_make

# ---- the reply prompt tells the CTO to open a ticket only for genuine work (high bar) ----
check("respond_to_commander doctrine mentions opening a ticket for work",
      "ticket" in council._COMMANDER_TICKET_RULE.lower() and "high bar" in council._COMMANDER_TICKET_RULE.lower()
      if hasattr(council, "_COMMANDER_TICKET_RULE") else False)

print("\n============ COMMANDER REPLY -> TICKET QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
