"""Memory consolidation + learning from rejections.

Two deterministic, zero-cost passes that compound the unit's learning over time:

1. **Consolidate the Lessons log** — dedup near-identical bullets (same lesson, ignoring its date) and
   prune to the newest ``max_bullets`` so ``UNIT.live.md`` stays tight instead of growing forever.
2. **Learn from rejections** — scan the Reviewer's FAIL verdicts in the audit log; when the same *kind*
   of change is demanded across >= ``min_count`` distinct tickets, fold a one-line lesson into the log
   ("Reviewer repeatedly required X — do Y"). So the unit stops repeating the mistakes its own
   Reviewer keeps catching. (The old "drill proposal" surfacing left with the drillmaster, EU-327.)

No model call — it reads the audit log and the log file it already keeps, so it runs unattended and free.
The Technical Writer calls ``run()`` after it writes, and ``general consolidate`` / the cockpit expose it directly.
"""
from __future__ import annotations

import json
import re
from datetime import date

from . import dashboard as D

# Recurring rejection themes, most-specific first. Each: (key, label, keywords, recommended action).
_THEMES: list[tuple[str, str, tuple[str, ...], str]] = [
    ("tenant_isolation", "an explicit tenant filter",
     ("business_id", "tenant_id", "tenant", "rls", "cross-tenant", "cross tenant"),
     "Add an explicit business_id/tenant_id filter to EVERY query — RLS is defense-in-depth, not the boundary."),
    ("missing_tests", "tests with the change",
     ("test", "coverage", "regression", "vitest", "pytest", "untested"),
     "Write tests WITH the code — happy path + a regression for the bug — before requesting review."),
    ("typing", "complete typing",
     ("type annotation", "typing", "type hint", "bare dict", "no any", "`any`", " any ", "tsc", "mypy"),
     "Annotate fully; no `any` / bare dict; run the typecheck gate locally before review."),
    ("error_handling", "error & edge-case handling",
     ("error handling", "exception", "edge case", "null check", "none check", "bare except", "guard clause"),
     "Handle failure paths + edge cases explicitly; fail fast over fail-silent."),
    ("validation", "input validation",
     ("validation", "validate", "pydantic", "schema", "sanitize input"),
     "Validate inputs against a schema (Pydantic) before acting on them."),
    ("security", "the security fix",
     ("security", "secret", "injection", "authz", "auth check", "vulnerab"),
     "Close the security gap (secrets, authz, injection) before it reaches review."),
    ("naming_style", "the project's style/lint",
     ("naming", "rename", "lint", "formatting", "convention", "unused import", "dead code"),
     "Follow the project's naming + lint conventions; clear the linter before review."),
    ("docs", "docstrings / comments",
     ("docstring", "comment", "documentation", "undocumented"),
     "Add a concise docstring/comment explaining the why."),
]
_LABELS = {k: lbl for k, lbl, _, _ in _THEMES}
_ACTIONS = {k: act for k, _, _, act in _THEMES}


def _theme_of(text: str) -> str | None:
    blob = (text or "").lower()
    for key, _lbl, kws, _act in _THEMES:
        if any(k in blob for k in kws):
            return key
    return None


def rejection_patterns(cfg, *, min_count: int = 2) -> list[dict]:
    """Recurring Reviewer-rejection themes: a theme demanded across >= min_count DISTINCT tickets.
    Returns [{theme, label, count, tickets, action}] sorted by count desc."""
    by_theme: dict[str, set[str]] = {}
    for line in D.audit_lines(cfg.audit_path):
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if ev.get("event") != "review" or str(ev.get("verdict", "")).upper() != "FAIL":
            continue
        tid = ev.get("ticket_id") or "?"
        texts = list(ev.get("required_changes") or [])
        for iss in (ev.get("issues") or []):
            if isinstance(iss, dict):
                texts.append(f"{iss.get('area', '')} {iss.get('detail', '')}")
        seen_here: set[str] = set()
        for t in texts:
            theme = _theme_of(t)
            if theme and theme not in seen_here:        # count each theme once per ticket
                by_theme.setdefault(theme, set()).add(tid)
                seen_here.add(theme)
    out = []
    for theme, tickets in by_theme.items():
        if len(tickets) >= min_count:
            out.append({"theme": theme, "label": _LABELS[theme], "count": len(tickets),
                        "tickets": sorted(tickets), "action": _ACTIONS[theme]})
    out.sort(key=lambda p: -p["count"])
    return out


# --------------------------------------------------------------------------- #
# Lessons-log consolidation (dedup + prune)
_DATE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*[:\-—]\s*")


def _parse_bullets(raw: str) -> list[str]:
    """Bullet bodies (without the leading '- '), in file order (newest first)."""
    out = []
    for line in (raw or "").splitlines():
        s = line.strip()
        if s.startswith("- "):
            out.append(s[2:].strip())
    return out


def _norm(body: str) -> str:
    """Normalize a lesson for dedup: drop the leading date, lowercase, keep alphanumerics only."""
    body = _DATE.sub("", body or "")
    return re.sub(r"[^a-z0-9]+", " ", body.lower()).strip()


def _dedup(bullets: list[str]) -> tuple[list[str], int]:
    seen: set[str] = set()
    out: list[str] = []
    removed = 0
    for b in bullets:
        n = _norm(b)
        if not n or n in seen:
            removed += 1
            continue
        seen.add(n)
        out.append(b)
    return out, removed


def consolidate_log(raw: str, *, max_bullets: int = 24) -> tuple[str, dict]:
    """Dedup + prune the raw living-log text. Returns (new bullet block, report)."""
    bullets = _parse_bullets(raw)
    deduped, removed = _dedup(bullets)
    pruned = max(0, len(deduped) - max_bullets)
    kept = deduped[:max_bullets]
    block = "\n".join(f"- {b}" for b in kept)
    return block, {"before": len(bullets), "removed_dupes": removed, "pruned": pruned, "kept": len(kept)}


def run(cfg, *, write: bool = True, max_bullets: int = 24, min_count: int = 2) -> dict:
    """Fold recurring rejection lessons into the log, then dedup + prune it. Best-effort; never raises."""
    try:
        from . import memory
        try:
            raw = memory.LIVE_PATH.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        bullets = _parse_bullets(raw)
        patterns = rejection_patterns(cfg, min_count=min_count)
        today = date.today().isoformat()
        added: list[str] = []
        for p in patterns:
            phrase = f"Reviewer repeatedly required {p['label']}"
            if any(phrase.lower() in b.lower() for b in bullets):
                continue   # idempotent — already captured
            bullet = (f"{today}: {phrase} ({p['count']} tickets, e.g. {p['tickets'][0]}) — {p['action']}")
            bullets.insert(0, bullet)
            added.append(bullet)
        deduped, removed = _dedup(bullets)
        pruned = max(0, len(deduped) - max_bullets)
        kept = deduped[:max_bullets]
        if write and (added or removed or pruned):
            memory.update_log("\n".join(f"- {b}" for b in kept))
        return {"patterns": patterns, "added": added, "removed_dupes": removed,
                "pruned": pruned, "kept": len(kept), "written": bool(write and (added or removed or pruned))}
    except Exception as exc:  # noqa: BLE001 - memory hygiene must never break a run
        return {"patterns": [], "added": [], "removed_dupes": 0, "pruned": 0, "kept": 0,
                "written": False, "error": str(exc)}
