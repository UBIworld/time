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
_env_path = Path(__file__).parent / ".env"
load_dotenv(_env_path, override=True)

# Telegram Bot Token — loaded from environment, never hardcoded.
# Set via EnvironmentFile in the systemd unit, or export BOT_TOKEN= locally.
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Admin Telegram ID — used for hidden admin commands (e.g. /reboot).
# Set ADMIN_TELEGRAM_ID in the env file. Raises at startup if missing.
ADMIN_TELEGRAM_ID = int(os.environ["ADMIN_TELEGRAM_ID"])

# Database
DB_PATH = "ubi_bot.db"

# Federation — local node domain (user-facing, e.g. "cat.ubi.asia").
# This is the domain that gets appended after the `@` when a handle from this
# node is rendered to a remote node, and the value populated into
# users.node_domain at registration time.
#
# Default is "localhost" so dev/test environments work out of the box. In
# production set LOCAL_NODE_DOMAIN in the systemd EnvironmentFile (the live
# bot at cat.ubi.asia sets this to "cat.ubi.asia").
#
# Federation transport (HTTP+JSON+Ed25519 vs. Avalanche, etc.) is NOT decided
# yet — this value only controls handle rendering and the local schema fields
# and is architecture-agnostic.
LOCAL_NODE_DOMAIN = os.environ.get("LOCAL_NODE_DOMAIN", "localhost")

# Federation (stage 2a) — where to persist this node's Ed25519 keypair.
#
# Defaults to ~/.ubi-bot/ so keys live OUTSIDE the repo (and outside any
# git-tracked path) by default. Operators on shared hosting where $HOME
# isn't writable from the bot process can override to any directory the
# bot can read+write — typically alongside .env.
#
# The directory will be created mode 700 on first run if it doesn't
# exist; the private key file inside it is created mode 600.
NODE_KEY_DIR = os.path.expanduser(
    os.environ.get("NODE_KEY_DIR", "~/.ubi-bot")
)

# Federation HTTP server port. The bot embeds an aiohttp app alongside
# aiogram's long-polling loop to serve `/.well-known/ubi-node` (and in
# stage 2b, the signed transfer endpoints). Defaults to 8765.
# Operators behind a reverse proxy / shared-hosting panel must route their
# public 443/80 to this port — see DEPLOY.md "Federation: cryptographic
# identity & discovery".
FEDERATION_HTTP_PORT = int(os.environ.get("FEDERATION_HTTP_PORT", "8765"))

# Federation HTTP bind address. Default 0.0.0.0 lets a reverse proxy on
# another host reach the listener. Operators who run nginx/Caddy on the
# same machine and want the federation listener loopback-only can set
# this to 127.0.0.1.
FEDERATION_HTTP_HOST = os.environ.get("FEDERATION_HTTP_HOST", "0.0.0.0")

# Federation spec version published in `/.well-known/ubi-node`. Bump on
# incompatible wire-format changes.
FEDERATION_SPEC_VERSION = "ubi-fed-1.0"

# Time constants (seconds)
DAILY_WALLET_AMOUNT = 86400    # 24h = 86,400 seconds
VAULT_CAPACITY_TIER1 = 86400  # 24h for Tier 1

# Daily reset timezone (UTC for MVP)
RESET_TIMEZONE = "UTC"

# Default feedback
DEFAULT_BLUE_PCT = 100

# History display limit
HISTORY_LIMIT = 10
