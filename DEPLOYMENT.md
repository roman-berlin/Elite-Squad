# The General — next steps & deployment

## The rule (why not Vercel)
The **engine** (the officers + build loop) must run on a *persistent machine* with your
repos, Claude Code, your Max auth, a shell, and minutes-per-batch of runtime. That is a
server — **not Vercel** (Vercel is stateless, seconds-long, no filesystem/repos). Vercel,
Supabase and Render come in *later* as a UI + shared DB + cloud runner. Telegram is the
remote arm; the cockpit is a local web app.

---

## Phase 0 — run it & put it on GitHub (do now)

**1. Install (one-time — Flask is new):**
```bash
cd ~/Documents/Claude/Projects/Superadmin/claude-pipeline
python3.12 -m venv .venv && source .venv/bin/activate      # if you don't have .venv yet
pip install -r requirements.txt
chmod +x general
```

**2. Auth & config:**
```bash
claude            # /login with your Max account (once)
cp .env.example .env       # fill TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, JIRA_* ; then: source .env
./general doctor           # should be all ✓ (auth, repo, DEV/MAIN branches)
```
`config.yaml` is already set (automatixy, DEV→MAIN, the QA column).

**3. Put the General on GitHub (private):**
`.gitignore` already excludes secrets (`.env`, `config.yaml`, `audit.jsonl`, `.venv`).
```bash
git init && git add -A && git commit -m "The General — autonomous dev unit"
gh repo create the-general --private --source=. --remote=origin --push
# (or make the repo on github.com, then: git remote add origin <url> && git push -u origin main)
```

**4. First real run:**
```bash
./general task automatixy --spec-file specs/AUTO-1.md --title "leads-kanban-scroll"   # dry-run
./general --live task automatixy --spec-file specs/AUTO-1.md --title "leads-kanban-scroll"
```
Then QA on DEV → pass: move the ticket to **Done**; fail: **comment** what's wrong + move to **To Do** (it re-picks it and reads your comment).

---

## Phase 1 — always-on + remote (low effort)
Keep the Mac on and run the cockpit (this also starts the Telegram listener):
```bash
./general serve      # cockpit at http://localhost:8787
```

**Restarting the cockpit safely (EU-405).** `./general deploy` is **THE way to restart** the cockpit
onto the current code. It refuses to restart while a build is in flight — it probes the cockpit's live
active-run count AND scans the audit tail for an open `ticket_start` with no terminal event — so an
operator can never kickstart a live build mid-edit (the AUTO-177 incident, 2026-07-19, killed a build
exactly this way). When the coast is clear it restarts via your supervisor (launchd on the Mac,
systemd on the VPS):
```bash
./general deploy              # restarts now if no build is running; refuses otherwise
./general deploy --force      # break-glass: restart even if a build appears in flight (may kill it)
```
A raw restart of your supervisor directly is **break-glass only** — it skips the in-flight guard and
kills whatever is running. On the VPS: `sudo systemctl restart general.service`. On the Mac, the
keepalive agent's label and the exact command live in `scripts/install-mac-cockpit-daemon.sh` (the
canonical source — `./general deploy` runs them for you, safely). Use `./general deploy` instead.

A self-repo land (the unit lands a change to its own code) restarts **automatically** once idle: the
land flags a pending restart and a serve-level watcher consumes it at the next idle boundary — no
manual step, and never mid-build. (If `self_update_auto_restart` is off, the land tells you to run
`./general deploy` by hand instead.)

From your phone (Telegram): `/standup`, `/status`, `/run automatixy <what> --live`, reply
`AUTO-1: <decision>`. Optional — reach the cockpit remotely:
```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:8787    # gives a private https URL to your cockpit
```

**Always-on watchdog (EU-403).** The cockpit keepalive restarts the process when it *exits*,
but a *wedged-but-alive* process (a deadlock, a hung HTTP call, a host that simply slept) looks
healthy to the keepalive and is invisible until you open the cockpit — the 2026-07-20 16:23→23:05
gap (host down 6.7h) produced zero alerts. Install the out-of-process watchdog: a separate
periodic launchd agent, pure shell + curl, that shares nothing with the serve process. It probes
the cockpit health endpoint and the audit log's freshness every ~4 min and pages you on Telegram
after two consecutive failures (and sends a recovery notice when it comes back).

```bash
bash scripts/install-mac-watchdog-daemon.sh            # install (runs a check immediately)
bash scripts/install-mac-watchdog-daemon.sh uninstall  # stop + remove
```

QA it once installed: with the cockpit running, pause the serve process
(`kill -STOP <serve pid>`) and confirm a "cockpit unreachable or wedged" alert lands within ~8
min, then resume it (`kill -CONT`) for a "cockpit recovered" notice. On the VPS, run the same
script from cron: `*/4 * * * * /path/to/repo/scripts/watchdog.sh >> ~/general-watchdog.log 2>&1`.
Tunables (env, all optional): `COCKPIT_URL`, `STALE_SEC`, `FAIL_THRESHOLD`, `RE_ALERT_EVERY`,
`HEALTH_TIMEOUT`.

**Config & secrets backup (EU-408).** `.env` (Jira/Telegram/GLM tokens) and `config.yaml` are
gitignored by design, so they have no version-control safety net — a disk failure, an errant
`rm -rf`, or a bad agent edit loses them and the unit is down until every credential is re-issued
and the ~150-line config is rebuilt. A nightly backup copies BOTH files to a **private location
outside the repo** (so a repo wipe does not take the backup with it), keeping a bounded changelog
of every edit. Install it:

```bash
bash scripts/install-mac-config-backup-daemon.sh            # Mac: nightly launchd agent (StartInterval 86400)
bash scripts/install-mac-config-backup-daemon.sh uninstall  # stop + remove
bash scripts/backup-config.sh                               # one-off: snapshot now
# VPS cron (3:17am daily):
#   17 3 * * *  /path/to/repo/scripts/backup-config.sh >> ~/general-config-backup.log 2>&1
```

- **Where:** `~/.general-config-backups/` by default (override: `GENERAL_CONFIG_BACKUP_DIR`). Each
  run writes `<name>.<YYYYMMDD-HHMMSS>` only when the file *changed* since the last backup, so the
  retained history is a real changelog of edits, not 14 identical nightly copies. `<name>.latest`
  always points at the newest copy.
- **Retention:** newest 14 per file (override: `BACKUP_KEEP`). **Perms:** dir `700`, every copy
  `600` — these files *are* the secrets.
- **Log:** `~/Library/Logs/General/config-backup.log` (one line per run; the backups themselves are
  never logged).

**Restore** (from the backup host — adjust the repo path to match your checkout):
1. See what's recoverable: `ls -t ~/.general-config-backups/` (the `.latest` links point at the
   newest of each).
2. Restore the files from the newest snapshots:
   ```bash
   REPO=/path/to/General
   cp ~/.general-config-backups/.env.latest         "$REPO/.env"         && chmod 600 "$REPO/.env"
   cp ~/.general-config-backups/config.yaml.latest  "$REPO/config.yaml"
   ```
   For a point-in-time restore, pick a specific timestamped copy (e.g. `.env.20260721-1405`)
   instead of `.latest`.
3. Reload creds into the running process: `source .env`, then restart the cockpit so it re-reads
   config.yaml — `./general deploy` (it refuses mid-build; see "Restarting the cockpit safely").
4. Verify: `./general doctor` should come back all ✓ (auth, repo, config).

---

## Later — the "software-company" platform (real builds, do when worth it)
1. **Supabase** — move `audit.jsonl` / tasks / decisions into Postgres (shared memory all surfaces read).
2. **Render background-worker** — run the engine headless with `CLAUDE_CODE_OAUTH_TOKEN`
   (`claude setup-token`, Max, no interactive login). Clones repos, pushes to GitHub. Now it
   runs **without your Mac**.
3. **Vercel** — host the cockpit UI as a web app over Supabase + the engine's API.

Each adds cost + complexity (always-on bill, a DB, an API layer, a frontend). Ship Phase 0
now; platformize once it earns its keep.

---

## Telegram API — how the remote arm works
- **Bot + token.** You create a bot with **@BotFather** (`/newbot`) and get a secret
  **token** (`123456:ABC…`). Every API call is `https://api.telegram.org/bot<TOKEN>/<method>`.
  The token *is* the bot — keep it in `.env`.
- **Chat id.** Each conversation has a `chat_id`. Message your bot once, then open
  `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `message.chat.id`. The General
  sends only to your `TELEGRAM_CHAT_ID` and ignores messages from anyone else.
- **Send (notify):** `POST /sendMessage {chat_id, text}` — how you get alerts.
- **Receive (commands/decisions):** two models —
  - **Long polling** (`getUpdates` with an `offset`): the bot *pulls* "any new messages?"
    every few seconds. Works from your Mac behind a firewall, no public URL. **This is what
    the General uses** (the `serve` listener). `offset = last update_id + 1` acknowledges
    messages so they aren't re-read (stored in `telegram_offset.txt`).
  - **Webhooks** (`setWebhook <url>`): Telegram *pushes* messages to a public HTTPS server.
    Needs a hosted endpoint — only relevant in the cloud phase.
- **In the code:** `notify.send` → sendMessage; `notify.get_updates`/`incoming_texts` →
  getUpdates filtered to your chat; `decisions.poll_loop` polls while `serve` runs and routes
  each message to a command (`/…`) or a decision reply.
- **Limits/safety:** the token is a secret (rotate via BotFather if leaked); ~30 msgs/sec
  rate limit (irrelevant here); chat-id filtering means only you can command it.
