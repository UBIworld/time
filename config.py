"""
UBI Bot — Configuration
All settings in one place. MVP keeps it simple.

Sensitive values (BOT_TOKEN, ADMIN_TELEGRAM_ID) are loaded from a .env file
via python-dotenv. Copy .env.example to .env and fill in real values before
running.

Using load_dotenv() at startup ensures values are always read fresh from the
file — this is critical after os.execv() reboots, where the new process
inherits the parent's (possibly stale) environment rather than a clean shell.

For production (systemd): set EnvironmentFile= in your service unit pointing
to your secrets file, and load_dotenv() will be a no-op (env vars already set).
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the same directory as this file.
# override=True ensures file values win over stale inherited env vars.
# In production with systemd EnvironmentFile=, the vars are already set
# and this call is safely a no-op.
# UBI_ENV_FILE lets a second instance (staging) run from the same checkout
# with its own token and database, e.g. UBI_ENV_FILE=.env.staging.
_env_path = Path(__file__).parent / os.environ.get("UBI_ENV_FILE", ".env")
load_dotenv(_env_path, override=True)

# Telegram Bot Token — loaded from environment, never hardcoded.
# Set via EnvironmentFile in the systemd unit, or export BOT_TOKEN= locally.
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Admin Telegram ID — used for hidden admin commands (e.g. /reboot).
# Set ADMIN_TELEGRAM_ID in the env file. Raises at startup if missing.
ADMIN_TELEGRAM_ID = int(os.environ["ADMIN_TELEGRAM_ID"])

# Database — override with DB_PATH so staging and production never share a file.
DB_PATH = os.environ.get("DB_PATH", "ubi_bot.db")

# Federation — local node domain (user-facing, e.g. "cat.ubi.asia").
# Stamped onto every signed ledger entry so any reader can tell which
# node originated a transaction. Defaults to "localhost" so dev works
# out of the box. The live bot sets this to "cat.ubi.asia".
LOCAL_NODE_DOMAIN = os.environ.get("LOCAL_NODE_DOMAIN", "localhost")

# Ledger root — where signed transaction JSONs are written and the
# generated explorer HTML lives. In production this is
# /home/vrgli/ubi.world/ledger (so Apache serves the static pages
# directly). If LEDGER_ROOT is unset, ledger writes are skipped with
# an INFO log — useful for local dev. The ledger is a mirror of the
# DB, never the source of truth.
LEDGER_ROOT = os.environ.get("LEDGER_ROOT", "").strip() or None

# Node signing keys — Ed25519 keypair used to sign ledger entries.
# Convention matches Stage 2a federation work: a directory containing
# node_private_key.pem (mode 600) and node_public_key.pem. Generated
# lazily on first ledger write if not present.
NODE_KEY_DIR = os.path.expanduser(
    os.environ.get("NODE_KEY_DIR", "~/.ubi-bot")
)

# Web app (webapp.py): public base URL used in /app login links; empty = off.
WEB_PUBLIC_URL = os.environ.get("WEB_PUBLIC_URL", "").strip().rstrip("/")

# Time constants (seconds)
DAILY_WALLET_AMOUNT = 86400    # 24h = 86,400 seconds
VAULT_CAPACITY_TIER1 = 86400  # 24h for Tier 1

# Daily reset timezone (UTC for MVP)
RESET_TIMEZONE = "UTC"

# History display limit
HISTORY_LIMIT = 10
