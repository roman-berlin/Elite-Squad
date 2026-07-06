"""detect_domain_gap parser coverage — the surviving read-only domain classifier (squad.py).

The Dev Team Lead's build-delegation squad was removed (Phase-2 §2 flag-off collapse), but
``detect_domain_gap`` survives: it is still called by the Engineering Manager's advisory roster
preview (``adjutant.propose``). The old squad suites that exercised its parser were deleted with the
squad, so this focused harness drives the REAL parser across every branch — covered / uncovered,
domain normalization, prose-wrapped JSON, absent/empty fields, and the no-JSON / malformed / SDK-error
fail-safes — so a regression can't slip through in the window before Slice C removes it.

Offline: the SDK + run_agent are stubbed; no models, no network.
"""
import asyncio
import sys
import types

sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(self, *a, **k): self.__dict__.update(k)
    def __call__(self, *a, **k): return self


sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import squad                    # noqa: E402
from orchestrator.agent import AgentRun           # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


_reply = {"text": ""}


async def _fake(prompt, options, tag="", ticket_id=None, **kw):
    return AgentRun(text=_reply["text"], final=_reply["text"], cost_usd=0.01, num_turns=1,
                    is_error=False, tools=[], input_tokens=10, output_tokens=5)


squad.run_agent = _fake


def _run(reply):
    _reply["text"] = reply
    return asyncio.run(squad.detect_domain_gap("ticket text", squad.SQUAD, ticket_id="EU-1"))


# covered=true -> (False, None): a lane already handles it, no gap.
chk("covered=true -> (False, None)", _run('{"covered": true, "domain": "vanguard-fe"}')[:2] == (False, None))

# covered=false + domain -> (True, domain): a real gap routes to the named domain.
chk("uncovered -> (True, domain)", _run('{"covered": false, "domain": "mql5"}')[:2] == (True, "mql5"))

# domain is lower-cased (so 'Rust' and 'rust' can't diverge downstream).
chk("domain is lower-cased", _run('{"covered": false, "domain": "Rust"}')[:2] == (True, "rust"))

# prose-wrapped JSON: the regex pulls the first {...} out of a chatty reply.
chk("prose-wrapped JSON is extracted",
    _run('Sure! Classification: {"covered": false, "domain": "solidity"} — hope that helps.')[:2]
    == (True, "solidity"))

# empty domain -> None (an uncovered-but-blank domain must not misroute to "").
chk("empty domain -> (True, None)", _run('{"covered": false, "domain": ""}')[:2] == (True, None))

# absent 'covered' key -> defaults to covered=True -> (False, None) (conservative: don't invent a gap).
chk("absent 'covered' defaults to covered -> (False, None)", _run('{"domain": "whatever"}')[:2] == (False, None))

# no JSON at all -> fail-safe (False, None) (the `if not m` branch).
chk("no JSON -> fail-safe (False, None)", _run('no json here, sorry')[:2] == (False, None))

# JSON-shaped but invalid (unquoted key) -> json.loads raises -> outer fail-safe (False, None).
chk("malformed JSON -> fail-safe (False, None)", _run('{covered: false}')[:2] == (False, None))

# the burn dict is always the 3rd element, carrying the classifier call's own spend.
r = _run('{"covered": true}')
chk("burn dict returned with cost", len(r) == 3 and abs(r[2].get("cost_usd", 0) - 0.01) < 1e-9, str(r[2:]))
chk("burn dict carries tokens", r[2].get("input_tokens") == 10 and r[2].get("output_tokens") == 5, str(r[2]))

# run_agent raising -> (False, None, {}) fail-safe (the classifier never breaks its caller).
async def _boom(prompt, options, tag="", ticket_id=None, **kw):
    raise RuntimeError("model down")

squad.run_agent = _boom
r_err = asyncio.run(squad.detect_domain_gap("t", squad.SQUAD))
chk("run_agent exception -> (False, None, {})", r_err[:2] == (False, None) and r_err[2] == {}, str(r_err))

print("\n========= detect_domain_gap parser QA =========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, d in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({d})" if d and not ok else ""))
print(f"  {passed}/{len(results)} passed")
sys.exit(0 if passed == len(results) else 1)
