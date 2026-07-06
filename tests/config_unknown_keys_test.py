"""Config tolerance for retired / unknown YAML keys (Phase-2 §2, 2026-07-06).

Config.load did `Config(apps=apps, **data)`, and a dataclass __init__ raises TypeError on an
unexpected keyword. So any field retired by the officer collapse (autonomy_*, smalltalk_prob,
prebuild_gate_enabled, liaison_*) still present in a DEPLOYED config.yaml — which the Mac/VPS
maintain by hand and self-update from main — would brick the process at startup. The loader now
drops unknown keys with a warning. Pinned here so a future retirement never re-introduces the
boot-crash, while a genuinely unknown key still can't silently corrupt a KNOWN field.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, ".")

from orchestrator.config import Config, _known_only, AppConfig  # noqa: E402

results = []
def chk(n, c, d=""):
    results.append((n, bool(c), d))


d = Path(tempfile.mkdtemp())
repo = d / "app"
repo.mkdir()
subprocess.run(["git", "init", "-q", str(repo)], check=True)   # validate() requires a git repo

_YAML = f"""\
builder_model: "claude-sonnet-5"
reviewer_model: "claude-opus-4-8"
max_iterations: 2
# --- retired Phase-2 §2 keys that a deployed config.yaml may still carry ---
autonomy_enabled: true
autonomy_cooldown_min: 45
smalltalk_prob: 0.15
random_meeting_prob: 0.06
meeting_on_security_block: true
parks_meeting_threshold: 3
prebuild_gate_enabled: false
liaison_enabled: true
liaison_external_chat_ids: ["-100999"]
# --- a plausible typo, also dropped ---
buildr_model: "oops"
apps:
  - name: automatixy
    repo_path: "{repo}"
    base_branch: "dev"
    backlog_backend: "none"
    autonomy_enabled: true          # retired key nested in an app block too
    bogus_app_key: 1
"""

cfg_path = d / "config.yaml"
cfg_path.write_text(_YAML, encoding="utf-8")

# The load must SUCCEED (no TypeError) despite the retired + typo keys.
try:
    cfg = Config.load(cfg_path)
    loaded = True
except TypeError as exc:
    loaded = False
    cfg = None
    chk("Config.load tolerates retired/unknown keys (no TypeError brick)", False, str(exc))

if loaded:
    chk("Config.load tolerates retired/unknown keys (no TypeError brick)", True)
    chk("known keys still applied", cfg.builder_model == "claude-sonnet-5" and cfg.max_iterations == 2,
        f"{cfg.builder_model},{cfg.max_iterations}")
    chk("retired top-level keys are not attributes", not hasattr(cfg, "autonomy_enabled")
        and not hasattr(cfg, "smalltalk_prob") and not hasattr(cfg, "liaison_enabled"),
        "a retired key leaked onto Config")
    chk("app block loaded, retired app key dropped", cfg.apps and cfg.apps[0].name == "automatixy"
        and not hasattr(cfg.apps[0], "autonomy_enabled"), str(cfg.apps))

# _known_only unit behaviour: filters, keeps knowns, tolerant of non-dict.
filtered = _known_only(Config, {"builder_model": "m", "autonomy_enabled": True}, where="t")
chk("_known_only keeps known keys", filtered.get("builder_model") == "m")
chk("_known_only drops unknown keys", "autonomy_enabled" not in filtered)
chk("_known_only tolerates a non-dict", _known_only(Config, None, where="t") == {})

passed = sum(1 for _, ok, _ in results if ok)
print(f"\nconfig_unknown_keys_test: {passed}/{len(results)} passed")
for n, ok, det in results:
    if not ok:
        print(f"  FAIL: {n}" + (f" — {det}" if det else ""))
sys.exit(0 if passed == len(results) else 1)
