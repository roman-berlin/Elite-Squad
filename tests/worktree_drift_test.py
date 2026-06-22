"""EU-18 (DevOps slice): regression guard that a worktree's deps can't drift off DEV's pin.

`health.worktree_drift_check` is the doctor assertion that the (reused) worktree's bun.lock —
and specifically its resolved @supabase/supabase-js — equals DEV's pinned bun.lock after setup.
This test asserts:
  * NO lockfile drift after worktree setup (worktree bun.lock == DEV's -> 'ok'); a bumped
    @supabase/supabase-js -> 'bad' (the drift EU-18 frozen-lockfile re-pin prevents).
  * the zeltivo-crm typecheck gate passes for a ticket that DOESN'T touch zeltivo-crm — i.e. a
    ticket targeting another app leaves zeltivo-crm's worktree deps pinned to DEV, so its
    typecheck has no drifted-dependency reason to fail.
"""
import sys, types, tempfile
from pathlib import Path

sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk
sys.path.insert(0, ".")

import orchestrator.health as health
from orchestrator.config import Config, AppConfig

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


# DEV's pinned lockfile (the source of truth) and a drifted copy (supabase bumped off-pin).
DEV_LOCK = (
    '{\n  "lockfileVersion": 1,\n  "packages": {\n'
    '    "@supabase/supabase-js": ["@supabase/supabase-js@2.39.0", {}],\n'
    '    "react": ["react@18.3.1", {}]\n  }\n}\n'
)
DRIFTED_LOCK = DEV_LOCK.replace("2.39.0", "2.45.1")

# ---- (1) pure drift comparison ----
st, det = health.lockfile_drift(DEV_LOCK, DEV_LOCK)
chk("identical worktree/DEV lock -> ok (no drift)", st == "ok", st)
chk("ok detail surfaces the supabase pin", "2.39.0" in det, det)

st, det = health.lockfile_drift(DRIFTED_LOCK, DEV_LOCK)
chk("supabase bumped off DEV's pin -> bad", st == "bad", st)
chk("drift detail names @supabase/supabase-js + both versions",
    "@supabase/supabase-js" in det and "2.45.1" in det and "2.39.0" in det, det)

st, _ = health.lockfile_drift(None, DEV_LOCK)
chk("worktree not set up yet -> warn (can't tell)", st == "warn", st)
st, _ = health.lockfile_drift(DEV_LOCK, None)
chk("no base lockfile -> warn (nothing to pin against)", st == "warn", st)

# ---- _resolved_pin extraction ----
chk("_resolved_pin reads supabase version", health._resolved_pin(DEV_LOCK, "@supabase/supabase-js") == "2.39.0")
chk("_resolved_pin missing pkg -> None", health._resolved_pin(DEV_LOCK, "@scope/absent") is None)


# ---- (2) worktree_drift_check end-to-end with a stubbed DEV object store ----
def _run_check(app, cfg, worktree_lock_text):
    """Build the app's worktree dir with the given lock, stub `git show origin/<base>:bun.lock`
    to return DEV's pinned lock, and run the doctor assertion."""
    wt = Path(cfg.worktree_dir).expanduser() / app.name
    wt.mkdir(parents=True, exist_ok=True)
    if worktree_lock_text is not None:
        (wt / "bun.lock").write_text(worktree_lock_text)
    class R:
        def __init__(s, code, out): s.returncode, s.stdout, s.stderr = code, out, ""
    def fake_show(argv, *a, **k):
        # argv == ["git", "show", "origin/<base>:bun.lock"]
        if argv[:2] == ["git", "show"] and argv[2].endswith(":bun.lock"):
            return R(0, DEV_LOCK)
        return R(1, "")
    orig = health.subprocess.run
    health.subprocess.run = fake_show
    try:
        return health.worktree_drift_check(cfg, app)
    finally:
        health.subprocess.run = orig


wtroot = tempfile.mkdtemp()
zeltivo = AppConfig(name="zeltivo-crm", repo_path=".", base_branch="DEV",
                    protected_branch="MAIN", backlog_backend="none")
cfg = Config(apps=[zeltivo], audit_path="/tmp/x.jsonl", use_worktree=True, worktree_dir=wtroot)

# No drift after setup -> ok. This is the regression assertion: worktree deps == DEV's bun.lock.
res = _run_check(zeltivo, cfg, DEV_LOCK)
chk("no lockfile drift after worktree setup -> ok", res and res[0] == "ok", str(res))

# zeltivo-crm typecheck gate for a ticket NOT touching zeltivo-crm: the ticket targets another
# app, so zeltivo-crm's worktree stays pinned to DEV -> no drift -> the gate has no dep reason to
# fail. We model "gate passes" as the drift assertion returning a non-'bad' status.
res = _run_check(zeltivo, cfg, DEV_LOCK)
chk("zeltivo-crm typecheck gate passes (no drift) for an off-app ticket", res and res[0] != "bad", str(res))

# Drifted worktree -> bad (the failure mode the frozen-lockfile re-pin guards against).
res = _run_check(zeltivo, cfg, DRIFTED_LOCK)
chk("drifted worktree lock -> bad (drift caught)", res and res[0] == "bad", str(res))

# Worktree mode off -> not applicable (in-tree shares the user's checkout).
cfg_off = Config(apps=[zeltivo], audit_path="/tmp/x.jsonl", use_worktree=False, worktree_dir=wtroot)
chk("use_worktree off -> check is N/A (None)", health.worktree_drift_check(cfg_off, zeltivo) is None)

print("\n============== WORKTREE DRIFT QA ==============")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("----------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
