"""EU-38 QA — the Builder caps the prior_issues/feedback (and the unit-memory preamble) it feeds back
on retry, so a deep ticket's per-pass input tokens stay bounded. Asserts the cap keeps the NEWEST
points, honours both the item and char ceilings, and tags the build pass to the ledger."""
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

from orchestrator import builder
from orchestrator.config import Config
from orchestrator.contracts import BuildRequest, Ticket

# ---- _cap_feedback: item ceiling, newest-kept ----
issues = [f"point {i}" for i in range(30)]
out = builder._cap_feedback(issues)
check("cap: bounded to <= max_items (+marker)", len(out) <= builder._FEEDBACK_MAX_ITEMS + 1, str(len(out)))
check("cap: keeps the NEWEST point", out[-1] == "point 29", out[-1])
check("cap: drops the OLDEST point", "point 0" not in out)
check("cap: marks how many were elided", "elided" in out[0], out[0])

# ---- _cap_feedback: char ceiling ----
big = ["x" * 5000, "y" * 5000, "newest point"]
o2 = builder._cap_feedback(big)
check("cap: total chars under the char ceiling",
      sum(len(x) for x in o2) <= builder._FEEDBACK_MAX_CHARS + 120, str(sum(len(x) for x in o2)))
check("cap: newest survives the char trim", any("newest point" in x for x in o2))

# ---- _cap_feedback: empty / passthrough ----
check("cap: None -> []", builder._cap_feedback(None) == [])
check("cap: [] -> []", builder._cap_feedback([]) == [])
small = ["a", "b", "c"]
check("cap: small list passes through untouched", builder._cap_feedback(small) == small)

# ---- configurable via cfg ----
cfg = Config(apps=[])
cfg.builder_feedback_max_items = 2
capped = builder._cap_feedback(["one", "two", "three", "four"], cfg)
check("cap: respects cfg.builder_feedback_max_items", "four" in capped and "one" not in capped, str(capped))

# ---- cfg char ceiling drives an independent trim (item count is fine, chars are not) ----
cfg_chars = Config(apps=[])
cfg_chars.builder_feedback_max_items = 10            # plenty of item room...
cfg_chars.builder_feedback_max_chars = 120           # ...but a tight char budget forces a trim
cc = builder._cap_feedback(["a" * 100, "b" * 100, "newest" * 5], cfg_chars)
check("cap: cfg.builder_feedback_max_chars trims by chars even when item count fits",
      sum(len(x) for x in cc) <= cfg_chars.builder_feedback_max_chars + 120, str(sum(len(x) for x in cc)))
check("cap: char-trim keeps the NEWEST item, drops the oldest",
      any("newest" in x for x in cc) and not any(x == "a" * 100 for x in cc), str(cc))

# ---- the cfg-tightened cap actually reaches the prompt fed to the Builder ----
tk2 = Ticket(id="EU-38", key="EU-38", summary="t", description="d", acceptance_criteria=["a"])
req2 = BuildRequest(ticket=tk2, branch="dev", iteration=2,
                    prior_issues=[f"old {i}" for i in range(20)] + ["the freshest issue"])
cfg_tight = Config(apps=[])
cfg_tight.builder_feedback_max_items = 1
prompt2 = builder._prompt(req2, cfg_tight)
check("prompt: cfg cap reaches _prompt — newest kept", "the freshest issue" in prompt2)
check("prompt: cfg cap reaches _prompt — older dropped", "- old 0" not in prompt2 and "- old 18" not in prompt2)

# ---- the prompt fed to the Builder is actually capped ----
tk = Ticket(id="EU-38", key="EU-38", summary="heavy ticket", description="x", acceptance_criteria=["a"])
req = BuildRequest(ticket=tk, branch="dev", iteration=4, prior_issues=[f"issue {i}" for i in range(50)])
prompt = builder._prompt(req)
check("prompt: does NOT contain the oldest issue", "issue 0\n" not in prompt and "- issue 0" not in prompt)
check("prompt: DOES contain the newest issue", "issue 49" in prompt)

# ---- _trim_preamble ----
check("preamble: short text passes through", builder._trim_preamble("short") == "short")
trimmed = builder._trim_preamble("A" * 9000)
check("preamble: oversized text is bounded", len(trimmed) < builder._PREAMBLE_MAX_CHARS + 200, str(len(trimmed)))
check("preamble: truncation is marked", "truncated" in trimmed)
# the Standing Orders live at the HEAD of the preamble — trimming must keep the head, drop the tail
head_tail = builder._trim_preamble("HEAD-ORDERS" + ("Z" * 9000) + "TAIL-OLD")
check("preamble: keeps the head (Standing Orders), drops the tail",
      head_tail.startswith("HEAD-ORDERS") and "TAIL-OLD" not in head_tail, head_tail[:20])

# ---- _trim_preamble: cfg override + the limit<=0 disable branch ----
cfg_pre = Config(apps=[])
cfg_pre.builder_preamble_max_chars = 50
small_limit = builder._trim_preamble("Q" * 4000, cfg_pre)
check("preamble: cfg.builder_preamble_max_chars tightens the bound",
      len(small_limit) < 50 + 120, str(len(small_limit)))
# NOTE: `int(getattr(...) or default)` coalesces 0 -> default, so 0 does NOT disable; only a
# negative value reaches the `limit <= 0` disable branch. Pin BOTH so the behaviour can't drift silently.
cfg_zero = Config(apps=[])
cfg_zero.builder_preamble_max_chars = 0              # 0 falls back to the default cap (still trims)
big_text = "W" * 20000
check("preamble: 0 coalesces to the default cap (still trims oversized text)",
      "truncated" in builder._trim_preamble(big_text, cfg_zero))
cfg_neg = Config(apps=[])
cfg_neg.builder_preamble_max_chars = -1              # negative reaches limit<=0 -> trimming disabled
check("preamble: negative limit disables trimming (full text passes through, no marker)",
      builder._trim_preamble(big_text, cfg_neg) == big_text)

print("\n============ BUILDER CONTEXT (EU-38) QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN ✅" if passed == len(results) else f"{len(results)-passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
