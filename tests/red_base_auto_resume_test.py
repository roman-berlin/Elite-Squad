"""A RED-base park resumes itself once the base is green — no answer needed (2026-07-22).

Live report (Commander): "I answered the tickets and they appeared again … if I ship the answer it
needs to unblock automatically, no more dialogue needed."

What actually happened, from state/audit.jsonl:
  14:23:23  base check on the current dev sha FAILED
  14:25:04  Commander answered           -> decision_resumed
  14:25:17  Commander answered again     -> decision_resumed
  14:25:20  the ticket re-ran, hit the SAME red base, parked again -> a FRESH card

Each answer worked exactly as designed and was immediately undone, because a red base is not a
decision anybody can make: the base tree fails its own gate, and that condition clears when the
base is fixed. The card even offered "Re-queue now — the base is green again", asking the operator
to verify something the unit can read out of red_base_cache.json itself.

Pins:
  1. a red-base park whose repo has a GREEN base verdict NEWER than the park is auto-resumed;
  2. it is dropped from the decision store AND un-parked (both surfaces, or the card returns);
  3. a green verdict OLDER than the park does NOT resume it (that green is what it already failed
     against — this is the loop-breaker's own loop-breaker);
  4. a non-red-base decision (turn-limit, review) is never touched — those DO need a human;
  5. no cache / corrupt cache / no matching repo -> nothing is resumed and nothing raises.
"""
import json
import sys
import tempfile
import time
import types
from pathlib import Path

_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
_sdk.__getattr__ = lambda _n: _D
sys.modules["claude_agent_sdk"] = _sdk
_req = types.ModuleType("requests")
_req.Session = lambda *a, **k: None
_req.RequestException = Exception
sys.modules["requests"] = _req
sys.path.insert(0, ".")

from orchestrator import autopilot

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


class _Audit:
    def __init__(s): s.events = []
    def record(s, ev, **kw): s.events.append((ev, kw))


def _setup(decisions_list, cache, repo="/repo/elite"):
    """A throwaway state dir with a decision store and a red_base_cache."""
    tmp = Path(tempfile.mkdtemp(prefix="redbase-"))
    (tmp / "state").mkdir()
    audit_path = tmp / "state" / "audit.jsonl"
    audit_path.write_text("", encoding="utf-8")
    (audit_path.with_name("red_base_cache.json")).write_text(json.dumps(cache), encoding="utf-8")
    app = types.SimpleNamespace(name="Elite-Unit", repo_path=repo)
    cfg = types.SimpleNamespace(audit_path=str(audit_path), apps=[app])
    autopilot.save_blocked(cfg, {str(d["id"]) for d in decisions_list})
    from orchestrator import decisions as _dec
    _dec._save(cfg, decisions_list)
    return cfg


RED_Q = "Base branch 'dev' is RED before any build — the gate fails on the clean base tree."
now = time.time()

# 1+2) green AFTER the park -> resumed from BOTH stores
cfg = _setup([{"id": "EU-438", "app": "Elite-Unit", "question": RED_Q, "ts": now - 600}],
             {"/repo/elite@abc123": {"passed": True, "ts": now - 60}})
aud = _Audit()
blocked = autopilot._auto_resume_red_base(cfg, autopilot.load_blocked(cfg), aud)
from orchestrator import decisions as _dec
chk("green after the park -> the decision is dropped",
    [p for p in _dec.load(cfg) if p.get("id") == "EU-438"] == [], _dec.load(cfg))
chk("…and the ticket is un-parked too (both surfaces)",
    "EU-438" not in autopilot.load_blocked(cfg) and "EU-438" not in blocked,
    (autopilot.load_blocked(cfg), blocked))
chk("…and it is audited", any(e == "red_base_auto_resumed" for e, _ in aud.events), aud.events)

# 3) green BEFORE the park -> must NOT resume (that is the green it already failed against)
cfg = _setup([{"id": "EU-500", "app": "Elite-Unit", "question": RED_Q, "ts": now - 60}],
             {"/repo/elite@abc123": {"passed": True, "ts": now - 600}})
autopilot._auto_resume_red_base(cfg, autopilot.load_blocked(cfg), _Audit())
chk("a green OLDER than the park does NOT resume it",
    [p.get("id") for p in _dec.load(cfg)] == ["EU-500"], _dec.load(cfg))

# 4) a decision that genuinely needs a human is untouched
cfg = _setup([{"id": "EU-501", "app": "Elite-Unit", "ts": now - 600,
               "question": "The build ran out of turns even after auto-split hit its depth cap."}],
             {"/repo/elite@abc123": {"passed": True, "ts": now - 60}})
autopilot._auto_resume_red_base(cfg, autopilot.load_blocked(cfg), _Audit())
chk("a turn-limit decision is NEVER auto-resumed (it needs a human)",
    [p.get("id") for p in _dec.load(cfg)] == ["EU-501"], _dec.load(cfg))

# 5) a RED cache verdict is not a green
cfg = _setup([{"id": "EU-502", "app": "Elite-Unit", "question": RED_Q, "ts": now - 600}],
             {"/repo/elite@abc123": {"passed": False, "ts": now - 60}})
autopilot._auto_resume_red_base(cfg, autopilot.load_blocked(cfg), _Audit())
chk("a FAILED verdict never resumes anything",
    [p.get("id") for p in _dec.load(cfg)] == ["EU-502"], _dec.load(cfg))

# 6) a green for a DIFFERENT repo must not release this ticket
cfg = _setup([{"id": "EU-503", "app": "Elite-Unit", "question": RED_Q, "ts": now - 600}],
             {"/repo/OTHER@abc123": {"passed": True, "ts": now - 60}})
autopilot._auto_resume_red_base(cfg, autopilot.load_blocked(cfg), _Audit())
chk("a green for another repo does not resume this ticket",
    [p.get("id") for p in _dec.load(cfg)] == ["EU-503"], _dec.load(cfg))

# 7) corrupt cache -> no raise, nothing resumed
cfg = _setup([{"id": "EU-504", "app": "Elite-Unit", "question": RED_Q, "ts": now - 600}], {})
Path(cfg.audit_path).with_name("red_base_cache.json").write_text("{not json", encoding="utf-8")
try:
    autopilot._auto_resume_red_base(cfg, autopilot.load_blocked(cfg), _Audit())
    ok = [p.get("id") for p in _dec.load(cfg)] == ["EU-504"]
except Exception as e:  # noqa: BLE001
    ok = False
chk("a corrupt cache neither raises nor resumes", ok)

print("\n========== RED-BASE AUTO-RESUME ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
