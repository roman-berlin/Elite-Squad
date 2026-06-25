"""Approvals inbox — the officers PROPOSE, the Commander approves, the unit APPLIES.

The Engineering Coach (doctrine upgrades) and the Engineering Manager (personnel actions) are propose-only: they
write a report and stop. This collects those pending recommendations so the Commander can
**Approve** (the unit runs the matching `--apply`, then commits + pushes the doctrine to The-General)
or **Disapprove** (cleared, and the reason is logged to Unit Memory so it isn't re-proposed).

It's the home for overnight self-improvement too: the unit drills at night, you approve in the
morning. Only `officers/` is committed — never a blanket `git add -A` — so nothing unrelated ships.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from .config import Config

# kind -> (label, report filename). Each report maps to an officer `apply` coroutine.
KINDS = {
    "drill": ("Engineering Coach — doctrine upgrade", "drill-report.md"),
    "adjutant": ("Engineering Manager — personnel action", "adjutant-report.md"),
}


def _state_file(cfg: Config) -> Path:
    return Path(cfg.audit_path).with_name("approvals.json")


def _load(cfg: Config) -> dict:
    try:
        d = json.loads(_state_file(cfg).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(cfg: Config, st: dict) -> None:
    try:
        _state_file(cfg).write_text(json.dumps(st), encoding="utf-8")
    except OSError:
        pass


def _report_path(cfg: Config, kind: str) -> Path:
    return Path(cfg.audit_path).with_name(KINDS[kind][1])


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def pending(cfg: Config) -> list[dict]:
    """Officer recommendations awaiting a decision — a report that exists and whose current
    content hasn't already been approved/disapproved."""
    st = _load(cfg)
    out: list[dict] = []
    for kind, (label, _fname) in KINDS.items():
        p = _report_path(cfg, kind)
        try:
            body = p.read_text(encoding="utf-8") if p.exists() else ""
        except OSError:
            body = ""
        if not body.strip():
            continue
        h = _hash(body)
        if (st.get(kind) or {}).get("hash") == h:   # this exact report already actioned
            continue
        out.append({"kind": kind, "label": label, "body": body, "hash": h})
    return out


def _commit_push(msg: str) -> str:
    """Commit ONLY officers/ (what an apply touches) and push. Never a blanket add -A."""
    root = str(Path(__file__).resolve().parent.parent)
    try:
        subprocess.run(["git", "-C", root, "add", "officers/"], capture_output=True, timeout=30)
        c = subprocess.run(["git", "-C", root, "commit", "-m", msg],
                           capture_output=True, text=True, timeout=30)
        if "nothing to commit" in (c.stdout + c.stderr).lower():
            return "applied (no officer-file change to push)"
        p = subprocess.run(["git", "-C", root, "push"], capture_output=True, text=True, timeout=60)
        return "committed + pushed to origin" if p.returncode == 0 \
            else f"committed; push failed: {(p.stderr or '').strip()[:140]}"
    except Exception as exc:  # noqa: BLE001 - commit/push must not crash the approval
        return f"commit/push error: {exc}"


async def approve(cfg: Config, kind: str) -> str:
    """Apply the recommendation (drill/adjutant --apply), then commit + push the doctrine."""
    if kind not in KINDS:
        return f"unknown approval kind: {kind}"
    p = _report_path(cfg, kind)
    if not p.exists():
        return f"no pending {kind} report to apply"
    body = p.read_text(encoding="utf-8")
    h = _hash(body)
    if kind == "drill":
        from . import drillmaster
        summary = await drillmaster.apply(cfg)
    else:
        from . import adjutant
        summary = await adjutant.apply(cfg)
    pushed = _commit_push(f"{KINDS[kind][0]} — applied (Commander-approved)")
    st = _load(cfg)
    st[kind] = {"hash": h, "action": "approved", "ts": time.time()}
    _save(cfg, st)
    try:
        from . import notify
        notify.send(f"✅ Approved & applied — {KINDS[kind][0]}. {pushed}")
    except Exception:  # noqa: BLE001
        pass
    return (summary or "") + "\n\n" + pushed


def disapprove(cfg: Config, kind: str, reason: str = "") -> None:
    """Clear the recommendation and log WHY to Unit Memory so it isn't re-proposed."""
    if kind not in KINDS:
        return
    p = _report_path(cfg, kind)
    h = _hash(p.read_text(encoding="utf-8")) if p.exists() else ""
    st = _load(cfg)
    st[kind] = {"hash": h, "action": "disapproved", "reason": reason, "ts": time.time()}
    _save(cfg, st)
    try:
        from . import council
        council.add_commander_note(cfg, f"Disapproved {KINDS[kind][0]}: {reason or '(no reason given)'}")
    except Exception:  # noqa: BLE001
        pass
