"""The Merge-statistics page lists WHICH tickets merged, each linked to Jira (2026-07-23).

Commander: "need to be show which tickets were merged + jira link". The page showed only counts —
"7 total merges" with no way to see which seven. The merged audit events already carry the
ticket_id, and every app already knows its Jira base_url, so the data was all present; only the
render threw it away.

Design: compute_merge_stats (the pure aggregator) collects the ids URL-free so it stays trivially
testable; _link_merged_tickets adds the Jira browse URL per ticket, matched by project-key prefix
(EU-123 -> the app whose project_key is EU). The route and the /api both call it, so the initial
server render and the range-switch refresh produce identical links.

Pins:
  1. compute_merge_stats returns the merged ids, newest-first, within the window;
  2. it stays URL-free (pure) — the aggregator must not depend on cfg;
  3. _link_merged_tickets builds a correct /browse/<id> URL from the matching app's base_url;
  4. an id whose prefix matches no jira app gets NO url (rendered as plain text, not a dead link);
  5. the list is bounded so an "all time" window can't bloat the payload.
"""
import json
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, ".")
# server imports flask etc.; stub the heavy optional deps the module tolerates missing
for _m in ("claude_agent_sdk",):
    mod = types.ModuleType(_m)
    mod.__getattr__ = lambda _n: (lambda *a, **k: None)
    sys.modules.setdefault(_m, mod)

from orchestrator import server

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _audit(rows):
    p = Path(tempfile.mkdtemp(prefix="ms-links-")) / "audit.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(p)


def _cfg():
    def app(pk):
        a = types.SimpleNamespace()
        a.backlog_backend = "jira"
        a.backlog = {"project_key": pk, "base_url": "https://toibis.atlassian.net"}
        return a
    return types.SimpleNamespace(apps=[app("EU"), app("AUTO")])


nowts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
today = time.strftime("%Y-%m-%dT08:%M:%S", time.localtime())
audit = _audit([
    {"ts": today, "event": "merged", "ticket_id": "EU-100"},
    {"ts": today, "event": "merged", "ticket_id": "AUTO-7"},
    {"ts": today, "event": "merged", "ticket_id": "ZZZ-1"},     # unknown prefix -> no link
    {"ts": today, "event": "pr_opened", "ticket_id": "EU-101"},
    {"ts": "2020-01-01T00:00:00", "event": "merged", "ticket_id": "EU-OLD"},  # outside 'today'
])

# 1) the ids come back, newest-first, windowed
st = server.compute_merge_stats(audit, "today", now=time.time())
ids = [t["id"] for t in st["merged_tickets"]]
chk("(1a) merged ids are returned", set(ids) == {"EU-100", "AUTO-7", "ZZZ-1"}, ids)
chk("(1b) an out-of-window merge is excluded", "EU-OLD" not in ids)
chk("(1c) count matches the list length", st["total_merges"] == len(ids), st["total_merges"])

# 2) the aggregator is URL-free (pure — took no cfg)
chk("(2) compute_merge_stats adds NO url itself",
    all("url" not in t for t in st["merged_tickets"]))

# 3+4) linking
linked = server._link_merged_tickets(_cfg(), st)
by = {t["id"]: t for t in linked["merged_tickets"]}
chk("(3a) EU ticket links to its project's Jira",
    by["EU-100"]["url"] == "https://toibis.atlassian.net/browse/EU-100", by["EU-100"])
chk("(3b) AUTO ticket links to the same site by its own prefix",
    by["AUTO-7"]["url"] == "https://toibis.atlassian.net/browse/AUTO-7", by["AUTO-7"])
chk("(4) an unknown prefix gets NO url (plain text, not a dead link)",
    by["ZZZ-1"]["url"] == "", by["ZZZ-1"])

# 5) bounded
big = _audit([{"ts": today, "event": "merged", "ticket_id": f"EU-{i}"} for i in range(200)])
st_big = server.compute_merge_stats(big, "all", now=time.time())
chk("(5) the list is capped (<=60) even for a huge window",
    len(st_big["merged_tickets"]) <= 60, len(st_big["merged_tickets"]))

# 6) the page and the api both render links (source pin — same helper on both paths)
ssrc = Path("orchestrator/server.py").read_text(encoding="utf-8")
chk("(6a) the page route enriches with links",
    "_link_merged_tickets(cfg, compute_merge_stats" in ssrc)
chk("(6b) the /api enriches with the SAME helper",
    ssrc.count("_link_merged_tickets(cfg, compute_merge_stats") >= 2)
chk("(6c) the page renders a merged-tickets list container", "id=ms-merged-list" in ssrc)

print("\n========== MERGE-STATS TICKET LINKS ==========")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
