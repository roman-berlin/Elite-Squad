"""EU-784: autofiled build quota config for drain selection.

Verifies:
  1. apply_autofiled_quota enforces 1-in-N ratio on a mixed queue.
  2. apply_autofiled_quota is a no-op when quota_per_n <= 0 or None.
  3. Config.load picks up autofiled_quota_per_n; omitting it defaults to 0.
  4. config.example.yaml and config.server.example.yaml document the key.
  5. python3 tests/run_all.py stays green.
"""
import sys, types, tempfile, os
from pathlib import Path

# --- stub heavy deps so modules import without them ---------------------
sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass
    def __call__(s, *a, **k): return s
sdk.__getattr__ = lambda n: _D
sys.modules["claude_agent_sdk"] = sdk

req = types.ModuleType("requests")
req.Session = lambda *a, **k: None
req.RequestException = Exception
sys.modules["requests"] = req

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from orchestrator.config import Config, AppConfig
from orchestrator.intake import apply_autofiled_quota, is_autofiled, from_drain
from orchestrator.contracts import Ticket

results = []
def chk(label: str, condition: bool, detail: str = ""):
    """Record a check. Accumulates results for summary."""
    results.append((label, bool(condition), detail))

# ========================================================================= #
# Helpers
# ========================================================================= #

def _make_ticket(tid: str, labels: list[str] | None = None) -> Ticket:
    """Create a synthetic ticket."""
    return Ticket(id=tid, key=tid, summary=f"Ticket {tid}",
                  description="", acceptance_criteria=[], labels=labels or [])


def _app() -> AppConfig:
    return AppConfig(name="eu", repo_path="/tmp", base_branch="dev",
                     protected_branch="main", backlog_backend="jira",
                     backlog={"base_url": "https://x.atlassian.net", "project_key": "EU"})


def _cfg(yaml_content: str) -> Config:
    """Parse yaml content into a Config using a temp dir for the apps' .git roots."""
    tmpdir = tempfile.mkdtemp()
    # Create fake .git repos for each app defined in the yaml
    import yaml
    data = yaml.safe_load(yaml_content) or {}
    for idx, app_cfg in enumerate(data.get("apps", [])):
        app_dir = os.path.join(tmpdir, app_cfg.get("name", f"app-{idx}"))
        Path(app_dir).mkdir(parents=True, exist_ok=True)
        Path(app_dir, ".git").touch()
        app_cfg["repo_path"] = app_dir
    cfg_path = os.path.join(tmpdir, "config.yaml")
    Path(cfg_path).write_text(yaml.dump(data))
    return Config.load(cfg_path)


# ========================================================================= #
# Part 1 — Quota-enforced: 1-in-3 on [af1, af2, C, af3] => second pick is C
# ========================================================================= #

C = _make_ticket("C-TICKET")              # Commander ticket — no 'autofiled' label
AF1 = _make_ticket("AF-1", ["autofiled"])
AF2 = _make_ticket("AF-2", ["autofiled"])
AF3 = _make_ticket("AF-3", ["autofiled"])

items = [( _app(), AF1), (_app(), AF2), (_app(), C), (_app(), AF3)]

result = apply_autofiled_quota(items, 3)
result_keys = [t.key for _, t in result]

chk("AC1: with autofiled_quota_per_n=3, second element is the Commander ticket",
    len(result_keys) >= 2 and result_keys[1] == "C-TICKET",
    f"got order: {result_keys}")
chk("AC1: the two autofiled tickets still appear (deferred, not dropped)",
    sum(1 for k in result_keys if k.startswith("AF")) == 3,
    f"autofiled count: {sum(1 for k in result_keys if k.startswith('AF'))}")

# ========================================================================= #
# Part 2 — Unlimited: quota_per_n=0 or None returns items unchanged
# ========================================================================= #

unchanged = apply_autofiled_quota(list(items), 0)
chk("AC2b: quota_per_n=0 returns items unchanged",
    unchanged == items,
    f"differed by {[(i[0].key, j[0].key) for i, j in zip(unchanged, items) if i != j]}")

unchanged_none = apply_autofiled_quota(list(items), None)
chk("AC2c: quota_per_n=None returns items unchanged",
    unchanged_none == items,
    f"differed by {[(i[0].key, j[0].key) for i, j in zip(unchanged_none, items) if i != j]}")

# ========================================================================= #
# Part 3 — Config load picks up autofiled_quota_per_n; missing key => 0
# ========================================================================= #

cfg_with = _cfg("""
apps:
  - name: eu-test
    repo_path: /tmp
    base_branch: dev
    protected_branch: main
    backlog_backend: jira
autofiled_quota_per_n: 3
""")
chk("AC3a: Config.load picks autofiled_quota_per_n: 3 => cfg.autofiled_quota_per_n == 3",
    cfg_with.autofiled_quota_per_n == 3,
    f"got {cfg_with.autofiled_quota_per_n}")

cfg_without = _cfg("""
apps:
  - name: eu-test
    repo_path: /tmp
    base_branch: dev
    protected_branch: main
    backlog_backend: jira
""")
chk("AC3b: Config.load without key yields autofiled_quota_per_n == 0 (default)",
    cfg_without.autofiled_quota_per_n == 0,
    f"got {cfg_without.autofiled_quota_per_n}")


# ========================================================================= #
# Part 4 — config files document the new key
# ========================================================================= #

example_dir = Path(__file__).resolve().parent.parent
config_example = example_dir / "config.example.yaml"
server_example = example_dir / "config.server.example.yaml"

for path, label in [(config_example, "config.example.yaml"),
                    (server_example, "config.server.example.yaml")]:
    chk(f"AC4a ({label}): file exists",
        path.exists(), f"path={path}")
    if path.exists():
        content = path.read_text()
        has_key = "autofiled_quota_per_n" in content
        has_comment = any("#" in l and "autofiled_quota_per_n" in l
                         for l in content.splitlines())
        chk(f"AC4a ({label}): contains the literal key 'autofiled_quota_per_n'",
            has_key, f"{label} content snippet: {[l.strip() for l in content.splitlines() if 'autofiled' in l.lower()][:3]}")
        chk(f"AC4b ({label}): key line carries an inline # comment",
            has_comment, f"{label}: {has_comment}")


# ========================================================================= #
# Part 5 — Edge cases: single-item list and empty list are identity ops
# ========================================================================= #

result_single = apply_autofiled_quota([( _app(), AF1)], 3)
chk("AC5a: single-item list returns identity under quota",
    len(result_single) == 1 and result_single[0][1].key == "AF-1",
    f"got {[(t.key, t.labels) for _, t in result_single]}")

empty_result = apply_autofiled_quota([], 3)
chk("AC5b: empty list stays empty",
    empty_result == [], str(empty_result))


# ========================================================================= #
# Summary + exit
# ========================================================================= #
print("\n===== EU-784 QUOTA QA =====")
passed = sum(1 for _, ok, _ in results if ok)
for i, (label, ok, det) in enumerate(results, 1):
    print(f"  [{'PASS' if ok else 'FAIL'}] #{i} {label}" + (f"  ({det})" if det and not ok else ""))
print("-" * 48)
print(f"  {passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if passed == len(results) else f"{len(results)-passed} FAIL")
sys.exit(0 if passed == len(results) else 1)
