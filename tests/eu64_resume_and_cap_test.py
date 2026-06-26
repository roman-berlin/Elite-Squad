"""EU-64: per-project keying of the RESUME threads + the route-level max-parallel-runs cap.

Slices 1 (cockpit_run_state) and the run routes are already pinned by the sibling harnesses
(``cockpit_run_state_test`` / ``eu64_run_routes_test`` / ``eu64_concurrency_test``). This harness
covers the two behaviours the ticket names that those don't reach:

  A. ``decisions._worklist_app_key`` — the per-project key a resume/run derives from its worklist:
     a single-app worklist keys to THAT app's name; an empty / non-tuple / multi-app worklist falls
     back to the unit-wide default (``None``), so a bare ``/drain`` keeps the legacy semantics.

  B. ``decisions._run_bg`` keyed PER PROJECT — a resume for project A runs truly in parallel with a
     resume for project B (neither blocks the other), a SECOND ``refuse_if_busy`` run on the SAME
     project is refused (F7 per-app TOCTOU guard) while a decision RESUME (``refuse_if_busy=False``)
     stays exempt and proceeds, and each thread releases ONLY its own app's slot.

  C. The cockpit run route honours the cross-project CAP — with ``max_parallel_runs=1`` a run on the
     first project proceeds, a concurrent run on a SECOND project is REFUSED through ``/api/run``
     with the "too many projects" banner on that project's own state, and the first stays active.

Stubs the Agent SDK / requests / health / loop so it's offline and fast. Soft ``k/n passed`` tally so
``tests/run_all.py`` (the EU-44 gate) judges it honestly.
"""
import sys, types, tempfile, threading, time
from pathlib import Path

# --- stub the Agent SDK + requests so importing the orchestrator needs no network / models. ---
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
req = types.ModuleType("requests")
req.Session = lambda: types.SimpleNamespace(auth=None,
                                            headers=types.SimpleNamespace(update=lambda *a, **k: None))
sys.modules["requests"] = req
sys.path.insert(0, ".")

import orchestrator.server as srv
import orchestrator.decisions as decisions
import orchestrator.loop as loop_mod
from orchestrator import cockpit_state
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


def _app(name, repo):
    return AppConfig(name=name, repo_path=repo, base_branch="DEV",
                     protected_branch="MAIN", backlog_backend="none")


# ==================================================================================================
# A — _worklist_app_key maps a worklist to its per-project run-state key.
# ==================================================================================================
alpha, beta = _app("alpha", "."), _app("beta", ".")
tk = object()   # the ticket half of a worklist item is irrelevant to the key

chk("single-app worklist keys to that app's name",
    decisions._worklist_app_key([(alpha, tk)]) == "alpha",
    decisions._worklist_app_key([(alpha, tk)]))
chk("multi-item single-app worklist still keys to that one app",
    decisions._worklist_app_key([(alpha, tk), (alpha, tk)]) == "alpha")
chk("empty worklist falls back to the unit-wide default key (None)",
    decisions._worklist_app_key([]) is None)
chk("None worklist falls back to the default key (None)",
    decisions._worklist_app_key(None) is None)
chk("multi-app worklist (bare /drain) falls back to the default key (None)",
    decisions._worklist_app_key([(alpha, tk), (beta, tk)]) is None)
chk("non-tuple worklist items fall back to the default key (None)",
    decisions._worklist_app_key(["wl"]) is None)


# ==================================================================================================
# B — decisions._run_bg is keyed per project (parallel resumes; same-project refuse; resume exempt).
# ==================================================================================================
cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(0)
decisions.notify.send = lambda *a, **k: None

started_keys = []
started_lock = threading.Lock()
release = threading.Event()
async def fake_run_loop(cfg, worklist, audit):
    with started_lock:
        started_keys.append(decisions._worklist_app_key(worklist))
    release.wait(3)
loop_mod.run = fake_run_loop

d = Path(tempfile.mkdtemp()); (d / "audit.jsonl").write_text("")
cfg = Config(apps=[_app("alpha", str(d)), _app("beta", str(d))],
             audit_path=str(d / "audit.jsonl"), use_worktree=False)

wl_alpha = [(cfg.apps[0], tk)]
wl_beta = [(cfg.apps[1], tk)]

# two DIFFERENT projects resume in parallel — neither blocks the other.
chk("resume for alpha starts", decisions._run_bg(cfg, None, wl_alpha, refuse_if_busy=True) is True)
chk("resume for beta starts in parallel (not blocked by alpha)",
    decisions._run_bg(cfg, None, wl_beta, refuse_if_busy=True) is True)
for _ in range(60):
    if len(started_keys) >= 2:
        break
    time.sleep(0.05)
chk("both projects' resume threads actually entered run_loop", len(started_keys) == 2,
    f"started={started_keys}")
chk("alpha run is active", cockpit_state.is_active("alpha"))
chk("beta run is active", cockpit_state.is_active("beta"))

# a SECOND refuse_if_busy run on the SAME project (alpha) is refused while alpha is live.
chk("second /run|/drain on the SAME project is refused",
    decisions._run_bg(cfg, None, wl_alpha, refuse_if_busy=True) is False)
# beta is untouched by alpha's refusal.
chk("beta still active after alpha's refusal", cockpit_state.is_active("beta"))

# a decision RESUME (refuse_if_busy=False) is exempt — it proceeds even while alpha is live.
n_before = len(started_keys)
chk("a decision resume runs even while that project is live (exempt)",
    decisions._run_bg(cfg, None, wl_alpha, refuse_if_busy=False) is True)
for _ in range(60):
    if len(started_keys) >= n_before + 1:
        break
    time.sleep(0.05)
chk("the exempt resume actually entered run_loop", len(started_keys) == n_before + 1,
    f"started={started_keys}")

# release everything; each thread clears ONLY the slot it owns.
release.set()
for _ in range(60):
    if not (cockpit_state.is_active("alpha") or cockpit_state.is_active("beta")):
        break
    time.sleep(0.05)
chk("both projects release their own slot when their resume ends",
    not cockpit_state.is_active("alpha") and not cockpit_state.is_active("beta"))


# ==================================================================================================
# C — the cockpit run route honours the cross-project max-parallel-runs cap.
# ==================================================================================================
cockpit_state.reset_run_state()
cockpit_state.set_max_parallel_runs(1)   # only ONE project may run at a time

cap_started = []
cap_lock = threading.Lock()
cap_release = threading.Event()
async def cap_run_loop(rcfg, worklist, audit, stop_event=None):
    with cap_lock:
        cap_started.append(worklist)
    cap_release.wait(3)
srv.health.summary = lambda c: {"healthy": True, "checks": []}
srv.intake.from_text = lambda rcfg, app, *a, **k: [f"wl:{app}"]
srv.run_loop = cap_run_loop

cap_cfg = Config(apps=[_app("alpha", str(d)), _app("beta", str(d))],
                 audit_path=str(d / "audit.jsonl"), use_worktree=False)
app = srv.create_app(cap_cfg)

# project alpha claims the only slot.
app.test_client().post("/api/run", data={"kind": "task", "text": "a", "app": "alpha"})
for _ in range(60):
    if cap_started:
        break
    time.sleep(0.05)
chk("first project's run proceeds under the cap", cockpit_state.is_active("alpha"))

# a DIFFERENT project (beta) is refused by the cap — not because beta itself is busy.
n_cap = len(cap_started)
app.test_client().post("/api/run", data={"kind": "task", "text": "b", "app": "beta"})
time.sleep(0.2)
chk("second project is refused once the parallel cap is reached", len(cap_started) == n_cap,
    f"started={len(cap_started)}")
chk("beta is NOT marked active when the cap refuses it", not cockpit_state.is_active("beta"))
st_beta = cockpit_state.get_state("beta")
chk("the cap refusal banner lands on beta's own state",
    "too many projects" in (st_beta.get("last_msg") or "").lower(), st_beta.get("last_msg"))
chk("alpha stays active through beta's cap refusal", cockpit_state.is_active("alpha"))

cap_release.set()
for _ in range(60):
    if not cockpit_state.is_active("alpha"):
        break
    time.sleep(0.05)
# with alpha's slot freed, beta can now claim under the cap.
app.test_client().post("/api/run", data={"kind": "task", "text": "b2", "app": "beta"})
for _ in range(60):
    if cockpit_state.is_active("beta"):
        break
    time.sleep(0.05)
chk("beta claims the freed slot once alpha releases", cockpit_state.is_active("beta"))
cap_release.set()
cockpit_state.set_max_parallel_runs(0)   # restore the unlimited default for any later harness


print("\n============ EU-64 RESUME-KEYING + CAP QA ============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("-----------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
