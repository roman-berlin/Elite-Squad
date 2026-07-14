"""EU-305 — auto-refresh appends/patches new chat messages instead of replacing #cinner innerHTML.

Covers the ticket's testable acceptance criteria:
  1. ``_chat_inner(cfg)`` stamps every ``.msg`` bubble with a ``data-seq`` attribute equal to the
     message's absolute index in the full transcript — stable across the default window and an
     ``offset`` batch (EU-304).
  2. The ``/chat`` page's auto-refresh script no longer wholesale-replaces
     ``#cinner`` innerHTML; instead it appends only bubbles with a ``data-seq`` greater than the
     current max and patches the ``.pending`` cards block separately.
  3. ``GET /chat`` still 200s and still carries the 5s ``setInterval`` -> ``/api/chat-thread`` poll;
     ``GET /api/chat-thread`` still 200s with ``data-seq``-tagged bubbles; the EU-304
     ``loadEarlierChat``/offset path is unchanged.
  4. Pinned 'needs-your-call' pending cards still render in ``_chat_inner`` — no regression.
  5. This harness itself is picked up by ``tests/run_all.py`` (a plain ``tests/*_test.py``).
"""
import json
import re
import sys
import tempfile
import types
from pathlib import Path

# ── minimal SDK stub so the orchestrator imports without real deps ──
sdk = types.ModuleType("claude_agent_sdk")


class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s


sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)

req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", req)

sys.path.insert(0, ".")

from orchestrator import cockpit_views as V
from orchestrator import council, server
from orchestrator.config import AppConfig, Config

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond, detail: str = "") -> None:
    results.append((name, bool(cond), str(detail)))


def _fresh_cfg() -> Config:
    tmp = Path(tempfile.mkdtemp())
    return Config(
        apps=[AppConfig(name="automatixy", repo_path=str(tmp), base_branch="DEV",
                         protected_branch="MAIN", backlog_backend="none")],
        audit_path=str(tmp / "audit.jsonl"),
        use_worktree=False,
    )


def _seed(cfg: Config, n: int) -> None:
    """Write n Q/A turns (2n bubbles) to the chat transcript, oldest first."""
    for i in range(n):
        council.append_chat(cfg, "Q", f"question number {i}")
        council.append_chat(cfg, "A", f"answer number {i}")


# ── AC1: every .msg bubble carries a data-seq = absolute index in the full transcript ───────
cfg_big = _fresh_cfg()
_seed(cfg_big, 30)  # 60 bubbles total, well over the 20-message default window

inner = V._chat_inner(cfg_big)
seqs_default = [int(s) for s in re.findall(r'class="msg [a-z]+" data-seq="(\d+)"', inner)]
chk("default window bubbles all carry a data-seq attribute",
    len(seqs_default) == len(re.findall(r'class="msg ', inner)), inner[:200])
# default window = the newest 20 of 60 bubbles -> absolute indices 40..59
chk("default window data-seq values are the transcript's topmost absolute indices",
    seqs_default == list(range(40, 60)), seqs_default)

batch = V._chat_inner(cfg_big, offset=20)
seqs_batch = [int(s) for s in re.findall(r'class="msg [a-z]+" data-seq="(\d+)"', batch)]
chk("older-batch bubbles also carry data-seq, contiguous and older than the default window",
    seqs_batch == list(range(20, 40)), seqs_batch)
chk("data-seq is stable across default window and offset batch (no overlap, no gap)",
    seqs_default[0] == seqs_batch[-1] + 1, (seqs_batch, seqs_default))

# ── AC2: auto-refresh script appends by data-seq, patches .pending separately ────────────────
app = server.create_app(cfg_big)
app.config.update(TESTING=True)
client = app.test_client()

r = client.get("/chat")
chk("GET /chat -> 200", r.status_code == 200, r.status_code)
body = r.get_data(as_text=True)
chk("auto-refresh script no longer wholesale-replaces #cinner innerHTML",
    'getElementById("cinner").innerHTML=await r.text()' not in body
    and "getElementById('cinner').innerHTML=await r.text()" not in body)
chk("auto-refresh script appends bubbles keyed on data-seq",
    "data-seq" in body and "dataset.seq" in body)
chk("auto-refresh script patches the .pending block separately from the thread",
    ".pending" in body)

# ── AC3: /chat still 200s + polls every 5s; /api/chat-thread still 200s w/ data-seq; EU-304 intact
chk("5s auto-refresh setInterval -> /api/chat-thread present, unchanged",
    'setInterval' in body and '"/api/chat-thread"' in body and ',5000)' in body)
chk("EU-304 loadEarlierChat function still present, unchanged offset contract",
    "loadEarlierChat" in body and 'btn.dataset.offset' in body)

r2 = client.get("/api/chat-thread")
chk("GET /api/chat-thread (no offset) -> 200, bubbles carry data-seq", r2.status_code == 200)
api_body = r2.get_data(as_text=True)
chk("default /api/chat-thread poll bubbles carry data-seq",
    len(re.findall(r'data-seq="\d+"', api_body)) == len(re.findall(r'class="msg ', api_body)))

r3 = client.get("/api/chat-thread?offset=20")
chk("GET /api/chat-thread?offset=20 -> 200, EU-304 offset path unchanged", r3.status_code == 200)
older_body = r3.get_data(as_text=True)
chk("offset batch via HTTP still excludes the newest default-window turn",
    "answer number 29" not in older_body and len(re.findall(r'class="msg ', older_body)) > 0)

# ── AC4: pinned 'needs-your-call' cards still render — no regression ────────────────────────
try:
    Path(cfg_big.audit_path).with_name("pending_decisions.json").write_text(
        json.dumps([{"id": "AUTO-9", "question": "ship it?"}]), encoding="utf-8")
    inner_pinned = V._chat_inner(cfg_big)
    pcard_count = len(re.findall(r'class=pcard', inner_pinned))
    chk("pinned decision card still renders on top (no regression)",
        pcard_count == 1 and "AUTO-9" in inner_pinned and "needs-your-call" not in ""
        and "the unit needs your call" in inner_pinned)
except Exception as e:  # noqa: BLE001
    chk("pinned decision card still renders on top (no regression)", False, str(e))


# ── AC5 (iter2): auto-refresh reconciles the 'load earlier' control so a later click stays
# contiguous with whatever the poller has live-appended since page load ─────────────────────
# The poller appends new bubbles to the live thread, so the on-screen window grows while the
# 'load earlier' button's original data-offset goes stale. If a click then re-fetched at the
# stale offset it would PREPEND bubbles that overlap the ones already on screen (duplicates).
# The fix: each tick, set the live button's offset to (server total) - (oldest on-screen seq).
cfg_le = _fresh_cfg()
_seed(cfg_le, 15)  # 30 bubbles, seq 0..29 -> default window shows seq 10..29, button appears
inner0 = V._chat_inner(cfg_le)
onscreen0 = [int(s) for s in re.findall(r'data-seq="(\d+)"', inner0)]
oldest_visible = min(onscreen0)  # 10 — the boundary a 'load earlier' click must stay strictly below
m_btn = re.search(r'class=load-earlier data-offset="(\d+)" data-limit="(\d+)"', inner0)
chk("load-earlier control rendered with data-offset/data-limit when history exists", bool(m_btn),
    inner0[:160])
init_off = int(m_btn.group(1)) if m_btn else 0  # 20

# Simulate more messages arriving after page load; the poller would have appended these newer
# bubbles to the live thread (oldest-visible seq is unchanged — appends only add newer ones).
_seed(cfg_le, 3)  # +6 bubbles -> total 36, seq 30..35 appended live
inner1 = V._chat_inner(cfg_le)
total_new = 1 + max(int(s) for s in re.findall(r'data-seq="(\d+)"', inner1))  # 36
refreshed_off = total_new - oldest_visible  # what the poller sets the live button to: 36-10 = 26

older = V._chat_inner(cfg_le, offset=refreshed_off)
older_seqs = [int(s) for s in re.findall(r'data-seq="(\d+)"', older)]
chk("refreshed 'load earlier' offset fetches a NON-OVERLAPPING older batch (no dup bubbles)",
    older_seqs and max(older_seqs) < oldest_visible, (older_seqs, oldest_visible))
chk("refreshed 'load earlier' offset fetches a CONTIGUOUS older batch (no gap)",
    older_seqs and max(older_seqs) == oldest_visible - 1, (older_seqs, oldest_visible))

# Teeth: the STALE (un-refreshed) offset WOULD overlap the on-screen window — proves the refresh
# is load-bearing, not cosmetic.
stale = V._chat_inner(cfg_le, offset=init_off)  # offset=20 against the grown total 36
stale_seqs = [int(s) for s in re.findall(r'data-seq="(\d+)"', stale)]
chk("un-refreshed (stale) offset WOULD overlap on-screen msgs — shows the fix is necessary",
    stale_seqs and max(stale_seqs) >= oldest_visible, (stale_seqs, oldest_visible))

# Script-source: the poller reconciles '.load-earlier' (insert / update-offset / remove), and
# skips the .pending swap when unchanged (so in-progress reply text isn't wiped every 5s).
r_le = client.get("/chat")
body_le = r_le.get_data(as_text=True)
chk("poller queries the live '.load-earlier' control to reconcile it",
    'querySelector(".load-earlier")' in body_le)
chk("poller UPDATES the live load-earlier data-offset on tick",
    'oldLE.dataset.offset' in body_le)
chk("poller REMOVES the load-earlier control when the fetched fragment no longer has one",
    'oldLE.remove()' in body_le)
chk("poller INSERTS the load-earlier control when it newly appears",
    'insertBefore(newLE' in body_le)
chk("poller skips the .pending outerHTML swap when unchanged (preserves in-progress reply text)",
    'oldPending.outerHTML!==newPending.outerHTML' in body_le)


print("\n============ EU-305 CHAT APPEND REFRESH ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
