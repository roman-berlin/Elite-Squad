"""EU-739 + EU-740 — stop throwing away partial work, and stop blaming tickets for dead backends.

Two measured wastes, both fixed here.

EU-739 AUTOSUBMIT. EU-659 hit the per-ticket budget stop with 283 lines of real work in its
worktree and 10,019,245 tokens spent. Both downstream paths discard that: a split gives fragments a
fresh branch, a park leaves the diff to be reaped with the worktree. Days later sibling tickets
rebuilt the same feature under a different design, and when the branch was finally examined it
conflicted with the shipped work — the 283 lines and the 10M tokens were pure loss. SWE-agent's
rule is the right one: on a cost limit, run a final diff and submit whatever exists, because
partial credit beats an exception. The commit now happens BEFORE the split/park decision, and the
park message names the branch so the work is findable rather than merely present.

EU-740 DEAD BACKEND. infra_classify had no 4xx entry, so when the Qwen secondary answered
HTTP 400 "The free quota has been exhausted" in ~2s with 0 tokens, the notes classified as '' —
the TICKET's fault. EU-476 therefore burned a strike and a full re-plan per attempt: three Planner
calls, ~$3.94, for provably zero possible progress. Anthropic's own error docs state the principle
plainly: retrying a 400 reproduces the 400, unlike a 429/529 which are load. A truthy tag means no
strike against the ticket AND arms the hold — the correct response to a backend that cannot answer.
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

from orchestrator import infra_classify  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


# ── EU-740: the dead-backend class is recognised ────────────────────────────
REAL_400 = ('API Error: 400 {"code":"InvalidParameter","message":"The free quota has been '
            'exhausted. To continue accessing the model on a paid basis, please complete your '
            'payment information"}')
chk("(1) the REAL EU-476 error is classified as infra, not the ticket's fault",
    infra_classify.classify(REAL_400) == "backend-dead", infra_classify.classify(REAL_400))
for txt in ("Model not exist", "insufficient_quota", "invalid_api_key", "authentication_error",
            "The model does not exist or you do not have access"):
    chk(f"(1a) dead-backend phrase recognised: {txt[:34]!r}",
        infra_classify.classify(txt) == "backend-dead", infra_classify.classify(txt))

# it must stay NARROW — none of these may be mistaken for a dead backend
for txt in ("AssertionError: expected 3 got 4",
            "test_foo failed: quota calculation is wrong",
            "ValueError: invalid api key format in the user's config parser test",
            "SyntaxError: unexpected EOF"):
    tag = infra_classify.classify(txt)
    chk(f"(2) a CODE failure is not tagged backend-dead: {txt[:34]!r}",
        tag != "backend-dead", f"tagged {tag!r}")

# the pre-existing taxonomy must be untouched
chk("(3) network/DNS/timeout/5xx classification still works",
    infra_classify.classify("NameResolutionError") == "dns"
    and infra_classify.classify("connection refused") == "network"
    and infra_classify.classify("read timed out") == "timeout"
    and infra_classify.classify("Bad Gateway") == "5xx")
chk("(3a) a turn-limit is still NEVER infra (EU-248 owns it)",
    infra_classify.classify("Claude Code process ended: max turns reached") == "")
chk("(3b) empty input is still not infra", infra_classify.classify("") == "")

# the consumer wiring: a truthy tag means no strike
AUTO = pathlib.Path("orchestrator/autopilot.py").read_text(encoding="utf-8")
chk("(4) a classified report contributes NO strike against the ticket",
    "if auth_probe.is_login_failure(r.notes) or infra_classify.classify(r.notes):" in AUTO
    and "infra.add(r.ticket_id)" in AUTO)

# EU-625 is what carries the provider's text into r.notes — without it nothing reaches classify()
LOOP = pathlib.Path("orchestrator/loop.py").read_text(encoding="utf-8")
chk("(5) the builder's REAL error rides on the report notes (EU-625 feeds this)",
    'notes=("builder process errored — "' in LOOP)


# ── EU-757: the dead_backend SHAPE heuristic must not read "unmeasured" as "instant" ────────
# Live on 2026-07-28 three GLM builds each hit the 3600s wall-clock timeout. A killed builder
# returns no SDK result message, so duration_s keeps its 0.0 default and input_tokens its 0 — and
# the old `duration < 5` test labelled the SLOWEST possible failure "dead backend", aiming the
# diagnosis at the provider's health instead of at its speed. These drive loop._is_dead_backend
# ITSELF — the predicate was extracted from the middle of _run precisely so this could be real.
from orchestrator import loop as _loop  # noqa: E402


def _dead(input_tokens: float, duration_s: float, err: str) -> bool:
    build = types.SimpleNamespace(input_tokens=input_tokens, duration_s=duration_s)
    return _loop._is_dead_backend(build, err)


chk("(11) the REAL 3600s timeout (0 tokens, duration unmeasured) is NOT called a dead backend",
    _dead(0, 0.0, "wall-clock timeout after 3600s") is False)
chk("(11a) …and a genuinely instant 0-token refusal still is",
    _dead(0, 1.8, "API Error: 400 something odd") is True)
chk("(11b) …as is a slow error whose TEXT names a dead provider (the classifier leads)",
    _dead(0, 3600.0, REAL_400) is True)
chk("(11c) a normal build error with real tokens spent is never a dead backend",
    _dead(4210, 92.0, "AssertionError: expected 3 got 4") is False)
chk("(11d) a build object missing the fields entirely never raises",
    _loop._is_dead_backend(types.SimpleNamespace(), "boom") is False)
chk("(12) the error path calls the extracted predicate (not an inline copy that can drift)",
    "_dead_backend = _is_dead_backend(build, _err)" in LOOP)
chk("(12a) …and infra_classify is imported LAZILY there (it imports loop — cycle)",
    "from . import infra_classify as _ic" in LOOP
    and "\nfrom .infra_classify import" not in LOOP)

# ── EU-739: the partial work is committed before the split/park decision ────
_bud = LOOP[LOOP.find('audit.record("ticket_budget_exceeded"'):]
_bud = _bud[:_bud.find("_notify(cfg, f\"⛔")] if "_notify(cfg, f\"⛔" in _bud else _bud[:6000]

chk("(6) the worktree diff is committed at the budget stop",
    "git.commit_all(" in _bud and "partial work at the per-ticket budget stop" in _bud)
chk("(6a) …BEFORE the split runs (so fragments can start from it, not from zero)",
    0 < _bud.find("git.commit_all(") < _bud.find("_try_scrum_split("),
    f"commit@{_bud.find('git.commit_all(')} split@{_bud.find('_try_scrum_split(')}")
chk("(6b) …and it is pushed so the work survives the worktree being reaped",
    "git.push(branch)" in _bud)
chk("(7) the commit is audited with the branch and sha",
    'audit.record("budget_autosubmit"' in _bud and "sha=_saved[:12]" in _bud)
chk("(8) the park message NAMES the branch, so the work is findable",
    "The partial work is SAVED on" in _bud)
chk("(9) it never changes the outcome — a git hiccup is swallowed",
    "budget autosubmit skipped" in _bud)
chk("(9a) …and an ephemeral ticket is skipped (no branch to save to)",
    "not ticket.ephemeral and git.has_changes()" in _bud)
chk("(10) the commit message says plainly this is NOT a finished change",
    "NOT a finished" in _bud and "never reached the gate" in _bud)

print("\n========== EU-739 AUTOSUBMIT + EU-740 DEAD BACKEND ==========")
passed = sum(1 for _, ok, _ in results if ok)
for name, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({det})" if det and not ok else ""))
print("-------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
