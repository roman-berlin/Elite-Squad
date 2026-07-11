# Telegram notifications — send messages and receive updates from the Commander's ops chat.
"""Telegram notifications.

Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in the environment to enable.
If they're unset, every call is a safe no-op (the run is never affected).

Setup (one time):
  1. In Telegram, message @BotFather -> /newbot -> copy the token.
  2. Message your new bot once (say "hi"), then visit
     https://api.telegram.org/bot<TOKEN>/getUpdates and copy the chat id.
  3. export TELEGRAM_BOT_TOKEN=... ; export TELEGRAM_CHAT_ID=...
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import requests

from . import locking

# Phone reports (EU-62): a verbose report is SUMMARISED into a COMPLETE bulleted brief — every decision,
# action item, and blocker in tight bullets — never a truncated prefix that ends with "…(full report in
# the cockpit)". The phone is the primary read, so the message must stand on its own. Text already short
# enough to skim is sent verbatim; anything longer is distilled by the cheap model (`report_brief`), with a
# deterministic bulletizer (`bulletize`) as the never-fails fallback so a report is never lost.
_BRIEF_PASSTHROUGH = 360   # ≤ this many chars is already phone-skimmable — send as-is, no model call
_BULLET = "• "

_REPORT_BRIEF_SYSTEM = (
    "You turn one long officer report into a COMPLETE brief a busy engineer reads on his phone. "
    "Output ONLY a tight bullet list — one '• ' per line, with nothing before or after the bullets. "
    "Keep EVERY essential point: each decision, each action item (who / what / next), and each blocker, "
    "one per bullet. Summarise in your own words; do NOT copy sentences verbatim and do NOT cut the "
    "report off. NEVER write 'see the cockpit', 'full report in the cockpit', or any 'continued "
    "elsewhere' note — the bullets must stand on their own. Aim for 4–8 bullets, ≤ ~110 words total."
)


def configured() -> bool:
    return bool(os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"))


def bulletize(text: str, max_bullets: int = 9, max_chars: int = 1000) -> str:
    """Deterministically fold a report into a tight, phone-skimmable bullet list — the never-fails
    fallback behind ``report_brief`` when the cheap model is unavailable.

    Existing bullet / numbered lines are normalised to '• '; a single prose paragraph is split into one
    bullet per sentence. The list is capped to stay skimmable, but each bullet is a COMPLETE thought
    (never cut mid-word) and there is NO 'continue in the cockpit' footer — the report is summarised, not
    truncated-and-punted, so it stands on its own and is never lost."""
    text = (text or "").strip()
    if not text:
        return ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    _marker = r"^([-*•]|\d+[.)])\s+"
    if len(lines) > 1:                         # a multi-line list -> one bullet per line, markers normalised
        points = [re.sub(_marker, "", ln) for ln in lines]
    else:                                      # one prose blob -> strip any marker, then split into sentences
        one = re.sub(_marker, "", lines[0])
        points = re.split(r"(?<=[.!?])\s+", one)
    points = [p.strip() for p in points if p.strip()]
    out: list[str] = []
    used = 0
    for p in points:
        if len(out) >= max_bullets or (out and used + len(p) > max_chars):
            break
        out.append(_BULLET + p)
        used += len(p) + len(_BULLET)
    return "\n".join(out)


async def distill(cfg, *, system: str, user: str, tag: str, fallback) -> str:
    """Run the CHEAP model once to compress ``user`` under ``system`` — the shared engine behind
    ``report_brief`` and the loop's decision brief (EU-62 generalised both onto this). Reads/writes
    nothing, one turn, low effort. NEVER raises and never returns empty: on any model error (or an empty
    answer) it returns ``fallback()`` so a report or escalation is summarised, never lost."""
    try:
        from claude_agent_sdk import ClaudeAgentOptions

        from .agent import run_agent
        run = await run_agent(user, ClaudeAgentOptions(
            model=getattr(cfg, "smalltalk_model", None) or getattr(cfg, "discussion_model", None),
            system_prompt=system, permission_mode="bypassPermissions",
            allowed_tools=[], disallowed_tools=["Task", "Agent"],  # no sub-agent fan-out (AUTO-93 class fix)
            setting_sources=[], max_turns=1, effort="low"), tag=tag)
        return (run.final or run.text or "").strip() or fallback()
    except Exception:  # noqa: BLE001 - summarisation must never break a notification
        return fallback()


async def report_brief(cfg, raw: str) -> str:
    """Distil a verbose officer report into a COMPLETE, phone-skimmable BULLETED brief — every decision,
    action item, and blocker in tight bullets, NOT a truncated prefix with a 'full report in the cockpit'
    punt. Runs on the cheap small-talk model; a report already short enough to skim is returned verbatim
    (no model call), and any model error falls back to the deterministic ``bulletize`` so a report is
    never lost. Replaces the old ``clip`` truncate-and-punt on every report send."""
    raw = (raw or "").strip()
    if len(raw) <= _BRIEF_PASSTHROUGH:         # already a phone line or two — keep it natural, no model call
        return raw
    return await distill(cfg, system=_REPORT_BRIEF_SYSTEM,
                         user="Report to compress into the bullet brief:\n\n" + raw[:3500],
                         tag="report-brief", fallback=lambda: bulletize(raw))


def send(text: str, chat_id: str | int | None = None) -> bool:
    """Send a Telegram message. Returns True if sent, False if not configured or
    failed. Never raises — notifications must not break the pipeline.

    By default the message goes to the Commander's ops chat (TELEGRAM_CHAT_ID), so every
    existing caller — ops reports, escalations, council summaries — stays pinned to the ops
    chat. ``chat_id`` may target a specific chat explicitly (needs only the bot token, not
    TELEGRAM_CHAT_ID); since the EU-65 liaison channel's deletion (Phase-2 §2) no production
    path supplies one."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    target = str(chat_id) if chat_id is not None else os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and target):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": target, "text": text, "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def get_updates(offset: int | None = None, timeout: int = 0) -> list:
    """Poll Telegram for incoming messages (long-poll if timeout>0). Returns the
    raw update list, or [] if not configured / on error."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return []
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates",
                         params=params, timeout=timeout + 15)
        return r.json().get("result", []) or [] if r.status_code == 200 else []
    except requests.RequestException:
        return []


# EU-211: persist the one-shot alert-dedup flags to a locked JSON sidecar so an orchestrator
# restart doesn't re-fire an alert that already went out (they used to live in process memory
# only). The sidecar lives in the same state root as sonnet_fallback_state.json / model_backend.json
# — the parent of ``cfg.audit_path`` (see backend_pref._file / sync.state_dir), which resolves to
# ``state/`` on the live unit and to ``./`` on the server (audit_path='./audit.jsonl'). ``cfg`` is
# threaded in from every autopilot.py call site; the in-memory flags lazy-load from disk on the
# first cfg-bearing call (module import has no cfg to anchor the path).
_DEDUP_FILENAME = "sonnet_alert_dedup.json"


def _dedup_path(cfg=None) -> Path | None:
    """State-dir sidecar anchored to ``cfg.audit_path``'s parent (``state/`` live; a tmp dir in
    tests). Returns ``None`` when no audit path is available, so persistence is a safe no-op
    rather than writing to an invented relative location."""
    audit = getattr(cfg, "audit_path", None) if cfg is not None else None
    if not audit:
        return None
    return Path(audit).resolve().parent / _DEDUP_FILENAME


def _load_dedup_state(cfg=None) -> dict:
    """Best-effort load of the persisted dedup flags. A missing/corrupt/unanchored file is treated
    as "no flags set" — it never raises, matching approvals.py's tolerance for a bad sidecar."""
    p = _dedup_path(cfg)
    if p is None:
        return {}
    try:
        if not p.exists():
            return {}
        raw = p.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_dedup_state(mutate_fn, cfg=None) -> None:
    """Locked read-modify-write of the dedup sidecar (same locking.locked_rmw pattern as
    approvals.py/decisions.py). ``mutate_fn`` receives the freshly-read ON-DISK state, so a
    concurrent process's key is merged, never clobbered. Best-effort: a missing state root or an
    OS error is swallowed — persistence must never break a notification."""
    p = _dedup_path(cfg)
    if p is None:
        return

    def _mut(st):
        return mutate_fn(st if isinstance(st, dict) else {})
    try:
        locking.locked_rmw(p, _mut, default={}, corrupt_to_default=True)
    except OSError:
        pass


# In-memory one-shot flags, lazily seeded from the persisted sidecar on the first cfg-bearing call
# (module import can't resolve the state root — it has no cfg). ``_dedup_loaded`` guards the seed so
# only the first call reads disk; every alert path calls ``_ensure_dedup_loaded(cfg)`` before it
# checks a flag, so a restart's persisted state is always applied before the first send decision.
# EU-118 (plan-limit) / EU-122 (dual-provider low-watermark) flags, persisted since EU-211.
_dedup_loaded = False
_plan_limit_alert_sent = False
_dual_low_watermark_alerted: set[str] = set()


def _ensure_dedup_loaded(cfg=None) -> None:
    """Seed the in-memory flags from disk once, on the first call that carries a cfg. Idempotent
    and never raises; a no-op until a state root can be resolved."""
    global _dedup_loaded, _plan_limit_alert_sent, _dual_low_watermark_alerted
    if _dedup_loaded or _dedup_path(cfg) is None:
        return
    st = _load_dedup_state(cfg)
    _plan_limit_alert_sent = bool(st.get("plan_limit_alert_sent", False))
    _dual_low_watermark_alerted = set(st.get("dual_low_watermark_alerted") or [])
    _dedup_loaded = True


def plan_limit_alert(over_limits: list, reset_times: list[str], cfg=None) -> bool:
    """Send a SEVERE Telegram alert when a Claude plan limit is hit.

    Returns True if sent, False if not configured or failed. Only sends ONCE per
    session — persisted to the dedup sidecar (EU-211) so an orchestrator restart does
    NOT re-fire the same alert; use ``reset_plan_limit_alert`` to allow a new one.

    Args:
        over_limits: List of limit dicts that are hit (from ``usage.plan_limit_hit``)
        reset_times: List of human-readable reset times like "Mon Jun 30 14:30 UTC"
    """
    global _plan_limit_alert_sent

    _ensure_dedup_loaded(cfg)

    # Only alert once per session (flag is seeded from disk, so this also covers restarts)
    if _plan_limit_alert_sent:
        return False

    if not configured():
        return False

    limit_names = []
    for limit in over_limits:
        label = limit.get("label") or limit.get("key") or "unknown"
        limit_names.append(str(label))

    resets_text = ", ".join(reset_times) if reset_times else "unknown time"

    message = (
        f"⛔ <b>CLAUDE PLAN LIMIT REACHED</b> ⛔\n\n"
        f"Autopilot has paused because the following Claude plan limit(s) are exhausted:\n"
        f"• {', '.join(limit_names)}\n\n"
        f"<b>Resets at:</b> {resets_text}\n\n"
        f"New builds are held until the limit renews. "
        f"This prevents API errors and silent churn."
    )

    sent = send(message)
    if sent:
        _plan_limit_alert_sent = True

        def _mark(st: dict) -> dict:
            st["plan_limit_alert_sent"] = True
            return st
        _save_dedup_state(_mark, cfg)

    return sent


def reset_plan_limit_alert(cfg=None) -> None:
    """Clear the plan-limit alert sent flag — e.g. after the limit resets.

    Allows a new alert to be sent if the limit is hit again in a future billing period.
    Clears both the in-memory flag and the persisted dedup entry (EU-211).
    """
    global _plan_limit_alert_sent
    _plan_limit_alert_sent = False

    def _mark(st: dict) -> dict:
        st["plan_limit_alert_sent"] = False
        return st
    _save_dedup_state(_mark, cfg)


def dual_low_watermark_alert(provider: str, usage_data: dict, cfg=None) -> bool:
    """Send a Telegram alert when a provider crosses the budget bad threshold (low-watermark).

    Args:
        provider: "claude" or "glm"
        usage_data: Provider status dict from usage.dual_provider_budget_status()

    Returns True if sent, False if not configured or failed. Only sends ONCE per provider
    per session (resets on process restart) to avoid spamming.
    """
    global _dual_low_watermark_alerted

    _ensure_dedup_loaded(cfg)

    # Only alert once per provider per session
    if provider in _dual_low_watermark_alerted:
        return False

    if not configured():
        return False

    if provider == "claude":
        claude_limits = usage_data.get("limits", [])
        if not claude_limits:
            return False

        limit_names = []
        for limit in claude_limits:
            label = limit.get("label") or limit.get("key") or "unknown"
            util = float(limit.get("utilization", 0.0))
            limit_names.append(f"{label} ({util:.1%})")

        message = (
            f"⚠️ <b>CLAUDE PLAN LOW-WATERMARK</b> ⚠️\n\n"
            f"Claude plan limit(s) approaching exhaustion:\n"
            f"• {', '.join(limit_names)}\n\n"
            f"Autopilot will finish the current ticket then stop/switch. "
            f"This prevents mid-build cutoff and API errors."
        )
    elif provider == "glm":
        used = usage_data.get("used", 0)
        cap = usage_data.get("cap", 0)
        pct = usage_data.get("pct", 0.0)

        message = (
            f"⚠️ <b>GLM QUOTA LOW-WATERMARK</b> ⚠️\n\n"
            f"GLM (Z.ai) quota is {pct:.1%} used ({used:,}/{cap:,} tokens).\n\n"
            f"Autopilot will finish the current ticket then stop/switch. "
            f"This prevents mid-build cutoff."
        )
    else:
        return False

    sent = send(message)
    if sent:
        _dual_low_watermark_alerted.add(provider)

        def _mark(st: dict) -> dict:
            # Merge with the ON-DISK set (read fresh under the lock) so a concurrent process's
            # persisted provider is never dropped — only THIS provider's key is added.
            existing = set(st.get("dual_low_watermark_alerted") or [])
            existing.add(provider)
            st["dual_low_watermark_alerted"] = sorted(existing)
            return st
        _save_dedup_state(_mark, cfg)

    return sent


def reset_dual_low_watermark_alert(provider: str | None = None, cfg=None) -> None:
    """Clear the dual-provider low-watermark alert sent flag.

    Args:
        provider: If "claude" or "glm", clears only that provider's flag.
                  If None, clears all providers (e.g. at midnight or after quota reset).

    Clears both the in-memory flag(s) and the persisted dedup entry (EU-211).
    """
    global _dual_low_watermark_alerted
    if provider is None:
        _dual_low_watermark_alerted.clear()
    else:
        _dual_low_watermark_alerted.discard(provider)

    def _mark(st: dict) -> dict:
        # Merge with the ON-DISK set so clearing one provider (or all) never clobbers a concurrent
        # process's persisted key for a DIFFERENT provider.
        existing = set(st.get("dual_low_watermark_alerted") or [])
        if provider is None:
            existing.clear()
        else:
            existing.discard(provider)
        st["dual_low_watermark_alerted"] = sorted(existing)
        return st
    _save_dedup_state(_mark, cfg)


def incoming_texts(updates: list, cfg=None) -> list[tuple[int, str, str]]:
    """Extract ``(update_id, text, origin)`` for text messages we accept.

    Only the Commander's operations chat (TELEGRAM_CHAT_ID) is accepted; any other chat id is
    ignored. ``origin`` is always ``"ops"`` — the 3-tuple shape (and the ``cfg`` parameter) are
    kept for caller compatibility from the EU-65 liaison era; that outward channel was DELETED
    in Phase-2 §2 (2026-07-06), so no external chat can ever reach the command router."""
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    out: list[tuple[int, str, str]] = []
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        text = msg.get("text")
        if not text:
            continue
        cid = str((msg.get("chat") or {}).get("id", ""))
        if not chat or not cid or cid == str(chat):   # ops chat (unchanged acceptance)
            out.append((u.get("update_id"), text, "ops"))
        # else: any other chat -> ignored (the EU-65 external channel no longer exists)
    return out
