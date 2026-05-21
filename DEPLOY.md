# UBI Bot — Operator Deployment Guide

A practical, end-to-end guide for standing up your own node of the
[UBI World](https://ubi.world) Telegram bot. Written for operators who have a
server and a Telegram account, and want to be up and running in under an hour.

If you only need the high-level idea, read **What this is** and **Federation
status** below; everything else is a runbook.

---

## What this is

This repository is the canonical source for the UBI World Telegram bot — a
Python 3.10+ async bot that implements the
[ubi.world](https://ubi.world) time-based Universal Basic Income protocol.
Every user gets a Daily Wallet of 24 hours per day, can send time to other
users, accrues received time in a Time Vault, and feeds unspent Wallet time
to community Circles at midnight UTC.

When you deploy this code, **you are standing up an independent node**. You
own your bot's Telegram identity (its `@handle`), your own database, and your
own users. Other operators (including the original one) run separate nodes
with separate state. See **Federation status** at the bottom of this guide
for honest detail on cross-node behaviour.

---

## What you need before you start

| Requirement | Why |
|---|---|
| A Telegram account | To create the bot via BotFather and to test it. |
| A server with SSH access | Either your own VPS (root + systemd) or shared cPanel/SPanel hosting (no root). Both flows are covered below. |
| Python 3.10 or newer | aiogram 3.x requires it. If your server only has Python 3.6/3.8, follow Path B. |
| ~200 MB of disk | Repo, venv, and headroom for the SQLite DB. |
| Optional: a (sub)domain | Not required for a polling bot, but useful for branding. See **About the subdomain**. |

You do **not** need a public IP, an open inbound port, a web server, or TLS.
The bot uses long-polling: it makes outbound HTTPS calls to
`api.telegram.org` and never accepts inbound connections.

---

## Step 1 — Get your Telegram bot token

1. Open Telegram and message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Pick a display name (e.g. *Time UBI — Tie Node*).
4. Pick a username ending in `bot` (e.g. `tie_ubi_bot`).
5. BotFather replies with a token like:

   ```
   1234567890:AAEbOdpEFbENy_2pTkzXaMQoI259U0O34rc
   ```

   That's your `BOT_TOKEN`. Treat it like a password. Anyone with it can
   impersonate your bot.

Optional but recommended:

```
/setdescription   — short blurb shown on the bot's profile
/setabouttext     — longer text shown in the "About" sheet
/setuserpic       — avatar
/setcommands      — pre-fills the in-app command menu, copy from this list:

start - Register and get your handle
balance - Show Daily Wallet + Time Vault
send - Send time to a user: /send @user 2h 30m
history - Recent transactions
handle - Show your handle
help - Command reference
```

---

## Step 2 — Get your Telegram user ID

The bot uses your Telegram user ID for hidden admin commands (e.g. `/reboot`).

1. Message [@userinfobot](https://t.me/userinfobot) on Telegram.
2. It replies with your numeric ID (e.g. `1522562113`).
3. That's your `ADMIN_TELEGRAM_ID`.

---

## Path A — VPS with root + systemd

Use this path if you have your own server (DigitalOcean, Hetzner, Linode,
AWS EC2, etc.) and a user account with `sudo`. The whole node lives under
your home directory; only the systemd unit needs root.

The example below assumes Ubuntu/Debian. For RHEL/Rocky/Alma, swap
`apt-get` for `dnf` and `python3.11` package names accordingly.

### A.1 Install prerequisites

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git
python3 --version   # must be 3.10+
```

If the system Python is older than 3.10, install a newer one
(`apt-get install python3.11`) and substitute `python3.11` everywhere
below.

### A.2 Clone the repo

```bash
cd ~
git clone https://github.com/UBIworld/time.git ubi-bot
cd ubi-bot
```

### A.3 Create the virtualenv and install dependencies

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
.venv/bin/python -c "import aiogram, aiosqlite, apscheduler, dotenv, pytz; print('aiogram', aiogram.__version__)"
```

The last line is a sanity check — it should print the aiogram version.

### A.4 Configure your `.env`

```bash
cp .env.example .env
chmod 600 .env
```

Then edit `.env` and fill in the two values from Steps 1 and 2:

```ini
BOT_TOKEN=<your-bot-token>
ADMIN_TELEGRAM_ID=<your-telegram-id>
```

**Never commit this file.** `.gitignore` already excludes it, but double-check
with `git status` before any commit.

### A.5 Smoke test on the foreground

```bash
.venv/bin/python bot.py
```

You should see log lines like:

```
Database initialized
Scheduler started. Daily reset scheduled for midnight UTC.
Bot starting in polling mode...
Run polling for bot @yourbotname id=<digits> - '<display name>'
```

In Telegram, send `/start` to your bot. It should reply with a registration
message and assign you a handle. Send `/balance` — you should see
`Daily Wallet: 24h 0m 0s`. If both work, the bot is good. `Ctrl-C` to stop.

### A.6 Install the systemd unit

The repo ships a template at `ubi-bot.service`. Copy it and replace the
placeholders for your environment:

```bash
sudo cp ubi-bot.service /etc/systemd/system/ubi-bot.service
sudo nano /etc/systemd/system/ubi-bot.service
```

Edit so the four lines under `[Service]` match your install. Example for a
user named `ubi` with the repo at `/home/ubi/ubi-bot`:

```ini
[Unit]
Description=UBI Bot (@yourbotname) — Time-based Universal Basic Income
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubi
WorkingDirectory=/home/ubi/ubi-bot
EnvironmentFile=/home/ubi/ubi-bot/.env
ExecStart=/home/ubi/ubi-bot/.venv/bin/python /home/ubi/ubi-bot/bot.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ubi-bot
sudo systemctl status ubi-bot
```

### A.7 Operating the bot (Path A)

| Action | Command |
|---|---|
| Status | `sudo systemctl status ubi-bot` |
| Live logs | `sudo journalctl -u ubi-bot -f` |
| Last 200 log lines | `sudo journalctl -u ubi-bot -n 200 --no-pager` |
| Restart | `sudo systemctl restart ubi-bot` |
| Stop | `sudo systemctl stop ubi-bot` |
| Disable (won't auto-start) | `sudo systemctl disable ubi-bot` |

systemd handles restart-on-crash (`Restart=on-failure`) and restart-on-reboot
(via the `enable` step). No cron needed.

### A.8 Updating to a newer version

```bash
cd ~/ubi-bot
sudo systemctl stop ubi-bot
cp ubi_bot.db ubi_bot.db.bak-$(date -u +%Y%m%dT%H%M%SZ)   # always back up first
git pull
.venv/bin/pip install -r requirements.txt

# Run any one-shot migrations from migrations/ — dry-run first, then for real.
# Example (only needed once, on upgrades that drop the `::` handle wrappers):
.venv/bin/python migrations/001_drop_handle_delimiters.py --dry-run
.venv/bin/python migrations/001_drop_handle_delimiters.py

sudo systemctl start ubi-bot
sudo journalctl -u ubi-bot -n 50 --no-pager
```

Migrations under `migrations/` are idempotent — safe to re-run, no-op if
already applied. The bot's own startup also applies internal schema
additions automatically. The schema is forward-only; the `.bak` snapshot
above is your fast rollback option.

---

## Path B — Shared cPanel/SPanel hosting (Scalahosting and similar)

Use this path if your host gives you SSH but no `sudo`, no `systemctl --user`
(no user DBus session), and probably an ancient system Python. This is the
flow proven on a real Scalahosting VPS in May 2026. The trick is two things:

1. **pm2 instead of systemd** for process supervision. pm2 runs entirely in
   userspace, restarts on crash, and is often pre-installed by the hoster.
2. **A portable Python** from
   [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
   dropped into your home directory, so you don't need root to get Python 3.11.

### B.1 SSH in and check what you have

```bash
ssh -p <port> <user>@<your-host>
python3 --version        # likely 3.6.8 or similar — too old
which pm2                # is pm2 already there? probably yes on Scalahosting
node --version           # pm2 needs Node ≥ 14
crontab -l               # for the @reboot trick later
```

If `pm2` is missing and you can't install Node system-wide, install a userland
Node via `nvm`:

```bash
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
source ~/.bashrc
nvm install --lts
npm install -g pm2
```

### B.2 Install a portable Python 3.11

Pick a recent tarball from
[python-build-standalone releases](https://github.com/astral-sh/python-build-standalone/releases).
You want the `x86_64-unknown-linux-gnu-install_only.tar.gz` flavour for a
typical 64-bit Linux VPS.

```bash
mkdir -p ~/apps && cd ~/apps
# Replace the URL with whatever the latest install_only tarball is.
curl -L -o python.tar.gz \
  https://github.com/astral-sh/python-build-standalone/releases/download/20241008/cpython-3.11.10+20241008-x86_64-unknown-linux-gnu-install_only.tar.gz
tar -xzf python.tar.gz       # extracts as ./python/
rm python.tar.gz
~/apps/python/bin/python3 --version
```

You should see something like `Python 3.11.10`. This Python is fully
self-contained — it does not touch any system files.

### B.3 Clone the repo

```bash
cd ~/apps
git clone https://github.com/UBIworld/time.git ubi-bot
cd ubi-bot
```

### B.4 Create the venv from the portable Python and install deps

```bash
~/apps/python/bin/python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
.venv/bin/python -c "import aiogram, aiosqlite, apscheduler, dotenv, pytz; print('aiogram', aiogram.__version__)"
```

### B.5 Configure your `.env`

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

Fill in `BOT_TOKEN` and `ADMIN_TELEGRAM_ID` exactly as in Path A.

### B.6 Write a pm2 ecosystem config

Create `~/apps/ubi-bot/ecosystem.config.cjs` with the following content,
adjusting paths to match your username:

```javascript
// pm2 ecosystem config for ubi-bot.
// pm2 supervises the python process directly via the venv's interpreter;
// no shell wrapper required.
module.exports = {
  apps: [
    {
      name: "ubi-bot",
      script: "bot.py",
      cwd: "/home/<your-user>/apps/ubi-bot",
      interpreter: "/home/<your-user>/apps/ubi-bot/.venv/bin/python",
      autorestart: true,
      max_restarts: 10,
      min_uptime: "30s",
      restart_delay: 5000,
      exp_backoff_restart_delay: 200,
      out_file: "/home/<your-user>/.pm2/logs/ubi-bot-out.log",
      error_file: "/home/<your-user>/.pm2/logs/ubi-bot-error.log",
      merge_logs: true,
      time: true,
    },
  ],
};
```

### B.7 Smoke test

```bash
cd ~/apps/ubi-bot
.venv/bin/python bot.py
```

As in Path A, send `/start` to your bot in Telegram. You should see a
registration message and a fresh handle. `Ctrl-C` to stop.

### B.8 Hand off to pm2

```bash
cd ~/apps/ubi-bot
pm2 start ecosystem.config.cjs
pm2 save                       # writes ~/.pm2/dump.pm2 — what `pm2 resurrect` reads on boot
pm2 list
pm2 logs ubi-bot --lines 50 --nostream
```

### B.9 Make pm2 survive reboot (no root)

`pm2 startup` wants `sudo` to install a real systemd unit. On shared
hosting you don't have it. The portable workaround is a `@reboot` cron
entry that runs `pm2 resurrect` automatically:

```bash
( crontab -l 2>/dev/null; echo "@reboot $(which pm2) resurrect >> ~/.pm2/cron-resurrect.log 2>&1 # ubi-bot pm2 resurrect" ) | crontab -
crontab -l
```

If your `crontab -l` already contains the marker `# ubi-bot pm2 resurrect`,
you've added it before — don't add it twice.

Verify the flow without actually rebooting:

```bash
pm2 kill          # stops the pm2 daemon and all apps
pm2 resurrect     # restores from ~/.pm2/dump.pm2
pm2 list          # ubi-bot should be back, status: online
```

If this works, the `@reboot` line will do the same thing automatically when
the VPS reboots.

### B.10 Operating the bot (Path B)

| Action | Command |
|---|---|
| Status / overview | `pm2 list` |
| Detailed info | `pm2 info ubi-bot` |
| Live log stream | `pm2 logs ubi-bot` |
| Last N log lines | `pm2 logs ubi-bot --lines 200 --nostream` |
| Stop the bot | `pm2 stop ubi-bot` |
| Start it back | `pm2 start ubi-bot` |
| Restart in place | `pm2 restart ubi-bot` |
| Remove from pm2 | `pm2 delete ubi-bot && pm2 save` |
| Persist current state | `pm2 save` |

After any change to the pm2 process list, run `pm2 save` so the new state is
captured in `dump.pm2`. That dump is what survives a reboot.

### B.11 Updating to a newer version (Path B)

```bash
cd ~/apps/ubi-bot
pm2 stop ubi-bot
cp ubi_bot.db ubi_bot.db.bak-$(date -u +%Y%m%dT%H%M%SZ)   # always back up first
git pull
.venv/bin/pip install -r requirements.txt

# Run any one-shot migrations from migrations/ — dry-run first, then for real.
.venv/bin/python migrations/001_drop_handle_delimiters.py --dry-run
.venv/bin/python migrations/001_drop_handle_delimiters.py

pm2 start ubi-bot
pm2 logs ubi-bot --lines 50 --nostream
```

---

## About the `.env` file

`.env` is a plain key=value file the bot reads at startup via `python-dotenv`.

```ini
# UBI Bot — environment variables. Copy from .env.example, fill in real
# values, then `chmod 600 .env`. NEVER commit this file.

BOT_TOKEN=<your-bot-token>            # from @BotFather
ADMIN_TELEGRAM_ID=<your-telegram-id>  # from @userinfobot
```

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Authenticates the bot to Telegram. Anyone with this token can impersonate your bot. Treat as a password. |
| `ADMIN_TELEGRAM_ID` | Your numeric Telegram ID. Used to gate hidden admin commands (e.g. `/reboot`). The bot does not enforce admin-only on user commands — this is for operational tooling only. |

The repo's `.gitignore` excludes `.env` and `*.env`. Confirm before any
commit:

```bash
git status         # .env must NOT appear under "Changes to be committed"
git check-ignore .env   # must print ".gitignore:<line>:.env  .env"
```

If you ever rotate `BOT_TOKEN`:

- **Path A:** edit `.env`, then `sudo systemctl restart ubi-bot`.
- **Path B:** edit `.env`, then `pm2 restart ubi-bot`.

---

## About the subdomain (e.g. `tie.ubi.asia`)

The bot is **polling-only**. It opens an outbound HTTPS connection to
`api.telegram.org` and pulls updates. It does **not** listen on any inbound
port and does **not** need a DNS record to function.

Subdomains are useful for two reasons:

1. **Branding / identification.** A node operator at `tie.ubi.asia` has a
   memorable URL even if the bot's Telegram username is something else.
2. **Future webhooks or status pages.** If you later want to switch from
   polling to webhooks, or publish a `/status` page, you'll need a domain
   with TLS pointing at your server. Polling-mode bots don't need either.

DNS handoff to a new operator (e.g. `tie.ubi.asia` migrating from one VPS
to another):

1. The new operator stands up their node (Path A or B above).
2. They share their public IP with whoever controls `ubi.asia` DNS.
3. DNS owner updates the A record for the subdomain.
4. Propagation: 5 minutes to a few hours depending on the previous TTL.

Nothing in the bot needs to be reconfigured for a DNS change — it's purely
external addressing.

---

## Federation status

**As of May 2026, federation between nodes does not exist.** Each node
operator runs an independent instance with its own database and its own set
of users. A user registered on `@timeubibot` is unknown to `@tie_ubi_bot`,
and vice versa. Time cannot be sent across nodes.

Each node is internally complete — Daily Wallet, Time Vault, Universal
Circles, daily reset — but those state machines do not synchronise with
other nodes' state.

### Handle format (forward-compatible with federation)

Handles look like `slot:slot:slot` — three colon-separated slots, with no
wrapping delimiters. Example: `house:cat:888`. The parser also accepts an
optional `@node.domain` suffix (e.g. `house:cat:888@cat.ubi.asia`) for the
day federation lands. Today the suffix is ignored on local-only nodes; it
exists in the parser so user-visible handles don't have to change when
cross-node transfers ship.

If you're upgrading an existing node from the older `::slot:slot:slot::`
form, run `migrations/001_drop_handle_delimiters.py` once during the
deploy cycle. It's idempotent (safe to re-run) and transactional (rolls
back on any error). See **Step A.8 / B.11** for where it slots into your
update workflow.

### Roadmap

Cross-node time transfer (and a shared notion of identity / handle
uniqueness across nodes) is on the roadmap, not in the code. When it lands,
this guide will be updated with the operational changes node operators need
to make.

If you want to discuss the federation design, the relevant conversation is
happening at [ubi.world](https://ubi.world) and in the project's
communication channels — not here.

---

## Troubleshooting

### `KeyError: 'BOT_TOKEN'` on startup
`.env` isn't being read, or the variable name is wrong. Check:
- Is `.env` in the same directory as `bot.py`?
- Does it contain `BOT_TOKEN=<value>` (no quotes, no spaces around `=`)?
- Permissions: `ls -l .env` should be readable by the bot's user.

### `Unauthorized` / `401` from Telegram
The token is wrong, was revoked, or was regenerated. Re-check the token in
BotFather (`/mybots` → your bot → API Token) and update `.env`.

### `TelegramConflictError: terminated by other getUpdates request`
**Another process is polling the same bot token.** Telegram only allows one
polling client per token. Common causes:
- You left the foreground smoke-test running and also started pm2/systemd.
- Two servers (e.g. an old VPS and a new one) both have the bot enabled.
- A teammate has the same token in their `.env` for testing.

Find and stop the other one. The bot library handles this gracefully —
exponential backoff, no crash — but it cannot receive updates until the
conflict clears.

### Python version too old
`bot.py` uses `match`/`case` and modern asyncio idioms. Anything below 3.10
will fail at import time. Fix via Path B's portable Python.

### `ModuleNotFoundError: No module named 'aiogram'` (or similar)
You ran `python bot.py` with the system Python instead of the venv. Use
`.venv/bin/python bot.py`, or activate the venv first
(`source .venv/bin/activate`).

### `Permission denied (publickey,password)` from SSH
You're trying to SSH into your VPS. Check the username, port, and whether
the host accepts password auth at all. On Scalahosting the default SSH port
is often non-22 (e.g. 6543).

### BotFather rejects your token format
You probably pasted the **token name** instead of the token itself. The token
looks like `<digits>:<random>`. Re-issue from BotFather (`/token`) if needed.

### Bot accepts `/start` but `/balance` says "user not found"
The database was reset or the bot was pointed at a different `ubi_bot.db`.
Send `/start` again to register on the current node. If you migrated from
another node, you need to physically copy `ubi_bot.db` across (bot stopped
during the swap).

### pm2 not surviving reboot
Three failure modes:
1. You forgot `pm2 save` after starting the app — `dump.pm2` is empty/stale.
2. The `@reboot` cron entry is missing or has a wrong path to `pm2`.
3. cron itself is disabled on the host (rare). Verify with
   `systemctl status crond` (root needed) or by asking the hoster.

### `apscheduler` errors about timezones
Make sure `pytz` installed cleanly: `.venv/bin/pip install -U pytz`.

### Logs show normal startup but `/start` is ignored
- TelegramConflictError (above) is the #1 cause.
- Less common: a firewall blocking outbound HTTPS to
  `api.telegram.org`. Test with
  `curl -sI https://api.telegram.org/bot<your-token>/getMe`.

---

## File layout reference

After a clean install your bot directory looks like:

```
ubi-bot/
├── .env                     # secrets, mode 600, NEVER committed
├── .env.example             # template (committed)
├── .gitignore
├── .venv/                   # virtualenv (not committed)
├── LICENSE
├── README.md                # project overview
├── DEPLOY.md                # this file
├── bot.py                   # main entry — all handlers + scheduler
├── config.py                # loads env, defines constants
├── database.py              # async SQLite layer
├── wallet.py                # time formatting, handle utilities
├── requirements.txt
├── start.sh                 # legacy launcher; not needed for the flows above
├── ubi-bot.service          # systemd template for Path A
├── ecosystem.config.cjs     # pm2 config for Path B (create yourself)
└── ubi_bot.db               # created on first run, NEVER committed
```

---

## Questions, bugs, contributions

Open issues and PRs at <https://github.com/UBIworld/time>. Protocol-level
discussion belongs at [ubi.world](https://ubi.world).
