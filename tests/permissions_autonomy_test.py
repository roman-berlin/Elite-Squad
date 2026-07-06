"""Autonomy fix QA: (1) every server-side officer runs bypassPermissions so it never dead-stops on a
tool prompt the headless VPS can't answer; (2) the General answers ticket questions from the unit's
OWN backlog (its Jira token) instead of an ambient Atlassian MCP that would stall on a permission grant.
"""
import sys, types, tempfile, asyncio
from pathlib import Path

# --- stub the Agent SDK: a ClaudeAgentOptions that STORES kwargs (so we can assert permission_mode),
#     everything else a permissive dummy ---
sdk = types.ModuleType("claude_agent_sdk")
class ClaudeAgentOptions:
    def __init__(self, **kw): self.__dict__.update(kw)
sdk.ClaudeAgentOptions = ClaudeAgentOptions
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import council
from orchestrator import memory as M
from orchestrator.backlog import base as backlog_base

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

# preamble reads memory files — neutralize so options tests stay pure
M.preamble = lambda: ""

# --- 1. every officer-options builder yields bypassPermissions (no gate, writes still disallowed) ---
cfg = types.SimpleNamespace(discussion_model="sonnet", smalltalk_model="haiku")
oo = council._officer_options(cfg, "lens", "/cwd")
chk("council officer options = bypassPermissions", oo.permission_mode == "bypassPermissions", oo.permission_mode)
chk("council officer still read-only (no Write/Edit/Bash)",
    set(oo.disallowed_tools) >= {"Write", "Edit", "Bash"}, str(oo.disallowed_tools))
go = council._group_options(cfg, "lens", "/cwd")
chk("group-chat options = bypassPermissions", go.permission_mode == "bypassPermissions", go.permission_mode)

# every inline ClaudeAgentOptions in council.py is bypass too (source-level guard against regressions)
src = Path("./orchestrator/council.py").read_text()
chk("zero 'default' gates remain in council.py", 'permission_mode="default"' not in src)
chk("council.py has 7 bypass officers (small-talk deleted, Phase-2 §2)",
    src.count('permission_mode="bypassPermissions"') == 7,
    str(src.count('permission_mode="bypassPermissions"')))

# the other read-only officers (adjutant/drillmaster/reviewer/squad) are flipped as well
root = Path("./orchestrator")
for f in ("adjutant", "drillmaster", "reviewer", "squad"):
    t = (root / f"{f}.py").read_text()
    chk(f"{f}.py has no 'default' gate", 'permission_mode="default"' not in t)

# --- 2. _ticket_context: pull referenced tickets from the unit's OWN backlog ---
class _FakeTicket:
    def __init__(s, key): s.key, s.summary, s.description = key, f"Summary of {key}", "Body  " * 400
calls = {"n": 0}
class _FakeBacklog:
    def get_task(s, key):
        calls["n"] += 1
        return _FakeTicket(key)
    def latest_builder_comment(s, key):  # no unit post on this fake ticket
        return None
def _fake_make_backlog(app):
    return _FakeBacklog()
backlog_base.make_backlog = _fake_make_backlog

jcfg = types.SimpleNamespace(apps=[types.SimpleNamespace(name="automatixy", backlog_backend="jira")])

out = asyncio.run(council._ticket_context(jcfg, "I think it's AUTO-14. Check if this ticket is ok"))
chk("ticket context found AUTO-14", "[AUTO-14]" in out and "Summary of AUTO-14" in out, out[:60])
# EU-70: limit raised from 1500 → 3000 so the Builder's appended comment section is not cut off.
chk("ticket description is truncated (<=3000)", len(out.split("\n", 1)[1]) <= 3000 if "\n" in out else False)

calls["n"] = 0
out2 = asyncio.run(council._ticket_context(jcfg, "compare AUTO-14 and AUTO-14 and ZEL-9"))
chk("dedups repeated keys, keeps distinct", calls["n"] == 2, f"backlog calls={calls['n']}")

chk("no key in message -> empty", asyncio.run(council._ticket_context(jcfg, "hey how's it going")) == "")

# apps with no tracker are skipped (never raises)
ncfg = types.SimpleNamespace(apps=[types.SimpleNamespace(name="x", backlog_backend="none")])
chk("backlog_backend none -> skipped, empty", asyncio.run(council._ticket_context(ncfg, "AUTO-14?")) == "")

# a backlog that raises must not break the chat (best-effort)
class _BoomBacklog:
    def get_task(s, key): raise RuntimeError("jira down")
backlog_base.make_backlog = lambda app: _BoomBacklog()
chk("backlog failure -> empty, no raise", asyncio.run(council._ticket_context(jcfg, "AUTO-14?")) == "")
backlog_base.make_backlog = _fake_make_backlog   # restore for the integration test

# --- 3. respond_to_commander injects the ticket + runs bypass + steers off external trackers ---
captured = {}
async def _fake_run_agent(prompt, options, tag=None):
    captured["prompt"] = prompt
    captured["options"] = options
    return types.SimpleNamespace(final="Looks fine, Commander.", text="", cost_usd=0.0, num_turns=1, tools=[])
council.run_agent = _fake_run_agent
council.notify.send = lambda *a, **k: None
council.collect_signals = lambda cfg: {}
council.format_signals = lambda s: ""

tmp = Path(tempfile.mkdtemp())
rcfg = types.SimpleNamespace(
    apps=[types.SimpleNamespace(name="automatixy", backlog_backend="jira")],
    audit_path=str(tmp / "audit.jsonl"),
    discussion_model="sonnet",
)
answer = asyncio.run(council.respond_to_commander(rcfg, "I think it's AUTO-14. Check if this ticket is ok"))
chk("General replied", answer == "Looks fine, Commander.", answer)
chk("ticket facts injected into the General's prompt",
    "AUTO-14" in captured["prompt"] and "Summary of AUTO-14" in captured["prompt"])
chk("General's chat runs bypassPermissions (no remote wall)",
    captured["options"].permission_mode == "bypassPermissions", captured["options"].permission_mode)
chk("General steered off opening external trackers itself",
    "external tracker" in captured["options"].system_prompt)
chk("General grounded on ALL products, not just one (live cross-project board)",
    "MULTIPLE products" in captured["options"].system_prompt)
chk("General's chat stays read-only",
    set(captured["options"].disallowed_tools) >= {"Write", "Edit", "Bash"})

print("\n============== PERMISSIONS / AUTONOMY QA ==============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
