"""EU-318 — /chat load-earlier offset must not undercount on a burst tick (duplicate bubbles).

Found 2026-07-14 by an adversarial review of code the unit merged to its own dev. Bug 1 (the poll
wholesale-replacing #cinner and wiping "load earlier") was fixed by the EU-305-iter2 seq diff. Bug 2's
root cause survived into /chat's refreshChat: the append loop appendChild-MOVES the new bubbles out of
the fetched fragment, and `total` was then computed by scanning that same (now-drained) fragment — so
on any burst tick total undercounts by the burst size, the load-earlier offset is too small, and a
click within ~5s prepends duplicate bubbles.

Repro (from the ticket): thread shows seq 10-29 (offset 20); 6 new messages arrive (server total 36);
a poll fetches newest-20 (16-35) and moves 30-35 out of frag → frag max=29 → total read as 30 (should
be 36) → off = total-minSeq = 20 (should be 26) → "Load earlier" refetches the 10-29 window → dups.

The JS lives in a Python string (no browser in the suite), so this pins the two structural invariants
that make the arithmetic correct, plus a simulation of the offset math over the ticket's exact repro.

Pins:
  (1) the fetched max seq is captured BEFORE the move loop (source order matters — this is the bug);
  (2) `total` is derived from that pre-move capture, not from a post-move scan of frag;
  (3) simulation: the ticket's exact burst yields off=26 (pre-move) not 20 (post-move, the bug);
  (4) loadEarlierChat dedups by data-seq before prepending (belt-and-braces vs a racing poll).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, ".")

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


src = Path("orchestrator/server.py").read_text()

# Anchor to the /chat refreshChat body (the /group page has its own similar block).
start = src.find("async function refreshChat(force)")
end = src.find("setInterval(function(){refreshChat(false);},5000)")
ok("refreshChat block located", start != -1 and end != -1 and end > start)
block = src[start:end]

# (1)+(2) the capture must come BEFORE the move loop, and total must use it.
cap_at = block.find("var fetchedMax=-1;")
move_at = block.find("oldThread.appendChild(m)")
total_at = block.find("total=fetchedMax+1")
ok("(1) fetched max seq is captured before the append/move loop",
   cap_at != -1 and move_at != -1 and cap_at < move_at,
   "capture must precede the loop that moves those nodes out of frag")
ok("(2) total is derived from the pre-move capture",
   total_at != -1 and "total=fetchedMax+1" in block)
ok("(2b) total is no longer computed by scanning frag after the move",
   "if(s+1>total)total=s+1;" not in block,
   "a post-move scan of frag is exactly the undercount bug")

# (3) simulate the offset math both ways over the ticket's exact repro.
ON_SCREEN = list(range(10, 30))        # thread shows seq 10..29
FETCHED = list(range(16, 36))          # poll fetches newest-20 → 16..35
SERVER_TOTAL = 36                      # 6 new messages arrived


def off_for(total: int) -> int:
    return total - min(ON_SCREEN)


moved_out = [s for s in FETCHED if s > max(ON_SCREEN)]          # 30..35 → appended into oldThread
frag_after_move = [s for s in FETCHED if s not in moved_out]    # what a post-move scan would see
buggy_total = max(frag_after_move) + 1                          # 30 — the undercount
fixed_total = max(FETCHED) + 1                                  # 36 — captured pre-move

ok("(3) pre-move capture yields the true server total", fixed_total == SERVER_TOTAL,
   f"got {fixed_total}")
ok("(3b) the fixed offset points at the true boundary (no overlap)",
   off_for(fixed_total) == 26, f"got {off_for(fixed_total)}")
ok("(3c) the old post-move scan undercounted → overlapping offset (the dup bug)",
   buggy_total == 30 and off_for(buggy_total) == 20,
   f"total={buggy_total} off={off_for(buggy_total)}")

# (4) loadEarlierChat dedups by seq before prepending.
le_start = src.find("async function loadEarlierChat(btn)")
le_block = src[le_start:le_start + 1200]
ok("(4) loadEarlierChat drops already-on-screen seqs before prepending",
   "have[m.dataset.seq]=1;" in le_block and "if(have[m.dataset.seq])m.remove();" in le_block,
   "a poll landing between this click's fetch and insert would otherwise double-render")

print(f"\n{checks}/{checks} passed")
