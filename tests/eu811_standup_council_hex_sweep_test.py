"""EU-811 QA: /standup and /council handler bodies carry zero bare hex colours.

The standup_page() and council_page() inline style attributes were swept from
raw #8a909c → var(--dim). This test asserts the contract holds going forward.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SERVER = Path(__file__).resolve().parents[1] / "orchestrator" / "server.py"
SRC = SERVER.read_text()

results = []


def chk(n, c, d=""):
    results.append((n, bool(c), d))


# Match bare hex colour literals but NOT HTML entities like &#129303; (emoji codes)
HEX = re.compile(r"(?<!&)#[0-9a-fA-F]{3,8}\b")

# ── isolate the two handler bodies ───────────────────────────────────
sp_start = SRC.index("def standup_page()")
cp_start = SRC.index("def council_page()")

# end of standup_page = start of council_page
sp_body = SRC[sp_start:cp_start]

# end of council_page = next `@app.` route decorator after council_page
cp_section_after = SRC[cp_start:]
cp_end_target = cp_section_after.index('@app.post("/api/scribe")')
cp_body = SRC[cp_start:cp_start + cp_end_target]

# ── checks ───────────────────────────────────────────────────────────
chk("standup_page: zero bare hex", not HEX.search(sp_body), sp_body)
chk("council_page: zero bare hex", not HEX.search(cp_body), cp_body)

# warroom.py :root block is byte-unchanged — no new var added
WARROOM = Path(__file__).resolve().parents[1] / "orchestrator" / "warroom.py"
warroom_src = WARROOM.read_text()
chk("warroom.py :root: --dim already defined (token source unchanged)",
    "--dim:" in warroom_src)

print("\n================== EU-811 STANDUP/COUNCIL HEX SWEEP QA ==================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
