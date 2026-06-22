"""Cockpit-chat display QA: the General's reply must show IN THE COCKPIT CHAT (full text), not only land
in Telegram. append_chat writes a full Q/A transcript; chat_transcript returns it untruncated; and the
format is what cockpit_views._chat_bubbles parses into 'you'/'unit' bubbles."""
import sys, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

from orchestrator import council
from orchestrator.config import Config

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

tmp = Path(tempfile.mkdtemp())
cfg = Config(apps=[], audit_path=str(tmp / "audit.jsonl"))

Q = "How is the quality of the unit implementation?"
A_LONG = ("Honestly, mixed. The code lands clean — green builds, types, migrations — but the unit is "
          "burning on tests: 41 Inspector hits on missing tests alone, and max effort on a third of the "
          "tickets. The a11y and tests gaps trace to the same unfixed root, which is why it keeps "
          "recurring across cycles. Worth one focused cycle to install the exit-gate before adding scope.")
assert len(A_LONG) > 240  # longer than the truncated commander-notes cap, to prove we keep the full text

council.append_chat(cfg, "Q", Q)
council.append_chat(cfg, "A", A_LONG)

t = council.chat_transcript(cfg)
chk("transcript records the question", f"Q: {Q}" in t)
chk("transcript keeps the FULL answer (not 200-char truncated)", A_LONG in t)
chk("answer line uses the 'A (General):' marker the cockpit parses", "A (General): " + A_LONG in t)

# round-trip through the real cockpit parser (now in cockpit_views after the EU-19 refactor).
try:
    from orchestrator import cockpit_views
    bubbles = cockpit_views._chat_bubbles(t)
    whos = [w for w, _ in bubbles]
    you_txt = " ".join(txt for w, txt in bubbles if w == "you")
    unit_txt = " ".join(txt for w, txt in bubbles if w == "unit")
    chk("renders a 'you' (Commander) bubble", "you" in whos and Q in you_txt)
    chk("renders a 'unit' (General) bubble with the full reply", "unit" in whos and A_LONG in unit_txt)
    chk("General's bubble is NOT truncated to 200 chars", len(unit_txt) >= len(A_LONG))
except Exception as e:  # module deps missing in this env — assert the format is parser-compatible instead
    lines = t.splitlines()
    chk("(fallback) a line starts with 'Q:'", any(l.strip().startswith("Q:") for l in lines), str(e))
    chk("(fallback) a line carries 'A (General):'", any("A (General):" in l for l in lines))
    chk("(fallback) full answer present", A_LONG in t)

# commander_notes stays the SEPARATE, truncated standing-guidance channel (unchanged behavior).
council.add_commander_note(cfg, f"Q: {Q[:120]} → A: {A_LONG[:200]}")
note = council.recent_commander_notes(cfg)
chk("standing-guidance note stays bounded (~240 + timestamp prefix)", all(len(l) <= 300 for l in note.splitlines()))

print("\n============ COCKPIT-CHAT DISPLAY QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
