"""
Web app authentication, shared by bot.py (issues login links) and webapp.py
(validates them and keeps a session cookie).

Two ways in:
  1. Magic link from the bot: /app -> one-time token -> /login/<token>.
     Works on the tailnet with no public hostname and no BotFather setup.
  2. Telegram Mini App initData (HMAC-SHA256 with the bot token), for when
     the same UI runs inside Telegram. Validated by verify_webapp_init_data().

Sessions are a signed cookie: "<telegram_id>.<expires>.<hmac>", keyed with a
random secret kept next to the node keys.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl

import aiosqlite

import config
from database import DB_PATH

LOGIN_TOKEN_TTL = 10 * 60          # seconds a magic link stays valid
SESSION_TTL = 30 * 86400           # seconds a browser session stays valid
WEBAPP_INITDATA_MAX_AGE = 86400    # Telegram initData older than this is refused


# ---------------------------------------------------------------------------
# Secret
# ---------------------------------------------------------------------------

_SECRET: Optional[bytes] = None


def _secret() -> bytes:
    global _SECRET
    if _SECRET is not None:
        return _SECRET
    d = Path(config.NODE_KEY_DIR).expanduser()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = d / "web_session_secret"
    if not p.exists():
        p.write_bytes(secrets.token_bytes(32))
        os.chmod(p, 0o600)
    _SECRET = p.read_bytes()
    return _SECRET


# ---------------------------------------------------------------------------
# Magic-link tokens (table shared by bot and web processes)
# ---------------------------------------------------------------------------

async def init_webauth_schema() -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS web_login_tokens (
                token        TEXT PRIMARY KEY,
                telegram_id  INTEGER NOT NULL,
                expires_at   INTEGER NOT NULL,
                used         INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.commit()


# Codes are typed by hand, so: short, upper-case, no 0/O/1/I. 8 chars of a
# 32-symbol alphabet is ~40 bits; a code lives 10 minutes and is burned on use.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _new_code() -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize_code(text: str) -> str:
    # 0/O and 1/I are not in the alphabet; map the look-alikes people type.
    t = "".join(ch for ch in text.upper() if ch.isalnum()).replace("0", "O").replace("1", "I")
    return f"{t[:4]}-{t[4:8]}" if len(t) >= 8 else t


async def create_login_token(telegram_id: int) -> str:
    """One-time login code. The bot shows it; the user types it in the browser.
    The code never contains the app's address, so nothing about where the
    app lives passes through Telegram."""
    token = _new_code()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM web_login_tokens WHERE expires_at < ?", (int(time.time()),)
        )
        await db.execute(
            "INSERT INTO web_login_tokens (token, telegram_id, expires_at) VALUES (?, ?, ?)",
            (token, telegram_id, int(time.time()) + LOGIN_TOKEN_TTL),
        )
        await db.commit()
    return token


async def consume_login_token(token: str) -> Optional[int]:
    """Return the telegram_id for a valid unused token and burn it."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "UPDATE web_login_tokens SET used = 1 "
            "WHERE token = ? AND used = 0 AND expires_at >= ? RETURNING telegram_id",
            (token, int(time.time())),
        )
        row = await cursor.fetchone()
        await db.commit()
    return int(row[0]) if row else None


# ---------------------------------------------------------------------------
# Session cookie
# ---------------------------------------------------------------------------

def make_session_cookie(telegram_id: int) -> str:
    exp = int(time.time()) + SESSION_TTL
    body = f"{telegram_id}.{exp}"
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session_cookie(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        tid, exp, sig = value.split(".")
        body = f"{tid}.{exp}"
        good = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        if int(exp) < time.time():
            return None
        return int(tid)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Telegram Mini App initData
# ---------------------------------------------------------------------------

def verify_webapp_init_data(init_data: str) -> Optional[int]:
    """Validate Telegram WebApp initData; return the user's telegram_id or None."""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received = pairs.pop("hash", None)
        if not received:
            return None
        check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        if time.time() - int(pairs.get("auth_date", "0")) > WEBAPP_INITDATA_MAX_AGE:
            return None
        import json
        return int(json.loads(pairs["user"])["id"])
    except Exception:
        return None
