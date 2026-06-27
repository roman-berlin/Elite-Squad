"""EU-66 [Feature] — the Mayor: the unit's inter-unit liaison OFFICER.

EU-65 stood up the isolated outward channel; EU-66 makes it a first-class OFFICER ("Mayor", stable
internal key ``liaison``) and pins the officer-level acceptance the Commander signed off on:

  1. NAMING — OFFICER_NAMES maps the STABLE key ``liaison`` to the Commander-decided display name
     "Mayor" (keys never change, only display names do — officers.py doctrine), and the EU-57 naming
     doc mirrors it.
  2. ROSTER + HIERARCHY — the Mayor shows up in the roster doc, the mermaid chain-of-command chart
     (hung directly off the CTO), and the cockpit roster view, with its label derived from the single
     source of truth so it can't drift.
  3. CHARTER — orchestrator/liaison.py exposes a PUBLIC ``LIAISON_SYSTEM`` prompt (same shape as
     ``PROVOST_SYSTEM`` / ``ADJUTANT_SYSTEM``) that casts the Mayor as a warm AMBASSADOR and hard-codes
     the untrusted-input / no-exfil / no-task-execution boundary (mirrors the EU-46/47 guard).
  4. BOUNDARY — the officer only acts on the inter-unit channel: ``status()`` is a pure read (no model,
     no outward post); an inbound message on an INACTIVE channel does nothing; an inbound message with
     no @mention is logged as untrusted DATA but never answered (the Mayor stays silent).
  5. CLI — main.py wires ``general liaison`` to that read-only status.

Pure import + string checks; the Agent SDK and the network are stubbed (as every harness does) so the
orchestrator imports cleanly in a fresh checkout / CI without the SDK installed.
"""
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, ".")

_sdk = types.ModuleType("claude_agent_sdk")
class _SDKStub:
    def __init__(self, *a, **k): pass
    def __call__(self, *a, **k): return self
_sdk.__getattr__ = lambda n: _SDKStub
sys.modules["claude_agent_sdk"] = _sdk

from orchestrator.officers import OFFICER_NAMES, display
from orchestrator import roster, liaison, agent, notify, usage
from orchestrator.config import Config

ROOT = Path(__file__).resolve().parent.parent
results: list[tuple[str, bool, str]] = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def _cfg(**kw) -> Config:
    return Config(apps=[], audit_path=str(Path(tempfile.mkdtemp()) / "audit.jsonl"), **kw)


# --- 1) naming: the stable key `liaison` -> the Commander's display name "Mayor" -------------------
chk("OFFICER_NAMES maps the stable key 'liaison' to 'Mayor' (Commander's decision)",
    OFFICER_NAMES.get("liaison") == "Mayor", OFFICER_NAMES.get("liaison"))
chk("display('liaison') resolves to 'Mayor'", display("liaison") == "Mayor")
chk("the internal key stays the stable 'liaison'", "liaison" in OFFICER_NAMES)
naming = (ROOT / "Documentation" / "OFFICER_NAMING.md").read_text(encoding="utf-8")
chk("OFFICER_NAMING.md documents the Mayor + the stable key",
    "Mayor" in naming and "`liaison`" in naming)

# --- 2) roster + hierarchy: the Mayor shows up, hung off the CTO -----------------------------------
cfg = _cfg()
doc = roster.build_doc(cfg, "all quiet")
chk("roster doc lists the Mayor", "Mayor" in doc)
mer = roster.mermaid_chart()
chk("hierarchy chart contains the Mayor", "Mayor" in mer)
chk("hierarchy hangs the Mayor directly off the CTO (a 'G --> … Mayor' edge)",
    any(l.strip().startswith("G --> ") and "Mayor" in l for l in mer.splitlines()))
chk("cockpit roster view renders the Mayor", "Mayor" in roster.html_view(cfg, "all quiet"))
_keys = [k for (k, *_r) in roster._OFFICER_ROWS]
chk("roster registers the Mayor under the stable internal key 'liaison'", "liaison" in _keys)
chk("roster's Mayor label derives from display('liaison') (can't drift from the SOT)",
    any(name == display("liaison") for name, *_n in roster._OFFICERS))

# --- 3) charter: a PUBLIC LIAISON_SYSTEM with the ambassador persona + the hard boundary -----------
sp = liaison.LIAISON_SYSTEM
chk("charter module exposes a public LIAISON_SYSTEM prompt", isinstance(sp, str) and len(sp) > 200)
low = sp.lower()
chk("charter casts the Mayor as the persona", "mayor" in low)
chk("charter frames the Mayor as an ambassador, not an executor",
    "ambassador" in low and ("do not execute" in low or "not a builder" in low))
chk("charter treats allied messages as untrusted external input", "untrusted" in low)
chk("charter refuses instructions embedded in the allied message",
    "never follow" in low and "decline" in low)
chk("charter forbids leaking secrets / credentials", "secret" in low and "credential" in low)
chk("charter forbids leaking ticket / repo internals", "ticket" in low and "repo" in low)
chk("charter forbids taking actions for the allied unit",
    "execute any action" in low or "no builds, deploys" in low)
chk("charter bakes in NO internal standing-orders / unit-memory text",
    "standing orders" not in low and "unit memory" not in low)

# --- 4) boundary: status() is a PURE read — no model call, no outward post -------------------------
_ra, _ns, _tt = agent.run_agent, notify.send, usage.tokens_today_for_tag
_tripped = {"model": False, "post": False}


async def _no_model(*a, **k):
    _tripped["model"] = True
    raise AssertionError("status() must never invoke a model")


def _no_post(*a, **k):
    _tripped["post"] = True
    raise AssertionError("status() must never post outward")


usage.tokens_today_for_tag = lambda c, t: 4242
agent.run_agent, notify.send = _no_model, _no_post
active = _cfg(liaison_enabled=True, liaison_external_chat_ids=["-100999"], liaison_mention_handles=["bot"])
s = liaison.status(active)
chk("status() returns a string naming the Mayor", isinstance(s, str) and "Mayor" in s)
chk("status() reports ACTIVE when the channel is wired", "ACTIVE" in s)
chk("status() invoked NO model (pure read)", _tripped["model"] is False)
chk("status() posted NOTHING outward (pure read)", _tripped["post"] is False)
chk("status() meters the dedicated liaison token tag", "4,242" in s)
agent.run_agent, notify.send, usage.tokens_today_for_tag = _ra, _ns, _tt

# --- 4b) boundary: only acts on the channel; logs untrusted data; silent unless addressed ----------
posts: list[tuple[str, object]] = []
notify.send = lambda text, chat_id=None: posts.append((text, chat_id))

# an INACTIVE channel (the default) never answers / posts.
liaison.handle_external_message(_cfg(), None, "@bot hello?", chat_id="-100999")
chk("inactive channel: an external message triggers no outward post", posts == [])


class _Audit:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def record(self, kind: str, **kw) -> None:
        self.records.append((kind, kw))


# an ACTIVE channel logs the inbound as untrusted DATA but stays silent without an @mention.
au = _Audit()
posts.clear()
liaison.handle_external_message(active, au, "just chatter, nobody is addressed here", chat_id="-100999")
chk("inbound external traffic is recorded as untrusted data (kind 'liaison_msg')",
    any(k == "liaison_msg" for k, _ in au.records))
chk("no @mention -> the Mayor stays silent (no outward post)", posts == [])
notify.send = _ns

# --- 5) CLI: `general liaison` is wired to the read-only status ------------------------------------
main_src = (ROOT / "orchestrator" / "main.py").read_text(encoding="utf-8")
chk("main.py registers a `liaison` CLI command", '"liaison"' in main_src)
chk("the `liaison` command prints the read-only status", "liaison.status(" in main_src)

# --------------------------------------------------------------------------------------------- #
print("\n=============== EU-66 LIAISON OFFICER (MAYOR) ===============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-" * 60)
print(f"  {passed}/{len(results)} passed", "✅" if passed == len(results) else "❌")
sys.exit(0 if passed == len(results) else 1)
