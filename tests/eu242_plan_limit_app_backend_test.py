"""EU-242 — the plan-limit banner must offer an alternate for the AFFECTED APP's backend.

_plan_limit_banner derived the current backend from the GLOBAL preference (`backend_pref.active(cfg)`)
while the switch it renders is app-scoped: the form posts `app=<last_run.app>` and the handler calls
`backend_pref.set_active(bk, cfg, app_name=app_param)` (server.py:1183-1186). When the affected app
carries an EU-223 per-app override that differs from global, the two disagree and the banner offers a
backend the app is ALREADY on:

  * global=Opus, app override=GLM  -> offers "Continue on GLM" to an app already running GLM: a no-op
    click that leaves the drain stalled until the limit resets.
  * global=GLM, app override=Opus  -> the more reachable inverse: offers "Continue on Claude (Opus)"
    to an app already on Opus. The handler then clears the banner and auto-resumes on the SAME
    limited backend, which re-hits the limit immediately.

`backend_pref.active(cfg, app_name)` already honours the override, so the fix is to resolve the
affected app FIRST and compute both the offered alternate and the banner's own label through it.

Pins:
  1. global=Opus + app override=GLM  -> the offer is Opus (not the app's own GLM).
  2. global=GLM  + app override=Opus -> the offer is GLM  (not the app's own Opus).
  3. No override -> global behaviour is unchanged (control).
  4. The offered backend is NEVER the affected app's current backend (the invariant behind 1-3).
  5. The banner's own "<backend> plan limit reached" label names the AFFECTED APP's backend too —
     it was global-derived on the same line, so it mislabelled the limit for an overridden app.
  6. The banner still resolves the GLOBAL backend when no app is affected (last_run carries no app).
"""
import sys
import tempfile
import types
from pathlib import Path

# Stub the Agent SDK + requests so importing the orchestrator never reaches the network.
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", sdk)
req = types.ModuleType("requests")
req.Session = lambda *a, **k: types.SimpleNamespace(
    auth=None, headers=types.SimpleNamespace(update=lambda *a, **k: None))
req.RequestException = Exception
sys.modules.setdefault("requests", req)
sys.path.insert(0, ".")

from orchestrator import backend_pref, backends, cockpit_views
from orchestrator.config import AppConfig, Config

tmp = Path(tempfile.mkdtemp())
import subprocess
subprocess.run(["git", "init", "-q", str(tmp)], check=True)
_APP = "automatixy"
cfg = Config(
    apps=[AppConfig(name=_APP, repo_path=str(tmp), base_branch="dev", protected_branch="main",
                    backlog_backend="none")],
    audit_path=str(tmp / "a.jsonl"), use_worktree=False)

results: list[tuple[str, bool, str]] = []
def chk(n, c, d=""):
    results.append((n, bool(c), str(d)))

_state = {"plan_limit_hit": True, "plan_limit_reset_at": 1781000000,
          "last_run": {"app": _APP, "tickets": ["AUTO-9"]}}


def _banner(*, global_bk, override=None, app=_APP):
    """Render the banner with a stubbed backend_pref that models the EU-223 per-app override.

    The stub takes (cfg=None, app_name=None) — the REAL active() signature. A one-arg stub would
    raise TypeError inside _plan_limit_banner's `except Exception`, which silently blanks the button
    and would make every pin below fail with a misleading "no offer" instead of a clear error.
    """
    def _active(cfg=None, app_name=None):
        if app_name and override:
            return override
        return global_bk
    st = dict(_state)
    st["last_run"] = {"app": app, "tickets": ["AUTO-9"]} if app else {"tickets": ["AUTO-9"]}
    _orig = backend_pref.active
    backend_pref.active = _active
    try:
        return cockpit_views._plan_limit_banner(st, cfg)
    finally:
        backend_pref.active = _orig


def _offered(html_out):
    """The backend id the Continue button would POST, or None when no offer was rendered."""
    import re
    m = re.search(r"name=backend value='([^']+)'", html_out)
    return m.group(1) if m else None


# GLM must be runnable for the "offer GLM" direction to be reachable at all.
_orig_avail = backends.available
backends.available = lambda bk=None: True

try:
    # ── 1. global=Opus, app override=GLM -> must offer Opus, the app is already on GLM ──────────
    print("\n=== 1: global=Opus + app override=GLM ===")
    h1 = _banner(global_bk=backends.NATIVE, override=backends.GLM)
    chk("an offer is rendered at all", _offered(h1) is not None,
        "no Continue button — check the stub signature")
    chk("offers Opus, NOT the GLM the affected app is already running  ← THE EU-242 pin",
        _offered(h1) == backends.NATIVE,
        f"offered={_offered(h1)!r} while the app's own backend is glm (a no-op click)")
    chk("the button is labelled for Claude (Opus)", "Continue on Claude (Opus)" in h1, h1[:0])
    chk("the banner labels the limit as the APP's backend (GLM), not the global Opus",
        "GLM (Z.ai) plan limit reached" in h1,
        [l for l in h1.split("<") if "plan limit reached" in l])

    # ── 2. global=GLM, app override=Opus -> must offer GLM (the reachable inverse) ──────────────
    print("\n=== 2: global=GLM + app override=Opus ===")
    h2 = _banner(global_bk=backends.GLM, override=backends.NATIVE)
    chk("offers GLM, NOT the Opus the affected app is already running  ← THE EU-242 pin (inverse)",
        _offered(h2) == backends.GLM,
        f"offered={_offered(h2)!r} while the app's own backend is opus — clicking it clears the "
        "banner and auto-resumes on the SAME limited backend")
    chk("the banner labels the limit as the APP's backend (Opus), not the global GLM",
        "Claude (Opus) plan limit reached" in h2,
        [l for l in h2.split("<") if "plan limit reached" in l])

    # ── 3. control: no override -> global behaviour unchanged ───────────────────────────────────
    print("\n=== 3: control — no per-app override ===")
    h3 = _banner(global_bk=backends.NATIVE, override=None)
    chk("no override, global=Opus: still offers GLM (unchanged)", _offered(h3) == backends.GLM,
        f"offered={_offered(h3)!r}")
    h4 = _banner(global_bk=backends.GLM, override=None)
    chk("no override, global=GLM: still offers Opus (unchanged)", _offered(h4) == backends.NATIVE,
        f"offered={_offered(h4)!r}")

    # ── 4. the invariant behind all of the above ────────────────────────────────────────────────
    print("\n=== 4: invariant — the offer is never the affected app's current backend ===")
    for _g, _o in ((backends.NATIVE, backends.GLM), (backends.GLM, backends.NATIVE),
                   (backends.NATIVE, None), (backends.GLM, None)):
        _cur_for_app = _o or _g
        _got = _offered(_banner(global_bk=_g, override=_o))
        chk(f"global={_g} override={_o}: offer {_got!r} != the app's current {_cur_for_app!r}",
            _got is not None and _got != _cur_for_app)

    # ── 5. no affected app -> the GLOBAL backend still resolves ─────────────────────────────────
    print("\n=== 5: no affected app — the global pref still drives the banner ===")
    h5 = _banner(global_bk=backends.GLM, override=backends.NATIVE, app=None)
    chk("last_run carries no app: falls back to the global backend (offers Opus vs global GLM)",
        _offered(h5) == backends.NATIVE, f"offered={_offered(h5)!r}")
    chk("no-app banner labels the limit as the GLOBAL backend", "GLM (Z.ai) plan limit reached" in h5,
        [l for l in h5.split("<") if "plan limit reached" in l])
finally:
    backends.available = _orig_avail


print("\n================ EU-242 PLAN-LIMIT APP BACKEND QA ================")
passed = sum(1 for _, ok, _ in results if ok)
for n, ok, det in results:
    print(f"  [{'PASS' if ok else 'FAIL'}] {n}" + (f"  ({det})" if det and not ok else ""))
print("---------------------------------------------------------------------")
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
