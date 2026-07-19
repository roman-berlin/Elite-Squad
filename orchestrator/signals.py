"""Pure, no-LLM track-record aggregation — collect_signals()/format_signals().

Moved verbatim out of drillmaster.py (EU-323): both functions are load-bearing for
council/roster and had to survive the LLM drill/apply code — drillmaster.py was then
deleted in EU-327 (2026-07-17) without bricking any officer that needs the unit's
track record.
"""
from __future__ import annotations

import collections
import json

from .config import Config
from .dashboard import audit_lines, load_tasks
from .officers import display


def collect_signals(cfg: Config) -> dict:
    """Aggregate the track record from the audit log (pure, no LLM)."""
    tasks = load_tasks(cfg.audit_path)
    sig = {
        "tasks": len(tasks),
        "outcomes": collections.Counter(),
        "retried_tasks": 0,
        "max_effort_hits": 0,
        "issue_areas": collections.Counter(),
        "gate_fails": 0,
        "needs_human": 0,
        "avg_passes": 0.0,
    }
    total_passes = 0
    for t in tasks:
        sig["outcomes"][t["outcome"] or "running"] += 1
        total_passes += t["passes"]
        if t["passes"] > 1:
            sig["retried_tasks"] += 1
        for d in t["passes_list"]:
            if d.get("effort") in ("max", "xhigh"):
                sig["max_effort_hits"] += 1
            for iss in d.get("issues", []) or []:
                sig["issue_areas"][iss.get("area", "?")] += 1
    sig["avg_passes"] = round(total_passes / len(tasks), 2) if tasks else 0.0

    for line in audit_lines(cfg.audit_path):   # merged: local audit + synced shared/<host>.jsonl
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("event") == "gate" and ev.get("passed") is False:
            sig["gate_fails"] += 1
        elif ev.get("event") == "needs_human":
            sig["needs_human"] += 1
    return sig


def format_signals(sig: dict) -> str:
    outcomes = ", ".join(f"{k}: {v}" for k, v in sig["outcomes"].most_common()) or "none"
    areas = ", ".join(f"{k} ×{v}" for k, v in sig["issue_areas"].most_common(8)) or "none"
    return (
        f"Tasks run: {sig['tasks']}\n"
        f"Outcomes: {outcomes}\n"
        f"Avg passes/ticket: {sig['avg_passes']} | retried: {sig['retried_tasks']} | "
        f"hit max effort: {sig['max_effort_hits']}\n"
        f"Gate failures: {sig['gate_fails']} | decisions needed: {sig['needs_human']}\n"
        f"Recurring {display('inspector')} issue areas: {areas}"
    )
