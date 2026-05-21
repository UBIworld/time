# UBI World Telegram Bot

First functional implementation of the [ubi.world](https://ubi.world) time-based Universal Basic Income protocol.

Bot: [@timeubibot](https://t.me/timeubibot)

---

## What This Is

A Telegram bot that runs the UBI World time protocol. Every registered user receives 24 hours per day in their Daily Wallet. Users can send time to each other, carry earned time in a Time Vault, and unspent Wallet time flows to Universal Circles at midnight UTC.

See [ubi.world](https://ubi.world) for the full protocol specification.

---

## Requirements

- Python 3.10 or higher
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/UBIworld/time.git
cd time
```

### 2. Create your environment file

```bash
cp .env.example .env
```

Open `.env` and fill in:

- `BOT_TOKEN` — your token from @BotFather
- `ADMIN_TELEGRAM_ID` — your Telegram user ID (message [@userinfobot](https://t.me/userinfobot) to find it)

### 3. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Run the bot

```bash
python bot.py
```

The bot starts in polling mode — no webhook or server required. The SQLite database (`ubi_bot.db`) is created automatically on first run.

---

## Getting a Bot Token

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the token BotFather gives you into your `.env` file

---

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Register and create your Handle (e.g. `house:cat:888`) |
| `/balance` | Show Daily Wallet + Time Vault |
| `/send @user 2h 30m` | Send time to another user by Telegram username |
| `/send house:cat:888 2h 30m` | Send time by Handle |
| `/send @user 1h blue:80` | Send time with 80% Blue feedback |
| `/history` | Recent transactions |
| `/handle` | Show your Handle |
| `/help` | Command reference |

---

## How It Works

- Every user gets **24 hours** in their Daily Wallet at midnight UTC
- Each user has a **Handle** in the form `slot:slot:slot` (e.g. `house:cat:888`) — the cross-node identity
  - The parser also accepts an optional `@node.domain` suffix (e.g. `house:cat:888@cat.ubi.asia`) for forward-compatibility with federation
- Send time to other users — it goes into their **Time Vault**
- Unspent Wallet time flows to **Universal Circles** at midnight
- Time Vault holds time received from others (max 24h for Tier 1)
- System draws from Wallet first, then Vault
- Every transfer carries a **Blue/Red** satisfaction signal (default 100% Blue)
- Time displayed as `HHh MMm SSs` — nothing rounded

---

## Project Structure

```
ubi-bot/
  bot.py              Main entry point, all command handlers, scheduler
  database.py         SQLite schema and async DB operations
  wallet.py           Time formatting, parsing, handle utilities
  config.py           Bot token and settings (loaded from .env)
  requirements.txt    Python dependencies
  .env.example        Template for environment variables
  ubi-bot.service     Example systemd unit for production deployment
  README.md           This file
```

---

## Production Deployment (systemd)

`ubi-bot.service` is included as a reference template. Adjust paths and the `EnvironmentFile=` directive to point at your secrets file. The bot handles `SIGTERM` gracefully and restarts on failure.

---

## Tech Stack

- **aiogram 3.x** — async Telegram bot framework
- **aiosqlite** — async SQLite access
- **APScheduler** — daily reset cron job
- **python-dotenv** — environment variable loading
- **Python 3.10+** — async/await throughout

---

## Key Design Rules (from ubi.world spec)

1. No conversion between time and money
2. Daily Wallet expires at midnight — unspent feeds community
3. Self-deposit impossible — cannot move own Wallet into own Vault
4. Vault does not expire — earned time persists
5. System draws from Wallet first, Vault only when Wallet insufficient
6. Every transfer carries Blue/Red feedback
7. Time denominated as HHh MMm SSs — nothing rounded

---

Protocol documentation: [ubi.world](https://ubi.world)
