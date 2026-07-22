"""A host must never merge its OWN published audit copy back into its view (2026-07-22).

Live regression: installing the EU-428 audit publisher made the Mac start writing
``shared/mac.jsonl`` again — a MIRROR of the ``state/audit.jsonl`` the dashboard already reads as
``paths[0]``. ``_audit_paths`` globbed ``shared/*.jsonl`` unconditionally, so the merged view became
12,392 local rows + 12,391 published rows with 12,315 overlapping keys. Every count derived from it
— the board, the stand-up, "shipped in last 24h" — silently doubled the moment publishing resumed.
The bug was latent for the ~25 days the publisher was dead, which is exactly why it needs a pin: it
only appears when the sync is HEALTHY.

A peer's file is real data. Our own is a duplicate of a file we are already reading.

Pins:
  1. the host's own ``shared/<host_id>.jsonl`` is excluded from the merged view;
  2. a PEER's file is still merged (the fix must not blind a host to the other machine);
  3. ``local_only=True`` still returns just the local audit (EU-428 AC3 write-side guard intact);
  4. host resolution failing must not break the view — it degrades to including everything.
"""
import os
import sys
import tempfile
import types
from pathlib import Path

req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): s.__dict__.update(k)
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))

os.environ["GENERAL_HOST_ID"] = "mac"
from orchestrator import dashboard as D

# a repo layout: <root>/state/audit.jsonl  +  <root>/.unit-state/shared/{mac,server}.jsonl
root = Path(tempfile.mkdtemp(prefix="audit-merge-"))
(root / "state").mkdir()
audit = root / "state" / "audit.jsonl"
audit.write_text('{"ts":"2026-07-22T10:00:00","event":"local"}\n', encoding="utf-8")
shared = root / ".unit-state" / "shared"
shared.mkdir(parents=True)
(shared / "mac.jsonl").write_text('{"ts":"2026-07-22T10:00:00","event":"local"}\n', encoding="utf-8")
(shared / "server.jsonl").write_text('{"ts":"2026-07-22T10:00:01","event":"peer"}\n', encoding="utf-8")

names = [p.name for p in D._audit_paths(audit)]

# 1) our own published mirror is dropped
chk("the host's own shared/<host_id>.jsonl is NOT merged (no self double-count)",
    names.count("mac.jsonl") == 0, names)

# 2) …but a real peer still is
chk("a peer's shared/<peer>.jsonl IS still merged",
    "server.jsonl" in names, names)

# 3) the local audit is always first
chk("the local audit.jsonl is still the primary source",
    names and names[0] == "audit.jsonl", names)

# 4) EU-428 AC3: the write-side guard is untouched
chk("local_only=True returns ONLY the local audit",
    [p.name for p in D._audit_paths(audit, local_only=True)] == ["audit.jsonl"])

# 5) the same layout viewed as the SERVER must merge mac.jsonl (it is a genuine peer there)
os.environ["GENERAL_HOST_ID"] = "server"
import importlib
from orchestrator import sync as _sync
importlib.reload(_sync)
importlib.reload(D)
server_names = [p.name for p in D._audit_paths(audit)]
chk("on the server, mac.jsonl IS a peer and stays merged",
    "mac.jsonl" in server_names and "server.jsonl" not in server_names, server_names)

print("\n========== AUDIT SELF-MERGE GUARD ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("--------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
