# Deploying the Elite Unit to a 24/7 server

The goal: the unit's **brain-stem** lives on an always-on Linux box — Telegram, the cockpit, and
the weekly security patrol run around the clock, so you can talk to the General from your phone
and reach the officers on demand while you sleep. Councils and meetings convene when you ask (CLI
/ cockpit / Telegram) — Phase-2 §2 retired the auto-convened ceremonies. Actual ticket
**implementation** stays on your Mac (it has the app repos + dev toolchain). MAIN is never touched on
either machine.

```
  ┌─────────────────────────── VPS (always on) ────────────────────────────┐
  │  general serve  →  cockpit (localhost:8787) + Telegram two-way listener │
  │  cron           →  weekly patrol + Mac<->server state sync (15 min)     │
  │  councils/meetings → on demand (CLI / cockpit / Telegram)              │
  │  officers DISCUSS, file recommendations → Needs-you + Telegram          │
  └──────────────┬──────────────────────────────────────────────────────────┘
                 │  git (The-General repo) — shared state
  ┌──────────────┴──────────── Your Mac (when on) ──────────────────────────┐
  │  Autopilot / general task → builds tickets, merges to DEV               │
  └─────────────────────────────────────────────────────────────────────────┘
```

---

## Two things to know before you start (the honest constraints)

1. **Claude auth on the server.** The officers call Claude through the Claude Code CLI. On the VPS you
   log that CLI into **your Max account** once (browser device-flow) — *no API key*. Caveat: a personal
   Max login driving a 24/7 box is fine for the unit's light discussion cadence (a few councils +
   chats a day); if you ever push heavy automated volume through it you may hit Max rate limits, at
   which point the fallback is a dedicated API key. We're not using one now.

2. **Shared state.** Phase 1 (this runbook) makes the VPS the home of the *brain-stem* state — council
   history, Unit Memory, decisions, approvals. The VPS commits those to the The-General repo; your Mac
   pulls them. Your Mac's ticket-run history stays local. Giving the server's council full visibility
   into what the Mac shipped is a small phase-2 sync (push the Mac's `audit.jsonl` to the repo on a
   cron) — noted at the end. The unit works fine without it; the council just won't see the very
   latest local builds until you sync.

---

## Step 0 — Pick the box

| | **Oracle Cloud "Always Free"** (recommended for free) | **Hetzner CX22** (zero-hassle, paid) |
|---|---|---|
| Cost | **€0 forever** | ~€4.35 / $4.59 a month |
| Specs | 2 OCPU + 12 GB RAM, ARM (Ampere A1) | 2 vCPU + 4 GB RAM, x86 |
| Catch | Sign-up needs a card (no charge); pick a region with ARM capacity; Oracle can reclaim a *truly idle* free box — ours runs Telegram + cron so it never idles | None — it just runs |

ARM is fine here: Python, Node, the Claude CLI and the unit all run on ARM Linux. **If you want free and
don't mind a slightly fiddlier signup → Oracle.** If you want it to "just work" for the price of a
coffee → Hetzner. The rest of this runbook is identical on either (Ubuntu 24.04 LTS).

### Provision — register Oracle + create the box (click-by-click)

**(a) Make an SSH key on your Mac** — you'll paste the public half into Oracle:
```bash
ls ~/.ssh/id_ed25519.pub 2>/dev/null || ssh-keygen -t ed25519 -C "elite-unit" -f ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub        # copy this whole line (ssh-ed25519 AAAA… elite-unit)
```

**(b) Register for Oracle Cloud (Always Free):**
1. Open <https://www.oracle.com/cloud/free/> → **Start for free**.
2. Enter your email, verify it, fill in account details.
3. **Choose your Home Region carefully — it is permanent for Always Free.** Pick a large region with good
   ARM capacity: **US East (Ashburn)**, **US West (Phoenix)**, or **UK South (London)**.
4. Add a credit/debit card for identity verification. Always Free does **not** charge it — you'll see a
   small temporary authorization that's reversed.
5. Finish; you land in the **OCI Console**.

**(c) Create the Always-Free ARM VM:**
1. Console → ☰ menu → **Compute → Instances → Create instance**.
2. **Name:** `elite-unit`.
3. **Image:** *Edit → Change image* → **Canonical Ubuntu 24.04** (the `aarch64`/ARM build).
4. **Shape:** *Change shape → Ampere →* check **VM.Standard.A1.Flex**, set **2 OCPU** and **12 GB** RAM
   (the Always-Free max as of mid-2026).
5. **Networking:** keep defaults (it creates a VCN + public IP); ensure **Assign a public IPv4 address** is on.
6. **Add SSH keys:** choose **Paste public keys** and paste the `id_ed25519.pub` line from step (a).
7. Leave **Shielded Instance** and **Confidential Computing** off. Click **Create**.

**If you hit "Out of host capacity"** (common on the free ARM shape — not an error you did):
- Click **Create** again every few minutes — capacity frees up; or
- Try a different **Availability Domain** (AD-1 / AD-2 / AD-3) in the same region; or
- **Upgrade to Pay-As-You-Go** (Account → *Upgrade*): bigger free pool, and it **does not charge you**
  unless you exceed Always-Free limits (our box stays within them); or
- Run the community retry script [`hitrov/oci-arm-host-capacity`](https://github.com/hitrov/oci-arm-host-capacity)
  (a cron that grabs capacity the instant it appears).

**(d) Get on the box.** Note the instance's **public IP** (instance page). Oracle's default network usually
allows SSH; if `ssh` times out, add an ingress rule: VCN → **Security Lists** → default → **Add Ingress
Rule** → Source `0.0.0.0/0`, TCP, dest port **22** (the only port we ever open — the cockpit is reached by
SSH tunnel, never exposed). Then:
```bash
ssh ubuntu@<IP>
```

> **Hetzner instead?** Cloud Console → *Add Server* → location Nuremberg/Falkenstein → image **Ubuntu 24.04**
> → type **CX22** → add your SSH key → *Create*. Then `ssh root@<IP>`. No capacity dance. Everything below
> is identical.

---

## Step 1 — Base packages

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install git curl build-essential python3.12 python3.12-venv python3-pip tmux ufw
# Node (for the Claude CLI) + Bun (the unit's toolchain)
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt -y install nodejs
curl -fsSL https://bun.sh/install | bash && source ~/.bashrc
```

## Step 2 — Log Claude into your Max account (no API key)

```bash
sudo npm install -g @anthropic-ai/claude-code     # the Claude Code CLI
claude            # first run prompts login — it prints a URL
```
Open that URL on your laptop, sign in with your **Max** account, approve. The token is stored on the
box (`~/.claude`). Verify it actually answers:

```bash
claude -p "reply with the single word READY"
```
If you get `READY`, auth works and the officers can think. (This is the linchpin — if it fails, stop
here and sort it before anything else.)

## Step 3 — Install the unit

```bash
cd ~ && git clone https://github.com/roman-berlin/The-General.git General
cd General
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # or: pip install -e .
```

Create the secrets file `~/General/.env` (same keys you use locally — Telegram + Jira; **no**
`ANTHROPIC_API_KEY`):

```bash
cat > ~/General/.env <<'EOF'
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
JIRA_EMAIL=...
JIRA_API_TOKEN=...
# (whatever else your local .env carries, minus ANTHROPIC_API_KEY)
EOF
chmod 600 ~/General/.env
```

Smoke-test the wiring:
```bash
cd ~/General && ./general doctor      # config/keys/repos/tooling
./general ping                        # sends a test Telegram message — check your phone
```

## Step 4 — Run the brain-stem as a service

`general serve` runs the cockpit **and** the Telegram two-way listener in one process. Keep it alive
with systemd:

```bash
bash scripts/install-service.sh
```

The `install-service.sh` script writes the hardened systemd unit (with the EU-184 crash-loop
backstop: `StartLimitIntervalSec=300` and `StartLimitBurst=5`), calls `systemctl daemon-reload`
to apply the changes, and enables the service. The script is idempotent — you can re-run it any
time to re-apply the unit configuration.

Verify:
```bash
systemctl status general.service        # should be 'active (running)'
systemctl show general.service -p StartLimitIntervalUSec  # should show 5min (300s)
```

> **Applying EU-184 to a box already running the old unit:** re-run `bash scripts/install-service.sh`
> (it overwrites the unit with the start-limit lines). `scripts/self-update.sh` already smoke-tests
> each new `main` before restarting and reverts a broken deploy — so the two together mean a bad push
> can neither deploy (import break → reverted + Telegram alert) nor crash-loop the box (runtime
> break → systemd stops after 5 tries).

## Step 5 — Schedule the discussions (cron)

```bash
crontab -e
```
Prefer `bash scripts/install-server-cron.sh` — it installs exactly this set idempotently, so you
never hand-maintain the crontab (and re-running it on a box that ran an OLDER installer drops the
retired council/small-talk lines). If you do it by hand, Phase-2 §2 leaves only the weekly patrol
and the state sync — the 06:30 council muster and the corridor small-talk crons are gone (councils
and meetings run on demand now):
```cron
# m  h  dom mon dow   command
  0  9   *   *   1    cd $HOME/General && ./general patrol    >> council/cron.log 2>&1   # weekly security patrol
*/15 *   *   *   *    cd $HOME/General && GENERAL_HOST_ID=server GENERAL_SYNC_PULL_ONLY=1 ./general sync >> council/cron.log 2>&1   # Mac<->server state sync (read-only)
```
`GENERAL_SYNC_PULL_ONLY=1` tells this box (which has read-only git access) to pull the Mac's audit but
not attempt a push — so sync exits clean instead of logging a 403 every run. Drop the flag only if you
give the box git **write** credentials (then it pushes its own councils back for the Mac to see).

**Frugality (so you never hit the Max limit):** the server's discussions run on **Sonnet** —
Opus is reserved for *implementation* on your Mac. Ceremonies are on-demand (corridor small-talk
and the hourly usage governor were retired — Phase-2 §2 / 2026-07-19 stabilization); the
`daily_token_budget` ceiling auto-pauses the autopilot on runaway burn. Net: necessary meetings
only, on cheap models.

## Step 6 — Reach the cockpit (don't expose it)

The cockpit has no login, so never open port 8787 to the internet. View it through an SSH tunnel:
```bash
ssh -L 8787:localhost:8787 ubuntu@<IP>
# then open http://localhost:8787 in your browser
```

## Step 7 — Lock it down

```bash
sudo ufw allow OpenSSH
sudo ufw --force enable          # inbound = SSH only; the unit only makes OUTbound calls
```
Secrets live in `~/General/.env` (chmod 600). No API keys. MAIN is never a target on this box.

## Step 8 — The Mac side stays as-is

Your Mac keeps doing implementation (Autopilot / `general task`). To share state both ways via the
The-General repo:

- **VPS → Mac (already happening):** the council/scribe commit Unit Memory + officer changes; your Mac
  `git pull` picks them up.
- **Mac ↔ VPS state sync (now wired — `general sync`):** each machine publishes its `audit.jsonl` to
  `shared/<host>.jsonl` on a dedicated orphan **`unit-state`** branch (in a separate `.unit-state/`
  clone, gitignored — runtime state never lands in `main`/`dev`). The cockpit + councils then read the
  local audit **plus** every other host's file, so the server reflects what the Mac ships (you'll see a
  `↔ synced: mac, server` badge under the KPI row). Single-writer files = no merge conflicts; it's
  best-effort, so a git hiccup just no-ops.
  - **VPS:** the `*/15` cron above (`GENERAL_HOST_ID=server GENERAL_SYNC_PULL_ONLY=1`). Pull-only is all
    the server needs to see the Mac; it skips the push so there's no 403 noise. Give the box git write
    credentials and drop the flag if you want its councils pushed back for the Mac to see too.
  - **Mac:** `export GENERAL_HOST_ID=mac`, then add a cron line (every 15 min) — the same `general sync`
    mechanism the server installs via `scripts/install-server-cron.sh`, but with push left **on** so the
    Mac publishes its audit for the server to read:
    ```bash
    */15 * * * * cd "$HOME/General" && GENERAL_HOST_ID=mac ./general sync >> council/cron.log 2>&1
    ```
  - First run from the machine that has push auth (your Mac) — it bootstraps the `unit-state` branch.

## Step 9 — Auto-deploy: push to `main` → the server updates itself

So you never hand-deploy again, the box self-updates: a cron checks `origin/main` every 15 min and, only
when it actually moved, does `git reset --hard origin/main` + restarts the service — the same ritual you'd
run by hand. Your whole workflow becomes: develop on `dev`, promote to `main` on the **Mac**, done.

Install both the sync cron and the self-updater in one shot (no `crontab -e`):
```bash
cd ~/General
bash scripts/install-server-cron.sh
```
That's idempotent — re-run it any time; it replaces the unit's own cron lines and leaves your others alone.

The restart needs passwordless sudo for that one service. Oracle's `ubuntu` user has full NOPASSWD sudo by
default, so it just works. If `scripts/self-update.sh` ever logs a password prompt, scope it explicitly:
```bash
echo "ubuntu ALL=(root) NOPASSWD: /usr/bin/systemctl restart general.service" | sudo tee /etc/sudoers.d/general-restart
```

---

## Verify (the 5-minute checklist)
1. `systemctl status general.service` → active.
2. Message the bot on Telegram → the General replies (colleague-style, your language).
3. `./general council` by hand once → a briefing appears, and `council/` gets a transcript.
4. SSH-tunnel to the cockpit → Reports shows the council; Needs-you shows any recommendations.
5. Close your laptop → the unit keeps answering on Telegram. That's the whole point.

## Maintenance — re-authenticate Claude when the credential lapses (EU-430)

The box authenticates to the model with a **Claude Code OAuth login** (Step 2), not an API key. That
login **expires** — the access token lapses on a schedule and is refreshed from a *refresh token*.
If the refresh token ever comes back **empty** (the login was dropped / not completed cleanly) the
box can no longer self-heal: every model call returns **`401 Invalid authentication credentials`**,
and — left un-fixed — that raw error used to be broadcast to your phone *as the daily brief* and
written into `ROSTER.md` / `last-standup.md`. EU-430 closes that hole two ways: a ceremony now
**refuses to broadcast or persist a provider error** (it sends a short `⚠️ SQUAD: … cannot
authenticate …` alert instead), and a **pre-flight** warns you to Telegram ~48 h before the token
lapses (or immediately, if the refresh token is empty). Either way, the fix on the box is the same:
re-authenticate by hand.

### Symptom
- Telegram: a `⚠️ SQUAD auth pre-flight: …` warning, and/or `⚠️ SQUAD: the VPS cannot authenticate
  to the model — briefs are degraded until re-auth` in place of a daily brief.
- `./general doctor` → the **Claude auth** check reads `bad / EXPIRED`.
- `./general council` (or `daily`) → returns the auth-outage alert instead of a briefing.

### Re-auth (do this on the box over SSH)
```bash
claude /login          # interactive: it prints a URL — open it on your laptop, sign in with Max, approve
```
If `/login` does not refresh cleanly, re-run the first-run flow instead:
```bash
claude                 # prompts login the same way as the initial Step 2 install
```
Then restart the service so the long-running `general serve` picks up the fresh credential:
```bash
sudo systemctl restart general.service
```

### Verify the re-auth took (BOTH must hold)
1. **`expiresAt` is in the future** AND **`refreshToken` is non-empty** — inspect the credential
   file directly (the two fields the pre-flight checks):
   ```bash
   python3 - <<'PY'
   import json, datetime, pathlib
   p = pathlib.Path.home() / ".claude" / ".credentials.json"
   b = json.loads(p.read_text())
   o = b.get("claudeAiOauth", b)
   exp = o.get("expiresAt")
   exp = datetime.datetime.fromisoformat(str(exp).replace("Z", "+00:00")) if exp else None
   print("refreshToken:", "PRESENT" if str(o.get("refreshToken") or "").strip() else "EMPTY")
   print("expiresAt   :", exp, "(in the future)" if exp and exp > datetime.datetime.now(exp.tzinfo) else "(!! not in the future)")
   PY
   ```
   You want `refreshToken: PRESENT` and `expiresAt: … (in the future)`. An **empty refresh token**
   is the dead state from EU-430 — `/login` must populate it; if it stays empty, re-run `claude`
   (not just `/login`) and re-check.
2. **A real round-trip answers** — the strongest check (also what the liveness probe runs):
   ```bash
   claude -p "reply with the single word READY"     # expect: READY
   ```
3. Confirm health is green and the artefacts regenerate clean on the next ceremony:
   ```bash
   ./general doctor                                  # Claude auth → ok
   ./general daily                                   # a real briefing (not the ⚠️ alert)
   ```

> Why there is no fallback: the box runs Max-plan **subscription** auth on purpose (Step 2). There
> is no `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` on the VPS, so when the OAuth login dies
> nothing else can answer — re-auth is the only remedy. (If you ever want a non-expiring fallback,
> set `ANTHROPIC_API_KEY` in `~/General/.env`; the pre-flight then skips the file-expiry check,
> since an API key is not login-expiry-bound.)
