"""End-to-end integration check for EU-604 / EU-595 CTO internal consult feature.

Verifies the whole consult chain end-to-end (EU-600 helpers + EU-602 wiring):
  1. Specialist path → one reply with consult note naming the officer
  2. Generic path → no consult, no regression on common path
  3. Consult failure → degrades silently to CTO's own answer
  4. Consult returns '' (empty) → same graceful degrade

Uses the repo's types.ModuleType('claude_agent_sdk') stub pattern.
"""
import asyncio
import sys
import types

# ── Stub claude_agent_sdk (repo convention from council_*_test.py) ───────
sdk = types.ModuleType("claude_agent_sdk")


class ClaudeAgentOptions:
    def __init__(self, **kw):
        self.__dict__.update(kw)


sdk.ClaudeAgentOptions = ClaudeAgentOptions


class _D:
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from pathlib import Path
import tempfile

from orchestrator import council
from orchestrator.config import Config, AppConfig

results: list[tuple[str, bool, str]] = []


def chk(n: str, c: bool, d: str = ""):
    results.append((n, bool(c), d))


ns = types.SimpleNamespace
cap: dict = {}  # last run_agent call capture


# ── Helpers ───────────────────────────────────────────────────────────────

_SENTINEL = object()


def _make_r(text: str | object = _SENTINEL, **extra):
    """Build a fake Agent SDK run result."""
    final_val = "" if text is _SENTINEL else text
    return ns(final=final_val, text=final_val,
              is_error=False, cost_usd=0.0, num_turns=1, tools=[],
              **(extra or {}))


# ── Shared mocks ─────────────────────────────────────────────────────────

SENT: list[tuple[str, ...]] = []


def _fake_notify(*args, **kwargs):
    SENT.append(args)


async def _fake_report_brief(cfg, text, *a, **kw):
    return text


council.notify = ns(send=_fake_notify, report_brief=_fake_report_brief)
council.add_commander_note = lambda *a, **k: None
council.memory = ns(preamble=lambda: "")
council.filing = ns(parse_tickets=lambda a: ([], a.strip()))
council._file_commander_ticket = lambda *a, **kw: None


def _build_cfg():
    tmp = Path(tempfile.mkdtemp())
    (tmp / "audit.jsonl").write_text("")
    return Config(apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                                  protected_branch="MAIN", backlog_backend="none")],
                  audit_path=str(tmp / "audit.jsonl"), use_worktree=False,
                  smalltalk_model="m")


# ======================================================================== #
# Test 1: Specialist path — consult triggered, note appended
# ======================================================================== #
cap.clear()
SENT.clear()
cfg1 = _build_cfg()

run_count_1 = [0]


async def fake_run_agent_1(prompt, options, tag=None):
    cap.clear()
    cap["prompt"] = prompt
    cap["options"] = options
    cap["tag"] = tag
    run_count_1[0] += 1
    if run_count_1[0] == 1:
        # Triage: picks Security Engineer
        return _make_r("Security Engineer")
    elif run_count_1[0] == 2:
        # Consult: returns brief answer
        return _make_r("No exposed secrets detected in recent commits.")
    else:
        # Main CTO call
        return _make_r("aye, checked — all clear on security.")


council.run_agent = fake_run_agent_1
ans1 = asyncio.run(council.respond_to_commander(cfg1, "is there a secret leaked in the repo?"))

chk("1-a: one reply emitted (single notify.send call)", len(SENT) == 1, f"sent={SENT}")
chk("1-b: CTO answer present in return value", "all clear on security" in ans1.lower(), repr(ans1))
chk("1-c: specialist brief is visible (folded into main prompt)",
    "exposed secrets" in cap.get("prompt", ""), repr(cap.get("prompt", "")[:300]))
chk("1-d: consult note names the officer in the reply",
    ("Security Engineer" in ans1) and ("asked" in ans1.lower() or "consult" in ans1.lower()),
    repr(ans1))
chk("1-e: exactly 3 run_agent calls (triage→consult→main)", run_count_1[0] == 3, f"called {run_count_1[0]} times")

# ======================================================================== #
# Test 2: Generic question — no specialist triaged, no consult note
# ======================================================================== #
cap.clear()
SENT.clear()
cfg2 = _build_cfg()

run_count_2 = [0]


async def fake_run_agent_2(prompt, options, tag=None):
    cap.clear()
    cap["prompt"] = prompt
    cap["tag"] = tag
    run_count_2[0] += 1
    # Triage returns NONE → consult skipped, then main CTO call
    if run_count_2[0] == 1:
        return _make_r("NONE")
    return _make_r("Everything running smoothly today.")


council.run_agent = fake_run_agent_2
ans2 = asyncio.run(council.respond_to_commander(cfg2, "how's it going?"))

chk("2-a: two run_agent calls (triage + main, no consult)", run_count_2[0] == 2, f"called {run_count_2[0]} times")
chk("2-b: no specialist grounding injected", "Specialist grounding" not in cap.get("prompt", ""), repr(cap.get("prompt", "")[:200]))
chk("2-c: no consult note in reply", "Security" not in ans2, repr(ans2))

# ======================================================================== #
# Test 3: Consult failure — RuntimeError raises, degrades gracefully
# ======================================================================== #
cap.clear()
SENT.clear()
cfg3 = _build_cfg()

run_count_3 = [0]


async def fake_run_agent_3(prompt, options, tag=None):
    cap.clear()
    cap["prompt"] = prompt
    cap["tag"] = tag
    run_count_3[0] += 1
    if run_count_3[0] == 1:
        return _make_r("Security Engineer")
    elif run_count_3[0] == 2:
        raise RuntimeError("simulated timeout")
    else:
        return _make_r("checked manually — looks fine.")


council.run_agent = fake_run_agent_3
ans3 = asyncio.run(council.respond_to_commander(cfg3, "is dev ready to ship?"))

chk("3-a: no exception propagates", isinstance(ans3, str), repr(ans3))
chk("3-b: non-empty CTO answer returned", len(ans3) > 0 and ans3 != "(the CTO had no answer)", repr(ans3))
chk("3-c: no consult note when consult failed",
    ("Security Engineer" not in ans3) or ("Consulted" not in ans3 and "consulted" not in ans3),
    repr(ans3))

# ======================================================================== #
# Test 4: Consult returns '' (empty string) — also degrades gracefully
# ======================================================================== #
cap.clear()
SENT.clear()
cfg4 = _build_cfg()

run_count_4 = [0]


async def fake_run_agent_4(prompt, options, tag=None):
    cap.clear()
    cap["prompt"] = prompt
    cap["tag"] = tag
    run_count_4[0] += 1
    if run_count_4[0] == 1:
        return _make_r("Security Engineer")
    elif run_count_4[0] == 2:
        return _make_r("")  # empty response from specialist
    else:
        return _make_r("I reviewed and found nothing.")


council.run_agent = fake_run_agent_4
ans4 = asyncio.run(council.respond_to_commander(cfg4, "any open security tickets?"))

chk("4-a: no exception propagates", isinstance(ans4, str), repr(ans4))
chk("4-b: non-empty CTO answer returned", len(ans4) > 0, repr(ans4))
chk("4-c: no consult note when consult returned empty",
    "Security Engineer" not in ans4, repr(ans4))

# ======================================================================== #
# Summary
# ======================================================================== #
print("\n=========== EU-604 CTO CONSULT E2E QA ===========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    marker = "PASS" if ok else "FAIL"
    extra = f"  ({det})" if det and not ok else ""
    print(f"  [{marker}] {n}{extra}")
print("--------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
if passed == len(results):
    print("  RESULT: ALL GREEN ✅")
else:
    print(f"  RESULT: {len(results) - passed} FAIL ❌")
sys.exit(0 if passed == len(results) else 1)
