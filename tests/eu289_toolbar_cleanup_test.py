"""EU-289: toolbar/nav cleanup — Jira-only intake, one QA control, de-duped Reports menu.

Roman 2026-07-12, three simplifications, all in the same `_control_bar()` block:
  1. "+ New task" is removed — intake is Jira-only, so the free-text /api/run panel is redundant
     UI (mirrors EU-205/EU-206, which already removed the promote/ship buttons). The /api/run
     ROUTE stays (server.py) for scripted use; only the affordance goes.
  2. Patrol (/api/patrol) and Ship review (/api/ship-review) collapse into a single "QA" control.
     EU-299 (b59315e) only grouped them under a `tclabel>QA` cluster — they were still two
     separate top-level buttons, so the "single QA control" AC was unmet. Both underlying flows
     are distinct (patrol files Jira findings; ship-review checks release readiness) and must
     both still be reachable.
  3. The Reports menu de-dups: "Unit roster" duplicated the top-level Roster button, and
     "Token usage" / "Budget monitor" both covered the usage+budget report (/usage already
     renders the budget-status and plan panels).

These assertions are the AC, not the implementation: they pin what must be GONE, what must
still be REACHABLE, and that no report is listed twice.
"""
import re
from pathlib import Path as _P
ROOT = _P(__file__).resolve().parent.parent
import sys
import tempfile
import types
from pathlib import Path

# --- stub the Agent SDK / requests so orchestrator modules import cleanly (no network) ---
_sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = _req

sys.path.insert(0, ".")

from orchestrator import cockpit_views as V
from orchestrator.config import AppConfig, Config
from orchestrator import sync as _sync

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail) if not cond else ""))


_sync.can_promote = lambda: False  # keep the bar off git / network

_tmp = Path(tempfile.mkdtemp())
_cfg = Config(
    apps=[AppConfig(name="automatixy", repo_path=str(_tmp / "app"), base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="jira",
                    backlog={"base_url": "https://acme.atlassian.net"})],
    audit_path=str(_tmp / "audit.jsonl"), use_worktree=False,
)
_cfg.detected_auth = lambda: "test"

bar = V._control_bar(_cfg, "automatixy", healthy=True, is_mac=True)

# ---------------------------------------------------------------------------
# 1. "+ New task" is gone — intake is Jira-only
# ---------------------------------------------------------------------------
chk("no '+ New task' affordance in the toolbar (intake is Jira-only)",
    "New task" not in bar,
    "the '+ New task' menu still renders in the control bar")

chk("no ad-hoc /api/run build form in the toolbar",
    re.search(r"action=/api/run(?![\w-])", bar) is None,
    "a <form action=/api/run> is still rendered in the control bar")

chk("the '&#9654; Run' submit button is gone with its form",
    "&#9654; Run" not in bar)

chk("the free-text task/bug intake fields are gone (name=text / name=dryrun / name=screenshot)",
    not any(f in bar for f in ("name=text", "name=dryrun", "name=screenshot")),
    "leftover New-task form inputs still render")

# The Jira-driven intake and the other build-cluster controls must survive untouched.
chk("Jira intake nav link survives (/jira?app=)", "/jira?app=automatixy" in bar)
chk("Autopilot form survives (action=/api/autopilot)", "action=/api/autopilot" in bar)

# ---------------------------------------------------------------------------
# 2. Patrol + Ship review reachable from ONE QA control
# ---------------------------------------------------------------------------
# 2026-07-19 (Commander order): Patrol + Ship-review merged into ONE "Run QA" action —
# the two buttons ran near-identical officer inspections. Both FLOWS survive inside the
# merged /api/qa worker (pinned against server source below); the toolbar carries one button.
chk("the merged QA form posts /api/qa", "action=/api/qa" in bar)
chk("Run QA label glyph present", "&#128269; Run QA" in bar)
chk("the old Patrol form is gone", "action=/api/patrol" not in bar)
chk("the old Ship review form is gone", "action=/api/ship-review" not in bar)
_ssrc = (ROOT / "orchestrator" / "server.py").read_text(encoding="utf-8")
chk("the merged endpoint still runs the patrol flow (findings filed)",
    "patrol_mod.patrol(cfg, app_name, do_file=True" in _ssrc)
chk("the merged endpoint still runs the ship-review flow (readiness verdict)",
    "council.ship_review(cfg, app_name" in _ssrc)

# Structural: isolate the QA cluster and assert it exposes exactly ONE control, a <details>
# menu whose panel holds both flows — not two sibling top-level buttons.
_qa = re.search(r'<div class=tclu>\s*<span class=tclabel>QA</span>(.*?)</div>\s*</div>', bar, re.S)
chk("a QA cluster still exists in the toolbar", _qa is not None,
    "could not locate the <span class=tclabel>QA</span> cluster")
qa_block = _qa.group(1) if _qa else ""

chk("the QA cluster exposes a single control (the merged Run QA form, no dropdown)",
    qa_block.count("<details class=menu>") == 0 and qa_block.count("action=/api/qa") == 1,
    f"expected exactly one /api/qa form and no dropdown, got: {qa_block[:200]}")

# ---------------------------------------------------------------------------
# 3. Reports menu: no duplicate of a top-level button, no two items per report
# ---------------------------------------------------------------------------
chk("Roster survives as a top-level button (EU-68/EU-94 contract)",
    'class="btn" href="/roster-doc"' in bar)

chk("the Reports menu no longer duplicates the top-level Roster button "
    "(exactly one /roster-doc link in the whole bar)",
    bar.count("/roster-doc") == 1,
    f"found {bar.count('/roster-doc')} /roster-doc references — the Reports 'Unit roster' "
    f"item still duplicates the top-level Roster button")

chk("Token usage + Budget monitor are merged into ONE usage/budget report entry",
    bar.count('href="/usage"') == 1 and bar.count('href="/budget"') == 0,
    f"found {bar.count('href=\"/usage\"')} /usage and {bar.count('href=\"/budget\"')} /budget "
    f"links — the two overlapping usage/budget items are still listed separately")

# The /usage freshness stamp is fed by usage.today_tokens(), which the temp fixture has no data
# for (it renders ""). Stub it so this pins the RENDER path — "each remaining item shows its
# freshness" (AC) — rather than the emptiness of the fixture.
import orchestrator.usage as _usage_mod
_orig_today = _usage_mod.today_tokens
_usage_mod.today_tokens = lambda _c: 12_000
try:
    bar_fresh = V._control_bar(_cfg, "automatixy", healthy=True, is_mac=True)
finally:
    _usage_mod.today_tokens = _orig_today

chk("the merged usage/budget entry keeps its freshness stamp",
    re.search(r'href="/usage">[^<]*12k today', bar_fresh) is not None,
    "the merged /usage entry lost its ' · 12k today' freshness stamp")

# Surviving Reports items must all still be present and each listed exactly once.
for _href in ("/tasks", "/council", "/memory", "/forensics"):
    chk(f"Reports menu link preserved exactly once: {_href}",
        bar.count(f'href="{_href}"') == 1,
        f"found {bar.count(f'href=&quot;{_href}&quot;')} occurrences")

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
print("\n========== EU-289 TOOLBAR CLEANUP QA ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, detail in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
