"""Doctrine backups — snapshot_doctrine().

Moved verbatim out of drillmaster.py (EU-331, following the EU-323 pattern for
collect_signals/format_signals): snapshot_doctrine() is the reversibility guard every
applied doctrine change (officer files / squad agents) relies on. It must survive
independently of the LLM drill/apply code that remains in drillmaster.py, so
drillmaster.py can eventually be deleted (EU-327) without losing the ability to back up
officers/ + the squad agents before an applied change.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .config import Config


def snapshot_doctrine(cfg: Config) -> Path:
    """Back up the officer files + the squad agents before any applied change."""
    root = Path(__file__).resolve().parent.parent
    dest = Path(cfg.audit_path).resolve().parent / "backups" / time.strftime("doctrine-%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    off = root / "officers"
    if off.exists():
        shutil.copytree(off, dest / "officers", dirs_exist_ok=True)
    squads = Path.home() / ".claude" / "agents"
    if squads.exists():
        shutil.copytree(squads, dest / "claude-agents", dirs_exist_ok=True)
    return dest
