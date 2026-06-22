"""Hard tool-call guardrail — the safety net underneath bypassPermissions.

Every write-capable officer runs ``permission_mode="bypassPermissions"``, so the model's own judgement is
the only thing between it and a destructive action. This installs the MISSING boundary: a **PreToolUse
hook** that BLOCKS, in code (not by instruction), regardless of what the agent decides —

  • writes to secrets / env / CI config (``.env*``, ``*.pem``/``*.key``, ``id_rsa``, ``.github/``,
    ``secrets.*``),
  • destructive shell (``rm -rf`` of a root/home path, ``git push --force``, push to a protected branch,
    ``git reset --hard`` of someone else's tree, ``DROP``/``TRUNCATE``, world-writable ``chmod 777``,
    fork bombs, ``curl … | sh``).

Defense-in-depth: the builder already works only in an isolated worktree and never touches MAIN — but a
bug, or a prompt-injection buried in a Jira ticket, must not be able to exfiltrate a secret or nuke a
tree just because the permission prompt is off. The guard fails CLOSED (deny) only on a clear match;
anything it doesn't recognise is allowed, so it never gets in the way of normal building.
"""
from __future__ import annotations

import re

# Paths an officer must never write to / edit.
_SECRET_PATH = re.compile(
    r"(^|/)\.env(\.[\w.-]+)?$"          # .env, .env.local, .env.production
    r"|(^|/)\.git/"                     # internal git plumbing
    r"|\.pem$|\.key$|(^|/)id_rsa"       # private keys
    r"|(^|/)\.github/"                  # CI / Actions config
    r"|(^|/)secrets?\.(ya?ml|json|toml|tfvars|env)$",
    re.IGNORECASE,
)

# Destructive / exfiltrating shell. Each entry: (pattern, human reason).
_DANGER_CMD: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\b[^|;&\n]*\s-{1,2}[a-z]*[rf][a-z]*\b[^|;&\n]*\s(/|~|\$HOME|\.\.?)(/|\s|$)"),
     "recursive delete of a root / home / repo path"),
    (re.compile(r"\bgit\s+push\b[^|;&\n]*(--force\b|--force-with-lease\b|\s-f\b)"), "force-push"),
    (re.compile(r"\bgit\s+push\b[^|;&\n]*\b(main|master)\b", re.IGNORECASE), "push to a protected branch"),
    (re.compile(r"\bgit\s+reset\s+--hard\b"), "git reset --hard (discards work)"),
    (re.compile(r"\bgit\s+clean\s+-[a-z]*f"), "git clean -f (deletes untracked files)"),
    (re.compile(r"\b(DROP|TRUNCATE)\s+(TABLE|DATABASE|SCHEMA)\b", re.IGNORECASE), "destructive SQL"),
    (re.compile(r"\bchmod\s+(-R\s+)?0?777\b"), "world-writable chmod 777"),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:?\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b"), "pipe-to-shell of a remote script"),
]

# Outbound network tools that can ship bytes off the box.
_NET_TOOL = re.compile(r"\b(curl|wget|nc|ncat|netcat)\b", re.IGNORECASE)

# An upload/data flag whose payload is a dotfile or secret-extension file — i.e. exfil of a credential,
# e.g. `curl --data @/path/.env`, `wget --post-file .netrc`, `curl --upload-file id_rsa`, `nc -F .env`.
# Requires a leading-dot filename OR a private-key/secret extension so normal POSTs (`-d name=value`,
# `-d @payload.json`) and ordinary GETs do not match.
_EXFIL_DOTFILE = re.compile(
    r"(?:--data(?:-binary|-raw|-ascii|-urlencode)?|--upload-file|--post-file|--form"
    r"|(?<![\w-])-[dFT](?![\w]))"          # short forms -d / -F / -T (not --data, not -dry)
    r"[=\s'\"]*@?\s*"                       # optional =, quote, @file marker, whitespace
    r"(?:~|\$HOME|\.{1,2})?/?"              # optional ~ / $HOME / . / .. prefix
    r"(?:[\w.-]+/)*"                        # optional directory components
    r"(?:\.[\w][\w.-]*|[^\s'\"|;&]*\.(?:pem|key))",  # a dotfile (leading dot) or a *.pem/*.key file
    re.IGNORECASE,
)

# Filename suffixes that mark a template/sample — they carry no real secret, so let them through.
_TEMPLATE_SUFFIXES = (".example", ".sample", ".template", ".dist", ".tmpl")

_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Tools that take a path and can EXPOSE a secret: writes (clobber/leak) plus Read (exfil source).
_PATH_TOOLS = _WRITE_TOOLS | {"Read"}


def _shell_secret_ref(cmd: str) -> str:
    """Return the first whitespace/quote/@-separated token in a shell command that names a protected
    secret path (``.env``, ``*.pem``/``*.key``, ``id_rsa``, ``secrets.*``, ``.git/``, ``.github/``),
    skipping templates. Empty string if none — this is how we deny `cat .env` / `curl --data @/x/.env`."""
    for tok in re.split(r"[\s'\"=|;&()<>`]+", cmd):
        ref = tok.lstrip("@")
        if not ref:
            continue
        if ref.lower().endswith(_TEMPLATE_SUFFIXES):
            continue
        if _SECRET_PATH.search(ref):
            return ref
    return ""


def is_dangerous(tool_name: str, tool_input: dict | None) -> tuple[bool, str]:
    """Pure denylist: does this tool call cross a hard line? Returns ``(blocked, reason)``.
    Unit-testable without the SDK — this is the heart of the guard."""
    ti = tool_input or {}
    if tool_name in _PATH_TOOLS:
        path = str(ti.get("file_path") or ti.get("path") or ti.get("notebook_path") or "")
        low = path.lower()
        # templates/samples carry no real secrets — let those through (e.g. .env.example, key.pem.sample)
        is_template = low.endswith(_TEMPLATE_SUFFIXES)
        if path and not is_template and _SECRET_PATH.search(path):
            verb = "writing to" if tool_name in _WRITE_TOOLS else "reading"
            return True, f"{verb} a protected/secret path ({path})"
    if tool_name == "Bash":
        cmd = str(ti.get("command") or "")
        for rx, why in _DANGER_CMD:
            if rx.search(cmd):
                return True, f"destructive shell — {why}"
        # (a) shell read/exfil of a real secret path (cat .env, curl --data @/x/.env, nc < secrets.yaml)
        hit = _shell_secret_ref(cmd)
        if hit:
            return True, f"shell access to a protected/secret path ({hit})"
        # (b) outbound network tool uploading a dotfile/secret payload (catches generic dotfiles too)
        if _NET_TOOL.search(cmd) and _EXFIL_DOTFILE.search(cmd):
            return True, "shell exfil — outbound network upload of a dotfile/secret"
    return False, ""


async def _pretooluse(input_data, tool_use_id, context):  # noqa: ANN001 - SDK callback signature
    """PreToolUse hook: DENY in code when the call is dangerous, otherwise stay out of the way."""
    try:
        if isinstance(input_data, dict):
            name, ti = input_data.get("tool_name", ""), input_data.get("tool_input", {})
        else:
            name, ti = getattr(input_data, "tool_name", ""), getattr(input_data, "tool_input", {})
        blocked, why = is_dangerous(name, ti)
    except Exception:  # noqa: BLE001 - a guard bug must not crash the run
        return {}
    if blocked:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (f"BLOCKED by the unit's hard guardrail — {why}. This is "
                                         "non-negotiable; do not retry it, find another way."),
        }}
    return {}


def hooks_config():
    """The ``hooks=`` dict to attach to every WRITE-CAPABLE officer's ClaudeAgentOptions. Returns None
    if the SDK is too old to support hooks (the guard then simply isn't installed — never an error)."""
    try:
        from claude_agent_sdk import HookMatcher
        # 'Read' MUST stay in this matcher: the hook only fires for tools it names, so without Read the
        # secret-READ blocking in is_dangerous() (EU-2 F1) would be dead code at runtime.
        return {"PreToolUse": [HookMatcher(matcher="Read|Bash|Write|Edit|MultiEdit|NotebookEdit",
                                           hooks=[_pretooluse])]}
    except Exception:  # noqa: BLE001
        return None
