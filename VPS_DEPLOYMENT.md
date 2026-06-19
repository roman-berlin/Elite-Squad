# Deploying the Elite Unit to a 24/7 server

The goal: the unit's **brain-stem** lives on an always-on Linux box — Telegram, the cockpit, and the
officers' discussions (council, stand-up, corridor small-talk, patrol) run around the clock, so you
can talk to the General from your phone and the officers keep thinking while you sleep. Actual ticket
**implementation** stays on your Mac (it has the app repos + dev toolchain). MAIN is never touched on
either machine.

```
  ┌─────────────────────────── VPS (always on) ────────────────────────────┐
  │  general serve  →  cockpit (localhost:8787) + Telegram two-way listener │
  │  cron           →  10:00 council (+ stand-up), 3× corridor small-talk,  │
  │                    weekly patrol                                        │
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
sudo tee /etc/systemd/system/general.service >/dev/null <<EOF
[Unit]
Description=Elite Unit — cockpit + Telegram listener
After=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/General
ExecStart=$HOME/General/general serve --port 8787
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now general.service
systemctl status general.service        # should be 'active (running)'
```

## Step 5 — Schedule the discussions (cron)

```bash
crontab -e
```
Add (the council now runs the stand-up too; small-talk gets random jitter so it isn't clockwork):
```cron
# m  h  dom mon dow   command
 30  6   *   *   *    cd $HOME/General && GENERAL_HOST_ID=server GENERAL_SYNC_PULL_ONLY=1 ./general sync >> council/cron.log 2>&1  # freshen Mac state right before the muster
 30  6   *   *   *    cd $HOME/General && ./general council   >> council/cron.log 2>&1   # daily muster (council + stand-up), off-peak
  0 11,14,16 *  *  *  bash -c 'sleep $((RANDOM % 2100)); cd $HOME/General && ./general smalltalk >> council/cron.log 2>&1'
  0  9   *   *   1    cd $HOME/General && ./general patrol    >> council/cron.log 2>&1
*/15 *   *   *   *    cd $HOME/General && GENERAL_HOST_ID=server GENERAL_SYNC_PULL_ONLY=1 ./general sync >> council/cron.log 2>&1   # Mac<->server state sync (read-only)
```
`GENERAL_SYNC_PULL_ONLY=1` tells this box (which has read-only git access) to pull the Mac's audit but
not attempt a push — so sync exits clean instead of logging a 403 every run. Drop the flag only if you
give the box git **write** credentials (then it pushes its own councils back for the Mac to see).

**Frugality (so you never hit the Max limit):** the server's discussions run on **Sonnet**, and corridor
small-talk on **Haiku** — Opus is reserved for *implementation* on your Mac. A usage governor caps
discretionary chatter (`usage_cap_per_hour`, default 40 calls/hr): if an hour's already busy, a
small-talk is skipped rather than spent. The daily muster is council + stand-up merged into one, run
off-peak at 06:30. Net: necessary meetings + study only, on cheap models, self-throttled.

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
  - **Mac:** `export GENERAL_HOST_ID=mac`, then load the launchd pair (every 15 min):
    ```bash
    cp scripts/com.roman.general.sync.plist ~/Library/LaunchAgents/
    launchctl load ~/Library/LaunchAgents/com.roman.general.sync.plist
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
