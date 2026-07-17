"""EU-378 — the STATE OF DEV brief: code-derived, head-spliced, reality-guarded.

The 2026-07-17 memory audit: memory.preamble() spent ~1,889 tok/turn on the living log while 0 of
its 12 lessons were durable dev facts and 5 described officers that DON'T EXIST (the scribe's only
inputs are council transcripts + run outcomes — it can never see the code, so 5 days after c276155
retired the Test Engineer it wrote "deployment complete"). Cost of the class, same week: a fresh
agent wrote a `python3 -c` test against the EU-187 terminal that bans interpreters; a triage sketch
ordered deleting the live Engineering Manager's roster row.

devstate.py is the ROSTER.md counterpart: no LLM writes it, every line derives from code. This
harness IS the reality guard — it re-derives each fact, so the brief can never become the next
UNIT.live.md. It runs in every gate, which makes drift a red suite, not a silent rot.

Pins:
  (1) every brief line re-derives from the code it claims to describe (pipeline, routing flag,
      terminal allowlist, live/retired officers);
  (2) refresh() writes the file and brief() round-trips it; the on-disk brief equals build_doc()
      (a hand-edited or stale file is DRIFT and must fail here);
  (3) size ceiling: the brief is paid every turn by every officer — hard cap enforced by test;
  (4) preamble() splices the brief at the HEAD, before UNIT MEMORY — builder._trim_preamble
      truncates the TAIL, so tail placement is exactly what gets cut on oversize;
  (5) precedence language: the brief block tells officers it outranks remembered lessons;
  (6) wiring: council's daily refresh and loop's post-land site both re-derive it.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, ".")

sdk = types.ModuleType("claude_agent_sdk")
sdk.__getattr__ = lambda n: (lambda *a, **k: None)
sys.modules.setdefault("claude_agent_sdk", sdk)

from orchestrator import devstate, memory, phases, routing  # noqa: E402
from orchestrator.officers import OFFICER_NAMES  # noqa: E402
from orchestrator.roster import _OFFICER_ROWS  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


doc = devstate.build_doc()

# ── (1) every fact re-derives from code ──────────────────────────────────────────────────────
ok("(1) pipeline line matches phases.PHASES exactly",
   " → ".join(phases.PHASES) in doc, f"PHASES={phases.PHASES}")
ok("(1b) no phantom Tests phase claimed", "Tests" not in " → ".join(phases.PHASES))

routing_on = routing.is_routing_enabled()
ok("(1c) the routing line matches the LIVE flag",
   ("ROUTING_ENABLED is ON" in doc) == routing_on and
   ("ROUTING_ENABLED is OFF" in doc) == (not routing_on),
   f"is_routing_enabled()={routing_on}")

try:
    from orchestrator.server import TERMINAL_ALLOWED_COMMANDS
    ok("(1d) the terminal allowlist line matches server.TERMINAL_ALLOWED_COMMANDS",
       ", ".join(sorted(TERMINAL_ALLOWED_COMMANDS)) in doc)
    ok("(1e) no interpreter is in the allowlist (the line's premise)",
       not ({"python", "python3", "node", "bun", "sh", "bash"} & set(TERMINAL_ALLOWED_COMMANDS)))
except ImportError:
    ok("(1d) server import unavailable — line dropped from brief", "EU-187" not in doc)

live = [key for key, *_ in _OFFICER_ROWS]
retired = sorted(set(OFFICER_NAMES) - set(live))
ok("(1f) LIVE officers = the roster rows", ", ".join(live) in doc)
ok("(1g) RETIRED officers = label keys without a roster row", ", ".join(retired) in doc,
   f"retired={retired}")
ok("(1h) test_engineer is in the retired set (c276155)", "test_engineer" in retired)

# ── (2) drift detection FIRST, then refresh/round-trip ───────────────────────────────────────
# The drift guard must read the file BEFORE this test refreshes it — comparing after refresh is a
# tautology (caught by mutation: a tampered file passed). Absent file = fresh worktree (the brief
# is gitignored), not drift — skip, don't fail.
if devstate.DEVSTATE_PATH.exists():
    ok("(2-pre) the on-disk brief matches a fresh derivation (drift/hand-edit = red suite)",
       devstate.DEVSTATE_PATH.read_text(encoding="utf-8") == doc,
       "the file no longer matches the code — a hand edit or a stale build is drift; "
       "run devstate.refresh()")
else:
    ok("(2-pre) no on-disk brief (fresh worktree) — drift check skipped", True)
p = devstate.refresh()
ok("(2) refresh writes memory/DEV_STATE.md", p.exists() and p.name == "DEV_STATE.md")
ok("(2b) brief() round-trips the rendered doc", devstate.brief() == doc.strip())

# ── (3) size ceiling — paid every turn by every officer ──────────────────────────────────────
ok("(3) the brief stays under 4000 chars (~1000 tokens)", len(doc) < 4000, f"len={len(doc)}")

# ── (4)+(5) preamble head-splice + precedence ────────────────────────────────────────────────
pre = memory.preamble()
ds_at = pre.find("STATE OF DEV")
um_at = pre.find("UNIT MEMORY")
ok("(4) preamble carries the brief", ds_at != -1)
ok("(4b) the brief precedes UNIT MEMORY (head placement — survives tail truncation)",
   um_at == -1 or ds_at < um_at, f"ds@{ds_at} um@{um_at}")
ok("(5) precedence language: the brief outranks remembered lessons",
   "THIS wins" in pre[ds_at:ds_at + 200] if ds_at != -1 else False)

# head placement must survive builder trimming at any limit that keeps the head
from orchestrator.builder import _trim_preamble
trimmed = _trim_preamble(pre, types.SimpleNamespace(builder_preamble_max_chars=len(pre) // 2))
ok("(4c) a trimmed preamble still carries the brief (the tail is what gets cut)",
   "STATE OF DEV" in trimmed)

# ── (6) refresh wiring ───────────────────────────────────────────────────────────────────────
csrc = Path("orchestrator/council.py").read_text()
lsrc = Path("orchestrator/loop.py").read_text()
ok("(6) council's daily ceremony re-derives the brief (next to roster.refresh)",
   "devstate.refresh(cfg)" in csrc)
ok("(6b) a land re-derives the brief (next to _record_changelog)",
   "devstate.refresh(cfg)" in lsrc)

print(f"\n{checks}/{checks} passed")
