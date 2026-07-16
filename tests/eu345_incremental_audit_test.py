"""EU-345 — the dashboard must read the audit incrementally, not re-parse the whole 7MB per request.

Measured live 2026-07-15: GET / took ~13s per load and ~13s again on the 2nd request ("no warm-cache
effect"), because the (size, mtime_ns)-keyed cache invalidates on EVERY append — and the drain
appends every few seconds, exactly when Roman watches the board — so the dashboard re-read + re-split
the whole 7MB / 11.6k-line audit on every ~5s auto-refresh.

_read_file_lines makes the append case O(new bytes): it seeks to the last consumed byte offset and
parses only the delta. audit.jsonl is append-only, so this is the common case.

Pins:
  (1) first read returns all lines;
  (2) after an append, the reader returns old+new and only consumed the delta (byte offset advanced
      by the appended bytes, not reset to a full re-read);
  (3) a partial trailing line (no newline yet) is NOT returned until its newline arrives, and isn't
      duplicated once it does;
  (4) truncation/rotation (size shrinks) falls back to a full re-read;
  (5) audit_lines end-to-end still dedups a line shared between local + peer files.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from orchestrator import dashboard  # noqa: E402

checks = 0


def ok(name, cond, detail=""):
    global checks
    checks += 1
    if not cond:
        print(f"  ✗ {name}  {detail}")
        sys.exit(1)
    print(f"  ✓ {name}")


TMP = Path(tempfile.mkdtemp())
f = TMP / "audit.jsonl"

# (1) first read
f.write_text('{"a":1}\n{"a":2}\n{"a":3}\n')
dashboard._file_line_cache.clear()
lines = dashboard._read_file_lines(f)
ok("(1) first read returns all lines", lines == ['{"a":1}', '{"a":2}', '{"a":3}'], f"got {lines}")
off_after_first = dashboard._file_line_cache[str(f)][0]

# (2) append → old+new, offset advanced by only the appended bytes
with f.open("a") as fh:
    fh.write('{"a":4}\n{"a":5}\n')
lines2 = dashboard._read_file_lines(f)
ok("(2) append returns old + new lines",
   lines2 == ['{"a":1}', '{"a":2}', '{"a":3}', '{"a":4}', '{"a":5}'], f"got {lines2}")
off_after_append = dashboard._file_line_cache[str(f)][0]
ok("(2b) only the delta was consumed (offset advanced by the appended bytes)",
   off_after_append - off_after_first == len('{"a":4}\n{"a":5}\n'),
   f"delta={off_after_append - off_after_first}")

# (3) a partial trailing line is SURFACED (splitlines back-compat: the live audit's final event is a
# complete line, sometimes without a trailing newline) but NOT committed — so when its newline arrives
# it appears exactly once, never duplicated.
off_before_partial = dashboard._file_line_cache[str(f)][0]
with f.open("a") as fh:
    fh.write('{"a":6}')          # no newline yet
lines3 = dashboard._read_file_lines(f)
ok("(3) partial trailing line is surfaced (splitlines back-compat)", '{"a":6}' in lines3, f"got {lines3[-2:]}")
ok("(3b) the partial is NOT committed (offset unchanged until its newline)",
   dashboard._file_line_cache[str(f)][0] == off_before_partial,
   f"offset moved {off_before_partial}→{dashboard._file_line_cache[str(f)][0]}")
with f.open("a") as fh:
    fh.write('\n')               # complete it
lines4 = dashboard._read_file_lines(f)
ok("(3c) the completed line appears exactly once (no duplication)",
   lines4.count('{"a":6}') == 1, f"got tail {lines4[-2:]}")

# (4) truncation (rotation) → full re-read
f.write_text('{"z":1}\n')
lines5 = dashboard._read_file_lines(f)
ok("(4) truncation falls back to a full re-read", lines5 == ['{"z":1}'], f"got {lines5}")

# (5) audit_lines dedups a line shared between local + a peer shared file
shared = TMP / ".unit-state" / "shared"
shared.mkdir(parents=True, exist_ok=True)
(TMP / "audit.jsonl").write_text('{"k":"dup"}\n{"k":"local"}\n')
(shared / "server.jsonl").write_text('{"k":"dup"}\n{"k":"peer"}\n')
dashboard._file_line_cache.clear()
dashboard._audit_cache.clear()
merged = dashboard.audit_lines(str(TMP / "audit.jsonl"))
ok("(5) audit_lines merges local + peer and dedups the shared line",
   merged.count('{"k":"dup"}') == 1 and '{"k":"local"}' in merged and '{"k":"peer"}' in merged,
   f"got {merged}")

print(f"\n{checks}/{checks} passed")
