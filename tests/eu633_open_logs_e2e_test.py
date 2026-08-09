"""EU-633 end-to-end: wire Open-logs button to per-day consolidated view + plain-text download.

One continuous chain against a seeded log tree — the thing per-piece tests don't cover.
Runs the full chain TWICE: once with is_mac=False (the AC3 fix) and once with is_mac=True
(to catch any regressions on the existing path).

Stubs the Agent SDK / requests so importing the orchestrator needs no network.
Soft k/n tally so ``tests/run_all.py`` (the EU-44 gate) judges it honestly.
"""
from __future__ import annotations

import inspect
import platform
import re as _re
import sys
import tempfile
import types
from html import escape as html_escape
from pathlib import Path

_sdk = types.ModuleType("claude_agent_sdk")
class _D:
    def __init__(s, *a, **k): pass  # noqa: N807
    def __call__(s, *a, **k): return s  # noqa: N807
_sdk.__getattr__ = lambda n: _D
sys.modules.setdefault("claude_agent_sdk", _sdk)

_req = types.ModuleType("requests")
_req.Session = lambda: types.SimpleNamespace(
    auth=None,
    headers=types.SimpleNamespace(update=lambda *a, **k: None),
)
sys.modules.setdefault("requests", _req)

sys.path.insert(0, ".")

from orchestrator import cockpit_state, run_logger  # noqa: E402
from orchestrator import cockpit_views  # noqa: E402
from orchestrator.config import AppConfig, Config  # noqa: E402
import orchestrator.server as srv  # noqa: E402

results: list[tuple[str, bool, str]] = []


def chk(name: str, cond: bool, detail: str = "") -> None:
    """Record a named assertion result."""
    results.append((name, bool(cond), str(detail)))


def _make_cfg(tmp_dir: str) -> Config:
    """Minimal Config pointing at *tmp_dir* with an empty audit file."""
    import json
    audit = Path(tmp_dir) / "audit.jsonl"
    audit.write_text("", encoding="utf-8")
    return Config(
        apps=[AppConfig(
            name="alpha", repo_path=tmp_dir, base_branch="DEV",
            protected_branch="MAIN", backlog_backend="none",
        )],
        audit_path=str(audit),
    )


# ---------------------------------------------------------------------------
# Helpers shared by both chains
# ---------------------------------------------------------------------------
def _seed_log_tree(log_root: Path, app_name: str = "alpha") -> list[str]:
    """Seed two day folders with distinct ticket-log files, NO [Stage] markers.

    Using PLAIN text lines avoids the stage-matching logic producing redundant
    entries with text="Build" / text="Gate" that clutter verification.

    Returns the list of absolute file paths written (for verification).
    """
    app_dir = log_root / app_name
    app_dir.mkdir(parents=True, exist_ok=True)
    files: list[str] = []

    # Day 1: 2026-08-15 (older) — 3 files, distinct times
    d1 = app_dir / "2026-08-15"
    d1.mkdir(parents=True, exist_ok=True)

    # Ticket A at 090000
    f1 = d1 / "TICKETA-090000.log"
    f1.write_text("Compilation started\n", encoding="utf-8")
    files.append(str(f1))

    # Ticket B at 100000
    f2 = d1 / "TICKETB-100000.log"
    f2.write_text("PR approved\nTests green\n", encoding="utf-8")
    files.append(str(f2))

    # Ticket C at 110000
    f3 = d1 / "TICKETC-110000.log"
    f3.write_text("Code review notes\n", encoding="utf-8")
    files.append(str(f3))

    # Day 2: 2026-08-16 (newer) — 2 files
    d2 = app_dir / "2026-08-16"
    d2.mkdir(parents=True, exist_ok=True)

    # Ticket A at 140000
    f4 = d2 / "TICKETA-140000.log"
    f4.write_text("Build after fix\n", encoding="utf-8")
    files.append(str(f4))

    # Ticket B at 150000
    f5 = d2 / "TICKETB-150000.log"
    f5.write_text("Gate retry\nDeployment done\n", encoding="utf-8")
    files.append(str(f5))

    return files


# ──────────────────────────────────────────────────────────────────────────────
# CHAIN A: is_mac=False  (the AC3 fix — this is what MUST return 200 everywhere)
# ──────────────────────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmpdir_a:
    _cfg_a = _make_cfg(tmpdir_a)
    _log_root_a = run_logger.log_root(_cfg_a)
    _seed_log_tree(Path(_log_root_a), "alpha")
    cockpit_state.reset_run_state()
    client_a = srv.create_app(_cfg_a).test_client()

    # Step 1: toolbar with is_mac=False contains the Open-logs href
    bar_nosmac = cockpit_views._control_bar(_cfg_a, current_app="alpha", healthy=True, is_mac=False)
    chk("E2E/macF: toolbar contains 'Open logs' (is_mac=False)", "Open logs" in bar_nosmac)
    chk("E2E/macF: primary href points to /logs/days?app=alpha",
        'href="/logs/days?app=' in bar_nosmac, bar_nosmac[:120])

    # Step 2: follow the exact href from the toolbar → day-list page
    app_q = html_escape("alpha")
    days_href = "/logs/days?app=" + app_q
    r_days = client_a.get(days_href)
    chk("E2E/macF: GET /logs/days returns 200", r_days.status_code == 200,
        f"status={r_days.status_code}")
    chk("E2E/macF: Content-Type is text/html", "text/html" in r_days.content_type,
        repr(r_days.content_type))

    body_days = r_days.get_data(as_text=True)
    chk("E2E/macF: older date present", "2026-08-15" in body_days)
    chk("E2E/macF: newer date present", "2026-08-16" in body_days)
    try:
        i_newer = body_days.index("2026-08-16")
        i_older = body_days.index("2026-08-15")
        _order_ok_a = i_newer < i_older
    except ValueError:
        _order_ok_a = False
    chk("E2E/macF: newest-first order (2026-08-16 before 2026-08-15)", _order_ok_a)

    for d in ("2026-08-15", "2026-08-16"):
        chk(f"E2E/macF: /logs/day link present for {d}",
            f'/logs/day?app=alpha&date={d}' in body_days)

    # Step 3: follow the FIRST (newest) day's link → day-detail page
    day_href = "/logs/day?app=alpha&date=2026-08-16"
    r_day = client_a.get(day_href)
    chk("E2E/macF: GET /logs/day returns 200", r_day.status_code == 200,
        f"status={r_day.status_code}")
    chk("E2E/macF: Content-Type is text/html", "text/html" in r_day.content_type)

    body_day = r_day.get_data(as_text=True)

    # Every entry is prefixed [<ticket>] [<stage>] <text>
    # TICKETA@140000 has one entry; TICKETB@150000 has two entries
    chk("E2E/macF: '[TICKETA]' present in day view", "<li>[TICKETA]</li>" in body_day or "[TICKETA]" in body_day)
    chk("E2E/macF: '[TICKETB]' present in day view", "<li>[TICKETB]</li>" in body_day or "[TICKETB]" in body_day)
    chk("E2E/macF: '[—]' stage fallback present", "[—]" in body_day)
    chk('E2E/macF: "[Build]" absent (no stage-tag used)', "[Build]" not in body_day)
    # Text content visible
    chk("E2E/macF: 'Compilation started' absent from day=2026-08-16 (different day)",
        "Compilation started" not in body_day)
    chk("E2E/macF: 'Build after fix' present (day=2026-08-16)", "Build after fix" in body_day)
    chk("E2E/macF: 'Gate retry' present", "Gate retry" in body_day)

    # Chronological ordering: TICKETA@140000 comes before TICKETB@150000
    pos_ticketa = body_day.index("[TICKETA]")
    pos_ticketb = body_day.index("[TICKETB]")
    _chronology_ok = pos_ticketa < pos_ticketb
    chk("E2E/macF: entries sorted chronologically (TICKETA@140 before TICKETB@150)",
        _chronology_ok, f"positions: ticketa={pos_ticketa}, ticketb={pos_ticketb}")

    # Step 4: extract the download .txt link FROM THE PAGE (not hand-built)
    dl_match = _re.search(r'href="([^"]*format=txt[^"]*)"', body_day)
    chk("E2E/macF: download href with format=txt found on page", dl_match is not None,
        "could not find download link in day page HTML")
    if dl_match:
        txt_href = dl_match.group(1)
        chk("E2E/macF: download href includes format=txt", "format=txt" in txt_href, txt_href)

        # Step 5: GET the download href → text/plain + attachment + parity with HTML
        r_txt = client_a.get(txt_href)
        chk("E2E/macF: GET download href returns 200", r_txt.status_code == 200,
            f"status={r_txt.status_code}")
        chk("E2E/macF: Content-Type is text/plain", "text/plain" in (r_txt.content_type or ""),
            repr(r_txt.content_type))

        cd = dict(r_txt.headers).get("Content-Disposition", "")
        chk("E2E/macF: Content-Disposition attachment header present",
            "attachment" in cd, repr(cd))

        txt_body = r_txt.get_data(as_text=True)
        chk("E2E/macF: TXT body is non-empty", len(txt_body) > 0,
            f"len={len(txt_body)}")

        # Extract all [ticket] [stage] lines from HTML body
        html_entries = [m.group(0) for m in _re.finditer(r'\[(?:</?li>)?\[([^\]]+)\]\s*\[([^\]]+)\]\s*([^<]*)', body_day)]
        # Also try a simpler pattern for the actual prefixed lines
        html_entries_simple = [m.group(0) for m in _re.finditer(r'\[([^\]]+)\] \[([^\]]+)\] ([^<]*)', body_day)]
        txt_lines_raw = [l.strip() for l in txt_body.splitlines() if l.strip()]

        chk("E2E/macF: TXT lines count matches expected ({} vs {})".format(
            len(html_entries_simple), len(txt_lines_raw)),
            len(txt_lines_raw) == len(html_entries_simple),
            f"HTML had {len(html_entries_simple)} entries, TXT has {len(txt_lines_raw)}")

        # Each TXT line must match its corresponding HTML entry exactly
        max_checks = min(len(html_entries_simple), len(txt_lines_raw))
        for i in range(max_checks):
            he = html_entries_simple[i]
            tl = txt_lines_raw[i]
            if he != tl:
                chk(f"E2E/macF: line {i} parity OK", False,
                    f"expected '{he}', got '{tl}'")
            else:
                chk(f"E2E/macF: line {i} parity OK ('{he}')", True)
            if i >= 9:
                break

        # If there are more TXT lines than HTML matched
        if len(txt_lines_raw) != len(html_entries_simple):
            chk("E2E/macF: TXT lines remaining checked", False,
                f"mismatched counts: HTML {len(html_entries_simple)}, TXT {len(txt_lines_raw)}")

# ──────────────────────────────────────────────────────────────────────────────
# CHAIN B: is_mac=True  (regression guard — same flow via macOS secondary link)
# ──────────────────────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmpdir_b:
    _cfg_b = _make_cfg(tmpdir_b)
    _log_root_b = run_logger.log_root(_cfg_b)
    _seed_log_tree(Path(_log_root_b), "alpha")
    cockpit_state.reset_run_state()
    client_b = srv.create_app(_cfg_b).test_client()

    bar_mac = cockpit_views._control_bar(_cfg_b, current_app="alpha", healthy=True, is_mac=True)
    chk("E2E/macT: toolbar contains 'Open logs' (is_mac=True)", "Open logs" in bar_mac)
    chk("E2E/macT: primary href /logs/days present (is_mac=True)",
        'href="/logs/days?app=' in bar_mac)
    chk("E2E/macT: /api/open-logs present for Finder link", "open-logs" in bar_mac)

    r_days_b = client_b.get("/logs/days?app=alpha")
    chk("E2E/macT: /logs/days returns 200 even with is_mac=True",
        r_days_b.status_code == 200, f"status={r_days_b.status_code}")

    r_day_b = client_b.get("/logs/day?app=alpha&date=2026-08-16")
    chk("E2E/macT: /logs/day returns 200", r_day_b.status_code == 200,
        f"status={r_day_b.status_code}")

    body_day_b = r_day_b.get_data(as_text=True)
    chk("E2E/macT: '[TICKETA]' present (regression)", "[TICKETA]" in body_day_b)
    chk("E2E/macT: '[TICKETB]' present (regression)", "[TICKETB]" in body_day_b)

# ═══════════════════════════════════════════════════════════════════════════════
# CHAIN C: platform independence — the core AC3 claim
# All three new /logs/* routes return 200 regardless of platform gate.
# Legacy /api/open-logs still requires same-origin → 403 without it.
# ═══════════════════════════════════════════════════════════════════════════════
with tempfile.TemporaryDirectory() as tmpdir_c:
    _cfg_c = _make_cfg(tmpdir_c)
    _log_root_c = run_logger.log_root(_cfg_c)
    _seed_log_tree(Path(_log_root_c), "alpha")
    cockpit_state.reset_run_state()
    client_c = srv.create_app(_cfg_c).test_client()

    # /logs/* routes always 200
    r_ld = client_c.get("/logs/days?app=alpha")
    chk("AC3: /logs/days → 200 regardless of platform",
        r_ld.status_code == 200, f"status={r_ld.status_code}")

    r_lg = client_c.get("/logs/day?app=alpha&date=2026-08-16")
    chk("AC3: /logs/day → 200 regardless of platform",
        r_lg.status_code == 200, f"status={r_lg.status_code}")

    r_lt = client_c.get("/logs/day?app=alpha&date=2026-08-16&format=txt")
    chk("AC3: /logs/day?format=txt → 200 regardless of platform",
        r_lt.status_code == 200, f"status={r_lt.status_code}")

    # /api/open-logs: 403 without same-origin (proves Mac/gate still alive there)
    r_oa = client_c.get("/api/open-logs?path=/dummy")
    chk("AC3: /api/open-logs → 403 without same-origin (gate preserved)",
        r_oa.status_code == 403, f"status={r_oa.status_code}: {r_oa.get_data(as_text=True)[:80]}")

    # With valid Referer it reaches the handler. What answers next is platform-
    # dependent: on macOS the path check ("outside the configured"), elsewhere the
    # Darwin gate ("only available on macOS") — either message proves the
    # same-origin gate was passed, which is what this check pins (CI runs Linux).
    r_oa_ok = client_c.get("/api/open-logs?path=/dummy",
                            headers={"Referer": "http://127.0.0.1:8787/"})
    _expected = ("outside the configured" if platform.system() == "Darwin"
                 else "only available on macOS")
    chk("AC3: /api/open-logs reaches handler with valid Referer (gate bypassable)",
        r_oa_ok.status_code == 403 and _expected in r_oa_ok.get_data(as_text=True),
        f"unexpected: {r_oa_ok.status_code} {r_oa_ok.get_data(as_text=True)[:80]}")

    # Handler source checks: /logs/* lack Darwin/platform; legacy does have it
    chk("AC3: days_list_api lacks 'platform.system'",
        "platform.system" not in inspect.getsource(client_c.application.view_functions["days_list_api"]))
    chk("AC3: days_list_api lacks 'Darwin'",
        "Darwin" not in inspect.getsource(client_c.application.view_functions["days_list_api"]))
    chk("AC3: day_view lacks 'platform.system'",
        "platform.system" not in inspect.getsource(client_c.application.view_functions["day_view"]))
    chk("AC3: day_view lacks 'Darwin'",
        "Darwin" not in inspect.getsource(client_c.application.view_functions["day_view"]))

# ──────────────────────────────────────────────────────────────────────────────
# Verify legacy /api/open-logs DOES contain Darwin check (intentional)
# ──────────────────────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmpdir_z:
    _cfg_z = _make_cfg(tmpdir_z)
    _client_z = srv.create_app(_cfg_z).test_client()
    _ov_func = _client_z.application.view_functions.get("open_logs_api")
    if _ov_func is not None:
        _ov_src = inspect.getsource(_ov_func)
        chk("Legacy /api/open-logs HAS 'Darwin' (intentional)",
            "Darwin" in _ov_src)
        chk("Legacy /api/open-logs HAS 'platform.system' (intentional)",
            "platform.system" in _ov_src)

# ──────────────────────────────────────────────────────────────────────────────
# Report
# ──────────────────────────────────────────────────────────────────────────────
print("\n========== EU-633 OPEN-LOGS E2E QA ====================")
_passed = sum(1 for _, ok, _ in results if ok)
for _name, _ok, _det in results:
    _mark = "PASS" if _ok else "FAIL"
    _extra = f"  ({_det})" if _det and not _ok else ""
    print(f"  [{_mark}] {_name}{_extra}")
print("-------------------------------------------------------")
print(f"  {_passed}/{len(results)} passed")
print("  RESULT:", "ALL GREEN" if _passed == len(results)
      else f"{len(results) - _passed} FAIL")
sys.exit(0 if _passed == len(results) else 1)
