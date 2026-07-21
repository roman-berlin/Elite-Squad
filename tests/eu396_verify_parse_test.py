"""EU-396 pure-logic pin: reviewer._parse_no_changes_verdict must never trust the model's own
"confident" flag at face value — it is a deterministic backstop, downgraded to False unless every
finding is satisfied=True AND carries non-empty file:line evidence. Offline, no agent calls."""
import sys
import types

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import reviewer

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


def wrap(body: str) -> str:
    return f"prose before\n```json\n{body}\n```\n"


# 1) Model says confident=true, every finding satisfied + cited -> stays confident.
r1 = reviewer._parse_no_changes_verdict(wrap("""
{"confident": true, "findings": [
  {"criterion": "A", "satisfied": true, "evidence": "src/a.ts:10 — present"},
  {"criterion": "B", "satisfied": true, "evidence": "src/b.ts:5 — present"}
], "summary": "both met"}
"""))
chk("all satisfied + cited + model says confident -> confident stays True", r1["confident"] is True, str(r1))

# 2) Model says confident=true but ONE finding unsatisfied -> downgraded to False (real bug this pins).
r2 = reviewer._parse_no_changes_verdict(wrap("""
{"confident": true, "findings": [
  {"criterion": "A", "satisfied": true, "evidence": "src/a.ts:10 — present"},
  {"criterion": "B", "satisfied": false, "evidence": "no offline handling found"}
], "summary": "one unmet"}
"""))
chk("model over-claims confident with an unsatisfied finding -> forced to False", r2["confident"] is False, str(r2))

# 3) Model says confident=true but a satisfied finding has NO evidence string -> downgraded to False.
r3 = reviewer._parse_no_changes_verdict(wrap("""
{"confident": true, "findings": [
  {"criterion": "A", "satisfied": true, "evidence": ""}
], "summary": "no citation"}
"""))
chk("satisfied finding with empty evidence -> forced to False (no bare claims)", r3["confident"] is False, str(r3))

# 4) Model says confident=true but findings list is EMPTY -> downgraded to False (nothing to verify on).
r4 = reviewer._parse_no_changes_verdict(wrap('{"confident": true, "findings": [], "summary": "trust me"}'))
chk("confident=true with zero findings -> forced to False", r4["confident"] is False, str(r4))

# 5) Model itself says confident=false -> stays False even if findings look fine (never upgraded).
r5 = reviewer._parse_no_changes_verdict(wrap("""
{"confident": false, "findings": [
  {"criterion": "A", "satisfied": true, "evidence": "src/a.ts:10 — present"}
], "summary": "cautious"}
"""))
chk("model says not confident -> never upgraded to True", r5["confident"] is False, str(r5))

# 6) Unparseable text -> fails closed (confident False, parse_failed True).
r6 = reviewer._parse_no_changes_verdict("not json at all")
chk("unparseable output -> confident False", r6["confident"] is False, str(r6))
chk("unparseable output -> parse_failed True", r6["parse_failed"] is True, str(r6))


print("\n================ EU-396 VERIFY-PARSE QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results) - passed} FAIL")
if passed != len(results):
    sys.exit(1)
