# Security Policy

## Supported versions

The latest commit on `main` is the supported version. Older commits receive no fixes.

## Reporting a vulnerability

Please report vulnerabilities **privately** — do not open a public issue.

- Preferred: GitHub → **Security** tab → **Report a vulnerability** (private advisory).
- Fallback: email `romario.berlin@gmail.com` with subject `[SECURITY] Elite-Squad`.

You'll get an acknowledgement within 7 days. Please include reproduction steps and, if
possible, the affected file/line.

## Scope notes for operators

SQUAD runs LLM agents with **write access to the repositories you register**. When you
deploy it:

- Treat `config.yaml` and `.env` as secrets — both are gitignored by design; never commit them.
- Point it only at repositories you own or are authorized to modify.
- The Reviewer agent is read-only and `main` is code-protected, but the Builder can edit
  anything inside a registered repo's worktree. Review what lands on `dev` before promoting.
